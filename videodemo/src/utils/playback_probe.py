"""单地址播放能力探测（源码运行与打包运行共用）。

判定成功的标准（按可靠性排序）:
  1. 真的解出了画面帧（QVideoSink 收到 videoFrameChanged）——最硬的证据
  2. 点播流拿到 duration > 0
  3. 纯音频流播放位置往前推进

!! 不能用"是否有时长"判断成败 !!
直播流的 duration 恒为 0，Qt 官方 FFmpeg 后端解 rtsp/rtmp 时也拿不到时长，
若以此判定会把能正常播放的直播误报为失败。

!! 线程/事件循环约定 !!
本函数会**自己驱动事件循环**（`app.exec()`），因此必须在事件循环未运行、
且只有一个 QApplication/QGuiApplication 实例的时机调用。
进程内若已有实例（例如已创建 QApplication）会复用，不再新建。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

from src.utils.urls import to_playable_url

LogFn = Callable[[str], None]

#: 判定"确实出画面了"所需的最少帧数。取 5 是为了避开偶发的单帧抖动。
MIN_FRAMES = 5


@dataclass
class ProbeResult:
    """探测结果。code 为 0 表示成功。"""

    code: int = 1
    text: str = "无结果"
    frames: int = 0
    first_frame: str = ""
    max_position_ms: int = 0
    events: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def tag(self) -> str:
        return "成功" if self.ok else "失败"


def _noop(_msg: str) -> None:
    pass


def probe_url(
    url: str,
    timeout_s: int = 12,
    *,
    log: LogFn | None = None,
    app=None,
    min_frames: int = MIN_FRAMES,
) -> ProbeResult:
    """打开一个地址并判定能否播放。

    Args:
        url: 要测试的地址。
        timeout_s: 超时秒数。
        log: 日志回调（默认丢弃），用于把过程输出到终端或界面。
        app: 复用已有的 QApplication/QGuiApplication；为 None 时自动寻找或新建。
        min_frames: 判定成功所需的最少解码帧数。

    Returns:
        ProbeResult，code == 0 为成功。
    """
    from PySide6.QtCore import QCoreApplication, QTimer, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

    emit: LogFn = log or _noop

    qapp = app or QCoreApplication.instance()
    if qapp is None:
        qapp = QGuiApplication(sys.argv[:1])

    player = QMediaPlayer()
    sink = QVideoSink()
    player.setVideoSink(sink)

    result = ProbeResult()
    state = {"done": False, "max_pos": 0}

    def finish(code: int, text: str) -> None:
        if state["done"]:
            return
        state["done"] = True
        result.code = code
        result.text = text
        qapp.quit()

    def note(text: str) -> None:
        result.events.append(text)
        emit(text)

    def on_error(code, message) -> None:
        finish(2, f"错误 code={int(getattr(code, 'value', code))} message={message}")

    def on_frame(frame) -> None:
        if frame is None or not frame.isValid():
            return
        result.frames += 1
        if result.frames == 1:
            result.first_frame = f"{frame.width()}x{frame.height()}"
            note(f"  [video] 收到首帧 {result.first_frame}")

    def on_position(ms: int) -> None:
        state["max_pos"] = max(state["max_pos"], int(ms))
        result.max_position_ms = state["max_pos"]

    def on_status(status) -> None:
        name = str(status).split(".")[-1]
        note(f"  [status] {name}")
        if name in ("LoadedMedia", "BufferedMedia"):
            duration = player.duration()
            note(
                f"  [media] 时长={duration} ms  可跳转={player.isSeekable()}  "
                f"有视频={player.hasVideo()}"
            )
            if duration > 0:
                finish(0, f"成功（点播，时长 {duration} ms）")
        elif name == "EndOfMedia":
            finish(0, "成功（播放到结尾）")
        elif name == "InvalidMedia":
            finish(3, "媒体无效（InvalidMedia）")

    def check() -> None:
        if result.frames >= min_frames:
            finish(
                0,
                f"成功（{result.first_frame}，已解码 {result.frames} 帧画面）",
            )
        elif result.max_position_ms > 1500:
            finish(0, f"成功（纯音频流，播放位置推进到 {result.max_position_ms} ms）")

    player.errorOccurred.connect(on_error)
    player.mediaStatusChanged.connect(on_status)
    player.positionChanged.connect(on_position)
    sink.videoFrameChanged.connect(on_frame)

    note("  [info] 开始加载…")

    # 本地路径必须先规范化成 file:// URL，否则 QUrl 会把它当成非法的 scheme，
    # 报 "Could not open file"。主程序走 url_router 时已处理，这里不能漏。
    target = to_playable_url(url)
    if target != url:
        note(f"  [url] 已规范化: {target}")

    player.setSource(QUrl(target))
    player.play()

    ticker = QTimer()
    ticker.timeout.connect(check)
    ticker.start(500)

    QTimer.singleShot(timeout_s * 1000, lambda: finish(4, "超时：未能确认画面或播放进度"))

    qapp.exec()

    ticker.stop()
    player.stop()

    tag = "成功" if result.code == 0 else "失败"
    note(f"结果: {tag} —— {result.text}")
    return result
