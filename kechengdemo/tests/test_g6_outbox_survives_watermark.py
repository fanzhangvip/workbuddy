# -*- coding: utf-8 -*-
"""G6 实测：回写失败不再永久丢失（跨进程验证）—— 需要 PostgreSQL(5433)。

    .venv/Scripts/python.exe tests/test_g6_outbox_survives_watermark.py

G6 是什么
    改造前，回写失败只留一行 ``score_writeback.status='failed'``；而增量水位
    （``assessment.last_msg_time``）在 assessment 落库时**已经推进**，该会话因此
    永久退出候选集 —— 这次回写**永远不会再被尝试**，客户评分悄悄少记一次。

怎么证明修好了
    阶段一（本进程）：打桩微伴返回失败 → 跑一次评分 → 断言
        · assessment 已落库且 outbox 为 pending
        · score_writeback 无 ok 行
        · ``_candidates`` **已不再**返回该会话（水位确实推进了 → 证明二者解耦）
    阶段二（**另起一个进程**，模拟「进程被杀后重启」）：恢复微伴 → 跑 drain → 断言
        · outbox 转 done
        · score_writeback 对该 assessment **恰好 1 行且 status=ok**
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"


import os
import subprocess
import sys

from _harness import (FAILED, PASSED, ROOT, check, delete_rows,  # noqa: E402
                      pick_session_for_test)

import analyze                # noqa: E402
import db                     # noqa: E402
import outbox                 # noqa: E402
import rules as rules_mod     # noqa: E402
import scoring_service as ss  # noqa: E402
import weiban                 # noqa: E402

RUN_ID = 990000001
_MODE_KEY = "score_writeback_mode"
_ORIG_MODE = None
_ORIG_DECIDE = None




def stub_analyze(conversation, facts, rules, **kw):
    """确定性 stub：固定给 1 分正分，保证会触发 live 入队。"""
    return {
        "positive_items": [{"rule_id": "positive_cooperative",
                            "dimension": "配合度、认可度高", "polarity": "positive",
                            "score": 1, "reason": "stub", "evidence": [],
                            "evidence_ids": ["#001"], "evidence_message_ids": ["stub-msg-1"]}],
        "negative_items": [], "invalid_items": [], "unchecked_rules": [],
        "profile_summary": "stub", "sales_strategy": "stub", "model_confidence": 0.5,
        "rule_version": rules.get("rule_version"), "prompt_version": "test-g6",
        "schema_version": "1", "contract_attempts": 1,
    }


def pick_unscored_candidate():
    """挑一个能做 G6 实测的会话（**复用** `_harness.pick_session_for_test`）。

    原先这段 SQL 是本文件私有的，于是同一个坑（挑到"客户一句话没说"的会话 →
    被 B4 闸门跳过 → 断言失败）在两个测试里各修一遍。现在收敛成一处。
    """
    return pick_session_for_test(min_messages=8, require_unscored=True)


def cleanup(staff_id, session_id):
    db.execute("""DELETE FROM writeback_outbox WHERE assessment_id IN
                  (SELECT assessment_id FROM customer_intent_assessment
                    WHERE source_run_id = %s)""", (RUN_ID,))
    db.execute("""DELETE FROM negative_review WHERE assessment_id IN
                  (SELECT assessment_id FROM customer_intent_assessment
                    WHERE source_run_id = %s)""", (RUN_ID,))
    # ⚠️ D2-1 之后回写成功会推进 `writeback_ledger`（**新增写副作用**）→ 必须同步清。
    # 两种删法都要：① 按 assessment 关联；② **按客户关系键** ——
    # 因为上一轮清理可能已把 assessment 删了（`last_assessment_id` 成了悬空引用），
    # 只按 ① 会残留水位，导致下一轮「差值=0 → 不入队」把用例弄挂（实测踩到）。
    db.execute("""DELETE FROM writeback_ledger WHERE last_assessment_id IN
                    (SELECT assessment_id FROM customer_intent_assessment
                      WHERE source_run_id = %s)""", (RUN_ID,))
    db.execute("""DELETE FROM writeback_ledger WHERE relationship_key IN
                    (SELECT c.staff_id || ':' || c.target_id FROM chat_session c
                      WHERE c.staff_id = %s AND c.session_id = %s)""",
               (staff_id, session_id))
    db.execute("""DELETE FROM score_writeback WHERE assessment_id IN
                  (SELECT assessment_id FROM customer_intent_assessment
                    WHERE source_run_id = %s)""", (RUN_ID,))
    delete_rows("customer_intent_assessment", source_run_id=RUN_ID)
    # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
    try: ss.repair_current_flags()
    except Exception: pass
    db.execute("DELETE FROM session_failure WHERE staff_id=%s AND session_id=%s",
               (staff_id, session_id))


# ============================================================ 阶段二：独立进程

def phase2_drain():
    """在**新进程**里排空 outbox（模拟后端被杀后重启，drain job 生效）。"""
    import json
    aid = json.loads(sys.argv[2])
    # 时间旅行：把退避窗口"拨到已到期"（否则 drain 会正确地不予抢占，
    # 那样测的就不是"能否排空"而是"退避是否生效"——后者在阶段一已断言）。
    db.execute("UPDATE writeback_outbox SET next_attempt_at = now() - interval '1 second' "
               "WHERE assessment_id=%s", (aid,))

    def ok_api(staff_id, external_user_id="", score=0, **kw):
        return {"errcode": 0, "errmsg": "ok"}

    weiban.add_customer_score = ok_api
    res = outbox.drain(limit=10)
    ob = db.query_one("SELECT status FROM writeback_outbox WHERE assessment_id=%s", (aid,))
    wb = db.query("SELECT status FROM score_writeback WHERE assessment_id=%s", (aid,))
    out = {"drain": res, "outbox": ob and ob["status"], "writeback": [r["status"] for r in wb]}
    print("PHASE2_RESULT " + json.dumps(out))
    return 0 if (ob and ob["status"] == "done" and len(wb) == 1
                 and wb[0]["status"] == "ok") else 1


# ============================================================ 阶段一

def main():
    global _ORIG_MODE
    _ORIG_MODE = db.get_setting(_MODE_KEY)
    cand = pick_unscored_candidate()
    if not cand:
        print("跳过：库里没有「未评分过且消息数 ≥8」的会话，无法做 G6 实测。")
        return 0
    print(f"G6 目标：staff={cand['staff_id'][:14]}… session={cand['session_id'][:18]}… "
          f"msgs={cand['msg_count']}")

    try:
        cleanup(cand["staff_id"], cand["session_id"])
        db.set_setting(_MODE_KEY, "live")          # 触发入队
        analyze.analyze = stub_analyze             # 不真调模型
        # Policy 层打桩（硬伤 4 引入）：本测试验的是 **outbox 机制**（G6 不丢），
        # 与"置信度够不够自动写"无关。不打桩的话，Policy 判定为 confirm/human
        # 会先拦住同步投递 → outbox 停在 pending，测试就测不到它本意要测的路径。
        # 这与本文件既有的"打桩 analyze / 打桩微伴接口"是同一手法。
        import policy
        global _ORIG_DECIDE
        _ORIG_DECIDE = policy.decide
        policy.decide = lambda **kw: {"action": policy.AUTO,
                                      "action_reasons": ["__test_stub__"],
                                      "thresholds": {}}
        weiban.add_customer_score = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("HTTP 503：微伴暂不可用"))  # 打桩 5xx

        rules = rules_mod.load_rules()
        rv = rules.get("rule_version") or "v1.0"

        print("\n--- 阶段一：评分（回写必失败）---")
        r = ss.service._score_one(RUN_ID, cand, rules, 8, "live", False)
        check("评分链路返回 scored（单会话失败不拖垮主流程）", r == "scored", r)

        aid = db.query_one("SELECT assessment_id FROM customer_intent_assessment "
                           "WHERE source_run_id=%s", (RUN_ID,))["assessment_id"]
        ob = db.query_one("SELECT status, attempts FROM writeback_outbox WHERE assessment_id=%s", (aid,))
        wb = db.query("SELECT status FROM score_writeback WHERE assessment_id=%s", (aid,))
        print(f"  assessment_id={aid} outbox={ob} writeback={[x['status'] for x in wb]}")

        check("assessment 已落库", aid is not None)
        check("outbox 已入队且为 failed（带退避待重试）",
              bool(ob) and ob["status"] == "failed", str(ob))
        check("score_writeback 无 ok 行（本次确实没写成功）",
              all(x["status"] != "ok" for x in wb), str([x["status"] for x in wb]))

        print("\n--- 关键断言：增量水位已推进（会话不再被选中）---")
        cands = ss.service._candidates(None, 20000, 8, False, rv)
        still = [c for c in cands if c["session_id"] == cand["session_id"]]
        check("该会话已退出候选集（水位推进 → 常规流程永远扫不到它）",
              not still, f"仍被选中 {len(still)} 次")
        check("→ 因此只能靠 outbox 独立排空（这正是 G6 的修复点）", not still)

        print("\n--- 退避生效：立刻 drain 不应抢占（未到 next_attempt_at）---")
        now_drain = outbox.drain(limit=10)
        check("退避未到期时 drain 不抢占", now_drain.get("checked") == 0, str(now_drain))

        print("\n--- 阶段二：另起进程模拟「重启后排空」---")
        py = sys.executable
        proc = subprocess.run([py, os.path.abspath(__file__), "--drain", str(aid)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        tail = (proc.stdout or "")[-500:]
        print("  " + tail.strip().replace("\n", "\n  "))
        check("子进程退出码 0", proc.returncode == 0, str(proc.returncode))
        check("子进程报告 outbox=done 且 score_writeback 恰 1 行 ok",
              '"outbox": "done"' in tail and '["ok"]' in tail, tail[-160:])

        ob2 = db.query_one("SELECT status FROM writeback_outbox WHERE assessment_id=%s", (aid,))
        wb2 = db.query("SELECT status FROM score_writeback WHERE assessment_id=%s", (aid,))
        check("重启后：outbox 终态 = done", ob2["status"] == "done", ob2["status"])
        check("重启后：score_writeback 恰好 1 行（不重复）", len(wb2) == 1, str(len(wb2)))
        check("重启后：该行 status = ok", wb2 and wb2[0]["status"] == "ok",
              str([x["status"] for x in wb2]))
    finally:
        cleanup(cand["staff_id"], cand["session_id"])
        if _ORIG_DECIDE is not None:            # 还原 Policy 判定的打桩
            import policy
            policy.decide = _ORIG_DECIDE
        if _ORIG_MODE is None:
            delete_rows("app_setting", key=_MODE_KEY)
        else:
            db.set_setting(_MODE_KEY, _ORIG_MODE)
        print("\n（已清理 G6 实测痕迹）")

    print(f"\n===== {len(PASSED)} passed / {len(FAILED)} failed =====")
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--drain":
        sys.exit(phase2_drain())
    sys.exit(main())
