# -*- coding: utf-8 -*-
"""置信度三层合成验收（架构 第六章 E4）。

对应架构条款
    §6.1  铁律：置信度必须「算出来」，不能「问出来」
    §6.2  ① 命中项级（engine / 逐字 / 模糊）· ② 会话级（均值×覆盖率−惩罚+一致性）· ③ 复核触发
    §6.3  命名分离：model_confidence（模型自报，不参与决策）/ session_confidence（系统算）
          / verification_status（人工验证状态）

关键不变量
    **`needs_human_review` 是改造前 `neg_items or unchecked_rules` 的超集** ——
    置信度这一层只允许"多想一层"，**不允许放松**原有的复核条件。

跑法：``.venv\\Scripts\\python.exe tests/test_confidence.py``
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "backend"))
sys.path.insert(0, BASE)

import confidence as cf          # noqa: E402
import db                        # noqa: E402
import rules as rules_mod        # noqa: E402
import scoring_service as ss     # noqa: E402

_TOTAL, _FAILED = 0, 0
_R = rules_mod.load_rules()
_RIS = rules_mod._index(_R)
_ENGINE_RID = next(rid for rid, r in _RIS.items() if r.get("engine") == "rule")


def check(name, cond, extra=""):
    global _TOTAL, _FAILED
    _TOTAL += 1
    if cond:
        print(f"  ok   {name}")
    else:
        _FAILED += 1
        print(f"  FAIL {name}  {extra}")


def P(rid, score, ev=(), eids=()):
    """造一个命中项（polarity 由规则推导，与 validate_items 口径一致）。"""
    return {"rule_id": rid, "score": score, "polarity": _RIS[rid]["polarity"],
            "reason": "t", "evidence": list(ev), "evidence_ids": list(eids)}


# ============================================================ ① 命中项级

def test_item():
    print("\n--- 1. ① 命中项级分档（§6.2）---")
    v, k = cf.item_confidence(P(_ENGINE_RID, -2), _RIS)
    check("engine=rule → 1.0", v == 1.0 and k == "engine", f"{v}/{k}")

    v, k = cf.item_confidence(P("negative_refuse", -3, ["#001 [06-01 10:00｜1天前] 客户: 还不想买课"],
                                ["#001"]), _RIS)
    check("证据含关键词 → 模糊命中（中文近义不算逐字）", k in ("fuzzy", "multi"), f"{v}/{k}")

    # 人工构造一个逐字命中：把规则关键词原样放进证据
    kw = sorted(cf._keywords(_RIS["negative_refuse"]))
    hit_ev = f"#001 [06-01 10:00｜1天前] 客户: {kw[0]}"
    v, k = cf.item_confidence(P("negative_refuse", -3, [hit_ev], ["#001"]), _RIS)
    check("证据原样含关键词 → 逐字命中 1.0", v == 1.0 and k == "exact", f"{v}/{k}  kw={kw[0]}")

    v, k = cf.item_confidence(P("negative_refuse", -3, ["#001 客户: 嗯嗯"], ["#001", "#002"]), _RIS)
    check("无字面依据 + ≥2 条证据 → multi 0.8", v == 0.8 and k == "multi", f"{v}/{k}")

    v, k = cf.item_confidence(P("negative_refuse", -3, ["#001 客户: 嗯嗯"], ["#001"]), _RIS)
    check("无字面依据 + 1 条证据 → fuzzy 0.7", v == 0.7 and k == "fuzzy", f"{v}/{k}")

    v, k = cf.item_confidence(P("negative_refuse", -3, [], ["#001"]), _RIS)
    check("回链上了但无原文行 → no_text 0.5", v == 0.5 and k == "no_text", f"{v}/{k}")

    v, k = cf.item_confidence(P("negative_refuse", -3, [], []), _RIS)
    check("无任何证据 → 0.0", v == 0.0 and k == "none", f"{v}/{k}")

    print("\n--- 2. 关键词提取与匹配（噪声控制）---")
    kws = cf._keywords(_RIS["negative_refuse"])
    check("提取到关键词", len(kws) >= 2, str(kws))
    check("不含超长整句（>10 字会带来噪声匹配）", all(len(k) <= 10 for k in kws), str(kws))
    check("不含泛词（客户/老师/消息…）",
          not (kws & {"客户", "老师", "消息", "内容"}), str(kws))
    check("泛词不会被误命中（'有的' 不该命中任何规则）",
          not any(cf._match_kw(k, "有的") for k in kws), str(kws))
    check("证据行前缀被剥掉（'客户:' 不参与匹配）",
          cf._strip_prefix("#004 [06-10 15:52｜126天前] 客户: 产品需要了解的资料") == "产品需要了解的资料",
          cf._strip_prefix("#004 [06-10 15:52｜126天前] 客户: 产品需要了解的资料"))
    check("大小写不敏感（rag ↔ RAG）", cf._match_kw("RAG", "我知道 rag 和 agent"), "大小写未忽略")
    check("中文近义容忍（'暂不考虑' ↔ '暂时不考虑'）",
          cf._match_kw("暂不考虑", "我暂时不考虑了"), "3 字子串容忍未生效")


# ============================================================ ② 会话级

def test_session():
    print("\n--- 3. ② 会话级合成公式（§6.2）---")
    pos = [P("positive_coder_profile", 2, ["#001 客户: 全栈编程"], ["#001"])]
    res = {"positive_items": pos, "negative_items": [], "unchecked_rules": [], "invalid_items": []}
    out = cf.session_confidence(pos, res, _R)
    check("无漏检 → coverage = 1.0", out["coverage"] == 1.0, str(out["coverage"]))
    check("无无效项 → invalid_ratio = 0", out["invalid_ratio"] == 0.0, str(out["invalid_ratio"]))
    check("无上次 → 无一致性加成", out["consistency_bonus"] == 0.0, str(out["consistency_bonus"]))
    check("session = 均值 × 覆盖率（此处两者相乘）",
          abs(out["session_confidence"] - out["mean_item_confidence"]) < 1e-6,
          f"{out['session_confidence']} vs {out['mean_item_confidence']}")

    # 漏检 7/14 → coverage 0.5 → 置信度腰斩
    res_half = dict(res, unchecked_rules=[f"r{i}" for i in range(7)])
    out2 = cf.session_confidence(pos, res_half, _R)
    check("漏检一半 → coverage = 0.5", out2["coverage"] == 0.5, str(out2["coverage"]))
    check("漏检会显著拉低会话置信度",
          out2["session_confidence"] < out["session_confidence"] * 0.6,
          f"{out2['session_confidence']} vs {out['session_confidence']}")

    # 无效项惩罚
    res_inv = dict(res, positive_items=pos + [P("positive_urgency", 1, [], [])],
                   invalid_items=[{"rule_id": "x"}] * 3)
    out3 = cf.session_confidence(pos, res_inv, _R)
    check("有无效项 → invalid_ratio > 0 且有惩罚", out3["invalid_ratio"] > 0, str(out3))

    # 一致性加成
    base = cf.session_confidence(pos, res, _R, last_grade="B", current_grade="B")
    flip = cf.session_confidence(pos, res, _R, last_grade="S", current_grade="D")
    check("与上次等级一致 → 正加成", base["consistency_bonus"] > 0, str(base["consistency_bonus"]))
    check("与上次等级翻转 ≥2 档 → 负加成", flip["consistency_bonus"] < 0, str(flip["consistency_bonus"]))
    check("等级一致使置信度更高", base["session_confidence"] > flip["session_confidence"],
          f"{base['session_confidence']} vs {flip['session_confidence']}")

    # 上下界
    zero = cf.session_confidence([], {"unchecked_rules": [], "invalid_items": []}, _R)
    check("无命中项 → 用中性值 0.5", zero["mean_item_confidence"] == 0.5, str(zero["mean_item_confidence"]))
    allfuzzy = [P("negative_refuse", -3, ["#001 x"], ["#001"]) for _ in range(20)]
    clamped = cf.session_confidence(allfuzzy, dict(res, invalid_items=[{"a": 1}] * 50,
                                                   unchecked_rules=[f"u{i}" for i in range(20)]),
                                    _R, last_grade="S", current_grade="D")
    check("结果被 clamp 到 [0,1]", 0.0 <= clamped["session_confidence"] <= 1.0,
          str(clamped["session_confidence"]))

    print("\n--- 4. grade_rank（算「跨几档」）---")
    check("S > A > B > C > D", cf.grade_rank("S") > cf.grade_rank("A") > cf.grade_rank("B")
          > cf.grade_rank("C") > cf.grade_rank("D"), "等级序错误")
    check("未知等级 → 0", cf.grade_rank("X") == 0 and cf.grade_rank(None) == 0)


# ============================================================ ③ 复核触发

def _mk(rid, score, ev=(), eids=("#001",)):
    return P(rid, score, ev or ["#001 客户: 嗯"], eids)


def test_triggers():
    print("\n--- 5. ③ 复核触发（六类，§6.2）---")

    def run(res, raw=0, grade="C", last=None, th=None, neg_override=None):
        score = {"raw_score": raw}
        neg = res.get("negative_items", []) if neg_override is None else neg_override
        conf = cf.session_confidence(res.get("positive_items", []) + neg,
                                     res, _R, last_grade=last, current_grade=grade)
        return cf.review_triggers(conf, score, res, _R, neg_items=neg,
                                  last_grade=last, current_grade=grade, thresholds=th)

    # 基线：无负分、无漏检、分数 3（不在门槛 0/2/4/6 上）、证据够硬
    clean = {"positive_items": [_mk("positive_coder_profile", 2, ["#001 在职编程技术岗"])],
             "negative_items": [], "unchecked_rules": [], "invalid_items": []}
    r = run(clean, raw=3)
    check("干净样本 → 不触发复核", r == [], str(r))

    r = run(dict(clean, negative_items=[_mk("negative_refuse", -3)]), raw=0)
    check("有语义负分 → has_negative", "has_negative" in r, str(r))

    r = run(dict(clean, negative_items=[_mk(_ENGINE_RID, -2)]), raw=1)
    check("仅引擎负分 → engine_negative_only（而非 has_negative）",
          r == ["engine_negative_only"], str(r))
    r_off = run(dict(clean, negative_items=[_mk(_ENGINE_RID, -2)]), raw=1,
                th={"engine_negative": False})
    check("engine_negative=false → 该原因消失（降噪旋钮）", "engine_negative_only" not in r_off, str(r_off))

    r = run(dict(clean, unchecked_rules=["r1"]), raw=3)
    check("有漏检 → unchecked_rules", "unchecked_rules" in r, str(r))

    r = run(clean, raw=4)   # 恰好压在 B 的门槛(4)上
    check("分数正好卡在等级门槛 → grade_boundary", "grade_boundary" in r, str(r))
    r = run(clean, raw=3)   # 门槛是 0/2/4/6，3 不在其上
    check("分数不在门槛上 → 不触发 grade_boundary", "grade_boundary" not in r, str(r))
    r = run(clean, raw=5, th={"boundary": 1})
    check("阈值可调（±1 → 5 距门槛 4 也触发）", "grade_boundary" in r, str(r))

    r = run(dict(clean, positive_items=[_mk("positive_coder_profile", 2, ["#001 嗯嗯"])]), raw=2)
    check("高权重(+2)项证据弱(fuzzy) → weak_evidence", "weak_evidence" in r, str(r))
    r = run(clean, raw=3)
    check("证据硬（逐字/engine）→ 不触发 weak_evidence", "weak_evidence" not in r, str(r))

    r = run(clean, raw=3, grade="D", last="S")
    check("与上次等级翻转 ≥2 档 → grade_flip", "grade_flip" in r, str(r))
    r = run(clean, raw=3, grade="C", last="B")
    check("等级只差 1 档 → 不触发 grade_flip", "grade_flip" not in r, str(r))

    r = run(dict(clean, unchecked_rules=[f"u{i}" for i in range(9)]), raw=1)
    check("覆盖率过低 → low_confidence", "low_confidence" in r, str(r))

    print("\n--- 6. ★ 不变量：needs_review 是原有条件的超集 ---")
    cases = [
        ({"positive_items": [_mk("positive_coder_profile", 2)], "negative_items": [],
          "unchecked_rules": [], "invalid_items": []}, 3),
        ({"positive_items": [], "negative_items": [_mk(_ENGINE_RID, -2)],
          "unchecked_rules": [], "invalid_items": []}, 1),
        ({"positive_items": [_mk("positive_coder_profile", 2)], "negative_items": [],
          "unchecked_rules": ["x"], "invalid_items": []}, 3),
        ({"positive_items": [], "negative_items": [_mk("negative_refuse", -3, ["#001 还不想买课"])],
          "unchecked_rules": ["y"], "invalid_items": []}, 0),
    ]
    ok = True
    for res, raw in cases:
        leg = bool(res["negative_items"] or res["unchecked_rules"])     # 改造前的判定
        conf = cf.session_confidence(res["positive_items"] + res["negative_items"],
                                     res, _R)
        now = bool(cf.review_triggers(conf, {"raw_score": raw}, res, _R))
        if leg and not now:
            ok = False
            print(f"     放松了！res={res} 旧={leg} 新={now}")
    check("旧判定为真要复核的，新判定**必须**也要（不放松）", ok, "出现放松")

    print("\n--- 6b. ★ 回归：引擎项只在「调用方合并后的 neg_items」里 ---")
    # 真实形态：res["negative_items"] 是模型判的（可能为空），引擎硬指标由 _score_one 后加。
    # 若 compute/review_triggers 读 res 而不是合并后的列表，就会漏判 → 有负分却无需复核。
    res_model_only = {"positive_items": [], "negative_items": [], "unchecked_rules": [],
                      "invalid_items": []}
    engine_item = _mk(_ENGINE_RID, -2, ["距最后一条员工消息已 96 小时无客户回复"], ("#001",))
    merged_neg = list(res_model_only["negative_items"]) + [engine_item]
    r_merged = run(res_model_only, raw=-2, grade="D", neg_override=merged_neg)
    check("引擎项经合并列表传入 → engine_negative_only 触发",
          r_merged == ["engine_negative_only"], str(r_merged))
    r_leak = run(res_model_only, raw=-2, grade="D", neg_override=[])
    check("对比：不传合并列表（错误用法）→ 不触发（复现那个 bug）", r_leak == [], str(r_leak))
    out_e = cf.compute([], merged_neg, res_model_only, {"raw_score": -2}, {"grade": "D"}, _R)
    check("compute 计入引擎项 → needs_human_review=True", out_e["needs_human_review"] is True,
          str(out_e["review_reasons"]))
    check("compute 的 items 含引擎项（session 置信度不被低估）",
          any(i["rule_id"] == _ENGINE_RID for i in out_e["items"]), str(out_e["items"])[:100])
    check("引擎项 item_confidence = 1.0", out_e["items"][0]["item_confidence"] == 1.0,
          str(out_e["items"][0]))

    print("\n--- 7. compute() 主入口 + 阈值可配 ---")
    res = {"positive_items": [_mk("positive_coder_profile", 2, ["#001 在职编程技术岗"])],
           "negative_items": [_mk("negative_refuse", -3, ["#002 还不想买课"])],
           "unchecked_rules": [], "invalid_items": []}
    out = cf.compute(res["positive_items"], res["negative_items"], res, {"raw_score": 0},
                     {"grade": "C"}, _R)
    check("compute 返回 session_confidence", isinstance(out["session_confidence"], float), str(out)[:80])
    check("compute 返回 needs_human_review", isinstance(out["needs_human_review"], bool))
    check("compute 返回 review_reasons 列表", isinstance(out["review_reasons"], list))
    check("compute 的 items 带 item_confidence",
          all("item_confidence" in i for i in out["items"]), str(out["items"])[:120])
    check("items 数量 = 正 + 负", len(out["items"]) == 2, str(len(out["items"])))


# ============================================================ DB 集成

def test_db():
    print("\n--- 8. 阈值配置读取（_confidence_thresholds）---")
    th = ss._confidence_thresholds()
    for k in ("conf_min", "boundary", "flip_steps", "weak_weight", "engine_negative"):
        check(f"阈值含 {k}", k in th, str(th))
    check("默认 engine_negative = True（不放松）", th["engine_negative"] is True, str(th))
    db.set_setting("review_engine_negative", "false")
    try:
        check("开关可被 app_setting 覆盖", ss._confidence_thresholds()["engine_negative"] is False,
              str(ss._confidence_thresholds()))
    finally:
        db.execute("DELETE FROM app_setting WHERE key='review_engine_negative'")

    print("\n--- 9. 新列已就位（§6.3 三项分离）---")
    cols = {c["column_name"] for c in db.query(
        """SELECT column_name FROM information_schema.columns
            WHERE table_name='customer_intent_assessment'""")}
    for c in ("model_confidence", "session_confidence", "verification_status", "review_reasons"):
        check(f"列 {c} 存在", c in cols, str(sorted(cols)))

    print("\n--- 10. 真实落库：新列可写入并读回（自造数据，不依赖生产现状）---")
    # ⚠️ 不能查「库里有没有新代码写的样本」——那是**依赖生产数据现状**的断言，
    # 批量跑完/清理后会莫名其妙变少（实测踩到：清理两条历史产物后 65→63）。
    # 改为自造一条 → 断言读回 → 清理，断言数恒定且可重复。
    aid = None
    try:
        aid = db.query_one(
            """INSERT INTO customer_intent_assessment
                 (relationship_key, staff_id, session_id, raw_score, positive_total,
                  negative_total, grade, session_confidence, review_reasons,
                  model_confidence, analyzed_at)
               VALUES (%s,%s,%s,-2,0,-2,'D',0.55,%s,0.9,now())
               RETURNING assessment_id""",
            ("_conftest_:s", "_conftest_", "_conftest_",
             db.as_json(["engine_negative_only"])))["assessment_id"]
        row = db.query_one("""SELECT assessment_id, session_confidence, review_reasons,
                                     model_confidence, confidence
                                FROM customer_intent_assessment WHERE assessment_id=%s""", (aid,))
        check("session_confidence 可写入并读回", row["session_confidence"] == 0.55, str(row))
        check("review_reasons 可写入并读回（JSONB 数组）",
              row["review_reasons"] == ["engine_negative_only"], str(row["review_reasons"]))
        check("confidence 兼容列为 NULL（E4：已停写）", row["confidence"] is None,
              str(row["confidence"]))
        check("model_confidence 独立保留（模型自报，仅留档）",
              row["model_confidence"] == 0.9, str(row["model_confidence"]))
    finally:
        if aid:
            db.execute("DELETE FROM customer_intent_assessment WHERE assessment_id=%s", (aid,))


def main():
    print("=== 置信度三层合成验收（架构 第六章 E4）===")
    test_item()
    test_session()
    test_triggers()
    test_db()
    print(f"\n===== {_TOTAL - _FAILED}/{_TOTAL} 通过 =====")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
