"""桌面端 API 客户端（设计文档 11.1 方案 A）。

桌面 UI **不直连引擎**，而是通过本地回环 HTTP/WS 调用 ``web/`` 的同一套接口。
这样做的好处：
- 桌面与网页共用一套调度/核心/存储，天然不会出现「两端数据不一致」；
- 引擎只需保证一种调用契约，避免双份业务逻辑；
- 桌面壳可以随时崩掉重启，后台继续挂机抢票。

约定：调用方拿到的是已解析的 dict；HTTP 错误统一抛 :class:`ApiError`，
由 UI 层转成状态栏/弹窗提示。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

DEFAULT_BASE = "http://127.0.0.1:8008"


class ApiError(Exception):
    """桌面端调用后台失败。"""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class ApiClient:
    """本地后台 REST 客户端。"""

    def __init__(self, base_url: str = DEFAULT_BASE, *, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        # ★ 桌面端只与**本机**后端通话（127.0.0.1），永远不许走代理：
        # 机器上有全局代理时，requests 默认会把本机请求也代理出去，
        # 表现是「界面连不上后端」，报错却是难懂的 ProxyError。
        self.session.trust_env = False
        self.token = ""
        self.user = ""

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #
    def request(self, method: str, path: str, *, body: dict | None = None, timeout: float | None = None) -> Any:
        url = f"{self.base_url}{path}"
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.session.request(
                method.upper(),
                url,
                json=body,
                headers=headers,
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(f"无法连接后台服务（{exc}）", status=0) from exc

        if response.status_code == 401:
            self.token = ""
            raise ApiError("登录已过期，请重新登录", status=401)

        payload: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.status_code >= 400:
            message = (payload or {}).get("message") if isinstance(payload, dict) else None
            raise ApiError(message or f"请求失败（HTTP {response.status_code}）", status=response.status_code)
        return payload

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: dict | None = None, **kwargs: Any) -> Any:
        return self.request("POST", path, body=body, **kwargs)

    def put(self, path: str, body: dict | None = None, **kwargs: Any) -> Any:
        return self.request("PUT", path, body=body, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    # ------------------------------------------------------------------ #
    # 就绪 / 登录
    # ------------------------------------------------------------------ #
    def ping(self, *, timeout: float = 2.0) -> dict[str, Any] | None:
        """探测后台是否已就绪（无需令牌，仅本机可调用）。"""
        try:
            return self.request("GET", "/api/internal/ping", timeout=timeout)
        except ApiError:
            return None

    def login(self, user: str, password: str) -> bool:
        payload = self.post("/api/auth/login", {"user": user, "pass": password})
        self.token = str(payload.get("token", ""))
        self.user = str(payload.get("user", user))
        return bool(self.token)

    def logout(self) -> None:
        try:
            self.post("/api/auth/logout")
        except ApiError:
            pass
        self.token = ""

    # ------------------------------------------------------------------ #
    # 业务封装
    # ------------------------------------------------------------------ #
    def list_users(self) -> list[dict[str, Any]]:
        return self.get("/api/users").get("users", [])

    def create_user(self, username: str, password: str, heartbeat: int = 120) -> dict[str, Any]:
        return self.post("/api/users", {"username": username, "password": password, "heartbeat": heartbeat})

    def update_user(self, user_id: int, **fields: Any) -> dict[str, Any]:
        return self.put(f"/api/users/{user_id}", fields)

    def toggle_user(self, user_id: int, enabled: bool) -> dict[str, Any]:
        return self.put(f"/api/users/{user_id}/state", {"enabled": enabled})

    def delete_user(self, user_id: int) -> dict[str, Any]:
        return self.delete(f"/api/users/{user_id}")

    def login_user(self, user_id: int) -> dict[str, Any]:
        return self.post(f"/api/users/{user_id}/login")

    def refresh_user_verify(self, user_id: int) -> dict[str, Any]:
        """重新检测人证核验状态（做完 12306 App 的核验后回来刷新用）。"""
        return self.post(f"/api/users/{user_id}/verify/refresh")

    # ------------------------------------------------------------------ #
    # 个人中心：账号信息 / 乘车人 / 我的订单（AC-06 / OD-01）
    # ------------------------------------------------------------------ #
    def user_profile(
        self,
        user_id: int,
        *,
        with_passengers: bool = True,
        with_pending: bool = True,
        with_orders: bool = False,
        where: str = "G",
        refresh: bool = False,
    ) -> dict[str, Any]:
        """一次拿到账号档案（账号信息 / 乘车人 / 未完成订单，订单按需）。"""
        query = (
            f"?with_passengers={int(with_passengers)}"
            f"&with_pending={int(with_pending)}"
            f"&with_orders={int(with_orders)}"
            f"&where={where}"
            f"&refresh={int(refresh)}"
        )
        return self.get(f"/api/users/{user_id}/profile{query}")

    def user_orders(
        self,
        user_id: int,
        *,
        where: str = "G",
        page: int = 1,
        size: int = 10,
        keyword: str = "",
        start: str = "",
        end: str = "",
    ) -> dict[str, Any]:
        """「我的订单」分页查询。``where``：G 未出行 / H 历史。"""
        query = f"?where={where}&page={page}&size={size}&kw={quote(keyword or '')}"
        if start:
            query += f"&start={quote(start)}"
        if end:
            query += f"&end={quote(end)}"
        return self.get(f"/api/users/{user_id}/orders{query}")

    def user_passengers(self, user_id: int, *, refresh: bool = False) -> list[dict[str, Any]]:
        return self.get(f"/api/users/{user_id}/passengers?refresh={int(refresh)}").get("passengers", [])

    # ------------------------------------------------------------------ #
    # 扫码登录（AC-08）
    # ------------------------------------------------------------------ #
    def start_qr_login(self, *, username_hint: str = "", ttl: float | None = None) -> dict[str, Any]:
        """申请登录二维码，返回 ``{uuid, image, state, expires_in, ...}``。"""
        body: dict[str, Any] = {}
        if username_hint:
            body["username_hint"] = username_hint
        if ttl is not None:
            body["ttl"] = ttl
        return self.post("/api/users/qr/start", body)

    def qr_login_status(self, uuid: str, *, seen: str = "") -> dict[str, Any]:
        """查询一次扫码状态（轮询由 UI 侧的定时器驱动）。

        ``seen`` 传界面当前显示的那张二维码的 uuid：官方二维码只有约 20 秒寿命，
        后台会自动换码，只有换了才会把新图片带回来（避免每次轮询都重传 PNG）。
        """
        query = f"/api/users/qr/status?uuid={quote(str(uuid), safe='')}"
        if seen:
            query += f"&seen={quote(str(seen), safe='')}"
        return self.get(query)

    def cancel_qr_login(self, uuid: str) -> dict[str, Any]:
        return self.post("/api/users/qr/cancel", {"uuid": uuid})

    def list_tasks(self) -> list[dict[str, Any]]:
        return self.get("/api/tasks").get("tasks", [])

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/api/tasks", payload)

    def update_task(self, task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.put(f"/api/tasks/{task_id}", payload)

    def delete_task(self, task_id: int) -> dict[str, Any]:
        return self.delete(f"/api/tasks/{task_id}")

    def start_task(self, task_id: int) -> dict[str, Any]:
        return self.post(f"/api/tasks/{task_id}/start")

    def stop_task(self, task_id: int) -> dict[str, Any]:
        return self.post(f"/api/tasks/{task_id}/stop")

    def task_status_all(self) -> dict[str, Any]:
        return self.get("/api/tasks/status/all")

    def task_remain(self, task_id: int) -> dict[str, Any]:
        return self.get(f"/api/tasks/{task_id}/remain")

    def train_options(
        self,
        *,
        dates: str,
        from_stations: str,
        to_stations: str,
        account: str = "",
        refresh: bool = False,
    ) -> dict[str, Any]:
        """从 12306 拉取候选车次（任务配置页勾选白/黑名单用）。

        超时给得比普通请求长：要按「日期 × 起止站」逐个查，
        还要带上被拒后的重新热身重试，慢起来十几秒很正常。
        """
        query = (
            f"?dates={quote(dates or '')}"
            f"&from={quote(from_stations or '')}"
            f"&to={quote(to_stations or '')}"
        )
        if account:
            query += f"&account={quote(account)}"
        if refresh:
            query += "&refresh=1"
        return self.get(f"/api/trains/options{query}", timeout=90.0)

    def orders(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = f"?limit={limit}" + (f"&status={status}" if status else "")
        return self.get(f"/api/orders{query}").get("orders", [])

    def notifications(self, limit: int = 100) -> dict[str, Any]:
        return self.get(f"/api/notifications?limit={limit}")

    def test_notify(self, channel: str | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """测试通知渠道。

        ``config`` 可传该渠道的临时配置：**用界面上当前填的值去测，不必先保存**。
        否则「填完 → 保存 → 测试」这个来回会让人以为「必须先存才能测」，
        而存了一份错的配置本身就是污染。
        """
        body: dict[str, Any] = {}
        if channel:
            body["channel"] = channel
        if config:
            body["config"] = config
        return self.post("/api/notify/test", body)

    def stats(self) -> dict[str, Any]:
        return self.get("/api/stats")

    def system_snapshot(self) -> dict[str, Any]:
        return self.get("/api/system/snapshot")

    def start_system(self) -> dict[str, Any]:
        return self.post("/api/system/start")

    def stop_system(self) -> dict[str, Any]:
        return self.post("/api/system/stop")

    def recent_logs(self, limit: int = 300, level: str | None = None) -> list[dict[str, Any]]:
        query = f"?limit={limit}" + (f"&level={level}" if level else "")
        return self.get(f"/api/logs{query}").get("logs", [])

    def get_config(self) -> dict[str, Any]:
        return self.get("/api/config")

    def update_config(self, section: str, values: dict[str, Any]) -> dict[str, Any]:
        return self.put(f"/api/config/{section}", values)

    def config_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """配置变更历史（网页端「系统设置」页读的是同一条接口）。"""
        return self.get(f"/api/config/history?limit={limit}").get("history", []) or []

    def config_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """配置变更历史（网页端「系统设置」页读的是同一条接口）。"""
        return self.get(f"/api/config/history?limit={limit}").get("history", []) or []

    def stations(self, keyword: str = "") -> list[str]:
        return self.get(f"/api/stations?kw={quote(keyword or '')}").get("stations", [])

    def station_items(self, keyword: str = "", *, limit: int = 60) -> list[dict[str, Any]]:
        """站点检索（任务配置页的站点选择器用）：返回 ``[{name, code}]``。

        ``keyword`` 留空时后端给一组常用站，所以「点开就能选」，
        不必先猜一个关键字。
        """
        query = f"?kw={quote(keyword or '')}&limit={int(limit)}"
        return self.get(f"/api/stations{query}").get("items", [])

    def captcha_pending(self) -> list[dict[str, Any]]:
        return self.get("/api/captcha/pending").get("tasks", [])

    def submit_captcha(self, task_id: str, answer: str) -> dict[str, Any]:
        return self.post("/api/captcha/submit", {"id": task_id, "answer": answer})

    # ------------------------------------------------------------------ #
    def ws_url(self, path: str) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[-1]
        return f"{scheme}://{host}{path}?token={self.token}"


# --------------------------------------------------------------------------- #
class BackendLauncher:
    """负责「后台没起就把它拉起来」。

    这是「双击 exe 即用」的关键一环（UI-10/UI-11）：
    桌面客户端自己不跑引擎，只在需要时把本地后台进程拉起，
    然后等它就绪即可。
    """

    def __init__(self, client: ApiClient) -> None:
        self.client = client
        self.process: Any = None
        self.command: list[str] = []

    # ------------------------------------------------------------------ #
    # 端口
    # ------------------------------------------------------------------ #
    @staticmethod
    def is_port_available(port: int, host: str = "127.0.0.1") -> bool:
        """端口是否可绑定（用于识别「被别的程序占用」的情况）。"""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                return False
        return True

    @classmethod
    def find_free_port(cls, preferred: int, host: str = "127.0.0.1", *, attempts: int = 30) -> int | None:
        """从 preferred 开始往后找一个可用端口。

        为什么需要：用户双击两次、或上一次的后台没退干净时，8008 会被占用。
        如果此时直接报「启动失败」，用户看到的就是「打开就崩了」。
        换一个端口继续跑，比报错有用得多。
        """
        for offset in range(attempts):
            candidate = preferred + offset
            if candidate > 65535:
                break
            if cls.is_port_available(candidate, host):
                return candidate
        return None

    def is_ready(self) -> bool:
        return self.client.ping() is not None

    def build_command(self, *, port: int | None = None) -> list[str]:
        """构造启动后台的命令（区分开发运行与打包运行）。"""
        import sys
        from pathlib import Path

        port_args = ["--port", str(port)] if port else []
        if getattr(sys, "frozen", False):
            # 打包后：直接复用同一个 exe，用子命令区分模式
            return [sys.executable, "web", *port_args]
        root = Path(__file__).resolve().parent.parent
        return [sys.executable, str(root / "main.py"), "web", *port_args]

    def start(self, *, port: int | None = None, wait: float = 45.0) -> bool:
        """拉起后台并等待就绪。已在运行则直接返回 True。"""
        if self.is_ready():
            log.info("检测到后台服务已在运行，直接复用")
            return True

        import subprocess
        import sys

        self.command = self.build_command(port=port)
        creationflags = 0
        if sys.platform == "win32":
            # 不弹黑窗；后台随桌面端一起退出的场景由用户显式停止
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        log.info("正在启动后台服务: %s", " ".join(self.command))
        try:
            self.process = subprocess.Popen(
                self.command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("后台启动失败: %s", exc)
            return False

        deadline = time.time() + wait
        while time.time() < deadline:
            if self.is_ready():
                log.info("后台服务已就绪（%.1fs）", wait - (deadline - time.time()))
                return True
            if self.process.poll() is not None:
                log.error("后台进程提前退出，返回码=%s", self.process.returncode)
                return False
            time.sleep(0.5)
        log.error("等待后台就绪超时")
        return False

    def stop(self) -> None:
        """关闭由本进程启动的后台（外部已在运行的后台不动）。"""
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=8)
        except Exception:  # noqa: BLE001
            try:
                self.process.kill()
            except Exception:  # noqa: BLE001
                pass
        self.process = None

    @property
    def owned(self) -> bool:
        return self.process is not None
