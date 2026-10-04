"""基于 PySide6 QtMultimedia 的播放后端。

Qt6 起 QtMultimedia 默认使用 FFmpeg 后端，PySide6 的 wheel 自带 FFmpeg 库，
因此无需额外安装 VLC/mpv 即可解码 mp4/mkv/flv/HLS 等常见格式。

已知限制:
- 无法为请求注入自定义 HTTP 头（如 Referer），防盗链地址需要本地代理或 mpv 后端。
- 官方分发的 FFmpeg 未编译 rtsp 协议，rtsp:// 实际很可能打不开。

关于 DASH 分离流（B站这类站点只给"画面一条 + 声音一条"）:
QMediaPlayer 一次只能打开一条地址，所以这里用**两个播放器实例**合流——
主实例播画面，附加实例播声音，再以画面为主时钟做同步校正。
实测两路在同一次播放指令里启动时，音画偏差只有十几毫秒；
seek 后会短暂拉开到一两百毫秒，由 `_sync_tick` 校正回来。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QImage, QPalette
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget

from src.core.backends.base import MediaStatus, PlaybackState, PlayerBackend
from src.utils.logger import get_logger

log = get_logger(__name__)

# QMediaPlayer.MediaStatus -> 内核无关状态码
_STATUS_MAP = {
    QMediaPlayer.MediaStatus.NoMedia: MediaStatus.NO_MEDIA,
    QMediaPlayer.MediaStatus.LoadingMedia: MediaStatus.LOADING,
    QMediaPlayer.MediaStatus.LoadedMedia: MediaStatus.LOADED,
    QMediaPlayer.MediaStatus.StalledMedia: MediaStatus.STALLED,
    QMediaPlayer.MediaStatus.BufferingMedia: MediaStatus.BUFFERING,
    QMediaPlayer.MediaStatus.BufferedMedia: MediaStatus.BUFFERED,
    QMediaPlayer.MediaStatus.EndOfMedia: MediaStatus.END_OF_MEDIA,
    QMediaPlayer.MediaStatus.InvalidMedia: MediaStatus.INVALID,
}

_PLAYBACK_MAP = {
    QMediaPlayer.PlaybackState.StoppedState: PlaybackState.STOPPED,
    QMediaPlayer.PlaybackState.PlayingState: PlaybackState.PLAYING,
    QMediaPlayer.PlaybackState.PausedState: PlaybackState.PAUSED,
}

#: 音画偏差容忍值（毫秒）。**这个值必须明显大于"两路各自起播"的固有差**。
#:
#: 实测（YouTube DASH 分离流 + 本地反向代理，两条流来自两个播放器实例）：
#: 两路从同一次 play() 启动，声音那一路仍然稳定落后 **270~600ms** —— 那是
#: 音频管线起播/缓冲的固有差，每次运行数值不同，但同一次运行内**恒定不动**
#: （关掉自动校正连测 30 秒：偏差一直钉在 372/392ms，另一次全程 592/272ms）。
#:
#: 于是这里有个反直觉的结论：**把阈值定得比固有差小，是在帮倒忙。**
#: 校正的办法是把声音 seek 到画面位置，而它要重新定位（经代理实测每个上游
#: 请求 ~0.5s，WebM 还要找 cluster，合计 0.5~2s），这期间声音的 position
#: 停在原地不动 —— 于是校正**自己**制造出一次更大的偏差。实测：校正前
#: 偏差 452ms，校正后变成 992ms；之后每 3 秒再来一刀，偏差在 267ms↔1225ms
#: 之间来回摆。用户听到的是**每 3 秒跳一下**，比不校正（恒定 400ms 左右、
#: 完全不跳）差得多。
#:
#: 所以取 1200ms：固有差（≤600ms）留出两倍余量，正常播放一次都不校正；
#: 只有真的卡顿/落后（缓冲欠载这类，偏差会到秒级）才出手。校正是兜底。
_SYNC_TOLERANCE_MS = 1200
#: 两次校正之间的最小间隔（毫秒）：音频 seek 有轻微断续感，不能频繁做。
_SYNC_MIN_INTERVAL_MS = 3000
#: 校正间隔的上限（毫秒）。见 `_sync_interval_ms` 的说明。
_SYNC_MAX_INTERVAL_MS = 30000
#: 判定"这一刀有没有用"的门槛（毫秒）：校正生效后的新偏差比校正前只小这么点，
#: 就算没帮上忙（多半是重新定位的开销吃掉了收益）。
_SYNC_IMPROVE_MS = 200
#: seek 之后的静默期（毫秒）：位置数据还没稳定，此时不该判定"不同步"。
_SYNC_SETTLE_MS = 1500
#: 同步检查间隔（毫秒）。
_SYNC_CHECK_MS = 500
#: 判定"这一刀还没生效"的余量（毫秒）：声音 position 离 seek 目标还不到这个
#: 距离，就认为它还停在原地。**必须比 `_SYNC_TOLERANCE_MS` 小得多**——
#: 否则"偏差 400ms"会被误判成"seek 没生效"，白白等超时（这条是我把阈值
#: 调大时踩出来的：两件事共用一个常量，语义却是反的）。
_SYNC_PENDING_EPS_MS = 120
#: 一次校正最多等多久还不见效就当它废了（毫秒）。
#:
#: **这是防死循环的关键。** 校正的方式是"把声音 seek 到画面位置"，而源站
#: 上的流（尤其 YouTube 的 WebM）重新定位可能要好几秒：这期间声音的 position
#: 会**停在 seek 目标上不动**。如果同步逻辑按"偏差还是很大"就再 seek 一次，
#: 就会在上一刀还没生效时又补一刀，声音被永远按在原地——用户听到的是
#: **整个视频都完全没有声音**。所以：校正未生效前绝不重复校正；
#: 超过这个时长还不动，就停掉自动校正并如实告诉用户。
_SYNC_STUCK_TIMEOUT_MS = 8000


def _enum_value(value) -> int:
    """把 PySide6 的枚举安全地转成 int（跨版本兼容）。"""
    raw = getattr(value, "value", value)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


class QtMediaBackend(PlayerBackend):
    """QMediaPlayer + QAudioOutput + QVideoWidget 实现。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._player = QMediaPlayer(self)
        # Qt6 必须显式创建 QAudioOutput 并挂到 player 上，否则没有声音
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)

        self._video = QVideoWidget()
        self._video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        # 视频未铺满的区域显示为黑色（播放器的常规观感）。
        # 只设 stylesheet 不够：QVideoWidget 走原生绘制路径，需要同时设调色板。
        palette = self._video.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0))
        palette.setColor(QPalette.ColorRole.Base, QColor(0, 0, 0))
        self._video.setPalette(palette)
        self._video.setAutoFillBackground(True)
        self._video.setStyleSheet("background-color: #000;")
        self._player.setVideoOutput(self._video)

        # 截图用的最后一帧缓存
        self._last_frame: QImage | None = None
        self._video_sink = None
        sink_getter = getattr(self._video, "videoSink", None)
        if callable(sink_getter):
            self._video_sink = sink_getter()
            if self._video_sink is not None:
                self._video_sink.videoFrameChanged.connect(self._on_frame)

        self._connect_signals()

        # ---- 附加音轨：站点的 DASH 分离流靠"再开一路播声音"合流 ----
        self._extra_player = QMediaPlayer(self)
        self._extra_audio = QAudioOutput(self)
        self._extra_player.setAudioOutput(self._extra_audio)
        self._extra_player.errorOccurred.connect(self._on_extra_error)
        self._extra_player.mediaStatusChanged.connect(self._on_extra_status)
        #: 附加音轨是否已挂上
        self._extra_active = False
        #: 附加音轨是否已真的加载成功（用来区分可自愈错误与致命错误）
        self._extra_loaded = False
        #: 音量/静音要自己记着，新挂上的音轨才沿用得上
        self._volume = 1.0
        self._muted = False

        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(_SYNC_CHECK_MS)
        self._sync_timer.timeout.connect(self._sync_tick)
        self._sync_hold_until = 0.0
        self._last_sync_at = 0.0
        #: 上一次校正把声音 seek 到了哪里（None = 没有待生效的校正）
        self._pending_target: int | None = None
        #: 那次校正是什么时候发出的
        self._pending_since = 0.0
        #: 发出那一刀时的偏差（用来判断这一刀到底有没有帮上忙）
        self._pending_drift: int | None = None
        #: 当前允许的两次校正间隔。远距离 seek 之后一次校正可能白费（声音
        #: 重新定位的开销把收益吃掉了），此时拉长间隔——宁可让偏差留着，
        #: 也不要刀刀相连把声音切碎。
        self._sync_interval_ms = _SYNC_MIN_INTERVAL_MS

    # ------------------------------------------------------------ 信号桥接
    def _connect_signals(self) -> None:
        p = self._player
        p.positionChanged.connect(self._on_position)
        p.durationChanged.connect(self._on_duration)
        p.playbackStateChanged.connect(self._on_playback_state)
        p.mediaStatusChanged.connect(self._on_media_status)
        p.errorOccurred.connect(self._on_error)
        p.bufferProgressChanged.connect(self._on_buffer_progress)

    def _on_position(self, ms: int) -> None:
        self.positionChanged.emit(int(ms))

    def _on_duration(self, ms: int) -> None:
        self.durationChanged.emit(int(ms))

    def _on_playback_state(self, state) -> None:
        self.playbackStateChanged.emit(_PLAYBACK_MAP.get(state, PlaybackState.STOPPED))

    def _on_media_status(self, status) -> None:
        self.mediaStatusChanged.emit(_STATUS_MAP.get(status, MediaStatus.NO_MEDIA))
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.endOfMedia.emit()

    def _on_error(self, error, error_string: str) -> None:
        log.error("QMediaPlayer 错误 code=%s: %s", _enum_value(error), error_string)
        self.errorOccurred.emit(_enum_value(error), error_string or "未知错误")

    def _on_buffer_progress(self, progress: float) -> None:
        self.bufferProgressChanged.emit(float(progress))

    def _on_frame(self, frame) -> None:
        if frame is not None and frame.isValid():
            self._last_frame = frame.toImage()

    # ------------------------------------------------------------ 播放控制
    def set_source(self, url: str, headers: dict | None = None) -> None:
        if headers:
            # QMediaPlayer 没有注入请求头的接口，这里明确记录，由上层决定降级策略
            log.warning(
                "Qt 后端无法注入 HTTP 请求头（需要 %s），该源可能因防盗链失败",
                ", ".join(headers.keys()),
            )
        self._last_frame = None
        self._player.setSource(QUrl(url))

    def play(self) -> None:
        self._player.play()
        if self._extra_active:
            # 关键：两路必须在同一轮事件循环里启动，偏差才只有十几毫秒。
            self._extra_player.play()

    def pause(self) -> None:
        self._player.pause()
        if self._extra_active:
            self._extra_player.pause()

    def stop(self) -> None:
        self._player.stop()
        if self._extra_active:
            self._extra_player.stop()

    def set_position(self, ms: int) -> None:
        self._player.setPosition(int(ms))
        if self._extra_active:
            self._seek_extra(int(ms))

    def _seek_extra(self, target_ms: int, drift_ms: int | None = None) -> None:
        """把声音移到指定位置，并记下"这一刀还没生效"。"""
        self._extra_player.setPosition(int(target_ms))
        self._pending_target = int(target_ms)
        self._pending_since = time.monotonic()
        self._pending_drift = None if drift_ms is None else abs(int(drift_ms))
        # 刚 seek 完位置数据还没稳定，先别急着判定"不同步"
        self._sync_hold_until = time.monotonic() + _SYNC_SETTLE_MS / 1000

    def position(self) -> int:
        return int(self._player.position())

    def duration(self) -> int:
        return int(self._player.duration())

    def set_volume(self, volume: float) -> None:
        value = max(0.0, min(1.0, float(volume)))
        self._volume = value
        self._audio.setVolume(value)
        self._extra_audio.setVolume(value)

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self._audio.setMuted(self._muted)
        self._extra_audio.setMuted(self._muted)

    def set_rate(self, rate: float) -> None:
        self._player.setPlaybackRate(float(rate))
        if self._extra_active:
            self._extra_player.setPlaybackRate(float(rate))

    # ---------------------------------------- 附加音轨（DASH 分离流合流）
    def supports_extra_audio(self) -> bool:
        return True

    def set_audio_source(self, url: str, headers: dict | None = None) -> None:
        if headers:
            log.warning(
                "Qt 后端无法为附加音轨注入请求头（需要 %s）",
                ", ".join(headers.keys()),
            )
        self._extra_loaded = False
        self._extra_active = bool(url)
        self._pending_target = None
        self._pending_drift = None
        # 新的一条音轨重新给机会：上一条流退化出来的退避不该跟着过来
        self._sync_interval_ms = _SYNC_MIN_INTERVAL_MS
        self._extra_player.setSource(QUrl(url) if url else QUrl())
        # 音量/静音挂在 QAudioOutput 上，QMediaPlayer 没有这两个方法
        self._extra_audio.setVolume(self._volume)
        self._extra_audio.setMuted(self._muted)
        if not url:
            self._sync_timer.stop()
            return

        self._sync_hold_until = time.monotonic() + _SYNC_SETTLE_MS / 1000
        self._last_sync_at = 0.0
        self._sync_timer.start()
        # 主源已经在播的情况下（补挂音轨）立即跟上，免得只有画面没声音
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._extra_player.play()

    def clear_audio_source(self) -> None:
        self._extra_active = False
        self._extra_loaded = False
        self._pending_target = None
        self._pending_drift = None
        self._sync_timer.stop()
        self._extra_player.stop()
        self._extra_player.setSource(QUrl())

    def extra_audio_active(self) -> bool:
        return self._extra_active and self._extra_loaded

    def _on_extra_status(self, status) -> None:
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferingMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            self._extra_loaded = True

    def _on_extra_error(self, error, error_string: str) -> None:
        log.warning("附加音轨错误 code=%s: %s", _enum_value(error), error_string)
        if self._extra_loaded:
            # 与主源同理：Qt 在 LoadedMedia 之后会补发一次可恢复的
            # Demuxing failed，此时声音其实正常，不能据此放弃音轨。
            return
        self._extra_active = False
        self._sync_timer.stop()
        self.extraAudioFailed.emit(error_string or "声音流加载失败")

    def _sync_tick(self) -> None:
        """把声音对齐到画面。

        以**画面为主时钟**：视频 seek 会看到明显跳帧，而音频 seek 几乎察觉不到，
        所以偏差一律靠调整音频来消除。

        但**校正是兜底，不是常规操作**（阈值见 `_SYNC_TOLERANCE_MS` 的说明）：
        实测把声音 seek 过去的代价是 0.5~2s 的无声停顿，比它修掉的那点偏差更
        伤听感，而且停顿本身又会被读成"偏差"，一轮轮校正下去就是"每 3 秒跳一下"。

        所以这里有两条闸：
        · **一次只允许有一刀在途**：校正未生效前绝不补刀（否则声音会被按死在
          seek 目标上，表现为全程无声）；
        · **一刀没帮上忙就拉长间隔**：远距离 seek 之后收益常常被重定位开销吃掉，
          此时指数退避（3s → 6s → 12s → … → 30s），把跳音频率压到最低。
        """
        if not self._extra_active:
            return
        if self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            return

        now = time.monotonic()
        if now < self._sync_hold_until:
            return

        audio_pos = int(self._extra_player.position())

        if self._pending_target is not None:
            if abs(audio_pos - self._pending_target) <= _SYNC_PENDING_EPS_MS:
                # 还停在 seek 目标上 → 这一刀没生效（多半还在重新定位）
                if now - self._pending_since > _SYNC_STUCK_TIMEOUT_MS / 1000:
                    elapsed = now - self._pending_since
                    log.warning(
                        "声音在校正后 %.1fs 仍未继续播放（目标 %d ms），"
                        "停止自动校正，避免反复 seek 把声音按死",
                        elapsed,
                        self._pending_target,
                    )
                    self._pending_target = None
                    self._pending_drift = None
                    self._sync_timer.stop()
                    self.extraAudioStalled.emit(
                        "声音比画面落后较多，且校正后没有恢复播放"
                    )
                return
            # 动起来了 → 校正生效。顺手结账：这一刀到底有没有把偏差压下去？
            self._settle_correction(int(self._player.position()), audio_pos)
            self._pending_target = None
            self._pending_drift = None

        if now - self._last_sync_at < self._sync_interval_ms / 1000:
            return

        video_pos = int(self._player.position())
        # 两边都还没真正播起来时读数不可信（刚加载完常常都是 0）
        if video_pos <= 0 or audio_pos <= 0:
            return

        drift = video_pos - audio_pos
        if abs(drift) <= _SYNC_TOLERANCE_MS:
            # 已经够齐了：把间隔收回基准，下次真有需要时能立刻出手
            self._sync_interval_ms = _SYNC_MIN_INTERVAL_MS
            return

        self._last_sync_at = now
        log.info(
            "音画偏差 %d ms，把声音对齐到画面 %d ms（间隔 %d ms）",
            drift,
            video_pos,
            self._sync_interval_ms,
        )
        self._seek_extra(video_pos, drift)

    def _settle_correction(self, video_pos: int, audio_pos: int) -> None:
        """给刚生效的那一刀结账：改善了就恢复基准间隔，没改善就退避。"""
        if self._pending_drift is None:
            return
        # 读数还没稳定（刚 seek 完常常两边都是 0）时不结账：那会把"没数据"
        # 误判成"没改善"，凭一次坏读数就把间隔放大。
        if video_pos <= 0 or audio_pos <= 0:
            return
        remaining = abs(video_pos - audio_pos)
        if remaining >= self._pending_drift - _SYNC_IMPROVE_MS:
            self._sync_interval_ms = min(self._sync_interval_ms * 2, _SYNC_MAX_INTERVAL_MS)
            log.info(
                "上次校正没把偏差压下去（%d ms -> %d ms），"
                "下次校正间隔放宽到 %d ms",
                self._pending_drift,
                remaining,
                self._sync_interval_ms,
            )
        else:
            self._sync_interval_ms = _SYNC_MIN_INTERVAL_MS

    # ------------------------------------------------------------ 画面与能力
    def video_widget(self) -> QVideoWidget:
        return self._video

    def grab_frame(self) -> QImage | None:
        if self._last_frame is not None and not self._last_frame.isNull():
            return self._last_frame
        # 兜底：直接抓控件画面（部分渲染路径下可能拿到黑图）
        pixmap = self._video.grab()
        if not pixmap.isNull():
            return pixmap.toImage()
        return None

    def is_seekable(self) -> bool:
        try:
            return bool(self._player.isSeekable())
        except AttributeError:
            return self.duration() > 0

    def capabilities(self) -> set[str]:
        return {"file", "http", "https", "hls", "local"}

    def release(self) -> None:
        self.clear_audio_source()
        self._player.stop()
        self._player.setSource(QUrl())
        if self._video_sink is not None:
            try:
                self._video_sink.videoFrameChanged.disconnect(self._on_frame)
            except (RuntimeError, TypeError):
                pass
