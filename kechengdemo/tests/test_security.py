# -*- coding: utf-8 -*-
"""应用层鉴权 + 审计主体服务端推导验收（代码评审 P0-2）。

    .venv/Scripts/python.exe tests/test_security.py

覆盖：
  · 凭据生成与持久化（API_PASSWORD 为空 → 自动生成强随机并写入 .env）
  · `verify()` 对 `Authorization: Basic` 头的各种形态（正确 / 错口令 / 畸形 / 缺失）
  · 中间件行为：鉴权开启时无凭据 → 401 + `WWW-Authenticate`；带凭据 → 放行
  · 健康检查免鉴权（探活用，不该要凭据）
  · `API_AUTH=off` → 全部放行
  · **审计主体由服务端推导**：客户端自报的 reviewer/annotator **不能**变成 `actor`
    （改造前 `actor` 直接取请求体 → `audit_log` 不能作为追责依据）

⚠️ 不真启动服务：直接用 ASGI 中间件做端到端请求（fastapi.testclient 依赖 httpx，
   本项目未装；这里手写最小 ASGI 调用，零新增依赖）。
"""
# 统一输出编码（修 P2-2）
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")

import base64
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "backend"))

import security                    # noqa: E402

_TOTAL = 0
_FAILED = 0


def check(name, cond, extra=""):
    global _TOTAL, _FAILED
    _TOTAL += 1
    if cond:
        print(f"  ok   {name}")
    else:
        _FAILED += 1
        print(f"  FAIL {name}  {extra}")


def basic(user, pwd) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


# ============================================================ 纯函数

def test_verify():
    print("\n--- 1. 凭据校验 ---")
    orig_user, orig_pwd = security._resolved.get("creds") or (None, None)
    security._resolved["creds"] = ("alice", "s3cret")
    try:
        check("正确凭据 → 返回用户名", security.verify(basic("alice", "s3cret")) == "alice")
        check("错误口令 → None", security.verify(basic("alice", "wrong")) is None)
        check("错误用户名 → None", security.verify(basic("bob", "s3cret")) is None)
        check("缺头 → None", security.verify(None) is None)
        check("非 Basic 方案 → None", security.verify("Bearer abc") is None)
        check("畸形 base64 → None", security.verify("Basic !!!not-base64!!!") is None)
        check("空字符串 → None", security.verify("") is None)
        # 口令为空时不允许认证（配置缺失 → 宁可拒绝）
        security._resolved["creds"] = ("alice", "")
        check("口令为空 → 一律拒绝（配置缺失宁可拒绝）",
              security.verify(basic("alice", "")) is None)
    finally:
        security._resolved["creds"] = (orig_user, orig_pwd)
        security._resolved.pop("creds", None)


def test_credentials_generation():
    print("\n--- 2. 凭据生成与持久化 ---")
    env_path = security.ENV_PATH
    old_env = env_path.read_text(encoding="utf-8") if env_path.exists() else None
    security._resolved.clear()
    # 模拟「未配置」：config.get 走 os.environ 优先 → 临时清掉
    old_u, old_p = os.environ.pop("API_USER", None), os.environ.pop("API_PASSWORD", None)
    security._resolved.clear()
    try:
        # 让 config 读不到 .env 里的值：临时改名 .env 代价大，改用 monkeypatch config.get
        import config as cfg
        orig_get = cfg.get
        cfg.get = lambda k, d=None: (None if k in ("API_USER", "API_PASSWORD") else orig_get(k, d))
        security._resolved.clear()
        user, pwd = security.credentials()
        check("未配置时生成口令（非空）", bool(pwd), repr(pwd))
        check("生成口令长度足够（>=16）", len(pwd) >= 16, str(len(pwd)))
        check("默认用户名 admin", user == "admin", user)
        check("生成的记录在 generated_password（供启动日志打印一次）",
              security.generated_password == pwd, str(security.generated_password))
        check("已写入 .env 的 API_PASSWORD", "API_PASSWORD=" in
              env_path.read_text(encoding="utf-8"))
        # 二次调用应命中缓存，不再重新生成
        gen_before = security.generated_password
        u2, p2 = security.credentials()
        check("二次调用复用同一口令（不每次重新生成）", p2 == pwd and u2 == user)
        check("generated_password 不被覆盖", security.generated_password == gen_before)
        cfg.get = orig_get
    finally:
        if old_env is None:
            try:
                env_path.unlink()
            except Exception:
                pass
        else:
            env_path.write_text(old_env, encoding="utf-8")
        if old_u is not None:
            os.environ["API_USER"] = old_u
        if old_p is not None:
            os.environ["API_PASSWORD"] = old_p
        security._resolved.clear()
        security.generated_password = None


def test_enabled_flag():
    print("\n--- 3. 开关语义（默认即安全）---")
    orig = os.environ.get("API_AUTH")
    try:
        for v in (None, "", "on", "true", "1", "yes"):
            if v is None:
                os.environ.pop("API_AUTH", None)
            else:
                os.environ["API_AUTH"] = v
            check(f"API_AUTH={v!r} → 启用", security.enabled() is True, v)
        for v in ("off", "false", "0", "no", "OFF"):
            os.environ["API_AUTH"] = v
            check(f"API_AUTH={v!r} → 关闭", security.enabled() is False, v)
    finally:
        if orig is None:
            os.environ.pop("API_AUTH", None)
        else:
            os.environ["API_AUTH"] = orig


def test_actor():
    print("\n--- 4. 审计主体由服务端推导 ---")
    tok = security.set_principal("alice")
    try:
        check("已认证 → actor = 登录用户名", security.actor() == "alice")
        check("★ 客户端自报值**不能**成为 actor",
              security.actor("超级管理员") == "alice", security.actor("超级管理员"))
        d = security.actor_detail("超级管理员", x=1)
        check("自报值与真主体不同 → 只进 reported_actor",
              d.get("reported_actor") == "超级管理员" and d.get("x") == 1, str(d))
    finally:
        security.reset_principal(tok)
    check("未认证 → actor = 固定常量 local", security.actor() == security.LOCAL_ACTOR,
          security.actor())
    check("未认证时自报值也不影响 actor", security.actor("alice") == security.LOCAL_ACTOR)
    d2 = security.actor_detail("被伪造的人")
    check("未认证时自报值进 reported_actor（明确不可信）",
          d2.get("reported_actor") == "被伪造的人", str(d2))
    check("自报值等于真主体时不重复记录 reported_actor",
          "reported_actor" not in security.actor_detail(security.LOCAL_ACTOR))


# ============================================================ ASGI 端到端

async def _call(app, path, auth_header=None, method="GET"):
    """最小 ASGI 调用，返回 (status, headers, body)。"""
    headers = []
    if auth_header:
        headers.append((b"authorization", auth_header.encode("latin-1")))
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(),
             "query_string": b"", "headers": headers, "http_version": "1.1",
             "scheme": "http", "server": ("127.0.0.1", 8848), "client": ("127.0.0.1", 1),
             "root_path": ""}
    out = {"status": None, "headers": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = [(k.decode(), v.decode()) for k, v in msg["headers"]]
        elif msg["type"] == "http.response.body":
            out.setdefault("body", b"")
            out["body"] += msg.get("body") or b""

    await app(scope, receive, send)
    return out["status"], dict(out["headers"]), out.get("body", b"")


def test_middleware():
    print("\n--- 5. 中间件端到端（ASGI 直调，不真起服务）---")
    import asyncio
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from main import BasicAuthMiddleware, OPEN_PATHS

    async def home(request):
        return PlainTextResponse("home")

    async def health(request):
        return PlainTextResponse('{"ok":true}')

    inner = Starlette(routes=[Route("/", home), Route("/api/health", health)])
    wrapped = BasicAuthMiddleware(inner)

    orig = os.environ.get("API_AUTH")
    security._resolved["creds"] = ("alice", "s3cret")
    try:
        # --- 鉴权开启 ---
        os.environ["API_AUTH"] = "on"
        st, hd, body401 = asyncio.run(_call(wrapped, "/"))
        check("无凭据 → 401", st == 401, str(st))
        check("401 带 WWW-Authenticate（浏览器才会弹登录框）",
              "www-authenticate" in hd and "Basic" in hd.get("www-authenticate", ""),
              str(hd))
        check("401 的 realm 正确",
              security.REALM in hd.get("www-authenticate", ""), str(hd))
        check("401 响应体是 JSON（前端可解析原因）",
              b"ok" in body401 and "未认证".encode() in body401, str(body401[:80]))

        st2, _, body2 = asyncio.run(_call(wrapped, "/", basic("alice", "s3cret")))
        check("正确凭据 → 200 放行", st2 == 200 and body2 == b"home", f"{st2} {body2!r}")

        st3, _, _ = asyncio.run(_call(wrapped, "/", basic("alice", "bad")))
        check("错误口令 → 401", st3 == 401, str(st3))

        st4, _, body4 = asyncio.run(_call(wrapped, "/api/health"))
        check("健康检查免鉴权（探活不该要凭据）", st4 == 200 and b"ok" in body4,
              f"{st4} {body4!r} OPEN_PATHS={OPEN_PATHS}")

        # --- 鉴权关闭 ---
        os.environ["API_AUTH"] = "off"
        st5, _, body5 = asyncio.run(_call(wrapped, "/"))
        check("API_AUTH=off → 放行（应急）", st5 == 200 and body5 == b"home", str(st5))
        check("API_AUTH=off 时主体为 None（actor 落 local）",
              security.current_principal() is None)
    finally:
        if orig is None:
            os.environ.pop("API_AUTH", None)
        else:
            os.environ["API_AUTH"] = orig
        security._resolved.pop("creds", None)


def main():
    print("=== 应用层鉴权与审计主体验收（评审 P0-2）===")
    test_verify()
    test_credentials_generation()
    test_enabled_flag()
    test_actor()
    test_middleware()
    print(f"\n===== {_TOTAL - _FAILED}/{_TOTAL} 通过 =====")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
