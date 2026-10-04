"""
环境能力探测工具。

用法:
    python tools/env_probe.py                  # 静态检查：后端、插件、协议支持
    python tools/env_probe.py <url>            # 用 QMediaPlayer 实际尝试打开地址
    python tools/env_probe.py <url> --debug    # 附带 FFmpeg 详细日志
    python tools/env_probe.py <url> --resolve-only   # 只解析网页地址，不播放
    python tools/env_probe.py <url> --no-resolve     # 网页地址也直接当媒体播（看原始报错）

用途：排查"某种地址为什么播不了"。尤其是 rtsp/rtmp 这类协议，
直接确认内核能力，比在界面里反复试要快得多。

判定成功的标准（按可靠性排序）:
  1. 点播流拿到 duration > 0
  2. 真的解出了画面帧（QVideoSink 收到 videoFrameChanged）——这是最硬的证据
  3. 纯音频流播放位置往前推进

注意：直播流 duration 恒为 0，不能用"是否有时长"来判断成败。

网页地址（B站/YouTube 等）默认会先经 yt-dlp 解析出真实流地址再播，
与界面里的行为保持一致；解析逻辑在 src/core/resolver.py，与主程序共用一份，
不在这里另写。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能直接运行（把项目根加入 sys.path）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.env_setup import describe_environment, setup_environment  # noqa: E402

#: 解析网页地址的超时（秒）。yt-dlp 要跑好几步，比播放超时给得宽一些。
RESOLVE_TIMEOUT_S = 25


# ---------------------------------------------------------------- 静态检查
def check_environment() -> None:
    print("=" * 68)
    print("环境变量")
    print("=" * 68)
    print(describe_environment())


def check_packages() -> None:
    print()
    print("=" * 68)
    print("依赖与版本")
    print("=" * 68)

    import PySide6
    from PySide6.QtCore import qVersion

    print(f"PySide6      : {PySide6.__version__}")
    print(f"Qt           : {qVersion()}")

    try:
        from PySide6.QtMultimedia import QMediaPlayer  # noqa: F401

        print("QtMultimedia : 可用")
    except Exception as exc:  # noqa: BLE001
        print(f"QtMultimedia : 不可用 -> {exc}")
        print("  提示: PySide6-Essentials 不含 QtMultimedia，需要安装 PySide6-Addons")

    try:
        import mpv  # noqa: F401

        print("libmpv       : 可用")
    except Exception:
        print("libmpv       : 未安装（rtsp/防盗链场景的备选后端）")

    try:
        from src.core import resolver

        print(f"yt-dlp       : {resolver.describe_availability()}")
    except Exception as exc:  # noqa: BLE001
        print(f"yt-dlp       : 检测失败 -> {exc}")


def check_plugins() -> None:
    print()
    print("=" * 68)
    print("多媒体插件与 FFmpeg 库")
    print("=" * 68)

    import PySide6

    base = Path(PySide6.__file__).parent
    candidates = [base / "plugins" / "multimedia", base]

    found_any = False
    for directory in candidates:
        if not directory.is_dir():
            continue
        ffmpeg_files = [
            p
            for p in directory.iterdir()
            if p.is_file() and ("ffmpeg" in p.name.lower() or p.name.lower().startswith("av"))
        ]
        if ffmpeg_files:
            found_any = True
            print(f"{directory}:")
            for p in sorted(ffmpeg_files):
                print(f"  {p.name}  ({p.stat().st_size / 1024 / 1024:.1f} MB)")

    if not found_any:
        print("未找到多媒体插件或 FFmpeg 库 —— QtMultimedia 可能不可用。")
        return

    # 在二进制里搜协议名字符串，粗略判断 FFmpeg 是否编入了该协议
    protocols = [
        "rtsp", "rtsps", "rtmp", "rtmps", "rtp", "udp", "tcp",
        "http", "https", "tls", "hls", "file", "crypto", "srt",
    ]
    print()
    print("FFmpeg 协议支持（按二进制内是否出现协议名推断，仅供参考，需实测确认）:")

    blobs: list[tuple[str, bytes]] = []
    for directory in candidates:
        if not directory.is_dir():
            continue
        for p in directory.iterdir():
            if p.is_file() and p.suffix.lower() == ".dll" and p.stat().st_size > 300_000:
                try:
                    blobs.append((p.name, p.read_bytes()))
                except OSError:
                    pass

    for name in protocols:
        needle = name.encode()
        hits = [fname for fname, data in blobs if needle in data]
        print(f"  {name:<8} {'支持' if hits else '未找到'}")


# ---------------------------------------------------------------- 网页地址解析
def resolve_target(url: str, *, force: bool) -> tuple[str, int]:
    """决定"接下来该播哪个地址"。

    返回 (要播放的地址, 结论码)。地址为空表示不该继续播放，结论码即可用作退出码。
    解析逻辑与主程序共用 src/core/resolver.py，这里不重复实现。
    """
    from src.core.resolver import resolve
    from src.core.url_router import UrlKind, classify

    kind = classify(url)
    if not force and kind is not UrlKind.WEB_PAGE:
        return url, 0

    print("=" * 68)
    print(f"解析网页地址: {url}")
    print(f"（判定类型: {kind.value}）")
    print("=" * 68)

    result = resolve(url, log=print, verbose=True, timeout_s=RESOLVE_TIMEOUT_S)
    print(result.report())
    print()

    if not result.ok:
        print("解析没有取到可直接播放的单文件流，不再继续播放。")
        return "", 1
    print(f"接下来播放解析结果: {result.url}")
    return result.url, 0


# ---------------------------------------------------------------- 实际播放测试
def probe_url(url: str, timeout_s: int) -> int:
    """实际打开地址并判定成败。

    探测逻辑放在 src/utils/playback_probe.py，与打包后的 exe 自检
    （src/utils/selfcheck.py）共用同一套判定标准，避免两处漂移。
    """
    from src.utils.playback_probe import probe_url as _probe

    print("=" * 68)
    print(f"测试地址: {url}")
    print(f"超时: {timeout_s} 秒")
    print("=" * 68)

    return _probe(url, timeout_s, log=print).code


def main() -> int:
    parser = argparse.ArgumentParser(description="环境能力探测")
    parser.add_argument("url", nargs="?", help="要测试的地址")
    parser.add_argument("--timeout", type=int, default=12, help="播放测试超时秒数")
    parser.add_argument("--debug", action="store_true", help="开启 FFmpeg 详细日志")
    parser.add_argument("--skip-static", action="store_true", help="跳过静态检查")
    parser.add_argument("--resolve", action="store_true",
                        help="无论什么地址都先用 yt-dlp 解析（页面地址默认就会解析）")
    parser.add_argument("--resolve-only", action="store_true",
                        help="只解析并打印结果，不播放")
    parser.add_argument("--no-resolve", action="store_true",
                        help="不解析，把地址直接当媒体播（用来看原始报错）")
    args = parser.parse_args()

    setup_environment(debug=args.debug)

    if not args.skip_static:
        check_environment()
        check_packages()
        check_plugins()

    if not args.url:
        return 0

    force = args.resolve or args.resolve_only
    if args.no_resolve:
        target, code = args.url, 0
    else:
        target, code = resolve_target(args.url, force=force)

    if args.resolve_only:
        return code
    if code or not target:
        return code
    return probe_url(target, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
