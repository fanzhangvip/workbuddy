# -*- coding: utf-8 -*-
"""图化后的**返回语义与异常语义**必须与旧 `_score_one` 完全一致 —— 需要 PostgreSQL(5433)。

    .venv/Scripts/python.exe tests/test_graph_skip_and_failure.py

验四件事：
  1. 早退：``kept < ctx_min_kept`` → 返回 ``"skipped"``，且**不调用** ``_bump_ctx``
  2. ``FatalLLMError`` 从 ``invoke()`` 原样穿透（交由 ``_run`` 记失败明细）
  3. ``ContractViolationError`` 同样穿透
  4. 可重试异常耗尽后抛 ``RuntimeError``（同样穿透）
并顺带验证成功路径会正确累加 ``assessment_new``。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"


import os
import sys

from _harness import FAILED, PASSED, ROOT, check, delete_rows, purge_run  # noqa: E402

import analyze                # noqa: E402
import contracts              # noqa: E402
import db                     # noqa: E402
import graph_session          # noqa: E402
import llm                    # noqa: E402
import rules as rules_mod     # noqa: E402
import scoring_service as ss  # noqa: E402

RUN_ID = 993000001




def pick_candidate():
    return db.query_one(
        """SELECT c.staff_id, s.name AS staff_name, c.session_id, c.target_id,
                  c.name AS target_name, c.msg_count, c.last_msg_time
             FROM chat_session c LEFT JOIN staff s ON s.staff_id = c.staff_id
            WHERE c.session_type='ext_user' AND c.target_id IS NOT NULL AND c.msg_count >= 8
            ORDER BY c.msg_count DESC, c.session_id LIMIT 1""")


def ok_stub(conversation, facts, rules, **kw):
    return {"positive_items": [], "negative_items": [], "invalid_items": [],
            "unchecked_rules": [], "profile_summary": "", "sales_strategy": "",
            "model_confidence": 0.5, "rule_version": rules.get("rule_version"),
            "prompt_version": "t", "schema_version": "1", "contract_attempts": 1}


def raise_stub(exc):
    def _s(conversation, facts, rules, **kw):
        raise exc
    return _s


def cleanup(sid):
    purge_run(RUN_ID)                       # 公共产物（见 _harness.purge_run）
    # 本用例额外的产物：按 session_id 的会话失败记录
    delete_rows("session_failure", session_id=sid)


def main():
    cand = pick_candidate()
    if not cand:
        print("跳过：库里没有可用的 ext_user 会话。")
        return 0
    rules = rules_mod.load_rules()
    orig_analyze, orig_min, orig_retry = analyze.analyze, None, None
    orig_progress = dict(ss.service._progress)
    try:
        db.set_setting("ctx_min_kept", "100000")          # 强制早退
        ss.service._progress = {"ctx_total": 0, "ctx_kept": 0}
        print("\n--- 1. 早退语义 ---")
        r = graph_session.run_session(ss.service, RUN_ID, cand, rules, 8, "off", False)
        check("返回 'skipped'（与旧路径逐字一致）", r == "skipped", r)
        check("早退时未调用 _bump_ctx（ctx_* 计数保持 0）",
              ss.service._progress.get("ctx_total", 0) == 0
              and ss.service._progress.get("ctx_kept", 0) == 0,
              str(ss.service._progress))
        check("早退时未落 assessment",
              db.query_one("SELECT count(*) AS n FROM customer_intent_assessment "
                           "WHERE source_run_id=%s", (RUN_ID,))["n"] == 0)
        db.execute("DELETE FROM app_setting WHERE key='ctx_min_kept'")

        print("\n--- 2. FatalLLMError 穿透 ---")
        analyze.analyze = raise_stub(llm.FatalLLMError("401 认证失败"))
        ss.service._progress = dict(orig_progress)
        try:
            graph_session.run_session(ss.service, RUN_ID, cand, rules, 8, "off", False)
            check("FatalLLMError 应穿透（未穿透）", False)
        except llm.FatalLLMError as e:
            check("FatalLLMError 原样穿透", True, str(e)[:40])
        except Exception as e:
            check("FatalLLMError 原样穿透（类型不符）", False, type(e).__name__)
        check("穿透时未落 assessment（不产生半截记录）",
              db.query_one("SELECT count(*) AS n FROM customer_intent_assessment "
                           "WHERE source_run_id=%s", (RUN_ID,))["n"] == 0)

        print("\n--- 3. ContractViolationError 穿透 ---")
        analyze.analyze = raise_stub(contracts.ContractViolationError("schema 不合规", raw="{", errors_text="x"))
        try:
            graph_session.run_session(ss.service, RUN_ID, cand, rules, 8, "off", False)
            check("ContractViolationError 应穿透（未穿透）", False)
        except contracts.ContractViolationError:
            check("ContractViolationError 原样穿透", True)
        except Exception as e:
            check("ContractViolationError 原样穿透（类型不符）", False, type(e).__name__)

        print("\n--- 4. 可重试异常耗尽 → RuntimeError 穿透 ---")
        db.set_setting("llm_retry_attempts", "1")
        analyze.analyze = raise_stub(llm.RetryableLLMError("503"))
        try:
            graph_session.run_session(ss.service, RUN_ID, cand, rules, 8, "off", False)
            check("重试耗尽应抛 RuntimeError（未抛）", False)
        except RuntimeError as e:
            check("重试耗尽抛 RuntimeError 并穿透", True, str(e)[:50])
        except Exception as e:
            check("重试耗尽抛 RuntimeError（类型不符）", False, type(e).__name__)
        db.execute("DELETE FROM app_setting WHERE key='llm_retry_attempts'")

        print("\n--- 5. 成功路径照常累加进度 ---")
        analyze.analyze = ok_stub
        ss.service._progress = {"assessment_new": 0}
        r = graph_session.run_session(ss.service, RUN_ID, cand, rules, 8, "off", False)
        check("返回 'scored'", r == "scored", r)
        check("assessment_new 累加为 1", ss.service._progress.get("assessment_new") == 1,
              str(ss.service._progress.get("assessment_new")))

        print("\n--- 6. 灰度开关 use_graph_session 分派正确 ---")
        cleanup(cand["session_id"])            # 清掉步骤5的 assessment，避免撞唯一索引
        seen = []
        orig_run = graph_session.run_session

        def spy(*a, **kw):
            seen.append(True)
            return "从图返回"

        graph_session.run_session = spy
        ss.service._progress = dict(orig_progress)
        try:
            db.set_setting("use_graph_session", "true")
            r_on = ss.service._score_cand(RUN_ID, cand, rules, 8, "off", False)
            db.set_setting("use_graph_session", "false")
            r_off = ss.service._score_cand(RUN_ID, cand, rules, 8, "off", False)
            check("开关=on → 走图路径", r_on == "从图返回" and len(seen) == 1, f"{r_on} seen={len(seen)}")
            check("开关=off → 走旧路径（未再调图）", r_off == "scored" and len(seen) == 1,
                  f"{r_off} seen={len(seen)}")
        finally:
            graph_session.run_session = orig_run
            db.execute("DELETE FROM app_setting WHERE key='use_graph_session'")
    finally:
        analyze.analyze = orig_analyze
        ss.service._progress = orig_progress
        db.execute("DELETE FROM app_setting WHERE key IN ('ctx_min_kept','llm_retry_attempts')")
        cleanup(cand["session_id"])
        print("\n（已清理）")

    print(f"\n===== {len(PASSED)} passed / {len(FAILED)} failed =====")
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
