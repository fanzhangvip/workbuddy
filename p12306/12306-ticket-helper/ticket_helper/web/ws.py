"""WebSocket 实时通道（WB-03 / UI-05 / NT-04）。

两条通道：
- ``/api/logs``   —— 实时日志流（先补发最近 N 条，再增量推送），日志窗不再读文件；
- ``/api/events`` —— 事件流：任务状态变化、出票/候补通知（用于弹窗 + 声音提醒）。

实现要点：
1. **心跳业务**: 服务端定时发送 ping 帧，及时清理已断开的连接，避免线程泄漏
   （长时间挂机的稳定性验收项）；
2. **发送必须串行化**: 一个 ws 连接可能同时收到日志与事件，两个线程并发写会
   破坏帧结构，因此每连接配一把锁；
3. 鉴权走查询参数（浏览器 WebSocket API 无法设置请求头），仅监听本机。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from flask import request

from ticket_helper.errors import AuthError
from ticket_helper.logger import get_log_bus
from ticket_helper.runtime.context import AppContext
from ticket_helper.web.auth import TokenManager

log = logging.getLogger(__name__)

PING_INTERVAL = 25.0
BACKLOG_SIZE = 150


class Client:
    """一个 WebSocket 订阅者。"""

    __slots__ = ("ws", "lock", "closed", "connected_at", "name")

    def __init__(self, ws: Any, name: str = "") -> None:
        self.ws = ws
        self.lock = threading.Lock()
        self.closed = False
        self.connected_at = time.time()
        self.name = name

    def send(self, payload: dict[str, Any]) -> bool:
        if self.closed:
            return False
        try:
            with self.lock:
                self.ws.send(json.dumps(payload, ensure_ascii=False, default=str))
            return True
        except Exception:  # noqa: BLE001 - 连接已断开是最常见情况
            self.closed = True
            return False

    def close(self) -> None:
        self.closed = True
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


class RealtimeHub:
    """实时通道订阅中心。"""

    def __init__(self, context: AppContext) -> None:
        self.context = context
        self._log_clients: list[Client] = []
        self._event_clients: list[Client] = []
        self._lock = threading.RLock()
        self._unsubscribe_log = get_log_bus().subscribe(self._on_log)
        self._unsubscribe_event = context.notifier.subscribe_web(self._on_notification)
        context.scheduler.on_event(self._on_scheduler_event)
        # 账号登录状态变化也要推给前端：否则「添加账号 → 自动登录成功」
        # 这个过程界面完全无感，用户只能靠手动刷新才发现状态变了。
        context.accounts.on_event(self._on_account_event)
        self._pump: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ #
    def register_log(self, client: Client) -> None:
        with self._lock:
            self._log_clients.append(client)
        for entry in get_log_bus().recent(BACKLOG_SIZE):
            client.send({"type": "log", "data": entry})

    def register_event(self, client: Client) -> None:
        with self._lock:
            self._event_clients.append(client)
        client.send({"type": "hello", "data": self.context.snapshot()})
        client.send({"type": "task_status", "data": self.context.scheduler.status_all()})

    def unregister(self, client: Client) -> None:
        with self._lock:
            if client in self._log_clients:
                self._log_clients.remove(client)
            if client in self._event_clients:
                self._event_clients.remove(client)
        client.closed = True

    # ------------------------------------------------------------------ #
    def _on_log(self, entry: dict[str, Any]) -> None:
        self._broadcast(self._log_clients, {"type": "log", "data": entry})

    def _on_notification(self, event: dict[str, Any]) -> None:
        self._broadcast(self._event_clients, {"type": "notify", "data": event})

    def _on_scheduler_event(self, event: str, payload: dict[str, Any]) -> None:
        self._broadcast(self._event_clients, {"type": "task_event", "event": event, "data": payload})
        self._broadcast(self._event_clients, {"type": "task_status", "data": self.context.scheduler.status_all()})

    def _on_account_event(self, event: str, payload: dict[str, Any]) -> None:
        """账号登录态变化 → 前端刷新账号列表。"""
        self._broadcast(self._event_clients, {"type": "user_event", "event": event, "data": payload})

    def _broadcast(self, clients: list[Client], payload: dict[str, Any]) -> None:
        with self._lock:
            targets = list(clients)
        for client in targets:
            if not client.send(payload):
                self.unregister(client)

    # ------------------------------------------------------------------ #
    def start_heartbeat(self) -> None:
        """心跳线程：定期 ping 并清理失效连接。"""
        if self._pump and self._pump.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                self._stop.wait(PING_INTERVAL)
                for clients in (self._log_clients, self._event_clients):
                    with self._lock:
                        targets = list(clients)
                    for client in targets:
                        if time.time() - client.connected_at < PING_INTERVAL:
                            continue
                        try:
                            with client.lock:
                                client.ws.send(json.dumps({"type": "ping"}))
                        except Exception:  # noqa: BLE001
                            self.unregister(client)

        self._pump = threading.Thread(target=_loop, name="ws-heartbeat", daemon=True)
        self._pump.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._unsubscribe_log()
        self._unsubscribe_event()
        for client in list(self._log_clients) + list(self._event_clients):
            client.close()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"log_clients": len(self._log_clients), "event_clients": len(self._event_clients)}


def _authorize(tokens: TokenManager) -> str:
    token = request.args.get("token") or ""
    if not token:
        raise AuthError("WebSocket 未携带登录凭证")
    data = tokens.verify(token)
    return str(data.get("u", ""))


def register_ws(app, context: AppContext, tokens: TokenManager) -> None:
    """注册 WebSocket 路由。"""
    try:
        from flask_sock import Sock
    except ImportError:  # pragma: no cover - 依赖缺失时优雅降级
        log.warning("未安装 flask-sock，实时日志与事件推送不可用（pip install flask-sock）")
        return

    sock = Sock(app)
    hub = RealtimeHub(context)
    hub.start_heartbeat()
    app.config["REALTIME_HUB"] = hub

    @sock.route("/api/logs")
    def ws_logs(ws):  # pragma: no cover - 需要真实 ws 连接
        try:
            _authorize(tokens)
        except AuthError as exc:
            ws.send(json.dumps({"type": "error", "message": exc.message}))
            ws.close()
            return
        client = Client(ws, name="logs")
        hub.register_log(client)
        try:
            while not client.closed:
                message = ws.receive(timeout=300)
                if message is None:
                    break
                if message == "ping":
                    client.send({"type": "pong"})
        except Exception:  # noqa: BLE001
            pass
        finally:
            hub.unregister(client)

    @sock.route("/api/events")
    def ws_events(ws):  # pragma: no cover - 需要真实 ws 连接
        try:
            _authorize(tokens)
        except AuthError as exc:
            ws.send(json.dumps({"type": "error", "message": exc.message}))
            ws.close()
            return
        client = Client(ws, name="events")
        hub.register_event(client)
        try:
            while not client.closed:
                message = ws.receive(timeout=300)
                if message is None:
                    break
                if message == "ping":
                    client.send({"type": "pong"})
                elif message == "snapshot":
                    client.send({"type": "task_status", "data": context.scheduler.status_all()})
        except Exception:  # noqa: BLE001
            pass
        finally:
            hub.unregister(client)
