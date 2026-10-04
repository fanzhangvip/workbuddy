"""统一日志：同时写入控制台与 %APPDATA%/UniversalURLPlayer/logs/。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from src.utils.paths import log_dir

_configured = False
_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"

#: 保留对上一个 Qt 消息处理器的引用，避免被垃圾回收后 Qt 拿到悬空指针
_previous_qt_handler = None


def setup_logging(level: int = logging.INFO) -> None:
    """初始化根 logger。重复调用安全。"""
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            log_dir() / "app.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        # 日志目录不可写时不应影响程序启动
        pass

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def install_qt_message_handler() -> None:
    """把 Qt 自身的日志转发进应用日志。

    这一步很重要：播放失败时真正的原因（FFmpeg 的 "Could not open file"、
    "Invalid data found"、RTSP 超时等）都是以 Qt 警告的形式输出的，
    默认只打到 stderr——窗口模式下用户根本看不到，排查时等于没有线索。

    用法与时机：在 setup_logging() 之后、创建 QApplication 之前调用。
    """
    global _previous_qt_handler
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:  # noqa: BLE001 - 没有 Qt 时不应影响日志系统
        return

    level_map = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    qt_log = logging.getLogger("qt")

    def handler(mode, context, message: str) -> None:  # noqa: ANN001 - Qt 回调签名
        level = level_map.get(mode, logging.INFO)
        # 交给根 logger 按级别过滤：默认 INFO，因此 Qt 的 debug/info 噪音不会入库
        qt_log.log(level, "%s", message)

    _previous_qt_handler = qInstallMessageHandler(handler)

