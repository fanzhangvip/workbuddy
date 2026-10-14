#!/usr/bin/env python
"""一键打包为 Windows 独立可执行文件（UI-10 ~ UI-16）。

用法::

    python scripts/build_exe.py            # onedir 目录版（默认，推荐挂机）
    python scripts/build_exe.py --onefile  # onefile 单文件绿色版
    python scripts/build_exe.py --clean    # 先清理 build/dist 再打包
    python scripts/build_exe.py --demo     # 打包「教学演示版」（见下）

产物：
    dist/TicketHelper/TicketHelper.exe              （onedir）
    dist/TicketHelper.exe                           （onefile）
    dist/TicketHelper-Demo/TicketHelper-Demo.exe    （--demo）
    dist/TicketHelper-Demo.exe                      （--onefile --demo）

发给别人怎么选：**单文件版（``--onefile``）只需发一个 exe**；目录版必须整个
目录一起发 —— exe 只是启动器，Python 本体（``_internal\\python313.dll``）和依赖
都在目录里，单独拷 exe 到别的机器会报「Failed to load Python DLL」。代价是单文件
首次启动要自解压十几秒，且被杀软误报的概率比目录版高一些（见下面「为什么默认 onedir」）。

教学演示版（--demo）
-------------------
录播课程要求界面能**长期复用**，所以演示版把界面上所有乘车日期显示成「明天」。
实现方式：打包期间把 ``ticket_helper/edition.py`` 的 ``DEMO_MODE`` 临时改成
``True``（打包结束**一定**还原，见 :class:`_DemoEdition`），再让 spec 把产物名
与 exe 文件属性改成带「教学演示版」的样子。**只改显示、不动数据** —— 任务里
存的仍是真实日期，抢票链路一行没变。

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

#: 教学演示版的产物名（目录名与两个 exe 一起变，正式版不受影响）
DEMO_BUILD_NAME = "TicketHelper-Demo"
#: 演示版写进 exe 文件属性的版本后缀
EDITION_LABEL = "教学演示版"
#: 演示开关在源码里的原文（唯一锚点；改 edition.py 那一行时同步这里）
DEMO_MODE_ANCHOR = "DEMO_MODE = False"


class _DemoEdition:
    """打包「教学演示版」期间把 ``edition.DEMO_MODE`` 置为 ``True``。

    为什么动源码而不是读环境变量：**打包产物运行时不会带着构建机的环境变量**，
    做成运行期开关只会得到一个「明明设了却没生效」的包，写进代码才可靠。

    用 ``with`` 而不是「打包前改、打包后改回」两段式：中途任何一个 return /
    异常都会走 ``__exit__``，源文件**一定**被还原 —— 否则会留下一个「默认就是
    演示版」的工作区，下次出正式版时悄悄打错包。
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.path = ROOT / "ticket_helper" / "edition.py"
        self.original: str | None = None

    def __enter__(self) -> "_DemoEdition":
        if not self.enabled:
            return self
        raw = self.path.read_text(encoding="utf-8", newline="")
        if "DEMO_MODE = True" in raw:
            print("edition.py 已经是演示态，跳过改写")
            return self
        assert raw.count(DEMO_MODE_ANCHOR) == 1, (
            f"{self.path} 里找不到唯一锚点 {DEMO_MODE_ANCHOR!r} —— 打包脚本请同步更新"
        )
        self.original = raw
        self.path.write_text(raw.replace(DEMO_MODE_ANCHOR, "DEMO_MODE = True"), encoding="utf-8", newline="")
        print(f"已切到教学演示版：{DEMO_MODE_ANCHOR} → DEMO_MODE = True")
        return self

    def __exit__(self, *exc: object) -> bool:
        if self.original is None:
            return False
        self.path.write_text(self.original, encoding="utf-8", newline="")
        print("已还原 ticket_helper/edition.py（默认正式版）")
        return False


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
    """把已存在的产物路径（目录或单文件 exe）改名让路，而不是删除。

    PyInstaller 在 COLLECT 阶段会 ``shutil.rmtree`` 掉旧的输出目录。
    在企业沙箱 / 受管控的机器上，批量删除会被安全策略拦截，
    结果是「打包到 99% 突然失败」，报错还看不出真正原因。
    改名是等价且永远可用的做法（新目录名带时间戳，不会互相覆盖）。

    ⚠️ 只对**本次要产出的那个路径**调用。目录版是 ``dist/TicketHelper/``、
    单文件版是 ``dist/TicketHelper.exe`` —— 两者同名不同路径、互不冲突；
    若不分形态一律按 ``build_name`` 让路，打单文件包时会把还能用的目录版
    整目录改名扔掉（dist 里攒一堆 ``.old-``），这正是本轮修掉的问题。
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


def _produced_after(target: Path, started: float, build_name: str) -> bool:
    """产物是否是在 ``started`` 之后新写出来的（用来判断"非 0 返回码"是否真的失败）。"""
    if not target.exists():
        return False
    if target.is_file():
        return target.stat().st_mtime >= started - 1
    markers = [target / f"{build_name}.exe", target / f"{build_name}-CLI.exe"]
    return all(marker.exists() and marker.stat().st_mtime >= started - 1 for marker in markers)


def build(onefile: bool, clean: bool, demo: bool = False) -> int:
    if clean:
        for path in (DIST, BUILD):
            _move_aside(path)

    if not ensure_requirements():
        return 1

    build_name = DEMO_BUILD_NAME if demo else "TicketHelper"
    target = DIST / (f"{build_name}.exe" if onefile else build_name)
    print(f"产物形态：{'教学演示版（界面上的乘车日期显示为「明天」）' if demo else '正式版'}"
          f" · {'单文件 exe' if onefile else '目录版'}")

    # 无论是否 --clean，都要保证 PyInstaller 的目标路径不存在，
    # 否则它会尝试删除（可能被拦截）而不是直接写入。
    # 注意让路的是**本次真正的产物路径**（单文件是 .exe、目录版是同名目录）。
    _move_aside(target)
    _move_aside(BUILD / build_name)

    started = time.time()

    # ⚠️ 演示开关只在这一段里是开的：with 保证任何 return / 异常都会还原源码
    with _DemoEdition(demo):
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
        environment["TICKET_HELPER_BUILD_NAME"] = build_name
        if demo:
            environment["TICKET_HELPER_EDITION"] = EDITION_LABEL
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
        if onefile:
            print(f"提示：--onefile 只产出图形界面版；命令行子命令请使用默认目录版的 {build_name}-CLI.exe")
            environment["TICKET_HELPER_ONEFILE"] = "1"
        command += [str(SPEC)]

        code = run(command, env=environment)

    if code != 0:
        # PyInstaller 收尾会 rmtree 掉目标目录，这在企业沙箱 / 受管控机器上会被安全策略
        # 拦截并让进程返回非 0 —— **产物其实已经写好了**。直接报「打包失败」会把人带去查
        # 一个根本不存在的打包错误（本轮实际踩到过），所以先核对产物再下结论。
        if _produced_after(target, started, build_name):
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
            for name in (f"{build_name}.exe", f"{build_name}-CLI.exe"):
                marker = "√" if (target / name).exists() else "×"
                print(f"  {marker} {name}")
        else:
            print(f"文件大小：{target.stat().st_size / 1024 / 1024:.1f} MB")

    print("\n验收提示（对应测试方案「场景 6」）：")
    if onefile:
        print("  1. **只拷这一个 exe 文件**就能在未安装 Python 的 Windows 上运行")
        print("     （首次启动会自解压到临时目录，约十几秒，之后正常）；")
    else:
        print("  1. **必须整个目录一起拷**（含 _internal\\）：exe 只是启动器，")
        print("     单独拷 exe 会报「Failed to load Python DLL ... python313.dll」；")
    print(f"  2. **双击 {build_name}.exe** → 应直接打开桌面客户端窗口（不是黑窗一闪而过）；")
    if not onefile:
        print(f"  3. 命令行用 {build_name}-CLI.exe（web / desktop / doctor / init / grab / mock）；")
    print("  4. 确认配置/数据库/日志写在 %USERPROFILE%\\.ticket-helper；")
    print("  5. 关闭重开，数据不丢。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 智泊AI抢票助手")
    parser.add_argument("--onefile", action="store_true", help="产出单文件 exe（默认目录版）")
    parser.add_argument("--clean", action="store_true", help="打包前清理 build/dist")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="打包教学演示版：界面上所有乘车日期显示为「明天」，产物为 dist/TicketHelper-Demo",
    )
    args = parser.parse_args(argv)
    return build(args.onefile, args.clean, demo=args.demo)


if __name__ == "__main__":
    raise SystemExit(main())
