"""Flask 应用（4.10 / WB-01 ~ WB-07）。

职责边界（遵守分层约定）：
- 路由层只做「解析请求 → 调 AppContext/服务 → 格式化响应」，**不写业务逻辑**；
- 所有业务错误由 :mod:`ticket_helper.errors` 的类型化异常承载，统一在
  :func:`_register_error_handlers` 里映射为规范化 JSON，绝不外泄堆栈；
- 每个请求分配 ``request_id`` 并写进日志，便于跨模块追踪。

桌面客户端与 Web 网页共用本服务的 REST + WebSocket，因此双端状态天然一致
（设计文档 11.1 方案 A）。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from ticket_helper import __version__
from ticket_helper.errors import TicketHelperError
from ticket_helper.logger import new_request_id, request_id_var
from ticket_helper.runtime.context import AppContext
from ticket_helper.web.api import register_api
from ticket_helper.web.auth import TokenManager
from ticket_helper.web.ws import register_ws

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(context: AppContext | None = None, *, start_background: bool = False) -> Flask:
    """创建 Flask 应用。

    ``start_background=True`` 时会在启动时登录账号并恢复任务；
    Web 独立进程用 True，桌面端（后台已由自己拉起）也使用 True，
    纯单测场景保持 False 以避免真实副作用。
    """
    if context is None:
        from ticket_helper.runtime.context import get_context

        context = get_context()

    app = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
    )
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False
    app.config["APP_CONTEXT"] = context

    web_cfg = context.config.web
    secret = web_cfg.secret or context.cipher.encrypt("web-secret")[:48]
    tokens = TokenManager(secret, ttl=web_cfg.token_ttl)
    app.config["TOKEN_MANAGER"] = tokens

    # CORS：显式来源（生产禁用通配符，安全头要求）
    CORS(
        app,
        resources={r"/api/*": {"origins": web_cfg.allowed_origins}},
        supports_credentials=False,
        allow_headers=["Content-Type", "Authorization", "X-Auth-Token", "X-Request-Id"],
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    )

    _register_request_hooks(app, context)
    register_api(app, context, tokens)
    register_ws(app, context, tokens)
    _register_static(app)
    _register_error_handlers(app)
    _register_security_headers(app)

    if start_background:
        context.start()

    log.info(
        "Web 应用已创建: version=%s cors=%s",
        __version__,
        "strict" if web_cfg.strict_cors else "wildcard",
    )
    return app


# --------------------------------------------------------------------------- #
def _register_request_hooks(app: Flask, context: AppContext) -> None:
    @app.before_request
    def _assign_request_id() -> None:
        request_id = request.headers.get("X-Request-Id") or new_request_id()
        request_id_var.set(request_id)
        request.environ["th.request_id"] = request_id
        request.environ["th.started_at"] = time.perf_counter()

    @app.after_request
    def _log_request(response):
        if request.path.startswith("/api/"):
            latency = int((time.perf_counter() - request.environ.get("th.started_at", time.perf_counter())) * 1000)
            level = logging.DEBUG if response.status_code < 400 else logging.WARNING
            # 只记录路径与方法，绝不记录请求体（可能含密码）
            log.log(
                level,
                "%s %s -> %s (%dms)",
                request.method,
                request.path,
                response.status_code,
                latency,
            )
        return response


def _register_static(app: Flask) -> None:
    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/favicon.ico")
    def favicon():
        icon = STATIC_DIR / "favicon.ico"
        if icon.exists():
            return send_from_directory(STATIC_DIR, "favicon.ico")
        return ("", 204)

    @app.get("/health")
    def health():
        """健康检查（无需鉴权，便于容器探针）。"""
        return jsonify({"status": "ok", "version": __version__, "uptime": int(time.time() - app.config["APP_CONTEXT"].started_at)})

    @app.get("/ready")
    def ready():
        """就绪检查：后台是否真正可用（账号与调度器状态）。"""
        ctx: AppContext = app.config["APP_CONTEXT"]
        accounts = ctx.accounts.snapshot()
        ready_flag = bool(accounts["online"]) or accounts["total"] == 0
        return (
            jsonify(
                {
                    "status": "ready" if ready_flag else "not_ready",
                    "accounts_online": accounts["online"],
                    "accounts_total": accounts["total"],
                    "scheduler": ctx.scheduler.summary(),
                }
            ),
            200 if ready_flag else 503,
        )


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(TicketHelperError)
    def _handle_business_error(exc: TicketHelperError):
        log.warning("业务异常 %s: %s", exc.code, exc.message)
        return jsonify(exc.to_dict()), exc.http_status

    @app.errorhandler(404)
    def _handle_404(_exc):
        return jsonify({"code": "not_found", "message": "接口不存在"}), 404

    @app.errorhandler(405)
    def _handle_405(_exc):
        return jsonify({"code": "method_not_allowed", "message": "请求方法不被允许"}), 405

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):  # noqa: ANN001
        # 只把「安全可展示」的信息返回客户端，细节进日志
        log.exception("未处理异常: %s", exc)
        return jsonify({"code": "internal_error", "message": "服务内部错误，请查看日志"}), 500


def _register_security_headers(app: Flask) -> None:
    @app.after_request
    def _security_headers(response):
        # 所有响应都带回请求 ID：出问题时用户能直接把 ID 交给排查方
        request_id = request.environ.get("th.request_id")
        if request_id:
            response.headers["X-Request-Id"] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # 本地工具不引入外部 CDN，因此可以较严格地限制
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:",
        )
        return response


def serve(context: AppContext | None = None, *, host: str | None = None, port: int | None = None, threaded: bool = True) -> None:
    """启动 Web 服务（阻塞）。"""
    ctx = context
    if ctx is None:
        from ticket_helper.runtime.context import get_context

        ctx = get_context()
    app = create_app(ctx, start_background=True)
    web_cfg = ctx.config.web
    bind_host = host or web_cfg.host
    bind_port = int(port or web_cfg.port)
    log.info("Web 服务监听 http://%s:%s", bind_host, bind_port)
    # 使用 werkzeug 原生服务器 + 线程模式：本工具是单机自用，
    # 无需引入 gunicorn/waitress 的额外复杂度；flask-sock 也依赖其线程模型。
    app.run(host=bind_host, port=bind_port, threaded=threaded, use_reloader=False, debug=False)
