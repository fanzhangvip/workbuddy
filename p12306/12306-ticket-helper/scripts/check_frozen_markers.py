"""冻结物取证：确认源码里的修复**真的进了包**。

为什么需要它：`grep 源码` 只能证明源码改了；而 PyInstaller 的 PYZ 是 zlib 压缩的，
裸 `grep` 二进制**看不到**这些字符串 —— 于是「源码对、包是旧的」这种事故
在打包日志里完全不显形。这里把 PYZ 里的代码对象解出来，逐个找标记。

产物形态说明：PyInstaller 6 的 onedir 布局里 **PYZ 在 exe 内部**（CArchive），
`_internal/` 下只有二进制。所以要先从 exe 里取出 ``PYZ.pyz`` 再读它。

用法::

    python scripts/check_frozen_markers.py                 # 默认 dist/TicketHelper
    python scripts/check_frozen_markers.py --dist <目录>
"""

from __future__ import annotations

import argparse
import io
import marshal
import sys
import tempfile
from pathlib import Path
from types import CodeType

ROOT = Path(__file__).resolve().parent.parent

#: 模块 → 必须出现在该模块代码对象里的标记
#:
#: 每条都对应一次**真实故障**，而且都是「离线全绿、真机必挂」那一类：
#:   #60 下单验证码的真实路径与 rand（错路径真机只回 HTML 错误页）
#:   #61 leftTicketStr 的真正来源 yp_info（索引 12，不是席别列表）
#:   #63 回环地址不走代理
#:   #62 桌面端「今天」的唯一来源
MARKERS: dict[str, tuple[str, ...]] = {
    "ticket_helper.inter.urls": ("/otn/passcodeNew/getPassCodeNew", "randp"),
    "ticket_helper.inter.parser": ("yp_info",),
    "ticket_helper.inter.api": ("yp_info", "_format_train_date_js"),
    "ticket_helper.inter.base": ("_is_loopback_host",),
    "desktop_ui.token_field": ("_today_qdate", "_on_calendar_picked"),
    "desktop_ui.api_client": ("trust_env",),
}


def _strings_of(code: object) -> set[str]:
    """递归收集代码对象里出现的所有字符串（常量 / 变量名 / 函数名）。"""

    found: set[str] = set()
    stack: list[object] = [code]
    budget = 400000
    while stack and budget > 0:
        budget -= 1
        item = stack.pop()
        if isinstance(item, CodeType):
            found.add(item.co_name)
            found.update(item.co_names)
            found.update(item.co_varnames)
            for const in item.co_consts:
                if isinstance(const, str):
                    found.add(const)
                elif isinstance(const, CodeType):
                    stack.append(const)
        elif isinstance(item, str):
            found.add(item)
    return found


class _Pyz:
    """把 PYZ 读成「模块名 → 代码对象」。"""

    def __init__(self, dist: Path) -> None:
        self.origin = ""
        self.toc: dict[str, object] = {}
        self._reader = None
        self._raw: dict[str, bytes] = {}

        pyz_files = sorted(dist.rglob("PYZ*.pyz"))
        if pyz_files:
            self._open_pyz(pyz_files[0])
            return

        exes = sorted(dist.glob("*.exe"))
        if not exes:
            raise SystemExit(f"{dist} 里既没有 PYZ 也没有 exe，打包没成功吧？")
        from PyInstaller.archive.readers import CArchiveReader

        archive = CArchiveReader(str(exes[0]))
        payload = archive.extract("PYZ.pyz")
        if not payload:
            raise SystemExit(f"{exes[0].name} 里没有 PYZ.pyz 条目")
        tmp = Path(tempfile.gettempdir()) / "ticket_helper_frozen_probe.pyz"
        with io.open(tmp, "wb") as fh:
            fh.write(payload)
        self.origin = f"{exes[0].name} → PYZ.pyz（{len(payload)} 字节）"
        self._open_pyz(tmp)

    def _open_pyz(self, path: Path) -> None:
        from PyInstaller.archive.readers import ZlibArchiveReader

        reader = ZlibArchiveReader(str(path))
        self._reader = reader
        self.toc = dict(getattr(reader, "toc", {}) or {})
        if not self.origin:
            self.origin = str(path)

    def code_of(self, module: str) -> object | None:
        if self._reader is not None:
            try:
                return self._reader.extract(module)
            except Exception:
                return None
        raw = self._raw.get(module)
        if raw is None:
            return None
        try:
            return marshal.loads(raw[16:])  # 跳过 pyc 头
        except Exception:
            return None


def main() -> int:
    parser = argparse.ArgumentParser(description="冻结物标记取证")
    parser.add_argument("--dist", default=str(ROOT / "dist" / "TicketHelper"))
    args = parser.parse_args()

    dist = Path(args.dist)
    if not dist.exists():
        print(f"✘ 目录不存在：{dist}")
        return 1

    pyz = _Pyz(dist)
    print(f"PYZ 来源：{pyz.origin}")
    print(f"模块条目：{len(pyz.toc)} 个")
    print()

    missing: list[str] = []
    for module, marks in MARKERS.items():
        code = pyz.code_of(module)
        if code is None:
            print(f"  ✘ {module}: 解不出代码对象（模块没进包？）")
            missing.extend(f"{module}::{m}" for m in marks)
            continue
        pool = _strings_of(code)
        hits = " ".join("%s=%s" % (m, "有" if m in pool else "缺") for m in marks)
        ok = all(m in pool for m in marks)
        print(f"  {'✔' if ok else '✘'} {module}: {hits}")
        if not ok:
            missing.extend(f"{module}::{m}" for m in marks if m not in pool)

    print()
    if missing:
        print(f"✘ 有 {len(missing)} 个标记不在冻结物里 —— 源码改了但包是旧的：")
        for item in missing:
            print("    -", item)
        return 1
    print("✔ 冻结物里的标记全在：源码的修复真的进了包。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
