# -*- coding: utf-8 -*-
"""第 2/3/4 批「架构全量落实」验收 —— 需要 PostgreSQL 5433。

    .venv/Scripts/python.exe tests/test_batch234.py

覆盖：
  · **E2** `is_current` / `superseded_by`（列表去重）
  · **B4** 有效信息量三口径
  · **E3** 结构化画像（8 槽位 + 逐槽位证据）
  · **闸1/F5** `examples` 机制 + 冲突优先级
  · **边界4** 数据保留（默认不清理、只清观测表）
  · **H2** `request_key` 幂等
  · **§2.5** 统一 HTTP wrapper（分类重试）
  · **G5** 失败重投 worker
  · **闸6** 门禁硬拦截（「无法判定」≠「通过」）
  · **E4** `verification_status` 写入
  · **F2** 早会/公开课复核入口
  · **E5** meta-loop（误报率排行 + 回改判回流）
  · **D2-1** 差值累加回写

⚠️ 全部用合成数据/打桩，不真调 LLM；跑完清理；断言不依赖生产现状。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"


import os
import sys

from _harness import FAILED, PASSED, check, delete_rows  # noqa: E402

import analyze                    # noqa: E402
import api_scoring                # noqa: E402
import contracts                  # noqa: E402
import db                         # noqa: E402
import failure_worker as fw       # noqa: E402
import hooks                      # noqa: E402
import http_client                # noqa: E402
import idempotency                # noqa: E402
import meta_loop as ml            # noqa: E402
import retention                  # noqa: E402
import rules as rules_mod         # noqa: E402
import scoring_service as ss      # noqa: E402

_REL = "B234TEST:subject"




#: 测试专用 session_id —— 用于隔离 eval_case（回流的样本会带走**真实** staff_id，
#: 所以按 staff_id 清不掉，必须按合成 session_id 清；这是踩过的坑）
_SESS = "_b234test_sess"

#: 测试开始时间戳 —— 用于清理「被间接触发的代码」写的审计（actor 不是测试自己的）
#: 测试开始时的 audit_log 最大 id（**水位**）。用它而不用时间戳：
#: id 单调、无时区问题；时间戳会连带删掉「测试窗口内真实运行写的审计」。
_AUDIT_MAX = 0


def _cleanup():
    # ⚠️ 关系键是 `staff:target`，合成标识在**第二段** → 必须按包含匹配（踩过）
    for tbl in ("writeback_ledger", "writeback_outbox", "negative_review",
                "score_writeback", "customer_intent_assessment"):
        db.execute(f"DELETE FROM {tbl} WHERE relationship_key LIKE %s", ("%B234TEST%",))
    db.execute("DELETE FROM eval_case WHERE session_id LIKE %s", ("_b234test%",))
    delete_rows("session_failure", staff_id="B234TEST")
    # audit_log：被间接触发的代码会用**自己的 actor** 写审计（如 meta_loop），
    # 按 actor 前缀删不干净 → 统一按「测试开始时间」删（通用做法，踩过）
    if _AUDIT_MAX:
        db.execute("DELETE FROM audit_log WHERE id > %s", (_AUDIT_MAX,))
    db.execute("DELETE FROM app_setting WHERE key LIKE %s", ("b234test%",))


def _session():
    r = db.query_one(
        """SELECT c.staff_id, c.session_id, c.target_id, c.name AS target_name,
                  c.msg_count, c.last_msg_time, s.name AS staff_name
             FROM chat_session c LEFT JOIN staff s ON s.staff_id=c.staff_id
            WHERE c.session_type='ext_user' AND c.target_id IS NOT NULL
              AND c.msg_count >= 8 ORDER BY c.msg_count DESC, c.session_id LIMIT 1""")
    return dict(r) if r else None


def _res(rid="positive_coder_profile", score=2):
    return {"positive_items": [{"rule_id": rid, "score": score, "dimension": "d",
                                "polarity": "positive", "reason": "t", "evidence": ["x"],
                                "evidence_ids": ["#001"], "evidence_message_ids": ["m"],
                                "evidence_verified": 1}],
            "negative_items": [], "invalid_items": [], "unchecked_rules": [],
            "model_confidence": 0.5, "rule_version": "v1.0", "prompt_version": "t",
            "schema_version": "1", "contract_attempts": 1,
            "model_name": "deepseek-chat", "rule_checks": []}


# ============================================================ E2

def test_e2_is_current():
    print("\n--- E2：is_current / superseded_by（列表去重）---")
    s = _session()
    cand = dict(s); cand["target_id"] = "B234TEST_E2"
    try:
        _cleanup()
        mk = lambda raw, g: ({"raw_score": raw, "positive_total": raw, "negative_total": 0},
                             {"grade": g, "priority": "", "advice": ""})
        a1 = ss.service._persist_assessment(0, cand, _res(), *mk(2, "B"), False, [],
                                            _res()["positive_items"], [], "off")
        a2 = ss.service._persist_assessment(0, cand, _res(), *mk(6, "S"), False, [],
                                            _res()["positive_items"], [], "off")
        rows = db.query("""SELECT assessment_id, is_current, superseded_by
                             FROM customer_intent_assessment
                            WHERE relationship_key LIKE %s ORDER BY assessment_id""",
                        ("%B234TEST_E2",))
        check("两条历史都保留", len(rows) == 2, str(len(rows)))
        check("★ 只有最新一条 is_current=true",
              rows[1]["is_current"] is True and rows[0]["is_current"] is False, str(rows))
        check("★ 旧行 superseded_by 指向新行",
              rows[0]["superseded_by"] == a2, str(rows[0]["superseded_by"]))
        out = api_scoring.assessments(staff_id=cand["staff_id"], include_history=False,
                                      page_size=100)
        mine = [i for i in out["items"] if i["target_id"] == "B234TEST_E2"]
        check("★ 列表默认只出现一次（同一客户不重复）", len(mine) == 1, str(len(mine)))
        out2 = api_scoring.assessments(staff_id=cand["staff_id"], include_history=True,
                                       page_size=100)
        mine2 = [i for i in out2["items"] if i["target_id"] == "B234TEST_E2"]
        check("include_history=true 时可看到 2 条", len(mine2) == 2, str(len(mine2)))
    finally:
        _cleanup()


def test_e2_selfheal():
    """E2 自愈：**删掉被取代列之后，旧行必须能恢复 current**。

    这不是假想场景 —— 生产实测出现过 1 例（#855）：新行被删除（测试清理），旧行的
    `is_current` 永久为 false、`superseded_by` 悬空 → 该客户从列表里静默消失。
    """
    print("\n--- E2 自愈：无 current 行 / 悬空 superseded_by ---")
    s = _session()
    cand = dict(s); cand["target_id"] = "B234TEST_E2H"
    try:
        _cleanup()
        mk = lambda raw, g: ({"raw_score": raw, "positive_total": raw, "negative_total": 0},
                             {"grade": g, "priority": "", "advice": ""})
        a1 = ss.service._persist_assessment(0, cand, _res(), *mk(2, "B"), False, [],
                                            _res()["positive_items"], [], "off")
        a2 = ss.service._persist_assessment(0, cand, _res(), *mk(6, "S"), False, [],
                                            _res()["positive_items"], [], "off")
        # 模拟「新行被外部删除」：留下旧行非 current、superseded_by 悬空
        # ⚠️ 这里**故意不修** —— 本用例就是要复现破损再验证自愈（不要在此加 repair）
        delete_rows("customer_intent_assessment", assessment_id=a2)
        rows = db.query("""SELECT is_current, superseded_by FROM customer_intent_assessment
                            WHERE relationship_key LIKE %s""", ("%B234TEST_E2H",))
        check("★ 复现破损：该客户一行 current 都没有",
              len(rows) == 1 and rows[0]["is_current"] is False, str(rows))

        res = ss.repair_current_flags()
        rows = db.query("""SELECT is_current, superseded_by FROM customer_intent_assessment
                            WHERE relationship_key LIKE %s""", ("%B234TEST_E2H",))
        check("★ 自愈：旧行恢复 current 且清掉悬空指针",
              rows[0]["is_current"] is True and rows[0]["superseded_by"] is None, str(rows))
        check("自愈结果含修复计数", res.get("no_current_fixed", 0) >= 1, str(res))
        again = ss.repair_current_flags()
        check("自愈幂等（再跑 0 改动）",
              again == {"no_current_fixed": 0, "dangling_cleared": 0}, str(again))
    finally:
        _cleanup()


# ============================================================ B4

def test_b4_effective():
    print("\n--- B4：有效信息量三口径 ---")
    check("客户未开口 → 拒绝（条数够也不行）",
          hooks.before_llm_call(kept=10, min_kept=4, customer_msgs=0,
                                min_customer_msgs=1).reason == "insufficient_content")
    check("文本过短 → 拒绝",
          hooks.before_llm_call(kept=10, min_kept=4, customer_msgs=2, min_customer_msgs=1,
                                text_len=10, min_effective_chars=100).detail["failed_on"]
          == "text_len")
    check("三口径全过 → 放行",
          hooks.before_llm_call(kept=10, min_kept=4, customer_msgs=2, min_customer_msgs=1,
                                text_len=999, min_effective_chars=100).allow is True)
    check("阈值 0 = 不限（不改变既有行为）",
          hooks.before_llm_call(kept=10, min_kept=4, customer_msgs=0, min_customer_msgs=0,
                                text_len=0, min_effective_chars=0).allow is True)
    # ctx 真的产出了这三个口径
    s = _session()
    ctx = ss.build_context(s["staff_id"], s["session_id"],
                           session_meta={"staff_id": s["staff_id"],
                                         "session_id": s["session_id"]},
                           rel_key=f"_b234_:{s['session_id']}")
    st = ctx["stats"]
    check("build_context 产出 customer_msgs / sales_msgs / text_len",
          all(k in st for k in ("customer_msgs", "sales_msgs", "text_len")), str(sorted(st))[:120])
    check("customer_msgs + sales_msgs = kept（口径自洽）",
          st["customer_msgs"] + st["sales_msgs"] == st["kept"], str(st["kept"]))


# ============================================================ E3

def test_e3_profile():
    print("\n--- E3：结构化画像（8 槽位 + 逐槽位证据）---")
    fields = [
        contracts.ProfileField(slot="position", value="在职后端开发", evidence_ids=["#001"]),
        contracts.ProfileField(slot="need", value="想做 RAG 知识库", evidence_ids=["#999"]),
        contracts.ProfileField(slot="bogus", value="未知槽位", evidence_ids=[]),
        contracts.ProfileField(slot="position", value="重复槽位", evidence_ids=[]),
    ]
    out = analyze._attach_profile_evidence(fields, {"#001": "m1"}, {"#001": "客户: 我是后端"})
    by = {o["slot"]: o for o in out}
    check("★ 固定 8 槽位齐全", len(out) == 8 and set(by) == set(contracts.PROFILE_SLOTS),
          str(sorted(by)))
    check("未知槽位被丢弃", "bogus" not in by)
    check("重复槽位只保留一条", by["position"]["value"] == "在职后端开发", str(by["position"]))
    check("能回链的槽位带出证据原文",
          by["position"]["evidence"] == ["客户: 我是后端"], str(by["position"]["evidence"]))
    check("★ 回链不上的槽位**保留 value**、只清空编号",
          by["need"]["value"] == "想做 RAG 知识库" and by["need"]["evidence_ids"] == [],
          str(by["need"]))
    check("缺席槽位补「未提及」且带 missing 标记",
          by["budget"]["value"] == "未提及" and by["budget"].get("missing") is True,
          str(by["budget"]))
    check("ProfileField 在模型契约里（JudgmentOut 有 customer_profile）",
          "customer_profile" in contracts.JudgmentOut.model_fields)
    check("落库契约有 profile_struct", "profile_struct" in contracts.AssessmentOut.model_fields)


# ============================================================ 闸1/F5

def test_examples():
    print("\n--- 闸1/F5：规则 examples 与冲突优先级 ---")
    r = rules_mod.load_rules()
    cov = rules_mod.examples_coverage(r)
    check("★ 绝大多数规则已带 examples（engine=rule 的除外）",
          cov["rules_with_examples"] >= cov["rules_total"] - 1, str(cov["coverage"]))
    check("三档示例都齐（positive/negative/ambiguous）",
          all(v > 0 for v in cov["examples_by_kind"].values()), str(cov["examples_by_kind"]))
    check("冲突优先级已配置", cov["rules_with_conflict_priority"] > 0,
          str(cov["rules_with_conflict_priority"]))
    brief = rules_mod.rules_brief_for_prompt(r)
    check("★ 提示词里出现「命中示例」", "命中示例" in brief)
    check("提示词里出现「不命中示例」", "不命中示例" in brief)
    check("提示词里出现「边界示例」", "边界示例" in brief)
    check("提示词里出现冲突优先级", "优先级" in brief)
    # 向后兼容：没有 examples 的规则输出与改造前一致
    bare = {"rule_version": "t", "positive_rules": [
        {"rule_id": "x", "dimension": "d", "options": [{"score": 1, "criteria": "c"}]}],
        "negative_rules": []}
    check("无 examples 时输出不含示例文案（向后兼容）",
          "示例" not in rules_mod.rules_brief_for_prompt(bare))


# ============================================================ 边界4

def test_retention():
    print("\n--- 边界4：数据保留（默认不清理，只清观测表）---")
    check("0 天 → 关闭（enabled=False）", retention.purge(0)["enabled"] is False)
    p = retention.purge(3650, dry_run=True)
    check("默认 dry_run 只预演（dry_run=True）", p.get("dry_run") is True)
    check("★ 业务结论表**不在**清理范围",
          all(t not in retention.PURGEABLE for t in
              ("customer_intent_assessment", "negative_review", "score_writeback")),
          str(sorted(retention.PURGEABLE)))
    check("★ 受保护表有明确理由（不是遗漏）",
          "score_writeback" in retention.PROTECTED and
          "差值" in retention.PROTECTED["score_writeback"],
          str(retention.PROTECTED.get("score_writeback")))
    check("chat_message 用 Unix 秒比较（类型正确，不报错）",
          retention._cutoff_expr("msg_time", 30)[0].startswith("msg_time < %s"))
    check("时间戳列用 interval 比较",
          "interval" in retention._cutoff_expr("created_at", 30)[0])


# ============================================================ H2

def test_idempotency():
    print("\n--- H2：request_key 幂等 ---")
    calls = {"n": 0}
    key = "b234test-key-1"
    try:
        _cleanup()
        delete_rows("request_guard", request_key=key)

        def fn():
            calls["n"] += 1
            return {"ok": True, "n": calls["n"]}

        r1 = idempotency.run_once(key, "t.fn", fn)
        r2 = idempotency.run_once(key, "t.fn", fn)
        check("首次真正执行", r1["n"] == 1 and not r1.get("_replayed"), str(r1))
        check("★ 重复调用**不执行**，直接复用首次结果",
              calls["n"] == 1 and r2.get("_replayed") is True, f"calls={calls['n']} {r2}")

        # 无 key → 不做幂等（保持原行为）
        r3 = idempotency.run_once(None, "t.fn", fn)
        r4 = idempotency.run_once(None, "t.fn", fn)
        check("无 key → 每次都执行（不改变既有行为）",
              r3["n"] != r4["n"] and calls["n"] == 3, str(calls["n"]))

        # 失败不留幂等记录（让重试有机会成功）
        key2 = "b234test-key-2"
        delete_rows("request_guard", request_key=key2)
        try:
            idempotency.run_once(key2, "t.fail", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        except RuntimeError:
            pass
        left = db.query_one("SELECT count(*) AS n FROM request_guard WHERE request_key=%s", (key2,))
        check("★ 失败**不留**幂等记录（重试仍可执行）", left["n"] == 0, str(left["n"]))

        check("make_key 稳定（跨进程可复现，不是内置 hash）",
              idempotency.make_key("a", 1) == idempotency.make_key("a", 1))
    finally:
        db.execute("DELETE FROM request_guard WHERE request_key LIKE %s", ("b234test-%",))


# ============================================================ 工具层 wrapper

def test_http_client():
    print("\n--- §2.5：统一 HTTP wrapper（分类重试）---")
    http_client.reset_stats()
    try:
        http_client.request_json("GET", "http://127.0.0.1:9/nope", retries=0,
                                 backoff=0.01, label="b234t")
        check("不可达地址应抛 HttpFailure", False)
    except http_client.HttpFailure as e:
        check("网络错误 → 可重试（retryable=True）", e.retryable is True, str(e)[:60])
    check("失败计入 metrics", http_client.stats().get("fail.b234t") == 1,
          str(http_client.stats()))
    safe = http_client.request_json_safe("GET", "http://127.0.0.1:9/nope", retries=0,
                                         label="b234t")
    check("request_json_safe 不抛异常，返回 ok=False", safe.get("ok") is False, str(safe)[:80])


# ============================================================ G5

def test_g5_worker():
    print("\n--- G5：失败重投 worker ---")
    s = _session()
    try:
        _cleanup()
        fid = db.query_one(
            """INSERT INTO session_failure
                 (run_id, staff_id, session_id, stage, error_class, error_message,
                  attempts, status, next_retry_at)
               VALUES (NULL,%s,%s,'llm_judge','EmptyCompletionError','x',0,'pending',now())
               RETURNING failure_id""", (s["staff_id"], s["session_id"]))["failure_id"]
        check("到期待重投的能被捞出", any(r["failure_id"] == fid for r in fw.due()),
              str([r["failure_id"] for r in fw.due()]))

        # 打桩 score_session：模拟重投仍失败 → attempts+1，退避
        orig = ss.service.score_session
        ss.service.score_session = lambda *a, **kw: {"ok": False, "reason": "stub 仍失败"}
        try:
            res = fw.drain(limit=50, verbose=False)
        finally:
            ss.service.score_session = orig
        row = db.query_one("SELECT attempts, status, next_retry_at FROM session_failure "
                           "WHERE failure_id=%s", (fid,))
        check("重投失败 → attempts 递增 + 退避（仍 pending）",
              row["attempts"] == 1 and row["status"] == "pending" and row["next_retry_at"],
              str(dict(row)))

        # 打桩成功 → resolved
        db.execute("UPDATE session_failure SET next_retry_at=now() WHERE failure_id=%s", (fid,))
        ss.service.score_session = lambda *a, **kw: {"ok": True, "assessment": {}}
        try:
            res2 = fw.drain(limit=50, verbose=False)
        finally:
            ss.service.score_session = orig
        row2 = db.query_one("SELECT status, resolution, resolved_at FROM session_failure "
                            "WHERE failure_id=%s", (fid,))
        check("★ 重投成功 → resolved + 记录来源 auto",
              row2["status"] == "resolved" and row2["resolution"] == "auto", str(dict(row2)))
        check("drain 返回统计", res2["resolved"] >= 1, str(res2))

        # 达阈值 → dead + 转人工
        fid2 = db.query_one(
            """INSERT INTO session_failure
                 (staff_id, session_id, stage, error_class, error_message, attempts,
                  status, next_retry_at)
               VALUES (%s,%s,'llm_judge','X','y',99,'pending',now())
               RETURNING failure_id""", (s["staff_id"], s["session_id"]))["failure_id"]
        ss.service.score_session = lambda *a, **kw: {"ok": False, "reason": "stub"}
        try:
            fw.drain(limit=50, verbose=False)
        finally:
            ss.service.score_session = orig
        row3 = db.query_one("SELECT status, manual_required FROM session_failure "
                            "WHERE failure_id=%s", (fid2,))
        check("★ 超阈值 → dead 且 manual_required=true（转人工）",
              row3["status"] == "dead" and row3["manual_required"] is True, str(dict(row3)))
        m = fw.resolve_manually(fid2, note="人工确认无需处理", actor="b234test")
        check("人工可标记已处理", m["ok"] is True, str(m))
    finally:
        _cleanup()


# ============================================================ 闸6

def test_gate6():
    print("\n--- 闸6：门禁硬拦截（「无法判定」≠「通过」）---")
    import eval_runner as er
    d = er.gate_decision()
    check("无基线时 blocked=False（不阻断）", d["blocked"] is False)
    check("★ 无基线时 decidable=False（**明确不是「通过」**）", d["decidable"] is False, str(d))
    check("reason 如实说明「无法判定」", "无法判定" in d["reason"], d["reason"][:60])
    # 假基线 + 假指标 → 指标下降应 blocked
    import tempfile, json
    tmp = tempfile.mkdtemp(prefix="b234_")
    p = os.path.join(tmp, "baseline.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"metrics": {"rule_f1": 0.99, "grade_acc": 0.99, "fp_case_rate": 0.0}}, fh)
    baseline = {"rule_f1": 0.99, "grade_acc": 0.99, "fp_case_rate": 0.0}
    now_bad = {"rule_f1": 0.5, "grade_acc": 0.5, "fp_case_rate": 0.5}
    passed, diffs = er.gate(now_bad, baseline)
    check("★ 指标下滑 → gate 判定不通过", passed is False, str(diffs))
    now_ok = {"rule_f1": 0.99, "grade_acc": 1.0, "fp_case_rate": 0.0}
    passed2, _ = er.gate(now_ok, baseline)
    check("指标持平或变好 → 通过", passed2 is True)
    check("端点已注册（rules/gate + rules/publish）",
          hasattr(api_scoring, "rules_gate") and hasattr(api_scoring, "rules_publish"))


# ============================================================ E4

def test_e4_verification():
    print("\n--- E4：verification_status 写入（人工验证状态）---")
    import api_review
    s = _session()
    rid = None
    try:
        _cleanup()
        aid = ss.service._persist_assessment(
            0, dict(s, target_id="B234TEST_E4", session_id=_SESS), _res(), 
            {"raw_score": 0, "positive_total": 0, "negative_total": -2},
            {"grade": "D", "priority": "", "advice": ""}, True, [], [],
            [], "off")
        rid = db.query_one(
            """INSERT INTO negative_review
                 (relationship_key, assessment_id, staff_id, session_id,
                  ai_negative_items, ai_negative_total, status)
               VALUES (%s,%s,%s,%s,%s,-2,'pending') RETURNING id""",
            (_REL, aid, s["staff_id"], _SESS,
             db.as_json([{"rule_id": "negative_refuse", "score": -2,
                          "polarity": "negative", "reason": "ai"}])))["id"]
        db.execute("UPDATE customer_intent_assessment SET verification_status='na' "
                   "WHERE assessment_id=%s", (aid,))
        # 人工改判：-2（AI）→ -3（人工认为更重）→ corrected
        # ⚠️ 分值必须是规则表里**真实存在的档位**（negative_refuse 只有 -3）；
        #    给了不存在的分值会被 validate_items 当无效项丢弃 → total=0 → 判成 'rejected'（踩过）
        out = api_review.submit({"review_id": rid, "reviewer": "b234test",
                                 "items": [{"rule_id": "negative_refuse", "score": -3,
                                            "reason": "人工认为更严重"}], "note": "n"})
        row = db.query_one("SELECT verification_status FROM customer_intent_assessment "
                           "WHERE assessment_id=%s", (aid,))
        check("★ 人工改判 → verification_status='corrected'",
              out.get("verification_status") == "corrected"
              and row["verification_status"] == "corrected", str(row))
        check("★ 提交时自动回流评估集（E5 联动）",
              out.get("backflow", {}).get("created") == 1, str(out.get("backflow"))[:100])
        check("回流样本带人工金标准（非空）",
              bool(db.query_one("SELECT expected_items FROM eval_case WHERE session_id=%s",
                                (_SESS,))["expected_items"]))
    finally:
        _cleanup()


# ============================================================ F2

def test_f2():
    print("\n--- F2：早会 / 公开课复核入口 ---")
    check("sa_review 端点存在", hasattr(api_scoring, "sa_review"))
    check("public_class_review 端点存在", hasattr(api_scoring, "public_class_review"))
    out = api_scoring.sa_review("S,A")
    check("★ 只返回 S/A 等级的当前有效评估",
          all(i["grade"] in ("S", "A") for i in out["items"]), str(out["scope"]))
    check("返回的是「当前有效」(is_current) 的那些",
          out["count"] == len(out["items"]))
    bad = api_scoring.public_class_review({"mode": "nonsense"})
    check("非法 mode 被拒", bad["ok"] is False, str(bad))
    # 只校验参数分支，不真的启动评分（避免触发真实 LLM）
    check("schedule 支持 morning 排程函数",
          hasattr(__import__("scheduler"), "_reschedule_morning"))


# ============================================================ E5

def test_e5_meta_loop():
    print("\n--- E5：meta-loop（误报率排行 + 回改判回流）---")
    r = ml.rule_fp_ranking()
    check("排行榜结构完整",
          {"rules", "reviews_with_human", "threshold"} <= set(r), str(sorted(r)))
    # 造一条「AI 报了、人工否掉」的复核 → 应进入误报统计并回流
    s = _session()
    rid = None
    try:
        _cleanup()
        aid = ss.service._persist_assessment(
            0, dict(s, target_id="B234TEST_E5"), _res(),
            {"raw_score": -2, "positive_total": 0, "negative_total": -2},
            {"grade": "D", "priority": "", "advice": ""}, True, [], [], [], "off")
        db.execute("UPDATE customer_intent_assessment SET verification_status='rejected' "
                   "WHERE assessment_id=%s", (aid,))
        # 先取基线：`rule_fp_ranking()` 是按 rule_id 对**全库**已完成复核做聚合，
        # 库里本来就有真实复核记录 → 只能断言「增量」，见下方断言处的说明。
        _before = [x for x in ml.rule_fp_ranking()["rules"]
                   if x["rule_id"] == "negative_low_cooperation"]
        b_fp = _before[0]["false_positives"] if _before else 0
        b_ai = _before[0]["ai_hits"] if _before else 0
        rid = db.query_one(
            """INSERT INTO negative_review
                 (relationship_key, assessment_id, staff_id, session_id,
                  ai_negative_items, ai_negative_total, human_negative_items,
                  human_negative_total, status)
               VALUES (%s,%s,%s,%s,%s,-2,'[]',0,'done') RETURNING id""",
            (_REL, aid, s["staff_id"], _SESS,
             db.as_json([{"rule_id": "negative_low_cooperation", "score": -2,
                          "polarity": "negative", "reason": "ai"}])))["id"]
        rank = ml.rule_fp_ranking()
        hit = [x for x in rank["rules"] if x["rule_id"] == "negative_low_cooperation"]
        check("★ AI 命中但人工否掉 → 计入误报（false_positives）",
              hit and hit[0]["false_positives"] >= 1, str(hit[:1]))
        check("误报率计算正确（fp/ai_hits）",
              # ⚠️ 不能断言 `fp_rate == 1.0`：`rule_fp_ranking()` 是按 rule_id
              # **对全库所有已完成复核**做聚合，库里本来就有真实复核记录
              # （实测 17 条 done，其中 4 条命中本规则）→ 比率必然是 0.x。
              # 正确的测法是**测增量**：基线 + 本用例插入的这 1 条。
              hit and hit[0]["fp_rate"] == round((b_fp + 1) / (b_ai + 1), 4),
              f"before(fp={b_fp},ai={b_ai}) after={hit[:1]}")
        bf = ml.backflow_reviews(kind="regression")
        check("★ 改判过的复核被回流成 eval_case", bf["created"] >= 1, str(bf["created"]))
        case = db.query_one("SELECT case_id, source, expected_items FROM eval_case "
                            "WHERE source='review' AND session_id=%s", (_SESS,))
        # ⚠️ 用 `is not None`：`[]` 是**有意义的结论**（人工判定一条都不该扣），
        #    用真值判断会把它当成「没标注」（踩过）
        check("回流样本带人工金标准（expected_items 非 None）",
              case is not None and case["expected_items"] is not None, str(case)[:100])
        linked = db.query_one("SELECT eval_case_id FROM negative_review WHERE id=%s", (rid,))
        check("复核记录被回填 eval_case_id（可追溯）",
              linked["eval_case_id"] == case["case_id"], str(linked))
    finally:
        _cleanup()


# ============================================================ D2-1

def test_d21_delta():
    print("\n--- D2-1：差值累加回写 ---")
    # 纯判定
    cases = [(None, 5, True, 5), (0, 5, True, 5), (3, 5, True, 2),
             (5, 5, False, 0), (7, 5, False, -2)]
    for wt, pt, allow, delta in cases:
        r = hooks.before_writeback(positive_total=pt, mode="live", written_total=wt)
        check(f"水位={wt} 本次={pt} → allow={allow} delta={delta}",
              r.allow is allow and (r.detail or {}).get("delta") == delta,
              f"{r.allow} {r.detail}")
    check("差值为 0 → reason=no_delta（不调接口）",
          hooks.before_writeback(positive_total=5, mode="live",
                                 written_total=5).reason == "no_delta")
    check("★ 关闭差值模式 → 退回整值（回滚开关）",
          hooks.before_writeback(positive_total=5, mode="live", written_total=5,
                                 delta_mode=False).detail["delta"] == 5)
    check("dry_run 也报 delta（便于预演）",
          hooks.before_writeback(positive_total=8, mode="dry_run",
                                 written_total=3).detail["delta"] == 5)
    check("水位表已建", bool(db.query_one(
        "SELECT 1 AS x FROM information_schema.tables WHERE table_name='writeback_ledger'")))
    # 水位推进（幂等 upsert）
    try:
        _cleanup()
        ss._advance_ledger(_REL, "s", "t", 1, 3)
        ss._advance_ledger(_REL, "s", "t", 2, 2)
        row = db.query_one("SELECT written_total, writes FROM writeback_ledger "
                           "WHERE relationship_key=%s", (_REL,))
        check("★ 水位累加正确（3 + 2 = 5）且 writes 计数",
              row["written_total"] == 5 and row["writes"] == 2, str(dict(row)))
        check("读水位一致", ss._ledger_total(_REL) == 5)
    finally:
        _cleanup()


# ============================================================

def main():
    print("=" * 72)
    print("第 2/3/4 批「架构全量落实」验收")
    print("=" * 72)
    global _AUDIT_MAX
    _AUDIT_MAX = (db.query_one("SELECT coalesce(max(id),0) AS n FROM audit_log") or {}).get("n", 0)
    try:
        test_e2_is_current()
        test_e2_selfheal()
        test_b4_effective()
        test_e3_profile()
        test_examples()
        test_retention()
        test_idempotency()
        test_http_client()
        test_g5_worker()
        test_gate6()
        test_e4_verification()
        test_f2()
        test_e5_meta_loop()
        test_d21_delta()
    finally:
        _cleanup()
    print("\n" + "=" * 72)
    print("PASS %d / FAIL %d" % (len(PASSED), len(FAILED)))
    if FAILED:
        for f in FAILED:
            print("  FAILED:", f)
    print("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
