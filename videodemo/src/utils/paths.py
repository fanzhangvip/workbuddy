"""路径解析。兼容源码运行与 PyInstaller 打包运行两种形态。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "UniversalURLPlayer"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的可执行文件中。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """应用根目录。

    - 源码运行: 项目根目录（src/utils/paths.py 向上三级）
    - 打包运行: exe 所在目录
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def app_data_dir() -> Path:
    """配置/日志/缓存目录（Windows 下为 %APPDATA%/UniversalURLPlayer）。"""
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    return app_data_dir() / "config.ini"


def snapshot_dir() -> Path:
    """截图默认输出目录。"""
    path = app_root() / "snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_file(name: str) -> Path:
    """应用数据文件（播放列表、历史记录等）的路径。

    放在 %APPDATA% 而不是程序目录：打包后 exe 所在目录可能是只读的
    （装在 Program Files 下时），写那里会直接失败。
    """
    return app_data_dir() / name


def default_download_dir() -> Path:
    """默认下载目录：系统「下载」文件夹，取不到就用应用数据目录。

    不再用程序目录下的子文件夹——打包分发后那个位置通常不可写。
    """
    name = "Downloads"
    try:
        # Windows 上"下载"可能被重定向到 OneDrive，注册表里的才是真实位置
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        try:
            raw, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
        finally:
            winreg.CloseKey(key)
        candidate = Path(os.path.expandvars(str(raw)))
        if candidate.is_dir():
            return candidate
    except (ImportError, OSError):
        pass

    candidate = Path.home() / name
    if candidate.is_dir():
        return candidate
    return app_data_dir() / "downloads"


def bundled_file(*parts: str) -> Path:
    """取打包进 exe 的只读资源路径（--add-data 目标目录 resources/）。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_root())).joinpath("resources", *parts)
    return app_root().joinpath("resources", *parts)
