# -*- coding: utf-8 -*-
"""回写 outbox 排空（drain）单测 —— 需要 PostgreSQL(5433)。

    .venv/Scripts/python.exe tests/test_outbox_drain.py

用打桩替换 ``weiban.add_customer_score``，覆盖状态机的五条路径：
    ok / failed（退避并前推 next_attempt_at）/ dry_run / off / dead（超尝试上限）
不触网、可反复运行；用负数 assessment_id 造数据，结束后清理。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "backend"))

import db                     # noqa: E402
import outbox                 # noqa: E402
import scoring_service as ss  # noqa: E402
import weiban                 # noqa: E402

PASSED, FAILED = [], []
_MODE_KEY = "score_writeback_mode"
_ORIG_MODE = None


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if detail else ""))


def set_mode(m):
    db.set_setting(_MODE_KEY, m)


def mk_outbox(aid, score=3, status="pending", attempts=0, max_attempts=8):
    db.execute(
        """INSERT INTO writeback_outbox
             (assessment_id, relationship_key, staff_id, target_id, score, mode,
              status, attempts, max_attempts, next_attempt_at)
           VALUES (%s,'t:rel','t_staff','t_target',%s,'live',%s,%s,%s,
                   now() - interval '1 hour')
           ON CONFLICT (assessment_id) DO UPDATE
              SET status=EXCLUDED.status, attempts=EXCLUDED.attempts,
                  max_attempts=EXCLUDED.max_attempts,
                  next_attempt_at=EXCLUDED.next_attempt_at,
                  updated_at=now()""",
        (aid, score, status, attempts, max_attempts))


def get_outbox(aid):
    return db.query_one("SELECT * FROM writeback_outbox WHERE assessment_id=%s", (aid,))


def get_wb(aid):
    return db.query("SELECT * FROM score_writeback WHERE assessment_id=%s", (aid,))


def cleanup(*aids):
    for aid in aids:
        db.execute("DELETE FROM writeback_outbox WHERE assessment_id=%s", (aid,))
        db.execute("DELETE FROM score_writeback WHERE assessment_id=%s", (aid,))


def main():
    global _ORIG_MODE
    _ORIG_MODE = db.get_setting(_MODE_KEY)
    calls = []

    def ok_api(staff_id, external_user_id="", score=0, **kw):
        calls.append((staff_id, external_user_id, score))
        return {"errcode": 0, "errmsg": "ok"}

    # ---- 用例 1：live 成功 → outbox done，score_writeback 恰 1 行 ok ----
    a1 = -901001
    try:
        cleanup(a1)
        set_mode("live")
        weiban.add_customer_score = ok_api
        mk_outbox(a1, score=5)
        res = outbox.drain(limit=10)
        ob, wb = get_outbox(a1), get_wb(a1)
        check("ok：outbox 置 done", ob["status"] == "done", ob["status"])
        check("ok：score_writeback 恰 1 行且 status=ok",
              len(wb) == 1 and wb[0]["status"] == "ok", str(wb))
        check("ok：确实调用了微伴（一次）", len(calls) == 1, str(calls))
        check("ok：drain 统计 ok=1", res.get("ok") == 1, str(res))

        # ---- 用例 2：重复 drain → 不产生第二行（幂等）----
        calls.clear()
        outbox.drain(limit=10)
        check("幂等：再次 drain 不再投递（outbox 已 done）", len(calls) == 0, str(calls))
        check("幂等：score_writeback 仍恰 1 行", len(get_wb(a1)) == 1)

        # ---- 用例 3：live 失败 → failed + 退避 + 前推 next_attempt_at ----
        a2 = -901002
        cleanup(a2)
        set_mode("live")

        def boom(staff_id, external_user_id="", score=0, **kw):
            raise RuntimeError("HTTP 503：微伴暂不可用")

        weiban.add_customer_score = boom
        mk_outbox(a2, score=4)
        before = db.query_one("SELECT now() AS t")["t"]
        res2 = outbox.drain(limit=10)
        ob2, wb2 = get_outbox(a2), get_wb(a2)
        check("失败：outbox 置 failed", ob2["status"] == "failed", ob2["status"])
        check("失败：attempts 累加为 1", int(ob2["attempts"]) == 1, str(ob2["attempts"]))
        check("失败：next_attempt_at 被前推（> now）", ob2["next_attempt_at"] > before,
              f"{ob2['next_attempt_at']} vs {before}")
        check("失败：score_writeback 记 failed（恰 1 行）",
              len(wb2) == 1 and wb2[0]["status"] == "failed", str(wb2))
        check("失败：drain 统计 failed=1", res2.get("failed") == 1, str(res2))

        # ---- 用例 4：dry_run → 记账 dry_run，且**不**外写 ----
        a3 = -901003
        cleanup(a3)
        calls.clear()
        set_mode("dry_run")
        weiban.add_customer_score = ok_api
        mk_outbox(a3, score=6)
        outbox.drain(limit=10)
        ob3, wb3 = get_outbox(a3), get_wb(a3)
        check("dry_run：outbox 置 done", ob3["status"] == "done", ob3["status"])
        check("dry_run：score_writeback 记 dry_run",
              len(wb3) == 1 and wb3[0]["status"] == "dry_run", str(wb3))
        check("dry_run：未调用微伴", len(calls) == 0, str(calls))

        # ---- 用例 5：off → 暂停（保持 pending，延后重试，不丢数据）----
        a4 = -901004
        cleanup(a4)
        calls.clear()
        set_mode("off")
        mk_outbox(a4, score=2)
        before4 = db.query_one("SELECT now() AS t")["t"]
        res4 = outbox.drain(limit=10)
        ob4 = get_outbox(a4)
        check("off：outbox 保持 pending（暂停而非作废）", ob4["status"] == "pending", ob4["status"])
        check("off：next_attempt_at 被延后", ob4["next_attempt_at"] > before4,
              f"{ob4['next_attempt_at']} vs {before4}")
        check("off：未写 score_writeback（未做任何决定）", len(get_wb(a4)) == 0)
        check("off：未调用微伴", len(calls) == 0, str(calls))
        check("off：drain 统计 deferred=1", res4.get("deferred") == 1, str(res4))

        # ---- 用例 6：超过尝试上限 → dead（终态）----
        a5 = -901005
        cleanup(a5)
        set_mode("live")
        weiban.add_customer_score = boom
        mk_outbox(a5, score=3, attempts=0, max_attempts=1)
        outbox.drain(limit=10)
        ob5 = get_outbox(a5)
        check("超限：outbox 转 dead", ob5["status"] == "dead", ob5["status"])
        check("超限：attempts=1 = max_attempts", int(ob5["attempts"]) == 1, str(ob5["attempts"]))
    finally:
        cleanup(-901001, -901002, -901003, -901004, -901005)
        if _ORIG_MODE is None:
            db.execute("DELETE FROM app_setting WHERE key=%s", (_MODE_KEY,))
        else:
            db.set_setting(_MODE_KEY, _ORIG_MODE)

    print(f"\n===== {len(PASSED)} passed / {len(FAILED)} failed =====")
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
