# -*- coding: utf-8 -*-
"""同步任务接口。"""
from fastapi import APIRouter, Body, Depends

import api_guard
import db
import scheduler
from sync_service import service

router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.post("/start", dependencies=[Depends(api_guard.require("operator", "admin"))])
def start(payload: dict = Body(default={})):
    """启动同步任务。

    kind: full（忽略水位全量）/ incremental（按水位增量，默认）

    ⚠️ 这个接口会发起大量外部调用（微伴会话/消息 + 企微解密），属于
    **operator 起步**的动作（角色定义见 `/api/auth/me`：viewer = 只读）。
    改造前它没有任何角色守卫 —— 一个只读账号就能启动全量同步。

    `max_sessions_per_staff`：每员工最多同步多少会话。**硬上限 200**
    （缺省/0/负数 → 200，超过 → 收敛到 200），收敛在 `sync_service.start()` 里做，
    返回值会带 `sessions_capped` 说明是否被收敛。
    """
    return service.start(
        kind=payload.get("kind") or "incremental",
        staff_ids=payload.get("staff_ids") or None,
        session_types=payload.get("session_types") or ["ext_user"],
        # 原样透传：封顶口径由服务层统一负责（唯一入口，API/调度器/脚本都绕不过）
        max_sessions_per_staff=payload.get("max_sessions_per_staff"),
        max_msgs_per_session=int(payload.get("max_msgs_per_session") or 0),
        triggered_by="manual",
        ignore_cursor=bool(payload.get("ignore_cursor")) or payload.get("kind") == "full",
    )


@router.post("/stop", dependencies=[Depends(api_guard.require("operator", "admin"))])
def stop():
    return service.stop()


@router.get("/status")
def status():
    return service.status()


@router.get("/jobs")
def jobs(limit: int = 20):
    rows = db.query(
        """SELECT id, kind, status, triggered_by, staff_count, session_total,
                  session_done, msg_total, msg_decrypted, msg_new, detail, error,
                  started_at, finished_at
             FROM sync_job ORDER BY id DESC LIMIT %s""", (limit,))
    return {"items": rows}


@router.get("/schedule")
def get_schedule():
    return scheduler.schedule_info()


@router.post("/schedule", dependencies=[Depends(api_guard.require("operator", "admin"))])
def set_schedule(payload: dict = Body(default={})):
    """设置定时同步间隔（分钟，0=关闭）。

    可选同时设置保活间隔：``keepalive_interval_minutes``。
    """
    if "keepalive_interval_minutes" in payload:
        scheduler.set_keepalive_interval(
            int(payload.get("keepalive_interval_minutes") or 0))
    if payload.get("interval_minutes") is not None:
        return scheduler.set_interval(int(payload["interval_minutes"]))
    return scheduler.schedule_info()


@router.post("/schedule/daily", dependencies=[Depends(api_guard.require("operator", "admin"))])
def set_daily_schedule(payload: dict = Body(default={})):
    """配置**日常任务**（需求 4.2）：什么时间同步、什么时候接着分析。

    可配项（只传要改的键）：

    · `sync_main_cron`              主增量同步 + 分析时刻 `HH:MM`（空 = 关闭）
    · `sync_main_window_minutes`    主任务执行窗口（分钟，0 = 不限）
    · `sync_compensate_cron`        48 小时补偿同步 + 分析时刻 `HH:MM`（空 = 关闭）
    · `sync_compensate_lookback_hours` 补偿回看窗口（小时）
    · `daily_run_analysis`          同步跑完是否自动接着分析

    ⚠️ 时刻**必须校验**：写成 `8:0` 这类非法值若静默存下，任务会被悄悄关掉，
    而界面上看不出问题 —— 所以这里直接拒绝并说明格式。
    """
    INT_KEYS = {"sync_main_window_minutes": (0, 240),
                "sync_compensate_lookback_hours": (1, 720)}
    if payload.get("sync_main_cron") is not None:
        v = str(payload["sync_main_cron"]).strip()
        if v and not scheduler._parse_hhmm(v):
            return {"ok": False, "reason": f"主任务时刻格式应为 HH:MM（收到 {v!r}；留空=关闭）"}
    if payload.get("sync_compensate_cron") is not None:
        v = str(payload["sync_compensate_cron"]).strip()
        if v and not scheduler._parse_hhmm(v):
            return {"ok": False, "reason": f"补偿任务时刻格式应为 HH:MM（收到 {v!r}；留空=关闭）"}
    for key, (lo, hi) in INT_KEYS.items():
        if payload.get(key) is None:
            continue
        try:
            n = int(payload[key])
        except (TypeError, ValueError):
            return {"ok": False, "reason": f"{key} 必须是整数"}
        if not (lo <= n <= hi):
            return {"ok": False, "reason": f"{key} 应在 {lo}~{hi} 之间（收到 {n}）"}

    for key in ("sync_main_cron", "sync_main_window_minutes", "sync_compensate_cron",
                "sync_compensate_lookback_hours", "daily_run_analysis"):
        if payload.get(key) is None:
            continue
        if key == "daily_run_analysis":
            db.set_setting(key, "true" if payload[key] else "false")
        else:
            db.set_setting(key, str(payload[key]).strip())
    scheduler.reschedule_all()          # 改完立即生效（不必重启后端）
    return {"ok": True, **scheduler.schedule_info()}


# ---------------- 凭据保活 ----------------

@router.get("/keepalive")
def keepalive_status():
    """凭据状态 + 最近一次保活结果。

    微伴登录态是滑动过期的，定期保活可让它永不失效。
    """
    import wb_keepalive
    return wb_keepalive.status()


@router.post("/keepalive", dependencies=[Depends(api_guard.require("operator", "admin"))])
def keepalive_run(payload: dict = Body(default={})):
    """手动触发一次凭据保活（会打开/刷新后台页面）。"""
    import wb_keepalive
    return wb_keepalive.refresh(wait_sec=int(payload.get("wait") or 45),
                                source="manual")
