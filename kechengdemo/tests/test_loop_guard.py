# -*- coding: utf-8 -*-
"""Loop 护栏验收 —— G3 总超时 / G4 成本预算 / B3 partial / G7 优先级排序 / H2 审计（需要 PostgreSQL 5433）。

    .venv/Scripts/python.exe tests/test_loop_guard.py

对应架构文档 §2.2 编排层与 §2.9「Loop 八条护栏」的补齐：
  · **G3** `deadline`   整轮总超时（改造前「超时」只能靠单会话内部重试耗时自然累积，无法中断整轮）
  · **G4** `max_llm_calls` 成本预算（LLM 调用真花钱，改造前完全无上限）
  · **B3** `partial`    状态：撞护栏 ≠ 失败，已处理的都有效
  · **G7** 候选优先级排序：改造前只按 `last_msg_time DESC`，限流时**先砍掉沉默的高价值客户**
  · **H2** 审计三节点 `scoring_start` / `scoring_done` / `scoring_failed`

设计原则：**纯函数 + 不真跑 LLM**。护栏判定抽成 `guard_reason()` 便于穷举边界；
`partial` 状态用「打桩 _score_cand 秒返回」的方式跑一次真实 `_run`，不产生模型调用。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"


import os
import sys
import time

from _harness import FAILED, PASSED, ROOT, check, delete_rows, purge_run  # noqa: E402

import db                     # noqa: E402
import audit                  # noqa: E402
import scoring_service as ss  # noqa: E402





def _set(key, val):
    db.set_setting(key, str(val))


def _clear(*keys):
    for k in keys:
        delete_rows("app_setting", key=k)


def _cleanup_run(run_id):
    purge_run(run_id)                       # 公共产物（见 _harness.purge_run）
    # 本用例额外的产物：会话失败记录（按 run_id）与审计
    delete_rows("session_failure", run_id=run_id)
    delete_rows("audit_log", target=f"run:{run_id}")


# ============================================================ 1. guard_reason 纯函数

def test_guard_reason():
    print("\n--- 1. guard_reason 纯函数（护栏判定，穷举边界）---")
    NOW = 1_000_000.0

    check("无护栏（都为空）→ 放行", ss.guard_reason(None, None, 0, NOW) is None)
    check("deadline 未到 → 放行", ss.guard_reason(NOW + 10, None, 0, NOW) is None)
    check("deadline 已到 → 'deadline'", ss.guard_reason(NOW - 1, None, 0, NOW) == "deadline")
    check("deadline 恰好等于 now → 触发（>= 语义）",
          ss.guard_reason(NOW, None, 0, NOW) == "deadline")
    check("预算未用满 → 放行", ss.guard_reason(None, 500, 499, NOW) is None)
    check("预算恰好用满 → 'max_llm_calls'",
          ss.guard_reason(None, 500, 500, NOW) == "max_llm_calls")
    check("预算为 0 → 视为不限（放行）", ss.guard_reason(None, 0, 99999, NOW) is None)
    check("预算为 None → 视为不限（放行）", ss.guard_reason(None, None, 99999, NOW) is None)
    check("llm_calls 为 None/缺省 → 当 0 处理",
          ss.guard_reason(None, 10, None, NOW) is None)
    check("两者同时越界 → deadline 优先返回",
          ss.guard_reason(NOW - 1, 1, 99, NOW) == "deadline")
    check("deadline=0 → 视为不限", ss.guard_reason(0, None, 0, NOW) is None)


# ============================================================ 2. start() 落 deadline / max_llm_calls

def test_start_writes_guard_fields():
    print("\n--- 2. start() 把 deadline / max_llm_calls 写进 scoring_run ---")
    run_id = None
    try:
        _set("scoring_deadline_minutes", 90)
        _set("scoring_max_llm_calls", 321)
        # 打桩：让 _run 立刻返回（不真评分）—— 这里只关心 start() 写入的字段
        orig = ss.service._run
        ss.service._run = lambda *a, **kw: None
        try:
            out = ss.service.start(triggered_by="test")
            # ⚠️ 不要直接 `out["run_id"]`：`start()` 会在**同步正在跑**时拒绝启动
            # （互斥闸是设计使然，见 scoring_service.start 的注释）。直接下标会抛
            # `KeyError: 'run_id'`，看起来像代码坏了，其实是环境里有个真同步在跑
            # —— 「app 容器开着且 SYNC_INTERVAL_MINUTES > 0」时最容易踩到。
            check("评分批量可以启动（无同步占用）", out.get("ok"), out)
            if not out.get("ok"):
                print("        ↳ 跑测试前请先停掉在跑的同步/评分："
                      "把 app 容器的 SYNC_INTERVAL_MINUTES 设为 0，或 `docker compose stop app`")
                return
            run_id = out["run_id"]
        finally:
            ss.service._run = orig
        row = db.query_one(
            "SELECT deadline, max_llm_calls, status FROM scoring_run WHERE id=%s", (run_id,))
        check("deadline 已写（非空）", row["deadline"] is not None, str(row["deadline"]))
        check("max_llm_calls = 321", row["max_llm_calls"] == 321, str(row["max_llm_calls"]))
        # deadline 应约等于 now + 90 分钟
        delta_min = (row["deadline"].timestamp() - time.time()) / 60
        check("deadline ≈ now + 90 分钟", 88 <= delta_min <= 91, f"{delta_min:.1f} 分钟")

        print("  · deadline=0 → 不限期（列写 NULL）")
        # 必须先收掉上一个打桩 run：`is_running()` 现在**含跨进程 DB 判定**，
        # 停在 `running` 的记录会正确拦下第二次 `start()`（这本身佐证互斥有效，
        # 但会挡住本用例的第二段，所以先清掉）。
        _cleanup_run(run_id)
        _set("scoring_deadline_minutes", 0)
        _set("scoring_max_llm_calls", 0)
        ss.service._run = lambda *a, **kw: None
        try:
            out2 = ss.service.start(triggered_by="test")
            if not out2.get("ok"):
                check("第二次评分批量可以启动", False, out2)
                return
            rid2 = out2["run_id"]
        finally:
            ss.service._run = orig
        row2 = db.query_one("SELECT deadline, max_llm_calls FROM scoring_run WHERE id=%s", (rid2,))
        check("deadline=0 → 写 NULL", row2["deadline"] is None, str(row2["deadline"]))
        check("max_llm_calls=0 → 写 NULL", row2["max_llm_calls"] is None, str(row2["max_llm_calls"]))
        _cleanup_run(rid2)
    finally:
        _clear("scoring_deadline_minutes", "scoring_max_llm_calls")
        # 打桩的 run 会**停在 `running`**（`_run` 被替换成 no-op，不会自己收尾）：
        # 必须整个删掉，而不是只改状态 —— 否则既留残留，
        # 又会因跨进程互斥把后续测试的 `start()` 拦住（实测踩到过）。
        if run_id:
            _cleanup_run(run_id)


# ============================================================ 3. _run 触发 deadline → partial

def test_deadline_partial():
    print("\n--- 3. deadline 到点 → _run 优雅退出并置 partial ---")
    run_id = None
    try:
        # 让候选池非空：用真实会话（只取少量，且 _score_cand 被打桩 → 不调模型）
        sess = db.query_one("""SELECT staff_id, session_id, target_id, msg_count, last_msg_time
                                 FROM chat_session
                                WHERE session_type='ext_user' AND target_id IS NOT NULL
                                  AND msg_count >= 4 ORDER BY msg_count DESC LIMIT 5""")
        if not sess:
            check("存在可用会话", False, "库里没有会话")
            return
        # 直接建 run 并手写一个「已过期」的 deadline（比走 start 更直接、更可控）
        run_id = db.query_one(
            """INSERT INTO scoring_run (kind, status, triggered_by, rule_version, detail,
                                        deadline, max_llm_calls)
               VALUES ('full','running','test',%s,%s,
                       now() - interval '1 minute', 1000) RETURNING id""",
            (ss.current_rule_version(), db.as_json({})))["id"]

        calls = {"n": 0}
        orig = ss.service._score_cand
        ss.service._score_cand = lambda *a, **kw: (calls.__setitem__("n", calls["n"] + 1), "scored")[1]
        try:
            # 直接跑 _run（同步，不启线程）；staff_ids=None → 走全库候选
            ss.service._stop = False
            ss.service._partial = None
            ss.service._progress = {"llm_calls": 0, "session_done": 0, "session_failed": 0}
            ss.service._run(run_id, None, 5, 4, False, ss.current_rule_version())
        finally:
            ss.service._score_cand = orig
        row = db.query_one(
            "SELECT status, partial_reason, session_done FROM scoring_run WHERE id=%s", (run_id,))
        check("状态 = partial（不是 failed/success）", row["status"] == "partial", str(row["status"]))
        check("partial_reason = 'deadline'", row["partial_reason"] == "deadline",
              str(row["partial_reason"]))
        check("未处理任何会话（第 1 轮就被拦）", calls["n"] == 0, f"_score_cand 调用了 {calls['n']} 次")
    finally:
        if run_id:
            _cleanup_run(run_id)


# ============================================================ 4. _run 触发预算 → partial

def test_budget_partial():
    print("\n--- 4. llm_calls 达到上限 → _run 优雅退出并置 partial ---")
    run_id = None
    try:
        run_id = db.query_one(
            """INSERT INTO scoring_run (kind, status, triggered_by, rule_version, detail,
                                        deadline, max_llm_calls)
               VALUES ('full','running','test',%s,%s,
                       now() + interval '1 hour', 2) RETURNING id""",
            (ss.current_rule_version(), db.as_json({})))["id"]

        calls = {"n": 0}

        def fake_score(*a, **kw):
            calls["n"] += 1
            ss.service._progress["llm_calls"] = ss.service._progress.get("llm_calls", 0) + 1
            return "scored"

        orig = ss.service._score_cand
        ss.service._score_cand = fake_score
        try:
            ss.service._stop = False
            ss.service._partial = None
            ss.service._progress = {"llm_calls": 0, "session_done": 0, "session_failed": 0}
            ss.service._run(run_id, None, 10, 4, False, ss.current_rule_version())
        finally:
            ss.service._score_cand = orig
        row = db.query_one(
            "SELECT status, partial_reason, session_done, llm_calls FROM scoring_run WHERE id=%s",
            (run_id,))
        check("状态 = partial", row["status"] == "partial", str(row["status"]))
        check("partial_reason = 'max_llm_calls'", row["partial_reason"] == "max_llm_calls",
              str(row["partial_reason"]))
        check("恰好处理了预算允许的 2 个会话", calls["n"] == 2, f"实际 {calls['n']}")
        check("session_done 已落库 = 2", row["session_done"] == 2, str(row["session_done"]))
    finally:
        if run_id:
            _cleanup_run(run_id)


# ============================================================ 5. session_failed 落库 + audit

def test_session_failed_and_audit():
    print("\n--- 5. 失败会话落 session_failed 列 + 审计三节点 ---")
    run_id = None
    try:
        run_id = db.query_one(
            """INSERT INTO scoring_run (kind, status, triggered_by, rule_version, detail)
               VALUES ('full','running','test',%s,%s) RETURNING id""",
            (ss.current_rule_version(), db.as_json({})))["id"]

        n = {"i": 0}

        def boom(*a, **kw):
            n["i"] += 1
            raise RuntimeError("stub 失败")

        orig = ss.service._score_cand
        ss.service._score_cand = boom
        try:
            ss.service._stop = False
            ss.service._partial = None
            ss.service._progress = {"llm_calls": 0, "session_done": 0, "session_failed": 0}
            ss.service._run(run_id, None, 3, 4, False, ss.current_rule_version())
        finally:
            ss.service._score_cand = orig
        row = db.query_one("SELECT session_failed, session_done, status FROM scoring_run WHERE id=%s",
                           (run_id,))
        check("session_failed 已落列（不再是只在内存的数）",
              row["session_failed"] == n["i"] and row["session_failed"] > 0,
              f"列={row['session_failed']} 实际失败={n['i']}")
        check("失败的会话**不计** session_done", row["session_done"] == 0, str(row["session_done"]))
        check("状态仍为 success（单会话失败不算整轮失败）", row["status"] == "success",
              str(row["status"]))

        acts = [r["action"] for r in db.query(
            "SELECT action FROM audit_log WHERE target=%s ORDER BY id", (f"run:{run_id}",))]
        check("写了 scoring_done 审计（_run 直接调用，无 start 节点）",
              "scoring_done" in acts, str(acts))

        print("  · 单独验证 scoring_start / scoring_failed 两个节点")
        before = db.query_one("SELECT count(*) AS n FROM audit_log WHERE action='scoring_start'")["n"]
        audit.record_action("test", "scoring_start", "run:999900001", {"k": 1})
        check("scoring_start 可写",
              db.query_one("SELECT count(*) AS n FROM audit_log WHERE target='run:999900001'")["n"] == 1)
        audit.record_action("system", "scoring_failed", "run:999900001", {"error": "x"})
        check("scoring_failed 可写",
              db.query_one("SELECT count(*) AS n FROM audit_log WHERE target='run:999900001' AND action='scoring_failed'")["n"] == 1)
        db.execute("DELETE FROM audit_log WHERE target='run:999900001'")
    finally:
        if run_id:
            _cleanup_run(run_id)


# ============================================================ 6. G7 优先级排序

def test_priority_order():
    print("\n--- 6. G7 候选优先级排序（S/A 顶到最前）---")
    rv = ss.current_rule_version()
    try:
        # force=True 才能把「已评过」的会话也纳入候选池（增量会因水位排除它们）
        _set("scoring_priority_order", "true")
        on = ss.service._candidates(None, 40, 8, True, rv)
        _set("scoring_priority_order", "false")
        off = ss.service._candidates(None, 40, 8, True, rv)
        _set("scoring_priority_order", "true")

        grades_on = [x["last_grade"] for x in on]
        ranked = [g for g in grades_on if g]
        if not ranked:
            print("    （当前库里没有「已评过且有等级」的会话，跳过顺序断言）")
        else:
            # 排在最前的非空等级必须是 S 或 A
            check("优先级排序：最前的是 S/A",
                  grades_on[0] in ("S", "A"), f"实际 {grades_on[0]}")
            # S/A 必须整体排在 B/C 之前
            top_idx = [i for i, g in enumerate(grades_on) if g in ("S", "A")]
            low_idx = [i for i, g in enumerate(grades_on) if g in ("B", "C", "D")]
            check("所有 S/A 都排在 B/C/D 之前",
                  (not top_idx or not low_idx or max(top_idx) < min(low_idx)),
                  f"S/A 末位={max(top_idx) if top_idx else '-'} B/C/D 首位={min(low_idx) if low_idx else '-'}")

        check("关掉开关 → 退回旧口径（仅按时间）", on != off,
              "两种排序结果不同（证明开关有效）")

        # 旧口径应严格按 last_msg_time 倒序
        times = [x["last_msg_time"] or 0 for x in off]
        check("旧口径：严格按 last_msg_time 倒序", times == sorted(times, reverse=True))
    finally:
        _clear("scoring_priority_order")


# ============================================================ 7. is_running 不被 partial 阻塞

def test_partial_does_not_block():
    print("\n--- 7. partial 状态的 run 不阻塞后续启动 ---")
    run_id = None
    try:
        run_id = db.query_one(
            """INSERT INTO scoring_run (kind, status, triggered_by, partial_reason)
               VALUES ('full','partial','test','deadline') RETURNING id""")["id"]
        check("is_running() 对 partial 返回 False", ss.service.is_running() is False)
    finally:
        if run_id:
            _cleanup_run(run_id)


# ============================================================

def main():
    print("=" * 68)
    print("Loop 护栏验收（G3/G4/B3/G7/H2）")
    print("=" * 68)
    try:
        test_guard_reason()
        test_start_writes_guard_fields()
        test_deadline_partial()
        test_budget_partial()
        test_session_failed_and_audit()
        test_priority_order()
        test_partial_does_not_block()
    finally:
        _clear("scoring_deadline_minutes", "scoring_max_llm_calls",
               "scoring_priority_order", "scoring_stale_minutes")
    print("\n" + "=" * 68)
    print("PASS %d / FAIL %d" % (len(PASSED), len(FAILED)))
    if FAILED:
        for f in FAILED:
            print("  FAILED:", f)
    print("=" * 68)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
