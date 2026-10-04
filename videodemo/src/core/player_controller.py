"""
播放控制器：面向 UI 的唯一入口，隔离播放后端与界面。

六个关键设计:
1. **加载超时兜底**。QMediaPlayer 遇到打不开的地址时，常常长时间停在
   LoadingMedia 状态而**不触发** errorOccurred，界面会永久卡在"正在加载"。
   因此这里用定时器 + 状态检查强制判定失败并给出明确提示。
2. **直播流识别**。m3u8 直播没有总时长（duration 恒为 0），进度条需要切换为
   不确定态；断流时应当重连而不是当作"播放结束"。
3. **地址诊断先于抱怨**。光说"加载超时"对用户毫无帮助，所以每次加载都会在
   后台线程并行做一次分层连通性诊断（见 utils/net_diagnose.py）。一旦发现
   DNS/TCP/TLS 这类硬性失败，立刻结束等待并告知具体原因，而不是干等十秒。
4. **网页地址先解析再播**。视频网站的页面地址（B站/YouTube 等）本身不是媒体，
   直接丢给播放器必然失败。所以 WEB_PAGE 走"解析 → 播放"两步，解析同样是
   异步的（解析期间界面可交互，用户换地址会作废旧的那次）。
5. **无法判定的地址失败后再解析**。UNKNOWN_HTTP 的策略是"先当媒体直链试一次"，
   失败后自动交给解析器再试一次——很多站点自建的播放页就属于这一类。
6. **分离流用"第二路音轨"合流**。B 站这类站点只给 DASH 分离流（画面一条、
   声音一条），而播放内核一次只能打开一条地址。做法是主源播画面、再挂一路
   纯音频同时播放并保持同步（见 backends/qt_backend.py 的附加音轨实现），
   这样音画都在、且不需要转码或落盘。
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from src.core import resolver
from src.core.backends.base import MediaStatus, PlaybackState, PlayerBackend
from src.core.backends.factory import create_backend
from src.core.media_item import MediaItem
from src.core.resolver import ResolveResult
from src.core.url_router import UrlKind, classify, timeout_for
from src.utils import net_proxy
from src.utils import settings as S
from src.utils.env_setup import RTSP_TRANSPORT_NOTE
from src.utils.logger import get_logger
from src.utils.net_diagnose import Diagnosis, diagnose
from src.utils.paths import snapshot_dir

log = get_logger(__name__)

# QMediaPlayer.Error -> 中文提示
_ERROR_HINTS = {
    1: "资源无法访问：地址错误、文件不存在，或服务器拒绝访问（防盗链）。",
    2: "格式错误：不支持的编码格式，或缺少对应解码器。",
    3: "网络错误：连接超时或网络不可达。",
    4: "访问被拒绝：该地址需要额外的请求头（如 Referer/Cookie）才能访问。",
}

_RECONNECT_DELAY_MS = 3000

#: 诊断每个网络阶段的超时（秒）
_DIAG_TIMEOUT_S = 4.0
#: 加载已超时但诊断还没跑完时，额外顺延多久去拿真实原因
_DIAG_GRACE_MS = 3500


def _is_live_scheme(url: str) -> bool:
    scheme = url.split(":", 1)[0].lower()
    return scheme in ("rtsp", "rtsps", "rtmp", "rtmps", "rtmpt", "rtp", "udp", "srt")


def _item_is_live_capable(item: MediaItem | None) -> bool:
    """该条目是否可能是一个"没有总时长"的持续流（直播）。

    解析过的条目要看**解析出来的流**，而不是用户输入的页面地址。
    """
    if item is None:
        return False
    if item.effective_kind in (UrlKind.HLS, UrlKind.LIVE_STREAM, UrlKind.UNKNOWN_HTTP):
        return True
    return _is_live_scheme(item.play_url)



def open_externally(url: str) -> tuple[bool, str]:
    """用系统上已有的播放器打开地址，作为内核无法播放时的兜底。

    返回 (是否成功, 说明)。
    """
    if not url:
        return False, "地址为空"
    for name in ("vlc", "mpv", "potplayer", "ffplay"):
        exe = shutil.which(name)
        if exe:
            try:
                subprocess.Popen([exe, url])
                return True, f"已用 {name} 打开"
            except OSError as exc:
                log.warning("调用 %s 失败: %s", name, exc)
    try:
        os.startfile(url)  # type: ignore[attr-defined]  # 仅 Windows
        return True, "已用系统默认程序打开"
    except (OSError, AttributeError) as exc:
        return False, f"无法打开：{exc}"


def find_external_player() -> str:
    """系统上第一个可用的外部播放器名（vlc/mpv/potplayer/ffplay）；没有则空串。"""
    for name in ("vlc", "mpv", "potplayer", "ffplay"):
        if shutil.which(name):
            return name
    return ""


class PlayerController(QObject):
    """播放控制与状态管理。"""

    # 面向 UI 的信号
    statusMessage = Signal(str)          # 状态栏文本
    errorRaised = Signal(str, str)       # 标题, 详情
    itemChanged = Signal(object)         # MediaItem
    liveChanged = Signal(bool)           # 是否为直播
    seekableChanged = Signal(bool)

    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playbackStateChanged = Signal(int)
    bufferProgressChanged = Signal(float)
    endOfMedia = Signal()

    #: 地址诊断完成（工作线程 → 主线程，强制队列投递）。参数为 Diagnosis 对象
    diagnosisReady = Signal(object)
    #: 面向 UI 的诊断结果通知（已过滤掉过期结果，只带 Diagnosis）
    diagnosisFinished = Signal(object)

    #: 开始解析网页地址（参数为页面地址）
    resolveStarted = Signal(str)
    #: 解析结果（工作线程 → 主线程，强制队列投递）
    resolveReady = Signal(object)
    #: 面向 UI 的解析结果通知（已过滤过期结果，只带 ResolveResult）
    resolveFinished = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        self._current: MediaItem | None = None
        self._is_live = False
        self._awaiting_load = False
        self._media_loaded = False
        self._load_error: tuple[int, str] | None = None
        self._volume_before_mute = 0.8
        self._state = PlaybackState.STOPPED

        # 诊断相关状态
        self._diagnosis: Diagnosis | None = None
        self._diag_token: object | None = None
        self._diag_running = False
        self._timeout_extended = False

        # 解析相关状态
        self._resolve_token: object | None = None
        self._resolve_cancel: threading.Event | None = None
        self._resolving = False
        self._resolution: ResolveResult | None = None
        #: 本轮地址是否已经走过"失败后改用解析器再试"（避免反复重试）
        self._resolve_attempted = False
        #: 本轮地址是否已经试过合流播放（避免"播→失败→再播"的死循环）
        self._merged_attempted = False
        #: 本轮地址是否已经自动丢给过外部播放器（避免每次失败都重复弹）
        self._external_fallback_attempted = False

        self._backend: PlayerBackend = create_backend(UrlKind.UNKNOWN, self)

        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.timeout.connect(self._on_load_timeout)

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._reconnect)

        # 诊断在工作线程里跑，用队列连接避免跨线程直接调用
        self.diagnosisReady.connect(
            self._on_diagnosis_ready, Qt.ConnectionType.QueuedConnection
        )
        self.resolveReady.connect(
            self._on_resolve_ready, Qt.ConnectionType.QueuedConnection
        )

        self._connect_backend()

        # 恢复上次的音量/倍速
        volume = S.get_float(S.K_VOLUME, 0.8)
        self._backend.set_volume(volume)
        self._volume_before_mute = volume or 0.8
        self._backend.set_muted(S.get_bool(S.K_MUTED, False))
        self._backend.set_rate(S.get_float(S.K_RATE, 1.0))

    # ------------------------------------------------------------ 后端接线
    def _connect_backend(self) -> None:
        b = self._backend
        b.positionChanged.connect(self.positionChanged)
        b.durationChanged.connect(self._on_duration)
        b.playbackStateChanged.connect(self._on_playback_state)
        b.mediaStatusChanged.connect(self._on_media_status)
        b.bufferProgressChanged.connect(self.bufferProgressChanged)
        b.errorOccurred.connect(self._on_error)
        b.endOfMedia.connect(self._on_end_of_media)
        b.extraAudioFailed.connect(self._on_extra_audio_failed)
        b.extraAudioStalled.connect(self._on_extra_audio_stalled)

    def _on_playback_state(self, state: int) -> None:
        self._state = state
        self.playbackStateChanged.emit(state)

    @property
    def backend(self) -> PlayerBackend:
        return self._backend

    @property
    def current(self) -> MediaItem | None:
        return self._current

    @property
    def is_live(self) -> bool:
        return self._is_live

    # ------------------------------------------------------------ 播放入口
    def play_text(self, text: str) -> bool:
        """解析用户输入（URL 或本地路径）并开始播放。"""
        text = (text or "").strip().strip('"').strip("'")
        if not text:
            self.errorRaised.emit("地址为空", "请粘贴一个视频地址或选择本地文件。")
            return False

        kind = classify(text)
        log.info("播放请求: kind=%s url=%s", kind.value, text)

        if kind is UrlKind.UNKNOWN:
            self.errorRaised.emit(
                "无法识别的地址",
                f"看不懂这个地址：\n{text}\n\n"
                "支持的格式：\n"
                "· 本地文件路径，如 D:\\video\\a.mp4\n"
                "· 媒体直链，如 https://example.com/a.mp4\n"
                "· HLS 清单，如 https://example.com/a.m3u8\n"
                "· 实时流，如 rtsp://、rtmp:// 开头\n"
                "· 视频网站页面，如 https://www.bilibili.com/video/BVxxx",
            )
            return False

        if kind is UrlKind.WEB_PAGE:
            # 页面地址本身不是媒体，必须先解析出真实流地址
            return self._begin_resolve(text)

        if kind is UrlKind.LOCAL and not os.path.exists(text):
            self.errorRaised.emit("文件不存在", f"找不到这个文件：\n{text}")
            return False

        item = MediaItem.from_url(text)
        self._resolve_attempted = False
        self._merged_attempted = False
        self._external_fallback_attempted = False
        self._start(item)
        return True

    # ------------------------------------------------------------ 网页地址解析
    def last_resolution(self) -> ResolveResult | None:
        """最近一次解析结果（供界面展示与降级选择）。"""
        return self._resolution

    @property
    def is_resolving(self) -> bool:
        return self._resolving

    def _begin_resolve(self, page_url: str, *, what: str = "网页地址",
                       status: str = "") -> bool:
        """异步解析一个页面地址，成功后自动开始播放。

        返回 False 表示没能启动解析（原因已通过 errorRaised 告知用户）。
        """
        if not resolver.is_available():
            self.errorRaised.emit(
                "缺少网页解析组件",
                f"{what}需要由 yt-dlp 解析出真实流地址，"
                "但当前环境里没有安装它。\n\n" + resolver.INSTALL_HINT,
            )
            return False

        token = object()
        cancel = threading.Event()
        self._resolve_token = token
        self._resolve_cancel = cancel
        self._resolving = True
        self._resolution = None
        self._load_error = None
        self._merged_attempted = False
        self._external_fallback_attempted = False

        # 解析期间不该再有任何倒计时在跑，否则会误报"加载超时"
        self._load_timer.stop()
        self._reconnect_timer.stop()
        self._awaiting_load = False
        self._media_loaded = False
        self._diag_token = None
        self._backend.stop()

        # 清掉上一轮的进度显示，别让用户看着旧的时长等新地址
        self.positionChanged.emit(0)
        self.durationChanged.emit(0)
        self.liveChanged.emit(False)
        self.seekableChanged.emit(False)

        self.resolveStarted.emit(page_url)
        self.statusMessage.emit(status or f"正在解析{what}…")
        log.info("开始解析%s: %s", what, page_url)

        threading.Thread(
            target=self._resolve_worker,
            args=(token, cancel, page_url),
            name="web-resolve",
            daemon=True,
        ).start()
        return True

    def _resolve_worker(self, token: object, cancel: threading.Event,
                        page_url: str) -> None:
        """工作线程入口：只解析与上报，绝不碰界面状态。"""
        try:
            result = resolver.resolve(page_url, log=self._resolve_log, cancel=cancel)
        except Exception as exc:  # noqa: BLE001 - 解析失败绝不能把界面搞崩
            log.warning("解析异常: %s", exc)
            result = ResolveResult(page_url=page_url, error=f"解析异常：{exc}")

        self.resolveReady.emit((token, result))

    @staticmethod
    def _resolve_log(message: str) -> None:
        """yt-dlp 的过程输出（工作线程内调用，只写日志）。"""
        log.debug("解析: %s", message)

    def _on_resolve_ready(self, payload: object) -> None:
        """主线程槽：解析结果回来了。"""
        try:
            token, result = payload  # type: ignore[misc]
        except (TypeError, ValueError):
            return

        if token is not self._resolve_token:
            # 用户已经换了地址，这份结果过期了：直接丢弃，不要覆盖新的一轮
            log.info("丢弃过期的解析结果: %s", getattr(result, "page_url", "?"))
            return

        self._resolving = False
        self._resolution = result if isinstance(result, ResolveResult) else None
        if self._resolution is not None:
            self.resolveFinished.emit(self._resolution)

        if self._resolution is None:
            self._fail("解析失败", "解析器没有返回结果。", allow_retry=False)
            return

        if not self._resolution.ok:
            # 保留页面地址，"外部打开"与重试都还用得上
            self._current = MediaItem.from_url(self._resolution.page_url)

            # 站点只提供分离流（B 站 / YouTube 的 DASH）时，解析器给的是
            # ok=False 加上"画面一条、声音一条"。这**不是**播不了：画面流当主源、
            # 声音流挂成附加音轨就能音画齐全。既然做得到，就别再弹个框让用户
            # 每次手动点一下——粘贴地址直接开播才是他想要的。
            # 后端做不到、或用户关掉了这项自动行为，才走失败提示。
            if self._can_auto_merge(self._resolution):
                log.info(
                    "解析只拿到分离流，自动合流播放: %s", self._resolution.page_url
                )
                self.statusMessage.emit(
                    "该站点只有分离的音视频流，正在自动把画面与声音合起来播放…"
                )
                if self.play_merged():
                    return

            detail = self._resolution.error
            if self._resolution.detail:
                detail += "\n\n" + self._resolution.detail
            self._fail("网页地址解析失败", detail, allow_retry=False)
            return

        item = MediaItem.from_url(self._resolution.page_url)
        item.resolved_url = self._resolution.url
        item.resolved_kind = self._resolution.kind
        item.title = self._resolution.title or item.title
        item.duration_ms = self._resolution.duration_ms
        item.http_headers = self._resolution.headers
        item.resolver_note = self._resolution.note

        log.info("解析成功，开始播放: %s", self._resolution.url)
        self.statusMessage.emit(f"已解析（{self._resolution.note}），正在加载…")
        self._start(item)

    def _current_resolution(self) -> ResolveResult | None:
        """当前条目对应的那份解析结果，**过期的不算**。

        用户换成别的地址之后，上一轮的解析结果不该再提供「仅画面 / 合流」
        这类入口——点了会把上一个视频播回来。
        """
        result = self._resolution
        if result is None:
            return None
        if self._current is not None and self._current.url != result.page_url:
            return None
        return result

    def can_play_video_only(self) -> bool:
        """当前是否有"只播画面"的降级方案可用。

        站点只提供分离的音视频流时，画面那条流本身是可以单独播放的，
        比直接失败要有用得多。
        """
        result = self._current_resolution()
        return bool(result is not None and result.video_url)

    def play_video_only(self) -> bool:
        """降级播放：只播画面（没有声音）。"""
        result = self._current_resolution()
        if result is None or not result.video_url:
            return False

        item = MediaItem.from_url(result.page_url)
        item.resolved_url = result.video_url
        item.resolved_kind = result.video_kind
        item.title = (result.title or item.title) + "（仅画面）"
        item.duration_ms = result.duration_ms
        item.http_headers = result.headers
        item.resolver_note = result.video_note
        log.info("降级播放画面流: %s", result.video_url)
        self.statusMessage.emit("正在播放画面流（该站点没有音视频合一的流，本条没有声音）")
        self._start(item)
        return True

    def _can_auto_merge(self, result: ResolveResult) -> bool:
        """这份"只有分离流"的解析结果，是否应当**自动**合流播放。

        四个条件缺一不可：确实拿到了音画两条流、后端支持附加音轨、用户没有
        关掉自动合流、本轮还没试过。最后一条是为了避免死循环——合流播放要是
        加载失败，`_fail` 会走到错误框，此时再自动重播同一个组合没有意义，
        该退到「仅播放画面」。
        """
        # 要两条都拿到才谈得上合流；只有一条时 play_merged() 也做不了
        if not (result.video_url and result.audio_url):
            return False
        if self._merged_attempted:
            return False
        if not S.get_bool(S.K_AUTO_MERGE_SEPARATE, True):
            return False
        return self._backend.supports_extra_audio()

    def can_play_merged(self) -> bool:
        """当前是否有"画面+声音一起播"的方案可用。

        站点只提供 DASH 分离流时这是**首选**方案（音画都有），比只播画面好，
        所以界面上把它排在「仅播放画面」前面。已经试过并失败的那一轮不再提供，
        免得用户点到一个刚刚才失败过的组合。
        """
        result = self._current_resolution()
        if result is None or not result.video_url or not result.audio_url:
            return False
        if self._merged_attempted:
            return False
        return self._backend.supports_extra_audio()

    def play_merged(self) -> bool:
        """合流播放：画面流当主源，声音流挂在附加音轨上同步播放。

        这是应对 DASH 分离流（画面一条、声音一条）的正解——不转码、不落盘，
        播放内核同时打开两条地址即可，声音由后端负责与画面保持同步。
        """
        result = self._current_resolution()
        if result is None or not result.video_url or not result.audio_url:
            return False
        if not self._backend.supports_extra_audio():
            # 后端做不到就退回仅画面，至少让用户看到内容
            return self.play_video_only()

        # 记下"这一轮试过合流了"：万一马上失败，错误框不该再推荐同一个组合
        self._merged_attempted = True

        item = MediaItem.from_url(result.page_url)
        item.resolved_url = result.video_url
        item.resolved_kind = result.video_kind
        item.audio_url = result.audio_url
        item.title = result.title or item.title
        item.duration_ms = result.duration_ms
        # 必须带上解析器拿到的请求头（B 站 upos CDN 严格校验 Referer），
        # 否则本地反向代理只能用 CDN 域名瞎猜 Referer，防盗链照样 403。
        item.http_headers = result.headers
        item.resolver_note = "画面+声音 · 本地双流同步"
        log.info("合流播放: 画面=%s 声音=%s", result.video_url, result.audio_url)
        self.statusMessage.emit("站点提供的是分离流，正在把画面与声音合起来播放…")
        self._start(item)
        return True

    def _on_extra_audio_failed(self, reason: str) -> None:
        """附加音轨挂了。**画面不受影响**，只是变成无声，所以只提示不报错。"""
        if self._current is None or not self._current.has_separate_audio:
            return
        log.warning("附加音轨失败，降级为仅画面: %s", reason)
        self.statusMessage.emit(f"声音流加载失败（{reason}），已降级为仅画面播放")

    def _on_extra_audio_stalled(self, reason: str) -> None:
        """音轨还在，但已经追不上画面了（自动校正已停止）。

        这种情况必须说出来：用户看到的是"画面正常但没声音"，而这正是最难
        自行判断的一类故障——画面一切正常，只有耳朵能发现问题。
        """
        if self._current is None or not self._current.has_separate_audio:
            return
        log.warning("附加音轨与画面失去同步，已停止自动校正: %s", reason)
        self.statusMessage.emit(
            f"{reason}，已停止自动校正；若仍然没有声音，"
            "可在「设置 → 播放内核」里换成 mpv 内核再试"
        )

    def _try_resolve_fallback(self) -> bool:
        """直连失败后的兜底：把地址交给解析器再试一次。

        只针对「无法判定的 http(s) 地址」——这类地址可能就是某个视频网页
        （站点自建的播放页、局域网里的页面），把它当媒体文件打开必然失败。
        每轮地址只试一次，避免解析失败后无限来回。
        """
        item = self._current
        if item is None or self._resolve_attempted:
            return False
        if item.effective_kind is not UrlKind.UNKNOWN_HTTP:
            return False
        if not resolver.is_available():
            return False

        self._resolve_attempted = True
        log.info("直连失败，改用解析器再试: %s", item.url)
        return self._begin_resolve(
            item.url,
            what="该地址",
            status="直接打开失败，正在尝试解析这个地址…",
        )

    def _start(self, item: MediaItem) -> None:
        # 任何一次新的播放都作废进行中的解析：否则一个迟到的解析结果
        # 会把用户刚刚换成的新地址顶掉。
        self._invalidate_resolve()

        self._current = item
        self._is_live = False
        self._media_loaded = False
        self._load_error = None
        self._awaiting_load = True

        # 新一轮加载：作废上一轮的诊断结果
        self._diagnosis = None
        self._timeout_extended = False
        self._start_diagnosis(item)

        self.itemChanged.emit(item)
        self.liveChanged.emit(False)
        self.seekableChanged.emit(False)
        self.statusMessage.emit(f"正在加载：{item.title or item.url}")

        # 重置进度显示
        self.positionChanged.emit(0)
        self.durationChanged.emit(0)

        # 换源时先拆掉上一轮的附加音轨，免得它接着播上一个视频的声音
        self._backend.clear_audio_source()
        self._backend.set_source(self._proxy_route(item.play_url, item), item.http_headers or None)
        if item.has_separate_audio and self._backend.supports_extra_audio():
            # **必须赶在 play() 之前挂上**：两路在同一次播放指令里启动，
            # 音画偏差才只有十几毫秒；错开启动会差出上百毫秒。
            self._backend.set_audio_source(
                self._proxy_route(item.audio_play_url, item, quiet=True),
                item.http_headers or None,
            )
        self._backend.play()

        timeout = timeout_for(item.effective_kind)
        self._load_timer.start(timeout)
        log.info("已开始加载，超时兜底 %d ms", timeout)

    def _proxy_route(self, url: str, item: MediaItem, *, quiet: bool = False) -> str:
        """需要自定义请求头时，改走本地反向代理（见 core/local_proxy.py）。

        播放内核发不出 Referer/User-Agent，而防盗链正是靠它们判定的。开启本地
        反向代理后，播放器请求的是 `127.0.0.1`，由代理把正确的头补上再转发。

        **三种情况下都要走代理**：
        1. 地址自带请求头（解析器从站点取回来的），不走就一定 403；
        2. 用户显式打开了代理开关——这正对应"我这条直链播不了，想补个 Referer"
           的场景。早期版本只认第 1 种，结果用户填了 Referer、开了开关，
           代理却因为"这条地址没有 headers"被整个跳过，设置形同虚设。
        3. **有代理在生效**（设置里填的 / 环境变量 / Windows 系统代理）。
           播放内核（Qt 的 FFmpeg 后端）不会用系统代理，而解析器用的是
           requests / yt-dlp，两者读的是同一份代理配置——于是出现"解析明明
           成功，播放却报连不上服务器"。本地反向代理转发时用的是 requests，
           会照同一份配置走代理，正好把这一环补上。

        代理没开启、地址不适合（HLS 清单、rtsp 等）、目标是本机回环、或启动
        失败时，一律返回原地址——这条链路是"能增强就增强"，绝不能让播放变差。
        """
        if not url:
            return url

        # 本机地址直连。本机服务不存在防盗链问题，而代理自己就监听在回环上，
        # 代理回环地址等于白绕一圈，还平白多一个可能失败的环节。
        from src.core.url_router import is_loopback

        if is_loopback(url):
            return url

        from src.core import local_proxy

        explicitly_on = S.local_proxy_enabled()
        upstream = net_proxy.proxy_for(url, app_proxy=S.get_str(S.K_PROXY, ""))

        # 有代理在生效时**必须**走这一层：播放内核不认系统代理，直连必然失败。
        # 这条不算"防盗链增强"，而是唯一能播的路子，所以不受总开关约束
        # （见 `local_proxy.proxy_url` 的 required 参数）。
        must_route = upstream is not None

        if not item.http_headers and not explicitly_on and not must_route:
            return url

        # 把解析器拿到的请求头一并交给代理：它可能带着比"站点根"更准确的
        # Referer/User-Agent（B 站这类 CDN 域名与站点域名不同的地址尤其重要）。
        proxied = local_proxy.proxy_url(
            url, item.http_headers or None, required=must_route
        )
        if not proxied:
            return url

        if item.http_headers:
            log.info(
                "该地址需要请求头 %s，改走本地反向代理: %s",
                ", ".join(item.http_headers), url,
            )
        elif must_route:
            log.info(
                "检测到%s（%s），改走本地反向代理，让播放也走代理: %s",
                upstream.source, upstream.proxy, url,
            )
        else:
            log.info("已开启防盗链代理，改走本地反向代理: %s", url)
        if not quiet:
            note = item.resolver_note
            item.resolver_note = (
                f"{note} · 已走本地代理代发请求头" if note else "已走本地代理代发请求头"
            )
            self.statusMessage.emit("已改走本地代理补发请求头")
        return proxied

    # ------------------------------------------------------------ 地址诊断
    def _start_diagnosis(self, item: MediaItem) -> None:
        """在后台线程做一次分层连通性诊断。

        本地文件无需联网，直接跳过；其余一律并行探测，不影响加载。
        """
        token = object()
        self._diag_token = token

        if item.kind is UrlKind.LOCAL:
            self._diag_running = False
            return

        self._diag_running = True
        threading.Thread(
            target=self._diagnose_worker,
            args=(token, item.play_url),
            name="url-diagnose",
            daemon=True,
        ).start()

    def _diagnose_worker(self, token: object, url: str) -> None:
        """工作线程入口：只做探测与上报，不碰任何 UI 状态。"""
        try:
            result: Diagnosis | None = diagnose(url, timeout=_DIAG_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - 诊断失败绝不能影响播放
            log.warning("地址诊断异常: %s", exc)
            result = None

        # 信号带令牌一起回去，槽里据此丢弃已过期的结果
        self.diagnosisReady.emit((token, result))

    def _on_diagnosis_ready(self, payload: object) -> None:
        """主线程槽：诊断结果回来了。"""
        try:
            token, result = payload  # type: ignore[misc]
        except (TypeError, ValueError):
            return

        if token is not self._diag_token:
            # 用户已经换了地址，这份结果过期了
            return

        self._diag_running = False
        if not isinstance(result, Diagnosis):
            return

        self._diagnosis = result
        log.info("地址诊断:\n%s", result.report())
        self.diagnosisFinished.emit(result)

        if not self._awaiting_load:
            # 已经播放成功了（或有别的错误），诊断只留档
            return

        if not result.reachable and not result.is_local:
            # DNS / TCP / TLS 硬性失败：继续等下去没有意义，立即给出具体原因。
            # 这种情况再交给解析器也是白跑一趟（同样的网络问题），故不允许重试。
            self._backend.stop()
            self._fail("无法连接到服务器", result.report(), allow_retry=False)
            return

        # 能连上，但协议握手异常：只提示，不终止（部分服务器会对
        # OPTIONS/HEAD 返回 404/405，实际却能正常播放，不能据此误杀）
        if not result.protocol_ok:
            log.warning("协议握手未通过，继续尝试播放: %s", result.headline)
            self.statusMessage.emit(f"服务器有响应，但 {result.headline}")

    def diagnosis(self) -> Diagnosis | None:
        """最近一次诊断结果（供界面展示）。"""
        return self._diagnosis

    def run_diagnosis_now(self, url: str) -> None:
        """手动触发诊断（界面上的「诊断」按钮）。"""
        token = object()
        self._diag_token = token
        self._diag_running = True
        threading.Thread(
            target=self._diagnose_worker,
            args=(token, url),
            name="url-diagnose-manual",
            daemon=True,
        ).start()

    def _invalidate_resolve(self) -> None:
        """让进行中的解析作废：迟到的结果会被 _on_resolve_ready 丢弃。"""
        if self._resolve_cancel is not None:
            self._resolve_cancel.set()
        self._resolve_token = None
        self._resolve_cancel = None
        self._resolving = False

    def stop(self) -> None:
        was_resolving = self._resolving
        self._load_timer.stop()
        self._reconnect_timer.stop()
        self._awaiting_load = False
        self._diag_token = None       # 作废进行中的诊断
        self._diag_running = False
        self._invalidate_resolve()
        self._backend.stop()
        self.statusMessage.emit("已取消解析" if was_resolving else "已停止")

    def pause(self) -> None:
        self._backend.pause()

    def toggle_play_pause(self) -> None:
        if self._current is None:
            return
        if self._state == PlaybackState.PLAYING:
            self._backend.pause()
        else:
            self._backend.play()

    def _current_state(self) -> int:
        return self._state

    def set_position(self, ms: int) -> None:
        if self._awaiting_load or self._is_live:
            return
        self._backend.set_position(int(ms))

    def seek_by(self, delta_ms: int) -> None:
        target = max(0, self._backend.position() + int(delta_ms))
        duration = self._backend.duration()
        if duration > 0:
            target = min(target, duration)
        self.set_position(target)

    def set_volume(self, volume: float) -> None:
        self._backend.set_volume(volume)
        S.settings().setValue(S.K_VOLUME, float(volume))

    def set_muted(self, muted: bool) -> None:
        if muted:
            if not S.get_bool(S.K_MUTED, False):
                self._volume_before_mute = S.get_float(S.K_VOLUME, 0.8) or 0.8
        elif S.get_float(S.K_VOLUME, 0.8) <= 0.0 and self._volume_before_mute > 0.0:
            # 取消静音时若音量正好是 0，恢复静音前的音量，避免"取消了还是没声音"
            self.set_volume(self._volume_before_mute)
        self._backend.set_muted(muted)
        S.settings().setValue(S.K_MUTED, bool(muted))

    def set_rate(self, rate: float) -> None:
        self._backend.set_rate(rate)
        S.settings().setValue(S.K_RATE, float(rate))

    # ------------------------------------------------------------ 截图
    def take_snapshot(self) -> Path | None:
        image = self._backend.grab_frame()
        if image is None or image.isNull():
            self.statusMessage.emit("截图失败：当前没有可用画面")
            return None
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = snapshot_dir() / f"snapshot_{stamp}.png"
        if image.save(str(path), "PNG"):
            self.statusMessage.emit(f"截图已保存：{path}")
            return path
        self.statusMessage.emit("截图保存失败")
        return None

    # ------------------------------------------------------------ 状态回调
    def _on_duration(self, ms: int) -> None:
        self._refresh_live(ms)
        self.durationChanged.emit(ms)

    def _refresh_live(self, duration_ms: int) -> None:
        """判定当前是否为直播（时长未知的持续流）。

        必须等媒体确实加载成功后才判定，否则加载初期那次 duration=0 会把
        普通点播误判成直播。

        另外不能只依赖 durationChanged：直播流 duration 恒为 0 且此后不再变化，
        该信号可能只在加载完成之前来过一次，所以加载成功时还要重算一次。
        """
        live = (
            self._media_loaded
            and duration_ms <= 0
            and _item_is_live_capable(self._current)
        )
        if live != self._is_live:
            self._is_live = live
            self.liveChanged.emit(live)
            self.seekableChanged.emit(self._backend.is_seekable() and not live)

    def _on_media_status(self, status: int) -> None:
        if status not in (MediaStatus.NO_MEDIA, MediaStatus.LOADING):
            # 已经进入加载成功/缓冲/播放阶段，取消超时兜底
            if self._awaiting_load and status != MediaStatus.INVALID:
                self._awaiting_load = False
                self._media_loaded = True
                self._load_timer.stop()
                # 直播流此后可能不再发 durationChanged，这里补一次判定
                self._refresh_live(self._backend.duration())
                title = self._current.title if self._current else ""
                note = self._current.resolver_note if self._current else ""
                # 把"解析到了什么"留在状态栏上，用户才知道自己看的是哪一档清晰度
                suffix = f"（{note}）" if note else ""
                if self._current is not None and self._current.has_separate_audio:
                    # 说清"声音来自另一条流"，免得用户以为漏了什么。
                    # 这里不断言"已同步"：音轨可能随后才加载成功（或失败）。
                    suffix += "（声音来自独立音轨）"
                self.statusMessage.emit(f"正在播放：{title}{suffix}")

        if status == MediaStatus.STALLED:
            self.statusMessage.emit("缓冲中…")
        elif status == MediaStatus.INVALID and self._awaiting_load:
            # 有些后端会直接给 InvalidMedia 而不发 errorOccurred
            self._fail("无法播放该地址", "播放器判定该媒体无效（格式不支持或地址不可达）。")

        if status in (MediaStatus.LOADED, MediaStatus.BUFFERED):
            self.seekableChanged.emit(self._backend.is_seekable() and not self._is_live)

    def _on_error(self, code: int, message: str) -> None:
        """后端报错。**已经播起来之后再报的错不当成致命错误**。

        为什么必须区分：Qt 的 FFmpeg 后端在"可自愈"的场合也会发
        errorOccurred——实测（.tools/switch_probe.py）本地 mp4 正常播放时，
        加载成功约 50 ms 后就会报一次 `Demuxing failed`，而画面照常往后走
        （FFmpeg 日志显示是 seek/读取片段的小问题）。若不区分，用户会在画面
        正常播放的同时被一个"播放失败"对话框打断，属于典型的误报。
        真正致命的错误都发生在"还没加载成功"阶段，用 _awaiting_load 就能分辨。
        """
        if self._media_loaded:
            if self._load_error is None:
                self._load_error = (code, message)
                log.warning("播放中报告错误（继续播放）: code=%s %s", code, message)
                self.statusMessage.emit(f"已忽略一次播放中的可恢复错误：{message}")
            return

        if self._load_error is not None:
            return
        self._load_error = (code, message)
        hint = _ERROR_HINTS.get(code, "未知错误。")
        self._fail(
            "播放失败",
            f"{message}\n\n{hint}{self._protocol_hint()}{self._diagnosis_suffix()}",
        )

    def _diagnosis_suffix(self) -> str:
        """把诊断报告附到错误详情里，让用户看到真实原因。"""
        if self._diagnosis is None:
            return ""
        return "\n\n—— 地址诊断 ——\n" + self._diagnosis.report()

    def _protocol_hint(self) -> str:
        """针对不同协议给出可操作的建议。

        注意不要写做不到的事：内置的 Qt 后端无法把 RTSP 改成 TCP 传输，
        所以这里只如实说明，并指向真正可行的做法（外部播放器）。
        """
        if self._current is None:
            return ""

        if self._current.resolved_url:
            return (
                "\n\n排查建议：\n"
                "· 这是从网页地址解析出来的临时直链，通常几小时后失效，"
                "重新点一次「播放」即可重新解析；\n"
                "· 部分站点要求携带 Referer / Cookie，内置内核无法发送请求头，"
                "此时可点「外部打开」改用 VLC / mpv。"
            )

        if _is_live_scheme(self._current.play_url):
            return (
                "\n\n排查建议：\n"
                "· 确认地址、端口、路径正确，且服务端此刻确实在推流；\n"
                f"· {RTSP_TRANSPORT_NOTE}——"
                "若你的网络屏蔽 UDP，会表现为一直加载不出画面，"
                "此时请点「外部打开」改用 VLC / mpv（它们支持强制走 TCP）；\n"
                "· 该流若需要鉴权，请确认用户名密码已包含在地址中。"
            )
        if self._current.effective_kind is UrlKind.HLS:
            return "\n\n排查建议：清单地址可能已过期，或需要携带 token/Cookie。"
        return "\n\n可点击「外部打开」按钮改用系统上的播放器试试。"

    def _fail(self, title: str, detail: str, *, allow_retry: bool = True) -> None:
        """判定失败。allow_retry=False 用于"再试也没意义"的场景（如解析失败本身）。"""
        if allow_retry and self._try_resolve_fallback():
            # 已经改用解析器重试，这不是终局，不弹错误
            return

        # 终局前的最后一道兜底：内置内核彻底失败，若用户开了"自动外部播放"，
        # 且系统上有 VLC/mpv，就把原始地址丢给它再试一次——很多格式
        # （RTSP 走 TCP、带鉴权的流、特殊编码）内置内核啃不动，外部播放器能行。
        if self._maybe_auto_external(title):
            return

        self._awaiting_load = False
        self._load_timer.stop()
        self.statusMessage.emit(title)
        log.error("%s: %s", title, detail.replace("\n", " "))
        self.errorRaised.emit(title, detail)

    def _maybe_auto_external(self, title: str) -> bool:
        """失败终局前，尝试把原始地址自动交给系统外部播放器。

        返回 True 表示已接管（不再弹错误框），False 表示不触发（照常报错）。
        只在以下条件**同时**满足时触发，避免打扰：
        - 用户开了「自动外部播放」开关；
        - 本轮还没试过（不重复弹）；
        - 系统上确实有可用的外部播放器；
        - 存在可交给外部播放器的原始地址。
        """
        if self._external_fallback_attempted:
            return False
        self._external_fallback_attempted = True

        try:
            from src.utils import settings as S

            enabled = S.auto_external_fallback_enabled()
        except Exception:  # noqa: BLE001
            enabled = False
        if not enabled:
            return False

        if find_external_player() == "":
            return False

        if self._current is None:
            return False
        url = self._current.external_url
        if not url:
            return False

        ok, message = open_externally(url)
        if ok:
            log.info("内置内核播放失败，已自动改用外部播放器: %s", url)
            self.statusMessage.emit(f"{title}——已自动改用外部播放器（{message}）")
            # 不弹错误框：已经交给外部播放器接手了
            self._awaiting_load = False
            self._load_timer.stop()
            return True
        # 外部播放器也没打开成功：不接管，照常报错
        self._external_fallback_attempted = False
        return False

    def _on_load_timeout(self) -> None:
        """超时兜底：后端一直没有给出任何有效状态，强制判定失败。"""
        if not self._awaiting_load:
            return

        # 诊断还在跑：先别急着报一个没有信息量的"超时"，
        # 顺延一小段去拿真实原因（只顺延一次，不会无限等待）。
        if self._diag_running and not self._timeout_extended:
            self._timeout_extended = True
            self._load_timer.start(_DIAG_GRACE_MS)
            log.info("加载超时，诊断尚未完成，顺延 %d ms", _DIAG_GRACE_MS)
            return

        kind = self._current.effective_kind if self._current else UrlKind.UNKNOWN
        self._fail(
            "加载超时",
            f"等待 {timeout_for(kind) / 1000:.0f} 秒后仍未开始播放。\n\n"
            f"地址：{self._current.play_url if self._current else ''}\n\n"
            "可能原因：地址不可达、需要请求头鉴权、格式不被支持，或该协议内置内核无法解析。"
            + self._protocol_hint()
            + self._diagnosis_suffix(),
        )

    def _on_end_of_media(self) -> None:
        if self._is_live:
            # 直播断流：重连而不是当作播放结束
            self.statusMessage.emit("直播流中断，3 秒后重连…")
            self._reconnect_timer.start(_RECONNECT_DELAY_MS)
            return
        self.endOfMedia.emit()
        self.statusMessage.emit("播放结束")

    def _reconnect(self) -> None:
        if self._current is None:
            return
        log.info("重连直播流: %s", self._current.play_url)
        self._start(self._current)

    # ------------------------------------------------------------ 资源释放
    def shutdown(self) -> None:
        self._load_timer.stop()
        self._reconnect_timer.stop()
        self._invalidate_resolve()
        S.settings().setValue(S.K_VOLUME, float(S.get_float(S.K_VOLUME, 0.8)))
        S.settings().sync()
        self._backend.release()
