# -*- coding: utf-8 -*-
"""定时任务调度（APScheduler）—— 四个任务：

1. ``auto-incremental-sync``  定时增量同步
   间隔取 .env 的 ``SYNC_INTERVAL_MINUTES``（分钟），0 表示关闭。
   运行期可通过 ``POST /api/sync/schedule`` 动态调整。

2. ``credential-keepalive``   凭据保活
   间隔取 .env 的 ``KEEPALIVE_INTERVAL_MINUTES``（分钟，默认 360），0 表示关闭。

3. ``auto-scoring``           定时增量评分
   间隔取 .env 的 ``SCORING_INTERVAL_MINUTES``（分钟），0 表示关闭。

4. ``writeback-outbox-drain`` 回写 outbox 排空（阶段B）
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
    print(f"[scheduler] 已启动｜同步间隔 {_interval or '关闭'} 分钟"
          f"｜保活间隔 {_ka_interval or '关闭'} 分钟"
          f"｜评分间隔 {_sc_interval or '关闭'} 分钟"
          f"｜回写 outbox {OUTBOX_INTERVAL_SECONDS} 秒")


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
