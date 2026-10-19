# -*- coding: utf-8 -*-
"""兴趣点分布接口（总览页「兴趣点分布图」用）。

读接口公开（登录即可）；写接口（开始 / 停止）要求 `operator, admin`
—— 它们会真调大模型（花钱）且改变图上数据，与 `/api/scoring/start` 同档。
"""
from fastapi import APIRouter, Body, Depends, HTTPException

import api_guard
import idempotency
import interest
import interest_repo
from interest_service import service

router = APIRouter(prefix="/api/interest", tags=["interest"])

#: 允许的切片值 —— 传错（`scope=s`、`scope=all `）时给 400，
#: 而不是返回一张空图让人以为"这个员工名下真的没有兴趣点"。
_SCOPES = {"all", "S", "A", "B", "C", "D"}


def _scope(value: str) -> str:
    v = (value or "all").strip()
    if v not in _SCOPES:
        raise HTTPException(status_code=400,
                            detail=f"scope 只能是 all/S/A/B/C/D，收到 {value!r}")
    return v


def _int(payload: dict, key: str) -> int:
    """从 body 里取整数，**非法值给 400 而不是 500**。

    `int("abc")` 抛 ValueError → 500 + 堆栈，看起来像系统坏了；而这是调用方传错。
    """
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{key} 必须是整数") from None


def _staff_ids(payload: dict):
    """`staff_ids` 只接受字符串数组（单个字符串会让 `= ANY(...)` 直接报类型错）。"""
    v = payload.get("staff_ids")
    if v in (None, "", []):
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise HTTPException(status_code=400, detail="staff_ids 必须是非空字符串数组")
    return [x.strip() for x in v]


@router.get("/distribution")
def distribution(scope: str = "all", staff_id: str = "", top: int = 8,
                 secondary_top: int = 40):
    """分布图数据：一级分类（按**客户数**降序）+ 二级标签 + 等级分布 + 覆盖面。

    `scope`：`all` 或等级（`S`/`A`/`B`/`C`/`D`，按客户**当前有效**的评估结论切片）；
    `staff_id`：只看某个员工的客户。
    """
    out = interest_repo.distribution(scope=_scope(scope), staff_id=staff_id, top=top,
                                     secondary_top=secondary_top)
    out["taxonomy"] = {"version": interest.load_taxonomy().get("taxonomy_version"),
                       "names": interest.primary_names(),
                       "order": interest.primary_keys()}
    out["prompt_version"] = interest.PROMPT_VERSION
    return out


@router.get("/customers")
def customers(primary: str = "", secondary: str = "", scope: str = "all",
              staff_id: str = "", limit: int = 50):
    """下钻：关心某一类（或某个二级标签）的客户名单 + 证据原话。

    ⚠️ 返回里带**客户原话片段**（仅限内部选题参考，不得直接对外发布）。
    """
    return {"items": interest_repo.customers(primary=primary, secondary=secondary,
                                             scope=_scope(scope), staff_id=staff_id,
                                             limit=limit)}


@router.get("/status")
def status():
    """运行状态 + 进度 + 覆盖度（剩余待分析会话数）。"""
    return service.status()


@router.post("/start", dependencies=[Depends(api_guard.require("operator", "admin"))])
def start(payload: dict = Body(default={})):
    """启动兴趣点分析（增量：只跑"没分析过"或"有新消息"的会话）。

    · `max_sessions`：本轮最多几个会话（0 = 用 `interest_max_sessions` 配置）；
    · `min_messages`：总会话消息数下限（0 = 用配置）；
    · `min_customer_msgs`：**客户**发言条数下限（0 = 用配置；员工刷屏不算语料）；
    · `staff_ids`：限定员工；
    · `request_key`：幂等键（前端重试/手抖双击安全）。
    """
    # 参数校验放在幂等之前：坏请求不该占用 request_key（否则改正后重试会被"重放"）
    kind = str(payload.get("kind") or "incremental")
    ids = _staff_ids(payload)
    max_sessions = _int(payload, "max_sessions")
    min_messages = _int(payload, "min_messages")
    min_customer_msgs = _int(payload, "min_customer_msgs")
    return idempotency.run_once(
        payload.get("request_key"), "interest.start",
        lambda: service.start(kind=kind, staff_ids=ids, max_sessions=max_sessions,
                              min_messages=min_messages,
                              min_customer_msgs=min_customer_msgs,
                              triggered_by="manual"))


@router.post("/stop", dependencies=[Depends(api_guard.require("operator", "admin"))])
def stop():
    """请求停止（在当前会话处理完后优雅退出；已分析的结果保留）。"""
    return service.stop()
