"""量上游（googlevideo，经代理）在两种 Range 下的真实投递节奏。

用法:
    python tools/upstream_rate.py "<页面地址>" [--target audio|video] [--mb 2]

想弄清的事：客户端只要 32KB 就能开始解析，而我们的本地反向代理要攒够
256KB 才写出去。如果上游是"细水长流"（几十 KB/s），那这 256KB 就要等十几秒，
表现就是"媒体要 20 秒才加载完"（正好撞上 Qt 设的 20 秒网络超时）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils import settings as S  # noqa: E402
from src.utils.logger import setup_logging  # noqa: E402

setup_logging()

#: 关心的里程碑（字节）
_MARKS = (32 * 1024, 64 * 1024, 256 * 1024, 1024 * 1024, 4 * 1024 * 1024)


def measure(
    url: str, headers: dict, range_value: str, chunk: int, limit: int, budget: float = 0.0
) -> None:
    import requests

    request_headers = dict(headers)
    if range_value:
        request_headers["Range"] = range_value
    request_headers["Accept-Encoding"] = "identity"
    started = time.monotonic()
    try:
        response = requests.get(
            url, headers=request_headers, stream=True, timeout=(15, budget or 60)
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  请求失败: {exc}")
        return
    first_byte = 0.0
    marks: list[str] = []
    got = 0
    next_mark = 0
    try:
        for piece in response.iter_content(chunk_size=chunk):
            if piece and not first_byte:
                first_byte = time.monotonic() - started
            got += len(piece)
            while next_mark < len(_MARKS) and got >= _MARKS[next_mark]:
                marks.append(f"{_MARKS[next_mark] // 1024}KB@{time.monotonic() - started:.2f}s")
                next_mark += 1
            if got >= limit:
                break
            if budget and time.monotonic() - started > budget:
                print("  （预算到，提前收工）")
                break
    except Exception as exc:  # noqa: BLE001
        print(f"  读取中断: {type(exc).__name__} {str(exc)[:80]}")
    elapsed = time.monotonic() - started
    shown = range_value or "（不带 Range）"
    print(
        f"  Range={shown:<24} chunk={chunk // 1024}KB → 收到 {got / 1024:.0f}KB "
        f"用时 {elapsed:.2f}s (首字节 {first_byte:.2f}s, "
        f"{got / max(elapsed, 1e-6) / 1024:.0f} KB/s)"
    )
    print(f"      里程碑: {' '.join(marks) if marks else '无'}")
    response.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--target", choices=("audio", "video"), default="audio")
    parser.add_argument("--mb", type=float, default=2.0)
    parser.add_argument(
        "--ranges",
        default="bytes=0-,bytes=0-4194303",
        help="逗号分隔的 Range 取值；留空代表不带 Range",
    )
    parser.add_argument("--chunk-kb", type=int, default=64)
    parser.add_argument("--budget", type=float, default=0.0, help="每个组合最多观察多少秒")
    args = parser.parse_args()
    limit = int(args.mb * 1048576)

    from src.core import resolver
    from src.utils import net_proxy

    result = resolver.resolve(args.url, timeout_s=60)
    headers = result.headers or {}
    target = result.audio_url if args.target == "audio" else result.video_url
    if not target:
        print("没有该流")
        return 1
    print(f"上游代理: {net_proxy.proxy_for(target, app_proxy=S.get_str(S.K_PROXY, ''))}")

    for range_value in args.ranges.split(","):
        measure(target, headers, range_value.strip(), args.chunk_kb * 1024, limit, args.budget)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
