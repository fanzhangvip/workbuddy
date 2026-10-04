"""V5 参数级基线（用户域）：本项目的端点与响应字段，逐项对上游出处。

本项目此前只有**路径级**基线（每条 URL 有出处），**参数级**是空缺 ——
而两次事故（`confirmHB` 路径整族写错、乘客串 6 段 vs 5 段）恰好都落在
「参数/结构」这一层：路径对得上，字段对不上，离线全绿，真机才炸。

这一份补的就是那一层。锚点是 `vendor/pjialin/ticket_facts.py` ——
它由 `scripts/build_vendor_pjialin.py` 从上游原件**逐字切出**
（`--check` 会重新生成一遍再比对），所以这里比的是「我们 vs 上游」，
而不是「我们 vs 我们自己写的期望值」。

⚠️ 与 `tests/test_query_baseline.py` 的分工：那份管余票字段下标，
这份管用户域的端点与响应结构。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ticket_helper.inter import parser as parser_mod
from ticket_helper.inter import urls as urls_mod
from ticket_helper.kernel.pjialin.shim import install

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "pjialin"


@pytest.fixture(scope="module")
def facts():
    """上游协议事实（生成物）。module 级：只装一次 sys.path。"""
    install()
    sys.path.insert(0, str(VENDOR))
    try:
        import ticket_facts  # type: ignore[import-not-found]

        yield ticket_facts
    finally:
        sys.path.remove(str(VENDOR))


# --------------------------------------------------------------------------- #
# 一、端点：四个一致、一个不同
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fact_attr,our_const",
    [
        ("USER_INFO_PATH", "QUERY_USER_INFO"),
        ("PASSENGERS_PATH", "GET_PASSENGER_DTOS"),
        ("QR_CREATE_PATH", "CREATE_QR"),
        ("QR_CHECK_PATH", "CHECK_QR"),
    ],
)
def test_endpoints_match_upstream(facts, fact_attr: str, our_const: str) -> None:
    """这四个端点两边**完全一致** —— 说明本项目的端点常量当初就是按官方来的。

    写成本用例的价值：它把「一致」也变成**可验证的**。否则哪天有人改了一边，
    没人会发现（而且改了还能跑，因为另一边才是真在用的）。
    """
    assert getattr(facts, fact_attr) == getattr(urls_mod, our_const)


def test_login_check_endpoint_differs_and_that_is_recorded(facts) -> None:
    """★ 登录态检测是**唯一不同的**那一个，且差异要显式留在案上。

    - 上游：``GET /otn/login/conf``（``user/job.py::check_user_is_login``）
    - 本项目：``POST /otn/login/checkUser``

    为什么不能只是「记一笔」：这两条都自称「检测登录态」，所以任何一处
    用错了不会有报错 —— 只会在真机上表现为「会话明明有效却判失效（或反之）」。
    `login_check` 域把这条差异做成了可切换 + 差分表
    （`scripts/login_check_diff.py`），本用例负责不让它**悄悄变成一致**。
    """
    assert facts.LOGIN_CHECK_PATH == "/otn/login/conf"
    assert urls_mod.CHECK_USER == "/otn/login/checkUser"
    assert facts.LOGIN_CHECK_PATH != urls_mod.CHECK_USER, (
        "两边居然一致了 —— 上游结构变了，还是本项目改了端点？需重新核对"
    )
    # 本项目其实**也有**这条路径常量（登录页配置用它），只是不用于检测登录态。
    # 钉住这一点，免得有人「看到有这条常量」就顺手把检测也改过去。
    assert urls_mod.LOGIN_CONF == facts.LOGIN_CHECK_PATH


# --------------------------------------------------------------------------- #
# 二、响应字段：上游是点号路径，我们是逐层剥 —— 两者必须能对上
# --------------------------------------------------------------------------- #
def test_login_alive_field_differs_and_uses_string_comparison(facts) -> None:
    """判活字段：上游 ``data.is_login``（与 ``'Y'`` 比较）vs 本项目 ``data.flag``。

    两边字段不同，**且取值语义不同**：上游是字符串比较，本项目是布尔真值。
    这个差异的杀伤力在于：若照抄字段却按 Python 真值判，
    ``is_login = "N"`` 会被判成「活着」（非空字符串恒为真）。
    """
    assert facts.LOGIN_ALIVE_FIELD == "data.is_login"
    assert facts.LOGIN_ALIVE_VALUES == ("Y",)
    assert facts.LOGIN_ALIVE_FIELD not in parser_mod.CHECK_USER_ALIVE_PATHS
    assert "data.flag" in parser_mod.CHECK_USER_ALIVE_PATHS


def test_user_info_field_is_unwrapped_by_our_parser(facts) -> None:
    """上游读用户信息用的是 ``data.userDTO.loginUserDTO``（**两层嵌套**）。

    本项目的 `unwrap_user_payload` 必须能剥到那一层 —— 否则整个 profile
    会解析成空串，而症状是「账号信息页一片空白」，与「接口挂了」长得一样。
    """
    assert facts.USER_INFO_FIELD == "data.userDTO.loginUserDTO"

    payload = {
        "data": {
            "userDTO": {
                "loginUserDTO": {
                    "user_name": "张三",
                    "name": "张三",
                    "sexCode": "M",
                }
            }
        }
    }
    profile = parser_mod.parse_user_profile(payload)
    assert profile["username"] == "张三"
    assert profile["real_name"] == "张三"
    assert profile["sex"] == "男"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"user_name": "张三", "sexCode": "M"}},
        {"data": {"userDTO": {"user_name": "张三", "sexCode": "M"}}},
        {"data": {"userDTO": {"loginUserDTO": {"user_name": "张三", "sexCode": "M"}}}},
    ],
    ids=["扁平data", "userDTO一层", "loginUserDTO两层"],
)
def test_three_nesting_shapes_give_the_same_profile(payload) -> None:
    """三种**层数不同、字段名相同**的结构必须解析出同一个结果。

    这是「剥层集中在一处」的存在理由：三种结构都不会报错，
    少剥一层只会静默地得到一片空白。三条写法各写一遍，就必然漏一处。
    """
    profile = parser_mod.parse_user_profile(payload)
    assert profile["username"] == "张三"
    assert profile["sex"] == "男"


def test_passengers_field_is_recognised_by_our_parser(facts) -> None:
    """乘车人列表：上游读 ``data.normal_passengers``；本项目必须认这个键。

    这正是候补链路里 `allEncStr` 的来源 —— 认不出它，「乘车人信息格式不正确」
    会出现在一个看起来毫无关联的地方。
    """
    assert facts.PASSENGERS_FIELD == "data.normal_passengers"
    rows = parser_mod.parse_passengers_payload(
        {
            "data": {
                "normal_passengers": [
                    {
                        "passenger_name": "张三",
                        "passenger_id_type_code": "1",
                        "passenger_id_no": "110101199001011234",
                        "allEncStr": "enc-1",
                    }
                ]
            }
        }
    )
    assert len(rows) == 1
    assert rows[0]["passenger_name"] == "张三"
