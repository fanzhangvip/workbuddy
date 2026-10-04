"""Web 后台鉴权（WB-07）。

方案选择：**HMAC 签名的无状态令牌 + 服务端撤销集合**。

为什么不用 JWT 库、也不把令牌存 localStorage：
- 令牌由 ``hmac(secret, payload)`` 生成，密钥只在服务端（``<home>/runtime/secret.key``），
  服务端无需存 session 即可校验，进程重启后仍有效；
- 同时维护撤销集合，使「退出登录立即失效」成为可能（纯无状态 JWT 做不到）；
- 前端把令牌放在内存 + ``sessionStorage``，不落 localStorage，降低 XSS 长尾风险。

登录接口带**简单的失败节流**，避免后台口令被离线爆破。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from functools import wraps
from typing import Any, Callable

from flask import g, jsonify, request

from ticket_helper.errors import AuthError, ForbiddenError

log = logging.getLogger(__name__)

#: 登录失败节流参数
MAX_FAILURES = 8
LOCK_SECONDS = 300


class TokenManager:
    """令牌签发 / 校验 / 撤销。"""

    def __init__(self, secret: str, *, ttl: int = 12 * 3600) -> None:
        self.secret = (secret or "insecure-default").encode("utf-8")
        self.ttl = max(300, int(ttl))
        self._revoked: set[str] = set()
        self._failures: dict[str, list[float]] = {}

    # ------------------------------------------------------------------ #
    @staticmethod
    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    @staticmethod
    def _unb64(text: str) -> bytes:
        padding = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(text + padding)

    def _sign(self, payload: str) -> str:
        return self._b64(hmac.new(self.secret, payload.encode("utf-8"), hashlib.sha256).digest())

    def issue(self, username: str, *, extra: dict[str, Any] | None = None) -> str:
        payload = self._b64(
            json.dumps(
                {"u": username, "iat": int(time.time()), "exp": int(time.time()) + self.ttl, **(extra or {})},
                separators=(",", ":"),
            ).encode("utf-8")
        )
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str) -> dict[str, Any]:
        if not token or "." not in token:
            raise AuthError("登录凭证格式非法")
        if token in self._revoked:
            raise AuthError("登录凭证已失效，请重新登录")
        payload, _, signature = token.partition(".")
        if not hmac.compare_digest(signature, self._sign(payload)):
            raise AuthError("登录凭证校验失败")
        try:
            data = json.loads(self._unb64(payload))
        except (ValueError, TypeError) as exc:
            raise AuthError("登录凭证解析失败") from exc
        if int(data.get("exp", 0)) < time.time():
            raise AuthError("登录已过期，请重新登录")
        return data

    def revoke(self, token: str) -> None:
        self._revoked.add(token)
        if len(self._revoked) > 5000:  # 防止长期运行内存膨胀
            self._revoked = set(list(self._revoked)[-1000:])

    # ------------------------------------------------------------------ #
    def is_locked(self, client: str) -> bool:
        now = time.time()
        attempts = [t for t in self._failures.get(client, []) if now - t < LOCK_SECONDS]
        self._failures[client] = attempts
        return len(attempts) >= MAX_FAILURES

    def record_failure(self, client: str) -> None:
        self._failures.setdefault(client, []).append(time.time())

    def clear_failures(self, client: str) -> None:
        self._failures.pop(client, None)


def _extract_token() -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("X-Auth-Token", "") or (request.args.get("token") or "")


def current_user() -> str:
    return getattr(g, "auth_user", "")


def require_auth(manager: TokenManager) -> Callable:
    """装饰器：保护需要登录的接口。"""

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            token = _extract_token()
            if not token:
                raise AuthError("未登录：请先登录后台")
            data = manager.verify(token)
            g.auth_user = data.get("u", "")
            g.auth_token = token
            return view(*args, **kwargs)

        return wrapper

    return decorator


def require_internal(*, allow_remote: bool = False) -> Callable:
    """装饰器：仅允许本机回环调用（桌面客户端 / 本地工具）。

    这是一个「纵深防御」：即使后台密码泄漏，公网来源也无法调用标记为
    internal 的接口（例如直接读取解密后的账号密码）。
    """

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not allow_remote:
                remote = (request.remote_addr or "").strip()
                if remote not in ("127.0.0.1", "::1", "localhost"):
                    log.warning("拒绝来自非本机地址的 internal 接口调用: %s -> %s", remote, request.path)
                    raise ForbiddenError("该接口仅允许本机调用")
            return view(*args, **kwargs)

        return wrapper

    return decorator


def unauthorized_response(message: str) -> Any:
    return jsonify({"code": "unauthorized", "message": message}), 401
