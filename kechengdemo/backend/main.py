# -*- coding: utf-8 -*-
"""企微聊天记录归档系统 —— 后端入口。

启动：
    python -m uvicorn backend.main:app --host 127.0.0.1 --port 8848
或（在项目根目录）：
    python backend/main.py
"""
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BACKEND_DIR = Path(__file__).resolve().parent
ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api_auth                      # noqa: E402
import api_chat                      # noqa: E402
import api_eval                      # noqa: E402
import api_review                    # noqa: E402
import api_scoring                   # noqa: E402
import api_sync                      # noqa: E402
import db                            # noqa: E402
import scheduler                     # noqa: E402
import security                      # noqa: E402

DIST = ROOT / "frontend" / "dist"
#: 免鉴权路径：健康检查是探活用的，不应要求凭据
OPEN_PATHS = ("/api/health",)


class BasicAuthMiddleware:
    """纯 ASGI 中间件：应用层 HTTP Basic Auth + 主体注入（评审 P0-2）。

    为什么不用 ``@app.middleware("http")``（BaseHTTPMiddleware）：
    它在独立 task 里调用下游 app，**在中间件里 set 的 contextvar 传不到路由函数**
    （审计取主体会拿到 None）。纯 ASGI 中间件与下游同一 context，contextvar 正常透传。

    为什么连静态资源也一起保护：Basic 的浏览器登录框**只在顶层导航的 401 上弹出**，
    XHR/fetch 的 401 不会弹。若只保护 `/api/*`，SPA 页面能加载但所有接口都 401，
    用户既拿不到凭据也没法登录 → 必须让**首次页面加载**触发认证。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}

        if not security.enabled() or path in OPEN_PATHS:
            token = security.set_principal(None)
            try:
                await self.app(scope, receive, send)
            finally:
                security.reset_principal(token)
            return

        who = security.verify(headers.get("authorization"))
        if who is None:
            body = ('{"ok":false,"reason":"未认证：请使用 HTTP Basic 凭据",'
                    '"hint":"用户名见 .env API_USER；口令见 .env API_PASSWORD '
                    '或后端启动日志"}').encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate",
                     f'Basic realm="{security.REALM}"'.encode("latin-1")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        token = security.set_principal(who)
        try:
            await self.app(scope, receive, send)
        finally:
            security.reset_principal(token)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # E2 自愈：修「有行但无 current」与「superseded_by 悬空」（由**外部删除**造成的破损，
    # 插入路径补不了）。幂等、几毫秒；不做的话破损客户会从评分列表里静默消失。
    try:
        import scoring_service
        r = scoring_service.repair_current_flags()
        if r.get("no_current_fixed") or r.get("dangling_cleared"):
            print(f"[app] is_current 自愈：{r}")
    except Exception as e:                      # noqa: BLE001  自愈失败不该拦住启动
        print(f"[app] is_current 自愈失败（不影响启动）：{e}")
    # 外循环恢复（修硬伤 3）：扫出心跳超时的僵尸任务并**交出检查点**。
    # 刻意放在启动路径上：进程崩溃来不及改状态时，只有启动时才有机会发现
    # —— `is_running()` 的"只回收不恢复"正是改造前的缺口。
    try:
        import task_runtime
        rec = task_runtime.recover_stale()
        if rec.get("recovered"):
            ids = [r["task_id"] for r in rec["resumable"]]
            print(f"[app] 僵尸任务回收 {rec['recovered']} 个；可续跑：{ids}"
                  f"（POST /api/scoring/tasks/{{id}}/resume 或 task_runtime.resume()）")
    except Exception as e:                      # noqa: BLE001  恢复失败不该拦住启动
        print(f"[app] 僵尸任务回收失败（不影响启动）：{e}")
    scheduler.start()
    print(security.startup_report())
    # Schema 漂移自检（评审 P1-3）：DDL 曾散在三处，「老库缺列」只有跑到那行才暴露。
    # 这里启动时比一次，把问题提前到「启动日志里就能看见」。
    try:
        import schema_check
        sc = schema_check.check()
        if sc.get("drift"):
            print(f"[app] ⚠️ schema 漂移：缺表 {sc.get('missing_tables')}｜"
                  f"缺列 {sc.get('missing_columns')}｜"
                  f"库中多余列（schema.sql 未声明）{sc.get('extra_columns')}")
            print("[app]    → 老库缺列可跑 db.init_db() 补齐；其余请把 DDL 补进 schema.sql")
        else:
            print(f"[app] schema 一致（{sc.get('tables_in_schema')} 张表，无漂移）")
    except Exception as e:                      # noqa: BLE001  自检失败不该拦住启动
        print(f"[app] schema 自检跳过：{type(e).__name__}: {e}")
    # 默认值清单自检（评审 §5.5）：防「保护措施被悄悄关掉却没人发现」。
    # 只报**保护类**「声称默认开、实测是关」的情形 —— 观测类默认关是设计意图。
    try:
        import defaults_manifest
        dm = defaults_manifest.verify()
        if dm.get("guard_mismatches"):
            print(f"[app] ⚠️ 保护类配置被关闭：{dm['guard_mismatches']}")
        else:
            print("[app] 保护类配置齐备（详见 GET /api/scoring/defaults-manifest）")
    except Exception as e:                      # noqa: BLE001
        print(f"[app] 默认值清单自检跳过：{type(e).__name__}: {e}")
    print("[app] 数据库就绪；服务地址 http://127.0.0.1:8848")
    yield


app = FastAPI(title="企微聊天记录归档系统", version="1.0.0", lifespan=lifespan)

# ⚠️ 顺序很重要：`add_middleware` 是**前插**，最后加的在最外层。
# 这里先加鉴权、后加 CORS → **CORS 在最外层**：未认证的 401 响应也带 CORS 头，
# 否则浏览器会把 401 显示成跨域错误，页面拿不到 `WWW-Authenticate`、弹不出登录框。
# 两个中间件都是纯 ASGI（不新建 task），contextvar 主体能正常透传到路由。
app.add_middleware(BasicAuthMiddleware)      # 评审 P0-2：应用层鉴权 + 审计主体注入
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_auth.router)
app.include_router(api_sync.router)
app.include_router(api_chat.router)
app.include_router(api_scoring.router)
app.include_router(api_review.router)
app.include_router(api_eval.router)


@app.get("/api/health")
def health():
    """健康检查。**不返回连接串信息**（曾返回 host:port/dbname；在无鉴权的端点里没有理由暴露）。"""
    return {"ok": True}


# ---------------- 前端静态资源（构建后自动托管）----------------
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        target = DIST / full_path
        if full_path and target.is_file():
            return FileResponse(target)
        return FileResponse(DIST / "index.html")
else:
    @app.get("/")
    def no_frontend():
        return JSONResponse({
            "message": "前端尚未构建。开发模式请访问 http://127.0.0.1:5173",
            "api_docs": "/docs",
        })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8848, reload=False)
