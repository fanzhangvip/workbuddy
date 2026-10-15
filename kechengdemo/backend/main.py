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

DIST = ROOT / "frontend" / "dist"


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
    scheduler.start()
    print("[app] 数据库就绪；服务地址 http://127.0.0.1:8848")
    yield


app = FastAPI(title="企微聊天记录归档系统", version="1.0.0", lifespan=lifespan)

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
