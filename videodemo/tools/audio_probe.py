"""附加音轨诊断：把两个 QMediaPlayer 的内部状态逐拍打出来。

用法:
    python tools/audio_probe.py "<页面地址>" [--seconds 40] [--seek 20]

为什么要单独做这个工具:
    合流播放走的是"两个播放器实例"的路子（画面一个、声音一个），
    而"没有声音"这件事在外部**完全看不出来**——主播放器的画面、时长、
    位置全都正常。唯一的证据是两个实例各自的状态机，所以直接读它们的
    `playbackState` / `mediaStatus` / `error` / `position`。

判读:
    · 声音位置从某一刻起不再增长 → 声音那一路停了（seek 失败 / 卡在缓冲）
    · `音轨 active=False` 且无 failed 事件 → 被 `_on_extra_error` 静默吞掉了
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.utils.env_setup import setup_environment  # noqa: E402

setup_environment()

from src.utils.logger import get_logger, setup_logging  # noqa: E402

setup_logging()
_log = get_logger("audio_probe")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

QMessageBox.exec = lambda self: 0  # type: ignore[method-assign]

from src.ui.main_window import MainWindow  # noqa: E402


def _name(value) -> str:
    return getattr(value, "name", str(value))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--seconds", type=int, default=40)
    parser.add_argument("--seek", type=int, default=0, help="第 N 秒触发一次 seek")
    parser.add_argument(
        "--seek-to", type=int, default=600000,
        help="seek 的目标位置（毫秒），默认 10 分钟——那是最容易暴露 WebM 重定位开销的位置",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="打出 DEBUG 日志（含本地代理的上游连接耗时/分片记账），排查「加载慢」用",
    )
    args = parser.parse_args()

    if args.debug:
        # setup_logging() 在上面已经跑过（默认 INFO），这里直接抬根 logger 的档位
        logging.getLogger().setLevel(logging.DEBUG)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = MainWindow()
    window.resize(1200, 760)
    window.show()
    ctrl = window.controller
    backend = ctrl.backend

    try:
        main_player = backend._player       # noqa: SLF001
        extra_player = backend._extra_player  # noqa: SLF001
    except AttributeError:
        print("当前后端没有双播放器结构，本工具只适用于 Qt 后端")
        return 2

    t0 = [0.0]
    import time

    def stamp() -> str:
        if not t0[0]:
            t0[0] = time.monotonic()
        return f"{time.monotonic() - t0[0]:7.2f}s"

    def on_extra_status(status) -> None:
        print(f"[{stamp()}] 声音 mediaStatus -> {_name(status)}")

    def on_extra_state(state) -> None:
        print(f"[{stamp()}] 声音 playbackState -> {_name(state)}")

    def on_extra_error(code, text) -> None:
        print(f"[{stamp()}] 声音 error -> {int(code)} {text!r}")

    def on_main_status(status) -> None:
        print(f"[{stamp()}] 画面 mediaStatus -> {_name(status)}")

    extra_player.mediaStatusChanged.connect(on_extra_status)
    extra_player.playbackStateChanged.connect(on_extra_state)
    extra_player.errorOccurred.connect(on_extra_error)
    main_player.mediaStatusChanged.connect(on_main_status)
    ctrl.statusMessage.connect(lambda s: print(f"[{stamp()}] 状态 {s}"))
    backend.extraAudioFailed.connect(lambda r: print(f"[{stamp()}] 音轨失败 {r}"))

    history: list[tuple] = []

    def tick() -> None:
        mp = int(main_player.position())
        ap = int(extra_player.position())
        history.append((mp, ap))
        # 声音 position 连续几拍没动 = 声音那一路正在停着（重定位/缓冲）。
        # 这是"听起来一顿一顿"的直接证据：位置冻结的那段就是静音。
        frozen = 0
        for prev_mp, prev_ap in reversed(history[:-1]):
            if prev_ap == ap:
                frozen += 1
            else:
                break
        mark = f" 冻结x{frozen}" if frozen else ""
        print(
            f"[{stamp()}] 画面 pos={mp:>7} {_name(main_player.playbackState()):>13} "
            f"{_name(main_player.mediaStatus()):>13} | "
            f"声音 pos={ap:>7} {_name(extra_player.playbackState()):>13} "
            f"{_name(extra_player.mediaStatus()):>13} "
            f"err={_name(extra_player.error())} active={backend.extra_audio_active()} "
            f"偏差={mp - ap:>5}ms{mark}"
        )

    QTimer.singleShot(200, lambda: (
        window.address.setText(args.url),
        window._play_from_address(),  # noqa: SLF001
    ))

    ticker = QTimer()
    ticker.timeout.connect(tick)
    ticker.start(1000)

    if args.seek:
        QTimer.singleShot(
            args.seek * 1000,
            lambda: (
                print(f"[{stamp()}] >>> 触发 seek 到 {args.seek_to} ms"),
                ctrl.set_position(args.seek_to),
            ),
        )

    QTimer.singleShot(args.seconds * 1000, lambda: (print("--- 结束 ---"), app.quit()))
    app.exec()
    ticker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
