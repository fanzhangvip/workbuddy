"""桌面端实时通道：优先 WebSocket，不可用时自动降级为轮询。

为什么要有降级
--------------
``QtWebSockets`` **不在** ``PySide6-Essentials`` 里（它在 ``PySide6-Addons`` 里）。
只装 Essentials 时 ``from PySide6.QtWebSockets import QWebSocket`` 直接 ImportError，
于是「实时日志 / 任务状态」整条链路静默失效 ——
界面上只剩一句「实时通道：未连接」，使用者完全不知道是环境缺件还是后台没起。

因此这里做成两条腿：

- **WebSocket 可用** → 走 ``/api/logs`` + ``/api/events``（真正的推送）；
- **不可用** → 自动降级为**轮询**（``/api/logs`` 与 ``/api/tasks/status/all``），
  不引入任何新依赖，功能不缺失，只是延迟略高。

对外暴露 :attr:`transport` 与 :attr:`mode_text`，让界面能如实显示当前模式。
HTTP 请求都放在后台线程里跑，避免网络卡顿时冻住界面。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl, Signal

log = logging.getLogger(__name__)

try:  # QtWebSockets 在 PySide6-Addons 里，Essentials 不含
    from PySide6.QtNetwork import QAbstractSocket
    from PySide6.QtWebSockets import QWebSocket

    HAS_WEBSOCKET = True
except ImportError:  # pragma: no cover - 取决于安装的 PySide6 组件
    QAbstractSocket = None  # type: ignore[assignment]
    QWebSocket = None  # type: ignore[assignment]
    HAS_WEBSOCKET = False


def is_connected_state(state: object) -> bool:
    """判断一个 socket 状态是不是「已连接」。

    为什么要单独抽这个函数（而不是直接 ``state == 3`` 或 ``int(state) == 3``）
    ------------------------------------------------------------------
    PySide6 6.11 起 ``stateChanged`` 发出的是 ``QAbstractSocket.SocketState``
    **枚举**，而该枚举**不支持 ``int()`` 转换**：

        >>> int(QAbstractSocket.SocketState.ConnectedState)
        TypeError: int() argument must be a string, ... not 'SocketState'

    一旦这么写，槽函数会抛异常并沿全局 excepthook 冒出去 ——
    实测后果是**整个桌面客户端崩溃**（打包版里才暴露，因为源码环境当时没装
    QtWebSockets，这段代码根本没执行过）。

    这里同时兼容枚举与旧的整型形态，避免依赖某个 PySide6 版本的具体行为。
    """
    if QAbstractSocket is not None:
        try:
            if state == QAbstractSocket.SocketState.ConnectedState:
                return True
        except Exception:  # noqa: BLE001 - 枚举比较本身不应成为崩溃来源
            pass
    # 兜底：QAbstractSocket.SocketState.ConnectedState 的历史数值为 3
    return state == 3


def is_connecting_state(state: object) -> bool:
    """是否处于「正在连接」的过渡状态。

    为什么需要它：``connect_all()`` 会依次打开两个 socket（日志 / 事件）。
    第一个 socket 刚进入 ``ConnectingState`` 时，第二个还没创建 ——
    若把这一刻当成「掉线」，就会启动重连定时器；等两个都连上后**那个定时器仍在跑**，
    到点便会把好好的连接关掉重开，表现为 websocket/offline **反复横跳**。
    所以过渡状态既不算掉线，也不该触发重连。
    """
    if QAbstractSocket is not None:
        try:
            if state in (
                QAbstractSocket.SocketState.ConnectingState,
                QAbstractSocket.SocketState.HostLookupState,
            ):
                return True
        except Exception:  # noqa: BLE001
            pass
    # 兜底：HostLookupState=1 / ConnectingState=2（历史数值）
    return state in (1, 2)

#: 轮询间隔（毫秒）。日志 2 秒一次已足够“准实时”，且不会给本地后台压力。
LOG_POLL_INTERVAL_MS = 2000
TASK_POLL_INTERVAL_MS = 3000
#: 单次轮询最多取多少条日志（去重后只把新增的推给界面）
LOG_FETCH_LIMIT = 100


class RealtimeClient(QObject):
    """带自动重连的 WebSocket 客户端；不可用时降级为轮询。"""

    log_received = Signal(dict)
    notification_received = Signal(dict)
    task_status_received = Signal(list)
    #: 账号登录态变化（登录成功/失败/风控）—— 用来即时刷新账号列表
    user_event_received = Signal(str, dict)
    connection_changed = Signal(bool)
    #: 传输方式变化：websocket / polling / offline
    transport_changed = Signal(str)

    RECONNECT_DELAY_MS = 4000

    def __init__(self, api_client: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.api = api_client
        self._log_socket = None
        self._event_socket = None
        self._connected = False
        self._stopping = False
        self._transport = "offline"

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self.connect_all)

        # ---- 轮询降级用 ----
        self._seen_logs: set[str] = set()
        self._seen_order: list[str] = []
        self._poll_busy = False
        #: 轮询是否处于「活动」状态。
        #: 用来挡住**迟到的轮询结果**：轮询在子线程里跑，一次请求返回时
        #: WebSocket 可能已经连上了；若不判断，那次返回会把传输状态从
        #: websocket 覆盖回 polling，状态栏就会显示错误的模式。
        self._polling_active = False
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(LOG_POLL_INTERVAL_MS)
        self._log_timer.timeout.connect(self._poll_logs)
        self._task_timer = QTimer(self)
        self._task_timer.setInterval(TASK_POLL_INTERVAL_MS)
        self._task_timer.timeout.connect(self._poll_tasks)

    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        """是否有可用的实时通道（WebSocket 或轮询降级都算）。"""
        return True

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def transport(self) -> str:
        return self._transport

    @property
    def mode_text(self) -> str:
        return {
            "websocket": "已连接（WebSocket）",
            "polling": "轮询模式（未安装 QtWebSockets）",
            "offline": "未连接",
        }.get(self._transport, "未连接")

    def _set_transport(self, transport: str, *, connected: bool) -> None:
        if transport != self._transport:
            log.info("实时通道切换为: %s", transport)
            self._transport = transport
            self.transport_changed.emit(transport)
        if connected != self._connected:
            self._connected = connected
            self.connection_changed.emit(connected)

    # ------------------------------------------------------------------ #
    def connect_all(self) -> None:
        """建立连接。没有 QtWebSockets 时直接进入轮询模式。"""
        if self._stopping or not getattr(self.api, "token", ""):
            return
        if not HAS_WEBSOCKET:
            # 关键：不能让这里直接 return —— 那会让实时功能彻底静默失效
            self._start_polling()
            return
        self._close_sockets()
        self._log_socket = self._open("/api/logs", self._on_log_message)
        self._event_socket = self._open("/api/events", self._on_event_message)

    # ------------------------------------------------------------------ #
    # WebSocket 分支
    # ------------------------------------------------------------------ #
    def _open(self, path: str, handler) -> Any:
        socket = QWebSocket()
        socket.textMessageReceived.connect(handler)
        # 注意：**不要**对 state 做 int() —— 新版 PySide6 发的是枚举，int() 会抛
        # TypeError 并把整个客户端带崩。交给 is_connected_state 统一判断。
        socket.stateChanged.connect(lambda state, s=socket: self._on_socket_state(state, s))
        # 把失败原因记下来：不然「连不上」只能靠猜（端口错？令牌过期？后台没起？）
        socket.errorOccurred.connect(
            lambda _err, s=socket: log.warning("WebSocket 出错(%s): %s", path, s.errorString())
        )
        try:
            socket.open(QUrl(self.api.ws_url(path)))
        except Exception as exc:  # noqa: BLE001
            log.warning("WebSocket 连接失败(%s): %s", path, exc)
        return socket

    def _on_socket_state(self, state: object, socket: Any) -> None:
        """socket 状态变化。

        整个函数体包在 try 里：这是 Qt 信号槽，异常会冒到全局 excepthook，
        实测会把桌面客户端直接带崩。实时通道属于「有则更好」的能力，
        绝不能因为它的内部异常影响主程序。
        """
        try:
            if is_connected_state(state):
                # 连上了就取消待执行的重连：否则它到点会把当前连接关掉重开，
                # 表现为状态栏 websocket/offline 反复横跳。
                self._reconnect_timer.stop()
                self._stop_polling()
                self._set_transport("websocket", connected=True)
                return

            sockets = (self._log_socket, self._event_socket)
            still_open = any(s is not None and is_connected_state(s.state()) for s in sockets)
            if still_open:
                return
            # 还在连接途中（另一个 socket 尚未就绪）→ 不是掉线，别急着重连
            if any(s is not None and is_connecting_state(s.state()) for s in sockets):
                return

            self._set_transport("offline", connected=False)
            if not self._stopping:
                self._reconnect_timer.start(self.RECONNECT_DELAY_MS)
        except Exception as exc:  # noqa: BLE001 - 实时通道的异常不得影响主程序
            log.warning("处理 WebSocket 状态变化时出错（已忽略）: %s", exc)

    def _close_sockets(self) -> None:
        for socket in (self._log_socket, self._event_socket):
            if socket is None:
                continue
            try:
                socket.close()
                socket.deleteLater()
            except Exception:  # noqa: BLE001
                pass
        self._log_socket = self._event_socket = None

    # ------------------------------------------------------------------ #
    # 轮询分支（无 QtWebSockets 时的兜底）
    # ------------------------------------------------------------------ #
    def _start_polling(self) -> None:
        self._polling_active = True
        if not self._log_timer.isActive():
            self._log_timer.start()
        if not self._task_timer.isActive():
            self._task_timer.start()
        self._poll_logs()
        self._poll_tasks()

    def _stop_polling(self) -> None:
        self._polling_active = False
        self._log_timer.stop()
        self._task_timer.stop()

    def _poll_logs(self) -> None:
        self._run_async(self._fetch_logs)

    def _poll_tasks(self) -> None:
        self._run_async(self._fetch_tasks)

    def _run_async(self, func) -> None:
        if self._poll_busy or self._stopping:
            return
        self._poll_busy = True

        def _worker() -> None:
            try:
                func()
            except Exception as exc:  # noqa: BLE001 - 轮询失败不该影响界面
                log.debug("实时轮询失败: %s", exc)
            finally:
                self._poll_busy = False

        threading.Thread(target=_worker, name="realtime-poll", daemon=True).start()

    def _fetch_logs(self) -> None:
        """拉取最近日志，只把**新增的**推给界面（否则会把日志窗刷重复）。"""
        data = self.api.get(f"/api/logs?limit={LOG_FETCH_LIMIT}")
        entries = data.get("logs") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return
        fresh = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = "|".join(str(entry.get(k, "")) for k in ("ts", "level", "logger", "message"))
            if key in self._seen_logs:
                continue
            self._seen_logs.add(key)
            self._seen_order.append(key)
            fresh.append(entry)
        # 记忆上限，避免长时间挂机后无限增长
        while len(self._seen_order) > 2000:
            self._seen_logs.discard(self._seen_order.pop(0))
        for entry in fresh:
            self.log_received.emit(entry)
        if (fresh or entries) and self._polling_active:
            # 只在轮询仍然是当前方式时才改状态：这一次请求可能是「迟到」的 ——
            # 它返回时 WebSocket 已经连上，此时再把状态改成 polling 是错的。
            self._set_transport("polling", connected=True)

    def _fetch_tasks(self) -> None:
        data = self.api.get("/api/tasks/status/all")
        tasks = data.get("tasks") if isinstance(data, dict) else None
        if isinstance(tasks, list):
            self.task_status_received.emit(tasks)
            if self._polling_active:
                self._set_transport("polling", connected=True)

    # ------------------------------------------------------------------ #
    def _on_log_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            return
        if payload.get("type") == "log":
            self.log_received.emit(payload.get("data") or {})

    def _on_event_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            return
        kind = payload.get("type")
        if kind == "notify":
            self.notification_received.emit(payload.get("data") or {})
        elif kind == "task_status":
            self.task_status_received.emit(payload.get("data") or [])
        elif kind == "user_event":
            self.user_event_received.emit(str(payload.get("event") or ""), payload.get("data") or {})

    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        self._stopping = True
        self._reconnect_timer.stop()
        self._stop_polling()
        self._close_sockets()
