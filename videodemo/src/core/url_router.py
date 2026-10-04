"""
URL 分类与降级链决策。

设计要点:
- 判断直链媒体时**只看 URL 的 path 部分**，天然忽略 `?token=xxx` 这类 query，
  避免 `.../a.m3u8?auth=xxx` 被误判为未知地址。
- 无法判定的 http(s) 地址归为 UNKNOWN_HTTP，策略是"先直接交给播放器试"，
  失败后由 player_controller 自动交给解析器再试一次
  （见 PlayerController._try_resolve_fallback）——很多站点自建的播放页正是
  这一类地址。
"""

from __future__ import annotations

import ipaddress
import os
import socket
from enum import Enum
from urllib.parse import urlparse


class UrlKind(Enum):
    """输入地址的类别。"""

    LOCAL = "local"                # 本地文件
    DIRECT_MEDIA = "direct_media"  # 可直接播放的媒体直链
    HLS = "hls"                    # HTTP Live Streaming 清单
    LIVE_STREAM = "live_stream"    # rtsp / rtmp / rtp / udp 等实时流
    WEB_PAGE = "web_page"          # 视频网站页面地址，需解析
    UNKNOWN_HTTP = "unknown_http"  # 无法判定的 http(s) 地址
    UNKNOWN = "unknown"


# 可直接播放的媒体扩展名（小写，含点）
MEDIA_EXTS = frozenset({
    ".mp4", ".m4v", ".mkv", ".webm", ".flv", ".f4v", ".mov", ".avi",
    ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".wmv", ".3gp", ".ogv",
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".opus", ".mpd",
})

HLS_EXTS = frozenset({".m3u8", ".m3u"})

# 实时流协议
LIVE_SCHEMES = frozenset({
    "rtsp", "rtsps", "rtmp", "rtmps", "rtmpt", "rtmpe", "rtmpte",
    "rtp", "udp", "srt",
})

# 主流视频网站：页面地址一律需要解析，直接尝试打开页面本身没有意义
WEB_HOSTS = (
    "youtube.com", "youtu.be", "bilibili.com", "b23.tv",
    "douyin.com", "ixigua.com", "kuaishou.com", "youku.com", "v.qq.com",
    "weibo.com", "vimeo.com", "dailymotion.com", "twitch.tv",
    "twitter.com", "x.com", "tiktok.com", "acfun.cn", "iqiyi.com",
    "mgtv.com", "nicovideo.jp", "bilibili.tv",
    "haokan.baidu.com", "v.baidu.com",
)

# 各类型对应的加载超时（毫秒）。超时后强制判定失败，避免界面永久卡在"加载中"。
LOAD_TIMEOUT_MS = {
    UrlKind.LOCAL: 10_000,
    UrlKind.DIRECT_MEDIA: 8_000,
    UrlKind.HLS: 12_000,
    UrlKind.LIVE_STREAM: 10_000,
    UrlKind.UNKNOWN_HTTP: 8_000,
    UrlKind.WEB_PAGE: 15_000,
    UrlKind.UNKNOWN: 5_000,
}


def _ext_of(path: str) -> str:
    """取扩展名，调用方需保证传入的是 URL 的 path 部分（不含 query）。"""
    return os.path.splitext(path.lower())[1]


#: 指向本机回环的主机名
#:
#: `0.0.0.0` 严格说是"未指定地址"，但在 Windows 上连它等价于连本机，而且
#: `src/utils/urls.is_loopback_url` 也把它当本机——两处判定必须一致，否则
#: 同一条地址会被一个判为"本机直连"、另一个判为"要走代理"。
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def is_loopback(url: str) -> bool:
    """地址是否指向本机回环。

    用途：本地反向代理不该去代理本机地址——本机服务没有防盗链问题，
    套一层代理只会白绕一圈（代理自己就监听在回环上）。

    ⚠️ **别用主机名做字符串比较**：整个 `127.0.0.0/8` 都是环回，不只是
    `127.0.0.1`，而 `127.1`、`127.0.0.2` 这些写法同样指向本机。早期版本
    只比对一张固定名单，于是这些地址会被拿去绕代理，多出一个可能失败的环节。
    """
    try:
        host = (urlparse((url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False

    if host in _LOOPBACK_HOSTS or host.endswith(".localhost"):
        return True

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass

    # `ip_address` 不认 `127.1` 这类省略写法，但套接字层是认的
    try:
        return socket.inet_aton(host)[0] == 127
    except OSError:
        return False


def host_matches(host: str, domains: tuple[str, ...] = WEB_HOSTS) -> bool:
    """host 是否命中域名表（含子域，如 www.bilibili.com 命中 bilibili.com）。"""
    host = (host or "").lower().split(":")[0]
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def classify(raw: str) -> UrlKind:
    """判断地址类别。"""
    raw = (raw or "").strip()
    if not raw:
        return UrlKind.UNKNOWN

    # 1) 本地路径 / file:// URI
    if raw.lower().startswith("file://"):
        return UrlKind.LOCAL
    try:
        if os.path.exists(raw):
            return UrlKind.LOCAL
    except (OSError, ValueError):
        # Windows 上非法路径字符可能抛异常，忽略即可
        pass

    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()

    # 2) 实时流协议
    if scheme in LIVE_SCHEMES:
        return UrlKind.LIVE_STREAM

    # 3) HTTP(S)：先看扩展名（只看 path，忽略 query）
    if scheme in ("http", "https"):
        ext = _ext_of(parsed.path)
        if ext in HLS_EXTS:
            return UrlKind.HLS
        if ext in MEDIA_EXTS:
            return UrlKind.DIRECT_MEDIA
        if host_matches(parsed.netloc):
            return UrlKind.WEB_PAGE
        return UrlKind.UNKNOWN_HTTP

    # 4) 无 scheme 但看起来像域名，例如 "www.bilibili.com/video/BVxxx"
    if "://" not in raw and "/" in raw and host_matches(raw.split("/", 1)[0]):
        return UrlKind.WEB_PAGE

    return UrlKind.UNKNOWN


def timeout_for(kind: UrlKind) -> int:
    return LOAD_TIMEOUT_MS.get(kind, 8_000)


def is_directly_playable(kind: UrlKind) -> bool:
    """该类型是否可以直接丢给播放器，不需要先解析。

    WEB_PAGE（网页地址）不是媒体，必须先经 src/core/resolver.py 解析；
    UNKNOWN 则是连类型都判断不出来，直接播放没有意义。
    """
    return kind not in (UrlKind.WEB_PAGE, UrlKind.UNKNOWN)


def needs_resolving(kind: UrlKind) -> bool:
    """该类型是否必须解析后才能播放。"""
    return kind is UrlKind.WEB_PAGE
