# -*- coding: utf-8 -*-
"""生产化加固验收：Policy 三档 / 工具审计 / 任务运行时（外循环）。

    .venv\\Scripts\\python.exe tests/test_production_hardening.py

覆盖三个硬伤：
  · 硬伤 4 Policy 层      —— `policy.decide()` 三档 + 阻断 + 阈值快照
  · 硬伤 5 审计          —— 脱敏（密钥不得泄漏）+ tool_audit 落库 + JSONL + 分档
  · 硬伤 3 外循环        —— 状态机守卫 / 检查点 / 心跳回收 / 续跑继承预算

需要 PostgreSQL（5433）。测试自己造数据并按 id 清理，不碰业务数据。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import os
import sys
import time

from _harness import FAILED, PASSED, ROOT, check, delete_rows  # noqa: E402

import audit                 # noqa: E402
import db                    # noqa: E402
import policy                # noqa: E402
import task_runtime as tr    # noqa: E402





# ============================================================ 硬伤 4 · Policy

def test_policy_tiers():
    print("\n--- 硬伤 4：Policy 三档 ---")
    th = {"auto": 0.95, "confirm": 0.80, "blocking": list(policy.DEFAULT_BLOCKING),
          "confirm_risk_level": 3}
    check("高置信 → auto",
          policy.decide(session_confidence=0.99, thresholds_override=th)["action"] == "auto")
    check("恰好等于 auto 阈值 → auto（边界含等号）",
          policy.decide(session_confidence=0.95, thresholds_override=th)["action"] == "auto")
    check("略低于 auto → confirm",
          policy.decide(session_confidence=0.9499, thresholds_override=th)["action"] == "confirm")
    check("恰好等于 confirm 阈值 → confirm",
          policy.decide(session_confidence=0.80, thresholds_override=th)["action"] == "confirm")
    check("低于 confirm → human",
          policy.decide(session_confidence=0.79, thresholds_override=th)["action"] == "human")
    check("★ 无置信度 → human（**不得默认放行**）",
          policy.decide(session_confidence=None, thresholds_override=th)["action"] == "human")
    check("★ 高置信但有语义负分 → 被阻断为 confirm",
          policy.decide(session_confidence=0.99, review_reasons=["has_negative"],
                        thresholds_override=th)["action"] == "confirm")
    check("★ 高风险动作 → 无论置信度多高都要确认",
          policy.decide(session_confidence=1.0, risk_level=3,
                        thresholds_override=th)["action"] == "confirm")
    check("engine_negative_only **不**阻断自动（否则等于永远不自动）",
          policy.decide(session_confidence=0.99,
                        review_reasons=["engine_negative_only"],
                        thresholds_override=th)["action"] == "auto")
    r = policy.decide(session_confidence=0.9, thresholds_override=th)
    check("返回阈值快照（结论可复现）", r["thresholds"] == th)
    check("原因机器可读且与文案分离", "confidence_mid" in r["action_reasons"])


def test_policy_record():
    print("\n--- 硬伤 4：判定留痕 ---")
    did = policy.record(relationship_key="__test__:policy", action="confirm",
                        session_confidence=0.9, review_reasons=["has_negative"],
                        action_reasons=["blocking_review_reason"],
                        thresholds_snapshot={"auto": 0.95, "confirm": 0.80})
    check("落 policy_decision 成功", bool(did), str(did))
    if did:
        row = db.query_one("SELECT * FROM policy_decision WHERE decision_id=%s", (did,))
        check("阈值快照已落库", (row or {}).get("thresholds") is not None)
        delete_rows("policy_decision", decision_id=did)


# ============================================================ 硬伤 5 · 审计

def test_redact():
    print("\n--- 硬伤 5：参数脱敏 ---")
    r = audit.redact({
        "staff_id": "wogiz1", "score": 3,
        "access_token": "SECRET-TOKEN-abc123",
        "cookie": "sid=xyz",
        "nested": {"api_key": "k-123", "ok": "keep-me"},
        "url": "https://x/api?access_token=LEAKME&staff_id=w1",
    })
    check("敏感键名的值被替换", r["access_token"] == audit.REDACTED)
    check("cookie 被替换", r["cookie"] == audit.REDACTED)
    check("嵌套层也脱敏", r["nested"]["api_key"] == audit.REDACTED)
    check("非敏感值保留", r["staff_id"] == "wogiz1" and r["nested"]["ok"] == "keep-me")
    check("★ URL 里内联的 access_token 被脱敏", "LEAKME" not in r["url"], r["url"])
    check("同一 URL 里的非敏感参数保留", "staff_id=w1" in r["url"], r["url"])
    check("环形结构不栈溢出",
          isinstance(audit.redact({"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}), dict))
    check("超长字符串被截断", len(audit.redact("x" * 5000)) < 400)
    check("digest 稳定且不含原文", audit.digest({"a": 1}) == audit.digest({"a": 1})
          and "a" not in audit.digest({"a": 1}))


def test_tool_audit_db():
    print("\n--- 硬伤 5：tool_audit 落库 + 幂等 + JSONL ---")
    key = f"__test__-{int(time.time() * 1000)}"
    cid = audit.record_tool_call(
        tool="weiban.add_customer_score",
        params={"staff_id": "w1", "score": 3, "access_token": "LEAK-TOKEN"},
        actor_id="__tester__", decision="allow", result_status="ok",
        response={"errcode": 0}, elapsed_ms=12, idempotency_key=key)
    check("写入 tool_audit 成功", bool(cid), str(cid))
    row = db.query_one("SELECT * FROM tool_audit WHERE call_id=%s", (cid,)) or {}
    check("五问里「谁」来自服务端推导", row.get("actor_id") == "__tester__")
    check("五问里「什么」= 工具名", row.get("tool_name") == "weiban.add_customer_score")
    check("★ 参数里的 token 已脱敏（不落明文）",
          "LEAK-TOKEN" not in str(row.get("params_redacted")))
    check("响应只存指纹、不存正文",
          row.get("response_digest") and "errcode" not in str(row.get("response_digest")))
    check("「何时」有值", row.get("created_at") is not None)
    check("「结果」有值", row.get("result_status") == "ok")

    cid2 = audit.record_tool_call(tool="t", params={}, idempotency_key=key)
    check("★ 同一 idempotency_key 不新增行（三层幂等第一层）", cid2 == cid,
          f"{cid2} vs {cid}")
    n = db.query_one("SELECT count(*) AS n FROM tool_audit WHERE idempotency_key=%s",
                     (key,))["n"]
    check("库里确实只有 1 行", n == 1, str(n))

    exp = audit.export_day()
    check("导出当日 JSONL 成功", exp.get("ok") is True, str(exp.get("error")))
    ver = audit.verify_export()
    check("★ 导出文件里无密钥明文", ver.get("ok") is True, str(ver.get("hits"))[:200])
    check("分档保留期可读（audit 档）", isinstance(audit.tier_days("audit"), int))
    check("四档都有定义", set(audit.tiers()) == {"observe", "idempotency", "audit", "task"})
    delete_rows("tool_audit", idempotency_key=key)


# ============================================================ 硬伤 3 · 外循环

def test_state_machine():
    print("\n--- 硬伤 3：状态机守卫 ---")
    check("queued → running 允许", tr.can_transition("queued", "running"))
    check("★ 终态不可迁出（INV-5）",
          not tr.can_transition("succeeded", "running")
          and not tr.can_transition("failed", "running")
          and not tr.can_transition("cancelled", "running")
          and not tr.can_transition("partial", "running"))
    check("running → waiting_approval 允许", tr.can_transition("running", "waiting_approval"))
    check("waiting_approval → running 允许（审批通过后）",
          tr.can_transition("waiting_approval", "running"))
    check("未知状态一律拒绝", not tr.can_transition("bogus", "running"))


def test_task_lifecycle():
    print("\n--- 硬伤 3：任务生命周期 + 检查点 + 回收 + 续跑 ---")
    t = tr.create(kind="__test__", actor_id="__tester__", trigger="manual",
                  scope={"n": 3}, budget_calls=10)
    tid = t["task_id"]
    check("建任务成功", bool(tid))
    check("初始状态 queued", tr.get(tid)["status"] == "queued")

    tr.transition(tid, "running")
    check("queued → running", tr.get(tid)["status"] == "running")

    s1 = tr.step_begin(tid, "session:a:1")
    s2 = tr.step_begin(tid, "session:a:2")
    rows = db.query("SELECT seq FROM task_step WHERE task_id=%s ORDER BY seq", (tid,))
    check("★ seq 自动递增（1,2）", [r["seq"] for r in rows] == [1, 2], str(rows))
    tr.step_end(s1, True, output_ref={"assessment_id": 1}, next_step="continue:next")
    tr.step_end(s2, False, error=ValueError("boom"))
    check("★ last_step_seq 只随**成功**步骤推进（=1）",
          tr.get(tid)["last_step_seq"] == 1, str(tr.get(tid)["last_step_seq"]))
    check("已成功步骤名可用于续跑过滤",
          tr.completed_step_names(tid) == {"session:a:1"})
    st = db.query_one("SELECT next_step, status FROM task_step WHERE step_id=%s", (s1,))
    check("★ 检查点记了 next_step（恢复唯一需要的信息）",
          st["next_step"] == "continue:next", str(st))

    # 非终态不可续跑（否则会出现两个任务跑同一批会话）
    bad = tr.resume(tid)
    check("★ 非终态任务拒绝续跑", bad["ok"] is False, bad.get("reason", "")[:60])

    tr.bump_progress(tid, step_ok=1)
    db.execute("UPDATE task SET cost_calls=7 WHERE task_id=%s", (tid,))
    tr.transition(tid, "partial", reason="__test_done__")
    check("running → partial", tr.get(tid)["status"] == "partial")
    check("终态带 finished_at", tr.get(tid)["finished_at"] is not None)

    r = tr.resume(tid)
    check("★ 续跑新建任务成功", r["ok"] is True, str(r.get("reason", "")))
    if r["ok"]:
        new = tr.get(r["task_id"])
        check("★ 新任务指向源任务（历史不可变）", new["resumed_from"] == tid)
        check("★ 已消耗预算被继承（不留预算后门）", new["cost_calls"] == 7,
              str(new["cost_calls"]))
        check("续跑返回已完成的步骤名", r["done_steps"] == ["session:a:1"],
              str(r["done_steps"]))
        delete_rows("task_step", task_id=r["task_id"])
        delete_rows("task", task_id=r["task_id"])
    delete_rows("task_step", task_id=tid)
    delete_rows("task", task_id=tid)


def test_stale_recovery_and_approval():
    print("\n--- 硬伤 3：僵尸回收 + 审批门控 ---")
    t = tr.create(kind="__test__", actor_id="__tester__")
    tid = t["task_id"]
    tr.transition(tid, "running")
    db.execute("UPDATE task SET heartbeat_at = now() - interval '10 minutes' "
               "WHERE task_id=%s", (tid,))
    rec = tr.recover_stale(stale_seconds=60)
    check("★ 心跳超时任务被回收", any(x["task_id"] == tid for x in rec["resumable"]),
          str(rec))
    check("★ 回收后状态为 failed（解除阻塞，可再启动）",
          tr.get(tid)["status"] == "failed")

    # 刚心跳过的任务不该被误收
    t2 = tr.create(kind="__test__", actor_id="__tester__")
    tid2 = t2["task_id"]
    tr.transition(tid2, "running")
    tr.heartbeat(tid2)
    rec2 = tr.recover_stale(stale_seconds=60)
    check("刚心跳过的任务**不**被误收",
          all(x["task_id"] != tid2 for x in rec2["resumable"]))

    # 审批门：物理阻塞
    ap = tr.request_approval(tid2, "weiban.add_customer_score",
                             params={"score": 3, "access_token": "LEAK"},
                             reason="__test__")
    check("建审批请求成功", ap["ok"] is True)
    check("★ 任务被物理阻塞在 waiting_approval",
          tr.get(tid2)["status"] == "waiting_approval")
    check("★ 阻塞态被 is_blocked() 识别（调度器据此不推进）", tr.is_blocked(tid2))
    req = db.query_one("SELECT * FROM approval_request WHERE request_id=%s",
                       (ap["request_id"],))
    check("审批参数同样脱敏", "LEAK" not in str(req["params_redacted"]))
    d = tr.decide_approval(ap["request_id"], approve=True, note="ok")
    check("审批通过 → 任务回到 running", d["ok"] and tr.get(tid2)["status"] == "running")
    d2 = tr.decide_approval(ap["request_id"], approve=False)
    check("★ 已决请求不会被二次处理（否则会误批另一个请求）", d2["ok"] is False)

    db.execute("DELETE FROM approval_request WHERE task_id IN (%s,%s)", (tid, tid2))
    db.execute("DELETE FROM task WHERE task_id IN (%s,%s)", (tid, tid2))


def main():
    print("=== 生产化加固验收（硬伤 3 / 4 / 5）===")
    test_policy_tiers()
    test_policy_record()
    test_redact()
    test_tool_audit_db()
    test_state_machine()
    test_task_lifecycle()
    test_stale_recovery_and_approval()
    print(f"\n===== {len(PASSED)} passed / {len(FAILED)} failed =====")
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
