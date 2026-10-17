# -*- coding: utf-8 -*-
"""定时任务调度（APScheduler）—— 四个任务：

1. ``auto-incremental-sync``  定时增量同步
   间隔取 .env 的 ``SYNC_INTERVAL_MINUTES``（分钟），0 表示关闭。
   运行期可通过 ``POST /api/sync/schedule`` 动态调整。

2. ``credential-keepalive``   凭据保活
   间隔取 .env 的 ``KEEPALIVE_INTERVAL_MINUTES``（分钟，默认 360），0 表示关闭。

3. ``auto-scoring``           定时增量评分
   间隔取 .env 的 ``SCORING_INTERVAL_MINUTES``（分钟），0 表示关闭。

4. ``data-retention``        数据保留清理（架构 边界4）
   间隔取 ``RETENTION_INTERVAL_MINUTES``（默认 720 = 12 小时），0 表示关闭。
   **实际清理天数由 `data_retention_days` 决定，默认 0 = 不清理**——
   保留多久是业务决策，不能由代码替业务拍板。只清运行观测类表，不碰业务结论（见 retention.py）。

5. ``writeback-outbox-drain`` 回写 outbox 排空（阶段B）
   固定 30 秒一次。把「回写微伴」从评分主流程解耦：即使会话早已因增量水位
   退出候选集，失败的待回写任务仍会被重试（修 G6）。

为什么需要保活？
    微伴后台没有账号密码登录（只能企微扫码），但登录态是 **滑动过期** 的：
    每次访问后台，``session_id`` 的过期时间会被重置为「当前时间 + 7 天」。
    所以定期访问一次即可让凭据永不过期，无需人工重新扫码。
    详见 ``wb_keepalive.py``。
"""
from datetime import datetime, timedelta

import json
import re
import time

import audit
import config
import db
import job_service            # 互斥判定（running_kinds）与跑批骨架的唯一来源
from apscheduler.schedulers.background import BackgroundScheduler

from scoring_service import service as scoring
from sync_service import service

SYNC_JOB_ID = "auto-incremental-sync"
KEEPALIVE_JOB_ID = "credential-keepalive"
SCORING_JOB_ID = "auto-scoring"
OUTBOX_JOB_ID = "writeback-outbox-drain"
RETENTION_JOB_ID = "data-retention"
FAILURE_JOB_ID = "session-failure-retry"
MORNING_JOB_ID = "morning-sa-review"
GRADE_TAG_JOB_ID = "grade-tag-drain"
REAP_JOB_ID = "idempotency-reap"

OUTBOX_INTERVAL_SECONDS = 30

# ---------------- 日常任务（需求 4.2）----------------
#: 「每晚 20:00–20:20 主增量同步 + 分析」「每日 03:30 48 小时补偿同步 + 分析」。
#: 三个 id：主任务 / 补偿任务 / 看门狗（推进「同步 → 分析」链条）。
DAILY_MAIN_JOB_ID = "daily-main"
DAILY_COMPENSATE_JOB_ID = "daily-compensate"
DAILY_WATCH_JOB_ID = "daily-watch"
EVAL_REGRESSION_JOB_ID = "eval-regression"
#: 看门狗节拍（秒）。链条推进要「同步一结束就起分析」，所以查得勤一点。
DAILY_WATCH_SECONDS = 30
#: 链条状态存在 app_setting（跨进程可见、重启后仍在）——**不放在内存里**，
#: 否则后端重启会让「同步跑完了，但没人去起分析」这件事无声发生。
DAILY_STATE_KEY = "daily_run_state"

_scheduler: BackgroundScheduler | None = None

#: 三个「按分钟间隔」任务的当前值（分钟，0 = 关闭）。
#: 改造前是 `_interval` / `_ka_interval` / `_sc_interval` 三个平行全局变量，
#: 外加三份**逐字相同**的 `_read_*_interval()` 与三份一模一样的 `set_*_interval()`
#: —— 「换个来源读配置」要改三处，漏一处就是三档间隔行为不一致。
_intervals: dict = {"sync": 0, "keepalive": 0, "scoring": 0}

#: 三个间隔的读取来源（配置键 → 默认值）。取值只在这张表里定义。
INTERVAL_SPECS: dict = {
    "sync": ("SYNC_INTERVAL_MINUTES", 0),
    "keepalive": ("KEEPALIVE_INTERVAL_MINUTES", 360),
    "scoring": ("SCORING_INTERVAL_MINUTES", 0),
}


def _cfg_int(key: str, default: int) -> int:
    """读一个整数配置；**缺失或非法都用 default**（配置写错不该让调度器起不来）。"""
    try:
        return int(config.get(key) or default)
    except Exception:                              # noqa: BLE001
        return default


def interval_minutes(kind: str) -> int:
    """某个定时任务的当前间隔（分钟，0 = 关闭）。"""
    return int(_intervals.get(kind) or 0)


def guard_can_start(owner: str) -> bool:
    """共享互斥守卫（修 H1）：同步与评分**不得同时运行**。

    为什么必须互斥：评分要读 ``chat_message``；若同步正在往里写，评分会读到
    「半截会话」（``msg_count`` / ``last_msg_time`` 都还在变），结论因此不可复现。

    改造前的问题：``_job``（同步）只查**同步自己**在不在跑，压根不查评分 ——
    于是「评分跑着，同步又启动」这条路没被堵住；而 ``_scoring_job`` 两边都查了。
    两个 job 的守卫口径不一致，收敛成同一个入口后才算真的互斥。

    返回 True = 可以启动；False = 应跳过（原因已打印）。
    """
    # 判定统一走 `job_service.running_kinds()`（**查库、跨进程可见**）：
    # 改造前这里直接调两个服务的 `is_running()`，与两个 `start()` 内的互斥判定
    # 是**三处各写一遍**的同一不变量，口径还略有出入。
    busy = set(job_service.running_kinds())
    busy_sync, busy_scoring = "sync" in busy, "scoring" in busy
    if owner == "sync":
        if busy_sync:
            print("[scheduler] 已有同步在跑，本次跳过")
            return False
        if busy_scoring:
            print("[scheduler] 评分进行中，本次同步跳过（避免读到半截数据）")
            return False
        return True
    if owner == "scoring":
        if busy_scoring:
            print("[scheduler] 已有评分任务在跑，本次跳过")
            return False
        if busy_sync:
            print("[scheduler] 同步进行中，本次评分跳过（避免读到半截数据）")
            return False
        return True
    return True


def _job():
    """定时任务体：增量同步（与评分互斥，见 :func:`guard_can_start`）。"""
    if not guard_can_start("sync"):
        return
    print("[scheduler] 触发定时增量同步")
    service.start(kind="incremental", triggered_by="schedule",
                  max_sessions_per_staff=200)


def _keepalive_job():
    """定时任务体：凭据保活（滑动过期，定期访问即续期）。"""
    if service.is_running():
        print("[scheduler] 同步进行中，本次保活跳过")
        return
    try:
        import wb_keepalive
        res = wb_keepalive.ensure_credentials(source="schedule")
        flag = "✅" if res.get("ok") else "❌"
        extra = "（无需刷新，已跳过）" if res.get("skipped") else ""
        print(f"[scheduler] 凭据保活 {flag} {res.get('message')}{extra}")
    except Exception as e:
        print(f"[scheduler] 凭据保活异常：{type(e).__name__}: {e}")


def _scoring_job():
    """定时任务体：增量评分（与同步互斥，见 :func:`guard_can_start`）。"""
    if not guard_can_start("scoring"):
        return
    print("[scheduler] 触发定时评分")
    scoring.start(triggered_by="schedule")


def _outbox_job():
    """定时任务体：排空回写 outbox（修 G6）。

    独立于评分与增量水位：只要 outbox 里还有 pending/failed 的到期任务就重试，
    即使该会话早已退出候选集也不会漏掉。总开关见 `use_outbox`。
    """
    try:
        import outbox
        res = outbox.drain(limit=50)
        if res.get("checked"):
            print(f"[scheduler] 回写 outbox：{res}")
    except Exception as e:
        print(f"[scheduler] 回写 outbox 异常：{type(e).__name__}: {e}")


def _retention_job():
    """定时任务体：按 `data_retention_days` 清理运行观测类数据。

    默认 `data_retention_days=0` → 直接返回（不清理）。这是刻意的：
    「保留多少天」是业务决策，代码不能替业务拍板，更不能默认删数据。
    """
    try:
        import retention
        days = scoring.setting_int("data_retention_days", "DATA_RETENTION_DAYS", 0)
        if days <= 0:
            return
        res = retention.purge(days, dry_run=False)
        print(f"[scheduler] 数据保留清理（{days} 天）：{res.get('deleted')}")
    except Exception as e:
        print(f"[scheduler] 数据保留清理异常：{type(e).__name__}: {e}")


def _failure_job():
    """定时任务体：重投失败会话（修 G5「连续失败 ≥N 转人工」）。

    评分失败后**没有任何东西会再试它** —— 会话就一直没被评过。
    本 job 每 15 分钟挑一批到期的 pending 重投，达阈值标 dead 并转人工。
    """
    try:
        import failure_worker
        res = failure_worker.drain(limit=20)
        if res.get("resolved") or res.get("dead") or res.get("marked_manual"):
            print(f"[scheduler] 失败重投：{res}")
    except Exception as e:
        print(f"[scheduler] 失败重投异常：{type(e).__name__}: {e}")


def _morning_job():
    """早会前复核 S/A（架构 §2.1 F2 / D3：「早会时刻 UI 可配」）。

    触发时刻来自 `scoring_morning_cron`（HH:MM）；**空 = 关闭**。
    走**增量刷新**（force=False）+ 只扫指定等级 —— 符合 D3 拍板的「复核走增量刷新」。
    """
    try:
        scope = db.setting("public_class_grade_scope", "PUBLIC_CLASS_GRADE_SCOPE", "S,A")
        grades = [g.strip().upper() for g in str(scope).split(",") if g.strip()]
        if not guard_can_start("scoring"):
            print("[scheduler] 早会复核跳过：已有任务在运行")
            return
        print(f"[scheduler] 早会复核（{','.join(grades)}）")
        scoring.start(triggered_by="schedule", grades=grades)
    except Exception as e:
        print(f"[scheduler] 早会复核异常：{type(e).__name__}: {e}")


def _reap_job():
    """定时任务体：清理过期幂等记录（修 P2-1）。

    ``idempotency.reap()`` 早就写好了，但全仓库**只有定义、没有调用方** ——
    叠加 ``data_retention_days`` 默认 0 不清理，``request_guard`` 实际**永不清理**。
    这类「防御措施存在但没接线」的状态最容易误导：看代码的人会以为已经生效。
    这里把它接上定时任务。
    """
    try:
        import idempotency
        n = idempotency.reap()
        if n:
            print(f"[scheduler] 幂等记录清理：{n} 条")
    except Exception as e:
        print(f"[scheduler] 幂等记录清理异常：{type(e).__name__}: {e}")


def _grade_tag_job():
    """定时任务体：排空「等级标签」outbox（D2-1，**与分数回写 outbox 独立的 job**）。

    与分数回写的分工见 `backend/grade_tag.py`。**开关默认 off** ——
    关着时 drain 只把任务延后（保持 pending），不会调用微伴。
    """
    try:
        import grade_tag
        if not grade_tag.enabled():
            return                       # 关着就完全不碰（连 DB 都不查）
        res = grade_tag.drain(limit=50)
        if res.get("ok") or res.get("failed") or res.get("dry_run") or res.get("deferred"):
            print(f"[scheduler] 等级标签回写：{res}")
    except Exception as e:
        print(f"[scheduler] 等级标签回写异常：{type(e).__name__}: {e}")


def start():
    global _scheduler
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _scheduler.start()
    for kind, (key, default) in INTERVAL_SPECS.items():
        _intervals[kind] = _cfg_int(key, default)
    _reschedule()
    _reschedule_keepalive()
    _reschedule_scoring()
    _reschedule_outbox()
    _reschedule_retention()
    _reschedule_failure()
    _reschedule_morning()
    _reschedule_grade_tag()
    _reschedule_reap()
    _reschedule_daily()
    _reschedule_eval_regression()
    try:
        _mo = db.setting('scoring_morning_cron', 'SCORING_MORNING_CRON', '') or '关闭'
        _rt = db.setting_int('data_retention_days', 'DATA_RETENTION_DAYS', 0)
    except Exception:
        _mo, _rt = '关闭', 0
    print(f"[scheduler] 已启动｜同步 {interval_minutes('sync') or '关闭'} 分"
          f"｜保活 {interval_minutes('keepalive') or '关闭'} 分"
          f"｜评分 {interval_minutes('scoring') or '关闭'} 分"
          f"｜回写 outbox {OUTBOX_INTERVAL_SECONDS} 秒"
          f"｜失败重投 {int(config.get('FAILURE_RETRY_INTERVAL_MINUTES') or 15)} 分"
          f"｜保留期 {('关闭' if _rt <= 0 else str(_rt) + ' 天')}"
          f"｜早会复核 {_mo}")
    # 等级标签回写（D2-1）：与分数回写**分开的开关与 job**，启动时把状态打出来 ——
    # 「到底会不会写微伴」是运维最该一眼看清的事，不能靠翻配置猜。
    try:
        import grade_tag as _gt
        _gm = _gt.mode()
        print(f"[scheduler] 等级标签：{_gm}"
              + (f"｜标签组「{_gt.tag_group()}」｜已缓存 {len(_gt.get_tag_ids())} 个 id"
                 if _gm != "off" else "｜（关闭，不会调用微伴）"))
    except Exception:
        pass


def _reschedule_interval(job_id: str, fn, *, minutes: int = 0, seconds: int = 0,
                         first_run_after=None) -> None:
    """注册 / 重排一个 interval 任务：**先摘旧的再挂新的**（幂等）。

    `minutes` 与 `seconds` 都 <= 0 → 只摘不挂，等于关闭该任务。

    ⚠️ 统一带 `max_instances=1, coalesce=True`：同一任务不并发、错过的那次不补跑。
    改造前只有同步 / 评分 / 保活带了这两个参数，清理类任务（reap、保留期、
    失败重投、标签排空）没带 —— 一旦某次跑得比间隔还久就会**叠起来**，
    对同一个库重复做清理。同一个不变量只该写一次。
    """
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    mins, secs = int(minutes or 0), int(seconds or 0)
    if mins <= 0 and secs <= 0:
        return
    kw = {"minutes": mins} if mins > 0 else {"seconds": secs}
    if first_run_after is not None:
        kw["next_run_time"] = first_run_after
    _scheduler.add_job(fn, "interval", id=job_id, replace_existing=True,
                       max_instances=1, coalesce=True, **kw)


def _reschedule_reap():
    """幂等记录清理任务（默认 60 分钟一次；0 = 关闭）。"""
    _reschedule_interval(REAP_JOB_ID, _reap_job,
                         minutes=_cfg_int("REAP_INTERVAL_MINUTES", 60))


def _reschedule_grade_tag():
    """标签排空任务（`GRADE_TAG_INTERVAL_SECONDS`，默认 30s；0 = 关闭）。"""
    _reschedule_interval(GRADE_TAG_JOB_ID, _grade_tag_job,
                         seconds=_cfg_int("GRADE_TAG_INTERVAL_SECONDS", 30))


# ============================================================ 日常任务（需求 4.2）

def _daily_settings() -> dict:
    """日常任务的运行时配置（app_setting > .env > 默认）。**改完立即生效**，不必重启。"""
    return {
        "main_cron": str(db.setting("sync_main_cron", "SYNC_MAIN_CRON", "20:00") or "").strip(),
        "window": db.setting_int("sync_main_window_minutes", "SYNC_MAIN_WINDOW_MINUTES", 20),
        "comp_cron": str(db.setting("sync_compensate_cron", "SYNC_COMPENSATE_CRON",
                                    "03:30") or "").strip(),
        "lookback": db.setting_int("sync_compensate_lookback_hours",
                                   "SYNC_COMPENSATE_LOOKBACK_HOURS", 48),
        "analysis": db.setting_bool("daily_run_analysis", "DAILY_RUN_ANALYSIS", True),
    }


def _parse_hhmm(text: str):
    """`"20:00"` → (20, 0)；空串或不合法 → None（= 关闭该任务）。"""
    m = re.match(r"^(\d{1,2}):(\d{2})$", (text or "").strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    return (hh, mm) if 0 <= hh <= 23 and 0 <= mm <= 59 else None


def _daily_state() -> dict:
    """读链条状态（app_setting 里的 JSON）。坏数据按「没有」处理。"""
    raw = db.get_setting(DAILY_STATE_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:                              # noqa: BLE001
        return {}


def _set_daily_state(state: dict) -> None:
    db.set_setting(DAILY_STATE_KEY, json.dumps(state or {}, ensure_ascii=False) if state else "")


def request_stop(table: str, row_id: int = None) -> None:
    """请求停止某个跑批任务（**写 DB 标志，跨进程生效** —— 与人工点「停止」同一条路）。

    日常任务的窗口收尾就用它：到点后不再新起会话，跑完当前这个就退出，
    然后把控制权交给链条的下一步（分析）。
    """
    if row_id:
        db.execute(f"UPDATE {table} SET stop_requested=true"
                   f" WHERE id=%s AND status='running'", (row_id,))
    else:
        db.execute(f"UPDATE {table} SET stop_requested=true"
                   f" WHERE id = (SELECT id FROM {table} WHERE status='running'"
                   f" ORDER BY id DESC LIMIT 1)")


def _start_daily(phase: str, *, lookback_hours: int = None, window_minutes: int = None) -> None:
    """日常任务的第一步：起同步，并把「下一步该起分析」写进状态。"""
    if not guard_can_start("sync"):
        return                                          # 原因已由 guard 打印
    r = service.start(kind="incremental", triggered_by=f"schedule-{phase}",
                      lookback_hours=lookback_hours)
    if not r.get("ok"):
        print(f"[scheduler] 日常任务（{phase}）启动被拒：{r.get('reason')}")
        return
    _set_daily_state({"phase": phase, "step": "sync", "job_id": r["job_id"],
                      "started_at": time.time(),
                      "window_minutes": int(window_minutes or 0),
                      "lookback_hours": lookback_hours})
    print(f"[scheduler] 日常任务（{phase}）已启动：同步 job={r['job_id']}"
          + (f"｜窗口 {window_minutes} 分钟" if window_minutes else "")
          + (f"｜回看 {lookback_hours} 小时" if lookback_hours else ""))


def _daily_main_job():
    """每日主任务（默认 20:00）：主增量同步 → 分析（需求 4.2 第一行）。"""
    cfg = _daily_settings()
    _start_daily("main", window_minutes=cfg["window"])


def _daily_compensate_job():
    """每日补偿任务（默认 03:30）：回看 48 小时重拉 + 分析（需求 4.2 第二行）。"""
    cfg = _daily_settings()
    _start_daily("compensate", lookback_hours=cfg["lookback"])


def _daily_watch_job():
    """链条看门狗（每 30 秒）：推进「同步 → 分析」，并守住执行窗口。

    为什么需要它：同步与评分是**同一条链**（先同步拿到最新消息，再基于它评分），
    而两者又必须互斥。这里把「同步跑完再起评分」做成显式的状态机，状态存
    `app_setting`（跨进程可见、重启后仍在）—— 后端重启不会让链条半路失踪。
    """
    state = _daily_state()
    if not state:
        return
    # 超时兜底：状态挂了超过 12 小时一律清掉，避免残留状态把后续判断带偏
    if time.time() - float(state.get("started_at") or 0) > 12 * 3600:
        print("[scheduler] 日常任务状态过期（>12 小时），已清理")
        _set_daily_state({})
        return

    step = state.get("step")
    if step == "sync":
        # 窗口到点 → 请求停止（同步会在当前会话结束后优雅退出，状态落 stopped）
        win = int(state.get("window_minutes") or 0)
        if win and (time.time() - float(state.get("started_at") or 0)) > win * 60:
            request_stop("sync_job", state.get("job_id"))
            state["stop_requested_at"] = time.time()
            _set_daily_state(state)
            return
        if service.is_running():
            return
        if not _daily_settings()["analysis"]:
            print("[scheduler] 日常任务：同步结束（未开启自动分析，链条到此为止）")
            _set_daily_state({})
            return
        # 链条第③步（兴趣点分析）还在跑时**不能**在这里启动评分：两者互斥，评分会被拒。
        # 但这种情况**不能**像其它拒绝那样"到此为止" —— 那是在等我们自己链上的活干完，
        # 该保留状态、下个 tick 再看（否则 20:00 启动的长兴趣分析，会把 03:30 那一轮
        # 评分静默跳过一整天）。这是本链内部的自阻塞，与"别的批次占着库"不同。
        import job_service
        if "interest" in job_service.running_kinds():
            return
        r = scoring.start(triggered_by=f"schedule-{state.get('phase')}")
        if not r.get("ok"):
            print(f"[scheduler] 日常任务：分析启动被拒：{r.get('reason')}")
            _set_daily_state({})
            return
        state.update({"step": "scoring", "scoring_run_id": r.get("run_id"),
                      "scoring_started_at": time.time()})
        _set_daily_state(state)
        print(f"[scheduler] 日常任务（{state.get('phase')}）：同步结束 → 已启动分析 "
              f"run={r.get('run_id')}")
    elif step == "scoring":
        if scoring.is_running():
            return
        # ── 链条第③步：兴趣点增量（2026-10-16）────────────────────────────
        # 为什么挂在链尾而不是独立 cron：它只读消息、只调模型，天然适合"数据刚更新完
        # 顺便跑一遍"。开关 `interest_auto_run`（默认开）；关掉就只在总览页手动触发。
        # ⚠️ 与评分**互斥**（都吃模型配额），所以必须等评分真的结束（上面的 is_running 判断）。
        if db.setting_bool("interest_auto_run", "INTEREST_AUTO_RUN", True):
            import interest_service
            r = interest_service.service.start(triggered_by=f"schedule-{state.get('phase')}")
            if r.get("ok"):
                state.update({"step": "interest", "interest_run_id": r.get("run_id"),
                              "interest_started_at": time.time()})
                _set_daily_state(state)
                print(f"[scheduler] 日常任务（{state.get('phase')}）：分析结束 → "
                      f"已启动兴趣点分析 run={r.get('run_id')}")
                return
            print(f"[scheduler] 日常任务：兴趣点分析启动被拒（{r.get('reason')}），链条到此为止")
        print(f"[scheduler] 日常任务（{state.get('phase')}）完成：同步 + 分析都已结束")
        _set_daily_state({})
    elif step == "interest":
        # 等到兴趣点分析跑完才算链条结束（它的耗时随增量大小变，可能几分钟到数小时）
        import interest_service
        if interest_service.service.is_running():
            return
        print(f"[scheduler] 日常任务（{state.get('phase')}）完成：同步 + 评分 + 兴趣点 均已结束")
        _set_daily_state({})


def _reschedule_daily():
    """按配置注册/更新两个日常任务 + 看门狗（时刻改了立即生效，不必重启）。"""
    cfg = _daily_settings()
    from apscheduler.triggers.cron import CronTrigger
    for job_id, cron_text, fn in ((DAILY_MAIN_JOB_ID, cfg["main_cron"], _daily_main_job),
                                  (DAILY_COMPENSATE_JOB_ID, cfg["comp_cron"],
                                   _daily_compensate_job)):
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)
        hm = _parse_hhmm(cron_text)
        if not hm:
            continue
        _scheduler.add_job(fn, CronTrigger(hour=hm[0], minute=hm[1], timezone="Asia/Shanghai"),
                           id=job_id, replace_existing=True, max_instances=1, coalesce=True)
    _reschedule_interval(DAILY_WATCH_JOB_ID, _daily_watch_job, seconds=DAILY_WATCH_SECONDS)


def _eval_regression_job():
    """每周一次的评估集回归（修 P8）—— 门禁之外的第二道体检。

    ## 为什么必须有它

    门禁只在"发布规则"那一个动作上触发。而模型侧会**静默漂移**（供应商换版本、
    参数默认值变化），数据分布也会变 —— 这些都跟"你改没改规则"无关，
    没有定期回归就永远发现不了（实测：正文通道断 3 天、同步空转两天，都是没人看见）。

    ## 两条纪律

    1. **先查互斥**：评估要读 `chat_message`，与同步/评分并发会读到半截会话
       （与评分必须互斥同一个理由）。`eval_run` 没有心跳列，所以**不注册成第三种
       kind**，改为启动前查 `running_kinds()`，非空就跳过（下次再说）。
    2. **越界即告警**：指标回退时写 `eval_regression_failed` 审计 ——
       `/api/eval/alerts` 也会把它读出来（那才是给人看的出口）。
    """
    if job_service.running_kinds():
        print(f"[scheduler] 评估回归：有批量在跑（{job_service.running_kinds()}），本轮跳过")
        return
    try:
        import eval_runner
        limit = db.setting_int("eval_regression_limit", "EVAL_REGRESSION_LIMIT", 0)
        res = eval_runner.run_sync(limit=limit, check_drift=True)
        if res.get("gate_passed") is False:
            audit.record_action("system", "eval_regression_failed", f"run:{res.get('run_id')}",
                                {"diffs": res.get("gate_diffs"), "tol": res.get("tol")})
            print(f"[scheduler] 评估回归：指标回退（run={res.get('run_id')}）")
        else:
            print(f"[scheduler] 评估回归完成：run={res.get('run_id')} "
                  f"case_ok={res.get('case_ok')}/{res.get('case_total')}")
    except Exception as e:                      # noqa: BLE001  定时任务不该把调度器带崩
        print(f"[scheduler] 评估回归异常：{type(e).__name__}: {e}")


def _reschedule_eval_regression():
    """按 `eval_regression_cron`（HH:MM，**每周一**）注册回归任务。空 = 关闭。"""
    if _scheduler.get_job(EVAL_REGRESSION_JOB_ID):
        _scheduler.remove_job(EVAL_REGRESSION_JOB_ID)
    hhmm = str(db.setting("eval_regression_cron", "EVAL_REGRESSION_CRON", "09:00") or "").strip()
    hm = _parse_hhmm(hhmm)
    if not hm:
        return
    from apscheduler.triggers.cron import CronTrigger
    _scheduler.add_job(_eval_regression_job,
                       CronTrigger(day_of_week="mon", hour=hm[0], minute=hm[1],
                                   timezone="Asia/Shanghai"),
                       id=EVAL_REGRESSION_JOB_ID, replace_existing=True,
                       max_instances=1, coalesce=True)


def _reschedule_morning():
    """按 `scoring_morning_cron`（HH:MM）注册/更新早会复核任务。空 = 关闭。"""
    if _scheduler.get_job(MORNING_JOB_ID):
        _scheduler.remove_job(MORNING_JOB_ID)
    hhmm = str(config.get("SCORING_MORNING_CRON") or "").strip()
    try:
        hhmm = str(db.setting("scoring_morning_cron", "SCORING_MORNING_CRON", "") or "").strip()
    except Exception:
        pass
    if not hhmm or ":" not in hhmm:
        return
    try:
        hh, mm = (int(x) for x in hhmm.split(":")[:2])
    except Exception:
        print(f"[scheduler] scoring_morning_cron 格式应为 HH:MM，收到 {hhmm!r}，已忽略")
        return
    from apscheduler.triggers.cron import CronTrigger
    _scheduler.add_job(_morning_job, CronTrigger(hour=hh, minute=mm, timezone="Asia/Shanghai"),
                       id=MORNING_JOB_ID, replace_existing=True)
    print(f"[scheduler] 早会复核已排程：每天 {hh:02d}:{mm:02d}")


def _reschedule_failure():
    """失败重投（默认 15 分钟；0 = 关闭）。"""
    _reschedule_interval(FAILURE_JOB_ID, _failure_job,
                         minutes=_cfg_int("FAILURE_RETRY_INTERVAL_MINUTES", 15))


def _reschedule_retention():
    """保留期清理（默认 720 分钟；0 = 关闭）。"""
    _reschedule_interval(RETENTION_JOB_ID, _retention_job,
                         minutes=_cfg_int("RETENTION_INTERVAL_MINUTES", 720))


def _reschedule_outbox():
    # 启动 15 秒后先跑一次，尽快清掉上次进程遗留的待回写任务
    _reschedule_interval(OUTBOX_JOB_ID, _outbox_job, seconds=OUTBOX_INTERVAL_SECONDS,
                         first_run_after=datetime.now() + timedelta(seconds=15))


def _reschedule():
    _reschedule_interval(SYNC_JOB_ID, _job, minutes=interval_minutes("sync"))


def _reschedule_keepalive():
    # 启动 1 分钟后先跑一次，避免刚启动时凭据就处于过期边缘
    _reschedule_interval(KEEPALIVE_JOB_ID, _keepalive_job,
                         minutes=interval_minutes("keepalive"),
                         first_run_after=datetime.now() + timedelta(minutes=1))


def _reschedule_scoring():
    _reschedule_interval(SCORING_JOB_ID, _scoring_job,
                         minutes=interval_minutes("scoring"))


def _set_interval_minutes(kind: str, minutes: int) -> int:
    """把间隔夹到 >= 0 并记下（三个 `set_*_interval` 的唯一差异就在这里）。"""
    _intervals[kind] = max(0, int(minutes or 0))
    return _intervals[kind]


def set_interval(minutes: int) -> dict:
    """调整定时同步间隔（分钟，0=关闭）。"""
    _set_interval_minutes("sync", minutes)
    _reschedule()
    return schedule_info()


def reschedule_all() -> dict:
    """设置变更后重排（`scoring_morning_cron` 改了就立刻生效，不必重启后端）。"""
    _reschedule_morning()
    _reschedule_retention()
    _reschedule_failure()
    _reschedule_daily()          # 日常任务时刻也是设置项 → 改了立即生效
    _reschedule_eval_regression()
    return schedule_info()


def set_scoring_interval(minutes: int) -> dict:
    """调整定时评分间隔（分钟，0=关闭）。"""
    _set_interval_minutes("scoring", minutes)
    _reschedule_scoring()
    return schedule_info()


def set_keepalive_interval(minutes: int) -> dict:
    """调整保活间隔（分钟，0=关闭）。"""
    _set_interval_minutes("keepalive", minutes)
    _reschedule_keepalive()
    return schedule_info()


def _job_info(job) -> dict:
    return {
        "enabled": bool(job),
        "next_run_time": job.next_run_time.isoformat()
        if job and job.next_run_time else None,
    }


def schedule_info() -> dict:
    sync_job = _scheduler.get_job(SYNC_JOB_ID) if _scheduler else None
    ka_job = _scheduler.get_job(KEEPALIVE_JOB_ID) if _scheduler else None
    sc_job = _scheduler.get_job(SCORING_JOB_ID) if _scheduler else None
    ka = _job_info(ka_job)
    ka["interval_minutes"] = interval_minutes("keepalive")
    sc = _job_info(sc_job)
    sc["interval_minutes"] = interval_minutes("scoring")
    info = _job_info(sync_job)
    info["interval_minutes"] = interval_minutes("sync")
    info["keepalive"] = ka
    info["scoring"] = sc
    # 日常任务（需求 4.2）：把「几点同步、几点补偿、跑完会不会自动分析」直接给前端，
    # 让运维一眼看到「系统什么时候动、动完会不会自动评分」。
    cfg = _daily_settings()
    info["daily"] = {
        "main_cron": cfg["main_cron"],
        "main_window_minutes": cfg["window"],
        "compensate_cron": cfg["comp_cron"],
        "compensate_lookback_hours": cfg["lookback"],
        "run_analysis": cfg["analysis"],
        "next_main_run": _job_info(_scheduler.get_job(DAILY_MAIN_JOB_ID) if _scheduler else None),
        "next_compensate_run": _job_info(
            _scheduler.get_job(DAILY_COMPENSATE_JOB_ID) if _scheduler else None),
        "chain": _daily_state(),
    }
    # 评估回归（P8）：每周一一次的体检 —— 让人一眼看到"下次什么时候跑"
    info["eval_regression"] = {
        "cron": str(db.setting("eval_regression_cron", "EVAL_REGRESSION_CRON", "09:00") or ""),
        "note": "每周一执行（空 = 关闭）",
        "limit": db.setting_int("eval_regression_limit", "EVAL_REGRESSION_LIMIT", 0),
        "next_run": _job_info(_scheduler.get_job(EVAL_REGRESSION_JOB_ID) if _scheduler else None),
    }
    return info
