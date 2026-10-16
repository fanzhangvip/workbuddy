# -*- coding: utf-8 -*-
"""G0「立即止血」验收 —— 三处真 bug 的回归测试（需要 PostgreSQL 5433）。

    .venv/Scripts/python.exe tests/test_g0_hardening.py

覆盖文档 §8 的 G0 组（零外部依赖、只修既有缺陷）：
  · **A3**  `force` 真全量：改造前 `force` 收了却从未进 SQL，「全量重评」点了等于没点
  · **H1**  互斥守卫：同步与评分不得同时跑（改造前 `_job` 只查同步自己）
  · **G1**  取标签不静默：`compute_facts` 取标签失败须留 `tool_failure` 并在事实文本标注降级
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"


import sys

from _harness import FAILED, PASSED, check, delete_rows  # noqa: E402

import db                     # noqa: E402
import scheduler              # noqa: E402
import scoring_service as ss  # noqa: E402
import sync_service           # noqa: E402
import weiban                 # noqa: E402





def pick_session():
    return db.query_one(
        """SELECT c.staff_id, c.session_id, c.target_id, c.msg_count, c.last_msg_time
             FROM chat_session c
            WHERE c.session_type = 'ext_user' AND c.target_id IS NOT NULL
              AND c.msg_count >= 1
            ORDER BY c.msg_count DESC, c.session_id LIMIT 1""")


# ============================================================ A3

def test_force_full_scan():
    print("\n--- A3. force 真全量 ---")
    sess = pick_session()
    if not sess:
        check("存在可用会话", False, "库里没有 ext_user 会话")
        return
    rv = ss.current_rule_version()
    rel = f"{sess['staff_id']}:{sess['target_id']}"
    aid = None
    try:
        # 造一条「已评过、水位刚好追平、规则版本相同」的评估 → 增量应排除该会话
        aid = db.query_one(
            """INSERT INTO customer_intent_assessment
                 (relationship_key, staff_id, session_id, raw_score, positive_total,
                  negative_total, grade, last_msg_time, rule_version,
                  writeback_status, review_status, analyzed_at)
               VALUES (%s,%s,%s,0,0,0,'C',%s,%s,'na','na',now())
               RETURNING assessment_id""",
            (rel, sess["staff_id"], sess["session_id"], sess["last_msg_time"], rv),
        )["assessment_id"]

        inc = ss.service._candidates(None, 5000, 1, False, rv)
        full = ss.service._candidates(None, 5000, 1, True, rv)
        inc_ids = {(c["staff_id"], c["session_id"]) for c in inc}
        full_ids = {(c["staff_id"], c["session_id"]) for c in full}
        key = (sess["staff_id"], sess["session_id"])

        check("增量口径排除「水位已追平」的会话", key not in inc_ids, f"inc={len(inc)}")
        check("force 全量**包含**该会话（修 A3）", key in full_ids, f"full={len(full)}")
        check("force 全量集合 ⊇ 增量集合", inc_ids.issubset(full_ids),
              f"inc={len(inc_ids)} full={len(full_ids)}")
    finally:
        if aid:
            delete_rows("customer_intent_assessment", assessment_id=aid)
            # E2 自愈：删掉「被取代的新行」会让旧行永久失去 current → 补修（幂等）
            try: ss.repair_current_flags()
            except Exception: pass
            print("（已清理合成的 assessment）")


# ============================================================ H1

def test_mutual_exclusion():
    print("\n--- H1. 互斥守卫 ---")
    # ⚠️ 别再用「打桩实例的 is_running」来模拟在跑的任务：P3 之后互斥判定统一走
    # `job_service.running_kinds()`（**查库、跨进程可见**），打桩实例拦不到它 ——
    # 那样测试会假绿/假红。这里改成**插一条真实的 running 行**，测的就是真实判据。
    def _fake_running(table):
        return db.query_one(
            f"""INSERT INTO {table} (status, triggered_by, detail)
                VALUES ('running', 'test-guard', '{{}}') RETURNING id""")["id"]

    try:
        # 都空闲
        check("都空闲 → sync 可启动", scheduler.guard_can_start("sync") is True)
        check("都空闲 → scoring 可启动", scheduler.guard_can_start("scoring") is True)

        # 评分在跑（真插一行 running，心跳随 now() 刷新）
        rid = _fake_running("scoring_run")
        check("评分在跑 → 定时同步被拒（修 H1）",
              scheduler.guard_can_start("sync") is False)
        check("评分在跑 → 定时评分被拒",
              scheduler.guard_can_start("scoring") is False)
        r3 = sync_service.service.start()
        check("评分在跑 → 手动 sync.start 被拒且提示含「评分」",
              r3.get("ok") is False and "评分" in str(r3.get("reason")), str(r3))
        delete_rows("scoring_run", id=rid)

        # 同步在跑
        jid = _fake_running("sync_job")
        check("同步在跑 → 定时评分被拒",
              scheduler.guard_can_start("scoring") is False)
        check("同步在跑 → 定时同步被拒",
              scheduler.guard_can_start("sync") is False)
        r2 = ss.service.start()
        check("同步在跑 → 手动 scoring.start 被拒且提示含「同步」",
              r2.get("ok") is False and "同步" in str(r2.get("reason")), str(r2))
        delete_rows("sync_job", id=jid)
    finally:
        delete_rows("scoring_run", triggered_by="test-guard")
        delete_rows("sync_job", triggered_by="test-guard")


# ============================================================ G1

def test_tag_failure_not_silent():
    print("\n--- G1. 取标签失败不再静默 ---")
    sess = pick_session()
    if not sess:
        check("存在可用会话", False, "库里没有 ext_user 会话")
        return
    orig = weiban.get_custom_fields
    try:
        # 失败路径
        def _boom(*a, **kw):
            raise RuntimeError("40058 凭据已失效")

        weiban.get_custom_fields = _boom
        f = ss.compute_facts(sess["staff_id"], sess["session_id"], fetch_tags=True)
        check("取标签失败 → tool_failure 有值（不再 except: pass）",
              bool(f.get("tool_failure")), str(f.get("tool_failure"))[:60])
        check("取标签失败 → 事实文本标注【降级】", "【降级】" in (f.get("text") or ""))

        # 计数接线
        progress_bak = dict(ss.service._progress)
        ss.service._progress = {}
        ss.service._note_facts(f)
        check("失败被计入 tool_failures", ss.service._progress.get("tool_failures") == 1,
              str(ss.service._progress))

        # 成功路径：不产生 tool_failure，且标签进事实文本
        weiban.get_custom_fields = lambda *a, **kw: [
            {"field_name": "到课", "field_value": "已到课", "field_type": "text",
             "option_list": []}]
        f2 = ss.compute_facts(sess["staff_id"], sess["session_id"], fetch_tags=True)
        check("取标签成功 → tool_failure 为空", not f2.get("tool_failure"),
              str(f2.get("tool_failure")))
        check("取标签成功 → 事实文本含标签值", "已到课" in (f2.get("text") or ""))
        ss.service._note_facts(f2)
        check("成功不计入 tool_failures（仍为 1）",
              ss.service._progress.get("tool_failures") == 1,
              str(ss.service._progress))
        ss.service._progress = progress_bak

        # fetch_tags=False → 根本不取，也不报降级
        f3 = ss.compute_facts(sess["staff_id"], sess["session_id"], fetch_tags=False)
        check("fetch_tags=False → 无 tool_failure 也无降级标注",
              not f3.get("tool_failure") and "【降级】" not in (f3.get("text") or ""))
    finally:
        weiban.get_custom_fields = orig


def main():
    test_force_full_scan()
    test_mutual_exclusion()
    test_tag_failure_not_silent()
    print(f"\n===== {len(PASSED)} passed / {len(FAILED)} failed =====")
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
