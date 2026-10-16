# -*- coding: utf-8 -*-
"""契约层单元测试（需要 pydantic，请用后端 venv 的 python 运行）。

    .venv\\Scripts\\python.exe tests/test_contracts.py
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t0"


import copy
import sys

from _harness import FAILED, PASSED, check  # noqa: E402

from pydantic import ValidationError          # noqa: E402

import contracts as C                          # noqa: E402





def raises(name, payload):
    try:
        C.JudgmentOut.model_validate(payload)
    except ValidationError as e:
        return C.render_validation_errors(e)
    return None


VALID = {
    "rule_checks": [
        {"rule_id": "positive_coder_profile", "hit": True, "score": 2,
         "evidence_ids": ["#12", "#013"], "reason": "在职编程技术岗"},
        {"rule_id": "positive_urgency", "hit": False, "score": None,
         "evidence_ids": [], "reason": "未提及时间要求"},
    ],
    "customer_profile_summary": "客户是 Java 后端",
    "sales_strategy": "推公开课",
    "model_confidence": 0.8,
}


def t_judgment_ok():
    j = C.JudgmentOut.model_validate(VALID)
    check("模型层：合法输出通过", True)
    check("编号零填充 #12 → #012", j.rule_checks[0].evidence_ids == ["#012", "#013"],
          str(j.rule_checks[0].evidence_ids))
    check("checked_rule_ids 可枚举", j.checked_rule_ids() == ["positive_coder_profile", "positive_urgency"])


def t_hit_score():
    p = copy.deepcopy(VALID)
    p["rule_checks"][1]["score"] = -1
    check("模型层：hit=false 带 score 被拦截", raises("x", p) is not None)
    p = copy.deepcopy(VALID)
    p["rule_checks"][0]["score"] = None
    check("模型层：hit=true 缺 score 被拦截", raises("x", p) is not None)
    p = copy.deepcopy(VALID)
    p["rule_checks"][0]["hit"] = False            # hit=false 但仍留着 score=2
    check("模型层：hit 改 false 但 score 残留被拦截", raises("x", p) is not None)


def t_evidence_format():
    p = copy.deepcopy(VALID)
    p["rule_checks"][0]["evidence_ids"] = ["abc"]
    msg = raises("x", p)
    check("模型层：非法编号被拦截", msg is not None and "格式非法" in msg, (msg or "")[:60])


def t_extra_forbid():
    p = copy.deepcopy(VALID)
    p["total_score"] = 99
    check("模型层：多余键被拦截（extra=forbid）", raises("x", p) is not None)
    p = copy.deepcopy(VALID)
    p["rule_checks"][0]["confidence"] = 0.9
    check("模型层：rule_check 内多余键被拦截", raises("x", p) is not None)


def t_empty_and_confidence():
    p = copy.deepcopy(VALID)
    p["rule_checks"] = []
    check("模型层：rule_checks 为空被拦截", raises("x", p) is not None)
    p = copy.deepcopy(VALID)
    p["model_confidence"] = 9.9
    check("模型层：置信度越界夹到 1.0", C.JudgmentOut.model_validate(p).model_confidence == 1.0)
    p = copy.deepcopy(VALID)
    p["model_confidence"] = None
    check("模型层：置信度缺省不报错", C.JudgmentOut.model_validate(p).model_confidence == 0.5)
    p = copy.deepcopy(VALID)
    p["customer_profile_summary"] = None
    check("模型层：摘要为 null 归一成空串", C.JudgmentOut.model_validate(p).customer_profile_summary == "")


def t_prompt_schema():
    import json as _json
    s = C.prompt_schema()
    check("schema 已展开、无 $ref", "$ref" not in s)
    check("schema 已去 title", '"title"' not in s)
    check("schema 已去掉开发者注释（description）", '"description"' not in s)
    check("schema 含 rule_checks", "rule_checks" in s)
    parsed = _json.loads(s)
    score = parsed["properties"]["rule_checks"]["items"]["properties"]["score"]
    check("Optional[int] 折成 type 数组", score.get("type") == ["integer", "null"], str(score))
    check("schema 是合法 JSON", isinstance(parsed, dict))


def t_assessment():
    items = [{"rule_id": "positive_coder_profile", "dimension": "程序员或转型画像",
              "polarity": "positive", "score": 2, "reason": "在职编程",
              "evidence": ["#001 [10-08 09:12] 客户: 我是做 Java 后端的"],
              "evidence_ids": ["#001"], "evidence_message_ids": ["wm123", "wm124"]}]
    a = C.AssessmentOut.model_validate({
        "raw_score": 2, "positive_total": 2, "negative_total": 0, "grade": "B",
        "positive_items": items, "unchecked_rules": ["negative_refuse"],
        "profile_summary": "Java 后端", "sales_strategy": "推课",
        "model_confidence": 0.8, "rule_version": "v1.0", "prompt_version": "v2",
    })
    check("落库层：合法评估通过", a.grade == "B")
    check("落库层：evidence_message_ids 是字符串列表", a.positive_items[0].evidence_message_ids == ["wm123", "wm124"])
    check("落库层：coverage 计算正确", a.coverage() == 0.5, str(a.coverage()))

    bad = {"grade": "X"}
    try:
        C.AssessmentOut.model_validate(bad)
        check("落库层：非法等级被拦截", False)
    except ValidationError:
        check("落库层：非法等级被拦截", True)

    bad2 = {"needs_human_review": "maybe"}
    try:
        C.AssessmentOut.model_validate(bad2)
        check("落库层：无法识别的布尔值被拦截", False)
    except ValidationError:
        check("落库层：无法识别的布尔值被拦截", True)

    # pydantic 默认宽松模式会把 "yes"/"1" 这类值强转成 True，这里把行为固化下来：
    # 这类强转不值得我们为它触发一次修复重试。
    check("落库层：'yes' 被宽松强转为 True（已固化，不触发重试）",
          C.AssessmentOut.model_validate({"needs_human_review": "yes"}).needs_human_review is True)

    five = all(C.AssessmentOut.model_validate({"grade": g}).grade == g for g in "SABCD")
    check("落库层：S/A/B/C/D 五档均合法", five)


def t_derive_items_regression():
    """回归：derive_items / check_coverage 必须同时吃 pydantic 对象与 dict。

    曾经用 ``getattr(rc, k, None) or rc.get(k)`` 兜底 —— ``hit=False`` 是 falsy，
    会继续求值右边，而 pydantic 模型没有 ``.get``，端到端实测直接 AttributeError。
    """
    import rules as R

    rules = R.load_rules()
    j = C.JudgmentOut.model_validate({
        "rule_checks": [
            {"rule_id": "positive_coder_profile", "hit": True, "score": 2,
             "evidence_ids": ["#001"], "reason": "在职编程"},
            {"rule_id": "positive_urgency", "hit": False, "score": None,
             "evidence_ids": [], "reason": "未提及"},
            {"rule_id": "negative_no_reply_2d", "hit": True, "score": -2,
             "evidence_ids": ["#002"], "reason": "模型不该判这条"},
        ],
        "model_confidence": 0.7,
    })
    pos, neg, bad = R.derive_items(j.rule_checks, rules)
    check("派生：对象入参不抛异常且命中项正确", len(pos) == 1 and pos[0]["score"] == 2)
    check("派生：engine=rule 的规则被忽略（不重复扣分）",
          all(i["rule_id"] != "negative_no_reply_2d" for i in pos + neg))
    check("派生：hit=false 不进 items", all(i["rule_id"] != "positive_urgency" for i in pos + neg))

    # dict 入参同样可用（CLI / 旧调用方）
    pos2, neg2, _ = R.derive_items([dict(rc) for rc in j.rule_checks], rules)
    check("派生：dict 入参与对象入参结果一致",
          [i["rule_id"] for i in pos2] == [i["rule_id"] for i in pos])

    unchecked = R.check_coverage(j.rule_checks, rules)
    check("覆盖率：分母是 14 条 llm 规则", len(R.llm_rule_ids(rules)) == 14, str(len(R.llm_rule_ids(rules))))
    check("覆盖率：漏检数与预期一致", len(unchecked) == 14 - 2, str(len(unchecked)))
    check("覆盖率：漏检集合里不含 engine=rule 的规则", "negative_no_reply_2d" not in unchecked)


def t_polarity_guard():
    """跨列表极性校验：扣分规则被误放进 positive_items 会直接污染 positive_total。"""
    import rules as R

    rules = R.load_rules()
    wrong = [{"rule_id": "negative_refuse", "score": -3, "evidence_ids": ["#001"]}]
    valid, invalid = R.validate_items(wrong, rules, expect_polarity="positive")
    check("极性：负分规则放进 positive_items 被拦截", not valid and len(invalid) == 1)
    valid2, invalid2 = R.validate_items(wrong, rules, expect_polarity="negative")
    check("极性：同一项放进 negative_items 合法", len(valid2) == 1 and not invalid2)


def main():
    for fn in (t_judgment_ok, t_hit_score, t_evidence_format, t_extra_forbid,
               t_empty_and_confidence, t_prompt_schema, t_assessment,
               t_derive_items_regression, t_polarity_guard):
        print(f"\n--- {fn.__name__} ---")
        fn()
    print(f"\n===== {len(PASSED)} passed / {len(FAILED)} failed =====")
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
