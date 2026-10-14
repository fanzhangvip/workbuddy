"""播放控制条：进度、播放控制、音量、倍速、截图、全屏。

## 布局为什么是两行

原来所有东西挤在**一行**里：4 个按钮 + 进度条 + 时间 + 静音 + 音量条 +
倍速 + 3 个按钮，一共 11 个控件。窗口一窄，进度条（唯一能伸缩的那个）
就被压成几十像素，拖动定位基本没法用；与此同时按钮还在按各自主的最小宽度
撑着，最后整行溢出。

改成两行之后各得其所：

- **上行**只放"观看时一直要用的"：进度条 + 时间 + 音量。进度条独占剩余宽度，
  永远不会被按钮挤扁。
- **下行**放"偶尔点一下的"：播放控制按钮在左，倍速/截图/外部播放/全屏在右。
  中间留伸缩空间，两组各归各位，不会飘。

这样调还有一个副作用是好的：上行那三样东西都是"当前状态"，下行都是"动作"，
按行为分了组，比原来一串平铺更好找。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from src.core.backends.base import PlaybackState

SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)

#: 窄窗口下的收缩档位：宽度低于阈值时收起若干控件。
#: 顺序按重要性从低到高——先收「倍速 → 截图 → 外部打开」，
#: **全屏永远留着**（窄屏用户更常用它来换取画面空间）。
_COMPACT_STEPS = (
    # (低于此宽度, 允许显示的尾部控件数)
    (760, 1),
    (640, 2),
    (0, 4),
)


def format_ms(ms: int) -> str:
    """把毫秒格式化成 HH:MM:SS 或 MM:SS。"""
    if ms is None or ms < 0:
        ms = 0
    total = int(ms) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


class ControlBar(QWidget):
    """底部控制条。"""

    playPauseClicked = Signal()
    stopClicked = Signal()
    prevClicked = Signal()
    nextClicked = Signal()
    seekRequested = Signal(int)          # 目标位置，毫秒
    seekStepRequested = Signal(int)      # 相对跳转，毫秒
    volumeChanged = Signal(float)        # 0.0 ~ 1.0
    mutedToggled = Signal(bool)
    rateChanged = Signal(float)
    snapshotClicked = Signal()
    openExternalClicked = Signal()
    fullscreenClicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._duration = 0
        self._dragging = False
        self._live = False
        self._muted = False
        #: 方向键快进/快退步长（毫秒），可在设置里改
        self._seek_step_ms = 5000

        # ---------------------------------------------------------- 播放控制
        self.btn_prev = self._button("上一", "播放播放列表中的上一条")
        self.btn_play = self._button("播放", "播放 / 暂停（空格）")
        self.btn_stop = self._button("停止", "停止播放")
        self.btn_next = self._button("下一", "播放播放列表中的下一条")
        # 播放列表为空时置灰，由主窗口通过 set_nav_enabled 控制
        self.btn_prev.setEnabled(False)
        self.btn_next.setEnabled(False)
        # 「播放」是这条里唯一的主按钮，给一点强调色的底
        self.btn_play.setObjectName("primaryButton")

        # ---------------------------------------------------------- 进度
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setToolTip("拖动可跳转播放位置")
        self.slider.setMinimumWidth(120)
        self.label_time = QLabel("00:00 / 00:00")
        # 112 是 `00:00:00 / 00:00:00` 的实际宽度。设死最小宽度而不是让它
        # 随文字变化：否则每次时间跨过 10 分钟，整行控件都会横向抖一下。
        self.label_time.setMinimumWidth(112)
        self.label_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_time.setObjectName("timeLabel")

        # ---------------------------------------------------------- 音量
        self.btn_mute = self._button("音量", "静音 / 取消静音")
        self.slider_volume = QSlider(Qt.Orientation.Horizontal)
        self.slider_volume.setRange(0, 100)
        self.slider_volume.setValue(80)
        self.slider_volume.setFixedWidth(92)
        self.slider_volume.setToolTip("音量")

        # ---------------------------------------------------------- 倍速
        self.combo_rate = QComboBox()
        for speed in SPEEDS:
            self.combo_rate.addItem(f"{speed:g}x", speed)
        self.combo_rate.setFixedWidth(72)
        self.combo_rate.setToolTip("播放速度")

        # ---------------------------------------------------------- 其它
        self.btn_snapshot = self._button("截图", "把当前画面保存为 PNG")
        self.btn_external = self._button("外部打开", "用系统上的播放器打开当前地址")
        self.btn_fullscreen = self._button("全屏", "全屏 / 退出全屏（Esc 退出）")

        # ---------------------------------------------------------- 组装
        # 上行：进度 + 时间 +（弹簧）+ 音量。三样都是"当前状态"。
        upper = QHBoxLayout()
        upper.setContentsMargins(0, 0, 0, 0)
        upper.setSpacing(10)
        upper.addWidget(self.slider, 1)
        upper.addWidget(self.label_time)
        upper.addSpacing(6)
        upper.addWidget(self.btn_mute)
        upper.addWidget(self.slider_volume)

        # 下行：播放控制（左） / 伸缩 / 辅助动作（右）
        lower = QHBoxLayout()
        lower.setContentsMargins(0, 0, 0, 0)
        lower.setSpacing(6)
        lower.addWidget(self.btn_prev)
        lower.addWidget(self.btn_play)
        lower.addWidget(self.btn_stop)
        lower.addWidget(self.btn_next)
        lower.addStretch(1)
        lower.addWidget(self.combo_rate)
        lower.addWidget(self.btn_snapshot)
        lower.addWidget(self.btn_external)
        lower.addWidget(self.btn_fullscreen)

        # 收哪些控件由这份列表决定，顺序 = 从最先收到最后收（从右往左）
        self._tail_widgets = [
            self.btn_fullscreen, self.btn_external, self.btn_snapshot, self.combo_rate
        ]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(5)
        layout.addLayout(upper)
        layout.addLayout(lower)

        self._connect()

    # ------------------------------------------------------------ 内部
    def _button(self, text: str, tip: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setToolTip(tip)
        btn.setMinimumWidth(58)
        # 不参与键盘焦点：否则空格会"按下"按钮、方向键会被别处吞掉，
        # 导致窗口级的播放/跳转快捷键失效
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return btn

    def _connect(self) -> None:
        # 滑条与下拉框同理，不抢焦点，好让方向键用于快进快退
        for widget in (self.slider, self.slider_volume, self.combo_rate):
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.btn_play.clicked.connect(self.playPauseClicked)
        self.btn_stop.clicked.connect(self.stopClicked)
        self.btn_prev.clicked.connect(self.prevClicked)
        self.btn_next.clicked.connect(self.nextClicked)
        self.btn_snapshot.clicked.connect(self.snapshotClicked)
        self.btn_external.clicked.connect(self.openExternalClicked)
        self.btn_fullscreen.clicked.connect(self.fullscreenClicked)

        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.slider.sliderMoved.connect(self._on_slider_moved)

        self.slider_volume.valueChanged.connect(self._on_volume_changed)
        self.btn_mute.clicked.connect(self._on_mute_clicked)
        self.combo_rate.currentIndexChanged.connect(self._on_rate_changed)

    # ------------------------------------------------------------ 窄窗口收缩
    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """窄窗口下依次收起右侧的辅助控件，保证播放按钮与进度条完整。"""
        super().resizeEvent(event)
        width = event.size().width()
        # _COMPACT_STEPS 按"低于此宽度 → 允许显示的控件数"理解；
        # 从最严的档开始找第一条命中的规则，全都不命中（够宽）就全显示。
        allowed = _COMPACT_STEPS[-1][1]
        for threshold, count in _COMPACT_STEPS:
            if width < threshold:
                allowed = count
                break
        # 从最右往左数，留下 allowed 个
        for index, widget in enumerate(self._tail_widgets):
            widget.setVisible(index < allowed)

    def _on_slider_pressed(self) -> None:
        self._dragging = True

    def _on_slider_moved(self, value: int) -> None:
        # 拖动过程中只更新文字，不回写播放位置，避免抖动
        self.label_time.setText(f"{format_ms(value)} / {format_ms(self._duration)}")

    def _on_slider_released(self) -> None:
        self._dragging = False
        self.seekRequested.emit(self.slider.value())

    def _on_volume_changed(self, value: int) -> None:
        if value > 0 and self._muted:
            self._muted = False
            self.btn_mute.setText("音量")
            self.mutedToggled.emit(False)
        self.volumeChanged.emit(value / 100.0)

    def _on_mute_clicked(self) -> None:
        self._muted = not self._muted
        self.btn_mute.setText("已静音" if self._muted else "音量")
        self.mutedToggled.emit(self._muted)

    def _on_rate_changed(self, index: int) -> None:
        speed = self.combo_rate.itemData(index)
        if speed:
            self.rateChanged.emit(float(speed))

    # ------------------------------------------------------------ 对外接口
    def set_position(self, ms: int) -> None:
        if self._dragging or self._live:
            return
        self.slider.blockSignals(True)
        self.slider.setValue(int(ms))
        self.slider.blockSignals(False)
        self.label_time.setText(f"{format_ms(ms)} / {format_ms(self._duration)}")

    def set_duration(self, ms: int) -> None:
        self._duration = max(0, int(ms))
        self.slider.setRange(0, self._duration)
        self.label_time.setText(
            f"{format_ms(self.slider.value())} / {format_ms(self._duration)}"
        )

    def set_playback_state(self, state: int) -> None:
        if state == PlaybackState.PLAYING:
            self.btn_play.setText("暂停")
        else:
            self.btn_play.setText("播放")

    def set_live(self, live: bool) -> None:
        self._live = bool(live)
        if live:
            self.slider.setEnabled(False)
            self.slider.setRange(0, 0)
            self.label_time.setText("直播中")
        else:
            self.slider.setEnabled(True)

    def set_seekable(self, seekable: bool) -> None:
        self.slider.setEnabled(bool(seekable) and not self._live)

    def set_volume(self, volume: float) -> None:
        self.slider_volume.blockSignals(True)
        self.slider_volume.setValue(int(round(max(0.0, min(1.0, volume)) * 100)))
        self.slider_volume.blockSignals(False)

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self.btn_mute.setText("已静音" if muted else "音量")

    def set_rate(self, rate: float) -> None:
        index = self.combo_rate.findData(rate)
        if index < 0:
            index = self.combo_rate.findData(1.0)
        if index >= 0:
            self.combo_rate.blockSignals(True)
            self.combo_rate.setCurrentIndex(index)
            self.combo_rate.blockSignals(False)

    def set_fullscreen(self, is_fullscreen: bool) -> None:
        self.btn_fullscreen.setText("退出全屏" if is_fullscreen else "全屏")

    def set_nav_enabled(self, enabled: bool) -> None:
        """播放列表有内容时才允许「上一/下一」。

        禁用状态比"点了没反应"更能说明问题：用户一眼就知道是自己还没建列表，
        而不是按钮坏了。
        """
        self.btn_prev.setEnabled(bool(enabled))
        self.btn_next.setEnabled(bool(enabled))

    def seek_step_ms(self) -> int:
        """当前方向键快进/快退的步长（毫秒）。"""
        return self._seek_step_ms

    def set_seek_step_ms(self, step_ms: int) -> None:
        self._seek_step_ms = max(1000, int(step_ms))
