# -*- coding: utf-8 -*-
"""批量主循环 `_run` × 图路径 端到端 —— 需要 PostgreSQL(5433)。

    .venv/Scripts/python.exe tests/test_run_with_graph.py

为什么需要这个测试
    ``_run`` 也是本阶段被改动的文件（循环体改调 ``_score_cand`` 分派），
    但图化/一致性的单测都只覆盖单会话。这里验证**批量外壳**没被改坏：
      · 开关关：走旧路径，计数正常
      · 开关开：走图路径，``scoring_run`` 各计数与旧路径**口径一致**
      · ``session_done / session_skipped / assessment_new`` 三项逐一相等

用确定性 stub 顶掉模型调用（无网络）；``mode="off"`` 不外写微伴；结束清理。
"""
# 统一输出编码（修 P2-2）：默认 GBK 控制台下，断言名里的 ↔ → 等字符会触发
# UnicodeEncodeError —— 看起来像测试失败，其实是环境问题。
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "backend"))

import analyze                # noqa: E402
import db                     # noqa: E402
import rules as rules_mod     # noqa: E402
import scoring_service as ss  # noqa: E402

N = 4
_MODE_KEY = "score_writeback_mode"
_ORIG_MODE = None
PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if detail else ""))


def ok_stub(conversation, facts, rules, **kw):
    """固定 1 条正分，保证两条路径都在“有产出”的分支上跑。"""
    return {"positive_items": [{"rule_id": "positive_cooperative",
                                "dimension": "配合度、认可度高", "polarity": "positive",
                                "score": 1, "reason": "stub", "evidence": [],
                                "evidence_ids": ["#001"], "evidence_message_ids": ["m"]}],
            "negative_items": [], "invalid_items": [], "unchecked_rules": [],
            "profile_summary": "stub", "sales_strategy": "stub", "model_confidence": 0.5,
            "rule_version": rules.get("rule_version"), "prompt_version": "t",
            "schema_version": "1", "contract_attempts": 1}


def run_batch(use_graph, tag):
    ss.service._progress = {}
    ss.service._stop = False
    db.set_setting("use_graph_session", "true" if use_graph else "false")
    res = ss.service.start(max_sessions=N, min_messages=8, triggered_by=f"test-{tag}")
    if not res.get("ok"):
        return None, res
    run_id = res.get("run_id") or res.get("id")
    for _ in range(120):                       # 等后台线程结束（stub 很快）
        if not ss.service.is_running():
            break
        time.sleep(0.5)
    return db.query_one("SELECT * FROM scoring_run WHERE id=%s", (run_id,)), res


def _purge_run(run_id):
    """清掉某个 run 的全部产物（评估 / 回写 / 审计 / run 行本身）。"""
    for tbl in ("writeback_outbox", "negative_review", "score_writeback"):
        db.execute(f"""DELETE FROM {tbl} WHERE assessment_id IN
                       (SELECT assessment_id FROM customer_intent_assessment
                         WHERE source_run_id=%s)""", (run_id,))
    db.execute("DELETE FROM customer_intent_assessment WHERE source_run_id=%s", (run_id,))
    # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
    try:
        ss.repair_current_flags()
    except Exception:
        pass
    db.execute("DELETE FROM scoring_run WHERE id=%s", (run_id,))
    # ⚠️ Loop 护栏（H2）之后 `_run` 会写 `scoring_start` / `scoring_done` 审计 ——
    # 这是**新增的写副作用**，跑该链路的测试必须同步清理（测试卫生铁律第 1 条）。
    db.execute("DELETE FROM audit_log WHERE target=%s", (f"run:{run_id}",))


def cleanup(run_ids):
    for run in run_ids:
        if not run:
            continue
        _purge_run(run)


def cleanup_stale_test_runs():
    """兜底：清掉**历史遗留**的测试 run（防「清理没跑到」留下永久残留）。

    ⚠️ 为什么必须有这一步：`cleanup()` 依赖本轮拿到的 `run_id`；若测试中途异常/被 kill，
    `run_id` 可能拿不到，残留就**永远清不掉**。实测后果：一次历史 run 留下 **4 条
    `is_current=true` 的评估** → 4 个真实客户被当成「已评分且 D 级」，从此不再进入候选集
    （静默漏评，从数字上完全看不出来）。

    收口条件刻意很窄：只认 `triggered_by LIKE 'test-%'`，**绝不碰** `manual` / `schedule`。
    """
    ids = [r["id"] for r in db.query(
        "SELECT id FROM scoring_run WHERE triggered_by LIKE %s", ("test-%",))]
    for rid in ids:
        _purge_run(rid)
    return ids


def main():
    global _ORIG_MODE
    _ORIG_MODE = db.get_setting(_MODE_KEY)
    orig_analyze = analyze.analyze
    analyze.analyze = ok_stub
    db.set_setting(_MODE_KEY, "off")           # 兜底：绝不在测试期间外写
    rows_old, rows_new = None, None
    run_old = run_new = None
    try:
        print("\n--- 旧路径（use_graph_session=false）---")
        rows_old, res_old = run_batch(False, "old")
        print(f"  start -> {res_old}")
        check("旧路径批量启动成功", rows_old is not None, str(res_old))
        if rows_old:
            run_old = rows_old["id"]
            print(f"  run={run_old} status={rows_old['status']} done={rows_old['session_done']} "
                  f"skipped={rows_old['session_skipped']} assessments={rows_old['assessment_new']} "
                  f"llm_failed={rows_old['llm_failed']}")

        print("\n--- 新图路径（use_graph_session=true）---")
        rows_new, res_new = run_batch(True, "new")
        print(f"  start -> {res_new}")
        check("图路径批量启动成功", rows_new is not None, str(res_new))
        if rows_new:
            run_new = rows_new["id"]
            print(f"  run={run_new} status={rows_new['status']} done={rows_new['session_done']} "
                  f"skipped={rows_new['session_skipped']} assessments={rows_new['assessment_new']} "
                  f"llm_failed={rows_new['llm_failed']}")

        if rows_old and rows_new:
            check("两路径均正常结束（status=success）",
                  rows_old["status"] == "success" and rows_new["status"] == "success",
                  f"{rows_old['status']}/{rows_new['status']}")
            for key in ("session_done", "session_skipped", "assessment_new", "llm_failed"):
                check(f"计数口径一致：{key}",
                      rows_old[key] == rows_new[key],
                      f"old={rows_old[key]} new={rows_new[key]}")
            check("确实评了会话（非空跑）", rows_old["assessment_new"] > 0,
                  str(rows_old["assessment_new"]))
            check("图路径 llm_calls 与旧路径同量级",
                  rows_old["llm_calls"] == rows_new["llm_calls"],
                  f"old={rows_old['llm_calls']} new={rows_new['llm_calls']}")
    finally:
        analyze.analyze = orig_analyze
        cleanup([run_old, run_new])
        cleanup_stale_test_runs()      # 兜底：连历史遗留一并收口（见该函数注释）
        db.execute("DELETE FROM app_setting WHERE key='use_graph_session'")
        if _ORIG_MODE is None:
            db.execute("DELETE FROM app_setting WHERE key=%s", (_MODE_KEY,))
        else:
            db.set_setting(_MODE_KEY, _ORIG_MODE)
        print("\n（已清理批量测试痕迹）")

    print(f"\n===== {len(PASSED)} passed / {len(FAILED)} failed =====")
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
