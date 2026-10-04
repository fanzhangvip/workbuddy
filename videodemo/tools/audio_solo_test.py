"""单独测「一路音频流自己能不能播」，以及 seek 的代价有多大。

用法:
    python tools/audio_solo_test.py "<页面地址>" [--via-proxy/--no-proxy]
                                     [--seek-at 12] [--seek-delta 30000] [--seconds 40]

为什么必须量 seek 的代价:
    "分离流合流"里声音比画面晚起 20 秒，同步逻辑就会去 seek 声音把它拉回来。
    如果这次 seek 要十几秒才能恢复（YouTube 的 WebM 没有索引，只能从头扫），
    而同步逻辑每 3 秒就再 seek 一次，声音就**永远起不来** —— 这就是"有画面没声音"。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.utils.env_setup import setup_environment  # noqa: E402

setup_environment()

from src.utils import settings as S  # noqa: E402
from src.utils.logger import (  # noqa: E402
    get_logger,
    install_qt_message_handler,
    setup_logging,
)

# DEBUG：把本地代理的每一次请求/响应耗时都打出来，排查"加载慢 / 卡住"全靠它
setup_logging(logging.DEBUG)
# Qt 会把 FFmpeg 自己的日志（"Opening ..."、seek、http 细节）转发进这里；
# 窗口模式下它们默认只打到 stderr，排查时等于没有线索。
install_qt_message_handler()
_log = get_logger("audio_solo_test")

from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def _name(value) -> str:
    return getattr(value, "name", str(value))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--seek-at", type=int, default=12, help="第 N 秒做 seek，0 表示不 seek")
    parser.add_argument("--seek-delta", type=int, default=30000, help="seek 的相对位移（毫秒）")
    parser.add_argument("--seek-absolute", type=int, default=-1, help="改成绝对位置（毫秒）")
    parser.add_argument("--seconds", type=int, default=40)
    parser.add_argument("--no-proxy", action="store_true", help="不走本地反向代理")
    parser.add_argument(
        "--direct-url",
        default="",
        help="直接给一条媒体地址，跳过解析（配合 dump_audio_formats 用）",
    )
    parser.add_argument(
        "--no-env-proxy",
        action="store_true",
        help="把 HTTP(S)_PROXY 环境变量摘掉（验证 FFmpeg 会不会把 127.0.0.1 也塞进代理）",
    )
    parser.add_argument(
        "--no-qt-system-proxy",
        action="store_true",
        help="关掉 Qt 自己的系统代理（验证 127.0.0.1 是不是被 Qt 又塞进代理走了一圈）",
    )
    args = parser.parse_args()

    from PySide6.QtNetwork import QNetworkProxyFactory

    if args.no_qt_system_proxy:
        QNetworkProxyFactory.setUseSystemConfiguration(False)
        print("已关闭 Qt 的系统代理配置")

    if args.no_env_proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
            os.environ.pop(key, None)
        print(f"已摘掉代理环境变量，剩余: {[k for k in os.environ if 'proxy' in k.lower()]}")

    app = QApplication.instance() or QApplication(sys.argv[:1])

    from src.core import local_proxy, resolver
    from src.utils import net_proxy

    result = resolver.resolve(args.url, timeout_s=60)
    headers = result.headers or {}
    if args.direct_url:
        target = args.direct_url
        headers = {}
        print("使用直接给出的地址")
    elif not (result.video_url or result.audio_url):
        print("解析失败:", result.error or result.note)
        return 1
    else:
        target = result.audio_url or result.video_url
        print(f"分离流={result.has_separate_streams} 用的是{'音频' if result.audio_url else '画面'}流")

    play_url = target
    if not args.no_proxy:
        must_route = net_proxy.proxy_for(target, app_proxy=S.get_str(S.K_PROXY, "")) is not None
        proxied = local_proxy.proxy_url(target, headers, required=must_route)
        if proxied:
            play_url = proxied
    print(f"上游代理: {net_proxy.proxy_for(target, app_proxy=S.get_str(S.K_PROXY, ''))}")
    print(f"播放地址: {play_url[:100]}…")

    devices = QMediaDevices.audioOutputs()
    print(f"音频输出设备: {[d.description() for d in devices]}")

    player = QMediaPlayer()
    audio = QAudioOutput()
    audio.setVolume(1.0)
    player.setAudioOutput(audio)

    t0 = [time.monotonic()]

    def stamp() -> str:
        return f"{time.monotonic() - t0[0]:6.2f}s"

    marks: dict[str, float] = {}

    player.mediaStatusChanged.connect(lambda s: print(f"[{stamp()}] 状态 -> {_name(s)}"))
    player.errorOccurred.connect(lambda c, t: print(f"[{stamp()}] 错误 -> {int(c)} {t!r}"))
    player.playbackStateChanged.connect(lambda s: print(f"[{stamp()}] 播放态 -> {_name(s)}"))

    history: list[int] = []

    def tick() -> None:
        pos = int(player.position())
        history.append(pos)
        moved = "" if len(history) < 2 else f" Δ={pos - history[-2]:+d}"
        print(
            f"[{stamp()}] pos={pos:>7}{moved} {_name(player.playbackState()):>13} "
            f"{_name(player.mediaStatus()):>13} err={_name(player.error())}"
        )

    ticker = QTimer()
    ticker.timeout.connect(tick)
    ticker.start(1000)

    def start() -> None:
        player.setSource(QUrl(play_url))
        player.play()
        print(f"[{stamp()}] >>> play()")

    def seek() -> None:
        before = int(player.position())
        wanted = args.seek_absolute if args.seek_absolute >= 0 else before + args.seek_delta
        marks["seek_at"] = time.monotonic()
        marks["seek_before"] = float(before)
        print(f"[{stamp()}] >>> setPosition({wanted})（当前 {before}）")
        player.setPosition(wanted)

    def report_resume() -> None:
        """第一次看到位置明显离开 seek 目标，就认为 seek 恢复了。"""
        if "seek_at" not in marks or "resumed" in marks:
            return
        pos = int(player.position())
        if abs(pos - marks.get("seek_before", 0)) > 200:
            marks["resumed"] = time.monotonic()
            print(f"[{stamp()}] <<< seek 后恢复推进（耗时 {marks['resumed'] - marks['seek_at']:.1f}s）")

    resume_watch = QTimer()
    resume_watch.timeout.connect(report_resume)
    resume_watch.start(300)

    QTimer.singleShot(500, start)
    if args.seek_at:
        QTimer.singleShot(args.seek_at * 1000, seek)
    QTimer.singleShot(args.seconds * 1000, app.quit)

    app.exec()
    ticker.stop()
    resume_watch.stop()
    if len(history) >= 2:
        print(f"结论: pos {history[0]} -> {history[-1]}（{'在走' if history[-1] > history[0] + 500 else '没走'}）")
    if "resumed" in marks:
        print(f"seek 恢复耗时: {marks['resumed'] - marks['seek_at']:.1f}s")
    elif "seek_at" in marks:
        print("seek 后**一直没恢复**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
