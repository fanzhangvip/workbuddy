# -*- coding: utf-8 -*-
"""第 1 批「纯加性观测」验收 —— 架构 §2.2 H2 / §7.2 B1·A1·闸2·F6 / §2.7 H5（需 PostgreSQL 5433）。

    .venv/Scripts/python.exe tests/test_observability.py

覆盖：
  1. **H2** `set_settings` 的 key 级 audit（此前只有 `writeback_mode` 一个键留痕）
  2. **§7.3** `llm_timeout_sec` 可配（此前 `llm.TIMEOUT=120` 硬编码）
  3. **B1** `model_name` 落库（取响应里的 model，非配置值）
  4. **A1** 逐项 `evidence_verified`（1 全成功 / 2 部分 / 0 未校验）
  5. **闸2** `rule_checks` 逐维度表态落库
  6. **F6** `old_raw_score`（= 本次之前的最新分数）
  7. **H5** 单会话耗时分布（`session_secs` + p50/p95/max）

⚠️ 全部用**合成数据 / 打桩**，不真调 LLM；跑完清理，断言不依赖生产现状。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"


import sys
import time

from _harness import FAILED, PASSED, check, delete_rows  # noqa: E402

import analyze                   # noqa: E402
import api_scoring               # noqa: E402
import db                        # noqa: E402
import scoring_service as ss     # noqa: E402

_REL = "OBSTEST:subject"




def _cleanup():
    # ⚠️ 关系键是 `staff_id:target_id`，合成标识在**第二段** ——
    # 用 `LIKE 'OBSTEST%'`（前缀）根本匹配不到，于是第二轮跑会看到上一轮的记录
    # （踩过：`old_raw_score` 断言莫名失败）。必须按**后缀/包含**匹配。
    like = "%OBSTEST_SUBJ"
    db.execute("DELETE FROM customer_intent_assessment WHERE relationship_key LIKE %s", (like,))
    # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
    try: ss.repair_current_flags()
    except Exception: pass
    db.execute("DELETE FROM score_writeback WHERE relationship_key LIKE %s", (like,))
    db.execute("DELETE FROM negative_review WHERE relationship_key LIKE %s", (like,))
    db.execute("DELETE FROM audit_log WHERE action='set_setting'")
    db.execute("DELETE FROM audit_log WHERE actor='obs-test'")
    delete_rows("app_setting", key="llm_timeout_sec")
    delete_rows("app_setting", key="scoring_max_sessions")
    # ⚠️ `writeback_mode` 分支会写 app_setting（即使值没变也写）——
    # 测完必须清掉，否则会在生产配置里留下一个本不存在的键（踩到过）。
    delete_rows("app_setting", key="score_writeback_mode")


def _pick_session(min_msgs=4):
    return db.query_one(
        """SELECT c.staff_id, c.session_id, c.target_id, c.name AS target_name,
                  c.msg_count, c.last_msg_time, s.name AS staff_name
             FROM chat_session c LEFT JOIN staff s ON s.staff_id=c.staff_id
            WHERE c.session_type='ext_user' AND c.target_id IS NOT NULL
              AND c.msg_count >= %s ORDER BY c.msg_count DESC, c.session_id LIMIT 1""", (min_msgs,))


# ============================================================ 1. H2 审计

def test_audit_settings():
    print("\n--- 1. H2：set_settings 的 key 级 audit（含旧值→新值）---")
    _cleanup()
    # ⚠️ 必须是 **id 水位**，不是行数！拿行数当 id 阈值会删掉全部真实审计
    # （实测踩到：id 已涨到 253 而行数只有 2 → `id > 2` 把 run#151 的 scoring_start/done 全删了）
    before = db.query_one("SELECT coalesce(max(id), 0) AS n FROM audit_log")["n"]
    try:
        # 直调路由函数（FastAPI 的 handler 本身就是普通函数）
        out = api_scoring.set_settings({
            "obstest_a": "1", "obstest_b": "2",
            "max_sessions": 123,            # 走白名单分支之一
            "llm_timeout_sec": 77,
        })
        # 白名单外的键不会被接受（obstest_* 不在白名单）→ 用白名单键再验一次
        rows = db.query("SELECT target, detail FROM audit_log WHERE action='set_setting' "
                        "AND target IN ('scoring_max_sessions','llm_timeout_sec') ORDER BY id")
        check("白名单键变更各写一条 audit", len(rows) == 2, str(len(rows)))
        check("audit 含 key / old / new 三要素",
              all({"key", "old", "new"} <= set(r["detail"]) for r in rows), str(rows[:1]))
        check("old 字段存在（首次设置时为 null = 原本没有这个键）",
              all("old" in r["detail"] for r in rows), str(rows[:1]))
        # 第二次改同一个键 → old 必须记录**上一次的值**
        api_scoring.set_settings({"max_sessions": 456})
        r2 = db.query_one("SELECT detail FROM audit_log WHERE action='set_setting' "
                          "AND target='scoring_max_sessions' ORDER BY id DESC LIMIT 1")
        check("★ 二次修改时 old 记录了上一次的值（123）",
              str(r2["detail"]["old"]) == "123" and str(r2["detail"]["new"]) == "456",
              str(r2["detail"]))
        check("detail.new 与写入值一致",
              any(str(r["detail"]["new"]) == "123" for r in rows), str([r["detail"] for r in rows]))
        check("返回体带 changed 列表",
              isinstance(out.get("changed"), list) and "scoring_max_sessions" in out["changed"],
              str(out.get("changed")))

        # writeback_mode 仍走自己的分支且留痕（action 名保持不变）
        api_scoring.set_settings({"writeback_mode": "dry_run"})
        wb = db.query_one("SELECT count(*) AS n FROM audit_log WHERE action='writeback_mode'")["n"]
        check("writeback_mode 仍留痕且 action 名不变（保持原行为）", wb >= 1, str(wb))

        # 幂等：同值再写不留痕（避免"点一次保存刷一堆无用记录"）
        # ⚠️ 必须用**当前值**再写一次 —— 用一个不同的值会真的产生变更（测试自己踩过）
        cur = api_scoring.get_settings()["max_sessions"]
        n1 = db.query_one("SELECT count(*) AS n FROM audit_log WHERE action='set_setting'")["n"]
        api_scoring.set_settings({"max_sessions": cur})
        n2 = db.query_one("SELECT count(*) AS n FROM audit_log WHERE action='set_setting'")["n"]
        check("值未变 → 不重复留痕", n2 == n1, f"{n1} → {n2}")
    finally:
        db.execute("DELETE FROM app_setting WHERE key IN ('scoring_max_sessions','llm_timeout_sec')")
        db.execute("DELETE FROM audit_log WHERE action='set_setting'")
        db.execute("DELETE FROM audit_log WHERE id > %s", (before,))


# ============================================================ 2. 超时可配

def test_llm_timeout_config():
    print("\n--- 2. §7.3：llm_timeout_sec 可配并透传到调用 ---")
    try:
        check("get_settings 暴露 llm_timeout_sec", "llm_timeout_sec" in api_scoring.get_settings())
        db.set_setting("llm_timeout_sec", "45")
        check("写入后可读回", db.setting_int("llm_timeout_sec", "LLM_TIMEOUT_SEC", 0) == 45)
        db.execute("DELETE FROM app_setting WHERE key='llm_timeout_sec'")
        check("默认 0（回退 llm.TIMEOUT）",
              db.setting_int("llm_timeout_sec", "LLM_TIMEOUT_SEC", 0) == 0)

        # 透传验证：打桩 analyze.analyze 捕获 kwargs
        captured = {}
        orig = analyze.analyze
        analyze.analyze = lambda *a, **kw: (captured.update(kw), {"positive_items": [],
                                                                  "negative_items": [],
                                                                  "invalid_items": [],
                                                                  "unchecked_rules": []})[1]
        try:
            db.set_setting("llm_timeout_sec", "33")
            ss.service._llm_analyze({"text": "x", "index": {}, "labels": {}}, "facts", {"rules": []})
            check("★ timeout 已透传到 analyze（= 配置值 33）",
                  captured.get("timeout") == 33, str(captured.get("timeout")))
            db.execute("DELETE FROM app_setting WHERE key='llm_timeout_sec'")
            ss.service._llm_analyze({"text": "x", "index": {}, "labels": {}}, "facts", {"rules": []})
            check("未配置 → timeout=None（用模块默认）",
                  captured.get("timeout") is None, str(captured.get("timeout")))
        finally:
            analyze.analyze = orig
    finally:
        db.execute("DELETE FROM app_setting WHERE key='llm_timeout_sec'")


# ============================================================ 3. A1 逐项回链

def test_evidence_verified():
    print("\n--- 3. A1：逐项 evidence_verified（1 全成功 / 2 部分 / 0 未校验）---")
    index = {"#001": "m1", "#002": "m2"}
    labels = {"#001": "客户: 甲", "#002": "客户: 乙"}

    kept, dropped = analyze._attach_evidence(
        [{"rule_id": "r1", "score": 2, "evidence_ids": ["#001", "#002"]}], index, labels)
    check("全部编号回链成功 → evidence_verified=1",
          kept and kept[0]["evidence_verified"] == 1, str(kept))

    kept, dropped = analyze._attach_evidence(
        [{"rule_id": "r2", "score": 2, "evidence_ids": ["#001", "#999"]}], index, labels)
    check("部分回链不成功 → evidence_verified=2",
          kept and kept[0]["evidence_verified"] == 2, str(kept))

    kept, dropped = analyze._attach_evidence(
        [{"rule_id": "r3", "score": 2, "evidence_ids": ["#999"]}], index, labels)
    check("全部回链失败 → 被丢弃且留痕 evidence_verified=0",
          not kept and dropped and dropped[0]["evidence_verified"] == 0, str(dropped))

    kept, dropped = analyze._attach_evidence(
        [{"rule_id": "r4", "score": 2, "evidence_ids": ["#001"]}], None, None)
    check("无 index（老调用方）→ evidence_verified=0 但保留",
          kept and kept[0]["evidence_verified"] == 0, str(kept))


# ============================================================ 4/5/6. 落库三列

def test_persist_new_columns():
    print("\n--- 4/5/6. B1 model_name + 闸2 rule_checks + F6 old_raw_score 落库 ---")
    sess = _pick_session()
    if not sess:
        check("存在可用会话", False, "库里没有会话")
        return
    cand = dict(sess)
    cand["target_id"] = "OBSTEST_SUBJ"       # 用合成 target 隔离，避免碰生产客户
    res = {
        "positive_items": [{"rule_id": "positive_coder_profile", "score": 2,
                            "dimension": "画像", "polarity": "positive", "reason": "t",
                            "evidence": ["客户: x"], "evidence_ids": ["#001"],
                            "evidence_message_ids": ["m1"], "evidence_verified": 1}],
        "negative_items": [], "invalid_items": [], "unchecked_rules": [],
        "profile_summary": "", "sales_strategy": "", "model_confidence": 0.5,
        "rule_version": "v1.0", "prompt_version": "t", "schema_version": "1",
        "contract_attempts": 1,
        "model_name": "deepseek-chat",                       # B1
        "rule_checks": [{"rule_id": "positive_coder_profile", "hit": True, "score": 2}],
    }
    score = {"raw_score": 2, "positive_total": 2, "negative_total": 0}
    grade = {"grade": "C", "priority": "", "advice": ""}
    aid1 = aid2 = None
    try:
        _cleanup()
        aid1 = ss.service._persist_assessment(0, cand, res, score, grade, False, [], 
                                              res["positive_items"], [], "off")
        row = db.query_one("""SELECT model_name, rule_checks, old_raw_score, positive_items
                                FROM customer_intent_assessment WHERE assessment_id=%s""", (aid1,))
        check("★ B1 model_name 落库", row["model_name"] == "deepseek-chat", str(row["model_name"]))
        check("★ 闸2 rule_checks 落库为 JSONB 数组",
              isinstance(row["rule_checks"], list) and row["rule_checks"][0]["rule_id"]
              == "positive_coder_profile", str(row["rule_checks"]))
        check("★ A1 逐项 evidence_verified 随 items 落库",
              all("evidence_verified" in i for i in row["positive_items"]),
              str(row["positive_items"])[:140])
        check("F6 首次评估 old_raw_score 为空（无上一条）",
              row["old_raw_score"] is None, str(row["old_raw_score"]))

        # 再评一次同一客户 → old_raw_score 应为上一次的 raw_score(2)
        res2 = dict(res)
        score2 = {"raw_score": 6, "positive_total": 6, "negative_total": 0}
        grade2 = {"grade": "S", "priority": "", "advice": ""}
        aid2 = ss.service._persist_assessment(0, cand, res2, score2, grade2, False, [],
                                              res2["positive_items"], [], "off")
        row2 = db.query_one("SELECT raw_score, old_raw_score FROM customer_intent_assessment "
                            "WHERE assessment_id=%s", (aid2,))
        check("★ F6 old_raw_score = 上一次的分数（6 ← 上一次 2）",
              row2["raw_score"] == 6 and row2["old_raw_score"] == 2, str(dict(row2)))
    finally:
        _cleanup()


# ============================================================ 7. H5 会话耗时

def test_session_secs():
    print("\n--- 7. H5：单会话耗时分布落 scoring_run.detail ---")
    run_id = None
    calls = {"n": 0}
    try:
        run_id = db.query_one(
            """INSERT INTO scoring_run (kind, status, triggered_by, rule_version, detail,
                                        max_llm_calls)
               VALUES ('full','running','obs-test',%s,'{}',1000) RETURNING id""",
            (ss.current_rule_version(),))["id"]

        def fake(_rid, cand, *_a, **_kw):
            calls["n"] += 1
            time.sleep(0.01)                     # 制造可观测耗时
            return "scored"

        orig = ss.service._score_cand
        ss.service._score_cand = fake
        try:
            ss.service._stop = False
            ss.service._partial = None
            ss.service._progress = {"llm_calls": 0, "session_done": 0, "session_failed": 0,
                                    "session_secs": []}
            ss.service._run(run_id, None, 4, 4, False, ss.current_rule_version())
        finally:
            ss.service._score_cand = orig

        d = (db.query_one("SELECT detail FROM scoring_run WHERE id=%s", (run_id,)) or {}).get("detail") or {}
        check("detail 含 session_secs 列表",
              isinstance(d.get("session_secs"), list) and len(d["session_secs"]) == calls["n"],
              f'{len(d.get("session_secs") or [])} vs {calls["n"]}')
        check("★ detail 含 p50 / p95 / max",
              {"session_secs_p50", "session_secs_p95", "session_secs_max"} <= set(d), str(sorted(d))[:160])
        check("耗时 > 0 且 p50 ≤ p95 ≤ max",
              d.get("session_secs_p50", 0) > 0
              and d["session_secs_p50"] <= d["session_secs_p95"] <= d["session_secs_max"],
              f'{d.get("session_secs_p50")} / {d.get("session_secs_p95")} / {d.get("session_secs_max")}')
    finally:
        if run_id:
            delete_rows("scoring_run", id=run_id)
            delete_rows("audit_log", target=f"run:{run_id}")


# ============================================================ 8. 观测汇总仍在

def test_observability_regression():
    print("\n--- 8. 观测汇总：hook 指标与耗时共存于 detail ---")
    run_id = None
    try:
        run_id = db.query_one(
            """INSERT INTO scoring_run (kind, status, triggered_by, rule_version, detail,
                                        max_llm_calls)
               VALUES ('full','running','obs-test',%s,'{}',1000) RETURNING id""",
            (ss.current_rule_version(),))["id"]
        orig = ss.service._score_cand
        ss.service._score_cand = lambda *a, **kw: "scored"
        try:
            ss.service._stop = False
            ss.service._partial = None
            ss.service._progress = {"llm_calls": 0, "session_done": 0, "session_failed": 0,
                                    "session_secs": [], "hook_denials": {}, "tool_failures": 0}
            ss.service._run(run_id, None, 2, 4, False, ss.current_rule_version())
        finally:
            ss.service._score_cand = orig
        d = (db.query_one("SELECT detail FROM scoring_run WHERE id=%s", (run_id,)) or {}).get("detail") or {}
        # ⚠️ 不断言 ctx_*：`_score_cand` 被打桩，`build_context` 从未执行，
        # 而 ctx_* 由 `_bump_ctx` 在真实评分链路上写入 —— 那是别的测试的覆盖面。
        check("detail 同时含 hook_denials / session_secs / tool_failures 观测",
              {"hook_denials", "session_secs", "tool_failures"} <= set(d),
              str(sorted(k for k in d if k.startswith(("hook", "session", "tool")))))
    finally:
        if run_id:
            delete_rows("scoring_run", id=run_id)
            delete_rows("audit_log", target=f"run:{run_id}")


# ============================================================

def main():
    print("=" * 70)
    print("第 1 批「纯加性观测」验收（H2 / B1 / A1 / 闸2 / F6 / H5）")
    print("=" * 70)
    try:
        test_audit_settings()
        test_llm_timeout_config()
        test_evidence_verified()
        test_persist_new_columns()
        test_session_secs()
        test_observability_regression()
    finally:
        _cleanup()
    print("\n" + "=" * 70)
    print("PASS %d / FAIL %d" % (len(PASSED), len(FAILED)))
    if FAILED:
        for f in FAILED:
            print("  FAILED:", f)
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
