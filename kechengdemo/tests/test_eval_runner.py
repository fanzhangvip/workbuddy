# -*- coding: utf-8 -*-
"""评估集验收（架构 第五章 · 闸6）。

对应架构条款
    §5.1  只标「命中了哪些 rule_id + 档位」，不标总分（总分由 rules.py 算）
    §5.2  `messages_hash` 防样本漂移
    §5.3  分层抽样（等级 × 是否有负分 × 消息数分档）；冻结后不得修改
    §5.4  回归门禁：F1 / 等级一致率不降、误报率不升，否则阻断
    铁律  **跑分只算不落库** —— 不得写 assessment / rule_tags / 触发回写

跑法：``.venv\\Scripts\\python.exe tests/test_eval_runner.py``
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"


import sys

from _harness import check, delete_rows, summary  # noqa: E402

import analyze                   # noqa: E402
import api_eval                  # noqa: E402
import db                        # noqa: E402
import eval_runner as er         # noqa: E402
import rules as rules_mod        # noqa: E402
import scoring_service as ss     # noqa: E402

_KIND = "edge"                   # 用最不可能有真实数据的层级做测试




def _pick(min_msgs=60):
    return db.query_one(
        """SELECT c.staff_id, c.session_id, c.target_id, c.msg_count
             FROM chat_session c
            WHERE c.session_type='ext_user' AND c.target_id IS NOT NULL AND c.msg_count >= %s
            ORDER BY c.msg_count DESC, c.session_id LIMIT 1""", (min_msgs,))


# ============================================================ 纯函数：指标

def test_metrics():
    print("\n--- 1. 指标计算（构造已知 TP/FP/FN）---")
    P = lambda *rids: [{"rule_id": r, "score": 1} for r in rids]
    N = lambda *rids: [{"rule_id": r, "score": -1} for r in rids]

    # 全对：期望 {a,b}，实际 {a,b} → F1 = 1
    m = er.metrics([{"expected_items": P("a", "b"), "actual_items": P("a", "b"),
                     "expected_grade": "B", "actual_grade": "B"}])
    check("全命中 → F1 = 1", m["rule_f1"] == 1.0, str(m["rule_f1"]))
    check("全命中 → 等级一致率 = 1", m["grade_acc"] == 1.0, str(m["grade_acc"]))
    check("无误报 → 误报率 = 0", m["fp_case_rate"] == 0.0, str(m["fp_case_rate"]))
    check("TP 计数正确", m["tp"] == 2 and m["fp"] == 0 and m["fn"] == 0, str(m))

    # 有误报：期望 {a}，实际 {a,b} → TP=1 FP=1 → precision=0.5, recall=1 → F1=2/3
    m2 = er.metrics([{"expected_items": P("a"), "actual_items": P("a", "b"),
                      "expected_grade": "B", "actual_grade": "B"}])
    check("有误报 → precision = 0.5", m2["precision"] == 0.5, str(m2["precision"]))
    check("有误报 → recall = 1", m2["recall"] == 1.0, str(m2["recall"]))
    check("有误报 → F1 ≈ 0.6667", abs(m2["rule_f1"] - 0.6667) < 1e-4, str(m2["rule_f1"]))
    check("有误报 → 误报率 = 1（该条有 FP）", m2["fp_case_rate"] == 1.0, str(m2["fp_case_rate"]))
    check("FP 计入", m2["fp"] == 1, str(m2["fp"]))

    # 有漏判：期望 {a,b}，实际 {a} → FN=1
    m3 = er.metrics([{"expected_items": P("a", "b"), "actual_items": P("a"),
                      "expected_grade": "B", "actual_grade": "B"}])
    check("有漏判 → recall = 0.5", m3["recall"] == 0.5, str(m3["recall"]))
    check("漏判进入 miss_top（可诊断哪个维度被漏）",
          m3["miss_top"].get("b") == 1, str(m3["miss_top"]))

    # 档位错：期望 a=+2，实际 a=+1 → rule 级命中但 exact 不符
    m4 = er.metrics([{"expected_items": [{"rule_id": "a", "score": 2}],
                      "actual_items": [{"rule_id": "a", "score": 1}],
                      "expected_grade": "B", "actual_grade": "B"}])
    check("档位错不影响 rule 级 F1", m4["rule_f1"] == 1.0, str(m4["rule_f1"]))
    check("档位错被 item_exact_rate 抓到（= 0）", m4["item_exact_rate"] == 0.0,
          str(m4["item_exact_rate"]))

    # 等级翻转
    m5 = er.metrics([{"expected_items": P("a"), "actual_items": P("a"),
                      "expected_grade": "S", "actual_grade": "C"}])
    check("等级翻转 → 一致率 = 0", m5["grade_acc"] == 0.0, str(m5["grade_acc"]))

    # 被跳过的样本（actual_grade=None）不计入
    m6 = er.metrics([{"expected_items": P("a"), "actual_items": [], "expected_grade": "B",
                      "actual_grade": None}])
    check("被跳过的样本不计入 case_valid", m6["case_valid"] == 0, str(m6["case_valid"]))
    check("被跳过的样本不算误报", m6["fp_case_rate"] == 0.0, str(m6["fp_case_rate"]))

    # 负分项同样参与
    m7 = er.metrics([{"expected_items": N("x"), "actual_items": N("x"),
                      "expected_grade": "D", "actual_grade": "D"}])
    check("负分项也计入命中", m7["tp"] == 1 and m7["rule_f1"] == 1.0, str(m7))


# ============================================================ 纯函数：门禁

def test_gate():
    print("\n--- 2. 回归门禁（方向敏感 + 容差）---")
    # ⚠️ 门禁主指标是 `rule_f1_llm`（**排除引擎硬指标**，见 `GATE_METRICS` 注释）——
    #    用 `rule_f1` 建夹具会让该项被「静默跳过」，于是"下降 → 阻断"永远测不出来
    #    （这个坑真踩过：改主指标时本用例立刻变红，正是它该有的表现）。
    base = {"rule_f1_llm": 0.80, "grade_acc": 0.70, "fp_case_rate": 0.20}

    ok, diffs = er.gate({"rule_f1_llm": 0.80, "grade_acc": 0.70, "fp_case_rate": 0.20}, base)
    check("指标持平 → 通过", ok is True, str(diffs))

    ok2, d2 = er.gate({"rule_f1_llm": 0.90, "grade_acc": 0.75, "fp_case_rate": 0.10}, base)
    check("指标全面变好 → 通过", ok2 is True, str(d2))

    ok3, d3 = er.gate({"rule_f1_llm": 0.60, "grade_acc": 0.70, "fp_case_rate": 0.20}, base)
    check("F1 明显下降 → 阻断", ok3 is False, str(d3))
    check("阻断项标出 ok=false",
          any(x["metric"] == "rule_f1_llm" and not x["ok"] for x in d3), str(d3))

    ok4, d4 = er.gate({"rule_f1_llm": 0.80, "grade_acc": 0.70, "fp_case_rate": 0.35}, base)
    check("误报率上升 → 阻断（方向相反）", ok4 is False, str(d4))
    check("误报率方向的 delta 为负（升了 = 变差）",
          next(x for x in d4 if x["metric"] == "fp_case_rate")["delta"] > 0, str(d4))

    ok5, _ = er.gate({"rule_f1_llm": 0.79, "grade_acc": 0.69, "fp_case_rate": 0.21}, base)
    check("降幅在容差（2pp）内 → 不阻断", ok5 is True, "容差未生效")

    ok6, _ = er.gate({"rule_f1_llm": 0.80}, {})     # 没基线
    check("无基线 → 不阻断（视为通过）", ok6 is True, str(ok6))


# ============================================================ DB：指纹 / 抽样 / 只算不落库

def test_hash_and_sample():
    S = _pick(60)
    if not S:
        print("\n!!! 找不到长会话，跳过指纹/抽样测试")
        return
    print(f"\n--- 3. 对话指纹（样本 {S['staff_id'][:10]}… {S['msg_count']} 条）---")
    h1 = er.messages_hash(S["staff_id"], S["session_id"])
    h2 = er.messages_hash(S["staff_id"], S["session_id"])
    check("同一会话两次指纹一致（渲染时间不参与，不会隔天漂移）", h1 == h2, f"{h1} vs {h2}")
    other = db.query_one(
        """SELECT staff_id, session_id FROM chat_session
            WHERE session_type='ext_user' AND target_id IS NOT NULL AND session_id <> %s
              AND msg_count >= 60 LIMIT 1""", (S["session_id"],))
    check("不同会话指纹不同",
          er.messages_hash(other["staff_id"], other["session_id"]) != h1, "指纹碰撞")

    # 内容真的变了 → 指纹必须变（用一条合成消息，finally 恢复）
    mid = "__evaltest__"
    try:
        db.execute("""INSERT INTO chat_message (staff_id, session_id, message_id, direction,
                        msg_time, content, plain)
                      VALUES (%s,%s,%s,'send',%s,%s,NULL)""",
                   (S["staff_id"], S["session_id"], mid, 1893456000, "指纹探测消息"))
        h3 = er.messages_hash(S["staff_id"], S["session_id"])
        check("新增一条消息 → 指纹变化", h3 != h1, f"{h1} vs {h3}")
    finally:
        delete_rows("chat_message", message_id=mid)
    check("删除后指纹恢复", er.messages_hash(S["staff_id"], S["session_id"]) == h1, "未恢复")

    print("\n--- 4. 分层抽样（等级 × 负分 × 消息数）---")
    before = db.query_one("SELECT count(*) AS n FROM eval_case WHERE kind=%s", (_KIND,))["n"]
    res = er.sample(kind=_KIND, limit=12, min_messages=20)
    created = res.get("case_ids") or []
    created2 = []
    try:
        check("抽样成功并落库", res["created"] == len(created) and len(created) > 0, str(res))
        check("分布跨多个层（不是单一等级堆在一起）",
              len(res.get("distribution") or {}) > 1, str(res.get("distribution")))
        rows = db.query(
            """SELECT case_id, kind, messages_hash, expected_items, baseline_grade
                 FROM eval_case WHERE case_id = ANY(%s)""", (created,))
        check("每条都带 messages_hash", all(r["messages_hash"] for r in rows), str(rows[:2]))
        check("刚抽样时 expected_items 为空（待人工标注）",
              all(r["expected_items"] is None for r in rows))
        check("kind 正确", all(r["kind"] == _KIND for r in rows))

        res2 = er.sample(kind=_KIND, limit=12, min_messages=20)
        created2 = res2.get("case_ids") or []
        dup = set(created) & set(created2)
        check("重复抽样不会抽到同一会话（去重）", dup == set(), str(dup))
    finally:
        # ⚠️ 两轮抽样的行都要清：只清第一轮会留残留（实测踩到过：残留 24 行 `edge`）
        all_created = list(created) + list(created2)
        if all_created:
            db.execute("DELETE FROM eval_case WHERE case_id = ANY(%s)", (all_created,))
    check("清理完成", db.query_one("SELECT count(*) AS n FROM eval_case WHERE kind=%s",
                                   (_KIND,))["n"] == before, "残留")

    print("\n--- 5. 跑分只算不落库（铁律）---")
    S2 = _pick(60)
    made = None
    run_id = None
    n_assess = db.query_one("SELECT count(*) AS n FROM customer_intent_assessment")["n"]
    n_tags = db.query_one(
        "SELECT count(*) AS n FROM chat_message WHERE cardinality(rule_tags) > 0")["n"]
    n_outbox = db.query_one("SELECT count(*) AS n FROM writeback_outbox")["n"]

    stub_ret = {"positive_items": [{"rule_id": "positive_coder_profile", "score": 2,
                                    "dimension": "画像", "polarity": "positive",
                                    "reason": "stub", "evidence": [], "evidence_ids": ["#001"],
                                    "evidence_message_ids": ["stub"]}],
                "negative_items": [], "invalid_items": [], "unchecked_rules": [],
                "profile_summary": "", "sales_strategy": "", "model_confidence": 0.5,
                "rule_version": "v1.0", "prompt_version": "t", "schema_version": "1",
                "contract_attempts": 1}
    orig = analyze.analyze
    analyze.analyze = lambda *a, **kw: stub_ret
    try:
        # ⚠️ 金标准必须**连引擎硬指标一起标**：`run_case` 与 `_score_one` 一样，
        # 会把 `negative_no_reply_2d`（客户超 48h 未回复）等 engine 项**追加**进负分。
        # 若人标只写 stub 那一条，引擎项就会算成 fp（实测 F1 掉到 0.67）。
        facts = ss.compute_facts(S2["staff_id"], S2["session_id"], fetch_tags=False)
        eng = rules_mod.engine_negative_items(facts, rules_mod.load_rules())
        exp_items = [{"rule_id": "positive_coder_profile", "polarity": "positive",
                      "score": 2, "reason": "人工标注"}]
        exp_items += [{"rule_id": e["rule_id"], "polarity": e.get("polarity", "negative"),
                       "score": e["score"], "reason": "引擎硬指标（人工核对）"} for e in eng]
        exp_raw = 2 + sum(int(e.get("score") or 0) for e in eng)
        exp_grade = rules_mod.grade_of(exp_raw, rules_mod.load_rules())["grade"]
        made = db.query_one(
            """INSERT INTO eval_case (kind, staff_id, session_id, target_id, msg_count,
                   messages_hash, expected_items, expected_raw_score, expected_grade, source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'manual') RETURNING case_id""",
            (_KIND, S2["staff_id"], S2["session_id"], S2["target_id"], S2["msg_count"],
             er.messages_hash(S2["staff_id"], S2["session_id"]),
             db.as_json(exp_items), exp_raw, exp_grade))
        check("造好一条已标注样本", bool(made), str(made))

        out = er.run_sync(kind=_KIND, case_ids=[made["case_id"]], check_drift=True)
        run_id = out.get("run_id")
        check("跑分成功", out.get("ok") and out.get("case_total") == 1, str(out)[:200])
        check("无漂移（指纹一致）", out.get("drift") == [], str(out.get("drift")))
        check("指标算出 F1（stub 命中 vs 人标命中 → 1.0）",
              out["metrics"]["rule_f1"] == 1.0, str(out["metrics"]))
        check("等级一致率 1.0（人标等级按 rules.py 算，与系统一致）",
              out["metrics"]["grade_acc"] == 1.0, str(out["metrics"]))
        check("落了一条 eval_run", bool(run_id))

        check("★ 未写 assessment（只算不落库）",
              db.query_one("SELECT count(*) AS n FROM customer_intent_assessment")["n"] == n_assess,
              "评估污染了业务表！")
        check("★ 未写 rule_tags", db.query_one(
            "SELECT count(*) AS n FROM chat_message WHERE cardinality(rule_tags) > 0")["n"] == n_tags)
        check("★ 未入 outbox", db.query_one(
            "SELECT count(*) AS n FROM writeback_outbox")["n"] == n_outbox)

        print("\n--- 6. 漂移样本被自动跳过 ---")
        db.execute("UPDATE eval_case SET messages_hash='deadbeef' WHERE case_id=%s",
                   (made["case_id"],))
        out2 = er.run_sync(kind=_KIND, case_ids=[made["case_id"]])
        check("指纹不符 → 跳过并计入 drift",
              out2["case_total"] == 1 and made["case_id"] in out2["drift"], str(out2["drift"]))
        check("漂移样本不计入 case_ok", out2["case_ok"] == 0, str(out2["case_ok"]))

        print("\n--- 7. 门禁落库与读回 ---")
        base_saved = er.load_baseline()
        er.save_baseline({"rule_f1": 0.99, "grade_acc": 0.99, "fp_case_rate": 0.0},
                         kind=_KIND, note="测试基线")
        out3 = er.run_sync(kind=_KIND, case_ids=[made["case_id"]])
        check("有基线后跑分给出 gate_passed 判定",
              out3.get("has_baseline") is True and out3.get("gate_passed") is not None, str(out3)[:200])
        try:
            er.BASELINE_PATH.unlink()
        except FileNotFoundError:
            pass
        globals()["_BASE_BAK"] = base_saved

        print("\n--- 8. 冻结后不得修改（§5.3）---")
        db.execute("UPDATE eval_case SET frozen=true WHERE case_id=%s", (made["case_id"],))
        frozen = db.query_one("SELECT frozen FROM eval_case WHERE case_id=%s", (made["case_id"],))
        check("已置冻结", frozen["frozen"] is True)
    finally:
        analyze.analyze = orig
        if made:
            delete_rows("eval_case", case_id=made["case_id"])
        if run_id:
            db.execute("DELETE FROM eval_run WHERE id >= %s AND kind=%s", (run_id, _KIND))
        delete_rows("eval_run", kind=_KIND)
        check("清理完成（eval_case / eval_run 归零）",
              db.query_one("SELECT count(*) AS n FROM eval_case WHERE kind=%s", (_KIND,))["n"] == 0
              and db.query_one("SELECT count(*) AS n FROM eval_run WHERE kind=%s",
                               (_KIND,))["n"] == 0)


def test_prefill():
    """AI 预标注：把 AI 判定填成草稿，但**不得**被当成人标计入指标/门禁。"""
    print("\n--- 6. AI 预标注（草稿 ≠ 金标准）---")
    S = _pick(60)
    before = db.query_one("SELECT count(*) AS n FROM eval_case WHERE kind=%s", (_KIND,))["n"]
    # ⚠️ 用 **id 水位**清审计：`prefill` 写的 audit 是 `target=<kind>`（不是 case_id），
    #    按 case_id 删不掉（实测踩到：edge 的预标注审计连跑三次留 3 条）。
    audit_max = db.query_one("SELECT coalesce(max(id),0) AS n FROM audit_log")["n"]
    cid = None
    try:
        base_items = [{"rule_id": "positive_coder_profile", "polarity": "positive",
                       "score": 2, "reason": "AI 当时的判定"}]
        cid = db.query_one(
            """INSERT INTO eval_case (kind, staff_id, session_id, target_id, msg_count,
                   messages_hash, baseline_items, baseline_raw_score, baseline_grade, source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'sample') RETURNING case_id""",
            (_KIND, S["staff_id"], S["session_id"], S["target_id"], S["msg_count"],
             er.messages_hash(S["staff_id"], S["session_id"]),
             db.as_json(base_items), 2, "B"))["case_id"]
        check("造好样本：有 baseline，无 expected", bool(cid), str(cid))

        dry = er.prefill(kind=_KIND, dry_run=True)
        check("dry_run 不写库", dry.get("filled", 0) >= 1 and
              db.query_one("SELECT expected_items FROM eval_case WHERE case_id=%s",
                           (cid,))["expected_items"] is None, str(dry)[:120])

        res = er.prefill(kind=_KIND)
        row = db.query_one("""SELECT expected_items, expected_raw_score, expected_grade,
                                     prefilled, annotator FROM eval_case WHERE case_id=%s""", (cid,))
        check("★ 预标注已填入 expected_items", row["expected_items"] is not None, str(row)[:120])
        check("★ 打上 prefilled=true（否则会变成「AI 批改自己」）",
              row["prefilled"] is True, str(row["prefilled"]))
        check("总分/等级由本地引擎算（不信 AI 自报）",
              row["expected_raw_score"] == 2 and row["expected_grade"], str(row))
        check("annotator 标记为 AI 预标注", "AI" in (row["annotator"] or ""), row["annotator"])

        # 核心：默认口径必须排除预标注
        check("★ 跑分默认**不**取预标注样本",
              all(c["case_id"] != cid for c in er._cases(_KIND, 0)), "被错误计入")
        check("★ 提供对照开关（include_prefilled=True 才取到）",
              any(c["case_id"] == cid for c in er._cases(_KIND, 0, include_prefilled=True)))

        check("重复预标注不覆盖已有（幂等）", er.prefill(kind=_KIND)["filled"] == 0, "")

        # 人工保存 → 转「已确认」
        api_eval.save({"case_id": cid, "items": base_items, "annotator": "张三",
                       "note": "复核无误"})
        row2 = db.query_one("SELECT prefilled, annotator FROM eval_case WHERE case_id=%s", (cid,))
        check("★ 人工保存后 prefilled=false（这才算金标准）",
              row2["prefilled"] is False, str(row2))
        # ⚠️ 评审 P0-2 之后行为**有意变更**：`annotator` 由**服务端从凭据推导**，
        # 不再采信请求体里的值（否则可以伪造「这条是谁标注的」）。
        # 因此这里断言的是「等于服务端主体」而不是「等于入参 张三」——
        # 客户端自报值只允许进审计 detail 的 `reported_actor`（见下一段断言）。
        import security as _sec
        check("★ annotator 由服务端推导（不采信请求体）",
              row2["annotator"] == _sec.actor(), f"{row2['annotator']} vs {_sec.actor()}")
        check("★ 客户端自报值**不**进入 annotator 字段（旧契约已废弃，勿改回）",
              row2["annotator"] != "张三", str(row2))
        _au = db.query_one("SELECT detail FROM audit_log WHERE action='eval_annotate' "
                           "AND target=%s ORDER BY id DESC LIMIT 1", (str(cid),))
        check("★ 自报值降级进审计 reported_actor（留痕但标记不可信）",
              bool(_au) and (_au["detail"] or {}).get("reported_actor") == "张三",
              str(_au and _au["detail"]))
        _au = db.query_one("SELECT detail FROM audit_log WHERE action='eval_annotate' "
                           "AND target=%s ORDER BY id DESC LIMIT 1", (str(cid),))
        check("★ 自报值降级进审计 reported_actor（留痕但标记不可信）",
              bool(_au) and (_au["detail"] or {}).get("reported_actor") == "张三",
              str(_au and _au["detail"]))
        check("人工确认后被跑分取到",
              any(c["case_id"] == cid for c in er._cases(_KIND, 0)))
    finally:
        if cid:
            db.execute("DELETE FROM eval_case WHERE case_id = ANY(%s)", ([cid],))
            db.execute("DELETE FROM audit_log WHERE id > %s", (audit_max,))
        # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
        try: ss.repair_current_flags()
        except Exception: pass
    check("清理完成", db.query_one("SELECT count(*) AS n FROM eval_case WHERE kind=%s",
                                   (_KIND,))["n"] == before, "残留")


def main():
    print("=== 评估集验收（架构 第五章 / 闸6）===")
    test_metrics()
    test_gate()
    test_hash_and_sample()
    test_prefill()
    return summary()

if __name__ == "__main__":
    sys.exit(main())
