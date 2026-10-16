# -*- coding: utf-8 -*-
"""静态规则数据的合法性校验测试（INV-12 / INV-13）。

    .venv\\Scripts\\python.exe tests/test_rules_validation.py

为什么要单独测"数据"（而不是只测函数）
    `rules.json` 是**静态数据**，但静态数据同样会有 Bug：一个分值放到错误档位、
    一条规则漏了 options，编译器不报错、单测也测不到，只会在真实评分里
    **静默算错分**（符号写反甚至把扣分变成加分）。
    所以这里既验证"真数据合法"，也**故意注入每一类错误**验证能被拦下
    —— 一个只会放行的校验器等于没有校验器。

不需要数据库、不调用大模型。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t0"

import copy
import os
import sys

from _harness import FAILED, PASSED, ROOT, check  # noqa: E402

import rules as R   # noqa: E402





def _has(errs: list, keyword: str) -> bool:
    return any(keyword in e for e in errs)


def test_real_data_is_valid():
    print("\n--- 1. 真规则表必须合法（否则一启动就炸）---")
    r = R.load_rules()                       # 加载期校验内置在 load_rules 里
    check("load_rules() 成功", isinstance(r, dict) and r.get("rule_version"))
    check("validate_rules(真数据) 零违规", R.validate_rules(r) == [],
          str(R.validate_rules(r))[:200])
    total = len(r.get("positive_rules") or []) + len(r.get("negative_rules") or [])
    check("规则条数与预期一致（15 条）", total == 15, str(total))
    check("engine=rule 的规则被排除在「必须表态」之外",
          all(rid != "negative_no_reply_2d"
              for rid in R.llm_rule_ids(r)))


def test_injected_faults_are_caught():
    print("\n--- 2. 故意注入每一类错误，必须被拦下 ---")
    base = R.load_rules()

    # ① 加分规则里出现负分（最致命：符号写反 = 把加分算成扣分）
    b = copy.deepcopy(base)
    b["positive_rules"][0]["options"][0]["score"] = -5
    e = R.validate_rules(b)
    check("★ 加分规则出现负分 → 被拦", _has(e, "存在非正分值"), str(e)[:120])

    # ② 扣分规则里出现正分
    b = copy.deepcopy(base)
    b["negative_rules"][0]["options"][0]["score"] = 3
    e = R.validate_rules(b)
    check("★ 扣分规则出现正分 → 被拦", _has(e, "存在非负分值"), str(e)[:120])

    # ③ 同一规则内分值重复（档位无法区分）
    b = copy.deepcopy(base)
    b["positive_rules"][1]["options"][1]["score"] = 2
    e = R.validate_rules(b)
    check("同规则内分值重复 → 被拦", _has(e, "档位分值重复"), str(e)[:120])

    # ④ polarity 写进规则体（应由分组决定，否则可能两处不一致）
    b = copy.deepcopy(base)
    b["negative_rules"][0]["polarity"] = "positive"
    e = R.validate_rules(b)
    check("规则体里塞 polarity → 被拦", _has(e, "不应包含 polarity"), str(e)[:120])

    # ⑤ 规则没有 options（永远不会命中）
    b = copy.deepcopy(base)
    b["positive_rules"][2]["options"] = []
    e = R.validate_rules(b)
    check("规则缺 options → 被拦", _has(e, "没有 options"), str(e)[:120])

    # ⑥ 档位缺 criteria（模型无法判断该档）
    b = copy.deepcopy(base)
    b["positive_rules"][0]["options"][0].pop("criteria", None)
    e = R.validate_rules(b)
    check("档位缺 criteria → 被拦", _has(e, "缺少 criteria"), str(e)[:120])

    # ⑦ 等级阈值非严格递减
    b = copy.deepcopy(base)
    b["grade_thresholds"] = [{"min": 6, "grade": "S"}, {"min": 6, "grade": "A"},
                             {"min": -999, "grade": "D"}]
    e = R.validate_rules(b)
    check("等级阈值非严格递减 → 被拦", _has(e, "严格递减"), str(e)[:120])

    # ⑧ 等级阈值未覆盖分数下界（有分数映射不到等级）
    b = copy.deepcopy(base)
    b["grade_thresholds"] = [{"min": 6, "grade": "S"}, {"min": 0, "grade": "C"}]
    e = R.validate_rules(b)
    check("等级阈值未覆盖下界 → 被拦", _has(e, "未覆盖分数下界"), str(e)[:120])

    # ⑨ score_bounds 反了
    b = copy.deepcopy(base)
    b["score_bounds"] = {"min": 20, "max": 10}
    e = R.validate_rules(b)
    check("score_bounds 反了 → 被拦", _has(e, "必须严格小于"), str(e)[:120])

    # ⑩ 正向规则声明 engine=rule（会被静默忽略）
    b = copy.deepcopy(base)
    b["positive_rules"][0]["engine"] = "rule"
    e = R.validate_rules(b)
    check("正向规则 engine=rule → 被拦（当前只支持负向）", _has(e, "只支持负向"), str(e)[:120])


def test_validator_is_not_trigger_happy():
    print("\n--- 3. 校验器不能误报（否则没人敢改规则）---")
    base = R.load_rules()
    # 删掉一个中间档位：覆盖仍完整、依旧合法 → **不应**报错
    b = copy.deepcopy(base)
    b["grade_thresholds"] = [{"min": 6, "grade": "S"}, {"min": 4, "grade": "A"},
                             {"min": 0, "grade": "C"}, {"min": -999, "grade": "D"}]
    check("删掉一个档位（覆盖仍完整）→ 不误报", R.validate_rules(b) == [],
          str(R.validate_rules(b))[:150])
    # 新增一条合法规则 → 不应报错
    b = copy.deepcopy(base)
    b["positive_rules"].append({
        "rule_id": "__test_new_rule__", "dimension": "测试维度",
        "options": [{"score": 1, "criteria": "测试条件"}]})
    check("新增一条合法规则 → 不误报", R.validate_rules(b) == [],
          str(R.validate_rules(b))[:150])


def test_load_raises_on_invalid_file():
    print("\n--- 4. 非法文件必须在**加载期**就抛错（而不是静默进运行时）---")
    import json
    import tempfile
    from pathlib import Path
    bad = copy.deepcopy(R.load_rules())
    bad["positive_rules"][0]["options"][0]["score"] = -99      # 符号写反
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bad_rules.json"
        p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        try:
            R.load_rules(p)
            check("★ 非法规则表 → load_rules 抛错", False, "居然没抛")
        except R.RulesInvalidError as e:
            check("★ 非法规则表 → load_rules 抛 RulesInvalidError", True)
            check("错误信息指出了**哪条规则**", "__test__" in str(e) or
                  "positive_coder_profile" in str(e), str(e)[:120])
        except Exception as e:                                  # noqa: BLE001
            check("★ 非法规则表 → 抛 RulesInvalidError", False, f"{type(e).__name__}: {e}")


def main():
    print("=== 静态规则数据合法性校验（INV-12 / INV-13）===")
    test_real_data_is_valid()
    test_injected_faults_are_caught()
    test_validator_is_not_trigger_happy()
    test_load_raises_on_invalid_file()
    print(f"\n===== {len(PASSED)} passed / {len(FAILED)} failed =====")
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
