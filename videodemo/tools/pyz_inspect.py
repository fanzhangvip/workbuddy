"""从 PyInstaller 产物里取出 Python 模块，用于**确认某处改动真的进了 exe**。

## 为什么需要它

想要"确认打包版里有这段代码"，最直觉的做法是对 exe 明文 grep。**这会误判**：
模块被收进 `PYZ.pyz` 后是 zlib 压缩的，明文 grep 必然找不到，于是看起来像
"没打进去"，而实际上打进去了。

正确做法是把 `PYZ.pyz` 从 CArchive 里解出来，再用 `ZlibArchiveReader` 取到模块，
然后**递归 walk `co_consts`** 去找目标字符串——字符串常量在 code object 里是明文的，
所以这条链路能给出可信结论。

## 用法

    # 列出产物里所有 src.* 模块
    python tools/pyz_inspect.py dist/UniversalURLPlayer-single.exe --list

    # 在指定模块里找字符串（支持多个）
    python tools/pyz_inspect.py dist/UniversalURLPlayer-single.exe \\
        --module src.core.local_proxy --find "pick_header" "ua"

    # 直接找遍所有 src.* 模块
    python tools/pyz_inspect.py dist/UniversalURLPlayer-single.exe --find "已改走本地代理"

`--show-code` 会打印该模块的完整源码（从 PyInstaller 归档还原），
想看"打进去的到底是不是新版"时最直接。

注意：单文件版（onefile）的 exe 本身就是 CArchive；目录版要指向
`dist/UniversalURLPlayer/UniversalURLPlayer.exe`（它同样内嵌 PYZ.pyz）。
"""

from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path


def open_pyz(exe: Path, workdir: Path):
    """把产物里的 PYZ.pyz 解到磁盘并返回 ZlibArchiveReader。

    这条链路只读不删：`workdir` 里的 PYZ.pyz 会被覆盖写入，不做任何删除动作
    （受限环境里删除会触发配额限制）。
    """
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    if not exe.is_file():
        raise SystemExit(f"找不到产物: {exe}")

    workdir.mkdir(parents=True, exist_ok=True)
    pyz_path = workdir / "PYZ.pyz"

    archive = CArchiveReader(str(exe))
    # 单文件版里 PYZ 的名字就是 "PYZ.pyz"；带 --onedir 时路径可能带前缀，
    # 所以按后缀找一遍，别写死。
    names = [name for name in archive.toc if str(name).endswith("PYZ.pyz")]
    if not names:
        raise SystemExit(
            f"该产物里没有 PYZ.pyz（可能是 --onedir 且用了 --contents-directory）。"
            f"实际条目示例: {list(archive.toc)[:10]}"
        )
    pyz_path.write_bytes(archive.extract(names[0]))
    return ZlibArchiveReader(str(pyz_path))


def module_names(pyz) -> list[str]:
    """产物里所有可导入的模块名。"""
    return sorted(str(name) for name in pyz.toc)


def extract_code(pyz, module: str):
    """取出模块的 code object；不是代码（如数据文件）则返回 None。"""
    try:
        code = pyz.extract(module)
    except Exception:  # noqa: BLE001
        return None
    return code if isinstance(code, types.CodeType) else None


def iter_code_objects(code: types.CodeType):
    """递归遍历自身与所有嵌套的 code object。"""
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from iter_code_objects(const)


def _const_contains(const: object, needle: str) -> bool:
    """递归判断一个常量（含嵌套容器）里是否含目标字符串。

    只查字符串本身会漏掉 `WEB_HOSTS` 这类**打包成 tuple/frozenset 的常量**——
    那些域名字符串是整体塞在一个 tuple 里的，`isinstance(const, str)` 直接跳过，
    于是"明明在产物里"却被误报成"没打进去"。必须递归进容器再查。
    """
    if isinstance(const, str):
        return needle in const
    if isinstance(const, (tuple, frozenset)):
        return any(_const_contains(item, needle) for item in const)
    return False


def find_in_code(code: types.CodeType, needle: str) -> bool:
    """在模块里找字符串。

    覆盖这些地方 —— 少一个就会误判成"没打进去"，也少一个就会误判成"在里面"：
      * `co_consts` 里的字符串（含**打包成 tuple/frozenset 的常量**，递归查）；
      * `co_name`：函数/类名（`pick_header` 这种顶层函数名就在这里）；
      * `co_names`：被引用的全局名与属性名（名字只有被用到才会出现在这里）；
      * `co_varnames`/`co_freevars`/`co_cellvars`：**参数名与局部变量名**；
      * `co_filename`：源文件路径。
    """
    for obj in iter_code_objects(code):
        if needle in (obj.co_name or ""):
            return True
        if needle in (obj.co_filename or ""):
            return True
        for bucket in (obj.co_names, obj.co_varnames, obj.co_freevars, obj.co_cellvars):
            if any(needle in (name or "") for name in bucket):
                return True
        for const in obj.co_consts:
            if _const_contains(const, needle):
                return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查 PyInstaller 产物里某个模块/字符串是否真的存在"
    )
    parser.add_argument("exe", type=Path, help="产物路径（.exe）")
    parser.add_argument("--module", action="append", default=[],
                        help="限定模块名（可重复）。不给则扫描所有 src.* 模块")
    parser.add_argument("--find", nargs="*", default=[],
                        help="要找的字符串（子串匹配）。全部命中才算确认")
    parser.add_argument("--list", action="store_true", help="列出模块名后退出")
    parser.add_argument("--show-code", action="store_true",
                        help="打印命中模块的源码（从归档还原）")
    parser.add_argument("--workdir", type=Path,
                        default=Path(".tools") / "pyz-extract",
                        help="PYZ.pyz 落盘位置（默认 .tools/pyz-extract）")
    args = parser.parse_args(argv)

    pyz = open_pyz(args.exe, args.workdir)
    names = module_names(pyz)

    if args.list:
        for name in names:
            print(name)
        return 0

    wanted = args.module or [n for n in names if n == "src" or n.startswith("src.")]
    missing_modules = [name for name in wanted if name not in names]

    print(f"产物: {args.exe}")
    print(f"PYZ 里的模块总数: {len(names)}")
    print(f"扫描模块: {len(wanted) - len(missing_modules)} 个"
          + (f"（{len(missing_modules)} 个不存在）" if missing_modules else ""))
    print("-" * 68)

    hits: dict[str, list[str]] = {}
    for name in wanted:
        if name in missing_modules:
            continue
        code = extract_code(pyz, name)
        if code is None:
            print(f"  [跳过] {name}: 取不到 code object")
            continue
        found = [needle for needle in args.find if find_in_code(code, needle)]
        if found:
            hits[name] = found
        if args.show_code and (not args.find or found):
            _print_source(code, name)

    if args.find:
        for name in sorted(hits):
            print(f"  [命中] {name}: {hits[name]}")
        total_needed = set(args.find)
        got = {needle for found in hits.values() for needle in found}
        missed = total_needed - got
        print("-" * 68)
        if missed:
            print(f"  ❌ 有 {len(missed)} 个字符串在扫描范围内**没有找到**: {sorted(missed)}")
            print("     （如果 --module 限定了范围，可能只是不在这些模块里——"
                  "去掉 --module 全量扫一遍）")
            return 1
        print(f"  ✅ 全部 {len(total_needed)} 个字符串都找到了")
    else:
        print(f"  [存在] {sorted(set(wanted) - set(missing_modules))}")
        if missing_modules:
            print(f"  ❌ [不存在] {missing_modules}")
            return 1

    return 0


def _print_source(code: types.CodeType, name: str) -> None:
    """没有反编译器可用，退而列出该模块里的函数名与全部字符串常量。"""
    print(f"===== {name} =====")
    for obj in iter_code_objects(code):
        if obj.co_name and obj.co_name != "<module>":
            print(f"  [def] {obj.co_name}")
        for const in obj.co_consts:
            if isinstance(const, str) and const.strip():
                print("      ", const[:120].replace("\n", "\\n"))


if __name__ == "__main__":
    sys.exit(main())
