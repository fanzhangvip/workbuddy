"""V0 静态纪律门 —— 一眼能判、与业务无关的机械规则。

设计依据：``docs/测试验证框架设计.md`` §2 V0。

为什么 V0 要排在所有测试之前
----------------------------
这一层的规则**不需要起服务、不需要 mock、不需要外网**，纯静态分析。
它便宜到应该每次提交都跑 —— 所以它是第一道闸。

它防的是四类「不会报错、只会腐烂」的问题：

1. **skip 掩盖问题**：`skip` 让测试变绿，但问题是**存在**的。本项目纪律：禁止 skip，
   改用显式 `pending` 标记 + 台账（见 §八 T2）。
2. **弱断言等于没断言**：`assert x` / `assert x is not None` 不检查任何具体值。
   存量太多（实测 92 处），故采用**棘轮**：冻结为基线，**只减不增**。
3. **行尾混合**：同一文件里既有 CRLF 又有 LF → 多行替换会**静默失败**
   （本项目真踩过：多行替换忘 `\\r\\n`，锚点找不到）。
4. **适配层混进协议细节**：决策 §10-16 要求适配层「只管搬运」，不得出现
   12306 的路径/参数/解析 —— 否则「用上游源码」会退化成「又写了一遍」。
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
TESTS_DIR = BASE / "tests"
BASELINE_FILE = TESTS_DIR / ".weak_assert_baseline.json"

#: 要扫纪律的目录。
#: ⚠️ `tests/` **必须在内** —— 最初漏了它，结果「制造行尾混合」的变异检验
#: 抓不住（我那处变异就做在 `tests/` 里）。而本项目的行尾事故恰恰**发生在 test 文件上**：
#: `tests/test_notify_verify.py`、`tests/test_parser.py` 都曾是 CRLF/LF 混用。
#: 这是变异检验第一次替我们揪出门本身的漏洞。
CODE_DIRS = ("ticket_helper", "desktop_ui", "mock_server", "web", "tests", "scripts")

#: **豁免的 skip**：必须写清理由。豁免不等于隐藏 —— `scripts/verify.py`
#: 会把它们**打印在报告里**，让人看得见。
SANCTIONED_SKIPS: dict[str, str] = {
    "tests/test_desktop.py": (
        "PySide6 属可选依赖：缺失时该模块整体 import 不了，"
        "与业务正确性无关。已在 verify.py 报告中列出。"
    ),
    "tests/test_cli.py": (
        "同上：CLI 测试依赖 PySide6 才能构造完整应用上下文。已在报告中列出。"
    ),
}

#: 适配层（必须「只管搬运」）—— 这些目录里不许出现协议细节
ADAPTER_DIRS = ("ticket_helper/kernel/pjialin",)

#: 协议细节的字面量特征（只查**代码里的字符串常量**，不查注释/docstring）
PROTOCOL_RE = re.compile(r"^(/otn/|/passport/|/agency/|https?://)")

SKIP_ATTRS = {"skip", "skipif", "importorskip", "xfail"}


# --------------------------------------------------------------------------- #
# 采集器（verify.py 也会 import 它们，避免规则两处各写一遍）
# --------------------------------------------------------------------------- #
def python_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.py") if "__pycache__" not in p.parts and p.name != "env.py"
    )


def collect_skips() -> list[tuple[str, int, str]]:
    """返回 ``[(相对路径, 行号, 形式), …]``。"""
    out: list[tuple[str, int, str]] = []
    for p in python_files(TESTS_DIR):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # pytest.mark.skip / skipif / importorskip / xfail
            if isinstance(node, ast.Attribute) and node.attr in SKIP_ATTRS:
                out.append((p.relative_to(BASE).as_posix(), node.lineno, f".{node.attr}"))
            # pytest.skip("...")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "skip"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "pytest"
            ):
                out.append((p.relative_to(BASE).as_posix(), node.lineno, "pytest.skip()"))
    return sorted(set(out))


def collect_weak_asserts(root: Path | None = None) -> Counter[tuple[str, str]]:
    """弱断言：``assert <裸名字>`` 与 ``assert ... is[/is not] None``。

    键是 ``(相对路径, 断言表达式源码)`` —— 用表达式而不是行号，
    这样代码移位不会让基线失效（否则基线天天要重建，很快就没人维护了）。

    ``root`` 默认是 ``tests/``；允许传别的目录是**为了能被测试**：
    `tests/test_verify_tool.py` 要验证「豁免的生效范围」，
    若只能扫 ``tests/``，探针文件就得写进真目录 ——
    一旦某次异常没清掉，**这个门自己就会变红**（曾经真的发生过）。
    """
    found: Counter[tuple[str, str]] = Counter()
    for p in python_files(root or TESTS_DIR):
        src = p.read_text(encoding="utf-8")
        try:
            rel = p.relative_to(BASE).as_posix()
        except ValueError:
            # 传进来的 root 可能在仓库外（测试用 tmp_path）—— 那时候用文件名就够了。
            # 硬要 relative_to 会直接抛错，让「探针」变成「把门弄崩」。
            rel = p.name
        tree = ast.parse(src)
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            # 带 noqa 的放行（要求写明理由，人在 review 时会看见）
            #
            # ⚠️ 查的是这条 assert 的**整个物理范围**（首行到末行），不只是首行。
            # 2026-09-30 修：原来还写了一个 `ast.get_source_segment(src, node)` 分支，
            # 它**永远为假** —— 尾随注释不属于语句节点（`end_col_offset` 停在表达式末尾），
            # 所以「把 noqa 写在多行 assert 的最后一行」看起来豁免了、实际会被判成新增弱断言。
            # 死分支比没有分支更坏：它让「看起来生效」变成可能。
            span = lines[node.lineno - 1 : node.end_lineno or node.lineno]
            if any("noqa: WEAK-ASSERT" in line for line in span):
                continue
            t = node.test
            weak = False
            if isinstance(t, ast.Name):
                weak = True
            elif (
                isinstance(t, ast.Compare)
                and len(t.ops) == 1
                and isinstance(t.ops[0], (ast.Is, ast.IsNot))
                and isinstance(t.comparators[0], ast.Constant)
                and t.comparators[0].value is None
            ):
                weak = True
            if weak:
                found[(rel, ast.unparse(t))] += 1
    return found


def collect_line_ending_violations() -> list[str]:
    """`.py` 一律 CRLF（项目约定）。

    为什么是「一律 CRLF」而不是「只要不混用」：
    作者写多行替换补丁时，锚点里的换行必须与文件一致。
    **纯 LF 的文件同样会让 CRLF 锚点找不到** —— 症状是「替换没生效」而不是报错。
    所以判据是「与项目约定一致」，不是「文件内部自洽」。

    ⚠️ 排除 `vendor/` 与 `reference/`：那里的上游原件必须**逐字节保持原样**
    （换行也算字节），动了它们，溯源 sha256 会立刻不一致。
    """
    out: list[str] = []
    for d in CODE_DIRS:
        root = BASE / d
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            b = p.read_bytes()
            crlf = b.count(b"\r\n")
            lf = b.count(b"\n") - crlf
            if lf:
                kind = f"混合（CRLF={crlf} LF={lf}）" if crlf else f"纯 LF（{lf} 行）"
                out.append(f"{p.relative_to(BASE).as_posix()} {kind}")
    return sorted(out)


def collect_non_ascii_bat() -> list[str]:
    """`.bat` 必须纯 ASCII —— cmd 按 GBK 读，中文乱码后的路径比英文提示危险得多。"""
    out: list[str] = []
    for p in BASE.rglob("*.bat"):
        if "__pycache__" in p.parts or "vendor" in p.parts:
            continue
        try:
            p.read_bytes().decode("ascii")
        except UnicodeDecodeError:
            out.append(p.relative_to(BASE).as_posix())
    return sorted(out)


def collect_protocol_literals_in_adapters() -> list[str]:
    """适配层里的协议字面量（**只查代码里的字符串常量**，不查注释与 docstring）。"""
    out: list[str] = []
    for d in ADAPTER_DIRS:
        root = BASE / d
        if not root.exists():
            continue
        for p in python_files(root):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if PROTOCOL_RE.match(node.value.strip()):
                        out.append(
                            f"{p.relative_to(BASE).as_posix()}:{node.lineno} {node.value[:60]!r}"
                        )
    return sorted(out)


def build_baseline() -> dict:
    weak = collect_weak_asserts()
    return {
        "_comment": (
            "弱断言棘轮基线（只减不增）。重建：python scripts/verify.py --update-baseline。"
            "新增弱断言会让 tests/test_discipline.py 失败。"
        ),
        "total": sum(weak.values()),
        # [相对路径, 断言表达式, 出现次数] —— 用表达式而不是行号，代码移位不会让基线失效
        "entries": [[path, expr, n] for (path, expr), n in sorted(weak.items())],
    }


def load_baseline() -> dict:
    if not BASELINE_FILE.exists():
        return {"total": 0, "entries": []}
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))


def baseline_counter(baseline: dict) -> Counter[str]:
    out: Counter[str] = Counter()
    for path, expr, n in baseline.get("entries", []):
        out[f"{path}::{expr}"] += int(n)
    return out


def current_counter() -> Counter[str]:
    out: Counter[str] = Counter()
    for (path, expr), n in collect_weak_asserts().items():
        out[f"{path}::{expr}"] += n
    return out


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
def test_no_unauthorized_skip():
    """防的是：**用 skip 把问题藏起来**。

    skip 让测试变绿，但问题仍然存在 —— 而且是**静默**的。
    本项目的替代做法：显式 `pending` 标记 + 台账（会出现在验证报告里）。
    确实无法避免的（可选依赖）进 `SANCTIONED_SKIPS` 并写明理由，且会被打印出来。
    """
    violations = [
        f"{path}:{line} {form}"
        for path, line, form in collect_skips()
        if path not in SANCTIONED_SKIPS
    ]
    assert not violations, (
        "出现未豁免的 skip/xfail —— 禁止用它掩盖问题：\n  "
        + "\n  ".join(violations)
        + "\n\n确需豁免时写进 SANCTIONED_SKIPS 并写明理由。"
    )


def test_sanctioned_skips_still_exist_and_have_reasons():
    """防的是：豁免清单腐烂 —— 文件改了、skip 没了，豁免却一直挂着。

    豁免是有代价的（它让一处问题不可见），所以**必须有人维护**。
    """
    actual_files = {path for path, _, _ in collect_skips()}
    stale = sorted(set(SANCTIONED_SKIPS) - actual_files)
    assert not stale, f"这些豁免已经没有对应的 skip 了，请从 SANCTIONED_SKIPS 删掉：{stale}"
    for path, why in SANCTIONED_SKIPS.items():
        assert len(why.strip()) >= 10, f"{path} 的豁免理由太短，等于没写"


def test_no_new_weak_assertions():
    """防的是：**弱断言悄悄增长** —— `assert x` / `assert x is not None` 不检查任何值。

    存量 92 处（实测）不能一次清完，所以用**棘轮**：
    基线内允许，**新增一条就失败**；基线里多出来的（已修掉的）也会失败，提醒收窄基线。
    """
    baseline = load_baseline()
    if not baseline["entries"]:
        pytest.fail(
            f"缺少弱断言基线 {BASELINE_FILE.relative_to(BASE)}；"
            "请先运行 python scripts/verify.py --update-baseline"
        )

    base = baseline_counter(baseline)
    current = current_counter()

    added = current - base
    removed = base - current

    assert not added, (
        f"新增了 {sum(added.values())} 条弱断言（禁止）：\n  "
        + "\n  ".join(f"{k} ×{v}" for k, v in sorted(added.items()))
        + "\n\n要么改成检查具体值，要么加 `# noqa: WEAK-ASSERT` 并写明理由。"
    )
    assert not removed, (
        f"基线里有 {sum(removed.values())} 条弱断言已被修掉 —— 很好，请收窄基线：\n  "
        + "\n  ".join(f"{k} ×{v}" for k, v in sorted(removed.items()))
        + "\n\n运行 python scripts/verify.py --update-baseline"
    )


def test_line_endings_are_crlf():
    """防的是：**多行替换静默失败** —— 症状是「替换没生效」，不报错，极难联想。

    本项目真踩过：多行替换忘了适配 `\\r\\n`，锚点找不到。
    所以 `.py` 一律 CRLF（`vendor/` 与 `reference/` 除外 —— 上游原件必须逐字节原样）。
    """
    violations = collect_line_ending_violations()
    assert not violations, (
        "这些 .py 不是 CRLF（多行替换会静默失败）：\n  " + "\n  ".join(violations)
    )


def test_bat_files_are_pure_ascii():
    """防的是：`.bat` 里写中文 → cmd 按 GBK 读，乱码后的**路径**比英文提示危险得多。"""
    violations = collect_non_ascii_bat()
    assert not violations, "这些 .bat 含非 ASCII 字符：\n  " + "\n  ".join(violations)


def test_adapter_layers_contain_no_protocol_details():
    """防的是：适配层「顺手写协议」—— 「用上游源码」退化成「又自己写了一遍」。

    决策 §10-16 明确：适配层**只做搬运**（类型转换、生命周期、开关），
    不得出现 12306 的路径 / 参数 / 解析。
    这条规则机器可判，所以它是**硬的**，不是愿望。

    现在 ``ticket_helper/kernel/pjialin/`` 还没建（P1 才建）——
    所以本用例当前是「空跑通过」，但规则已经生效：P1 一建目录就被约束。
    """
    violations = collect_protocol_literals_in_adapters()
    assert not violations, (
        "适配层出现协议字面量（应留在上游代码或 Protocol 定义里）：\n  "
        + "\n  ".join(violations)
    )
