"""让 FFmpeg 自己吐日志：它到底怎么读我们的本地代理地址。

用法:
    python tools/ffmpeg_http_probe.py "<页面地址>" [--seconds 12] [--target audio|video]

背景：Qt 的 FFmpeg 后端读经本地反向代理的音频流要 14~20 秒才 LoadedMedia，
直连只要 1.5 秒；而两条路的上游其实是同一台服务器。Qt 把 FFmpeg 的日志
转发到应用日志时级别会被压掉，所以这里**直接用 FFmpeg 命令行**跑一遍，
拿它自己的 http/seak 日志（-v debug）来定位。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils import settings as S  # noqa: E402
from src.utils.logger import setup_logging  # noqa: E402

setup_logging()

#: 想要看的 FFmpeg 日志行
_INTERESTING = re.compile(
    r"Opening|Reconnect|seek|Seek|http|HTTP|proxy|Connection|Transfer|"
    r"Input #|Stream #|Duration|error|Error|failed",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--seconds", type=int, default=12)
    parser.add_argument("--target", choices=("audio", "video"), default="audio")
    parser.add_argument("--no-proxy", action="store_true", help="用直连地址做对照")
    args = parser.parse_args()

    from src.core import local_proxy, resolver
    from src.utils import net_proxy

    result = resolver.resolve(args.url, timeout_s=60)
    headers = result.headers or {}
    target = result.audio_url if args.target == "audio" else result.video_url
    if not target:
        print(f"没有{args.target}流")
        return 1

    hit = net_proxy.proxy_for(target, app_proxy=S.get_str(S.K_PROXY, ""))
    print(f"上游代理: {hit}")
    if args.no_proxy:
        play_url = target
    else:
        play_url = local_proxy.proxy_url(target, headers, required=hit is not None) or target
    print(f"给 FFmpeg 的地址: {play_url[:110]}…")

    from src.utils.paths import app_root

    candidates = [
        Path(os.environ.get("APPDATA", "")) / "UniversalURLPlayer" / "bin" / "ffmpeg.exe",
        app_root() / "bin" / "ffmpeg.exe",
        app_root() / "dist" / "UniversalURLPlayer" / "_internal" / "imageio_ffmpeg"
        / "binaries" / "ffmpeg-win-x86_64-v7.1.exe",
    ]
    ffmpeg = next((str(p) for p in candidates if p.is_file()), "")
    if not ffmpeg:
        import shutil

        ffmpeg = shutil.which("ffmpeg") or ""
    if not ffmpeg:
        print("找不到 ffmpeg 可执行文件")
        return 1
    print(f"FFmpeg: {ffmpeg}")

    header_args: list[str] = []
    for name, value in headers.items():
        header_args += ["-headers", f"{name}: {value}\r\n"]

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "debug",
        *header_args,
        "-t", str(args.seconds),
        "-i", play_url,
        "-f", "null", "-",
    ]
    print("命令:", " ".join(cmd[:6]), "…")
    proc = subprocess.run(
        cmd, capture_output=True, text=True, errors="replace", timeout=args.seconds + 90
    )
    lines = (proc.stdout + "\n" + proc.stderr).splitlines()
    for line in lines:
        if _INTERESTING.search(line):
            print("  " + line[:220])
    print(f"返回码={proc.returncode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
