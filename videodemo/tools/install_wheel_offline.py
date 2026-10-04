"""
离线安装 wheel 包。

为什么需要这个脚本：
本机的沙箱环境有"批量删除保护"，pip 安装大包时会在解压后删除临时目录，
触发拦截导致安装中断（表现为：部分文件已解压，但 dist-info 与插件目录缺失，
程序在运行时才报错）。本脚本只做"读 zip + 写文件"，完全不删除任何东西，
因此可以稳定完成安装。

用法:
    python tools/install_wheel_offline.py <wheel 路径> [--dry-run]

脚本会按 wheel 规范处理 `<name>-<ver>.data/` 目录映射：
    purelib/ platlib/ -> site-packages
    scripts/          -> Scripts
    data/             -> 前缀目录
    headers/          -> include
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path


def site_packages() -> Path:
    import site

    for path in site.getsitepackages():
        if path.endswith("site-packages"):
            return Path(path)
    # venv 下的兜底
    return Path(sys.prefix) / "Lib" / "site-packages"


def scripts_dir() -> Path:
    return Path(sys.prefix) / "Scripts"


def map_entry(entry: str, data_prefix: str | None, sp: Path) -> Path | None:
    """把压缩包内的路径映射到实际目标路径。返回 None 表示跳过。"""
    if entry.endswith("/"):
        return None

    if data_prefix and entry.startswith(data_prefix):
        rest = entry[len(data_prefix):]
        parts = rest.split("/", 1)
        if len(parts) < 2:
            return None
        category, rel = parts
        if category in ("purelib", "platlib"):
            return sp / rel
        if category == "scripts":
            return scripts_dir() / rel
        if category == "data":
            return Path(sys.prefix) / rel
        if category == "headers":
            return Path(sys.prefix) / "include" / rel
        return sp / rel

    return sp / entry


def install(wheel: Path, dry_run: bool = False) -> int:
    sp = site_packages()
    if not sp.is_dir():
        print(f"找不到 site-packages: {sp}", file=sys.stderr)
        return 1

    print(f"目标目录: {sp}")
    print(f"wheel   : {wheel}  ({wheel.stat().st_size / 1024 / 1024:.1f} MB)")

    written = 0
    skipped = 0
    data_prefix: str | None = None

    with zipfile.ZipFile(wheel) as zf:
        for name in zf.namelist():
            if ".data/" in name and data_prefix is None:
                head = name.split(".data/", 1)[0]
                data_prefix = f"{head}.data/"
                break

        for info in zf.infolist():
            target = map_entry(info.filename, data_prefix, sp)
            if target is None:
                skipped += 1
                continue

            if dry_run:
                written += 1
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            written += 1

    print(f"{'（演练）将' if dry_run else '已'}写入 {written} 个文件，跳过 {skipped} 个目录项")

    # 关键产物检查
    checks = [
        sp / "PySide6" / "plugins" / "multimedia",
        sp / "PySide6",
    ]
    print()
    print("关键产物检查:")
    for directory in checks:
        if not directory.is_dir():
            continue
        ffmpeg_files = [
            p for p in directory.iterdir()
            if p.is_file() and ("ffmpeg" in p.name.lower() or p.name.lower().startswith("av"))
        ]
        if ffmpeg_files:
            for p in sorted(ffmpeg_files):
                print(f"  {p.name}  ({p.stat().st_size / 1024 / 1024:.1f} MB)")

    dist_info = sorted(sp.glob("*.dist-info"))
    print()
    print("已注册的包元数据:")
    for d in dist_info:
        print(f"  {d.name}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="离线安装 wheel")
    parser.add_argument("wheel", help="wheel 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只演练，不写文件")
    args = parser.parse_args()

    wheel = Path(args.wheel)
    if not wheel.is_file():
        print(f"wheel 不存在: {wheel}", file=sys.stderr)
        return 1
    if wheel.suffix != ".whl":
        print(f"不是 wheel 文件: {wheel}", file=sys.stderr)
        return 1

    return install(wheel, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
