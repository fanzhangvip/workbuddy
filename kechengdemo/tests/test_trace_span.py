# -*- coding: utf-8 -*-
"""本地 trace_span 追踪 —— 需要 PostgreSQL(5433)。

    .venv/Scripts/python.exe tests/test_trace_span.py

验四件事：
  1. 默认关闭时**零额外写入**
  2. 开启后逐节点落 span（成功路径每个图节点 1 条），耗时/指纹/ok 齐备
  3. 节点抛异常时落 ``ok=false`` + ``error_class``，且异常继续穿透
  4. **写 trace 失败（表不可写）不得影响评分主流程**
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"


import re
import sys

from _harness import FAILED, PASSED, check, delete_rows, purge_run  # noqa: E402

import analyze                # noqa: E402
import db                     # noqa: E402
import graph_session          # noqa: E402
import llm                    # noqa: E402
import rules as rules_mod     # noqa: E402
import scoring_service as ss  # noqa: E402

RUN_ID = 994000001
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_ORIG = None




def pick_candidate():
    return db.query_one(
        """SELECT c.staff_id, s.name AS staff_name, c.session_id, c.target_id,
                  c.name AS target_name, c.msg_count, c.last_msg_time
             FROM chat_session c LEFT JOIN staff s ON s.staff_id = c.staff_id
            WHERE c.session_type='ext_user' AND c.target_id IS NOT NULL AND c.msg_count >= 8
            ORDER BY c.msg_count DESC, c.session_id LIMIT 1""")


def ok_stub(conversation, facts, rules, **kw):
    return {"positive_items": [], "negative_items": [], "invalid_items": [],
            "unchecked_rules": [], "profile_summary": "secret-profile-文本",
            "sales_strategy": "secret-strategy", "model_confidence": 0.5,
            "rule_version": rules.get("rule_version"), "prompt_version": "t",
            "schema_version": "1", "contract_attempts": 1}


def spans():
    return db.query("SELECT * FROM trace_span WHERE run_id=%s ORDER BY span_id", (RUN_ID,))


def cleanup(sid):
    # 本用例额外的产物：链路追踪
    delete_rows("trace_span", run_id=RUN_ID)
    purge_run(RUN_ID)                       # 公共产物（见 _harness.purge_run）
    delete_rows("session_failure", session_id=sid)


def main():
    global _ORIG
    _ORIG = db.get_setting("trace_enabled")
    cand = pick_candidate()
    if not cand:
        print("跳过：库里没有可用的 ext_user 会话。")
        return 0
    rules = rules_mod.load_rules()
    orig_analyze = analyze.analyze
    try:
        cleanup(cand["session_id"])

        print("\n--- 1. 默认关闭：零额外写入 ---")
        db.execute("DELETE FROM app_setting WHERE key='trace_enabled'")
        analyze.analyze = ok_stub
        graph_session.run_session(ss.service, RUN_ID, cand, rules, 8, "off", False)
        check("关闭时未写任何 span", len(spans()) == 0, str(len(spans())))

        print("\n--- 2. 开启：逐节点落 span ---")
        db.set_setting("trace_enabled", "true")
        cleanup(cand["session_id"])          # 清掉步骤1的 assessment，避免撞唯一索引
        graph_session.run_session(ss.service, RUN_ID, cand, rules, 8, "off", False)
        rows = spans()
        nodes = {r["node"] for r in rows}
        # 期望节点数从编译后的图里取，而不是写死数字 —— 否则每加一个节点都要改测试
        expect_nodes = {n for n in graph_session.graph_for(ss.service).get_graph().nodes
                        if not n.startswith("__")}
        check(f"成功路径逐节点落 span（{len(expect_nodes)} 条）",
              len(rows) == len(expect_nodes), f"{len(rows)} vs {len(expect_nodes)}")
        check("覆盖全部主节点", expect_nodes <= nodes, str(sorted(nodes)))
        check("全部 ok=true", all(r["ok"] for r in rows))
        check("elapsed_ms 均已记录",
              all(r["elapsed_ms"] is not None for r in rows))
        check("input/output_digest 均为 16 位十六进制指纹",
              all(_HEX16.match(r["input_digest"] or "") and _HEX16.match(r["output_digest"] or "")
                  for r in rows))
        check("detail 为空（未塞任何自由文本）", all(r["detail"] is None for r in rows))
        blob = " ".join(str(r) for r in rows)
        check("绝不落原文：指纹表里不含摘要/策略文本",
              "secret-profile" not in blob and "secret-strategy" not in blob)

        print("\n--- 3. 异常节点落 ok=false + error_class ---")
        cleanup(cand["session_id"])
        analyze.analyze = lambda *a, **kw: (_ for _ in ()).throw(llm.FatalLLMError("401"))
        try:
            graph_session.run_session(ss.service, RUN_ID, cand, rules, 8, "off", False)
        except llm.FatalLLMError:
            pass
        rows = spans()
        bad = [r for r in rows if not r["ok"]]
        check("存在 ok=false 的 span", len(bad) == 1, str(len(bad)))
        check("ok=false 的 span 带 error_class",
              bool(bad) and bad[0]["error_class"] == "FatalLLMError",
              str(bad[0]["error_class"]) if bad else "无")
        check("异常发生在 judge → 其后节点未执行",
              {r["node"] for r in rows} == {"prepare_context", "emit_ctx_stats", "facts", "judge"},
              str(sorted({r["node"] for r in rows})))

        print("\n--- 4. trace 写库失败不影响评分主流程 ---")
        cleanup(cand["session_id"])
        analyze.analyze = ok_stub
        db.set_setting("trace_enabled", "true")
        db.execute("ALTER TABLE trace_span RENAME TO trace_span_bak")
        try:
            r = graph_session.run_session(ss.service, RUN_ID, cand, rules, 8, "off", False)
            check("表不可写时评分照常完成", r == "scored", r)
            check("assessment 正常落库",
                  db.query_one("SELECT count(*) AS n FROM customer_intent_assessment "
                               "WHERE source_run_id=%s", (RUN_ID,))["n"] == 1)
        finally:
            db.execute("ALTER TABLE trace_span_bak RENAME TO trace_span")
        check("表恢复后 trace 表结构完好",
              db.query_one("SELECT count(*) AS n FROM trace_span WHERE 1=0") is not None)
    finally:
        analyze.analyze = orig_analyze
        if _ORIG is None:
            db.execute("DELETE FROM app_setting WHERE key='trace_enabled'")
        else:
            db.set_setting("trace_enabled", _ORIG)
        cleanup(cand["session_id"])
        print("\n（已清理）")

    print(f"\n===== {len(PASSED)} passed / {len(FAILED)} failed =====")
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
