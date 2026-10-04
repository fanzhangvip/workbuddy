"""网页地址解析：把「视频网站页面地址」变成一条能直接播放的流地址（yt-dlp）。

## 为什么必须是"单文件流"

``QMediaPlayer`` 只接受**一条 URL**，它没有"取两条流再合流"的能力。
yt-dlp 的默认选择（``bestvideo+bestaudio``）给的是两条分离的流，
交给播放器只能播其中一条——要么没声音，要么没画面。
所以本模块的目标是：从站点提供的格式里挑出**音视频在同一个文件里**的
progressive 流。

## 站点只提供分离流时怎么办

B 站、YouTube 的 DASH 格式就是这种情况（B 站**只有** DASH，一条 progressive
都没有）。这时不能"报错了事"，而是：
  · 明确说明原因，别让用户以为是自己地址填错了；
  · 把解析到的画面流 / 声音流地址一并带回去，界面据此可以提供
    「仅播放画面（无声音）」这种降级选择。
本模块只负责"如实返回结果"，降级与否由上层决定。

## 两个踩过的坑（改动时务必注意）

1. **不要把 format 传给 yt-dlp。**
   本想用 ``best[acodec!=none][vcodec!=none]`` 一步到位，但 B 站根本没有
   这样的格式，yt-dlp 会直接抛 "Requested format is not available" 并
   **中断整个解析**——连 formats 列表都拿不到，我们连"为什么没有"都说不清。
   正确做法：让 yt-dlp 用默认格式（它可能选到需要合流的两条，无所谓，
   我们只用它的元信息），然后自己从 ``info["formats"]`` 里挑能单独播的那条。

2. **整体必须能硬超时。**
   yt-dlp 的 ``socket_timeout`` 只约束单个 socket；站点不断重定向/重试时
   仍可能长时间不返回。因此解析跑在子线程里、用 ``join(timeout)`` 兜住，
   超时就当失败返回。子线程是守护线程，放弃后不会阻塞进程退出。

## 尽可能拿到地址：三层保障

"解析失败"其实有很多种，笼统地报一句"失败"对用户没有帮助，也浪费了本来能
救回来的机会。所以这个模块按"能救就救"的顺序分三层：

1. **可达性预检**（`src/core/reachability.py`）
   先花两三秒确认这个站点真的连得上。站点被整段拦截时立刻给出精确原因
   （是解析不到？还是解析到了但连不上？还是 DNS 被污染？），而不是让
   yt-dlp 白跑满 25 秒再报一句"超时"。
   **只要检测到代理在生效就跳过预检**（设置里手填的 / 环境变量里的 /
   Windows 系统代理，见 `src/utils/net_proxy.py`）——走代理时"直连 origin
   通不通"与能否播放无关。系统代理这一条尤其不能漏：Clash 这类工具的开关
   不改变裸 TCP，却决定 yt-dlp 能不能连通，漏了就会误杀本来能播的地址。

2. **逐策略重试**（`strategies_for`）
   被站点拒绝时，换一套请求方式再试：YouTube 换备用播放器接口、
   换浏览器标识、补上来源页。**只对"换种方式有可能绕过去"的失败重试**
   （机器人校验、403、抓不到格式）；网络不通、视频已删除这类重试只是白等。

3. **JavaScript 运行时**（`detect_js_runtime`）
   2026 版 yt-dlp 起，**没有 JS 运行时 YouTube 解析会退化**——默认只启用
   deno，而大多数机器上根本没装 deno，于是只能用"无 JS"的播放器接口，
   拿到的格式明显更少。实测本机没有 deno、但有 node，切成 node 之后
   可用接口立刻变多。所以这里会自动找一个能用的运行时交给 yt-dlp。

## 其他

- vcodec/acodec 的取值有三种含义，不能混为一谈：
    ``"none"``  = 明确没有这条轨（B 站视频流就是 acodec="none"）
    ``None``/``""`` = 未知（generic 解析器抓到的普通 mp4 就是这样）
  把 ``None`` 当成"没有"会把普通 mp4 误判成"分离流"。
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, parse_qs

from src.core.url_router import UrlKind, host_matches
from src.utils import js_runtime, net_proxy
from src.utils.logger import get_logger

_log = get_logger(__name__)

LogFn = Callable[[str], None]

#: 解析默认超时（秒）。yt-dlp 要经历「取页面 → 取签名 → 取播放地址」好几步，
#: 慢站点 20 秒很正常，给足；真要卡死也有这个上限兜底。
DEFAULT_TIMEOUT_S = 25

#: 允许直接交给播放器的协议前缀。
#: download=False 时 yt-dlp 已把每条格式的真实地址（含签名）写在 url 里，
#: 这里只是把"必须经过 yt-dlp 下载器分片拉取"的排掉。
_PLAYABLE_PROTOCOLS = ("http", "m3u8", "m3u", "rtsp", "rtmp")

#: 协议里出现这些子串，说明该格式要靠下载器分片拼接，不能直接播
_NOT_PLAYABLE_HINTS = ("dash_segments", "frag", "rtmp_ffmpeg")

#: 同分辨率下的编码兼容性排序（分越高越优先）。
#: avc1(h264) 是 Qt 自带 FFmpeg 最稳的选择；av01 依赖 dav1d、hevc 依赖系统解码器，
#: 万一缺了就表现为"能解析但播不出画面"，所以排在后面。
_CODEC_RANK = (
    ("avc1", 3), ("h264", 3),
    ("hvc1", 2), ("hev1", 2), ("hevc", 2),
    ("vp09", 1), ("vp9", 1), ("vp8", 1),
    ("av01", 0),
)

#: extractor 名字 → 中文展示名（没有映射就原样显示）
_EXTRACTOR_NAMES = {
    "bilibili": "哔哩哔哩",
    "youtube": "YouTube",
    "generic": "通用解析",
    "vimeo": "Vimeo",
    "douyin": "抖音",
    "kuaishou": "快手",
    "ixigua": "西瓜视频",
    "youku": "优酷",
    "qq": "腾讯视频",
    "iqiyi": "爱奇艺",
    "weibo": "微博",
    "acfun": "AcFun",
    "twitter": "X（推特）",
    "twitch": "Twitch",
    "tiktok": "TikTok",
    "dailymotion": "Dailymotion",
    "haokan": "好看视频",
}

#: 好看视频的页面域名。`haokan.baidu.com/v?vid=...` 是 React SPA，
#: 首屏 HTML 只有骨架、没有视频数据；`v.baidu.com/*.htm` 是 yt-dlp 内置
#: BaiduVideo 提取器认的老格式。两者最终都要落到 `mbd.baidu.com` 落地页
#: 内嵌的 `window.jsonData` 上才能拿到直链（见 `_extract_haokan`）。
_HAOKAN_HOSTS = ("haokan.baidu.com", "v.baidu.com")

#: 好看视频落地页：把 vid 拼进去就能拿到内嵌 `window.jsonData` 的移动页。
#: 里面 `curVideoMeta.clarityUrl` 按清晰度（rank）列出各档直链，
#: 取 rank 最高（1080p 蓝光）那条即可；直链在 vd*.bdstatic.com，无需 Referer。
_HAOKAN_PAGE = "https://mbd.baidu.com/newspage/data/videoshare?nid=sv_{vid}"

#: yt-dlp 支持的 JavaScript 运行时名（**必须**与 `yt_dlp.globals.supported_js_runtimes`
#: 的键一致，否则 yt-dlp 会整条忽略）。探测与"内置优先"的策略在
#: `src/utils/js_runtime.py` 里，这里只保留名单用于校验用户手填的值。
_JS_RUNTIMES = js_runtime.PATH_RUNTIMES

#: 网络类故障的关键词。
#: `friendly_error()` 与 `is_retryable()` 共用这一份——两处各写一份必然慢慢对不上。
_NETWORK_HINTS = (
    "timed out", "timeout", "connection", "ssl", "certificate",
    "temporary failure", "name or service not known", "getaddrinfo",
    "errno 11001", "unreachable", "proxy",
)

#: "连接被中途断开"的特征。
#:
#: **必须先于 `_NETWORK_HINTS` 判断**：这类报错文本里带 "connection"，
#: 会被"网络类"先吃掉，于是本该重试的被判成"重试没用"。
#: 之所以值得重试，是因为连接被重置常常是**按 TLS 指纹**拦的——
#: 换浏览器指纹正好能过，这是实测有效的一招。
_RESET_HINTS = (
    "connection reset", "connection aborted", "connectionreseterror",
    "connection was reset", "reset by peer", "远程主机强迫关闭",
    "connection closed", "broken pipe",
)

#: 指纹伪装用的浏览器目标。yt-dlp 会挑第一个**当前可用**的，
#: 所以按"最像普通用户"的顺序排。
_IMPERSONATE_TARGETS = ("chrome", "safari", "firefox", "edge")

#: 换一套请求方式**有可能**绕过去的失败特征。
#:
#: 只收这类：站点按客户端类型拒绝（YouTube 的机器人校验、403）、
#: 按 UA / Referer 拒绝、或解析器抓到的播放器响应结构不认识。
#: 网络类与"东西本身没了"不在此列，见 `is_retryable()`。
_RETRYABLE_HINTS = (
    "sign in to confirm", "not a bot", "confirm your age",
    "http error 403", "forbidden",
    "requested format is not available", "no video formats found",
    "no formats found", "no playable",
    "unable to extract", "unable to parse", "failed to extract",
    "player response", "precondition check failed",
    "content is not available", "content isn't available",
)

#: 换策略也救不回来的失败特征：重试纯属白等。
_HOPELESS_HINTS = (
    "drm", "http error 404", "video unavailable", "has been removed",
    "no longer exists", "does not exist", "不存在",
    "unsupported url", "no suitable extractor",
    "not available in your country", "blocked in your",
    "private video", "only available for registered", "premium", "vip",
)

#: "这是本程序自己的 bug，不是站点问题"的报错特征。
#:
#: 典型表现是**只有异常类型名、没有消息体**——来源通常是拼给 yt-dlp 的选项不
#: 合法，在构造阶段就 assert/类型检查掉了。本项目真踩过：`impersonate` 传了
#: 字符串列表，`YoutubeDL` 构造时抛空 `AssertionError`（见 `_impersonate_option`）。
#:
#: 为什么单独拎出来：这类报错对用户零信息量，却因为"排在最后"而盖住了真正的
#: 原因；顺着它去查站点只会白费功夫。
_INTERNAL_BUG_TYPES = (
    "assertionerror", "typeerror", "attributeerror", "keyerror",
    "indexerror", "unboundlocalerror", "nameerror", "valueerror",
)

#: YouTube 系站点：只有它们才值得用"备用播放器接口"这一招
_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")

#: YouTube 的备用播放器接口。
#:
#: 刻意避开 yt-dlp 的默认集合（`_DEFAULT_CLIENTS` 等，已试过的再试没意义），
#: 只列"默认之外、且历史上确实能过机器人校验"的几个。
#: 名字写错也不会有副作用：yt-dlp 遇到不认识的 client 只会打一条警告并跳过。
_YOUTUBE_FALLBACK_CLIENTS = (
    "tv", "web_embedded", "web_safari", "mweb", "android_vr",
)

#: 换策略时用的浏览器标识（与默认 UA 明显不同，才谈得上"换了个身份"）
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: 预检单次 TCP 探测超时（秒）
_PREFLIGHT_TIMEOUT_S = 3.0

#: 留给后续策略的最小预算（秒）。第一条策略万一慢，也不能把重试饿死。
_RETRY_RESERVE_S = 6.0

#: yt-dlp 不存在时的安装指引（打包版自带，不需要装）
INSTALL_HINT = (
    "缺少解析组件 yt-dlp。安装命令：\n"
    "  ./.venv/Scripts/python.exe -m pip install yt-dlp "
    "-i https://mirrors.aliyun.com/pypi/simple\n"
    "（打包后的 exe 自带该组件，不需要单独安装。）"
)

_availability: bool | None = None


def _noop(_message: str) -> None:
    pass


# ------------------------------------------------------------------ 可用性
def is_available() -> bool:
    """yt-dlp 是否可用（结果缓存，重复调用无开销）。"""
    global _availability
    if _availability is None:
        try:
            import yt_dlp  # noqa: F401

            _availability = True
        except Exception as exc:  # noqa: BLE001 - 缺依赖是预期情形，不该抛给上层
            _log.info("yt-dlp 不可用: %s", exc)
            _availability = False
    return _availability


def version() -> str:
    """yt-dlp 版本号；不可用时返回空串。"""
    try:
        from yt_dlp import version as yt_version

        return str(yt_version.__version__)
    except Exception:  # noqa: BLE001
        return ""


def describe_availability() -> str:
    """一行摘要，供自检 / 界面展示。"""
    if is_available():
        return f"yt-dlp {version()}：可用（网页地址解析已启用）"
    return "yt-dlp：**不可用**（网页地址无法解析）"


# ------------------------------------------------------------------ streamlink 降级
_streamlink_availability: bool | None = None


def streamlink_available() -> bool:
    """streamlink 是否可用（结果缓存）。它是 yt-dlp 失败后的降级解析器。"""
    global _streamlink_availability
    if _streamlink_availability is None:
        try:
            import streamlink  # noqa: F401

            _streamlink_availability = True
        except Exception as exc:  # noqa: BLE001
            _log.info("streamlink 不可用: %s", exc)
            _streamlink_availability = False
    return _streamlink_availability


def describe_streamlink() -> str:
    """一行摘要，供自检展示。"""
    if streamlink_available():
        try:
            import streamlink

            return f"streamlink {streamlink.__version__}：可用（yt-dlp 失败后的降级）"
        except Exception:  # noqa: BLE001
            pass
    return "streamlink：**不可用**（yt-dlp 失败后无降级解析）"


def _streamlink_info(url: str, sink: LogFn) -> dict | None:
    """用 streamlink 提取一个地址的流，转成 yt-dlp 风格的 info dict。

    streamlink 覆盖约 130 个站点（Twitch / YouTube / Dailymotion 等），
    与 yt-dlp 有重叠也有互补。它不返回"格式列表 + 分离流"，而是直接给
    "这个质量下的可用流"。这里取 `best` 质量，拿到的是可直接播放的
    HTTP / HLS / DASH 流地址（HLS/DASH 用 manifest 地址，交给内核自己拉）。

    失败（无插件 / 提取失败 / 超时）返回 None，由调用方继续走下一步。
    """
    if not streamlink_available():
        return None
    import streamlink

    try:
        streams = streamlink.streams(url)
    except streamlink.StreamlinkError as exc:
        # NoPluginError 这类异常的 str 是空的，用类型名兜底
        reason = _first_line(str(exc)) or exc.__class__.__name__
        _log.info("streamlink 提取失败: %s", reason)
        sink(f"  streamlink 也没取到（{reason[:40]}）")
        return None
    except Exception as exc:  # noqa: BLE001 - 任何异常都当"降级失败"
        _log.warning("streamlink 异常: %s", exc)
        return None

    if not streams:
        return None

    # 优先 best，其次 worst（有些站点只有这几个键）
    chosen = streams.get("best") or streams.get("worst")
    if chosen is None:
        chosen = next(iter(streams.values()), None)
    if chosen is None:
        return None

    # 拿到实际可播放地址：单文件流用 .url；HLS/DASH 用 manifest 地址
    # （to_manifest_url 保留分片协议，交给内置内核按 HLS/DASH 处理）。
    play_url = ""
    headers = {}
    try:
        play_url = getattr(chosen, "url", "") or ""
    except Exception:  # noqa: BLE001
        play_url = ""
    if not play_url:
        try:
            play_url = chosen.to_manifest_url()
        except Exception:  # noqa: BLE001
            play_url = ""
    if not play_url:
        return None

    # streamlink 的请求头（防盗链 Referer 等）
    try:
        headers = dict(getattr(chosen, "headers", None) or {})
    except Exception:  # noqa: BLE001
        headers = {}

    # 判断是单文件还是 HLS/DASH
    ext = Path(urlparse(play_url).path).suffix.lower()
    protocol = "m3u8" if ext in (".m3u8", ".m3u") else "http"

    info: dict[str, Any] = {
        "url": play_url,
        "protocol": protocol,
        "ext": ext.lstrip(".") or "mp4",
        "vcodec": None,
        "acodec": None,
        "http_headers": headers,
        "extractor_key": "streamlink",
        "title": "",
        "is_live": True if streams.get("best") is None and streams.get("worst") is not None else False,
    }
    return info


# ------------------------------------------------------------------ 结果模型
@dataclass
class ResolveResult:
    """一次解析的结果。

    ``ok=True`` 表示拿到了可直接交给 QMediaPlayer 的**单文件流**。
    ``ok=False`` 且有 ``video_url`` / ``audio_url`` 时，说明站点只提供分离流，
    上层可以据此提供降级播放。
    """

    ok: bool = False
    page_url: str = ""
    url: str = ""                          # 可直接播放的流地址（含签名）
    kind: UrlKind = UrlKind.DIRECT_MEDIA   # 该流地址的类别（影响超时与直播判定）
    title: str = ""
    duration_ms: int = 0
    ext: str = ""
    width: int = 0
    height: int = 0
    codecs: str = ""                       # 形如 "avc1.64001F+mp4a.40.2"
    protocol: str = ""
    headers: dict = field(default_factory=dict)
    extractor: str = ""
    is_live: bool = False
    note: str = ""                         # 面向用户的一行说明
    detail: str = ""                       # 面向用户的多行说明（失败时用）
    error: str = ""                        # 失败原因（一句话）

    # 站点只提供分离流时的两条流（仅供降级播放，不是"解析成功"）
    video_url: str = ""
    video_note: str = ""
    video_kind: UrlKind = UrlKind.DIRECT_MEDIA
    audio_url: str = ""
    audio_note: str = ""

    @property
    def has_separate_streams(self) -> bool:
        return bool(self.video_url or self.audio_url)

    def report(self) -> str:
        """多行文本报告（自检 / 命令行用）。"""
        lines = [f"页面地址: {self.page_url or '<空>'}"]
        if self.title:
            lines.append(f"标题    : {self.title}")
        if self.ok:
            lines.append(f"结果    : 成功（音视频合一的单文件流）")
            lines.append(f"流地址  : {self.url}")
            lines.append(f"流类型  : {self.kind.value}"
                         + ("（直播）" if self.is_live else ""))
            if self.height:
                lines.append(f"清晰度  : {self.height}P（{self.width}x{self.height}）")
            if self.codecs:
                lines.append(f"编码    : {self.codecs}")
            if self.ext or self.protocol:
                lines.append(f"容器/协议: {self.ext or '?'} / {self.protocol or '?'}")
            if self.duration_ms:
                lines.append(f"时长    : {self.duration_ms / 1000:.1f} 秒")
            if self.extractor:
                lines.append(f"解析器  : {_extractor_label(self.extractor)}")
            if self.note:
                lines.append(f"说明    : {self.note}")
        else:
            lines.append(f"结果    : 失败 —— {self.error}")
            if self.has_separate_streams:
                lines.append(f"画面流  : {self.video_url or '<无>'}")
                lines.append(f"声音流  : {self.audio_url or '<无>'}")
            if self.detail:
                lines.append("")
                lines.append(self.detail)
        return "\n".join(lines)


# ------------------------------------------------------------------ 格式筛选
def codec_absent(value: Any) -> bool:
    """该轨是否**明确**不存在（yt-dlp 用字符串 "none" 表示）。"""
    return value is not None and str(value).strip().lower() == "none"


def codec_known(value: Any) -> bool:
    """该轨是否明确存在（既不是 "none"，也不是未知）。"""
    return value is not None and str(value).strip().lower() not in ("", "none")


def is_playable_format(fmt: dict) -> bool:
    """这条格式能否**不经下载器**直接交给播放器。"""
    if not isinstance(fmt, dict) or not fmt.get("url"):
        return False
    if fmt.get("has_drm"):
        return False
    if fmt.get("fragments"):
        # 有分片列表说明要由下载器拼，直接播会失败
        return False
    protocol = str(fmt.get("protocol") or "").lower()
    if any(hint in protocol for hint in _NOT_PLAYABLE_HINTS):
        return False
    return protocol.startswith(_PLAYABLE_PROTOCOLS)


def _codec_rank(codec: Any) -> int:
    text = str(codec or "").lower()
    for needle, rank in _CODEC_RANK:
        if text.startswith(needle):
            return rank
    # 未知编码给中性分数，不因为"不认识"而被排到最后
    return 2


def _number(fmt: dict, *keys: str) -> float:
    for key in keys:
        try:
            value = float(fmt.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def quality_key(fmt: dict) -> tuple:
    """质量排序键（越大越好）。

    分辨率优先，其次选兼容性好的编码，再挑单文件地址而不是清单地址，
    最后比码率与体积。
    特意把编码排在码率**之前**：同是 1080P 时 av01 往往码率更低、画质相当，
    但一旦设备的 FFmpeg 缺 AV1 解码器，就会变成"能解析、没画面"。
    清单地址排在编码之后，是为了不破坏这条兼容性底线（见 `is_manifest_format`）。
    """
    return (
        int(fmt.get("height") or 0),
        int(fmt.get("width") or 0),
        _codec_rank(fmt.get("vcodec")),
        0 if is_manifest_format(fmt) else 1,
        _number(fmt, "tbr", "vbr", "abr", "bandwidth"),
        _number(fmt, "filesize", "filesize_approx"),
    )


def is_manifest_format(fmt: dict) -> bool:
    """这条格式是不是"清单"型地址（HLS 的 .m3u8 / DASH 的 .mpd）。

    清单本身不是媒体文件：播放器得先把它拉下来，再照着里面的**分片地址**
    去取数据。而分片地址是播放器自己直连的——本地反向代理只能改写我们递给
    播放器的那一条 URL，管不到清单里的分片。所以在"必须走代理"的网络里，
    清单型地址会"清单能拉到、分片拉不到"，最终还是播不出来。

    同一个分辨率、同一种编码下，普通单文件/分离流优先——这条路我们能
    完整地替播放器把代理补上。站点只提供清单时照旧选它（比没好）。
    """
    protocol = str(fmt.get("protocol") or "").lower()
    if "m3u8" in protocol:
        return True

    ext = str(fmt.get("ext") or "").lower().lstrip(".")
    if ext in ("m3u8", "m3u", "mpd"):
        return True

    path = ""
    try:
        path = urlparse(str(fmt.get("url") or "")).path.lower()
    except ValueError:
        return False
    return path.endswith((".m3u8", ".m3u", ".mpd"))



def _best(formats: list[dict]) -> dict | None:
    if not formats:
        return None
    return max(formats, key=quality_key)


def select_single_file(formats: list[dict]) -> dict | None:
    """挑出音视频在同一条流里的格式（progressive）。

    vcodec/acodec 都是 ``None``（未知）时也算——普通 HTML5 `<video src=...>`
    抓出来就是这样，它显然是单文件。
    """
    return _best([
        fmt for fmt in formats
        if is_playable_format(fmt)
        and not codec_absent(fmt.get("vcodec"))
        and not codec_absent(fmt.get("acodec"))
    ])


def select_video_only(formats: list[dict]) -> dict | None:
    """挑出只有画面（音轨明确为 none）的格式。"""
    return _best([
        fmt for fmt in formats
        if is_playable_format(fmt) and codec_absent(fmt.get("acodec"))
    ])


def select_audio_only(formats: list[dict]) -> dict | None:
    """挑出只有声音（视频轨明确为 none）的格式。"""
    return _best([
        fmt for fmt in formats
        if is_playable_format(fmt) and codec_absent(fmt.get("vcodec"))
    ])


def stream_kind(protocol: Any, ext: Any = "") -> UrlKind:
    """由协议/容器判断这条流属于哪一类（决定加载超时与直播判定）。"""
    text = str(protocol or "").lower()
    suffix = str(ext or "").lower()
    if "m3u8" in text or "hls" in text or suffix in ("m3u8", "m3u"):
        return UrlKind.HLS
    if text.split("+")[0] in ("rtsp", "rtsps", "rtmp", "rtmps", "rtp", "udp", "srt"):
        return UrlKind.LIVE_STREAM
    return UrlKind.DIRECT_MEDIA


# ------------------------------------------------------------------ 文案
def _extractor_label(name: str) -> str:
    if not name:
        return ""
    return _EXTRACTOR_NAMES.get(name.lower(), name)


def describe_format(
    fmt: dict, *, extractor: str = "", live: bool = False, suffix: str = ""
) -> str:
    """把一条格式描述成给用户看的一行字，如「哔哩哔哩 · 1080P · mp4」。"""
    bits: list[str] = []
    label = _extractor_label(extractor)
    if label:
        bits.append(label)
    if live:
        bits.append("直播")
    height = int(fmt.get("height") or 0)
    note = str(fmt.get("format_note") or "").strip()
    if height:
        bits.append(f"{height}P")
    elif note:
        bits.append(note)
    ext = str(fmt.get("ext") or "").strip()
    if ext:
        bits.append(ext)
    if suffix:
        bits.append(suffix)
    return " · ".join(bits) or "已解析"


_ERROR_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("unsupported url", "no suitable extractor"),
     "yt-dlp 不认识这个网站，没有可用的解析器。"),
    (("drm",),
     "检测到 DRM 版权加密视频：Cookie 无法解密这类内容，本播放器无法播放。"),
    # 「需要新鲜 cookie」必须先于「需要登录」：抖音 / 西瓜视频要的是**匿名 cookie**
    # （不一定是登录态），提示用户开「从浏览器读 cookie」就能解决，说成"会员/私密"
    # 会把方向带偏。
    (("fresh cookies", "cookies (not necessarily logged in)", "cookies are needed",
      "cookie is needed", "cookie required", "cookies required"),
     "该站点需要浏览器 Cookie（匿名即可，不一定登录）。请在设置里开启「从浏览器读 Cookie」后重试。"),
    # 浏览器正在运行时 cookie 数据库会被锁，yt-dlp 复制失败报这个错。用户最常见的
    # 情况就是"浏览器开着"，必须给出可操作提示，而不是让晦涩英文穿透。
    (("could not copy", "cookie database", "decrypt with", "no keyring"),
     "读取浏览器 Cookie 失败：浏览器可能正在运行（数据库被锁）。可关闭浏览器后重试，或改用「手动指定 cookies 文件」。"),
    (("requested format is not available",),
     "该站点没有可用格式（多半需要登录后才能取到）。"),
    # 连接被重置必须排在网络类**之前**：它的文本里带 "connection"，
    # 落在网络类里会被错报成笼统的"网络连接失败"。
    (_RESET_HINTS,
     "连接被中途断开：可能是网络拦截，也可能是站点按浏览器指纹拒绝了本次请求。"),
    # 网络类必须排在 "unable to download webpage" 之前：后者是很多网络故障的
    # 外层包装，先匹配它会把"连不上"错报成"页面打不开"。
    (_NETWORK_HINTS, "网络连接失败或超时：请检查网络与代理设置。"),
    (("http error 404", "not found"),
     "打不开这个页面（404）：地址可能有误或内容已下架。"),
    (("http error 403", "forbidden"),
     "服务器拒绝访问（403）：可能是防盗链。可尝试在设置里开启「从浏览器读 Cookie」，或更换 User-Agent。"),
    # YouTube 的机器人校验**必须排在下面那条通用「需要登录」之前**：它的原文里
    # 带 "cookies" 和 "sign in"，落到通用规则就会得出"该视频需要登录身份"的结论，
    # 把用户引到"这个视频是会员/私密"的错方向——实际视频完全公开，缺的是反爬校验。
    # 这条**换客户端参数没用**：实测 13 个播放器接口 + 三种 JS 运行时（含内置
    # quickjs）全部被拒，用浏览器 UA 直接请求观看页也一样（LOGIN_REQUIRED）。
    # 判的是**出口 IP 的声誉**，所以有效手段是换一条出口。
    # 注意别把话说成"机房 IP 一律被拒"——实测同一份订阅里，同为机房 IP 的两个节点
    # 一个被拒、一个放行（FDCservers 拒 / Psychz 过），所以只能说"换节点试"。
    (("not a bot", "confirm you're not a bot", "confirm you’re not a bot",
      "sign in to confirm"),
     "YouTube 判定这个出口地址像机器人（视频本身是公开的，与视频无关）。"
     "换播放接口、换 JS 运行时都没用——**换一个代理节点通常就过了**："
     "同一份订阅里不同节点的出口声誉不一样，实测确实有的节点直接被放行。"
     "如果换节点无效，再考虑在设置里配好 cookies 文件带上登录态。"),
    (("private video", "login required", "sign in", "authentication", "cookies",
      "only available for registered", "premium", "vip"),
     "该视频需要登录身份才能观看。先在浏览器里登录对应账号，再把登录态交给程序："
     "Windows 上新版 Chrome/Edge 的 cookie 换了加密方式，yt-dlp 解不出来，"
     "建议改用 Firefox，或用浏览器插件导出 cookies.txt 后填到设置里的「cookies 文件」。"),
    # 「直播离线」在「地区/删除」之前单独判断：主播没开播是常见情况，别误判成
    # "视频已删除"或"地区不可用"。
    (("streamer is not live", "not live", "is offline", "video is offline",
      "offline"),
     "当前没有正在直播（主播未开播或已下播）。"),
    # 「已删除/下架」必须先于「地区」：很多站点把"删除"和"地区限制"写在同一句
    # 里（如 B 站 "This video may be deleted or geo-restricted"），geo 规则
    # 若排前面会误判成"地区不可用"。
    (("video unavailable", "has been removed", "no longer exists", "deleted",
      "does not exist", "稿件不可见", "不存在", "removed"),
     "视频不可用：可能已删除、已下架，或地址有误。"),
    (("geo", "not available in your country", "blocked in your", "geo-restricted"),
     "该视频在当前网络所在地区不可用。"),
    # 「无可用格式」单独给提示：小红书这类"打开页面但没抓到视频格式"的情况，
    # 多半是要登录，也可能是页面里根本不是视频。
    (("no video formats", "no formats found", "no playable"),
     "页面里没有可用的视频格式（可能需要登录，或该页面不是视频）。"),
    (("unable to download webpage",),
     "打不开这个页面：地址可能有误，或当前网络访问不通。"),
    (("unable to extract", "unable to parse", "failed to obtain", "extractor failed",
      "failed to extract"),
     "页面结构变了，解析器暂时取不到视频（yt-dlp 通常很快会跟进修复）。"),
    (("unsupported", "not supported"),
     "该地址不受支持。"),
)


def friendly_error(raw: str) -> str:
    """把 yt-dlp 的英文报错换成一句中文结论。"""
    text = (raw or "").lower()
    for needles, message in _ERROR_RULES:
        if any(needle in text for needle in needles):
            return message
    return "解析失败，具体原因见下方原始信息。"


def _first_line(raw: str) -> str:
    """yt-dlp 的报错常带多行与 "ERROR:" 前缀，这里收拾成一行原始信息。"""
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for prefix in ("ERROR: ", "ERROR:"):
            if line.startswith(prefix):
                line = line[len(prefix):]
                break
        return line
    return (raw or "").strip()


def _is_internal_bug(raw: str) -> bool:
    """这条报错像是**本程序自己**的问题（而不是站点/网络）吗。

    判据是"只有异常类型名、没有消息体"——`AssertionError`、`TypeError` 这种。
    带消息的异常（例如 `AssertionError: xxx`）不算：那多半是 yt-dlp 主动抛的。
    """
    text = (raw or "").strip()
    if not text or ":" in text:
        return False
    return text.lower() in _INTERNAL_BUG_TYPES


def _error_rank(raw: str) -> int:
    """给一条报错打分：**分越高越该展示给用户**。

    多策略全失败时,"最后一条"不等于"最有用的一条"。典型反例：排在末尾的
    「浏览器指纹伪装」抛了个空 `AssertionError`，而前一条早就说清了"需要登录"。
    用户拿到的却是前者，顺着它排查只会跑偏。

    所以按"能不能转化成一句可操作的中文"来排：
      3 = 明确的、不重试也能给出结论的原因（地区限制 / 已删除 / DRM）
      2 = 站点拒绝或网络类，配得上一条具体提示
      1 = 其他（至少还是条真报错）
      0 = 本程序自己的 bug，最没资格当结论
     -1 = 空
    """
    text = (raw or "").strip()
    if not text:
        return -1
    if _is_internal_bug(text):
        return 0
    lowered = text.lower()
    if any(needle in lowered for needle in _HOPELESS_HINTS):
        return 3
    if any(needle in lowered for needle in _RETRYABLE_HINTS):
        return 2
    if any(needle in lowered for needle in _RESET_HINTS):
        return 2
    if any(needle in lowered for needle in _NETWORK_HINTS):
        return 2
    return 1


def is_retryable(raw: str) -> bool:
    """这次失败值不值得换一套策略再试一次。

    三条判断，顺序不能换：
      1. 我们自己的选项构造炸了 —— 这跟站点无关，换下一条策略仍有意义；
      2. 网络不通 —— 换 UA 也好不了，重试只是白等；
      3. 剩下的才看是不是"被拒"类（机器人校验、403、抓不到格式）。
    """
    text = (raw or "").lower()
    if not text:
        return False
    # 顺序不能换：重置文本里带 "connection"，网络类会先把它吃掉。
    if any(needle in text for needle in _RESET_HINTS):
        return True
    if _is_internal_bug(raw):
        return True
    if any(needle in text for needle in _NETWORK_HINTS):
        return False
    if any(needle in text for needle in _HOPELESS_HINTS):
        return False
    return any(needle in text for needle in _RETRYABLE_HINTS)


# ------------------------------------------------------------------ 重试策略
@dataclass
class Strategy:
    """一次解析尝试的配置。"""

    label: str
    options: dict = field(default_factory=dict)


def _origin_of(url: str) -> str:
    """取站点根（``scheme://host``）；取不到返回空串。"""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return ""
    if not parsed.scheme or not parsed.hostname:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def strategies_for(url: str, *, enabled: bool = True) -> list[Strategy]:
    """按站点给出渐进的解析尝试序列。

    第一条永远是默认配置——绝大多数地址第一次就过了，后面几条只在被拒时轮到。
    关掉多策略时只返回第一条（等于回到老行为）。
    """
    specs = [Strategy("默认配置")]
    if not enabled:
        return specs

    try:
        host = (urlparse((url or "").strip()).hostname or "").lower()
    except ValueError:
        host = ""

    # ① 站点按"播放器接口类型"拒绝：YouTube 的机器人校验就是这一类。
    #    换 client 是官方解析器自己提供的能力（`--extractor-args`），比换 UA 有效得多。
    if host_matches(host, _YOUTUBE_HOSTS):
        specs.append(Strategy(
            "备用播放器接口",
            {"extractor_args": {"youtube": {
                "player_client": list(_YOUTUBE_FALLBACK_CLIENTS),
            }}},
        ))

    # ② 站点按 **TLS 指纹**拒绝（Cloudflare 一类）：换 UA 没用，必须换指纹。
    #    排在"换 UA"前面，因为按指纹拦的站点比按 UA 拦的更多。
    if impersonation_available():
        option = _impersonate_option()
        if option:
            specs.append(Strategy("浏览器指纹伪装", option))

    # ③ 站点按浏览器标识区别对待
    specs.append(Strategy("浏览器标识", {"http_headers": {"User-Agent": _BROWSER_UA}}))

    # ④ 站点按来源页防盗链（缺 Referer 直接 403）
    origin = _origin_of(url)
    if origin:
        specs.append(Strategy("补发来源页", {"http_headers": {"Referer": origin + "/"}}))

    return specs


#: 指纹伪装可用性（缓存；依赖不会中途变化，所以只在首次调用时探测）
_impersonate_cache: bool | None = None


def impersonation_available() -> bool:
    """浏览器 TLS 指纹伪装能不能用。

    需要 `curl_cffi`：yt-dlp 靠它发出与真实浏览器一致的 TLS 指纹，
    从而过掉一大类"按指纹拒绝"的站点（Cloudflare 一类）。
    装了它**不会**改变普通请求的走法——yt-dlp 给 curl_cffi 的默认优先级是最低的，
    只有站点明确要求伪装时才会用它，所以对现有站点没有影响。
    """
    global _impersonate_cache
    if _impersonate_cache is None:
        try:
            import curl_cffi  # noqa: F401

            _impersonate_cache = True
        except Exception as exc:  # noqa: BLE001 - 缺依赖是预期情形
            _log.info("未安装 curl_cffi，浏览器指纹伪装不可用: %s", exc)
            _impersonate_cache = False
    return _impersonate_cache


def reset_impersonation_cache() -> None:
    """清掉指纹伪装的可用性缓存。"""
    global _impersonate_cache, _impersonate_option_cache
    _impersonate_cache = None
    _impersonate_option_cache = None


#: 指纹伪装策略的选项（缓存；`reset_impersonation_cache()` 会清掉）
_impersonate_option_cache: dict | None = None


def _impersonate_target_supported(target) -> bool:
    """这个伪装目标当前可用吗。

    直接问 curl_cffi 那个请求处理器——它才是 `YoutubeDL` 构造时会去校验的
    一方（`ImpersonateRequestHandler.is_supported_target`）。绕开 `YoutubeDL`
    是为了不在**构建策略列表**时就构造一个完整的下载器。

    问不到就返回 True：让 yt-dlp 自己去报那条清楚的 "not available" 错误，
    比在这里默默把策略砍掉更容易排查。
    """
    try:
        from yt_dlp.networking._curlcffi import CurlCFFIRH

        return any(target in supported for supported in CurlCFFIRH.supported_targets)
    except Exception:  # noqa: BLE001 - 拿不到名单时不做筛选
        return True


def _impersonate_option() -> dict:
    """指纹伪装策略用的选项；一个可用的目标都没有时返回空字典（跳过该策略）。

    ## 必须传 `ImpersonateTarget` **对象**，而且只能传**一个**

    yt-dlp 走命令行时会先把 `--impersonate chrome` 转成 `ImpersonateTarget`
    （见 `yt_dlp/__init__.py`），走 **Python API 则没有这层转换**。而
    `YoutubeDL.__init__` 会拿 `params['impersonate']` 直接去校验：

        if impersonate_target := self.params.get('impersonate'):
            if not self._impersonate_target_available(impersonate_target):

    而被调用的 `is_supported_target()` 第一行就是：

        assert isinstance(target, ImpersonateTarget)

    于是：**传字符串**（"chrome"）或**传对象列表**（[ImpersonateTarget(...)]）
    都会在 `YoutubeDL` 构造阶段抛出一个**没有消息体**的 `AssertionError`。
    它整条策略一次都没成功过，还因为排在最后、把真正的原因（例如"需要登录"）
    从最终报错里挤掉了——用户看到的就是光秃秃一句 `AssertionError`。
    CLI 那种"传多个目标、让 yt-dlp 挑一个"的玩法在 API 下不成立。
    """
    global _impersonate_option_cache
    if _impersonate_option_cache is not None:
        return _impersonate_option_cache

    option: dict = {}
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget

        for name in _IMPERSONATE_TARGETS:
            target = ImpersonateTarget.from_str(name)
            if _impersonate_target_supported(target):
                option = {"impersonate": target}
                break
    except Exception as exc:  # noqa: BLE001 - 伪装是"锦上添花"，坏了不该影响解析
        _log.debug("构造浏览器指纹伪装选项失败（该策略跳过）: %s", exc)

    if not option:
        _log.info("没有可用的浏览器指纹伪装目标，跳过该策略")
    _impersonate_option_cache = option
    return option


def describe_impersonation() -> str:
    """一行摘要，供自检 / 设置面板展示。"""
    if impersonation_available():
        return "浏览器指纹伪装：可用（curl_cffi，可过按 TLS 指纹拦截的站点）"
    return (
        "浏览器指纹伪装：**不可用**（缺少 curl_cffi，部分站点会拒绝解析）。"
        "安装：pip install curl_cffi -i https://mirrors.aliyun.com/pypi/simple"
    )


#: 探测到的 JS 运行时（缓存；改设置后由 `reset_js_runtime_cache()` 清掉）
_js_runtime_cache: dict | None = None


def detect_js_runtime() -> dict:
    """找一个能用的 JavaScript 运行时，返回给 yt-dlp 的 `js_runtimes` 选项。

    顺序：设置里显式指定 > （内置的 + PATH 里探测到的）。

    找不到就返回空字典（沿用 yt-dlp 自己的默认）。**绝不因此报错**：
    没有它照样能解析绝大多数站点，只是 YouTube 这类会退化甚至失败。

    关于"内置优先"的原因，见 `src/utils/js_runtime.py`——一句话：从 PATH 找
    node 在开发机上总是成立、在用户机器上经常不成立（打包版实测踩过）。
    """
    global _js_runtime_cache
    if _js_runtime_cache is not None:
        return _js_runtime_cache

    explicit = ""
    try:
        from src.utils import settings as S

        explicit = S.get_str(S.K_JS_RUNTIME, "").strip()
    except Exception as exc:  # noqa: BLE001 - 读配置失败不该拖垮解析
        _log.debug("读取 JS 运行时配置失败（忽略）: %s", exc)

    found: dict = {}
    if explicit.lower() in ("none", "off", "0", "no"):
        _log.info("已按配置禁用 JavaScript 运行时")
    elif explicit:
        name, _, path = explicit.partition(":")
        name, path = name.strip().lower(), path.strip()
        if name not in _JS_RUNTIMES:
            _log.warning(
                "不认识的 JS 运行时 %r（可选：%s），已忽略",
                explicit, " / ".join(_JS_RUNTIMES),
            )
        elif path and not Path(path).is_file():
            _log.warning("配置的 JS 运行时路径不存在，已忽略: %s", path)
        else:
            found = {name: ({"path": path} if path else {})}
    else:
        found = js_runtime.auto_select()

    if found:
        _log.info(
            "网页解析启用 JavaScript 运行时: %s",
            ", ".join(f"{name}={cfg.get('path', 'PATH')}" for name, cfg in found.items()),
        )
    else:
        _log.warning(js_runtime.MISSING_HINT)
    _js_runtime_cache = found
    return found


def reset_js_runtime_cache() -> None:
    """清掉 JS 运行时探测缓存（改设置后调用）。"""
    global _js_runtime_cache
    _js_runtime_cache = None


# ------------------------------------------------------------------ 解析主体
def _candidate_formats(info: dict) -> list[dict]:
    """把 info 里所有"可能是播放地址"的条目汇总成候选列表。"""
    formats = [fmt for fmt in (info.get("formats") or []) if isinstance(fmt, dict)]

    # yt-dlp 已经选定一条时（requested_formats 为空），它可能会把结果写在
    # info 顶层而不是 formats 里，所以把顶层也当成一个候选。
    if info.get("url") and not info.get("requested_formats"):
        keys = (
            "url", "format_id", "format_note", "ext", "vcodec", "acodec",
            "width", "height", "tbr", "vbr", "abr", "filesize",
            "filesize_approx", "protocol", "http_headers", "has_drm",
        )
        formats.insert(0, {key: info.get(key) for key in keys})
    return formats


def _build_result(page_url: str, info: dict) -> ResolveResult:
    """把 yt-dlp 的 info 变成 ResolveResult。"""
    result = ResolveResult(page_url=page_url)

    playlist_note = ""
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
        if not entries:
            result.error = "这是一个播放列表 / 合集地址，但里面没有可解析的视频。"
            result.detail = "可以换成其中单个视频的页面地址再试。"
            return result
        info = entries[0]
        playlist_note = "该地址是合集/播放列表，已取第 1 个视频。"

    result.title = str(info.get("title") or "")
    result.extractor = str(info.get("extractor_key") or info.get("extractor") or "")
    live_status = str(info.get("live_status") or "")
    result.is_live = live_status in ("is_live", "is_upcoming", "post_live") or bool(
        info.get("is_live")
    )
    duration = info.get("duration")
    try:
        if duration:
            result.duration_ms = int(float(duration) * 1000)
    except (TypeError, ValueError):
        result.duration_ms = 0

    formats = _candidate_formats(info)
    single = select_single_file(formats)

    if single is not None:
        result.ok = True
        result.url = str(single.get("url") or "")
        result.kind = stream_kind(single.get("protocol"), single.get("ext"))
        result.ext = str(single.get("ext") or "")
        result.protocol = str(single.get("protocol") or "")
        result.width = int(single.get("width") or 0)
        result.height = int(single.get("height") or 0)
        result.codecs = "+".join(
            str(codec) for codec in (single.get("vcodec"), single.get("acodec"))
            if codec_known(codec)
        )
        result.headers = dict(single.get("http_headers") or info.get("http_headers") or {})
        notes = [describe_format(
            single, extractor=result.extractor, live=result.is_live, suffix="单文件"
        )]
        if playlist_note:
            notes.append(playlist_note)
        result.note = " · ".join(notes)
        return result

    # ---- 没有单文件流：把"为什么"说清楚 ----
    video = select_video_only(formats)
    audio = select_audio_only(formats)

    if video is not None or audio is not None:
        if video is not None:
            result.video_url = str(video.get("url") or "")
            result.video_kind = stream_kind(video.get("protocol"), video.get("ext"))
            result.video_note = describe_format(video, extractor=result.extractor,
                                                live=result.is_live, suffix="仅画面")
        if audio is not None:
            result.audio_url = str(audio.get("url") or "")
            result.audio_note = describe_format(audio, extractor=result.extractor,
                                                live=result.is_live, suffix="仅声音")
        # 分离流也必须带出 http_headers：B 站的 upos CDN 严格校验 Referer，
        # 缺了（或错成 CDN 域名自己）直接 403。这里把画面/声音流各自的
        # Referer/User-Agent 合并进 result.headers，供本地反向代理转发时补上。
        result.headers = dict(
            (video or {}).get("http_headers")
            or (audio or {}).get("http_headers")
            or info.get("http_headers")
            or {}
        )
        result.error = "该站点只提供「分离的音视频流」，内置内核无法同时播放"
        result.detail = _separate_stream_detail(result, playlist_note)
        return result

    # ---- 有格式、但全被过滤掉时，先判断是不是 DRM 加密 ----
    # is_playable_format 会把 has_drm 的流全部拦下，落到这里；此时若
    # 确实存在 DRM 流，就该明确告诉用户「加密、无法解密」，而不是笼统的
    # 「没有可播放的流」（后者会让人误以为再加个 cookie 就能解决）。
    if any(fmt.get("has_drm") for fmt in formats):
        result.error = "该视频有 DRM 版权保护，无法播放"
        result.detail = (
            "解析器能拿到视频流地址，但这些分片是 DRM 加密的（Widevine 等），"
            "需要浏览器硬件解密密钥才能解开。\n"
            "即使登录、带上 Cookie 也无法播放——这是版权保护，不是登录问题。"
        )
        return result

    result.error = "没有找到可以直接播放的流"
    result.detail = (
        "解析器打开了页面，但里面没有可以用来播放的媒体地址。\n"
        "可能原因：内容是付费/登录可见、页面里是播放器而不是媒体文件"
        "（如需要浏览器执行的 JS 播放器）。"
    )
    if playlist_note:
        result.detail += "\n" + playlist_note
    return result


def _separate_stream_detail(result: ResolveResult, playlist_note: str) -> str:
    """分离流时的多行说明——这是最常见的一种结果（B 站就是这样），必须讲透。"""
    lines = [
        "这个站点把画面和声音放在**两条独立的流**里（DASH 格式），",
        "播放内核一次只能打开一条地址，所以单靠一条地址播不出完整的音画。",
        "",
        "可以这样做：",
        "· 点「同时播放画面与声音」——播放器会同时打开这两条流并保持同步（推荐）；",
        "· 点「仅播放画面（无声音）」只播画面，省流量，或声音流出问题时用；",
        "· 点「外部打开」交给 VLC / mpv（它们会自己合流）。",
    ]
    if result.video_note:
        lines.append("")
        lines.append(f"解析到的画面流：{result.video_note}")
    if result.audio_note:
        lines.append(f"解析到的声音流：{result.audio_note}")
    if playlist_note:
        lines.append("")
        lines.append(playlist_note)
    return "\n".join(lines)


class _LoggerBridge:
    """yt-dlp 的日志回调：把它的输出接到我们自己的 log 上。"""

    def __init__(self, sink: LogFn, verbose: bool) -> None:
        self._sink = sink
        self._verbose = verbose

    def debug(self, message: str) -> None:
        text = str(message).strip()
        if not text or text.startswith("[debug] "):
            return
        if self._verbose:
            self._sink(text)

    def info(self, message: str) -> None:
        self.debug(message)

    def warning(self, message: str) -> None:
        text = str(message).strip()
        _log.warning("yt-dlp: %s", text)
        self._sink(f"  警告: {text}")

    def error(self, message: str) -> None:
        text = str(message).strip()
        _log.error("yt-dlp: %s", text)


def _network_options(
    proxy: str | None, cookie_file: str | None, user_agent: str | None
) -> dict:
    """从配置里取代理 / cookies / UA；取值失败一律忽略（解析不该被配置拖垮）。"""
    if proxy is None or cookie_file is None or user_agent is None:
        try:
            from src.utils import settings as S

            if proxy is None:
                proxy = S.get_str(S.K_PROXY, "")
            if cookie_file is None:
                cookie_file = S.get_str(S.K_COOKIE_FILE, "")
            if user_agent is None:
                user_agent = S.get_str(S.K_USER_AGENT, "")
        except Exception as exc:  # noqa: BLE001
            _log.debug("读取网络配置失败（忽略）: %s", exc)

    options: dict[str, Any] = {}
    if proxy:
        options["proxy"] = proxy
    # cookies 优先级：显式指定的 cookie 文件 > 直接从浏览器读。
    # `cookiesfrombrowser` 的取值是 (浏览器名, profile, keyring, container) 元组，
    # yt-dlp 只用前两个，`(chrome,)` / `(chrome, None)` 都合法。
    if cookie_file:
        if Path(cookie_file).is_file():
            options["cookiefile"] = cookie_file
        else:
            _log.warning("配置里的 cookies 文件不存在，已忽略: %s", cookie_file)
    else:
        browser = _cookie_browser()
        if browser:
            options["cookiesfrombrowser"] = (browser, None, None, None)
    if user_agent:
        options["http_headers"] = {"User-Agent": user_agent}
    return options


def _cookie_browser() -> str:
    """配置里指定的「从浏览器读 cookie」的浏览器名；空串表示不读。

    校验一下名字是否在 yt-dlp 支持的白名单里，避免把非法值直接塞给
    yt-dlp 导致报错（它会在拿到坏值时抛异常，让整个解析失败）。
    """
    try:
        from src.utils import settings as S

        name = S.get_str(S.K_COOKIE_FROM_BROWSER, "").strip().lower()
    except Exception as exc:  # noqa: BLE001
        _log.debug("读取浏览器 cookie 配置失败（忽略）: %s", exc)
        return ""
    if not name:
        return ""
    # yt-dlp 支持的浏览器名（见 yt_dlp.cookies.SUPPORTED_BROWSERS）
    supported = (
        "brave", "chrome", "chromium", "edge", "firefox", "opera",
        "safari", "vivaldi", "whale", "arc",
    )
    if name not in supported:
        _log.warning("不支持的浏览器名（已忽略）: %s", name)
        return ""
    return name


def _base_options(sink: LogFn, verbose: bool) -> dict[str, Any]:
    """与具体策略无关的那部分 yt-dlp 选项。"""
    return {
        "quiet": True,
        "no_warnings": not verbose,
        "noprogress": True,
        "skip_download": True,       # 只要地址，不下载
        "noplaylist": True,          # 页面里带合集时只取当前视频
        "cachedir": False,           # 不往用户目录写缓存（打包后目录可能只读）
        "no_color": True,
        "socket_timeout": 10,
        "retries": 1,
        "extractor_retries": 1,
        "logger": _LoggerBridge(sink, verbose),
    }


def _merge_options(base: dict, network: dict, spec: Strategy) -> dict:
    """把「基础选项 ← 网络配置 ← 本次策略」摞起来。

    `http_headers` 单独做**合并**而不是整体覆盖：策略只想改 UA 或补 Referer，
    不该顺手把用户在设置里填的另一个头丢掉。
    """
    options = dict(base)
    options.update(network)

    headers = dict(options.get("http_headers") or {})
    headers.update(spec.options.get("http_headers") or {})
    options.update({
        key: value for key, value in spec.options.items() if key != "http_headers"
    })
    if headers:
        options["http_headers"] = headers
    return options


def _extract(url: str, options: dict) -> dict:
    """真正调用 yt-dlp。只在工作线程里跑。"""
    from yt_dlp import YoutubeDL

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise ValueError(f"解析器返回了意外的结果类型: {type(info).__name__}")
    return info


def _is_haokan(url: str) -> bool:
    """是否是好看视频页面地址。"""
    try:
        host = (urlparse((url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in _HAOKAN_HOSTS or host.endswith(".haokan.baidu.com")


def _extract_haokan(url: str, sink: LogFn) -> dict | None:
    """好看视频专用提取：绕过 yt-dlp 内置 BaiduVideo 提取器（只认
    ``v.baidu.com/*.htm`` 老格式，不认识 ``haokan.baidu.com/v?vid=...``）。

    走法：从 URL 里抠出 ``vid``，请求移动端落地页
    ``mbd.baidu.com/newspage/data/videoshare?nid=sv_<vid>``，页面内嵌
    ``window.jsonData``，从 ``curVideoMeta.clarityUrl`` 按 rank 取最高清直链。

    返回标准 yt-dlp 风格 info（可交给 `_build_result`），拿不到返回 None。
    """
    target = (url or "").strip()
    try:
        parsed = urlparse(target)
        vid = parse_qs(parsed.query).get("vid", [""])[0].strip()
        if not vid:
            # 老格式 v.baidu.com/<type>/<id>.htm 由 yt-dlp 内置提取器处理，
            # 这里不抢。
            return None
    except ValueError:
        return None

    try:
        import requests
    except Exception as exc:  # noqa: BLE001
        _log.warning("requests 不可用，好看视频直链提取跳过: %s", exc)
        return None

    page_url = _HAOKAN_PAGE.format(vid=vid)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Referer": "https://haokan.baidu.com/",
    }
    try:
        sink("  识别为好看视频，直接抓取移动端落地页的播放地址…")
        resp = requests.get(page_url, headers=headers, timeout=12)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:  # noqa: BLE001
        _log.info("好看视频落地页请求失败: %s", exc)
        return None

    # 定位 window.jsonData，并用花括号配对截出完整 JSON（页面里字段含嵌套
    # 对象/数组，正则的贪婪匹配会截断，必须按括号深度收口）。
    marker = html.find("window.jsonData")
    if marker < 0:
        _log.info("好看视频落地页没有内嵌 jsonData")
        return None
    eq = html.find("=", marker)
    if eq < 0:
        return None
    start = eq + 1
    depth = 0
    end = start
    for i in range(start, len(html)):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        data = json.loads(html[start:end])
    except json.JSONDecodeError as exc:
        _log.info("好看视频 jsonData 解析失败: %s", exc)
        return None

    meta = data.get("curVideoMeta") or {}
    clarity = meta.get("clarityUrl")
    # 兼容旧版字段：有些页面直接给 playurl / clarityArr
    if not clarity and isinstance(meta.get("playurl"), str):
        clarity = [{"url": meta["playurl"], "rank": 0}]
    if not clarity and meta.get("clarityArr"):
        clarity = meta["clarityArr"]

    best = None
    if isinstance(clarity, list):
        best = max(
            (c for c in clarity if isinstance(c, dict) and c.get("url")),
            key=lambda c: int(c.get("rank", 0) or 0),
            default=None,
        )

    if best is None:
        _log.info("好看视频 jsonData 里没有可用清晰度地址")
        return None

    play_url = str(best.get("url") or "").strip()
    if not play_url:
        return None

    return {
        "id": vid,
        "title": str(meta.get("title") or ""),
        "extractor": "haokan",
        "extractor_key": "Haokan",
        "url": play_url,
        "format_id": str(best.get("key") or best.get("rank") or "best"),
        "format_note": str(best.get("title") or ""),
        "ext": "mp4",
        "protocol": "https",
        "width": int(best.get("vodVideoHW", "0$$0").split("$$")[-1] or 0) or None,
        "height": int(best.get("vodVideoHW", "0$$0").split("$$")[0] or 0) or None,
        "http_headers": {"Referer": "https://haokan.baidu.com/"},
    }



@dataclass
class _Attempt:
    """一次解析尝试的结果。"""

    status: str                       # ok | error | timeout
    info: dict | None = None
    error: BaseException | None = None


def _run_attempt(
    url: str, options: dict, budget: float
) -> _Attempt:
    """跑一次尝试，用硬超时兜住（yt-dlp 的 socket 超时管不住整体）。

    超时后线程放弃等待即可：它是守护线程，不会阻塞进程退出。
    """
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["info"] = _extract(url, options)
        except BaseException as exc:  # noqa: BLE001 - 任何异常都变成"这次失败"
            box["error"] = exc

    thread = threading.Thread(target=worker, daemon=True, name="ytdlp-resolve")
    thread.start()
    thread.join(budget)

    if thread.is_alive():
        return _Attempt(status="timeout")
    if "error" in box:
        return _Attempt(status="error", error=box["error"])
    return _Attempt(status="ok", info=box.get("info"))


def _budget_for(remaining: float, attempts_left: int) -> float:
    """给"当前这一次"分配多少秒。

    还有后续策略时要**留一点**给它们：第一条要是慢悠悠耗光预算，
    后面本来能救回来的重试就永远轮不到。真正耗时的失败（超时）本来也不重试，
    所以这点预留不影响正常路径。
    """
    if attempts_left <= 1:
        return max(1.0, remaining)
    reserve = min(_RETRY_RESERVE_S, remaining * 0.35)
    return max(2.0, remaining - reserve)


def _preflight(target: str, network: dict, sink: LogFn) -> Any:
    """解析前的网络可达性预检。不适用时返回 None（表示"不适用"，不是"不可达"）。"""
    try:
        from src.utils import settings as S

        if not S.resolve_preflight_enabled():
            return None
        use_doh = S.doh_fallback_enabled()
        endpoint = S.doh_endpoint()
    except Exception as exc:  # noqa: BLE001 - 读配置失败不该拖垮解析
        _log.debug("读取预检配置失败（跳过预检）: %s", exc)
        return None

    # 检测到代理在生效就跳过：走代理时"直连源站通不通"与能否播放无关，
    # 拿它判失败会把本来能播的地址误杀。
    # 尤其要认"系统代理"——Clash 这类工具的开关只改 Windows 系统代理，
    # 不改变裸 TCP，于是被墙站点直连必然超时，而 yt-dlp 其实是走代理的。
    hit = net_proxy.proxy_for(target, app_proxy=str(network.get("proxy") or ""))
    if hit is not None:
        _log.debug("检测到%s（%s），跳过可达性预检", hit.source, hit.proxy)
        return None

    from src.core.reachability import probe

    # "适不适用"由 probe 一处判定（非 http、取不到主机、本机地址一律返回 None），
    # 这里不再重复一遍——两处各判一次必然慢慢对不上。
    sink("  正在检查网络可达性…")
    endpoints = (endpoint,) if endpoint else None
    return probe(target, timeout=_PREFLIGHT_TIMEOUT_S, use_doh=use_doh,
                 endpoints=endpoints)


def _unreachable_detail(pre: Any) -> str:
    """把可达性结论整理成给用户看的失败说明。"""
    return (
        "已跳过网页解析：这个站点在当前网络下连不上，继续解析也拿不到播放地址。\n"
        "（不改网络直接重试不会有不同结果。）\n\n" + pre.report()
    )


def resolve(
    url: str,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    log: LogFn | None = None,
    cancel: threading.Event | None = None,
    verbose: bool = False,
    proxy: str | None = None,
    cookie_file: str | None = None,
    user_agent: str | None = None,
) -> ResolveResult:
    """解析一个页面地址，返回结果（不抛异常）。

    Args:
        url: 视频网站页面地址。
        timeout_s: 硬超时（秒）。超时立即返回失败，不等 yt-dlp。
        log: 过程日志回调（默认丢弃）。
        cancel: 置位后尽快收工；处理不了正在进行中的网络请求，靠调用方丢弃结果。
        verbose: 把 yt-dlp 的详细步骤也输出到 log。
        proxy / cookie_file / user_agent: 显式指定，默认从配置读取。

    Returns:
        ResolveResult。用 ``ok`` 判断成败，用 ``report()`` 拿可读报告。
    """
    sink: LogFn = log or _noop
    target = (url or "").strip()
    result = ResolveResult(page_url=target)

    if not target:
        result.error = "地址为空"
        return result
    if not is_available():
        result.error = "缺少解析组件 yt-dlp"
        result.detail = INSTALL_HINT
        return result
    if cancel is not None and cancel.is_set():
        result.error = "解析已取消"
        return result

    network = _network_options(proxy, cookie_file, user_agent)
    # JS 运行时是全局选项（不分策略），探一次到处用。
    # 注意探测结果 `{运行时名: 配置}` 必须放进 `js_runtimes` 这个键——yt-dlp 收的
    # 就是这个字典。曾经直接 `network.update(...)`，运行时名被当成顶层选项名，
    # 等于没传：日志里说"启用 node"，yt-dlp 却报"没有可用的 JavaScript 运行时"。
    js_runtime = detect_js_runtime()
    if js_runtime:
        network["js_runtimes"] = js_runtime

    # ---- 第 1 层：网络可达性预检 ----
    pre = _preflight(target, network, sink)
    if pre is not None and pre.blocked:
        _log.warning("站点不可达，跳过解析: %s", pre.headline)
        result.error = pre.headline
        result.detail = _unreachable_detail(pre)
        return result
    if pre is not None and pre.doh_rescued:
        sink("  系统 DNS 给出的地址不可用，已改用 DoH 找到的可用地址")

    # ---- 第 1.5 层：好看视频专用直链提取 ----
    # yt-dlp 内置的 BaiduVideo 提取器只认 v.baidu.com/*.htm 老格式，
    # 不认 haokan.baidu.com/v?vid=... 新格式（直接报 Unsupported URL）。
    # 这里先用自己的直链提取兜住，拿不到再落回 yt-dlp（老格式地址仍走它）。
    if _is_haokan(target):
        info = _extract_haokan(target, sink)
        if isinstance(info, dict):
            built = _build_result(target, info)
            if built.ok:
                _log.info("好看视频直链提取成功: %s -> %s", target, built.note)
                return built
            _log.info("好看视频直链提取未取得可播放流: %s", built.error)

    # ---- 第 2 层：逐策略解析 ----
    try:
        from src.utils import settings as S

        retry_enabled = S.resolve_retry_strategies_enabled()
    except Exception as exc:  # noqa: BLE001 - 读配置失败就用默认行为
        _log.debug("读取重试配置失败（按开启处理）: %s", exc)
        retry_enabled = True

    specs = strategies_for(target, enabled=retry_enabled)
    base = _base_options(sink, verbose)
    deadline = time.monotonic() + max(1.0, float(timeout_s))

    last_status = ""
    #: 留一条**最有信息量**的报错，而不是最后一条——理由见 `_error_rank()`。
    best_raw = ""

    for index, spec in enumerate(specs):
        if cancel is not None and cancel.is_set():
            result.error = "解析已取消"
            return result

        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            last_status = "timeout"
            break

        if len(specs) > 1:
            sink(f"  解析尝试 {index + 1}/{len(specs)}：{spec.label}")

        attempt = _run_attempt(
            target,
            _merge_options(base, network, spec),
            _budget_for(remaining, len(specs) - index),
        )
        last_status = attempt.status
        has_more = index + 1 < len(specs) and deadline - time.monotonic() > 1.0

        if attempt.status == "timeout":
            _log.warning(
                "解析超时（第 %d/%d 次尝试，%s）: %s",
                index + 1, len(specs), spec.label, target,
            )
            if has_more:
                continue
            break

        if attempt.status == "error" and attempt.error is not None:
            raw = str(attempt.error) or attempt.error.__class__.__name__
            if _error_rank(raw) > _error_rank(best_raw):
                best_raw = raw
            if _is_internal_bug(raw):
                # 这类报错几乎总是本程序的锅，别让它当成"站点原因"传下去
                _log.warning(
                    "策略「%s」抛了内部错误（多半是传给 yt-dlp 的选项不合法，"
                    "不是站点问题）: %s", spec.label, raw,
                )
            else:
                _log.warning(
                    "解析失败（第 %d/%d 次尝试，%s）: %s",
                    index + 1, len(specs), spec.label, _first_line(raw),
                )
            if is_retryable(raw) and has_more:
                sink(f"  被站点拒绝（{_first_line(raw)[:50]}），换一种方式再试…")
                continue
            break

        info = attempt.info
        if not isinstance(info, dict):
            result.error = "解析器没有返回可用信息"
            return result

        built = _build_result(target, info)
        if index > 0:
            # 让用户知道这次是"换了种方式才拿到的"——将来站点改版时，
            # 这条信息能直接告诉他哪种方式还有效。
            built.note = f"{built.note} · 改用「{spec.label}」".strip(" ·")
            _log.info("换用「%s」后解析成功: %s", spec.label, target)
        if built.ok:
            _log.info("解析成功: %s -> %s", target, built.note)
        else:
            _log.info("解析未取得单文件流: %s -> %s", target, built.error)
        return built

    if cancel is not None and cancel.is_set():
        result.error = "解析已取消"
        return result

    if last_status == "timeout":
        result.error = f"解析超时（{timeout_s} 秒）"
        result.detail = (
            "解析器在限定时间内没有返回。可能原因：站点响应慢、"
            "当前网络访问该站点不通、或该地址需要登录后才能取到播放地址。\n"
            "可以把超时调大后重试，或换成该视频的直链地址。"
        )
        return result

    # ---- 第 3 层：yt-dlp 失败后，降级到 streamlink 再试一次 ----
    # streamlink 覆盖约 130 个站点（Twitch / YouTube / Dailymotion 等），
    # 和 yt-dlp 有重叠也有互补。yt-dlp 报"不支持/被拒/结构变了"时，
    # 换个解析器常有意外收获——这是"尽量拿到地址"的最后一道保险。
    if best_raw:
        if streamlink_available():
            sink("  yt-dlp 未能解析，尝试用 streamlink 提取…")
            info = _streamlink_info(target, sink)
            if info is not None:
                built = _build_result(target, info)
                if built.ok:
                    built.note = f"{built.note} · 改用「streamlink」".strip(" ·")
                    _log.info("streamlink 降级解析成功: %s -> %s", target, built.note)
                    return built
                _log.info("streamlink 拿到了流但不可直接播放: %s", built.error)
        if _is_internal_bug(best_raw):
            # 所有策略都失败，而手上最有信息量的那条也只是我们自己的异常
            # 类型名。如实说明是程序内部问题，别引着用户去查站点。
            result.error = "解析失败：解析组件内部出错（不是站点问题）。"
            result.detail = (
                f"原始信息：{best_raw}\n\n"
                "这通常是本程序传给解析器的参数没被接受，而不是地址或网络有毛病。"
                "麻烦把日志文件一起反馈（设置 → 打开日志目录），便于定位。\n"
                "可以先确认该地址在浏览器里能正常播放，再重试一次。"
            )
            return result
        result.error = friendly_error(best_raw)
        result.detail = (
            f"原始信息：{_first_line(best_raw)}\n\n"
            f"yt-dlp 版本：{version() or '未知'}\n"
            "若提示「不支持该网站」，多半是解析器尚未跟进改版；"
            "若提示需要登录或「确认不是机器人」，可在设置里开启「从浏览器读 Cookie」。"
        )
        if not detect_js_runtime():
            result.detail += (
                "\n另外：本机没有可用的 JavaScript 运行时（deno / node / bun / quickjs），"
                "YouTube 这类站点会因此退化甚至解析失败。装一个 Node.js 即可，"
                "或在设置里指定 JS 运行时的路径。"
            )
        return result

    result.error = "解析器没有返回结果"
    return result
