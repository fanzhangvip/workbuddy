# -*- coding: utf-8 -*-
"""登录页（Cookie 会话）验收 —— 取代浏览器原生 Basic 弹框的那一套。

    .venv/Scripts/python.exe tests/test_login_session.py

用 `TestClient` 跑**真实的 ASGI 中间件 + 真实路由**（不是打桩），所以能验证到
「中间件到底有没有发 `WWW-Authenticate`」「Cookie 属性对不对」这类**只有端到端才看得见**
的行为。刻意**不用** `with TestClient(...)`：那样会触发 lifespan（起调度器、连数据库池），
而这里只需要 HTTP 层。

覆盖：
  1. 未登录访问 `/api/*` → 401，且**响应头里没有 `WWW-Authenticate`**（关键：
     发了浏览器就弹原生登录框，我们的登录页就白做了）
  2. 未登录访问静态首页 → 200（登录页得先能加载出来）
  3. 口令错 → 401 且 `ok=false`；口令对 → 下发 `HttpOnly` Cookie
  4. 「记住我」勾与不勾的差别（有没有 `Max-Age`）
  5. 只带 Cookie（**不带** Basic）能访问受保护接口；退出后立刻失效
  6. 过期会话 → 拒绝
  7. Basic 仍然可用（脚本 / curl / 既有测试不受影响）
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import sys
import time

from _harness import check, summary      # noqa: E402

import config                                              # noqa: E402
import db                                                  # noqa: E402
import security                                            # noqa: E402
from fastapi.testclient import TestClient                  # noqa: E402

import main                                                # noqa: E402

client = TestClient(main.app)          # 不进入上下文 → 不触发 lifespan
USER = (config.get("API_USER") or "admin").strip() or "admin"
PWD = config.get("API_PASSWORD") or ""


def _basic(u, p):
    import base64
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def test_unauthenticated_shape():
    print("\n--- 1. 未登录：API 401 但**不发挑战头**，静态首页放行 ---")
    r = client.get("/api/scoring/settings")
    check("未登录访问 /api/* → 401", r.status_code == 401, r.status_code)
    check("★ 响应里没有 WWW-Authenticate（否则浏览器弹原生登录框）",
          "www-authenticate" not in {k.lower() for k in r.headers},
          dict(r.headers))
    check("返回的是可读的 JSON 原因", "未认证" in r.text, r.text[:120])

    r2 = client.get("/")
    check("静态首页对未登录**放行**（登录页才加载得出来）", r2.status_code == 200,
          r2.status_code)

    r3 = client.get("/api/auth/me")
    check("/api/auth/me 免鉴权且返回 authenticated=false",
          r3.status_code == 200 and r3.json().get("authenticated") is False,
          r3.text[:120])


def test_sign_in_flow():
    print("\n--- 2. 登录 / Cookie 属性 / 退出 ---")
    if not PWD:
        check("环境里配了 API_PASSWORD（否则本用例无法进行）", False, "API_PASSWORD 为空")
        return

    bad = client.post("/api/auth/session",
                      json={"username": USER, "password": "definitely-wrong"})
    check("口令错 → 401", bad.status_code == 401, bad.status_code)
    check("口令错 → ok=false 且不落 Cookie",
          bad.json().get("ok") is False and "wb_session" not in bad.cookies,
          bad.text[:120])

    # 不勾「记住我」→ 浏览器会话 Cookie（没有 Max-Age）
    r = client.post("/api/auth/session", json={"username": USER, "password": PWD})
    check("口令对 → 200 且 ok=true", r.status_code == 200 and r.json().get("ok"),
          r.text[:120])
    sid = r.cookies.get(security.COOKIE_NAME)
    check("下发了会话 Cookie", bool(sid), r.cookies)
    raw = r.headers.get("set-cookie", "")
    check("Cookie 是 HttpOnly（JS 读不到）", "httponly" in raw.lower(), raw[:160])
    check("Cookie 是 SameSite=Lax", "samesite=lax" in raw.lower(), raw[:160])
    check("未勾记住我 → **不带 Max-Age**（关浏览器即失效）",
          "max-age" not in raw.lower(), raw[:160])

    # 只带 Cookie（不带 Basic）访问受保护接口
    r2 = client.get("/api/scoring/settings", cookies={security.COOKIE_NAME: sid})
    check("★ 只带 Cookie 就能访问受保护接口（无 Basic 头）", r2.status_code == 200,
          r2.status_code)
    r3 = client.get("/api/auth/me", cookies={security.COOKIE_NAME: sid})
    check("/api/auth/me 认得出这个会话", r3.json().get("user") == USER, r3.text[:120])

    # 退出 → 旧 Cookie 立刻失效
    client.delete("/api/auth/session", cookies={security.COOKIE_NAME: sid})
    r4 = client.get("/api/scoring/settings", cookies={security.COOKIE_NAME: sid})
    check("★ 退出后旧 Cookie 立刻失效（401）", r4.status_code == 401, r4.status_code)


def test_remember_and_expiry():
    print("\n--- 3. 记住我 / 会话过期 / Basic 兼容 ---")
    if not PWD:
        return
    r = client.post("/api/auth/session",
                    json={"username": USER, "password": PWD, "remember": True})
    raw = r.headers.get("set-cookie", "")
    check("勾了记住我 → 带 Max-Age（30 天 = 2592000 秒）",
          "max-age=2592000" in raw.lower(), raw[:200])
    sid = r.cookies.get(security.COOKIE_NAME)

    # 手工把会话改成已过期 → 必须拒绝，并顺手删掉那行
    db.execute("UPDATE login_session SET expires_at = now() - interval '1 minute'"
               " WHERE sid = %s", (sid,))
    r2 = client.get("/api/scoring/settings", cookies={security.COOKIE_NAME: sid})
    check("★ 过期会话被拒（401）", r2.status_code == 401, r2.status_code)
    left = db.query_one("SELECT count(*) AS n FROM login_session WHERE sid=%s", (sid,))
    check("过期的会话行被顺手清掉（不留垃圾）", left["n"] == 0, left)

    # Basic 仍然可用（脚本/curl/既有测试走这条）
    r3 = client.get("/api/scoring/settings", headers=_basic(USER, PWD))
    check("★ HTTP Basic 仍然可用（未被会话机制取代）", r3.status_code == 200,
          r3.status_code)
    r4 = client.get("/api/scoring/settings", headers=_basic(USER, "wrong"))
    check("Basic 口令错 → 401", r4.status_code == 401, r4.status_code)


def test_auth_disabled_passthrough():
    print("\n--- 4. API_AUTH=off 的逃生阀仍然有效 ---")
    orig = security.enabled
    security.enabled = lambda: False
    try:
        r = client.get("/api/scoring/settings")
        check("鉴权关闭 → 直接放行（200）", r.status_code == 200, r.status_code)
        me = client.get("/api/auth/me").json()
        check("鉴权关闭时 /me 说 authenticated=true、auth_enabled=false",
              me.get("authenticated") is True and me.get("auth_enabled") is False, me)
    finally:
        security.enabled = orig


def main():
    started = time.time()
    try:
        test_unauthenticated_shape()
        test_sign_in_flow()
        test_remember_and_expiry()
        test_auth_disabled_passthrough()
    finally:
        n = db.execute("DELETE FROM login_session")
        print(f"\n（已清理 {n} 行登录会话，用时 {time.time() - started:.1f}s）")
    return summary()


if __name__ == "__main__":
    sys.exit(main())
