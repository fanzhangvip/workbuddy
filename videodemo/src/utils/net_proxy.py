"""代理探测：当前有没有代理在生效，以及它对某个主机是否适用。

## 为什么需要它

解析前的可达性预检（`src/core/reachability.py`）做的是**裸 TCP 直连**：
"这个站点连得上吗"。但只要有一条代理在生效，"直连通不通"就与"能不能播"
无关了，最典型的例子就是 Clash Verge 这类工具：

  · 它的"系统代理"开关只把 Windows 系统代理指向自己的混合端口
    （如 ``127.0.0.1:7897``），**完全不改变裸 TCP 连接**。于是被墙的站点
    直连必然超时，预检据此判定"不可达"，把本来经代理完全能播的地址误杀。
  · 而 yt-dlp / requests 走的是 `urllib.request.getproxies()`，在 Windows 上
    会读注册表里的系统代理，所以它们其实**是**走代理的——两边的结论正好相反。

结论：探测到有效代理就跳过预检，让解析器自己去试（它本来就认得代理）。

## 来源与优先级

  1. 设置里手填的代理（用户明确指定，最高优先级）
  2. 环境变量 ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY``（大小写都认）
  3. Windows 系统代理：注册表 ``Internet Settings`` 的
     ``ProxyEnable`` / ``ProxyServer``（Clash Verge 等的"系统代理"写的就是这里）

``no_proxy``（环境变量）与 Windows 的 ``ProxyOverride`` 会让指定主机**不走**
代理，这类主机要继续做预检——所以按目标主机逐个判断，而不是"有代理就一刀切"。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.request import proxy_bypass

#: 环境变量里的代理键，按"对 https 地址的适用性"从高到低
_ENV_KEYS = ("https_proxy", "http_proxy", "all_proxy")

#: Windows 系统代理所在的注册表位置
_IE_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

#: 来源说明：会写进日志，便于排查"为什么跳过了预检"
SOURCE_SETTING = "设置中指定的代理"
SOURCE_ENV = "环境变量里的代理"
SOURCE_SYSTEM = "系统代理"


@dataclass(frozen=True)
class ProxyHit:
    """探测结果：有一条代理对这个地址生效。"""

    proxy: str
    source: str


def env_proxy() -> str:
    """环境变量里配置的代理；没有则返回空串。"""
    for key in _ENV_KEYS:
        for name in (key, key.upper()):
            value = (os.environ.get(name) or "").strip()
            if value:
                return value
    return ""


def normalize(server: str) -> str:
    """把系统代理的写法补成带协议的地址。

    注册表里的 ``ProxyServer`` 有两种写法，都要认：
      · ``127.0.0.1:7897``（统一一个端口）
      · ``http=127.0.0.1:7897;https=127.0.0.1:7897``（按协议分开）
    """
    text = (server or "").strip()
    if not text:
        return ""

    if "=" in text:
        entries: dict[str, str] = {}
        for item in text.split(";"):
            name, sep, value = item.partition("=")
            if sep:
                entries[name.strip().lower()] = value.strip()
        text = entries.get("https") or entries.get("http") or ""

    if not text:
        return ""
    return text if "://" in text else f"http://{text}"


def system_proxy() -> str:
    """Windows 系统代理（读注册表）；未开启或非 Windows 返回空串。"""
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _IE_SETTINGS) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return ""
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return ""
    except Exception:  # noqa: BLE001 - 注册表读不出来就当"没有代理"
        return ""
    return normalize(str(server))


def host_of(target: str) -> str:
    """取出地址里的主机名（带不带协议都认）；取不到返回空串。"""
    raw = (target or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"//{raw}")
    except ValueError:
        return ""
    return (parsed.hostname or "").lower()


def _bypassed(host: str) -> bool:
    """``no_proxy`` / ``ProxyOverride`` 是否让这个主机绕开代理。

    判定失败按"不绕开"处理——宁可跳过预检，也不要拿一个可能失真的直连
    结论去误杀本来能播的地址。
    """
    if not host:
        return False
    try:
        return bool(proxy_bypass(host))
    except Exception:  # noqa: BLE001
        return False


def proxy_for(target: str, *, app_proxy: str = "") -> ProxyHit | None:
    """这条地址当前是否有一条代理在生效。

    Args:
        target: 目标地址（http/https）。
        app_proxy: 设置里手填的代理，最高优先级。

    Returns:
        ``ProxyHit``：有代理在生效，裸 TCP 预检的结论不可信，应当跳过。
        ``None``：没检测到代理，预检仍然有效。

    手填的代理不再看 ``no_proxy``：程序把它交给 yt-dlp / requests 时本来就
    只绕开本机地址（见 `downloader._proxies_for`），这里保持一致。
    """
    app = (app_proxy or "").strip()
    if app:
        return ProxyHit(proxy=app, source=SOURCE_SETTING)

    host = host_of(target)
    if not host:
        return None

    env = env_proxy()
    if env:
        return None if _bypassed(host) else ProxyHit(proxy=env, source=SOURCE_ENV)

    system = system_proxy()
    if system:
        return None if _bypassed(host) else ProxyHit(proxy=system, source=SOURCE_SYSTEM)

    return None
