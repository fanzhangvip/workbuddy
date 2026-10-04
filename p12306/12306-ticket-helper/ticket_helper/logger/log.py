"""日志与指标（LG-01~LG-05）。

- 结构化 JSON 落盘 + 彩色控制台输出（人类可读）；
- 每条日志携带 ``request_id``（跨模块追踪，适配 Web 请求链路）；
- 内置环形缓冲区 + 订阅机制，供 WebSocket 实时日志推送（WB-03）与
  桌面端「实时日志窗」（UI-05）使用，无需读文件；
- 定时轮转（大小 + 时间双维度），挂机数月不会撑爆磁盘。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import uuid
from collections import deque
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Callable

from ticket_helper.runtime.paths import AppPaths, get_app_paths

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_LEVEL_COLOR = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[41;37m",
}
_RESET = "\033[0m"


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


class JsonFormatter(logging.Formatter):
    """结构化 JSON 格式：便于采集与检索。"""

    def __init__(self, *, service: str = "ticket-helper") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "request_id": getattr(record, "request_id", None) or request_id_var.get(),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info).splitlines()[-1]
        for key in ("task_id", "user", "channel", "latency_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """控制台友好格式，含分色（UI-05 / Web 日志窗按级别着色）。"""

    def __init__(self, *, color: bool = True) -> None:
        super().__init__("%(asctime)s %(levelname)-7s [%(name)s] %(message)s", datefmt="%H:%M:%S")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.color:
            return text
        return f"{_LEVEL_COLOR.get(record.levelname, '')}{text}{_RESET}"


# --------------------------------------------------------------------------- #
# 实时日志总线
# --------------------------------------------------------------------------- #
class LogBusHandler(logging.Handler):
    """把日志推入内存环形缓冲，并分发给已注册的订阅者（WebSocket / 桌面端）。"""

    def __init__(self, capacity: int = 800) -> None:
        super().__init__()
        self.buffer: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.RLock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "request_id": getattr(record, "request_id", None) or request_id_var.get(),
                "message": record.getMessage(),
                "task_id": getattr(record, "task_id", None),
            }
        except Exception:  # noqa: BLE001 - 日志自身绝不能抛异常
            return
        with self._lock:
            self.buffer.append(entry)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(entry)
            except Exception:  # noqa: BLE001
                pass

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return _unsubscribe

    def recent(self, limit: int = 200, level: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self.buffer)
        if level:
            items = [i for i in items if i.get("level") == level.upper()]
        return items[-limit:]


log_bus = LogBusHandler()

_configured = False
_config_lock = threading.Lock()


def make_console_safe() -> None:
    """把标准输出/错误流调整到「任何环境下都不会直接崩」的状态。

    解决两类 Windows 上的真实故障：

    1. **中文 Windows 控制台是 GBK（cp936）**，而日志里会出现 ``🎫`` 这类
       GBK 无法编码的字符，默认的 strict 策略会抛 ``UnicodeEncodeError``，
       后果是「命令跑到一半直接失败」。
       这里改成 ``errors="replace"``：中文照常显示，个别符号降级为 ``?``。
       注意**不要**把 encoding 改成 utf-8 —— 那样中文在 GBK 控制台会变乱码，
       比 replace 更糟。

    2. **PyInstaller 以窗口模式（console=False）打包时，
       ``sys.stdout`` / ``sys.stderr`` 会是 ``None``**。
       此时任何 ``print()`` 都会抛
       ``AttributeError: 'NoneType' object has no attribute 'write'``，
       表现为「双击 exe 一闪就没」——找不到任何报错信息。
       这里把它们兜底到 ``os.devnull``，让程序照常运行。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8", errors="replace"))
            except OSError:  # pragma: no cover - 极端环境下无法打开 devnull
                pass
            continue
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass

    # argparse / 第三方库有时会写 sys.__stdout__，一并兜底
    if sys.__stdout__ is None:
        sys.__stdout__ = sys.stdout  # type: ignore[assignment]
    if sys.__stderr__ is None:
        sys.__stderr__ = sys.stderr  # type: ignore[assignment]


def setup_logging(
    *,
    level: str = "INFO",
    paths: AppPaths | None = None,
    to_file: bool = True,
    to_console: bool = True,
) -> LogBusHandler:
    """初始化日志系统（幂等）。"""
    global _configured
    with _config_lock:
        root = logging.getLogger()
        if _configured:
            root.setLevel(getattr(logging, level.upper(), logging.INFO))
            return log_bus

        paths = paths or get_app_paths()
        paths.ensure_dirs()
        make_console_safe()
        root.setLevel(getattr(logging, level.upper(), logging.INFO))

        if to_console:
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(ConsoleFormatter(color=sys.stdout.isatty()))
            root.addHandler(console)

        if to_file:
            file_handler = logging.handlers.TimedRotatingFileHandler(
                paths.log_file, when="midnight", backupCount=14, encoding="utf-8"
            )
            file_handler.setFormatter(JsonFormatter())
            root.addHandler(file_handler)

            error_handler = logging.handlers.RotatingFileHandler(
                paths.error_log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            error_handler.setLevel(logging.ERROR)
            error_handler.setFormatter(JsonFormatter())
            root.addHandler(error_handler)

        log_bus.setLevel(logging.DEBUG)
        root.addHandler(log_bus)

        # 降低第三方库噪声，避免刷屏掩盖业务日志
        for noisy in ("urllib3", "werkzeug", "requests"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        _configured = True
        logging.getLogger(__name__).info("日志系统就绪: level=%s dir=%s", level, paths.log_dir)
        return log_bus


def get_log_bus() -> LogBusHandler:
    return log_bus


class Metrics:
    """轻量运行指标采集（LG-04），线程安全。"""

    def __init__(self) -> None:
        self._counters: dict[str, float] = {}
        self._latencies: dict[str, deque[float]] = {}
        self._lock = threading.RLock()
        self._started = time.time()

    def incr(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + value

    def observe(self, name: str, value: float, capacity: int = 200) -> None:
        with self._lock:
            bucket = self._latencies.setdefault(name, deque(maxlen=capacity))
            bucket.append(float(value))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            latency = {
                name: {
                    "count": len(values),
                    "avg": round(sum(values) / len(values), 1) if values else 0.0,
                    "max": max(values) if values else 0.0,
                }
                for name, values in self._latencies.items()
            }
        return {
            "uptime_seconds": int(time.time() - self._started),
            "counters": counters,
            "latency": latency,
        }


metrics = Metrics()
