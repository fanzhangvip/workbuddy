# -*- coding: utf-8 -*-
"""兴趣点分布接口（总览页「兴趣点分布图」用）。

读接口公开（登录即可）；写接口（开始 / 停止）要求 `operator, admin`
—— 它们会真调大模型（花钱）且改变图上数据，与 `/api/scoring/start` 同档。
"""
from fastapi import APIRouter, Body, Depends

import api_guard
import idempotency
import interest
import interest_repo
from interest_service import service

router = APIRouter(prefix="/api/interest", tags=["interest"])


@router.get("/distribution")
def distribution(scope: str = "all", staff_id: str = "", top: int = 8,
                 secondary_top: int = 40):
    """分布图数据：一级分类（按**客户数**降序）+ 二级标签 + 等级分布 + 覆盖面。

    `scope`：`all` 或等级（`S`/`A`/`B`/`C`/`D`，按客户**当前有效**的评估结论切片）；
    `staff_id`：只看某个员工的客户。
    """
    out = interest_repo.distribution(scope=scope, staff_id=staff_id, top=top,
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
                                             scope=scope, staff_id=staff_id, limit=limit)}


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
    return idempotency.run_once(
        payload.get("request_key"), "interest.start",
        lambda: service.start(
            kind=str(payload.get("kind") or "incremental"),
            staff_ids=payload.get("staff_ids") or None,
            max_sessions=int(payload.get("max_sessions") or 0),
            min_messages=int(payload.get("min_messages") or 0),
            min_customer_msgs=int(payload.get("min_customer_msgs") or 0),
            triggered_by="manual"))


@router.post("/stop", dependencies=[Depends(api_guard.require("operator", "admin"))])
def stop():
    """请求停止（在当前会话处理完后优雅退出；已分析的结果保留）。"""
    return service.stop()
