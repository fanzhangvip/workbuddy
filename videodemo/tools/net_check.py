"""命令行地址排查工具：一条命令看清某个地址为什么播不了。

相比 env_probe.py（真的去解码），这个工具**不启动播放器**，只做网络层
与协议层的只读探测，因此非常快（通常 1 秒内），适合对着一条地址反复试。

用法::

    python tools/net_check.py "rtsp://host:554/path"
    python tools/net_check.py "rtmp://ip:1935/livetv/hunantv" "https://x/a.mp4"
    python tools/net_check.py --timeout 8 "rtsp://慢服务器/..."
    python tools/net_check.py --no-probe "http://x/a.mp4"   # 只测到 TCP 连接

退出码：全部可达为 0，否则为 1（便于脚本判断）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.net_diagnose import diagnose  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="播放地址分层诊断")
    parser.add_argument("urls", nargs="+", help="一个或多个待诊断的地址")
    parser.add_argument("--timeout", type=float, default=4.0, help="每个阶段超时秒数")
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="跳过协议握手，只测到 TCP 连接（更快）",
    )
    args = parser.parse_args()

    failures = 0
    for index, url in enumerate(args.urls, start=1):
        if len(args.urls) > 1:
            print(f"\n{'#' * 74}\n# [{index}/{len(args.urls)}]\n{'#' * 74}")

        result = diagnose(url, timeout=args.timeout, probe_protocol=not args.no_probe)
        print(result.report())

        healthy = result.reachable and result.protocol_ok
        print()
        print("=" * 74)
        print("判定：" + ("可以正常连接" if healthy else "连通性存在问题"))
        print("=" * 74)

        if not healthy:
            failures += 1

    if len(args.urls) > 1:
        print(f"\n共 {len(args.urls)} 条地址，{failures} 条连通性异常")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
