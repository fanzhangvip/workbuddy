# -*- coding: utf-8 -*-
"""回写 outbox —— 把「评分」与「回写微伴」解耦（修 G6）。

问题（G6）
    改造前，回写失败只落一行 ``score_writeback.status='failed'``，仅此而已。
    而增量水位（``assessment.last_msg_time``）在 assessment 落库时**已经推进**，
    该会话因此永久退出候选集 —— 这次回写**永远不会再被尝试**，客户评分永久丢失
    （微伴「客户评分」是累计行为分，少一次就是少记一次热度）。

解法
    ``_persist_assessment`` 在**同一个事务**里写 assessment 与 outbox；
    回写失败由 ``drain()`` 按指数退避独立重试，**不再依赖增量水位**。

幂等分工
    · ``writeback_outbox UNIQUE(assessment_id)``  —— 保证「一定会尝试」
    · ``score_writeback  UNIQUE(assessment_id)``  —— 保证「一定只落一行」

投递语义
    至少一次（at-least-once）。极端情况（投递成功但记状态前进程被杀）会重复
    调用一次微伴接口；这比「永久丢失」可接受得多，且 ``score_writeback`` 不会
    产生重复行。卡在 ``sending`` 超过 ``STUCK_SECONDS`` 的行会在下次 drain 被回收。
"""
import time

import db

BACKOFF_BASE = 30           # 秒；首次失败后的等待
BACKOFF_CAP = 3600          # 秒；退避上限
DEFAULT_MAX_ATTEMPTS = 8    # 超过即转 dead（终态，待人工）
STUCK_SECONDS = 600         # sending 超过此时长视为「投递中断」，回收重试
OFF_DEFER_SECONDS = 600     # 回写被关闭时，任务延后这么久再看（不丢，不刷屏）


def _json(value):
    return db.as_json(value) if value is not None else None


def _delay(attempts: int) -> int:
    """第 attempts 次失败后的等待秒数（指数退避 + 上限）。"""
    return int(min(BACKOFF_BASE * (2 ** max(0, attempts - 1)), BACKOFF_CAP))


# ============================================================ 入队（调用方事务内）

def enqueue(conn, assessment_id, relationship_key, staff_id, target_id, score, mode) -> None:
    """在**调用方的事务连接**上入队。

    必须复用调用方的 ``conn``：只有和 assessment 的 INSERT 在同一个事务里，
    「评估落库」与「回写待办」才可能同时成功或同时失败（否则 G6 不成立）。
    """
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO writeback_outbox
                 (assessment_id, relationship_key, staff_id, target_id, score, mode, status)
               VALUES (%s,%s,%s,%s,%s,%s,'pending')
               ON CONFLICT (assessment_id) DO NOTHING""",
            (assessment_id, relationship_key, staff_id, target_id, int(score), mode))


# ============================================================ 图内同步尝试后的状态回填

def _apply_result(conn, assessment_id, ok, errcode=None, errmsg=None, resp=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT outbox_id, attempts, max_attempts
                 FROM writeback_outbox WHERE assessment_id = %s""",
            (assessment_id,))
        row = cur.fetchone()
        if not row:
            return                                  # 旧路径 / 未入队：静默跳过
        if ok:
            cur.execute(
                """UPDATE writeback_outbox
                      SET status='done', last_errcode=%s, last_errmsg=%s,
                          last_response=%s, updated_at=now()
                    WHERE outbox_id=%s""",
                (errcode, errmsg, _json(resp), row["outbox_id"]))
            return
        attempts = int(row["attempts"]) + 1
        dead = attempts >= int(row["max_attempts"])
        cur.execute(
            """UPDATE writeback_outbox
                  SET status=%s, attempts=%s, last_errcode=%s, last_errmsg=%s,
                      last_response=%s,
                      next_attempt_at = now() + make_interval(secs => %s),
                      updated_at=now()
                WHERE outbox_id=%s""",
            ("dead" if dead else "failed", attempts, errcode, errmsg,
             _json(resp), _delay(attempts), row["outbox_id"]))


def mark_result(assessment_id, ok, errcode=None, errmsg=None, resp=None, conn=None) -> None:
    """回填 outbox 状态。图内同步尝试成功后置 done，失败则进入退避待重试。

    自身异常一律吞掉：outbox 记账失败绝不能影响评分主流程（失败时该行仍是
    pending/failed，drain 下一轮照样会捡起来重试）。
    """
    try:
        if conn is not None:
            _apply_result(conn, assessment_id, ok, errcode, errmsg, resp)
        else:
            with db.tx() as c:
                _apply_result(c, assessment_id, ok, errcode, errmsg, resp)
    except Exception as e:
        print(f"[outbox] 回填状态失败（不影响主流程）：{e}")


# ============================================================ 排空（drain worker）

def _claim(conn, limit: int) -> list:
    """抢占到期任务：先回收「投递中断」的行，再把到期行标记为 sending。"""
    conn.execute(
        """UPDATE writeback_outbox
              SET status='failed', next_attempt_at=now(), updated_at=now(),
                  last_errmsg = COALESCE(last_errmsg, '') || ' [投递中断，已回收]'
            WHERE status='sending'
              AND updated_at < now() - make_interval(secs => %s)""",
        (STUCK_SECONDS,))
    with conn.cursor() as cur:
        cur.execute(
            """SELECT outbox_id, assessment_id, relationship_key, staff_id, target_id,
                      score, attempts, max_attempts
                 FROM writeback_outbox
                WHERE status IN ('pending', 'failed')
                  AND next_attempt_at <= now()
                ORDER BY next_attempt_at ASC, outbox_id ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED""",
            (limit,))
        rows = cur.fetchall()
        for r in rows:
            cur.execute(
                "UPDATE writeback_outbox SET status='sending', updated_at=now() WHERE outbox_id=%s",
                (r["outbox_id"],))
    return rows


def _record(conn, row, status, ok, attempts, errcode=None, errmsg=None, resp=None,
            dead=False) -> None:
    """一次投递结束：写 score_writeback（幂等 upsert）+ 回填 assessment + 落 outbox 终态。"""
    with conn.cursor() as cur:
        # score_writeback：UNIQUE(assessment_id)，用 upsert 保证恒一行
        cur.execute(
            """INSERT INTO score_writeback
                 (relationship_key, assessment_id, staff_id, external_user_id, score,
                  mode, status, attempts, errcode, errmsg, response)
               VALUES (%s,%s,%s,%s,%s,'live',%s,%s,%s,%s,%s)
               ON CONFLICT (assessment_id) DO UPDATE
                  SET status=EXCLUDED.status, attempts=EXCLUDED.attempts,
                      errcode=EXCLUDED.errcode, errmsg=EXCLUDED.errmsg,
                      response=EXCLUDED.response, updated_at=now()""",
            (row["relationship_key"], row["assessment_id"], row["staff_id"], row["target_id"],
             row["score"], status, attempts, errcode, errmsg, _json(resp)))
        cur.execute(
            "UPDATE customer_intent_assessment SET writeback_status=%s WHERE assessment_id=%s",
            (status, row["assessment_id"]))
        # outbox 终态
        if ok:
            cur.execute(
                """UPDATE writeback_outbox
                      SET status='done', last_errcode=%s, last_errmsg=%s,
                          last_response=%s, updated_at=now()
                    WHERE outbox_id=%s""",
                (errcode, errmsg, _json(resp), row["outbox_id"]))
        else:
            cur.execute(
                """UPDATE writeback_outbox
                      SET status=%s, attempts=%s, last_errcode=%s, last_errmsg=%s,
                          last_response=%s,
                          next_attempt_at = now() + make_interval(secs => %s),
                          updated_at=now()
                    WHERE outbox_id=%s""",
                ("dead" if dead else "failed", attempts, errcode, errmsg, _json(resp),
                 _delay(attempts), row["outbox_id"]))


def _defer(conn, row, seconds: int, note: str) -> None:
    """把任务延后（保持 pending）：回写被临时关闭时用，**不丢数据**。"""
    conn.execute(
        """UPDATE writeback_outbox
              SET status='pending', last_errmsg=%s,
                  next_attempt_at = now() + make_interval(secs => %s),
                  updated_at=now()
            WHERE outbox_id=%s""",
        (note, int(seconds), row["outbox_id"]))


def drain(limit: int = 50) -> dict:
    """排空到期任务。**实时**读取回写模式作为安全闸门。

    网络调用刻意放在事务**之外**（避免持锁做 I/O）：先抢占 → 逐个投递 →
    再开短事务落状态。单实例调度下不会重复投递。
    """
    import weiban
    from scoring_service import service

    stats = {"checked": 0, "ok": 0, "failed": 0, "skipped": 0, "dead": 0, "deferred": 0}
    try:
        mode = service.writeback_mode()
    except Exception:
        mode = "off"

    with db.tx() as conn:
        rows = _claim(conn, limit)

    for row in rows:
        stats["checked"] += 1
        # --- 闸门：模式在排空时刻实时判定 ---
        if mode == "off":
            # 关闭 = 暂停，不是作废：保持 pending 延后重试，避免又变成一次「永久丢失」
            with db.tx() as conn:
                _defer(conn, row, OFF_DEFER_SECONDS, "回写已关闭，暂缓投递")
            stats["deferred"] += 1
            continue
        if mode == "dry_run":
            # 演练 = 只记账不外写，记完即完成
            with db.tx() as conn:
                _record(conn, row, "dry_run", True, int(row["attempts"]),
                        errmsg="演练模式：仅记录，未真实写入")
            stats["skipped"] += 1
            continue

        # --- live：真实投递（事务外） ---
        attempts = int(row["attempts"]) + 1
        errcode = errmsg = None
        resp = None
        ok = False
        try:
            resp = weiban.add_customer_score(row["staff_id"], external_user_id=row["target_id"],
                                             score=int(row["score"]), use_header_token=True)
            errcode = resp.get("errcode")
            errmsg = resp.get("errmsg")
            ok = errcode == 0
        except Exception as e:
            errmsg = f"{type(e).__name__}: {e}"
        dead = (not ok) and attempts >= int(row["max_attempts"])

        with db.tx() as conn:
            _record(conn, row, "ok" if ok else "failed", ok, attempts,
                    errcode, errmsg, resp, dead=dead)
        if ok:
            stats["ok"] += 1
        elif dead:
            stats["failed"] += 1
            stats["dead"] += 1
        else:
            stats["failed"] += 1

    return stats
