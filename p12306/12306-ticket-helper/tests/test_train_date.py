"""乘车日期可查性（``ticket_helper.train_date``）—— **契约测试**。

这里的每一条断言都对应**真机实测出来的一行事实**，不是我自己造的口径。
表格来源：2026-09-29 20:37 用 ``scripts/diagnose_left_ticket.py``
在同一时刻、同一会话、并排跑出来的对照结果（深圳 → 长沙南）。

每条用例的 docstring 写明它**防的是哪一种真实会发生、而且静默的故障**。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from ticket_helper import train_date


#: 实测时的「今天」
NOW = date(2026, 9, 29)


# --------------------------------------------------------------------------- #
# 1. 复现真机对照表
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-03", train_date.DATE_OK),          # 今天+4  → JSON 84 车次
        ("2026-10-12", train_date.DATE_OK),          # 今天+13 → JSON 82 车次
        ("2026-10-13", train_date.DATE_OK),          # 今天+14 → JSON 75 车次（可查的最后一天）
        ("2026-09-29", train_date.DATE_OK),          # 今天本身可查
        ("2026-10-14", train_date.DATE_NOT_ON_SALE),  # 今天+15 → HTML 错误页
        ("2027-05-01", train_date.DATE_NOT_ON_SALE),  # 远期    → HTML 错误页
        ("2025-10-03", train_date.DATE_PAST),        # 去年    → HTML 错误页
        ("2026-09-28", train_date.DATE_PAST),        # 昨天    → HTML 错误页
        ("", train_date.DATE_BAD_FORMAT),
        ("今天", train_date.DATE_BAD_FORMAT),
    ],
)
def test_matches_real_machine_table(value: str, expected: str) -> None:
    """防的是：可售窗口被写错一天，导致「明明能买却不查」或「白打一次注定被拒的请求」。

    边界（今天+14 / 今天+15）是实测出来的，不是估的 —— 差一天就会把
    「可查」判成「未开售」，而症状是任务静默地不去查。
    """
    assert train_date.classify(value, now=NOW) == expected


def test_bad_format_accepts_common_variants() -> None:
    """防的是：官方/界面传来 ``2026/10/03`` 或 ``20261003`` 时被误判成非法。"""
    assert train_date.classify("2026/10/03", now=NOW) == train_date.DATE_OK
    assert train_date.classify("20261003", now=NOW) == train_date.DATE_OK


# --------------------------------------------------------------------------- #
# 2. 「过去」与「未开售」必须是两种结论
# --------------------------------------------------------------------------- #
def test_past_and_not_on_sale_are_distinct() -> None:
    """**这是本次故障的核心**。

    真机上两者返回的是**同一张 HTML 错误页**，所以代码把它们合并成「风控」，
    于是「任务日期填成了去年」被报成「🚨 风控告警」：使用者去降频、换账号、干等，
    全部无效。它们的处理动作完全相反 —— 一个必须改日期，一个只要等。

    实现一旦被退化成「两者都算 not_on_sale」或「都算 past」，这条立刻红。
    """
    past = train_date.classify("2025-10-03", now=NOW)
    future = train_date.classify("2026-11-01", now=NOW)
    assert past == train_date.DATE_PAST
    assert future == train_date.DATE_NOT_ON_SALE
    assert past != future


def test_queryable_is_exactly_date_ok() -> None:
    """防的是：``queryable`` 与 ``classify`` 各自维护一套判断而漂移。"""
    for value in ("2026-10-03", "2026-10-13", "2026-10-14", "2025-10-03", "2027-01-01", "x"):
        assert train_date.queryable(value, now=NOW) == (
            train_date.classify(value, now=NOW) == train_date.DATE_OK
        ), value


# --------------------------------------------------------------------------- #
# 3. explain 必须指向可执行的动作
# --------------------------------------------------------------------------- #
def test_explain_of_past_date_tells_you_to_change_it() -> None:
    """防的是：只报「查询失败」，使用者不知道该干什么。

    「已经过去」的出路只有一条：改日期。文案里必须明确出现。
    """
    text = train_date.explain("2025-10-03", now=NOW)
    assert "已经过去" in text
    assert "2025-10-03" in text
    assert "改到今天" in text
    assert str(NOW) in text  # 告诉使用者今天到底是几号


def test_explain_of_not_on_sale_states_the_real_window() -> None:
    """防的是：把「还没开售」说成「失败」，使用者以为程序坏了。

    文案必须给出**当前真正可查的区间**，否则使用者只能靠反复试。
    """
    text = train_date.explain("2026-10-14", now=NOW)
    assert "还没开售" in text
    assert str(NOW) in text
    assert str(train_date.last_queryable_date(NOW)) in text


def test_explain_of_queryable_date_is_empty() -> None:
    """防的是：能查的日期也返回一段解释，界面无谓地弹提示。"""
    assert train_date.explain("2026-10-03", now=NOW) == ""


def test_explain_of_bad_format_says_the_expected_shape() -> None:
    """防的是：日期格式写错时报一句「参数错误」，看不出该改成什么样。"""
    text = train_date.explain("2026年10月3日", now=NOW)
    assert "格式非法" in text
    assert "YYYY-MM-DD" in text


# --------------------------------------------------------------------------- #
# 4. 「今天」只能有一个来源
# --------------------------------------------------------------------------- #
def test_today_is_the_single_source_of_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    """防的是：某个函数偷偷用了 ``date.today()``，于是测试冻不住时钟、
    真机行为与测试行为分叉（这类分叉极难发现：测试全绿，线上全错）。
    """
    monkeypatch.setattr(train_date, "today", lambda: date(2099, 1, 1))
    # 2099 年 1 月 1 日之后 14 天内的日期应当可查
    assert train_date.classify("2099-01-10") == train_date.DATE_OK
    # 而按真实今天（2026 年）它是「未开售」
    assert train_date.classify("2099-01-10", now=NOW) == train_date.DATE_NOT_ON_SALE
    # 2026 年的日期在 2099 年看来已经是过去
    assert train_date.classify("2026-10-03") == train_date.DATE_PAST


def test_presale_window_is_inclusive_and_exact() -> None:
    """防的是：边界写成开区间，把可查的最后一天判成未开售。"""
    last = train_date.last_queryable_date(NOW)
    assert last == NOW + timedelta(days=train_date.PRESALE_DAYS)
    assert train_date.queryable(last, now=NOW) is True
    assert train_date.queryable(last + timedelta(days=1), now=NOW) is False


# --------------------------------------------------------------------------- #
# 5. 「拒绝页」的识别只有一处
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body",
    [
        "<!DOCTYPE html><html><li>网络可能存在问题，请您重试一下！</li></html>",
        "\ufeff  <html><body>err</body></html>",
    ],
)
def test_reject_page_recognition(body: str) -> None:
    """防的是：接口层的 HTML 判定与这里的特征串各写一份而漂移。"""
    assert train_date.is_reject_page(body) is True


@pytest.mark.parametrize("body", ["", "   ", '{"status":true,"data":{"result":[]}}'])
def test_reject_page_rejects_non_pages(body: str) -> None:
    """防的是：把正常 JSON 也当成拒绝页，于是「有票」被判成「被风控」。"""
    assert train_date.is_reject_page(body) is False
