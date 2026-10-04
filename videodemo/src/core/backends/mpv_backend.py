"""基于 libmpv 的可选播放后端（P6）。

## 它解决 Qt 内核解决不了的两件事

1. **RTSP 只能走 UDP。** Qt 的 FFmpeg 后端把传输方式写死成 UDP，没有开关
   （见 `src/utils/env_setup.py` 的 `RTSP_TRANSPORT_NOTE`），网络屏蔽 UDP 时
   就是"一直加载不出画面"。libmpv 有 `rtsp-transport` 选项，可以强制 TCP。
2. **发不出自定义请求头。** `QMediaPlayer` 没有注入请求头的接口（防盗链地址
   只能靠 `src/core/local_proxy.py` 绕）。libmpv 支持 `http-header-fields`，
   可以直接带 Referer / User-Agent / Cookie。

此外 libmpv 的容错与重连普遍强于 Qt 的 FFmpeg 封装，处理"更苛刻的流媒体场景"
更有余地（可调 cache / demuxer 缓冲 / 超时）。

## 依赖与降级

需要 **python-mpv**（`pip install python-mpv`）**加上 libmpv 动态库**
（Windows 上是 `mpv-2.dll`）。两者缺一不可：

- 缺 python-mpv → `import mpv` 就失败；
- 有 python-mpv 但没有 `mpv-2.dll` → 构造时抛出 "Cannot find libmpv"。

两种情况的处理方式一样：**factory 捕获异常后回退到 Qt 后端**，界面照常可用。
所以这个后端是可选的，装了就用，没装不影响任何现有功能。

装好后可在「设置 → 播放内核」里选 mpv，或勾选"直播流优先用 mpv"。
"""

from __future__ import annotations

import importlib.util
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QVBoxLayout, QWidget

from src.core.backends.base import MediaStatus, PlaybackState, PlayerBackend
from src.utils.logger import get_logger

log = get_logger(__name__)

#: 位置/状态轮询间隔（毫秒）。
#:
#: 为什么用轮询而不是全靠 observe_property：libmpv 的属性回调跑在它自己的
#: 事件线程里，高频回调（time-pos 每秒几十次）要跨线程回 Qt 主线程，
#: 在拖动进度条、频繁换源时容易出现"回调晚于状态变更"的错序问题。
#: 位置这种"只要最新值"的数据用定时器读反而更简单可靠；
#: 真正不能漏的 eof / 错误仍走事件回调。
_POLL_INTERVAL_MS = 200

#: 允许通过环境变量指定 libmpv 动态库位置
MPV_DLL_ENV = "UNIVERSAL_PLAYER_MPV_DLL"

#: 用户可选的 RTSP 传输方式
RTSP_TRANSPORTS = ("tcp", "udp", "auto")


def _settings():
    from src.utils import settings as S

    return S


def find_mpv_dll() -> str:
    """找 libmpv 动态库。

    顺序：环境变量 → 设置里的路径 → 程序目录 → 空串（交给 python-mpv 自己找）。
    """
    env = os.environ.get(MPV_DLL_ENV, "").strip()
    if env and Path(env).is_file():
        return env

    try:
        from src.utils import settings as S

        configured = S.get_str(S.K_MPV_DLL, "").strip()
        if configured and Path(configured).is_file():
            return configured
    except Exception as exc:  # noqa: BLE001 - 配置读不到不该影响后端可用性
        log.debug("读取 mpv 库路径配置失败: %s", exc)

    from src.utils.paths import app_root, is_frozen

    # 便携用法：把 mpv-2.dll 放在 exe / 项目根目录旁边。
    # 打包后（onefile）binaries 被解压到 _MEIPASS，而 app_root() 指向的是
    # exe 所在目录（不是临时目录），所以 frozen 模式下还要额外查 _MEIPASS。
    for name in ("mpv-2.dll", "libmpv-2.dll", "libmpv.so.2", "libmpv.dylib"):
        candidates = [app_root() / name, app_root() / "mpv" / name]
        if is_frozen():
            import sys

            meipass = Path(getattr(sys, "_MEIPASS", app_root()))
            candidates += [meipass / name, meipass / "mpv" / name]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return ""


def prepare_dll_search_path() -> str:
    """把 libmpv 所在目录加进 `PATH`，返回库路径。

    **必须在 `import mpv` 之前调用，顺序不能颠倒。** python-mpv 是在
    *模块导入时*就去加载动态库的，找不到直接抛
    `OSError: Cannot find mpv-2.dll ...`；而它只认 `%PATH%`，
    不认识我们后面才通过 `libmpv=` 传进去的路径。

    踩过的坑：只做 `find_mpv_dll()` + `kwargs["libmpv"]=` 是**不够的**——
    库明明就在那儿，`import mpv` 照样失败，表现成"装了绑定却说未安装"。
    """
    dll = find_mpv_dll()
    if not dll:
        return ""
    folder = str(Path(dll).parent)
    entries = os.environ.get("PATH", "").split(os.pathsep)
    if folder not in entries:
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
    return dll


def binding_installed() -> bool:
    """python-mpv 这个绑定本身装了没有（不看动态库）。

    用 `find_spec` 而不是 `import`：后者会连带加载动态库，缺库时一样失败，
    于是"没装绑定"和"装了但缺库"两种完全不同的情况被混成一种，
    提示文案就只能含糊地说成"未安装 python-mpv"，把排查方向带偏。
    """
    try:
        return importlib.util.find_spec("mpv") is not None
    except (ImportError, ValueError):
        return False


def mpv_available() -> bool:
    """libmpv 后端是否可用（python-mpv 能 import 且动态库能加载）。"""
    prepare_dll_search_path()
    try:
        import mpv  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def describe() -> str:
    if mpv_available():
        dll = find_mpv_dll()
        where = dll or "由 python-mpv 自行定位"
        return f"libmpv 后端：可用（动态库：{where}）"
    if not binding_installed():
        return "libmpv 后端：不可用（未安装 python-mpv）"
    return "libmpv 后端：不可用（装了 python-mpv，但找不到 libmpv 动态库）"


INSTALL_HINT = (
    "libmpv 后端需要两部分，缺一不可：\n"
    "1. Python 绑定：\n"
    "   ./.venv/Scripts/python.exe -m pip install python-mpv "
    "-i https://mirrors.aliyun.com/pypi/simple\n"
    "2. libmpv 动态库（Windows 上是 libmpv-2.dll）：\n"
    "   从 https://github.com/shinchiro/mpv-winbuild-cmake/releases 下载\n"
    "   mpv-dev-x86_64-*.7z，解压出 libmpv-2.dll（约 115 MB），放到：\n"
    "     <程序目录>/libmpv-2.dll   或   <程序目录>/mpv/libmpv-2.dll\n"
    f"   也可以设置环境变量 {MPV_DLL_ENV} 直接指向该文件。\n"
    "   注意 sourceforge 的 libmpv 页面已不可用（实测 403），别再去那里找。\n"
    "（不装也能用：Qt 内核照常工作，只是 RTSP 只能走 UDP、发不出自定义请求头。）"
)


class MpvBackend(PlayerBackend):
    """libmpv 实现。构造失败时抛异常，由 factory 回退到 Qt 后端。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 先修 PATH 再 import —— python-mpv 在导入时就要 load 动态库
        prepare_dll_search_path()
        import mpv  # 延迟导入：没装时由 factory 捕获 ImportError

        # --- 承载画面的控件 ---
        # libmpv 直接把画面渲染进给定窗口句柄（wid），不需要视频 sink。
        self._container = QWidget()
        self._container.setStyleSheet("background-color: #000;")
        layout = QVBoxLayout(self._container)
        layout.setContentsMargins(0, 0, 0, 0)
        self._surface = QWidget()
        self._surface.setStyleSheet("background-color: #000;")
        layout.addWidget(self._surface)
        # 必须先有原生窗口句柄，才能交给 mpv
        self._wid = int(self._surface.winId())

        kwargs = {
            "wid": str(self._wid),
            # 按键交给 Qt 处理（我们的空格/方向键快捷键在窗口层）
            "input_default_bindings": False,
            "input_vo_keyboard": False,
            "osc": False,
            # 我们有自己的控制条，mpv 自带的不需要
            "keep_open": "yes",
            "hr_seek": "yes",
            "osd_level": 0,
            "terminal": False,
            # 注意写法：`msg-level` 的值是 `模块=级别`，直接写 "no" 会被当成
            # 模块名，libmpv 报 `Invalid value for mpv option`，**整个构造失败**。
            "msg_level": "all=no",
            # 网络容错：直播断流时自动重连，缓存给足
            "cache": "yes",
            "demuxer_max_bytes": "64MiB",
            "network_timeout": 20,
        }
        # 不传 `libmpv=`。它不是 mpv 的选项，python-mpv 会把未知关键字原样
        # 转给 libmpv，于是报 `mpv option does not exist: libmpv` 而整个构造失败。
        # 库位置已经由 prepare_dll_search_path() 加进 PATH —— 这才是 python-mpv
        # 认的方式（它导入时就是按 %PATH% 找库，给不给这个参数都一样）。
        self._player = mpv.MPV(**kwargs)

        self._mpv = mpv
        self._headers: dict[str, str] = {}
        self._url = ""
        self._audio_url = ""
        self._extra_active = False
        #: 是否还有一个"已记录但还没交给 mpv"的源
        self._pending_load = False
        self._duration = 0
        self._position = 0
        self._seekable = False
        self._ended = False
        self._eof_handled = False
        self._volume = 0.8
        self._muted = False
        self._rate = 1.0
        self._last_error = ""
        self._lock = threading.Lock()

        self._install_events()

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

        self.mediaStatusChanged.emit(MediaStatus.NO_MEDIA)
        log.info(
            "libmpv 后端已就绪（wid=%s, 库=%s）",
            self._wid,
            find_mpv_dll() or "由 PATH 定位",
        )

    # ------------------------------------------------------------ 事件
    def _install_events(self) -> None:
        """只订阅"不能漏"的事件；位置这类可轮询的走定时器。"""
        player = self._player

        @player.event_callback("end-file")
        def _on_end_file(event) -> None:  # noqa: ANN001
            data = getattr(event, "data", None)
            reason = str(getattr(data, "reason", "") or "")
            error = str(getattr(data, "error", "") or "")
            self._ended = True
            self._eof_handled = False
            if reason.lower().endswith("error") or error not in ("", "success"):
                self._report_error(error or f"播放结束（{reason}）")

        @player.event_callback("file-loaded")
        def _on_loaded(event) -> None:  # noqa: ANN001
            self._ended = False
            self._eof_handled = False
            self.mediaStatusChanged.emit(MediaStatus.LOADED)
            self._read_duration()
            if self._headers:
                # headers 只在加载生效，日志留个痕迹便于排查防盗链
                log.info("mpv 已注入请求头: %s", ", ".join(self._headers))
            self._sync()

        @player.event_callback("log-message")
        def _on_log(event) -> None:  # noqa: ANN001
            data = getattr(event, "data", None)
            text = str(getattr(data, "text", "") or "").strip()
            level = str(getattr(data, "level", "") or "")
            if text:
                log.debug("mpv[%s]: %s", level, text)

    def _report_error(self, message: str) -> None:
        self._last_error = message
        log.warning("mpv 播放错误: %s", message)
        self.mediaStatusChanged.emit(MediaStatus.INVALID)
        self.errorOccurred.emit(1, message)

    # ------------------------------------------------------------ 轮询
    def _poll(self) -> None:
        if not self._url:
            return
        try:
            position = float(self._player.time_pos or 0.0) * 1000.0
        except Exception:  # noqa: BLE001 - 属性在切换瞬间可能取不到
            position = 0.0

        if abs(position - self._position) >= 100:
            self._position = int(position)
            self.positionChanged.emit(self._position)

        self._read_duration()
        self._sync()

        # eof 只能从属性/事件拿（time_pos 停住不代表结束，暂停也一样）
        try:
            eof = bool(self._player.eof_reached)
        except Exception:  # noqa: BLE001
            eof = False
        if eof and not self._eof_handled:
            self._eof_handled = True
            self.mediaStatusChanged.emit(MediaStatus.END_OF_MEDIA)
            self.endOfMedia.emit()

    def _read_duration(self) -> None:
        try:
            duration = float(self._player.duration or 0.0) * 1000.0
        except Exception:  # noqa: BLE001
            return
        if abs(duration - self._duration) >= 500:
            self._duration = int(duration)
            self.durationChanged.emit(self._duration)

    def _sync(self) -> None:
        """同步播放状态与可跳转性。"""
        try:
            paused = bool(self._player.pause)
            idle = bool(self._player.core_idle)
        except Exception:  # noqa: BLE001
            return
        if idle:
            return
        state = PlaybackState.PAUSED if paused else PlaybackState.PLAYING
        self.playbackStateChanged.emit(state)

        try:
            seekable = bool(self._player.seekable)
        except Exception:  # noqa: BLE001
            seekable = self._duration > 0
        if seekable != self._seekable:
            self._seekable = seekable

    # ------------------------------------------------------------ 播放控制
    def set_source(self, url: str, headers: dict | None = None) -> None:
        """记录主源，但**先不 loadfile**——见 `_ensure_loaded` 的说明。"""
        self._url = (url or "").strip()
        self._headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self._position = 0
        self._duration = 0
        self._ended = False
        self._eof_handled = False
        self._last_error = ""
        # 新主源默认不带附加音轨；要挂的话控制器随后会调 set_audio_source
        self._audio_url = ""
        self._extra_active = False
        self._pending_load = bool(self._url)

        if not self._url:
            self.stop()
            return

        self.mediaStatusChanged.emit(MediaStatus.LOADING)

    def set_audio_source(self, url: str, headers: dict | None = None) -> None:
        """挂一路独立的音轨，与主源同时播放。

        mpv 的 `audio-file` 选项是**加载期**选项：必须在 loadfile 之前设好，
        两条流才会在同一次加载里一起开始。所以这里也走"先记录、等加载时统一应用"
        的路子（控制器正是 set_source → set_audio_source → play 的顺序）。

        如果主源已经加载完了（运行中补挂），退回 `audio-add` 命令。这条路
        音画起点可能对不齐（差几十到几百毫秒），但总比没有声音强。
        """
        target = (url or "").strip()
        self._audio_url = target
        self._extra_active = bool(target)

        if not target:
            self._detach_runtime_audio()
            return
        if headers and not self._headers:
            self._headers = {str(k): str(v) for k, v in headers.items()}
        if self._pending_load:
            return   # 正常路径：等 _ensure_loaded 一次性设好

        try:
            self._player.audio_add(target)
            log.info("mpv 运行中补挂音轨: %s", target)
        except Exception as exc:  # noqa: BLE001
            self._extra_active = False
            log.warning("mpv 补挂音轨失败: %s", exc)
            self.extraAudioFailed.emit(str(exc))

    def clear_audio_source(self) -> None:
        self._extra_active = False
        self._audio_url = ""
        if not self._pending_load:
            self._detach_runtime_audio()

    def _detach_runtime_audio(self) -> None:
        """把 audio-file 选项清空，后续加载就不会带上它。"""
        try:
            self._player["audio-file"] = ""
        except Exception as exc:  # noqa: BLE001
            log.debug("清空 mpv audio-file 失败: %s", exc)

    def supports_extra_audio(self) -> bool:
        # mpv 原生支持多音轨，且音量由它统一混音，不需要我们自己做同步
        return True

    def extra_audio_active(self) -> bool:
        return bool(self._extra_active)

    def _ensure_loaded(self) -> None:
        """把所有"加载期选项"一次性设好，再真正 loadfile。

        为什么要推迟到这一刻：请求头（`http-header-fields`）、附加音轨
        （`audio-file`）、RTSP 传输方式都是**加载期**选项，加载之后再设不生效。
        而控制器的调用顺序是 set_source → set_audio_source → play()，
        信息到 set_audio_source 才齐。所以把实际的 loadfile 放到 play() 前一步，
        才能保证所有选项都赶在加载之前到位。
        """
        if not self._pending_load or not self._url:
            return
        self._pending_load = False

        if self._headers:
            fields = [f"{name}: {value}" for name, value in self._headers.items()]
            try:
                self._player["http-header-fields"] = fields
            except Exception as exc:  # noqa: BLE001
                log.warning("设置 mpv 请求头失败: %s", exc)

        transport = _settings().get_str(_settings().K_RTSP_TRANSPORT, "udp").lower()
        if transport in RTSP_TRANSPORTS and transport != "auto":
            try:
                self._player["rtsp-transport"] = transport
            except Exception as exc:  # noqa: BLE001
                log.debug("设置 rtsp-transport 失败: %s", exc)

        if self._audio_url:
            try:
                self._player["audio-file"] = self._audio_url
                log.info("mpv 附加音轨已排入本次加载: %s", self._audio_url)
            except Exception as exc:  # noqa: BLE001
                self._extra_active = False
                log.warning("设置 mpv audio-file 失败: %s", exc)
                self.extraAudioFailed.emit(str(exc))

        self._apply_volume()
        self._player.loadfile(self._url)

    def play(self) -> None:
        self._ensure_loaded()
        try:
            self._player.pause = False
        except Exception as exc:  # noqa: BLE001
            log.warning("mpv play 失败: %s", exc)

    def pause(self) -> None:
        try:
            self._player.pause = True
        except Exception as exc:  # noqa: BLE001
            log.warning("mpv pause 失败: %s", exc)

    def stop(self) -> None:
        try:
            self._player.command("stop")
        except Exception as exc:  # noqa: BLE001
            log.debug("mpv stop 失败: %s", exc)
        self._url = ""
        self._pending_load = False
        self._audio_url = ""
        self._extra_active = False
        self._position = 0
        self.mediaStatusChanged.emit(MediaStatus.NO_MEDIA)
        self.playbackStateChanged.emit(PlaybackState.STOPPED)

    def set_position(self, ms: int) -> None:
        try:
            self._player.command("seek", float(ms) / 1000.0, "absolute")
        except Exception as exc:  # noqa: BLE001
            log.debug("mpv seek 失败: %s", exc)

    def position(self) -> int:
        return self._position

    def duration(self) -> int:
        return self._duration

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))
        self._apply_volume()

    def _apply_volume(self) -> None:
        try:
            # mpv 的音量是 0~100
            self._player.volume = self._volume * 100.0
        except Exception as exc:  # noqa: BLE001
            log.debug("mpv set_volume 失败: %s", exc)

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        try:
            self._player.mute = self._muted
        except Exception as exc:  # noqa: BLE001
            log.debug("mpv set_muted 失败: %s", exc)

    def set_rate(self, rate: float) -> None:
        self._rate = max(0.1, min(4.0, float(rate)))
        try:
            self._player.speed = self._rate
        except Exception as exc:  # noqa: BLE001
            log.debug("mpv set_rate 失败: %s", exc)

    # ------------------------------------------------------------ 画面与能力
    def video_widget(self) -> QWidget:
        return self._container

    def grab_frame(self) -> QImage | None:
        """截图：让 mpv 直接吐一帧 PNG 字节，再交给 Qt 解码。"""
        try:
            data = self._player.screenshot_raw()
        except Exception as exc:  # noqa: BLE001
            log.warning("mpv 截图失败: %s", exc)
            return None
        if not data:
            return None
        image = QImage()
        if image.loadFromData(bytes(data)):
            return image
        return None

    def is_seekable(self) -> bool:
        return self._seekable

    def capabilities(self) -> set[str]:
        return {
            "file", "http", "https", "hls", "rtsp", "rtsp-tcp", "rtmp",
            "local", "headers",
        }

    def release(self) -> None:
        self._timer.stop()
        try:
            self._player.terminate()
        except Exception as exc:  # noqa: BLE001
            log.debug("mpv terminate 失败: %s", exc)
        try:
            self._container.deleteLater()
        except Exception:  # noqa: BLE001
            pass


def probe_dll() -> tuple[bool, str]:
    """尝试真正加载一次 libmpv，返回 (是否成功, 说明)。

    只判断"python-mpv 能 import"是不够的：它还依赖外部的 mpv-2.dll，
    那个库缺失时 import 成功、构造时才炸。自检里用这个把真实情况暴露出来。
    """
    if not binding_installed():
        return False, "未安装 python-mpv"
    if not mpv_available():
        return False, "装了 python-mpv，但找不到 libmpv 动态库（mpv-2.dll）"

    dll = find_mpv_dll()
    started = time.time()
    try:
        import mpv

        # 不要传 libmpv=（原因见 MpvBackend.__init__ 的注释）
        player = mpv.MPV(vo="null", ao="null", terminal=False, msg_level="all=no")
        player.terminate()
    except Exception as exc:  # noqa: BLE001
        return False, f"libmpv 加载失败：{exc}"

    elapsed = (time.time() - started) * 1000
    return True, f"libmpv 可用（{elapsed:.0f} ms，库：{dll or '默认'}）"
