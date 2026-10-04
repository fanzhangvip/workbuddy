"""播放后端抽象层。

UI 层只依赖 PlayerBackend 这套接口与信号，不直接触碰 QMediaPlayer。
这样后续引入 libmpv 等其它内核时，只需新增一个实现文件并在 factory 里注册，
UI 与控制器代码零改动。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QWidget


class PlayerBackend(QObject):
    """播放内核统一接口。"""

    # --- 统一信号（与具体内核无关） ---
    positionChanged = Signal(int)          # 当前播放位置，毫秒
    durationChanged = Signal(int)          # 媒体总时长，毫秒；直播为 0
    playbackStateChanged = Signal(int)     # 0=停止 1=播放 2=暂停
    mediaStatusChanged = Signal(int)       # 内核无关的状态码，见 MediaStatus
    bufferProgressChanged = Signal(float)  # 缓冲进度 0.0 ~ 1.0
    errorOccurred = Signal(int, str)       # 错误码, 错误描述
    endOfMedia = Signal()                  # 播放到结尾
    #: 附加音轨（DASH 分离流里的"声音"那一路）失败。
    #: 画面不受影响、只是没有声音，所以这是提示而非错误，上层据此降级为"仅画面"。
    extraAudioFailed = Signal(str)
    #: 附加音轨还挂着，但音画已经对不上了（校正之后声音没能继续播放）。
    #: 与 extraAudioFailed 的区别：这一路并没有"失败"，只是不再自动校正，
    #: 上层据此提醒用户"可能仍然听不到声音"，而不是断言已降级。
    extraAudioStalled = Signal(str)

    # --- 生命周期与播放控制 ---
    def set_source(self, url: str, headers: dict | None = None) -> None:
        """设置播放源。headers 用于需要自定义请求头的场景（Qt 后端不支持）。"""
        raise NotImplementedError

    def play(self) -> None:
        raise NotImplementedError

    def pause(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def set_position(self, ms: int) -> None:
        raise NotImplementedError

    def position(self) -> int:
        raise NotImplementedError

    def duration(self) -> int:
        raise NotImplementedError

    def set_volume(self, volume: float) -> None:
        """音量，0.0 ~ 1.0。"""
        raise NotImplementedError

    def set_muted(self, muted: bool) -> None:
        raise NotImplementedError

    def set_rate(self, rate: float) -> None:
        """播放倍速。"""
        raise NotImplementedError

    # --- 附加音轨：应对站点的 DASH 分离流（画面一条、声音一条）---
    def supports_extra_audio(self) -> bool:
        """本后端能否在主源之外再挂一路纯音频，与主源同时播放。

        播放内核一次只能打开一条地址，所以"DASH 分离流"没法直接播。
        可行的办法是再开一路专门播声音，由后端起保持两路同步。
        原生支持多流的内核（如 mpv）返回 True 并给出等价实现。
        """
        return False

    def set_audio_source(self, url: str, headers: dict | None = None) -> None:
        """在主源之外挂一路纯音频，与主源同时播放。

        必须在 ``play()`` **之前**调用：两路在同一次播放指令里启动，音画才对得齐；
        错开启动会让偏差达到上百毫秒。后端不支持时忽略即可。
        """
        pass

    def clear_audio_source(self) -> None:
        """拆掉附加音轨（换源、停止时调用）。后端不支持时忽略即可。"""
        pass

    def extra_audio_active(self) -> bool:
        """当前是否真的有一路附加音轨在播（供上层判断"有没有声音"）。"""
        return False

    # --- 画面与能力 ---
    def video_widget(self) -> QWidget:
        """返回承载视频画面的 Qt 控件。"""
        raise NotImplementedError

    def grab_frame(self) -> QImage | None:
        """抓取当前画面用于截图；无画面时返回 None。"""
        raise NotImplementedError

    def is_seekable(self) -> bool:
        """当前媒体是否可拖动进度（直播通常不可）。"""
        return False

    def capabilities(self) -> set[str]:
        """本后端支持的协议集合，用于能力探测与降级决策。"""
        return set()

    def release(self) -> None:
        """释放资源。"""
        pass


class MediaStatus:
    """内核无关的媒体状态码，供 UI 使用。"""

    NO_MEDIA = 0
    LOADING = 1
    LOADED = 2
    STALLED = 3
    BUFFERING = 4
    BUFFERED = 5
    END_OF_MEDIA = 6
    INVALID = 7


class PlaybackState:
    STOPPED = 0
    PLAYING = 1
    PAUSED = 2
