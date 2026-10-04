"""VPN / 代理体检：一条命令看清"现在走的是哪条路、那条路通不通"。

背景：程序里有三个地方要用代理，但读法不同——

  · 解析（yt-dlp）与下载（requests）自己会读环境变量和 Windows 系统代理；
  · **播放**（Qt 的 FFmpeg 后端）**不认任何代理配置**，只能靠本地反向代理
    替它转发（检测到有代理在生效时自动接上）。

所以"我配了 VPN，播放还是连不上"这类问题，得先确认生效的到底是哪一条。
本工具只读，不改任何配置、不发任何写请求。

用法::

    python tools/vpn_check.py                      # 用设置里填的代理测 YouTube
    python tools/vpn_check.py --proxy http://127.0.0.1:7897
    python tools/vpn_check.py --target https://www.google.com/
    python tools/vpn_check.py --timeout 10

退出码：目标可访问为 0，否则为 1（可直接用于脚本判断）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils import proxy_check  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="VPN / 代理体检")
    parser.add_argument("--target", default="", help="要测的目标地址（默认 YouTube 首页）")
    parser.add_argument(
        "--proxy",
        default=None,
        help="临时指定代理地址（不写就读设置里填的那个）",
    )
    parser.add_argument("--timeout", type=float, default=6.0, help="每次探测超时秒数")
    args = parser.parse_args()

    if args.proxy is None:
        from src.utils import settings as S

        app_proxy = S.get_str(S.K_PROXY, "").strip()
        origin = "设置里填写的代理"
    else:
        app_proxy = args.proxy.strip()
        origin = "命令行指定的代理"

    print("=" * 72)
    print("代理来源（按优先级，取第一个配置了的）")
    print("=" * 72)
    for item in proxy_check.sources(app_proxy):
        mark = "✓" if item.configured else "·"
        value = item.value or "（未配置）"
        print(f" {mark} {item.label:<12} {value}")
        print(f"     {item.role}")
    print(f"\n（本次以「{origin}」为准）")

    print("\n" + "=" * 72)
    print("当前生效")
    print("=" * 72)
    print(proxy_check.describe_active(app_proxy))

    print("\n" + "=" * 72)
    print("连通性实测")
    print("=" * 72)
    report = proxy_check.check(args.target, app_proxy, timeout=args.timeout)
    print(report.text())

    print()
    if report.ok:
        print("→ 结论：这条通路可用。")
        return 0
    print("→ 结论：不可用，按上面的「结论 / 建议」逐条排查。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
