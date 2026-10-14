# -*- coding: utf-8 -*-
"""同步任务接口。"""
from fastapi import APIRouter, Body

import db
import scheduler
from sync_service import service

router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.post("/start")
def start(payload: dict = Body(default={})):
    """启动同步任务。

    kind: full（忽略水位全量）/ incremental（按水位增量，默认）
    """
    return service.start(
        kind=payload.get("kind") or "incremental",
        staff_ids=payload.get("staff_ids") or None,
        session_types=payload.get("session_types") or ["ext_user"],
        max_sessions_per_staff=int(payload.get("max_sessions_per_staff") or 200),
        max_msgs_per_session=int(payload.get("max_msgs_per_session") or 0),
        triggered_by="manual",
        ignore_cursor=bool(payload.get("ignore_cursor")) or payload.get("kind") == "full",
    )


@router.post("/stop")
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


@router.post("/schedule")
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


# ---------------- 凭据保活 ----------------

@router.get("/keepalive")
def keepalive_status():
    """凭据状态 + 最近一次保活结果。

    微伴登录态是滑动过期的，定期保活可让它永不失效。
    """
    import wb_keepalive
    return wb_keepalive.status()


@router.post("/keepalive")
def keepalive_run(payload: dict = Body(default={})):
    """手动触发一次凭据保活（会打开/刷新后台页面）。"""
    import wb_keepalive
    return wb_keepalive.refresh(wait_sec=int(payload.get("wait") or 45),
                                source="manual")
