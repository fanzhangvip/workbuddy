"""网页视频地址「能不能拿到播放地址」一站式排查。

相比 net_check.py（只查网络层连通性），这个工具走**完整链路**：
网络可达性预检 → 逐策略解析（备用播放器接口 / 浏览器指纹伪装 / 补 Referer…）
→ 最终能否拿到可播放的地址。

它能说清楚三类结论：

1. **网络层不可达** —— 站点被整段拦掉（如本机对 youtube.com / vimeo.com
   的 DNS 污染 + TCP 拦截）。这时候省掉二十多秒的解析空等，直接给结论。
2. **解析失败** —— 网络通，但拿不到地址（要登录 / 地区限制 / 不支持该站）。
3. **解析成功** —— 给出能播的地址、类型、是否分离流等。

用法::

    python tools/site_check.py "https://www.youtube.com/watch?v=xxx"
    python tools/site_check.py "https://vimeo.com/76979871"
    python tools/site_check.py --no-preflight "https://www.bilibili.com/video/BV1xx"
    python tools/site_check.py --verbose "https://example.com/page"
    python tools/site_check.py --timeout 40 "https://慢站点/..."

退出码：拿到可播放地址为 0，否则为 1。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import reachability as RC  # noqa: E402
from src.core import resolver as R  # noqa: E402


def _print_preflight(url: str, timeout: float) -> None:
    print("─" * 74)
    print("① 网络可达性预检")
    print("─" * 74)
    started = time.time()
    pre = RC.probe(url, timeout=timeout)
    elapsed = time.time() - started
    if pre is None:
        print("  不适用：非 http(s) 地址，跳过预检。")
        return
    print(f"  耗时 {elapsed:.1f} 秒")
    for line in pre.report().splitlines():
        print(f"  {line}")
    print()
    if pre.blocked:
        print("  ▶ 结论：网络层不可达。下面的解析已无意义，不再浪费时间。")
        print("    请检查网络/代理，或在设置里配置可用的 HTTP 代理后重试。")


def main() -> int:
    parser = argparse.ArgumentParser(description="网页视频地址解析一站式排查")
    parser.add_argument("url", help="视频网站页面地址")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="解析硬超时秒数（默认 30）")
    parser.add_argument("--no-preflight", action="store_true",
                        help="跳过网络可达性预检，直接解析")
    parser.add_argument("--verbose", action="store_true",
                        help="输出 yt-dlp 详细步骤")
    args = parser.parse_args()

    url = args.url.strip()
    if not url:
        print("请提供一个页面地址。")
        return 2

    print(f"目标地址：{url}")
    print()
    print(f"解析组件：{R.describe_availability()}")
    print(f"指纹伪装：{R.describe_impersonation()}")
    js = R.detect_js_runtime()
    print(f"JS 运行时：{', '.join(js) if js else '未启用（YouTube 解析可能退化）'}")
    print()

    # ---- 第 1 层：预检 ----
    if not args.no_preflight:
        _print_preflight(url, timeout=min(args.timeout, 6.0))
        print()
        print("─" * 74)
        print("② 解析尝试")
        print("─" * 74)
    else:
        print("─" * 74)
        print("① 解析尝试（已跳过预检）")
        print("─" * 74)

    # ---- 第 2 层：逐策略解析 ----
    attempts = []

    def sink(message: str) -> None:
        print(f"  {message}")

    started = time.time()
    result = R.resolve(url, timeout_s=args.timeout, log=sink, verbose=args.verbose)
    elapsed = time.time() - started
    print(f"  解析耗时 {elapsed:.1f} 秒")
    print()

    print("─" * 74)
    print("③ 结论")
    print("─" * 74)
    print(result.report())

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
