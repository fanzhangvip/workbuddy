#!/usr/bin/env python
"""打包完整性自检（P5）。

## 为什么需要它

上游 `vendor/pjialin/py12306` **不是被静态 import 进来的**：垫片
（`kernel/pjialin/shim.py`）把一个目录挂上 `sys.path`，各 pjialin 后端在
**方法内部**才 `import py12306.xxx`。

对 PyInstaller 来说，这意味着两件事同时成立：

1. 它的静态分析**根本看不到** `py12306` 及其第三方依赖
   （`requests_html` / `redis` / `pyppeteer` / `pyquery` / …），
   于是这些包不会被自动收进来；
2. `vendor/` 目录本身是**数据**而不是 Python 包，得显式写进 `datas`，
   否则冻结后 `VENDOR_ROOT` 指向一个空目录（`shim.is_available()` 为假，
   所有 pjialin 后端**静默回退**到 `self` —— 不报错，只是行为变了）。

两条合起来 = **源码跑得好好的，打包后一点「打码」或「过滑块」就
ModuleNotFoundError（或者更糟：静默回退成另一个后端）**，而那时用户已经在挂了。

## 它做什么

- 把**真实的 `TicketHelper.spec` 执行一遍**（stub 掉 PyInstaller 注入的
  `Analysis`/`PYZ`/`EXE`/`COLLECT`，捕获 `Analysis` 的 kwargs）——
  读的是 spec 的实际取值，不是正则匹配文本；
- 从本项目源码里找出**真正会去 import 上游**的模块（含 `upstream_module("py12306.x")`
  这种字符串引用），在 vendor 内部**做可达性分析**，只对**可达**的第三方
  依赖要求归属。

  为什么非要可达性：上游自带一整套 web 后台（`py12306/web/**`）和两个我们
  不用的通知渠道。它们 import `flask_jwt_extended` / `DingtalkChatbot`，
  但**永远走不到**。把「上游源码里出现了 import X」当成「我们需要 X」，
  会让豁免清单变成橡皮图章 —— 而真正的遗漏就淹没在里面。

  ⚠️ 方向性：可达性分析只用来**放行**，不用来**新增**豁免。
  可达集合算错只会让检查更严（多要求几个包），不会漏掉真需要的包。

用法::

    python scripts/check_packaging.py            # 人读的报告
    python scripts/check_packaging.py --quiet    # 只出错时才输出（给 CI/门用）
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import deque
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
SPEC_PATH = BASE / "TicketHelper.spec"
VENDOR_ROOT = BASE / "vendor" / "pjialin"
HELPER_ROOT = BASE / "ticket_helper"

#: 本项目源码里对上游模块的引用（**含字符串**：`upstream_module("py12306.helpers.station")`）。
#: 只扫 import 语句会漏掉那种写法。
#:
#: ⚠️ 必须是**整条点分路径**（`*` 而不是 `?`）。写成 `?...` 只能匹配两段，
#: `py12306.helpers.station` 会被截成 `py12306.helpers` —— 而后者往往不是一个
#: 真实模块（上游很多子包没有 `__init__.py`），可达性 BFS 会直接把它跳过。
UPSTREAM_REF_RE = re.compile(r"py12306(?:\.[A-Za-z_][A-Za-z0-9_]*)*")

# --------------------------------------------------------------------------- #
# 有意「不打进包」的**可达**上游依赖
#
# 只登记**可达**的项。每一条都必须写清楚**为什么可以缺** —— 判据是：
# 缺了它，功能会走到一条**已明确设计过**的兜底路径，而不是崩掉。
# 不可达的项**不需要**登记（见模块 docstring 里对「橡皮图章」的说明）。
#
# ⚠️ 这里只谈**上游**的依赖。本项目自己的可选依赖（例如 ddddocr ——
# `verify/captcha_server.py` 用它做本机打码服务）走 spec 的 `excludes`，
# 理由写在那边的注释里；两者不要混到一起，否则清单会开始收不属于它的东西
# （`tests/test_packaging.py` 有一条专门钉住「清单里的名字必须真的可达」，
# 就是被 ddddocr 混进来这件事逼出来的）。
# --------------------------------------------------------------------------- #
ALLOWED_MISSING: dict[str, str] = {
    "dingtalkchatbot": "上游的钉钉通知渠道：只在方法体内 import，本项目用自己的通知实现、从不调用该方法",
    "lightpush": "上游的 ServerChan/PushBear 渠道：同上，方法体内 import、从不调用",
}

#: 上游源码里出现、但**跟着 vendor 目录一起作为数据文件发布**的顶层名字。
#: 它们不是「第三方依赖」，所以不走 hiddenimports 那套对账。
SHIPPED_WITH_VENDOR = {"py12306", "ticket_facts", "env", "main", "config"}

#: 本项目自己的模块。
OWN_MODULES = {"ticket_helper", "desktop_ui", "mock_server"}


class _Stub:
    """PyInstaller 在 spec 里注入的类的替身：只负责把 kwargs 记下来。

    属性缺失时返回空列表 —— `PYZ(a.pure, a.zipped_data)` 这类调用会把
    `Analysis` 的产物当参数继续传，但我们不关心它们的内容。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __getattr__(self, item: str) -> Any:
        return []


def load_spec(spec_path: Path = SPEC_PATH) -> dict[str, list[Any]]:
    """执行真实 spec，返回 `Analysis(...)` 实际收到的四个列表。

    ⚠️ 不要改成「正则读 spec 文本」：spec 里有 `hiddenimports += collect_submodules(...)`，
    文本匹配看不出那一步展开了什么；而那一步**正是**本项目模块被打进包的依据。
    """
    captured: dict[str, _Stub] = {}

    def make(kind: str):
        def factory(*args: Any, **kwargs: Any) -> _Stub:
            stub = _Stub(*args, **kwargs)
            captured[kind] = stub
            return stub

        return factory

    namespace: dict[str, Any] = {
        "__name__": "tickethelper_spec_probe",
        "__file__": str(spec_path),
        "SPECPATH": str(spec_path.parent),
        "Analysis": make("analysis"),
        "PYZ": make("pyz"),
        "EXE": make("exe"),
        "COLLECT": make("collect"),
    }
    source = spec_path.read_text(encoding="utf-8")
    exec(compile(source, str(spec_path), "exec"), namespace)  # noqa: S102 - 读的是本仓库自己的 spec

    if "analysis" not in captured:
        raise RuntimeError(f"{spec_path.name} 里没有执行到 Analysis(...) —— spec 结构变了？")

    analysis = captured["analysis"]
    return {
        "datas": list(analysis.datas),
        "hiddenimports": list(analysis.hiddenimports),
        "excludes": list(analysis.excludes),
        "pathex": list(analysis.pathex),
    }


def _module_name(path: Path, vendor_root: Path) -> str:
    """`vendor/pjialin/py12306/order/order.py` -> `py12306.order.order`。"""
    rel = path.relative_to(vendor_root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports_of(tree: ast.AST) -> list[tuple[str, int, str | None]]:
    """列出所有 import：`(顶层名, level, module)`。含函数体内的（上游大量延迟导入）。"""
    out: list[tuple[str, int, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name.split(".")[0], 0, None) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0] if module else ""
            out.append((top, node.level, node.module))
    return out


def scan_vendor(vendor_root: Path = VENDOR_ROOT) -> dict[str, Any]:
    """扫 vendor：返回每个模块的**内部后继**与**外部 import**。"""
    modules: dict[str, set[str]] = {}
    external: dict[str, set[str]] = {}
    files: dict[str, str] = {}

    for path in sorted(vendor_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        name = _module_name(path, vendor_root)
        files[name] = path.relative_to(BASE).as_posix()
        successors: set[str] = set()
        externals: set[str] = set()
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        for top, level, module in _imports_of(tree):
            if level:  # 相对 import：在 vendor 内部解析
                base = package.split(".") if package else []
                if level > 1:
                    base = base[: len(base) - (level - 1)]
                target = ".".join(base + (module.split(".") if module else []))
                successors.add(target)
                continue
            successors.add(module or top)
            externals.add(top)
        modules[name] = successors
        external[name] = externals
    return {"modules": modules, "external": external, "files": files}


def upstream_references(helper_root: Path = HELPER_ROOT, vendor_root: Path = VENDOR_ROOT) -> set[str]:
    """本项目源码里引用到的上游模块名（含字符串引用）。

    匹配到的不一定是模块（可能是 `py12306.helpers.station` 里的某个类），
    所以**从长到短找最长的那段真实模块名**，找不到就继续往上退。
    """
    known = set(scan_vendor(vendor_root)["modules"])
    found: set[str] = set()
    for path in sorted(helper_root.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in UPSTREAM_REF_RE.finditer(text):
            parts = match.group(0).split(".")
            for size in range(len(parts), 0, -1):
                candidate = ".".join(parts[:size])
                if candidate in known:
                    found.add(candidate)
                    break
    # 垫片自己会 import 这个；不依赖它出现在源码文本里
    found.add("py12306.config")
    return found


def reachable_from(entries: set[str], vendor_root: Path = VENDOR_ROOT) -> set[str]:
    """从 entries 出发，在 vendor 内部做可达性 BFS。

    ⚠️ **祖先包也要算可达**：import 一个模块时 Python 会先执行它的父包，
    父包 `__init__.py` 里的顶层 import 同样会跑到。少了这一步就会**漏报** ——
    实测踩到过：入口收敛成 `py12306.helpers.station` 这种精确路径之后，
    `py12306.helpers`（父包）自己就掉出了可达集合，它里面的第三方
    import 会直接看不见（而漏报正是这个工具最不该犯的错）。
    """
    modules = scan_vendor(vendor_root)["modules"]
    seen: set[str] = set()
    queue: deque[str] = deque(entries)
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        if current not in modules:
            continue  # 外部包或 vendor 里不存在的名字
        seen.add(current)
        for nxt in modules[current]:
            if nxt and nxt not in seen:
                queue.append(nxt)
        parts = current.split(".")
        for size in range(1, len(parts)):
            ancestor = ".".join(parts[:size])
            if ancestor in modules and ancestor not in seen:
                queue.append(ancestor)
    return seen


def _covered_by(module: str, entries: list[str]) -> bool:
    """entries 里有没有一条能覆盖 module（本身、父包、或子模块）。"""
    for entry in entries:
        if entry == module or entry.startswith(module + ".") or module.startswith(entry + "."):
            return True
    return False


def bundle_landing(datas: list[Any], target: Path, project_root: Path = BASE) -> str | None:
    """按 datas 的 `src -> dest` 映射，算出 `target` 在包内的相对路径。

    这个函数的用处是把「VENDOR_ROOT 在冻结后能不能指对」变成**算得出来的**：
    shim 里写死的是 ``_MEIPASS / "vendor" / "pjialin"``，所以这里必须能算出
    恰好等于 ``vendor/pjialin`` —— 而不是「datas 里有一条看起来像 vendor 的项」。

    有多个映射同时命中时，**最具体的 src 胜出**（比如同时写了
    `(vendor, "vendor")` 和 `(vendor/pjialin, "vendor/pjialin")`，取后者）。
    """
    target = target.resolve()
    candidates: list[tuple[int, str]] = []
    for entry in datas:
        try:
            src, dest = Path(entry[0]).resolve(), Path(entry[1])
        except (TypeError, IndexError, ValueError):
            continue
        if target == src:
            candidates.append((len(src.parts), dest.as_posix()))
        elif src in target.parents:
            candidates.append((len(src.parts), (dest / target.relative_to(src)).as_posix()))
    if not candidates:
        return None
    candidates.sort(key=lambda item: -item[0])
    return candidates[0][1].strip("/").removeprefix("./")


def statically_imported_helpers(helper_root: Path = HELPER_ROOT, vendor_root: Path = VENDOR_ROOT) -> tuple[list[str], list[str]]:
    """`vendor/` 里**不走 sys.path 注入**的那些模块，必须被静态 import 到。

    上游有两个包，进包方式**完全不同**，而且错了都是静默的：

    - `vendor/pjialin`：靠垫片挂 sys.path，所以只能作为**数据**入包（见 datas）；
    - `vendor/testerSunshine`：是被 `from vendor.testerSunshine import standby`
      **正常 import** 的 —— 这一条 PyInstaller 看得见，会跟着收进 PYZ。

    后者的风险是：哪天有人为了「统一风格」把它也改成按路径加载，静态分析就
    看不见了、模块不会进包，而源码模式下一切正常。
    返回 (problems, notes)。
    """
    problems: list[str] = []
    notes: list[str] = []

    imported: set[str] = set()
    for path in sorted(helper_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

    checked = 0
    for path in sorted(vendor_root.parent.rglob("*.py")):
        rel = path.relative_to(vendor_root.parent)
        if rel.parts[0] == "pjialin":
            continue  # 由 datas + 可达性分析负责
        dotted = ".".join(("vendor", *rel.with_suffix("").parts))
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        checked += 1
        covered = any(name == dotted or name.startswith(dotted + ".") or dotted.startswith(name + ".") for name in imported)
        if not covered:
            problems.append(
                f"{path.relative_to(BASE).as_posix()} 没有被**静态** import —— "
                "PyInstaller 看不见它，不会进包（源码模式下不会暴露）"
            )
    if checked:
        notes.append(f"vendor/ 下非 pjialin 的模块 {checked} 个，均有静态 import 覆盖")
    return problems, notes


def dependency_report(
    vendor_root: Path = VENDOR_ROOT, helper_root: Path = HELPER_ROOT
) -> tuple[dict[str, set[str]], list[str], set[str], set[str]]:
    """把「上游到底需要哪些第三方包」算出来。

    返回 `(required, unreachable_only, reached, entries)`：

    - `required`：**可达路径上**用到的第三方顶层名 → 用到它的上游模块集合；
    - `unreachable_only`：只在上游**到不了**的模块里出现的第三方名（不需要进包）；
    - `reached` / `entries`：可达模块集合 / 本项目引用的上游入口集合（便于排查）。
    """
    graph = scan_vendor(vendor_root)
    entries = upstream_references(helper_root, vendor_root)
    reached = reachable_from(entries, vendor_root)
    external = graph["external"]

    required: dict[str, set[str]] = {}
    for module in sorted(reached):
        for name in external.get(module, set()):
            if name in sys.stdlib_module_names:
                continue
            if name in SHIPPED_WITH_VENDOR or name in OWN_MODULES:
                continue
            if name in graph["modules"]:
                continue  # vendor 内部模块
            required.setdefault(name, set()).add(module)

    unreachable_only = sorted(
        {
            name
            for module, names in external.items()
            if module not in reached
            for name in names
            if name not in sys.stdlib_module_names
            and name not in SHIPPED_WITH_VENDOR
            and name not in OWN_MODULES
            and name not in required
        }
    )
    return required, unreachable_only, reached, entries


def check(spec_path: Path = SPEC_PATH, vendor_root: Path = VENDOR_ROOT) -> tuple[list[str], list[str]]:
    """返回 (problems, notes)。problems 非空即视为失败。"""
    problems: list[str] = []
    notes: list[str] = []

    spec = load_spec(spec_path)
    datas = spec["datas"]
    hidden = [str(item) for item in spec["hiddenimports"]]
    excludes = [str(item) for item in spec["excludes"]]

    # ---- ① vendor 必须作为数据入包，且**落在 shim 会去找的那个路径上** ----
    landing = bundle_landing(datas, vendor_root)
    if landing != "vendor/pjialin":
        found = "（没有映射到它）" if landing is None else f"（落到了 {landing}）"
        problems.append(
            f"datas 里没有把 vendor/pjialin 映射到包内 `vendor/pjialin` {found}："
            "shim 的 VENDOR_ROOT 是 `_MEIPASS/vendor/pjialin`，指不对的话 "
            "`shim.is_available()` 为假 → 所有 pjialin 后端**静默回退**"
        )
    else:
        notes.append("vendor/pjialin 已入包，包内路径 vendor/pjialin（与 VENDOR_ROOT 一致）")

    if not (vendor_root / "ticket_facts.py").exists():
        problems.append("vendor/pjialin/ticket_facts.py 不存在 —— 请跑 scripts/build_vendor_pjialin.py --init")

    # ---- ② 只对**可达**的上游依赖要求归属 ----
    required, unreachable_only, reached, entries = dependency_report(vendor_root, HELPER_ROOT)

    for name in sorted(required):
        if _covered_by(name, hidden) or name in ALLOWED_MISSING:
            continue
        where = ", ".join(sorted(required[name])[:3])
        if _covered_by(name, excludes):
            problems.append(
                f"{name!r} 在 spec 的 excludes 里，但它在**可达路径**上（{where}）—— "
                "排除了又没写理由等于把功能打掉"
            )
        else:
            problems.append(f"可达路径上用到了 {name!r}，spec 既没收进包也没登记豁免（{where}）")

    notes.append(f"上游模块可达 {len(reached)} 个 / 上游引用入口 {len(entries)} 个")
    notes.append(
        f"可达路径上的第三方依赖 {len(required)} 个，已登记豁免："
        + (", ".join(sorted(ALLOWED_MISSING)) if ALLOWED_MISSING else "（无）")
    )
    if unreachable_only:
        notes.append(
            f"上游源码里还有 {len(unreachable_only)} 个第三方 import **不在可达路径上**（不需要进包）："
            + ", ".join(unreachable_only)
        )
    notes.append(f"hiddenimports {len(hidden)} 条 / excludes {len(excludes)} 条")

    # ---- ③ 关键依赖不能同时出现在 hiddenimports 与 excludes ----
    for name in ("PySide6.QtNetwork", "PySide6.QtWebSockets"):
        if _covered_by(name, excludes):
            problems.append(f"{name} 被 excludes 排除了，但它被隐藏导入需要（会报 libshiboken 导入失败）")

    # ---- ④ vendor 里不走 sys.path 注入的部分，必须被静态 import 到 ----
    static_problems, static_notes = statically_imported_helpers(HELPER_ROOT, vendor_root)
    problems.extend(static_problems)
    notes.extend(static_notes)

    return problems, notes


def main() -> int:
    parser = argparse.ArgumentParser(description="打包完整性自检：上游可达 import × spec 对账")
    parser.add_argument("--quiet", action="store_true", help="只在有问题时输出")
    args = parser.parse_args()

    problems, notes = check()

    if not args.quiet:
        print("打包完整性自检")
        for note in notes:
            print(f"  · {note}")

    if problems:
        print("\n✘ 发现问题：")
        for item in problems:
            print(f"  - {item}")
        return 1

    if not args.quiet:
        print("\n✔ 打包配置与上游依赖对得上")
    return 0


if __name__ == "__main__":
    sys.exit(main())
