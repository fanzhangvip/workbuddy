# -*- coding: utf-8 -*-
"""记忆治理接口：查看 / 人工改判 / 拒绝一条客户记忆。

读接口登录即可（评分详情抽屉要用）；写接口要 `operator/admin`
—— 它改的是"系统对客户的长期认知"，会影响之后的每一次判定的上下文。

为什么单独一个路由而不是塞进 `api_scoring`：记忆的读写入口必须**唯一**
（与 `memory_repo` 一一对应），放在评分路由里会让"谁能写记忆"再次分散。
"""
from fastapi import APIRouter, Body, Depends, HTTPException

import api_guard
import memory_repo
import security

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _rel_key(value: str) -> str:
    """`relationship_key` 形如 `staff_id:target_id` —— 少了冒号就是调用方传错。"""
    rel = str(value or "").strip()
    if ":" not in rel or rel.startswith(":") or rel.endswith(":"):
        raise HTTPException(status_code=400, detail="relationship_key 必须是 staff_id:target_id")
    return rel


@router.get("/{relationship_key}")
def list_memory(relationship_key: str, history: int = 0):
    """该客户的结构化记忆（默认只给当前有效；`history=1` 连历史一起）。"""
    rel = _rel_key(relationship_key)
    return {"relationship_key": rel, "items": memory_repo.list_rows(rel, bool(history))}


@router.post("", dependencies=[Depends(api_guard.require("operator", "admin"))])
def set_memory(payload: dict = Body(default={})):
    """人工改判一条记忆（`source='human'`，模型再也覆盖不了它）。"""
    rel = _rel_key(payload.get("relationship_key"))
    r = memory_repo.set_manual(rel=rel, slot=str(payload.get("slot") or ""),
                               value=str(payload.get("value") or ""),
                               note=str(payload.get("note") or ""),
                               actor=security.actor())
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("reason") or "保存失败")
    return r


@router.post("/{memory_id}/reject", dependencies=[Depends(api_guard.require("operator", "admin"))])
def reject_memory(memory_id: int, payload: dict = Body(default={})):
    """拒绝一条记忆；有上一条历史值就回退到它。"""
    r = memory_repo.reject(memory_id=memory_id, actor=security.actor())
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r.get("reason") or "记忆不存在")
    return r


@router.get("/", include_in_schema=True)
def stats():
    """记忆总体状态（排障/体检用）。"""
    return memory_repo.stats()
