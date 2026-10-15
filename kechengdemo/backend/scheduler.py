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

import config
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

_scheduler: BackgroundScheduler | None = None
_interval: int = 0
_ka_interval: int = 0
_sc_interval: int = 0


def _read_interval() -> int:
    try:
        return int(config.get("SYNC_INTERVAL_MINUTES") or 0)
    except Exception:
        return 0


def _read_ka_interval() -> int:
    try:
        return int(config.get("KEEPALIVE_INTERVAL_MINUTES") or 360)
    except Exception:
        return 360


def _read_scoring_interval() -> int:
    try:
        return int(config.get("SCORING_INTERVAL_MINUTES") or 0)
    except Exception:
        return 0


def guard_can_start(owner: str) -> bool:
    """共享互斥守卫（修 H1）：同步与评分**不得同时运行**。

    为什么必须互斥：评分要读 ``chat_message``；若同步正在往里写，评分会读到
    「半截会话」（``msg_count`` / ``last_msg_time`` 都还在变），结论因此不可复现。

    改造前的问题：``_job``（同步）只查**同步自己**在不在跑，压根不查评分 ——
    于是「评分跑着，同步又启动」这条路没被堵住；而 ``_scoring_job`` 两边都查了。
    两个 job 的守卫口径不一致，收敛成同一个入口后才算真的互斥。

    返回 True = 可以启动；False = 应跳过（原因已打印）。
    """
    busy_sync = service.is_running()
    busy_scoring = scoring.is_running()
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
        import scoring_service as ss
        scope = ss.setting("public_class_grade_scope", "PUBLIC_CLASS_GRADE_SCOPE", "S,A")
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
    global _scheduler, _interval, _ka_interval, _sc_interval
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _scheduler.start()
    _interval = _read_interval()
    _ka_interval = _read_ka_interval()
    _sc_interval = _read_scoring_interval()
    _reschedule()
    _reschedule_keepalive()
    _reschedule_scoring()
    _reschedule_outbox()
    _reschedule_retention()
    _reschedule_failure()
    _reschedule_morning()
    _reschedule_grade_tag()
    _reschedule_reap()
    try:
        import scoring_service as _ss
        _mo = _ss.setting('scoring_morning_cron', 'SCORING_MORNING_CRON', '') or '关闭'
        _rt = _ss.setting_int('data_retention_days', 'DATA_RETENTION_DAYS', 0)
    except Exception:
        _mo, _rt = '关闭', 0
    print(f"[scheduler] 已启动｜同步 {_interval or '关闭'} 分"
          f"｜保活 {_ka_interval or '关闭'} 分"
          f"｜评分 {_sc_interval or '关闭'} 分"
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


def _reschedule_reap():
    """注册幂等记录清理任务（默认 60 分钟一次；0 = 关闭）。"""
    if _scheduler.get_job(REAP_JOB_ID):
        _scheduler.remove_job(REAP_JOB_ID)
    minutes = int(config.get("REAP_INTERVAL_MINUTES") or 60)
    if minutes > 0:
        _scheduler.add_job(_reap_job, "interval", minutes=minutes,
                           id=REAP_JOB_ID, replace_existing=True)


def _reschedule_grade_tag():
    """按 `GRADE_TAG_INTERVAL_SECONDS`（默认 30s）注册标签排空任务；0 = 关闭。"""
    if _scheduler.get_job(GRADE_TAG_JOB_ID):
        _scheduler.remove_job(GRADE_TAG_JOB_ID)
    secs = int(config.get("GRADE_TAG_INTERVAL_SECONDS") or 30)
    if secs > 0:
        _scheduler.add_job(_grade_tag_job, "interval", seconds=secs,
                           id=GRADE_TAG_JOB_ID, replace_existing=True)


def _reschedule_morning():
    """按 `scoring_morning_cron`（HH:MM）注册/更新早会复核任务。空 = 关闭。"""
    if _scheduler.get_job(MORNING_JOB_ID):
        _scheduler.remove_job(MORNING_JOB_ID)
    hhmm = str(config.get("SCORING_MORNING_CRON") or "").strip()
    try:
        import scoring_service as ss
        hhmm = str(ss.setting("scoring_morning_cron", "SCORING_MORNING_CRON", "") or "").strip()
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
    if _scheduler.get_job(FAILURE_JOB_ID):
        _scheduler.remove_job(FAILURE_JOB_ID)
    minutes = int(config.get("FAILURE_RETRY_INTERVAL_MINUTES") or 15)
    if minutes > 0:
        _scheduler.add_job(_failure_job, "interval", minutes=minutes,
                           id=FAILURE_JOB_ID, replace_existing=True)


def _reschedule_retention():
    if _scheduler.get_job(RETENTION_JOB_ID):
        _scheduler.remove_job(RETENTION_JOB_ID)
    minutes = int(config.get("RETENTION_INTERVAL_MINUTES") or 720)
    if minutes > 0:
        _scheduler.add_job(_retention_job, "interval", minutes=minutes,
                           id=RETENTION_JOB_ID, replace_existing=True)


def _reschedule_outbox():
    if _scheduler.get_job(OUTBOX_JOB_ID):
        _scheduler.remove_job(OUTBOX_JOB_ID)
    _scheduler.add_job(
        _outbox_job, "interval", seconds=OUTBOX_INTERVAL_SECONDS, id=OUTBOX_JOB_ID,
        max_instances=1, coalesce=True,
        # 启动 15 秒后先跑一次，尽快清掉上次进程遗留的待回写任务
        next_run_time=datetime.now() + timedelta(seconds=15),
    )


def _reschedule():
    if _scheduler.get_job(SYNC_JOB_ID):
        _scheduler.remove_job(SYNC_JOB_ID)
    if _interval > 0:
        _scheduler.add_job(_job, "interval", minutes=_interval, id=SYNC_JOB_ID,
                           max_instances=1, coalesce=True)


def _reschedule_keepalive():
    if _scheduler.get_job(KEEPALIVE_JOB_ID):
        _scheduler.remove_job(KEEPALIVE_JOB_ID)
    if _ka_interval > 0:
        _scheduler.add_job(
            _keepalive_job, "interval", minutes=_ka_interval, id=KEEPALIVE_JOB_ID,
            max_instances=1, coalesce=True,
            # 启动 1 分钟后先跑一次，避免刚启动时凭据就处于过期边缘
            next_run_time=datetime.now() + timedelta(minutes=1),
        )


def set_interval(minutes: int) -> dict:
    """调整定时同步间隔（分钟，0=关闭）。"""
    global _interval
    _interval = max(0, int(minutes or 0))
    _reschedule()
    return schedule_info()


def _reschedule_scoring():
    if _scheduler.get_job(SCORING_JOB_ID):
        _scheduler.remove_job(SCORING_JOB_ID)
    if _sc_interval > 0:
        _scheduler.add_job(_scoring_job, "interval", minutes=_sc_interval,
                           id=SCORING_JOB_ID, max_instances=1, coalesce=True)


def reschedule_all() -> dict:
    """设置变更后重排（`scoring_morning_cron` 改了就立刻生效，不必重启后端）。"""
    _reschedule_morning()
    _reschedule_retention()
    _reschedule_failure()
    return schedule_info()


def set_scoring_interval(minutes: int) -> dict:
    """调整定时评分间隔（分钟，0=关闭）。"""
    global _sc_interval
    _sc_interval = max(0, int(minutes or 0))
    _reschedule_scoring()
    return schedule_info()


def set_keepalive_interval(minutes: int) -> dict:
    """调整保活间隔（分钟，0=关闭）。"""
    global _ka_interval
    _ka_interval = max(0, int(minutes or 0))
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
    ka["interval_minutes"] = _ka_interval
    sc = _job_info(sc_job)
    sc["interval_minutes"] = _sc_interval
    info = _job_info(sync_job)
    info["interval_minutes"] = _interval
    info["keepalive"] = ka
    info["scoring"] = sc
    return info
