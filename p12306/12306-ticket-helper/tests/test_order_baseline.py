"""V5 参数级基线（下单六步）：本项目 vs 上游，逐字段对照。

为什么这一份非有不可
--------------------
下单是本项目唯一**涉资金**的链路，而它出错的形态是**静默**的：
官方对不合法参数基本不用 4xx 表达，而是回一句
``{"status":false,"messages":["系统忙，请稍后重试"]}`` ——
「参数不对」与「真的系统忙」在日志里长得一模一样。

所以这里把六步的请求规格**逐项**对上游出处比一遍：

- **一致**的项也要断言（否则哪天有人改了一边，没人会发现）；
- **不同**的项更要断言 —— 而且要断言「差异就是这个」，多一处少一处都变红。

锚点是 ``vendor/pjialin/ticket_facts.py``（由生成器从上游原件逐字切出，
``--check`` 会重新生成再比对），所以这里比的是「我们 vs 上游」，
不是「我们 vs 我们自己写的期望值」。

⚠️ 本文件**没有**任何「顺手把上游补平」的地方：差异如实留档，
它会在切默认值之前提醒我们「这条路从没被真机检验过」。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

from ticket_helper.inter import urls as urls_mod
from ticket_helper.kernel.domains.order_submit import (
    STEP_CHECK_ORDER_INFO,
    STEP_CONFIRM_SINGLE_FOR_QUEUE,
    STEP_GET_QUEUE_COUNT,
    STEP_QUERY_WAIT_TIME,
    STEP_SUBMIT,
    STEPS,
)
from ticket_helper.kernel.pjialin.shim import install

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "pjialin"


@pytest.fixture(scope="module")
def facts():
    """上游协议事实（生成物）。module 级：只装一次 sys.path。"""
    install()
    sys.path.append(str(VENDOR))  # 只能 append：vendor 里也有 main.py
    try:
        import ticket_facts  # type: ignore[import-not-found]

        yield ticket_facts
    finally:
        sys.path.remove(str(VENDOR))


@pytest.fixture(scope="module")
def self_forms():
    from ticket_helper.kernel.self import SelfOrderSubmitter

    return SelfOrderSubmitter().forms()


@pytest.fixture(scope="module")
def upstream_forms():
    from ticket_helper.kernel.pjialin import PjialinOrderSubmitter

    return PjialinOrderSubmitter().forms()


# --------------------------------------------------------------------------- #
# 一、步骤集合与顺序
# --------------------------------------------------------------------------- #
def test_step_set_and_order_match_upstream(facts) -> None:
    """六步**不多不少、顺序一致**。

    顺序是协议事实：`passengerInitApi` 提前调会回页面（候补链路上真机踩过），
    下单链路同理 —— 少一步或换顺序，官方只会回「系统忙」。
    """
    assert tuple(facts.ORDER_PATHS) == STEPS


# --------------------------------------------------------------------------- #
# 二、路径：六步全部一致
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("step", STEPS)
def test_paths_match_upstream(facts, self_forms, step: str) -> None:
    """六步的路径两边**完全一致**（本项目当初就是按官方路径写的）。"""
    assert self_forms.form(step).path == facts.ORDER_PATHS[step]


def test_upstream_wait_time_path_drops_the_query_suffix(facts) -> None:
    """上游常量里带着 ``?{}`` 占位（``...queryOrderWaitTime?{}``）。

    生成器必须把它**拆掉**只留路径：留着的话本项目会把 ``?{}`` 当路径的一部分
    发出去 —— 那是个 404/HTML 错误页，而且看起来像「被风控」。
    """
    assert facts.ORDER_PATHS["query_wait_time"].endswith("/queryOrderWaitTime")
    assert "?" not in facts.ORDER_PATHS["query_wait_time"]


# --------------------------------------------------------------------------- #
# 三、请求方法
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("step", STEPS)
def test_methods_match_upstream(self_forms, upstream_forms, step: str) -> None:
    """只有排队轮询是 GET，其余是 POST —— 两边一致。"""
    assert self_forms.form(step).method == upstream_forms.form(step).method
    assert self_forms.form(STEP_QUERY_WAIT_TIME).method == "get"


# --------------------------------------------------------------------------- #
# 四、Referer：**本项目全带、上游全不带**
# --------------------------------------------------------------------------- #
def test_referer_difference_is_exactly_this(self_forms, upstream_forms) -> None:
    """防的是：有人「顺手给上游补 Referer」，把差异抹平。

    上游六步都不带 Referer。本项目每一道都带同源 Referer —— 真机上
    「没逛过页面」的请求会被 WAF 拒（回 HTML 错误页而不是 4xx）。
    **这个差异必须留在能被看见的地方**：切默认值之前，它正是「上游那条路
    从没被真机检验过」的证据。
    """
    ours = {step: self_forms.form(step).referer for step in STEPS}
    theirs = {step: upstream_forms.form(step).referer for step in STEPS}
    assert all(ours.values()), f"本项目每步都应带 Referer，实际 {ours}"
    assert all(v == "" for v in theirs.values()), f"上游每步都不该带，实际 {theirs}"


# --------------------------------------------------------------------------- #
# 五、字段名：差异必须**只有**清单里那几处
# --------------------------------------------------------------------------- #
def test_submit_extra_field_is_only_undefined(self_forms, upstream_forms) -> None:
    """``submit``：本项目**多传** ``undefined=""``，其余字段名一致。

    ⚠️ 上游没有这个字段。多传一个空字段在表单编码下通常无害，
    但「无害」是**推断**，不是证据 —— 所以它留在差异表里，不删也不加。
    """
    ours = set(self_forms.form(STEP_SUBMIT).keys)
    theirs = set(upstream_forms.form(STEP_SUBMIT).keys)
    assert ours - theirs == {"undefined"}
    assert theirs - ours == set()


@pytest.mark.parametrize(
    "step",
    [STEP_CHECK_ORDER_INFO, STEP_GET_QUEUE_COUNT, STEP_CONFIRM_SINGLE_FOR_QUEUE,
     STEP_QUERY_WAIT_TIME, "init_dc"],
)
def test_other_steps_have_identical_field_names(self_forms, upstream_forms, step: str) -> None:
    """其余五步的字段名**完全一致**（含顺序）。

    顺序一起比：官方对字段顺序不敏感，但顺序一变 diff 就看不见真差异了 ——
    钉住顺序，是为了让「差异表」始终可信。
    """
    assert self_forms.form(step).keys == upstream_forms.form(step).keys


def test_field_order_of_submit_is_upstream_order_plus_undefined(self_forms, upstream_forms) -> None:
    """``submit`` 的字段顺序 = 上游顺序 + 末尾 ``undefined``。"""
    ours = self_forms.form(STEP_SUBMIT).keys
    theirs = upstream_forms.form(STEP_SUBMIT).keys
    assert ours[: len(theirs)] == theirs
    assert ours[len(theirs):] == ("undefined",)


# --------------------------------------------------------------------------- #
# 六、固定取值：能对上的逐项对上；对不上的**逐个列清**
# --------------------------------------------------------------------------- #
#: 每一步「两边都写死、因而可以逐项对照」的字段。
#: **写全**而不是设个下限 —— 少一项（有人把某个字段改成动态）要变红。
EXPECTED_SHARED_CONST: dict[str, tuple[str, ...]] = {
    STEP_SUBMIT: ("purpose_codes",),
    "init_dc": ("_json_att",),
    STEP_CHECK_ORDER_INFO: ("_json_att", "bed_level_order_num", "cancel_flag", "whatsSelect"),
    STEP_GET_QUEUE_COUNT: ("_json_att",),
    STEP_CONFIRM_SINGLE_FOR_QUEUE: (
        "_json_att", "dwAll", "roomType", "seatDetailType", "whatsSelect",
    ),
    STEP_QUERY_WAIT_TIME: ("_json_att",),
}


@pytest.mark.parametrize("step", STEPS)
def test_shared_const_values_agree_by_string(self_forms, upstream_forms, step: str) -> None:
    """两边都写死的同名字段，取值必须一致（按字符串比 —— 表单编码后就是字符串）。

    这就是 ``cancel_flag`` 的情形：上游写**整数** ``2``，本项目写**字符串** ``"2"``。
    发出去完全相同，所以这里按字符串比；而「两边类型不同」这件事由下面
    单独一条用例钉住 —— 它现在无害，但哪天有人把字段当数字用就会出事。
    """
    ours = self_forms.form(step).const
    theirs = upstream_forms.form(step).const
    shared = tuple(sorted(set(ours) & set(theirs)))
    assert shared == EXPECTED_SHARED_CONST[step], f"步骤 {step} 可对照的固定取值变了"
    for key in shared:
        assert str(ours[key]) == str(theirs[key]), f"{step}.{key} 取值不同"


def test_cancel_flag_type_difference_is_recorded(self_forms, upstream_forms) -> None:
    """``cancel_flag``：上游是 ``int``，本项目是 ``str``。**差异留档**。"""
    ours = self_forms.form(STEP_CHECK_ORDER_INFO).const["cancel_flag"]
    theirs = upstream_forms.form(STEP_CHECK_ORDER_INFO).const["cancel_flag"]
    assert (isinstance(ours, str), isinstance(theirs, int)) == (True, True)
    assert ours == "2" and theirs == 2


def test_bed_level_order_num_is_thirty_zeros_on_both_sides(self_forms, upstream_forms) -> None:
    """``bed_level_order_num`` 是 **30 个 0** 的字符串 —— 少一个 0 官方只会回「系统忙」。

    这类「看起来像凑数的常量」最容易被顺手「清理」成 ``"0"``，所以钉死。
    """
    for spec in (self_forms, upstream_forms):
        value = spec.form(STEP_CHECK_ORDER_INFO).const["bed_level_order_num"]
        assert value == "0" * 30, f"{spec.name} 的 bed_level_order_num 不对：{value!r}"


def test_confirm_single_const_values_match(self_forms, upstream_forms) -> None:
    """``confirmSingleForQueue`` 的固定取值逐项一致（``seatDetailType``/``roomType``/``dwAll``…）。"""
    ours = self_forms.form(STEP_CONFIRM_SINGLE_FOR_QUEUE).const
    theirs = upstream_forms.form(STEP_CONFIRM_SINGLE_FOR_QUEUE).const
    for key in ("seatDetailType", "whatsSelect", "roomType", "dwAll", "_json_att"):
        assert str(ours[key]) == str(theirs[key]), key
    assert ours["seatDetailType"] == "000"
    assert ours["roomType"] == "00"
    assert ours["dwAll"] == "N"


# --------------------------------------------------------------------------- #
# 七、**取值来源不同**的两处：显式对照
# --------------------------------------------------------------------------- #
def test_back_train_date_value_source_is_a_recorded_difference(self_forms, upstream_forms) -> None:
    """``back_train_date``：本项目传**今天**，上游传**出发日**。

    单程票（``tour_flag=dc``）下官方多半忽略它，但「多半」不是证据 ——
    所以这一条差异在这里登记，而不是被悄悄统一掉。
    """
    assert self_forms.form(STEP_SUBMIT).source_of("back_train_date") == "back_train_date"
    assert upstream_forms.form(STEP_SUBMIT).source_of("back_train_date") == "train_date"


def test_random_value_source_is_a_recorded_difference(self_forms, upstream_forms) -> None:
    """``random``：本项目用**毫秒时间戳**，上游用**随机小数串**。"""
    assert self_forms.form(STEP_QUERY_WAIT_TIME).source_of("random") == "random_ms"
    assert upstream_forms.form(STEP_QUERY_WAIT_TIME).source_of("random") == "random_fraction"


# --------------------------------------------------------------------------- #
# 八、滑块三件套：只有上游有，且只在 checkOrderInfo 上
# --------------------------------------------------------------------------- #
def test_slide_fields_only_exist_upstream_and_only_on_check_order_info(
    self_forms, upstream_forms, facts
) -> None:
    """滑块三件套（``sessionId``/``sig``/``scene``）**只有上游实现**，且只挂在 checkOrderInfo 上。

    本项目此前没有滑块实现（人工兜底），所以它的 ``optional_keys`` 是空的 ——
    这个「空」是**事实**，不是遗漏：切到上游后端时，滑块才第一次有了程序化解法。
    """
    assert upstream_forms.form(STEP_CHECK_ORDER_INFO).optional_keys == facts.ORDER_SLIDE_FIELDS
    assert facts.ORDER_SLIDE_FIELDS == ("sessionId", "sig", "scene")
    assert self_forms.form(STEP_CHECK_ORDER_INFO).optional_keys == ()
    for step in STEPS:
        if step == STEP_CHECK_ORDER_INFO:
            continue
        assert self_forms.form(step).optional_keys == ()
        assert upstream_forms.form(step).optional_keys == ()


def test_slide_scene_value_matches_upstream(facts, upstream_forms) -> None:
    """滑块三件套里的 ``scene`` 上游写死 ``nc_login``（不是 ``nc_``别的）。"""
    assert facts.ORDER_SLIDE_CONST == {"scene": "nc_login"}
    assert upstream_forms.form(STEP_CHECK_ORDER_INFO).const.get("scene") == "nc_login"


def test_slide_selector_and_state_regex_are_machine_readable(facts) -> None:
    """上游的选择器与「要不要过滑块」的正则，必须能被机器读出来。

    判据要素（选择器 / 正则）**必须被记下来**：本项目踩过一次
    「判据没被记下来 → 离线只能证明自洽」的坑（回流 #26）。
    正则存的是**源码文本**（``r"...\\d?..."``），所以这里用 ``ast.literal_eval``
    还原并编译 —— 顺带证明它真的是个可用的正则。
    """
    assert facts.ORDER_SLIDE_SELECTOR == "#slide_passcode .nc-lang-cnt"
    source = facts.ORDER_SLIDE_STATE_REGEX_SOURCE
    pattern = ast.literal_eval(source)
    compiled = re.compile(pattern)
    # 上游的正则抓的是单引号里的一个数字：'0' / '1'
    assert compiled.search("var if_check_slide_passcode  ='1'").group(1) == "1"
    assert compiled.search("var if_check_slide_passcode  ='0'").group(1) == "0"


# --------------------------------------------------------------------------- #
# 九、协议层与生成物必须对得上（防止「两份数据各自漂移」）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "step,const_name",
    [
        (STEP_SUBMIT, "SUBMIT_ORDER_REQUEST"),
        ("init_dc", "INIT_DC"),
        (STEP_CHECK_ORDER_INFO, "CHECK_ORDER_INFO"),
        (STEP_GET_QUEUE_COUNT, "GET_QUEUE_COUNT"),
        (STEP_CONFIRM_SINGLE_FOR_QUEUE, "CONFIRM_SINGLE_FOR_QUEUE"),
        (STEP_QUERY_WAIT_TIME, "QUERY_ORDER_WAIT_TIME"),
    ],
)
def test_protocol_layer_matches_upstream(facts, step: str, const_name: str) -> None:
    """`inter/urls.py` 的路径常量 == 上游路径（**三方一致**：urls / facts / 规格）。"""
    assert getattr(urls_mod, const_name) == facts.ORDER_PATHS[step]
