"""配置持久化。使用 INI 格式，便于人工排查（而非写入注册表）。"""

from __future__ import annotations

from PySide6.QtCore import QSettings

from src.utils.paths import config_file

# ---------------------------------------------------------------- key 常量
# 播放后端
K_BACKEND = "backend/type"                     # qt | mpv
K_PREFER_MPV_FOR_LIVE = "backend/prefer_mpv_for_live"
K_MPV_DLL = "backend/mpv_dll"                  # 空 = 自动查找 mpv-2.dll

# 播放行为
K_VOLUME = "playback/volume"                   # 0.0 ~ 1.0
K_MUTED = "playback/muted"
K_RATE = "playback/rate"
K_AUTOPLAY_NEXT = "playback/autoplay_next"
K_SEEK_STEP_MS = "playback/seek_step_ms"
K_RESUME_LAST = "playback/resume_last"         # 启动时恢复上次播放的地址
#: 解析到 DASH 分离流（画面/声音各一条）时，是否自动合流播放而不弹框询问
K_AUTO_MERGE_SEPARATE = "playback/auto_merge_separate"
#: 内置内核彻底失败后，是否自动改用系统上的 VLC/mpv 播放原始地址
K_AUTO_EXTERNAL_FALLBACK = "playback/auto_external_fallback"

# 网络
K_PROXY = "network/proxy"
K_USER_AGENT = "network/user_agent"
K_COOKIE_FILE = "network/cookie_file"
#: 直接从浏览器读 cookie 的浏览器名（chrome/edge/firefox/brave/opera...），空 = 不用
K_COOKIE_FROM_BROWSER = "network/cookie_from_browser"
K_SOCKET_TIMEOUT = "network/socket_timeout"
#: 网页地址解析的总超时（秒）。站点响应慢、或解析器要换几种方式重试时，
#: 给足时间才拿得到地址；调得太小会把"慢但能成功"误报成"解析超时"。
K_RESOLVE_TIMEOUT_S = "network/resolve_timeout_s"
K_RTSP_TRANSPORT = "network/rtsp_transport"    # tcp | udp
K_LOAD_TIMEOUT_MS = "network/load_timeout_ms"

# 网页地址解析的"尽可能拿到"策略
#: 解析前先做 DNS/TCP 可达性预检：站点整段不通时立刻给出精确原因，
#: 而不是让 yt-dlp 白跑满「解析超时」（默认 60 秒）
K_RESOLVE_PREFLIGHT = "network/resolve_preflight"
#: 预检发现"系统 DNS 解析出来的地址连不上"时，改用 DoH 再查一次；
#: 只有 DoH 的地址实测可用才启用改写（治 DNS 污染）
K_DOH_FALLBACK = "network/doh_fallback"
#: DoH 端点；留空用内置默认（doh.pub -> dns.alidns.com）
K_DOH_ENDPOINT = "network/doh_endpoint"
#: 解析失败后按错误类型换策略重试（备用播放器接口 / 换 UA / 补 Referer）
K_RESOLVE_RETRY_STRATEGIES = "network/resolve_retry_strategies"
#: 交给 yt-dlp 的 JavaScript 运行时。
#: 留空 = 自动探测（deno -> node -> bun -> quickjs）；
#: 也可写 `node:C:\path\to\node.exe` 指定绝对路径，或写 `none` 禁用。
#: 2026 版 yt-dlp 起，没有它 YouTube 解析会退化（默默拿不到部分格式）。
K_JS_RUNTIME = "network/js_runtime"

# 本地反向代理（注入 Referer / UA / Cookie，解决防盗链）
K_LOCAL_PROXY_ENABLED = "proxy/enabled"
K_LOCAL_PROXY_PORT = "proxy/port"              # 0 = 自动选空闲端口
K_LOCAL_PROXY_REFERER = "proxy/referer"        # 空 = 从目标地址自动推导

# 剪贴板监听
K_CLIPBOARD_WATCH = "clipboard/watch"
K_CLIPBOARD_AUTOFILL = "clipboard/autofill"    # 识别到地址后是否直接填入输入框

# 下载
K_DOWNLOAD_DIR = "download/dir"
K_DOWNLOAD_CONCURRENCY = "download/concurrency"
K_FFMPEG_PATH = "download/ffmpeg_path"     # 空 = 自动查找（PATH / imageio-ffmpeg）

# 界面
K_WINDOW_GEOMETRY = "window/geometry"
K_WINDOW_STATE = "window/state"
K_PLAYLIST_PANEL_VISIBLE = "window/playlist_panel"
K_SETTINGS_VERSION = "app/settings_version"

_settings: QSettings | None = None


def settings() -> QSettings:
    """返回全局 QSettings 实例（惰性创建）。"""
    global _settings
    if _settings is None:
        _settings = QSettings(str(config_file()), QSettings.Format.IniFormat)
    return _settings


def get_float(key: str, default: float) -> float:
    try:
        return float(settings().value(key, default))
    except (TypeError, ValueError):
        return default


def get_int(key: str, default: int) -> int:
    try:
        return int(settings().value(key, default))
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    value = settings().value(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def get_str(key: str, default: str = "") -> str:
    value = settings().value(key, default)
    return default if value is None else str(value)


def local_proxy_enabled() -> bool:
    """本地反向代理是否启用。

    **只在这里读这一项**：`get_bool(key, default)` 的默认值由调用点传入，
    所以同一个开关如果各处各写一次 `False`，改默认值时必然漏掉几处。
    统一走这个函数，默认值的唯一来源就是 `DEFAULTS`。
    """
    return get_bool(K_LOCAL_PROXY_ENABLED, bool(DEFAULTS[K_LOCAL_PROXY_ENABLED]))


def _flag(key: str) -> bool:
    """读一个布尔项，默认值取自 `DEFAULTS`（唯一来源）。"""
    return get_bool(key, bool(DEFAULTS[key]))


def resolve_preflight_enabled() -> bool:
    """解析前是否做网络可达性预检。"""
    return _flag(K_RESOLVE_PREFLIGHT)


def doh_fallback_enabled() -> bool:
    """系统 DNS 不可靠时是否用 DoH 兜底。"""
    return _flag(K_DOH_FALLBACK)


def doh_endpoint() -> str:
    """用户指定的 DoH 端点；空串表示用内置默认。"""
    return get_str(K_DOH_ENDPOINT, "").strip()


def resolve_retry_strategies_enabled() -> bool:
    """解析失败后是否换策略重试。"""
    return _flag(K_RESOLVE_RETRY_STRATEGIES)


def auto_external_fallback_enabled() -> bool:
    """内置内核失败后是否自动改用系统 VLC/mpv 播放。"""
    return _flag(K_AUTO_EXTERNAL_FALLBACK)


# ---------------------------------------------------------------- 默认值
#: 各配置项的出厂默认值。设置面板的「恢复默认」与文档都以此为准，
#: 避免默认值散落在界面的各个角落（写两份必然对不上）。
DEFAULTS: dict[str, object] = {
    K_BACKEND: "qt",
    K_PREFER_MPV_FOR_LIVE: False,
    K_VOLUME: 0.8,
    K_MUTED: False,
    K_RATE: 1.0,
    K_AUTOPLAY_NEXT: True,
    K_SEEK_STEP_MS: 5000,
    K_RESUME_LAST: False,
    K_AUTO_MERGE_SEPARATE: True,
    K_AUTO_EXTERNAL_FALLBACK: False,
    K_PROXY: "",
    K_USER_AGENT: "",
    K_COOKIE_FILE: "",
    K_COOKIE_FROM_BROWSER: "",
    K_SOCKET_TIMEOUT: 10,
    K_RESOLVE_TIMEOUT_S: 60,
    K_RTSP_TRANSPORT: "udp",
    K_LOAD_TIMEOUT_MS: 0,          # 0 = 使用 src/core/url_router.py 里按类型定的超时
    # 三项"尽可能拿到视频地址"的默认全开：它们只在"本来就要失败"的路径上
    # 起作用（站点连不上、解析器被拒），不会给正常站点增加负担。
    K_RESOLVE_PREFLIGHT: True,
    K_DOH_FALLBACK: True,
    K_DOH_ENDPOINT: "",
    K_RESOLVE_RETRY_STRATEGIES: True,
    K_JS_RUNTIME: "",               # 空 = 自动探测
    # 默认开启：防盗链地址（B站 CDN 等）不补发 Referer 必然 403，
    # 关掉等于把"能播"变回"看运气"。只绑 127.0.0.1，不影响局域网暴露面。
    K_LOCAL_PROXY_ENABLED: True,
    K_LOCAL_PROXY_PORT: 0,
    K_LOCAL_PROXY_REFERER: "",
    K_CLIPBOARD_WATCH: False,
    K_CLIPBOARD_AUTOFILL: True,
    K_DOWNLOAD_DIR: "",            # 空 = 系统「下载」文件夹
    K_DOWNLOAD_CONCURRENCY: 2,
    K_FFMPEG_PATH: "",
    K_MPV_DLL: "",
    K_PLAYLIST_PANEL_VISIBLE: True,
}


def default_for(key: str):
    """取某项的出厂默认值（未登记时返回 None）。"""
    return DEFAULTS.get(key)


def reset_to_defaults() -> None:
    """把所有已登记的配置项恢复为出厂默认值。"""
    store = settings()
    for key, value in DEFAULTS.items():
        store.setValue(key, value)
    store.sync()


def as_dict() -> dict[str, str]:
    """当前生效的配置快照（值为字符串），供日志与"复制配置"功能使用。"""
    store = settings()
    return {
        key: str(store.value(key, default))
        for key, default in DEFAULTS.items()
    }
