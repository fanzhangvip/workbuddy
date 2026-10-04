"""读取本机配置（桌面端自动登录用）。

桌面端与后台跑在同一台机器上，因此可以直接读取运行目录里的 ``env.py``
拿到后台密码，实现「双击即用、不用手输密码」（对接 UI-10/UI-11）。

这里刻意**不导入 ticket_helper**，避免桌面进程顺带初始化日志/数据库，
与正在运行的后台进程产生不必要的文件竞争。
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR_NAME = ".ticket-helper"
ENV_HOME = "TICKET_HELPER_HOME"


def resolve_home() -> Path:
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("USERPROFILE") or os.environ.get("HOME")
        root = Path(base) if base else Path.cwd()
    else:
        root = Path.home()
    return (root / APP_DIR_NAME).resolve()


def load_web_credentials(home: Path | None = None) -> tuple[str, str, str]:
    """返回 ``(用户名, 密码, 后台地址)``；读取失败时返回空串。"""
    home = home or resolve_home()
    env_file = home / "env.py"
    if not env_file.exists():
        return "", "", ""

    try:
        spec = importlib.util.spec_from_file_location("th_desktop_env", env_file)
        if spec is None or spec.loader is None:
            return "", "", ""
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        env = getattr(module, "ENV", None) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("读取本地配置失败: %s", exc)
        return "", "", ""

    web = env.get("web") or {}
    host = str(web.get("host") or "127.0.0.1")
    port = int(web.get("port") or 8008)
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    base_url = f"http://{host}:{port}"
    return str(web.get("auth_user") or "admin"), str(web.get("auth_pass") or ""), base_url
