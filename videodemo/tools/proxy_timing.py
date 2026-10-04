"""量一下本地反向代理转发媒体流的耗时（对比直连）。

用法:
    python tools/proxy_timing.py "<页面地址>" [--mb 4]

只做只读的 GET（带 Range），不改任何东西。用来回答:
  · 经本地反向代理取第一段字节要多久？直连呢？
  · 再发一次 Range（模拟播放器 seek 重新定位）要多久？

如果代理这条路比直连慢一个数量级，"分离流合流"的音频就必然来不及加载，
表现就是"只有画面、没有声音"。
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

from src.utils import settings as S  # noqa: E402
from src.utils.logger import get_logger, setup_logging  # noqa: E402

setup_logging()
log = get_logger("proxy_timing")


def timed_get(url: str, headers: dict, byte_count: int, label: str) -> None:
    import requests

    end = byte_count - 1
    request_headers = dict(headers)
    request_headers["Range"] = f"bytes=0-{end}"
    request_headers["Accept-Encoding"] = "identity"
    started = time.monotonic()
    try:
        response = requests.get(url, headers=request_headers, stream=True, timeout=(15, 60))
    except Exception as exc:  # noqa: BLE001
        print(f"  {label}: 请求失败 {exc}")
        return
    first_byte = time.monotonic()
    got = 0
    try:
        for chunk in response.iter_content(chunk_size=256 * 1024):
            got += len(chunk)
            if got >= byte_count:
                break
    except Exception as exc:  # noqa: BLE001
        print(f"  {label}: 读取中断 {exc}")
    done = time.monotonic()
    print(
        f"  {label}: 状态={response.status_code} 首字节={first_byte - started:.2f}s "
        f"读完{got / 1048576:.2f}MB={done - started:.2f}s "
        f"({got / max(done - started, 1e-6) / 1024:.0f} KB/s)"
    )
    response.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--mb", type=float, default=4.0)
    args = parser.parse_args()
    byte_count = int(args.mb * 1048576)

    from src.core import local_proxy, resolver
    from src.utils import net_proxy

    result = resolver.resolve(args.url, timeout_s=60)
    headers = result.headers or {}
    print(f"请求头: {list(headers)}")
    hit = net_proxy.proxy_for(result.video_url or args.url, app_proxy=S.get_str(S.K_PROXY, ""))
    print(f"上游代理: {hit}")

    for label, target in (("画面", result.video_url), ("声音", result.audio_url)):
        if not target:
            continue
        print(f"\n[{label}] {target[:100]}")
        print("  直连（FFmpeg 走 fc 的 http_proxy 时也是这条）:")
        timed_get(target, headers, byte_count, "直连")
        proxied = local_proxy.proxy_url(target, headers, required=True)
        if not proxied:
            print("  本地代理不可用")
            continue
        print(f"  经本地反向代理 ({proxied[:60]}...):")
        timed_get(proxied, headers, byte_count, "首段")
        timed_get(proxied, headers, byte_count, "再取一次")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
