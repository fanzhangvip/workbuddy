"""
元测试：扫描 tests/acceptance/ 目录，禁止"假测试"模式。
由人类维护，WorkBuddy 不得修改。

违反后果：任何一条断言失败，整个验收层失效，WorkBuddy 的产出全部作废。
"""
import ast
from pathlib import Path

import pytest

ACCEPTANCE_ROOT = Path(__file__).parent


def _iter_acceptance_py():
    """只扫描本目录，不递归到开发测试"""
    for f in ACCEPTANCE_ROOT.glob("test_*.py"):
        if f.name == "test_no_fake_green.py":
            continue  # 跳过自己，否则会误判
        yield f


class TestNoFakeGreen:
    """禁止清单的机器可读版本"""

    def test_no_skip_decorators(self):
        """禁止 pytest.mark.skip / pytest.skip / unittest.skip"""
        forbidden = ("pytest.mark.skip", "pytest.skip(", "unittest.skip")
        for f in _iter_acceptance_py():
            src = f.read_text(encoding="utf-8")
            for pat in forbidden:
                assert pat not in src, f"{f.name} 出现禁止模式: {pat}"

    def test_no_swallowed_exceptions(self):
        """禁止 try/except 静默吞异常"""
        for f in _iter_acceptance_py():
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    body = node.body
                    # 只包含 pass 或 return None 的 except
                    if len(body) == 1:
                        stmt = body[0]
                        if isinstance(stmt, ast.Pass):
                            raise AssertionError(
                                f"{f.name}:{node.lineno} 吞异常 (except: pass)"
                            )
                        if (isinstance(stmt, ast.Return)
                                and isinstance(stmt.value, ast.Constant)
                                and stmt.value.value is None):
                            raise AssertionError(
                                f"{f.name}:{node.lineno} 吞异常 (except: return None)"
                            )

    def test_no_weak_not_none_assertion(self):
        """禁止 `assert x is not None` 作为唯一断言"""
        for f in _iter_acceptance_py():
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for func in ast.walk(tree):
                if not isinstance(func, ast.FunctionDef):
                    continue
                if not func.name.startswith("test_"):
                    continue
                asserts = [n for n in ast.walk(func) if isinstance(n, ast.Assert)]
                if len(asserts) == 1:
                    a = asserts[0]
                    if (isinstance(a.test, ast.Compare)
                            and len(a.test.ops) == 1
                            and isinstance(a.test.ops[0], ast.IsNot)
                            and isinstance(a.test.comparators[0], ast.Constant)
                            and a.test.comparators[0].value is None):
                        raise AssertionError(
                            f"{f.name}::{func.name} 使用弱断言 `assert x is not None`"
                        )

    def test_no_bare_magicmock_return(self):
        """禁止 mock 返回 MagicMock() 或 {} 而不指定字段（简单文本检查）"""
        for f in _iter_acceptance_py():
            src = f.read_text(encoding="utf-8")
            # 这个规则靠文本匹配，容易误报，只检查明显模式
            if "return_value = MagicMock()" in src:
                raise AssertionError(f"{f.name} 存在裸 MagicMock() 返回")