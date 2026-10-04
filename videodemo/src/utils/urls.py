"""地址规范化工具。"""

from __future__ import annotations

import os
import re
from pathlib import Path

# 形如 C:\dir\a.mp4 或 D:/dir/a.mp4
_WINDOWS_DRIVE = re.compile(r"^[a-zA-Z]:[\\/]")
# UNC 路径 \\server\share\a.mp4
_UNC = re.compile(r"^\\\\")


def is_local_path(text: str) -> bool:
    """判断字符串是否是一个本地文件路径（而不是 URL）。"""
    if not text:
        return False
    if "://" in text:
        return False
    if text.lower().startswith("file:"):
        return False
    return bool(_WINDOWS_DRIVE.match(text) or _UNC.match(text) or text.startswith("/"))


def is_loopback_url(text: str) -> bool:
    """地址是否指向本机。

    这个判断很实用：环境变量里配了 `HTTP_PROXY` 时（很多公司网络、以及本项目的
    开发沙箱都是这样），requests 会**把发往 127.0.0.1 的请求也丢给代理**，
    结果是清晰可辨的 `ProxyError: Unable to connect to proxy`。
    而本机地址（含我们自己起的反向代理）绝不该经过外部代理，必须显式绕开。
    """
    from urllib.parse import urlparse

    raw = (text or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    except ValueError:
        return False

    if parsed.scheme not in ("http", "https", "ws", "wss"):
        return False

    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    # RFC 6761 把 *.localhost 也划给本机
    if host.endswith(".localhost"):
        return True
    # 整个 127.0.0.0/8 都是环回地址
    return host.startswith("127.")


def to_playable_url(text: str) -> str:
    """把用户输入规范成播放器可接受的地址。

    QMediaPlayer 需要合法的 URL：直接传 `C:\\dir\\a.mp4` 会因为不是合法 URL
    而报 "Could not open file"，必须转成 `file:///C:/dir/a.mp4`。
    """
    text = (text or "").strip()
    if not text:
        return text

    # 已经是 URL（含 file://）就原样返回
    if "://" in text or text.lower().startswith("file:"):
        return text

    if is_local_path(text):
        try:
            return Path(text).resolve().as_uri()
        except (ValueError, OSError):
            # 路径非法时退回手工拼接，至少保证能试一次
            return "file:///" + text.replace("\\", "/").lstrip("/")

    # 相对路径且文件确实存在
    if os.path.exists(text):
        try:
            return Path(text).resolve().as_uri()
        except (ValueError, OSError):
            pass

    return text
