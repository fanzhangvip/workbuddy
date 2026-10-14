# -*- coding: utf-8 -*-
"""鉴权相关接口：扫码登录、状态查询、凭据校验。"""
from fastapi import APIRouter, Body

import auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status")
def status():
    """凭据与登录状态。"""
    return auth_service.credential_status()


@router.post("/login")
def login(payload: dict = Body(default={})):
    """弹出浏览器扫码登录并自动抓取凭据。"""
    return auth_service.start_login(
        staff_id=payload.get("staff_id"),
        session_id=payload.get("session_id"),
        wait_sec=int(payload.get("wait_sec") or 300),
    )


@router.post("/verify")
def verify():
    """真调一次企微接口校验凭据是否仍然有效。"""
    return auth_service.verify()
