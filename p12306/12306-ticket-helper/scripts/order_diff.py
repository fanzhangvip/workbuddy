"""下单六步差分表：**两套实现到底往官方发什么**。

为什么这张表值得单独存在
------------------------
下单是本项目唯一**涉资金**的链路，而它的差异几乎都**看不到**：

- 六步的**路径完全相同** —— 「切了没切」不体现在 URL 上；
- 返回值也相同 —— 也不体现在结果上；
- 差别在**表单里放了什么**、**带不带 Referer**、**同一个字段的取值从哪来**。

⇒ 判据只能落在「真实发出去的那份表单」上。这张表把它打印出来，
并且**对着预期差异做断言** —— 只打印不断言的话，
「看起来有输出」会被当成「结论正确」（本项目真栽过一次：差分表逻辑写反了）。

用法::

    python scripts/order_diff.py

断言侧：``tests/test_order_baseline.py``（对着上游出处逐项比）
与 ``tests/test_kernel_order.py``（端到端看 mock 记下的表单）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from ticket_helper.kernel import registry  # noqa: E402
from ticket_helper.kernel.domains.order_submit import (  # noqa: E402
    STEP_CHECK_ORDER_INFO,
    STEP_CONFIRM_SINGLE_FOR_QUEUE,
    STEP_GET_QUEUE_COUNT,
    STEP_QUERY_WAIT_TIME,
    STEP_SUBMIT,
    STEPS,
    OrderFormsSpec,
    build_form,
)

DOMAIN = "order_submit"

#: 造一份「一次真实下单」的取值袋（值本身不重要，**用的是哪一份**才重要）。
SAMPLE_CTX: dict[str, object] = {
    "secretStr": "<secretStr>",
    "train_date": "2026-10-05",
    "back_train_date": "2026-09-30",
    "today": "2026-09-30",
    "tour_flag": "dc",
    "tourFlag": "dc",
    "query_from_station_name": "北京南",
    "query_to_station_name": "上海虹桥",
    "passengerTicketStr": "<票种>,0,1,张三,1,<证件号>,<手机>,N,<encStr>",
    "oldPassengerStr": "张三,1,<证件号>,1___ _ _",
    "randCode": "",
    "REPEAT_SUBMIT_TOKEN": "<token>",
    "train_no": "24000G610200",
    "stationTrainCode": "G6102",
    "seatType": "O",
    "fromStationTelecode": "VNP",
    "toStationTelecode": "AOH",
    "leftTicket": "<leftTicketStr>",
    "purpose_codes": "00",
    "train_location": "P2",
    "key_check_isChange": "<key_check>",
    "leftTicketStr": "<leftTicketStr>",
    "choose_seats": "",
    "random_ms": 1759199999000,
    "random_fraction": "8609371124501",
    "sessionId": "<sessionId>",
    "sig": "<sig>",
}

#: 期望的差异（**逐项**；多一处、少一处都要报错）
EXPECTED_REFERER_DIFF = True
EXPECTED_KEY_DIFF: dict[str, tuple[str, ...]] = {
    # 步骤 → 本项目**多出**的字段
    STEP_SUBMIT: ("undefined",),
}
EXPECTED_OPTIONAL_DIFF: dict[str, tuple[str, ...]] = {
    # 步骤 → 上游**独有的**可选字段组
    STEP_CHECK_ORDER_INFO: ("sessionId", "sig", "scene"),
}
EXPECTED_ALIAS_DIFF: dict[str, dict[str, str]] = {
    # 步骤 → 上游**取值来源不同**的字段
    STEP_SUBMIT: {"back_train_date": "train_date"},
    STEP_QUERY_WAIT_TIME: {"random": "random_fraction"},
}

#: 「同一个字段，**一边写死、一边是动态**」的差异 —— 也要逐项钉住。
#: 这类差异最不显眼：字段名一样、值也常常一样，只有**来源**不同。
EXPECTED_CONST_ONLY_OURS: dict[str, tuple[str, ...]] = {
    STEP_SUBMIT: ("undefined",),
    STEP_GET_QUEUE_COUNT: ("purpose_codes",),
    STEP_CONFIRM_SINGLE_FOR_QUEUE: ("purpose_codes",),
}
EXPECTED_CONST_ONLY_THEIRS: dict[str, tuple[str, ...]] = {
    STEP_SUBMIT: ("tour_flag",),
    STEP_CHECK_ORDER_INFO: ("randCode", "scene", "tour_flag"),
    STEP_CONFIRM_SINGLE_FOR_QUEUE: ("choose_seats", "randCode"),
    STEP_QUERY_WAIT_TIME: ("tourFlag",),
}

#: 本项目**自己**的取值来源映射（同样是数据，也要钉住）：
#: ``random`` 从毫秒时间戳取 —— 与上游的随机小数串形成对照。
EXPECTED_ALIAS_SELF: dict[str, dict[str, str]] = {
    STEP_QUERY_WAIT_TIME: {"random": "random_ms"},
}


def _spec_with(backend: str) -> OrderFormsSpec:
    previous = os.environ.get(registry.ENV_VAR)
    os.environ[registry.ENV_VAR] = f"{DOMAIN}={backend}"
    registry.reset_cache()
    try:
        return registry.get(DOMAIN).forms()
    finally:
        if previous is None:
            os.environ.pop(registry.ENV_VAR, None)
        else:
            os.environ[registry.ENV_VAR] = previous
        registry.reset_cache()


def _wire_form(spec: OrderFormsSpec, step: str, ctx: dict) -> dict:
    """按规格把取值组装成**真正发出去的那份表单**。"""
    return build_form(spec.form(step), ctx)


def main() -> int:
    self_spec = _spec_with("self")
    up_spec = _spec_with("pjialin")

    print("=" * 78)
    print("下单六步：两套实现对比（判据 = 真实发出去的表单，不是函数返回值）")
    print("=" * 78)

    problems: list[str] = []

    for step in STEPS:
        ours = self_spec.form(step)
        theirs = up_spec.form(step)
        print()
        print(f"── {step} " + "─" * max(0, 70 - len(step)))
        print(f"    {'路径':<8}: self {ours.path}")
        print(f"    {'':<8}  up   {theirs.path}")
        if ours.path != theirs.path:
            problems.append(f"{step}: 路径不一致（{ours.path} vs {theirs.path}）")
        print(f"    {'方法':<8}: self {ours.method.upper():<5} up {theirs.method.upper()}")
        print(f"    {'Referer':<8}: self {ours.referer or '（不带）':<28} up {theirs.referer or '（不带）'}")

        ours_keys = set(ours.keys)
        theirs_keys = set(theirs.keys)
        extra_ours = tuple(k for k in ours.keys if k not in theirs_keys)
        extra_theirs = tuple(k for k in theirs.keys if k not in ours_keys)
        expected_extra = EXPECTED_KEY_DIFF.get(step, ())
        if extra_ours != expected_extra or extra_theirs:
            problems.append(
                f"{step}: 字段名差异与预期不符 —— 本项目多 {extra_ours}（预期 {expected_extra}）、"
                f"上游多 {extra_theirs}（预期 ()）"
            )
        if extra_ours or extra_theirs:
            print(f"    {'字段名':<8}: 本项目多 {extra_ours or '（无）'} / 上游多 {extra_theirs or '（无）'}")

        # 取值来源不同
        ours_alias = dict(ours.alias)
        expected_alias_self = EXPECTED_ALIAS_SELF.get(step, {})
        if ours_alias != expected_alias_self:
            problems.append(
                f"{step}: 本项目的取值来源映射与预期不符：{ours_alias}（预期 {expected_alias_self}）"
            )
        expected_alias = EXPECTED_ALIAS_DIFF.get(step, {})
        if theirs.alias != expected_alias:
            problems.append(f"{step}: 上游取值来源差异与预期不符：{dict(theirs.alias)}（预期 {expected_alias}）")
        if expected_alias:
            for key, source in expected_alias.items():
                print(f"    {'取值来源':<8}: {key} 本项目取 {ours.source_of(key)!r} / 上游取 {source!r}")

        # 可选字段组
        expected_optional = EXPECTED_OPTIONAL_DIFF.get(step, ())
        if theirs.optional_keys != expected_optional or ours.optional_keys != ():
            problems.append(
                f"{step}: 可选字段组与预期不符 —— 上游 {theirs.optional_keys}（预期 {expected_optional}）"
                f"、本项目 {ours.optional_keys}（预期 ()）"
            )
        if expected_optional:
            print(f"    {'可选组':<8}: 上游独有 {theirs.optional_keys}（scene={theirs.const.get('scene')!r}）")

        # 固定取值差异（只比同名字段）
        shared = sorted(set(ours.const) & set(theirs.const))
        mismatched = [k for k in shared if str(ours.const[k]) != str(theirs.const[k])]
        if mismatched:
            problems.append(f"{step}: 同名字段的固定取值不一致 {mismatched}")
        only_ours = sorted(set(ours.const) - set(theirs.const))
        only_theirs = sorted(set(theirs.const) - set(ours.const))
        if tuple(only_ours) != EXPECTED_CONST_ONLY_OURS.get(step, ()):
            problems.append(
                f"{step}: 「本项目写死 / 上游动态」的字段与预期不符：{only_ours}"
                f"（预期 {list(EXPECTED_CONST_ONLY_OURS.get(step, ()))}）"
            )
        if tuple(only_theirs) != EXPECTED_CONST_ONLY_THEIRS.get(step, ()):
            problems.append(
                f"{step}: 「上游写死 / 本项目动态」的字段与预期不符：{only_theirs}"
                f"（预期 {list(EXPECTED_CONST_ONLY_THEIRS.get(step, ()))}）"
            )
        if only_ours:
            print(f"    {'固定值':<8}: 本项目写死而上游是动态的 {only_ours}")
        if only_theirs:
            print(f"    {'固定值':<8}: 上游写死而本项目是动态的 {only_theirs}")

    print()
    print("=" * 78)
    print("真正发出去的表单（同一份取值袋；两套后端各自组装）")
    print("=" * 78)
    for step in STEPS:
        print(f"\n── {step}")
        print(f"    self   : {_wire_form(self_spec, step, SAMPLE_CTX)}")
        print(f"    pjialin: {_wire_form(up_spec, step, SAMPLE_CTX)}")

    # 抽样断言：三处**语义性**差异必须在真实表单里看得见
    ours_submit = _wire_form(self_spec, STEP_SUBMIT, SAMPLE_CTX)
    theirs_submit = _wire_form(up_spec, STEP_SUBMIT, SAMPLE_CTX)
    if "undefined" not in ours_submit or "undefined" in theirs_submit:
        problems.append("submit 表单里 undefined 的差异没体现出来")
    if theirs_submit["back_train_date"] != SAMPLE_CTX["train_date"]:
        problems.append("上游 submit 的 back_train_date 应当等于出发日")
    if ours_submit["back_train_date"] != SAMPLE_CTX["today"]:
        problems.append("本项目 submit 的 back_train_date 应当等于今天")

    slide_form = _wire_form(up_spec, STEP_CHECK_ORDER_INFO, SAMPLE_CTX)
    for key in ("sessionId", "sig", "scene"):
        if key not in slide_form:
            problems.append(f"上游 checkOrderInfo 在给了滑块参数时应当带上 {key}")
    if slide_form.get("scene") != "nc_login":
        problems.append("上游 checkOrderInfo 的 scene 应当是 nc_login")

    print()
    print("-" * 78)
    if problems:
        print(f"✘ 与预期不符（{len(problems)} 处）：")
        for item in problems:
            print(f"    - {item}")
        return 1
    print("✔ 差异与文档一致：路径相同；Referer 本项目全带、上游全不带；")
    print("  字段名只差 submit 的 undefined；取值来源差两处；滑块三件套只在上游。")
    print("")
    print("⚠️  这张表**只说明差在哪**，不说明该不该切默认：")
    print("   - 上游六步都不带 Referer，真机上很可能被 WAF 拒（回 HTML 错误页）；")
    print("   - 「更接近上游」不是换行为的理由 —— 换默认值要真机证据（v3 §10-17）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
