"""用 libmpv 实测「分离流合流」：主源画面 + 附加音轨，看声音到底有没有在走。

用法:
    python tools/mpv_merge_test.py "<页面地址>" [--seconds 30] [--seek-at 15]

为什么值得单独测：mpv 有原生的 `audio-file`（多音轨），音画同步由它自己的
时钟统一处理，不需要我们手动 seek 去对齐。如果这条路可靠，"分离流没有声音"
的问题就可以从根上绕开。

判读（不能靠"能加载"下结论）:
  · `audio-pts` 在推进  → 声音真的在解码输出
  · `aid`=1 且 track-list 里有 audio → 附加音轨确实挂上了
  · `ao`（音频输出驱动）非空 → 音频设备打开成功
"""

from __future__ import annotations

import argparse
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
from src.utils.logger import get_logger, setup_logging  # noqa: E402

setup_logging()
log = get_logger("mpv_merge_test")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv[:1])

from src.core.backends.mpv_backend import MpvBackend  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--seek-at", type=int, default=0)
    args = parser.parse_args()

    from src.core import local_proxy, resolver
    from src.utils import net_proxy

    result = resolver.resolve(args.url, timeout_s=60)
    headers = result.headers or {}
    if not (result.video_url and result.audio_url):
        print(f"不是分离流（video={bool(result.video_url)} audio={bool(result.audio_url)}），改用主地址")
        target = result.video_url or result.audio_url
        video_url, audio_url = target, ""
    else:
        video_url, audio_url = result.video_url, result.audio_url

    must_route = net_proxy.proxy_for(video_url, app_proxy=S.get_str(S.K_PROXY, "")) is not None
    video_play = local_proxy.proxy_url(video_url, headers, required=must_route) or video_url
    audio_play = ""
    if audio_url:
        audio_play = local_proxy.proxy_url(audio_url, headers, required=must_route) or audio_url
    print(f"上游代理={must_route} 画面={video_play[:80]}…")
    print(f"声音={audio_play[:80]}…")

    backend = MpvBackend()
    print(f"mpv 后端可用: {sorted(backend.capabilities())}")

    def prop(name, default=None):
        try:
            return backend._player[name]  # noqa: SLF001
        except Exception:  # noqa: BLE001
            return default

    t0 = time.monotonic()
    history: list[float] = []

    def stamp() -> str:
        return f"{time.monotonic() - t0:6.2f}s"

    def tick() -> None:
        pos = prop("time-pos")
        apts = prop("audio-pts")
        tracks = prop("track-list") or []
        audio_tracks = [t for t in tracks if t.get("type") == "audio"]
        if apts is not None:
            history.append(float(apts))
        moved = "" if len(history) < 2 else f" Δ={history[-1] - history[-2]:+.2f}"
        print(
            f"[{stamp()}] time-pos={pos} audio-pts={apts}{moved} "
            f"aid={prop('aid')} ao={prop('ao')} audio轨={len(audio_tracks)} "
            f"video轨={len([t for t in tracks if t.get('type') == 'video'])} "
            f"pause={prop('pause')} cache={prop('demuxer-cache-time')}"
        )

    def start() -> None:
        backend.set_source(video_play)
        if audio_play:
            backend.set_audio_source(audio_play)
        backend.play()
        print(f"[{stamp()}] >>> play() 已发出")

    def seek() -> None:
        print(f"[{stamp()}] >>> set_position(600000)")
        backend.set_position(600000)

    ticker = QTimer()
    ticker.timeout.connect(tick)
    ticker.start(1000)

    QTimer.singleShot(300, start)
    if args.seek_at:
        QTimer.singleShot(args.seek_at * 1000, seek)
    QTimer.singleShot(args.seconds * 1000, app.quit)

    app.exec()
    ticker.stop()
    backend.release()
    if len(history) >= 2:
        print(f"结论: audio-pts {history[0]:.2f} -> {history[-1]:.2f}（{'在走' if history[-1] > history[0] + 1 else '没走'}）")
    else:
        print("结论: 没拿到 audio-pts（声音那一路没起来）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
