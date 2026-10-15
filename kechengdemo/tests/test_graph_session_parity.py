# -*- coding: utf-8 -*-
"""新旧路径一致性测试（图化的**放行门槛**）—— 需要 PostgreSQL(5433)。

    .venv/Scripts/python.exe tests/test_graph_session_parity.py

做法
    用**确定性 stub** 顶掉真实模型调用（按会话文本哈希造契约输出，无网络、可复现），
    再让「旧串行 `_score_one`」与「新图 `graph_session.run_session`」跑**同一批**会话，
    逐条比对 ``raw_score / grade / positive_total / negative_total /
    needs_human_review / unchecked_rules``。

判据
    差异计数必须为 **0**。只要有一个字段对不上，说明图化改动了口径，不得上线。

副作用
    ``mode="off"`` + ``fetch_tags=False`` → 不外写微伴；结束按 assessment_id 全量清理。

C-B 之后的注意点
    上下文装配（``build_context``）自 C-B 起依赖「上次评估水位」（③ 增量段）。
    旧路径先跑会插入 assessment → 水位推进 → 新路径的 ③ 变成 0 条，
    两条路径看到的上下文就不再相同 —— 那是在比较「水位前 vs 水位后」，
    不是比较「图化前 vs 图化后」。故每条样本在新路径开跑前**先抹掉旧路径的痕迹**。
"""
# 统一输出编码（修 P2-2）：默认 GBK 控制台下，断言名里的 ↔ → 等字符会触发
# UnicodeEncodeError —— 看起来像测试失败，其实是环境问题。
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")

import hashlib
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "backend"))

import analyze                # noqa: E402
import db                     # noqa: E402
import graph_session          # noqa: E402
import rules as rules_mod     # noqa: E402
import scoring_service as ss  # noqa: E402

OLD_RUN = 991000001
NEW_RUN = 992000001
N_TARGET = 30

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if detail else ""))


def make_deterministic_stub():
    """按会话文本哈希产出契约结果：无网络、无随机、两条路径拿到完全相同的东西。"""

    def stub(conversation, facts, rules, **kw):
        idx = rules_mod._index(rules)
        h = int(hashlib.sha256((conversation or "").encode("utf-8")).hexdigest(), 16)
        pos, neg, unchecked = [], [], []
        for n, rid in enumerate(rules_mod.llm_rule_ids(rules)):
            rule = idx[rid]
            bit = (h >> (n % 61))
            if (bit & 3) == 3:                      # 约 1/4 漏检 → 覆盖 unchecked_rules 分支
                unchecked.append(rid)
                continue
            if bit & 1:                             # 约 1/2 命中
                item = {"rule_id": rid, "dimension": rule.get("dimension"),
                        "polarity": rule["polarity"],
                        "score": rule["options"][0]["score"],
                        "reason": f"stub 命中 {rid}", "evidence": [],
                        "evidence_ids": ["#001"], "evidence_message_ids": ["stub-msg"]}
                (pos if rule["polarity"] == "positive" else neg).append(item)
        return {
            "positive_items": pos, "negative_items": neg, "invalid_items": [],
            "unchecked_rules": unchecked,
            "profile_summary": f"stub-{h % 1000}", "sales_strategy": "stub",
            "model_confidence": 0.5, "rule_version": rules.get("rule_version"),
            "prompt_version": "test-parity", "schema_version": "1", "contract_attempts": 1,
        }

    return stub


def pick_candidates(n):
    return db.query(
        """SELECT c.staff_id, s.name AS staff_name, c.session_id, c.target_id,
                  c.name AS target_name, c.msg_count, c.last_msg_time
             FROM chat_session c LEFT JOIN staff s ON s.staff_id = c.staff_id
            WHERE c.session_type = 'ext_user' AND c.target_id IS NOT NULL
              AND c.msg_count >= 8
              AND NOT EXISTS (
                  SELECT 1 FROM customer_intent_assessment a
                   WHERE a.relationship_key = c.staff_id || ':' || c.target_id)
            ORDER BY c.msg_count DESC LIMIT %s""", (n,))


def row_of(run_id, session_id):
    return db.query_one(
        """SELECT raw_score, positive_total, negative_total, grade,
                  needs_human_review, unchecked_rules
             FROM customer_intent_assessment
            WHERE source_run_id = %s AND session_id = %s""", (run_id, session_id))


def _purge_run(run_id, session_id):
    """抹掉某条样本上「某个 run 刚落的」评估痕迹，让它回到未评分的初始状态。"""
    row = db.query_one(
        """SELECT assessment_id FROM customer_intent_assessment
            WHERE source_run_id = %s AND session_id = %s""", (run_id, session_id))
    if not row:
        return
    aid = row["assessment_id"]
    for tbl in ("writeback_outbox", "negative_review", "score_writeback"):
        db.execute(f"DELETE FROM {tbl} WHERE assessment_id = %s", (aid,))
    db.execute("DELETE FROM customer_intent_assessment WHERE assessment_id = %s", (aid,))
    # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
    try: ss.repair_current_flags()
    except Exception: pass


def cleanup(session_ids):
    for run in (OLD_RUN, NEW_RUN):
        for tbl in ("writeback_outbox", "negative_review", "score_writeback"):
            db.execute(f"""DELETE FROM {tbl} WHERE assessment_id IN
                           (SELECT assessment_id FROM customer_intent_assessment
                             WHERE source_run_id = %s)""", (run,))
        db.execute("DELETE FROM customer_intent_assessment WHERE source_run_id = %s", (run,))
        # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
        try: ss.repair_current_flags()
        except Exception: pass
    for sid in session_ids:
        db.execute("DELETE FROM session_failure WHERE session_id = %s", (sid,))


def main():
    stub = make_deterministic_stub()
    analyze.analyze = stub                        # 两条路径共用同一个 stub
    rules = rules_mod.load_rules()

    cands = pick_candidates(N_TARGET)
    if len(cands) < 3:
        print(f"跳过：库里未评分且消息数 ≥8 的会话只有 {len(cands)} 个，样本不足。")
        return 0
    print(f"样本数：{len(cands)}")

    diffs = []
    n_old = n_new = 0
    try:
        for cand in cands:
            sid = cand["session_id"]
            old_r = ss.service._score_one(OLD_RUN, cand, rules, 8, "off", False)
            a = row_of(OLD_RUN, sid) if old_r == "scored" else None
            if old_r == "scored":
                n_old += 1
            # 让新路径从**完全相同的初始状态**出发（见模块 docstring 的 C-B 注意点）
            _purge_run(OLD_RUN, sid)

            new_r = graph_session.run_session(ss.service, NEW_RUN, cand, rules, 8, "off", False)
            b = row_of(NEW_RUN, sid) if new_r == "scored" else None
            if new_r == "scored":
                n_new += 1

            if old_r != new_r:
                diffs.append((sid, "outcome", old_r, new_r))
                continue
            if old_r == "skipped":
                continue

            if not a or not b:
                diffs.append((sid, "missing_row", bool(a), bool(b)))
                continue
            for key in ("raw_score", "positive_total", "negative_total", "grade",
                        "needs_human_review", "unchecked_rules"):
                va, vb = a.get(key), b.get(key)
                if isinstance(va, list):
                    va, vb = sorted(va), sorted(vb or [])
                if va != vb:
                    diffs.append((sid, key, va, vb))
                    continue

        check("两条路径的返回语义一致（scored/skipped）",
              all(d[1] != "outcome" for d in diffs), str(len([d for d in diffs if d[1] == 'outcome'])))
        check("两条路径的评分字段差异数 = 0", not diffs,
              f"差异 {len(diffs)} 处：" + str(diffs[:5]))

        check("两条路径落库条数相同", n_old == n_new, f"old={n_old} new={n_new}")
        check("样本确实产生了评估（非空跑）", n_old >= 3, str(n_old))
    finally:
        cleanup([c["session_id"] for c in cands])
        print("\n（已清理一致性测试痕迹）")

    print(f"\n===== {len(PASSED)} passed / {len(FAILED)} failed =====")
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
