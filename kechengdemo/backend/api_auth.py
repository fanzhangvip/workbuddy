# -*- coding: utf-8 -*-
"""鉴权相关接口。

⚠️ 这里有**两件容易混的事**，名字刻意分开了：

  · **登录本系统**（`POST/DELETE /api/auth/session`、`GET /api/auth/me`）——
    用户名 + 口令 → 服务端会话（HttpOnly Cookie）。前端那个登录页走这条。
  · **微伴/企微数据源凭据**（`GET /status`、`POST /login`、`POST /verify`）——
    「弹出浏览器扫码」拿 open_sid / cookie，是**抓数据的凭据**，不是登录本系统。
    `POST /login` 这个名字是历史遗留（前端「数据源凭据」页在用），别和会话登录搞混。
"""
from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import JSONResponse

import auth_service
import security

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ============================================================ 登录本系统（会话）

def _is_https(request: Request) -> bool:
    """是否 HTTPS。决定 Cookie 要不要带 `Secure`。

    本地直连 `http://127.0.0.1:8848` 时带 `Secure` 会让浏览器**直接不存**这个 Cookie
    （表现为「登录成功但立刻又回到登录页」），所以按实际协议给。
    """
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return proto == "https" or request.url.scheme == "https"


@router.post("/session")
def sign_in(request: Request, payload: dict = Body(default={})):
    """**登录本系统**：校验用户名/口令，通过则下发会话 Cookie。

    `remember=true` → 30 天（浏览器关了再开仍登录）；否则 12 小时且**关闭浏览器即失效**
    （Cookie 不带 `Max-Age`）。口令**不进前端存储**，浏览器只留一个 HttpOnly 的 sid。
    """
    user = str(payload.get("username") or "").strip()
    pwd = str(payload.get("password") or "")
    remember = bool(payload.get("remember"))
    if not security.enabled():
        return {"ok": True, "user": security.LOCAL_ACTOR, "role": "admin",
                "note": "鉴权已关闭（API_AUTH=off），无需登录"}
    # 复用 Basic 的同一份账号表与定长比较（`API_USERS` 多账号也一并支持）
    who = security.verify("Basic " + _b64(f"{user}:{pwd}"))
    if who is None:
        return JSONResponse(status_code=401,
                            content={"ok": False, "reason": "用户名或口令不正确"})
    sid = security.create_session(
        who, remember=remember,
        user_agent=request.headers.get("user-agent"),
        client_ip=(request.headers.get("x-real-ip")
                   or (request.client.host if request.client else "")))
    resp = JSONResponse(content={"ok": True, "user": who, "role": security.role_of(who),
                                 "remember": remember})
    resp.set_cookie(
        security.COOKIE_NAME, sid, httponly=True, samesite="lax", path="/",
        secure=_is_https(request),
        # 不勾「记住我」→ 不带 Max-Age（浏览器会话 Cookie，关浏览器就没了）
        max_age=security._session_ttl_seconds(remember) if remember else None)
    return resp


@router.delete("/session")
def sign_out(request: Request):
    """退出登录：删会话行 + 清 Cookie（幂等，未登录也返回 ok）。"""
    from main import _cookie_value                      # 延迟导入避免循环

    security.revoke_session(_cookie_value(request.headers.get("cookie")))
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(security.COOKIE_NAME, path="/")
    return resp


@router.get("/me")
def me(request: Request):
    """当前登录身份（前端首屏用它决定「进应用」还是「去登录页」）。

    **免鉴权**：未登录时也要能拿到 `authenticated: false`，而不是一个 401 噪声。
    """
    from main import _cookie_value

    if not security.enabled():
        return {"authenticated": True, "user": security.LOCAL_ACTOR, "role": "admin",
                "auth_enabled": False}
    who = security.verify(request.headers.get("authorization")) or \
        security.resolve_session(_cookie_value(request.headers.get("cookie")))
    return {"authenticated": bool(who), "user": who, "role": security.role_of(who),
            "auth_enabled": True, "roles": list(security.ROLES),
            "role_desc": dict(security.ROLE_DESC)}


def _b64(s: str) -> str:
    import base64
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


# ============================================================ 微伴数据源凭据（扫码）

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
