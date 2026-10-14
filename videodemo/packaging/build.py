"""一键打包脚本。

用法（务必用项目 .venv 的解释器）:

    ./.venv/Scripts/python.exe packaging/build.py                 # 打包两种形态并验证
    ./.venv/Scripts/python.exe packaging/build.py --onedir        # 只打目录版（推荐日常用）
    ./.venv/Scripts/python.exe packaging/build.py --onefile       # 只打单文件版
    ./.venv/Scripts/python.exe packaging/build.py --no-verify     # 不自动验证
    ./.venv/Scripts/python.exe packaging/build.py --fresh-work    # 受限环境（沙箱）用

产物:
    dist/UniversalURLPlayer/UniversalURLPlayer.exe      目录版
    dist/UniversalURLPlayer-single.exe                  单文件版

打包后会自动跑一次产物自检（因为 exe 是窗口程序，结果通过自检报告文件读取）:
    %APPDATA%/UniversalURLPlayer/logs/selfcheck.txt
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DIST_DIR = ROOT / "dist"
WORK_DIR = ROOT / "build"

#: 形态 -> (spec 文件, 产物路径)
#: 目录版报整个目录（exe 本体只有 2 MB，真正的大头在 _internal 里），
#: 单文件版报那个 exe。
SPECS = {
    "onedir": (
        HERE / "UniversalURLPlayer.spec",
        DIST_DIR / "UniversalURLPlayer",
    ),
    "onefile": (
        HERE / "UniversalURLPlayer-onefile.spec",
        DIST_DIR / "UniversalURLPlayer-single.exe",
    ),
}

#: 验证时运行的 exe 名（目录版在目录里）
EXE_NAME = "UniversalURLPlayer.exe"

#: 验证用的样例视频（本地生成的测试片，随 .tools 一起 gitignore）
SAMPLE = ROOT / ".tools" / "testmedia" / "local_test.mp4"

#: 报告目录（每次构建写独立文件，避免删除动作）
REPORT_DIR = WORK_DIR / "reports"

#: 与 src/utils/selfcheck.py 约定的环境变量：指定自检报告输出路径
REPORT_ENV = "VIDEODEMO_SELFCHECK_REPORT"


def human(size_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def describe_artifact(target: Path) -> str:
    """汇报产物体积（目录版统计整个目录）。"""
    if target.is_file():
        return f"{human(target.stat().st_size)}  {target}"
    if target.is_dir():
        files = [p for p in target.rglob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        return f"{human(total)}  {target}  （{len(files)} 个文件）"
    return f"未找到产物: {target}"


def exe_of(artifact: Path) -> Path:
    """从产物路径取出可执行文件路径。"""
    return artifact / EXE_NAME if artifact.is_dir() else artifact


def clear_target(target: Path, *, avoid_bulk_delete: bool) -> None:
    """把上次的产物挪开，让 PyInstaller 每次都产出全新目录。

    直接交给 PyInstaller 去删旧目录，在受限环境（沙箱对"批量删除"有配额限制）
    里会让整个构建中途失败，而且错误信息很隐晦。
    改名只是一次目录操作，不受影响。

    Args:
        target: 上次的产物路径。
        avoid_bulk_delete: True 时不删除挪开的目录（受限环境用），只改名。
    """
    if not target.exists():
        return

    stamp = time.strftime("%Y%m%d-%H%M%S")
    stale = target.with_name(f"{target.name}.stale-{stamp}")
    try:
        target.replace(stale)
    except OSError as exc:
        print(f"[清理] 旧产物无法挪开（{exc}），交由 PyInstaller 自行处理")
        return

    print(f"[清理] 旧产物已挪开: {stale.name}")
    if avoid_bulk_delete:
        print("[清理] 受限模式：不删除挪开的目录，可稍后手动清理")
        return

    shutil.rmtree(stale, ignore_errors=True)
    if stale.exists():
        print(f"[清理] 提示: {stale} 未能删除，可稍后手动清理")


def work_path_for(spec: Path, *, fresh: bool) -> Path:
    """中间目录。

    非 fresh 模式复用固定路径，PyInstaller 能做增量分析；
    fresh 模式每次用新目录，全程不触发任何批量删除。
    """
    base = WORK_DIR / spec.stem
    if not fresh:
        return base
    return WORK_DIR / f"{spec.stem}-{time.strftime('%Y%m%d-%H%M%S')}"


def run_pyinstaller(spec: Path, work_path: Path) -> int:
    """调用 PyInstaller。"""
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(work_path),
        str(spec),
    ]
    print("\n" + "=" * 72)
    print("执行: " + " ".join(command))
    print("=" * 72, flush=True)
    return subprocess.run(command, cwd=str(ROOT)).returncode


def verify_symbols(exe: Path, kind: str) -> bool:
    """确认清单里的模块与符号**真的在产物里**。

    自检证明"产物能跑"，但证明不了"我这次改的那段代码在里面"。而**明文 grep
    exe 是查不出来的**——模块收进 `PYZ.pyz` 后是 zlib 压缩的，grep 必然找不到，
    会得出"没打进去"的错误结论。

    所以这里解出 PYZ，按 `pack_config.REQUIRED_SYMBOLS` 逐条核对。
    失败只是警告不中止构建：这是防"静默丢模块"，不是构建成败的判据。
    """
    sys.path.insert(0, str(ROOT / "tools"))
    sys.path.insert(0, str(HERE))
    try:
        from pack_config import REQUIRED_SYMBOLS  # noqa: PLC0415
        from pyz_inspect import extract_code, find_in_code, module_names, open_pyz
    except ImportError as exc:  # noqa: BLE001
        print(f"[符号核对] 跳过：无法导入检查工具 -> {exc}")
        return True

    print("\n" + "-" * 72)
    print("[符号核对] 确认 P3–P6 的代码真的在产物里")
    print("-" * 72)

    try:
        pyz = open_pyz(exe, WORK_DIR / "pyz-inspect")
        available = set(module_names(pyz))
    except Exception as exc:  # noqa: BLE001
        print(f"[符号核对] 跳过：无法读取产物归档 -> {exc}")
        return True

    missing: list[str] = []
    checked = 0
    for module, symbols in REQUIRED_SYMBOLS.items():
        if module not in available:
            missing.append(f"{module}（整个模块不在产物里）")
            continue
        code = extract_code(pyz, module)
        if code is None:
            missing.append(f"{module}（取不到 code object）")
            continue
        for symbol in symbols:
            checked += 1
            if not find_in_code(code, symbol):
                missing.append(f"{module}.{symbol}")

    if missing:
        print(f"  ❌ {len(missing)} 项没找到（已核对 {checked} 项）:")
        for item in missing:
            print(f"       - {item}")
        print("     这可能意味着该模块被漏收、被缓存成旧版、或关键函数被改名。")
        return False

    print(f"  ✅ 全部 {checked} 项符号都在产物里（{len(REQUIRED_SYMBOLS)} 个模块）")
    return True


def verify(exe: Path, kind: str) -> bool:
    """运行产物自检并读取报告。

    产物是 console=False 的窗口程序，从脚本里调用时没有可附加的父控制台，
    所以不靠 stdout，而是让自检把结果写进报告文件再读回来。

    报告路径通过环境变量指定为本次构建专属的新文件，全程不删除任何东西
    （受限环境里删除动作会触发配额限制，进而让验证失败）。
    """
    if not exe.is_file():
        print(f"[验证] 跳过：产物不存在 {exe}")
        return False

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / f"selfcheck-{kind}-{time.strftime('%Y%m%d-%H%M%S')}.txt"

    print("\n" + "=" * 72)
    print(f"[验证] 运行产物自检: {exe.name}")
    print("=" * 72, flush=True)

    command = [str(exe), "--self-check", "--offscreen"]
    if SAMPLE.is_file():
        command.append(str(SAMPLE))
    else:
        print(f"[验证] 未找到样例视频 {SAMPLE}，只做结构自检")

    env = dict(os.environ)
    env[REPORT_ENV] = str(report)

    try:
        proc = subprocess.run(command, cwd=str(exe.parent), env=env, timeout=180)
    except subprocess.TimeoutExpired:
        print("[验证] 失败：产物自检超时（180 秒）")
        return False
    except OSError as exc:
        print(f"[验证] 失败：无法启动产物 -> {exc}")
        return False

    if report.is_file():
        print("\n----- 自检报告 -----")
        print(report.read_text(encoding="utf-8", errors="replace"))
        print("----- 报告结束 -----")
    else:
        print(f"[验证] 失败：未生成自检报告 {report}（产物可能启动即崩溃）")
        return False

    ok = proc.returncode == 0
    print(f"[验证] 退出码 {proc.returncode} -> {'通过' if ok else '存在问题'}")

    # 再加一道：自检跑通 ≠ 我改的代码在里面（见 verify_symbols 的说明）
    symbols_ok = verify_symbols(exe, kind)
    return ok and symbols_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="打包智泊AI视频播放器")
    parser.add_argument("--onedir", action="store_true", help="只打目录版")
    parser.add_argument("--onefile", action="store_true", help="只打单文件版")
    parser.add_argument("--no-verify", action="store_true", help="不自动验证产物")
    parser.add_argument(
        "--fresh-work",
        action="store_true",
        help="每次使用全新的中间目录，全程不做批量删除（受限/沙箱环境必备）",
    )
    args = parser.parse_args()

    if args.onedir or args.onefile:
        selected = []
        if args.onedir:
            selected.append("onedir")
        if args.onefile:
            selected.append("onefile")
    else:
        selected = ["onedir", "onefile"]

    if not (ROOT / "main.py").is_file():
        print(f"错误：找不到入口脚本 {ROOT / 'main.py'}，请在项目内运行本脚本。")
        return 2

    started = time.time()
    results: dict[str, tuple[bool, str]] = {}

    for kind in selected:
        spec, target = SPECS[kind]
        clear_target(target, avoid_bulk_delete=args.fresh_work)
        work_path = work_path_for(spec, fresh=args.fresh_work)
        code = run_pyinstaller(spec, work_path)
        built = code == 0 and exe_of(target).is_file()

        if not built:
            results[kind] = (False, f"构建失败（退出码 {code}）")
            continue

        summary = describe_artifact(target)
        print(f"\n[产物] {summary}")
        results[kind] = (True, summary)

        if not args.no_verify:
            results[kind] = (True, summary) if verify(exe_of(target), kind) else (
                False,
                f"{summary} —— 但自检未通过",
            )

    print("\n" + "=" * 72)
    print(f"打包汇总（耗时 {time.time() - started:.1f} 秒）")
    print("=" * 72)
    failed = 0
    for kind in selected:
        ok, note = results[kind]
        if not ok:
            failed += 1
        print(f"  [{'成功' if ok else '失败'}] {kind:<8} {note}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
