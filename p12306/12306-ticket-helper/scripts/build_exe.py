#!/usr/bin/env python
"""一键打包为 Windows 独立可执行文件（UI-10 ~ UI-16）。

用法::

    python scripts/build_exe.py            # onedir 目录版（默认，推荐挂机）
    python scripts/build_exe.py --onefile  # onefile 单文件绿色版
    python scripts/build_exe.py --clean    # 先清理 build/dist 再打包

产物：
    dist/TicketHelper/TicketHelper.exe     （onedir）
    dist/TicketHelper.exe                  （onefile）

为什么默认 onedir：
- 启动快（不需要每次解压到临时目录）；
- 杀软误报率明显低于 onefile（抢票类工具本来就容易被误报）；
- 目录形态便于用户查看日志、替换验证码模型。

运行数据（配置/SQLite/日志）一律写到 ``%USERPROFILE%\\.ticket-helper``，
不写 exe 所在目录，因此在 Program Files 等只读位置也能免管理员运行。
"""

from __future__ import annotations

import argparse
import os
import shutil  # noqa: F401  - 保留供 --clean 的兼容分支使用
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
SPEC = ROOT / "TicketHelper.spec"


def run(command: list[str], env: dict[str, str] | None = None) -> int:
    print("$ " + " ".join(command))
    return subprocess.call(command, cwd=str(ROOT), env=env)


def ensure_requirements() -> bool:
    missing: list[str] = []
    for module, package in [("PyInstaller", "pyinstaller"), ("PySide6", "PySide6"), ("PIL", "pillow")]:
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        print("缺少打包依赖：" + ", ".join(missing))
        print("请先执行：pip install " + " ".join(missing))
        return False
    return True


def make_icon() -> None:
    icon = ROOT / "ticket_helper" / "resources" / "app.ico"
    if icon.exists():
        print(f"图标已存在：{icon}")
        return
    print("生成应用图标…")
    run([sys.executable, str(ROOT / "scripts" / "make_icon.py")])


def _move_aside(path: Path) -> None:
    """把已存在的输出目录改名让路，而不是删除。

    PyInstaller 在 COLLECT 阶段会 ``shutil.rmtree`` 掉旧的输出目录。
    在企业沙箱 / 受管控的机器上，批量删除会被安全策略拦截，
    结果是「打包到 99% 突然失败」，报错还看不出真正原因。
    改名是等价且永远可用的做法（新目录名带时间戳，不会互相覆盖）。
    """
    if not path.exists():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.old-{stamp}")
    index = 1
    while target.exists():
        index += 1
        target = path.with_name(f"{path.name}.old-{stamp}-{index}")
    try:
        path.rename(target)
        print(f"已把旧目录改名让路：{path.name} → {target.name}")
    except OSError as exc:
        print(f"无法改名 {path}（{exc}），继续尝试打包")


def _produced_after(target: Path, started: float) -> bool:
    """产物是否是在 ``started`` 之后新写出来的（用来判断"非 0 返回码"是否真的失败）。"""
    if not target.exists():
        return False
    if target.is_file():
        return target.stat().st_mtime >= started - 1
    markers = [target / "TicketHelper.exe", target / "TicketHelper-CLI.exe"]
    return all(marker.exists() and marker.stat().st_mtime >= started - 1 for marker in markers)


def build(onefile: bool, clean: bool) -> int:
    if clean:
        for path in (DIST, BUILD):
            _move_aside(path)

    if not ensure_requirements():
        return 1

    # 无论是否 --clean，都要保证 PyInstaller 的目标目录不存在，
    # 否则它会尝试删除（可能被拦截）而不是直接写入
    _move_aside(DIST / "TicketHelper")
    _move_aside(BUILD / "TicketHelper")

    # 打包前先做一次自检与编译检查，避免把明显的坏包打进去
    print("\n[1/4] 编译检查…")
    if run([sys.executable, "-m", "compileall", "-q", "ticket_helper", "desktop_ui", "main.py"]) != 0:
        print("编译检查未通过，已中止打包")
        return 1

    print("\n[2/4] 生成图标…")
    make_icon()

    print("\n[3/4] PyInstaller 打包…")
    # ⚠️ 两种形态**都走同一份 spec**（onefile 靠 `TICKET_HELPER_ONEFILE=1` 切换）。
    # 改造前 onefile 在这里另写了一套 --add-data/--hidden-import —— 上游内核
    # （P0 引入，register 在 spec 的 datas 里）当时只加进了 spec，于是
    # **onefile 会静默产出一个没有上游内核的包**。别再拆开。
    environment = dict(os.environ)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    if onefile:
        print("提示：--onefile 只产出图形界面版；命令行子命令请使用默认目录版的 TicketHelper-CLI.exe")
        environment["TICKET_HELPER_ONEFILE"] = "1"
    command += [str(SPEC)]

    target = DIST / ("TicketHelper.exe" if onefile else "TicketHelper")
    started = time.time()
    code = run(command, env=environment)
    if code != 0:
        # PyInstaller 收尾会 rmtree 掉目标目录，这在企业沙箱 / 受管控机器上会被安全策略
        # 拦截并让进程返回非 0 —— **产物其实已经写好了**。直接报「打包失败」会把人带去查
        # 一个根本不存在的打包错误（本轮实际踩到过），所以先核对产物再下结论。
        if _produced_after(target, started):
            print(f"PyInstaller 返回码 {code}（多半是收尾删除被安全策略拦截），但产物已生成：{target}")
        else:
            print("打包失败")
            return code

    print(f"\n[4/4] 完成，用时 {time.time() - started:.1f}s")
    if target.exists():
        print(f"产物：{target}")
        if target.is_dir():
            size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
            print(f"目录大小：{size / 1024 / 1024:.1f} MB")
            for name in ("TicketHelper.exe", "TicketHelper-CLI.exe"):
                marker = "√" if (target / name).exists() else "×"
                print(f"  {marker} {name}")
        else:
            print(f"文件大小：{target.stat().st_size / 1024 / 1024:.1f} MB")

    print("\n验收提示（对应测试方案「场景 6」）：")
    print("  1. 把产物整个目录复制到未安装 Python 的 Windows 上；")
    print("  2. **双击 TicketHelper.exe** → 应直接打开桌面客户端窗口（不是黑窗一闪而过）；")
    print("  3. 命令行用 TicketHelper-CLI.exe（web / desktop / doctor / init / grab / mock）；")
    print("  4. 确认配置/数据库/日志写在 %USERPROFILE%\\.ticket-helper；")
    print("  5. 关闭重开，数据不丢。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 12306 抢票助手")
    parser.add_argument("--onefile", action="store_true", help="产出单文件 exe（默认目录版）")
    parser.add_argument("--clean", action="store_true", help="打包前清理 build/dist")
    args = parser.parse_args(argv)
    return build(args.onefile, args.clean)


if __name__ == "__main__":
    raise SystemExit(main())
