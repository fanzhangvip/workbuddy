"""Windows 桌面客户端（PySide6）。

桌面壳只负责界面与交互，所有调度/抢票/存储由本地后台服务承担，
与 Web 网页共用同一套 REST + WebSocket 接口（设计文档 11.1 方案 A / UI-09）。
"""

from desktop_ui.api_client import ApiClient, ApiError, BackendLauncher

__all__ = ["ApiClient", "ApiError", "BackendLauncher", "__version__"]

__version__ = "1.0.0"
