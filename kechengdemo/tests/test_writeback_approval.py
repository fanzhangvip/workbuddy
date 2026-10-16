# -*- coding: utf-8 -*-
"""回写审批门控：策略拦下的回写必须**有人工入口放行**。

    .venv\\Scripts\\python.exe tests\\test_writeback_approval.py

## 守的是什么（2026-10-16 接线）

改造前这条链是**断的**，而且断得很隐蔽：

  · `policy` 判定 `confirm` / `human` → `scoring_service._writeback` 记一行
    `status='skipped'`，中文写着「需人工二次确认后才写入」；
  · `outbox._policy_hold()` 也确实把那条待办**挂起不投递**（G6：宁可留着也不静默丢）；
  · 但是 —— **没有任何地方能把这个"确认"送回来**：`approval_request` 只有测试在写，
    审批队列永远是空的。于是那句承诺在系统里兑现不了，待办会永远躺在 `pending`。

现在：拦下即登记审批（**不阻塞批次**）→ 批准放行投递 / 驳回置终态 / 过期拒绝再签。

⚠️ 全部用合成行（`ZZAPPR:` 前缀），跑完删净；不调微伴（只验门控与状态流转）。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import sys

from _harness import check, delete_rows, summary  # noqa: E402

import db                     # noqa: E402
import outbox                 # noqa: E402
import task_runtime as tr     # noqa: E402
import writeback_repo         # noqa: E402

STAFF = "ZZAPPR:staff"
TARGET = "ZZAPPR:u1"
REL = f"{STAFF}:{TARGET}"


def _wipe():
    # approval_request 不在 `_harness.CLEANABLE_TABLES` 白名单里（那张表的清理闸没开），
    # 所以这里按 assessment 关联删 —— 条件写死成 REL，不会碰到真实数据。
    db.execute("DELETE FROM approval_request WHERE assessment_id IN "
               "(SELECT assessment_id FROM customer_intent_assessment "
               " WHERE relationship_key = %s)", (REL,))
    db.execute("DELETE FROM writeback_outbox WHERE relationship_key = %s", (REL,))
    delete_rows("customer_intent_assessment", relationship_key=REL)
    delete_rows("writeback_ledger", relationship_key=REL)
    # `staff` 也不在清理白名单里 → 直接删（条件写死成 ZZAPPR: 前缀）
    db.execute("DELETE FROM staff WHERE staff_id = %s", (STAFF,))


def _mk(policy_action: str = "confirm", score: int = 10) -> int:
    """造一条「当前有效」的评估 + 一条待投递的回写。"""
    db.execute("INSERT INTO staff (staff_id, name) VALUES (%s,%s) "
               "ON CONFLICT (staff_id) DO NOTHING", (STAFF, "审批测试员工"))
    aid = db.query_one(
        """INSERT INTO customer_intent_assessment
             (relationship_key, staff_id, staff_name, target_id, target_name, session_id,
              raw_score, grade, positive_total, negative_total, rule_version,
              policy_action, writeback_status, review_status, is_current)
           VALUES (%s,%s,%s,%s,%s,%s,%s,'A',%s,0,'test',%s,'skipped','na',true)
           RETURNING assessment_id""",
        (REL, STAFF, "审批测试员工", TARGET, "测试客户", "ZZAPPR:s1",
         score, score, policy_action))["assessment_id"]
    with db.tx() as conn:
        outbox.enqueue(conn, aid, REL, STAFF, TARGET, score, "live")
    return aid


def _row(aid):
    return db.query_one("SELECT * FROM writeback_outbox WHERE assessment_id=%s", (aid,))


def _ask(aid, score=10):
    return tr.request_writeback_approval(
        assessment_id=aid, relationship_key=REL, staff_id=STAFF,
        external_user_id=TARGET, score=score, reason="__test__")


def main():
    _wipe()
    try:
        # ---------------- ① 挂起：策略说"要人工确认" ----------------
        aid = _mk("confirm")
        check("★ 策略 confirm 的回写被挂起（不自动投递）",
              outbox._policy_gate(_row(aid)) == "hold")

        # ---------------- ② 登记审批：幂等、不阻塞批次 ----------------
        r1 = _ask(aid)
        check("审批请求已建", r1.get("ok") is True and bool(r1.get("request_id")))
        check("★ 不阻塞批次（blocked=False；它卡的是这一条回写，不是整轮评分）",
              r1.get("blocked") is False)
        req = db.query_one("SELECT * FROM approval_request WHERE request_id=%s",
                           (r1["request_id"],))
        check("审批行挂到了这条评估上（放行时据此找回要投递的分数）",
              req["assessment_id"] == aid and req["task_id"] is None, str(req["task_id"]))
        check("待投递分数写进审批参数（人工看得到会加多少分）",
              (req["params_redacted"] or {}).get("score") == 10,
              str(req["params_redacted"])[:80])
        r2 = _ask(aid)
        check("★ 幂等：同一条评估不重复建请求",
              r2.get("reused") is True and r2["request_id"] == r1["request_id"])
        check("待审期间仍然挂起（批准前不投递）", outbox._policy_gate(_row(aid)) == "hold")

        # ---------------- ③ 批准 → 放行投递 ----------------
        d = tr.decide_approval(r1["request_id"], approve=True, note="同意")
        check("批准成功", d.get("ok") is True)
        check("★ 批准后不再挂起（下一轮 drain 会投递）",
              outbox._policy_gate(_row(aid)) == "deliver")
        n = writeback_repo.release_on_approve(aid)
        row = _row(aid)
        check("★ 放行时把 next_attempt_at 拉回现在（否则要再等最多 10 分钟）",
              n == 1 and row["next_attempt_at"] <= row["updated_at"], f"n={n}")
        d2 = tr.decide_approval(r1["request_id"], approve=True)
        check("★ 已决请求不会被二次处理", d2.get("ok") is False)

        # ---------------- ④ 驳回 → 待办置终态 ----------------
        # 用 `confirm` 的行来验「驳回 ≠ 放行」：驳回后闸门仍是 hold（不放行）。
        # （`human` 走的是另一条路 —— 闸门直接是 review、待办停投，见
        #  `test_writeback_unknown_and_correction.py`。）
        aid2 = _mk("confirm", score=7)
        r3 = _ask(aid2, score=7)
        tr.decide_approval(r3["request_id"], approve=False, note="证据不足")
        closed = writeback_repo.close_on_reject(aid2, note="证据不足")
        check("★ 驳回把待办置死（不再反复延后、也不投递）",
              closed == 1 and _row(aid2)["status"] == "dead", str(_row(aid2)["status"]))
        check("★ 驳回**不算放行**（人工明确说过不写）",
              outbox._policy_gate(_row(aid2)) == "hold"
              and db.query_one("SELECT status FROM approval_request WHERE request_id=%s",
                               (r3["request_id"],))["status"] == "rejected")

        # ---------------- ⑤ 过期：拒绝再签 ----------------
        aid3 = _mk("confirm", score=5)
        r4 = _ask(aid3, score=5)
        db.execute("""UPDATE approval_request SET expires_at = now() - interval '1 minute'
                       WHERE request_id=%s""", (r4["request_id"],))
        d3 = tr.decide_approval(r4["request_id"], approve=True)
        check("★ 过期请求拒绝处理（分数/会话/阈值都可能已变）",
              d3.get("ok") is False and "过期" in (d3.get("reason") or ""),
              str(d3.get("reason"))[:80])
        check("过期后就地标成 expired（不再挂在「待审批」里误导人）",
              db.query_one("SELECT status FROM approval_request WHERE request_id=%s",
                           (r4["request_id"],))["status"] == "expired")

        # ---------------- ⑥ 有 task_id 的审批仍走原语义（工具网关那条路）----------------
        t = tr.create(kind="__test_appr__", actor_id="__tester__")
        tid = t["task_id"]
        tr.transition(tid, "running")
        ap = tr.request_approval(tid, "some.tool", params={"x": 1}, reason="__test__")
        check("有 task 的审批仍然是**物理阻塞**（工具网关语义不变）",
              tr.get(tid)["status"] == "waiting_approval" and ap["blocked"] is True)
        tr.decide_approval(ap["request_id"], approve=True)
        check("通过后任务回到 running", tr.get(tid)["status"] == "running")
        db.execute("DELETE FROM approval_request WHERE task_id=%s", (tid,))
        db.execute("DELETE FROM task WHERE task_id=%s", (tid,))
    finally:
        _wipe()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
