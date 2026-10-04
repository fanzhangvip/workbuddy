"""隔离测试：本地小文件经「本地反向代理」再给 Qt，加载是否也慢。

用法:
    python tools/proxy_smallfile_test.py [--codec opus|mp3]

思路：把"源头"换成本机的一个静态文件服务器（快、无抖动），这样就把
"是不是 googlevideo 那条流有问题"排除掉。三种情况对比 LoadedMedia 耗时：
  1. 直接 http://127.0.0.1:<静态服务>/test.<ext>
  2. 经本地反向代理（本地静态服务作为上游）
如果 1 快、2 慢，说明问题出在我们的代理响应上（不是上游、不是编解码）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.utils.env_setup import setup_environment  # noqa: E402

setup_environment()

from src.utils.logger import setup_logging  # noqa: E402

setup_logging()

from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def find_ffmpeg() -> str:
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "UniversalURLPlayer" / "bin" / "ffmpeg.exe",
        ROOT / "bin" / "ffmpeg.exe",
        ROOT / "dist" / "UniversalURLPlayer" / "_internal" / "imageio_ffmpeg" / "binaries"
        / "ffmpeg-win-x86_64-v7.1.exe",
        ROOT / "dist" / "UniversalURLPlayer-single.exe",
    ]
    for path in candidates:
        if path.is_file() and path.suffix == ".exe" and "single" not in path.name:
            return str(path)
    return shutil.which("ffmpeg") or ""


def make_sample(ffmpeg: str, out: Path, codec: str) -> bool:
    if codec == "opus":
        cmd = [
            ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=600",
            "-c:a", "libopus", "-b:a", "128k", "-f", "webm", str(out),
        ]
    else:
        cmd = [
            ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=600",
            "-c:a", "aac", "-b:a", "128k", str(out),
        ]
    proc = subprocess.run(cmd, capture_output=True)
    return proc.returncode == 0 and out.is_file()


class _Server:
    def __init__(self, directory: str) -> None:
        handler = partial(_QuietHandler, directory=directory)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: ANN002
        pass


def time_load(url: str, label: str, seconds: int = 20) -> float:
    """返回 LoadedMedia 出现的时间（秒），超时返回 -1。"""
    app = QApplication.instance() or QApplication(sys.argv[:1])
    player = QMediaPlayer()
    audio = QAudioOutput()
    player.setAudioOutput(audio)
    result = {"loaded": -1.0}
    import time

    t0 = time.monotonic()
    player.mediaStatusChanged.connect(
        lambda s: result.update(loaded=time.monotonic() - t0)
        if getattr(s, "name", "") == "LoadedMedia" and result["loaded"] < 0
        else None
    )
    player.errorOccurred.connect(
        lambda c, t: print(f"  {label} 错误 {int(c)} {t!r}")
    )
    player.setSource(QUrl(url))
    player.play()

    done = threading.Event()
    QTimer.singleShot(seconds * 1000, lambda: (done.set(), app.quit()))
    app.exec()
    player.setSource(QUrl())
    print(f"  {label}: LoadedMedia={result['loaded']:.2f}s" if result["loaded"] >= 0 else f"  {label}: 超时未加载")
    return result["loaded"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codec", choices=("opus", "mp3"), default="opus")
    parser.add_argument("--seconds", type=int, default=20)
    args = parser.parse_args()

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("找不到 ffmpeg")
        return 1
    workdir = Path(tempfile.mkdtemp(prefix="proxy_smallfile_"))
    name = "sample.webm" if args.codec == "opus" else "sample.m4a"
    sample = workdir / name
    if not make_sample(ffmpeg, sample, args.codec):
        print("生成测试音频失败")
        return 1
    print(f"样本: {sample} ({sample.stat().st_size / 1024:.0f} KB, {args.codec})")

    server = _Server(str(workdir))
    direct = f"http://127.0.0.1:{server.port}/{name}"
    print(f"静态服务: {direct}")

    from src.core import local_proxy

    proxied = local_proxy.proxy_url(direct, {"User-Agent": "test"}, required=True)
    print(f"经本地代理: {proxied[:100] if proxied else '（不可用）'}")

    QTimer  # noqa: B018 - 保持导入明显
    print("加载耗时对照：")
    time_load(direct, "① 直连静态服务", args.seconds)
    if proxied:
        time_load(proxied, "② 经本地反向代理", args.seconds)

    server.stop()
    local_proxy.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
