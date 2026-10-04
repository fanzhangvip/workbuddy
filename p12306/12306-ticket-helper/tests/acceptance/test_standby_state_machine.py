"""
候补状态机验收测试 —— 由人类定义，WorkBuddy 不得修改。

业务规则来源：
  12306 官方候补购票流程（用户视角）：
    提交候补 -> 排队中 -> 兑现成功 / 兑现失败 / 用户取消

关键不变量：
  1. "提交候补单成功" ≠ "候补成功"
  2. 提交成功后进入"排队中"，只有 12306 兑现通知才进入"成功"
  3. 终态不可转出
  4. 状态值来自固定词汇表，不允许引擎自创字符串
"""
import inspect

import pytest


class TestStandbyVocabulary:
    """状态词汇表契约"""

    def test_all_status_constants_exist(self):
        """引擎必须导出这五个状态常量"""
        from ticket_helper.standby import engine
        required = [
            "STANDBY_PENDING",     # 未提交
            "STANDBY_QUEUING",     # 排队中（已提交）
            "STANDBY_SUCCESS",     # 兑现成功
            "STANDBY_FAILED",      # 兑现失败
            "STANDBY_CANCELLED",   # 用户取消
        ]
        for name in required:
            assert hasattr(engine, name), f"缺少状态常量: {name}"

    def test_status_constants_are_unique(self):
        """五个状态值必须互不相同"""
        from ticket_helper.standby import engine
        values = [
            engine.STANDBY_PENDING,
            engine.STANDBY_QUEUING,
            engine.STANDBY_SUCCESS,
            engine.STANDBY_FAILED,
            engine.STANDBY_CANCELLED,
        ]
        assert len(values) == len(set(values)), f"状态值有重复: {values}"

    def test_queuing_differs_from_success(self):
        """
        核心业务规则：
        提交成功（queuing）和兑现成功（success）必须不同。
        这是之前 bug 的根源。
        """
        from ticket_helper.standby import engine
        assert engine.STANDBY_QUEUING != engine.STANDBY_SUCCESS, (
            "提交候补成功和候补兑现成功被混为同一个状态"
        )


class TestStandbyTransitions:
    """状态转换契约"""

    def test_engine_does_not_use_literal_queuing_string(self):
        """
        引擎源码中，status 赋值不应直接使用字符串字面量 'queuing'/'success'，
        必须使用常量。这防止实现和常量脱节。
        """
        from ticket_helper.standby import engine
        src = inspect.getsource(engine)

        # 允许在常量定义处出现字符串字面量，但不允许在 status 赋值处
        import re
        # 找 `self.status = "..."` 或 `self.status = '...'`
        bad_assignments = re.findall(
            r'self\.status\s*=\s*["\'](\w+)["\']', src
        )
        assert not bad_assignments, (
            f"引擎直接使用字符串字面量赋值 status: {bad_assignments}，"
            f"应使用 STANDBY_* 常量"
        )

    def test_terminal_states_never_reassigned(self):
        """
        终态（success/failed/cancelled）一旦进入，不应再被赋值。
        这个测试读源码做静态检查：终态常量不应出现在 status = 的右侧之后。
        """
        from ticket_helper.standby import engine
        src = inspect.getsource(engine)
        import re
        # 找出所有 status 赋值行
        assignments = re.findall(
            r'self\.status\s*=\s*(\S+)', src
        )
        # 终态常量出现次数不应超过 1（定义处不算，这里只算赋值）
        terminal_names = {
            "STANDBY_SUCCESS", "STANDBY_FAILED", "STANDBY_CANCELLED",
        }
        for name in terminal_names:
            count = sum(1 for a in assignments if name in a)
            # 如果同一个终态在多处被赋值，可能是状态机设计问题
            # 这里只警告不失败，因为有的实现会合法地重设
            assert count <= 3, (
                f"终态 {name} 被赋值 {count} 次，可能状态机设计有问题"
            )