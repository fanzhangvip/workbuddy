# -*- coding: utf-8 -*-
"""回写「结果未知」与「人工改判修正结论」两条新语义 —— 需要 PostgreSQL 5433。

    .venv\\Scripts\\python.exe tests\\test_writeback_unknown_and_correction.py

## 守的是什么（2026-10-16 的一轮缺陷修复）

1. **结果未知的回写不许自动重投**：微伴「客户评分」是**累加、不可撤销**的，
   "HTTP 发出去了但没拿到结果"（超时/断连/进程被杀）重投就是**再多加一次分**。
   现在这类行置 `dead + needs_reconcile`，等人工读回客户累计分后收口（`reconcile`）。
2. **`human` 档不进自动链路**：低置信度（< confirm_min）优先判 `human`，
   既不发审批也不投递；此前它会被判成 `confirm`，于是"有人点一下批准"就能把
   0.1 置信度的结论写进微伴。
3. **人工改判要真的修正结论**：复核提交只写队列与 verification_status 时，
   客户在微伴上的分数/等级仍是 AI 的错判。现在按人工负分**本地确定性重算**总分与等级。
4. **配置写侧校验**：注册表有类型与边界声明，写侧不再直接落库。

⚠️ 全部合成行（`ZZWBC:` 前缀）+ 打桩微伴接口，跑完删净并自检无残留。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import sys

from _harness import check, delete_rows, summary  # noqa: E402

import assessment_repo          # noqa: E402
import db                       # noqa: E402
import outbox                   # noqa: E402
import policy                   # noqa: E402
import settings_registry as sr  # noqa: E402
import weiban                   # noqa: E402

STAFF = "ZZWBC:staff"
TARGET = "ZZWBC:u1"
REL = f"{STAFF}:{TARGET}"
_MODE_KEY = "score_writeback_mode"


def _wipe():
    db.execute("DELETE FROM approval_request WHERE assessment_id IN "
               "(SELECT assessment_id FROM customer_intent_assessment WHERE relationship_key=%s)",
               (REL,))
    for t in ("writeback_outbox", "score_writeback", "writeback_ledger", "negative_review"):
        db.execute(f"DELETE FROM {t} WHERE relationship_key=%s", (REL,))
    delete_rows("customer_intent_assessment", relationship_key=REL)
    db.execute("DELETE FROM staff WHERE staff_id=%s", (STAFF,))


def _mk(policy_action="auto", positive=5, negative_total=0, raw=5, grade="A") -> int:
    db.execute("INSERT INTO staff (staff_id, name) VALUES (%s,%s) ON CONFLICT (staff_id) DO NOTHING",
               (STAFF, "回写测试员工"))
    aid = db.query_one(
        """INSERT INTO customer_intent_assessment
             (relationship_key, staff_id, staff_name, target_id, target_name, session_id,
              raw_score, grade, positive_total, negative_total, rule_version,
              policy_action, writeback_status, review_status, is_current)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'test',%s,'skipped','na',true)
           RETURNING assessment_id""",
        (REL, STAFF, "回写测试员工", TARGET, "测试客户", "ZZWBC:s1",
         raw, grade, positive, negative_total, policy_action))["assessment_id"]
    with db.tx() as conn:
        outbox.enqueue(conn, aid, REL, STAFF, TARGET, positive, "live")
    return aid


def _row(aid):
    return db.query_one("SELECT * FROM writeback_outbox WHERE assessment_id=%s", (aid,))


def _ledger():
    return db.query_one("SELECT * FROM writeback_ledger WHERE relationship_key=%s", (REL,))


def main():
    _wipe()
    orig_mode = db.get_setting(_MODE_KEY)
    orig_send = weiban.add_customer_score
    try:
        db.set_setting(_MODE_KEY, "live")

        # ---------------- ① 结果未知的判定口径 ----------------
        import http_client
        check("★ 连接层失败（超时/断连）→ 判定为「结果未知」",
              outbox._unknown_outcome(http_client.HttpFailure("网络错误：TimeoutError: timed out",
                                                              retryable=True)) is True)
        check("★ 对端给了明确 HTTP 状态 → 明确失败（可重投）",
              outbox._unknown_outcome(http_client.HttpFailure("HTTP 500 boom", retryable=True,
                                                              status=500)) is False)
        check("★ 明确业务 errcode → 明确失败（可重投）",
              outbox._unknown_outcome(http_client.HttpFailure(
                  "接口返回错误 errcode=20215 errmsg=score 非法", retryable=False)) is False)

        # ---------------- ② 未知结果 → 停投 + 待对账（不重投） ----------------
        aid = _mk("auto", positive=4, raw=4, grade="A")
        weiban.add_customer_score = lambda *a, **kw: (_ for _ in ()).throw(
            TimeoutError("read timed out"))
        st = outbox.drain(limit=10)
        row = _row(aid)
        check("★ 结果未知的行被标 dead（不再自动重投）", row["status"] == "dead", str(row["status"]))
        check("★ 并打上「待对账」标记", row["needs_reconcile"] is True, str(row["needs_reconcile"]))
        check("投递统计里单列 unknown", st.get("unknown") == 1, str(st))
        check("再 drain 一次不会碰它（已被排除在抢占之外）",
              outbox.drain(limit=10).get("checked") == 0)

        # ---------------- ③ 人工对账：确认已写入 → 补记账 + 推水位 ----------------
        r = outbox.reconcile(aid, written=True, note="读回累计分确认已写")
        check("对账「已写入」成功", r.get("ok") is True, str(r))
        check("★ 水位被推进（不推的话下次差值会把这笔分再写一遍）",
              (_ledger() or {}).get("written_total") == 4, str(_ledger()))
        check("★ 待办收口为 done，且清掉待对账标记",
              _row(aid)["status"] == "done" and _row(aid)["needs_reconcile"] is False)
        check("已对账的条目不会重复处理",
              outbox.reconcile(aid, written=True).get("ok") is False)

        # ---------------- ④ 人工对账：确认未写入 → 恢复投递 ----------------
        aid2 = _mk("auto", positive=3, raw=3, grade="B")
        weiban.add_customer_score = lambda *a, **kw: (_ for _ in ()).throw(
            ConnectionResetError("connection reset by peer"))
        outbox.drain(limit=10)
        check("第二条也停在待对账", _row(aid2)["needs_reconcile"] is True)
        outbox.reconcile(aid2, written=False, note="读回累计分确认没写")
        check("★ 确认未写入 → 回到 pending 且可投递",
              _row(aid2)["status"] == "pending" and _row(aid2)["next_attempt_at"] <= _row(aid2)["updated_at"])

        # ---------------- ⑤ `human` 档不进自动链路 ----------------
        check("★ 低置信度优先判 human（不再被高风险短路成 confirm）",
              policy.decide(session_confidence=0.1, risk_level=3)["action"] == policy.HUMAN,
              policy.decide(session_confidence=0.1, risk_level=3)["action"])
        check("★ 低置信度 + 阻断原因也是 human",
              policy.decide(session_confidence=0.1,
                            review_reasons=["has_negative"])["action"] == policy.HUMAN)
        check("置信度够 + 高风险 → confirm（人工确认后可写）",
              policy.decide(session_confidence=0.97, risk_level=3)["action"] == policy.CONFIRM)
        check("置信度够 + 无风险 → auto",
              policy.decide(session_confidence=0.97)["action"] == policy.AUTO)

        aid3 = _mk("human", positive=6, raw=6, grade="A")
        gate = outbox._policy_gate(_row(aid3))
        check("human 的闸门结论是 review（不是 hold）", gate == "review", gate)
        weiban.add_customer_score = lambda *a, **kw: {"errcode": 0, "errmsg": "ok"}
        st3 = outbox.drain(limit=10)
        check("★ human 的待办被停投（dead），不会写进微伴",
              _row(aid3)["status"] == "dead", str(_row(aid3)["status"]))
        check("没产生任何 ok 回写记录",
              db.query_one("SELECT count(*) n FROM score_writeback WHERE assessment_id=%s"
                           " AND status='ok'", (aid3,))["n"] == 0)
        check("统计里单列 review_hold", st3.get("review_hold", 0) >= 1, str(st3))

        # ---------------- ⑥ confirm 仍走审批队列（批准后可投递） ----------------
        aid4 = _mk("confirm", positive=2, raw=2, grade="B")
        check("confirm 的闸门结论是 hold（等签字）",
              outbox._policy_gate(_row(aid4)) == "hold")
        import task_runtime as tr
        ap = tr.request_writeback_approval(assessment_id=aid4, relationship_key=REL,
                                           staff_id=STAFF, external_user_id=TARGET,
                                           score=2, reason="__test__")
        tr.decide_approval(ap["request_id"], approve=True, note="同意")
        check("批准后放行（闸门变 deliver）", outbox._policy_gate(_row(aid4)) == "deliver")

        # ---------------- ⑦ 人工改判修正结论分数与等级 ----------------
        aid5 = _mk("auto", positive=5, negative_total=0, raw=5, grade="A")
        res = assessment_repo.apply_human_correction(aid5, human_negative_total=-3,
                                                    human_items=[{"rule_id": "x", "score": -3}],
                                                    actor="__test__")
        after = db.query_one("SELECT raw_score, grade, negative_total, positive_total,"
                             " rule_version, is_current FROM customer_intent_assessment"
                             " WHERE assessment_id=%s", (aid5,))
        check("★ 改判后总分 = 正分 + 人工负分（5 + (-3) = 2）", after["raw_score"] == 2,
              f"{after['raw_score']} / {res}")
        check("★ 等级跟着重算（2 → B）", after["grade"] == "B", after["grade"])
        check("正分与规则版本**不动**（否则会触发增量重评）",
              after["positive_total"] == 5 and after["rule_version"] == "test")
        check("修正结果里带 before/after（可审计）",
              res["before"]["grade"] == "A" and res["after"]["grade"] == "B"
              and res["changed"] is True, str(res))
        res2 = assessment_repo.apply_human_correction(aid5, human_negative_total=0)
        check("人工认为不该扣分 → 回到正分满分、等级 A",
              res2["after"]["raw_score"] == 5 and res2["after"]["grade"] == "A", str(res2))

        # ---------------- ⑧ 配置写侧校验 ----------------
        check("未知键被拒", sr.validate("没这个键", 1)[0] is False)
        check("INT 收到非数字被拒", sr.validate("ctx_window_size", "abc")[0] is False)
        check("★ 声明了 min=1 的键拒绝 0", sr.validate("ctx_window_size", 0)[0] is False
              and sr.validate("ctx_window_size", 30)[0] is True)
        check("★ 声明了 max 的键拒绝越界",
              sr.validate("confidence_review_min", 1.5)[0] is False
              and sr.validate("confidence_review_min", 0.6)[1] == 0.6)
        check("BOOL 接受常见写法并归一",
              sr.validate("trace_enabled", "yes") == (True, True)
              and sr.validate("trace_enabled", "0") == (True, False))
    finally:
        weiban.add_customer_score = orig_send
        _wipe()
        if orig_mode is None:
            db.execute("DELETE FROM app_setting WHERE key=%s", (_MODE_KEY,))
        else:
            db.set_setting(_MODE_KEY, orig_mode)
        print("（已清理本次实测痕迹）")
    return summary()


if __name__ == "__main__":
    sys.exit(main())
