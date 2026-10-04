"""列出某页面所有"仅声音"格式的地址（用来对比 opus/webm 与 m4a 的 seek 代价）。

用法:
    python tools/dump_audio_formats.py "<页面地址>" [--limit 6]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import resolver  # noqa: E402
from src.utils.logger import setup_logging  # noqa: E402

setup_logging()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--write", default="", help="把 format_id -> url 写成 JSON 到这里")
    args = parser.parse_args()

    options = resolver._base_options(lambda m: None, False)  # noqa: SLF001
    network = resolver._network_options(None, None, None)  # noqa: SLF001
    merged = resolver._merge_options(options, network, resolver.Strategy("默认"))  # noqa: SLF001
    info = resolver._extract(args.url, merged)  # noqa: SLF001
    if not info:
        print("解析失败")
        return 1

    entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)] or [info]
    data = entries[0]
    formats = [f for f in (data.get("formats") or []) if isinstance(f, dict)]
    audio = [
        f for f in formats
        if resolver.codec_absent(f.get("vcodec")) and resolver.is_playable_format(f)
    ]
    audio.sort(key=resolver.quality_key, reverse=True)
    if args.write:
        import json

        payload = {
            str(f.get("format_id")): {
                "url": str(f.get("url") or ""),
                "ext": str(f.get("ext") or ""),
                "acodec": str(f.get("acodec") or ""),
                "headers": f.get("http_headers") or {},
            }
            for f in audio
        }
        Path(args.write).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"已写入 {args.write}")
    print(f"共 {len(formats)} 条格式，其中仅声音 {len(audio)} 条：")
    for fmt in audio[: args.limit]:
        url = str(fmt.get("url") or "")
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        print(
            f"  itag={fmt.get('format_id'):>6} ext={fmt.get('ext'):>5} "
            f"acodec={fmt.get('acodec')} abr={fmt.get('abr')} "
            f"proto={fmt.get('protocol')} size={int(size) / 1048576:.1f}MB "
            f"host={urlparse(url).netloc}"
        )
        print(f"        {url[:150]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
