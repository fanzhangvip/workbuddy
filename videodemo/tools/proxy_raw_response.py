"""拿原始 socket 看本地反向代理到底回了什么字节（排查 FFmpeg 为何卡住）。

用法:
    python tools/proxy_raw_response.py "<页面地址>" [--target audio|video]

起因：Qt 的 FFmpeg 后端经本地反向代理读 googlevideo 的音频流时，
读完 256KB 就一直等，直到 Qt 设的 20 秒网络超时把它踢开（日志里就是
"Using custom network timeout: 20000ms" → 20 秒后 "Could not open media"）。
本工具用和 FFmpeg 一模一样的请求头去问代理，把回的前若干字节原样打出来，
看响应头是不是少了什么、或响应体接得不对。
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils import settings as S  # noqa: E402
from src.utils.logger import setup_logging  # noqa: E402

setup_logging()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--target", choices=("audio", "video"), default="audio")
    parser.add_argument("--wait", type=float, default=3.0, help="观察多少秒")
    parser.add_argument("--range", default="bytes=0-", help="发给代理的 Range 头")
    args = parser.parse_args()

    from src.core import local_proxy, resolver
    from src.utils import net_proxy

    result = resolver.resolve(args.url, timeout_s=60)
    headers = result.headers or {}
    target = result.audio_url if args.target == "audio" else result.video_url
    if not target:
        print("没有该流")
        return 1
    hit = net_proxy.proxy_for(target, app_proxy=S.get_str(S.K_PROXY, ""))
    proxied = local_proxy.proxy_url(target, headers, required=hit is not None)
    if not proxied:
        print("代理不可用")
        return 1

    from urllib.parse import urlparse

    parsed = urlparse(proxied)
    path = parsed.path + ("?" + parsed.query if parsed.query else "")

    # 与 Qt 后端发出去的那份请求头逐字对齐（见调试日志里的 request: ...）
    request = (
        f"GET {path} HTTP/1.1\r\n"
        "User-Agent: Lavf/61.7.103\r\n"
        "Accept: */*\r\n"
        f"Range: {args.range}\r\n"
        "Connection: close\r\n"
        f"Host: {parsed.netloc}\r\n"
        "Icy-MetaData: 1\r\n"
        "\r\n"
    ).encode()

    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
    sock.sendall(request)
    print(f"已发送 {len(request)} 字节请求到 {parsed.netloc}{path[:60]}…")

    deadline = time.monotonic() + args.wait
    received = bytearray()
    sock.settimeout(0.5)
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            print("对端关闭连接")
            break
        received += chunk
    sock.close()

    print(f"共收到 {len(received)} 字节（{args.wait} 秒内）")
    head = bytes(received[:1200])
    print("--- 响应开头（换行可视化）---")
    print(head.decode("latin-1").replace("\r\n", "\n"))
    print("--- 结束 ---")
    text = head.decode("latin-1")
    print(f"有完整头部结尾 \\r\\n\\r\\n: {b'\r\n\r\n' in bytes(received)}")
    if "content-length" in text.lower():
        line = [ln for ln in text.splitlines() if ln.lower().startswith("content-length")]
        print("Content-Length 头:", line)
    # 重复的响应头会让解析器犯迷糊（我们的 send_response 已经加过一次 Date）
    names = [ln.split(":", 1)[0].lower() for ln in text.split("\r\n") if ":" in ln]
    dupes = {n for n in names if names.count(n) > 1}
    print("重复的响应头:", sorted(dupes) or "无")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
