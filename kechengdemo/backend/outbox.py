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

import db
import assessment_repo
import http_client                  # 全项目唯一的退避实现（Reuse Before Rewrite）
import writeback_repo                # 回写记账与水位唯一写入口（与评分内联路径共用）

BACKOFF_BASE = 30           # 秒；首次失败后的等待
BACKOFF_CAP = 3600          # 秒；退避上限
STUCK_SECONDS = 600         # sending 超过此时长视为「投递中断」，回收重试
OFF_DEFER_SECONDS = 600     # 回写被关闭时，任务延后这么久再看（不丢，不刷屏）


def _json(value):
    return db.as_json(value) if value is not None else None


def _delay(attempts: int) -> int:
    """第 attempts 次失败后的等待秒数。

    统一走 `http_client.backoff_seconds()` —— 原先本函数与 `grade_tag._delay()`
    是**逐字相同的两份**（指数 + ±30% 抖动 + 上限），改一处必然忘另一处。
    差异只靠 `base`/`cap` 参数表达：回写要等得更久（最长 1 小时）。

    抖动不会破坏「退避生效」这一不变量：最小值为 ``BACKOFF_BASE × 0.7``，
    仍严格大于 0（`test_outbox_drain` 断言 `next_attempt_at > updated_at`）。
    """
    return max(1, int(http_client.backoff_seconds(
        attempts - 1, BACKOFF_BASE, cap=BACKOFF_CAP)))


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
    db.reclaim_stuck_sending(conn, "writeback_outbox", STUCK_SECONDS,
                             " [投递中断，已回收]")
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
        # score_writeback：与评分内联路径**共用同一份 SQL**（`writeback_repo`）
        writeback_repo.record_delivery(
            conn, rel_key=row["relationship_key"], aid=row["assessment_id"],
            staff_id=row["staff_id"], target_id=row["target_id"], score=row["score"],
            status=status, attempts=attempts, errcode=errcode, errmsg=errmsg, resp=resp)
        # 与评分内联回写路径**共用同一个写入口**（这一列被两条路径写）
        assessment_repo.set_writeback_status(row["assessment_id"], status, conn=conn)
        # outbox 终态
        if ok:
            cur.execute(
                """UPDATE writeback_outbox
                      SET status='done', last_errcode=%s, last_errmsg=%s,
                          last_response=%s, updated_at=now()
                    WHERE outbox_id=%s""",
                (errcode, errmsg, _json(resp), row["outbox_id"]))
            # D2-1：投递成功才推进客户级回写水位（差值累加的依据）。
            # ⚠️ 必须与「写 score_writeback」在**同一事务**里 —— 否则出现
            # 「记账说写成功了、水位没动」→ 下次差值重复算 → 重复回写。
            writeback_repo.advance_ledger(row["relationship_key"], row["staff_id"],
                                          row["target_id"], row["assessment_id"],
                                          int(row["score"] or 0), conn=conn)
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


def _policy_hold(row) -> bool:
    """该评估是否被 Policy 判定为「**不能自动投递**」（需确认 / 需人工）。

    返回 True 表示：保持 pending 等待人工，**不要投递**。

    读的是 `customer_intent_assessment.policy_action` —— 与同步路径同一个判据来源，
    避免"同步一套、异步另一套"的两套真相。
    查不到（历史数据 / 列未写）时返回 False：**宁可照旧投递，也不要因为查不到就把
    待回写任务永久卡住**（这正是 G6 要避免的"永久丢失"的另一种形态）。

    **人工放行（2026-10-16 接线）**：`approval_request` 里同一 `assessment_id`
    已有 `approved` 的请求 → 返回 False（放行投递）。

    为什么必须补这一段：改造前这里是"挂起等人工"，但**没有任何地方能把人工的结论
    送回来** —— `approval_request` 只有测试在写。表现是：策略拦下的回写会**永远**
    躺在 pending 里，既没投递、也没人知道要去处理，等于静默丢弃（比投递失败更坏，
    因为 `score_writeback` 里连一行失败记录都没有）。

    驳回（`rejected`）**不算放行**：那是"人工明确说过不写"。此时若行还在 pending，
    由 `api_scoring` 的审批收尾把它置 `dead`（终态），不再反复延后。
    """
    try:
        import policy
        pa = (db.query_one(
            "SELECT policy_action FROM customer_intent_assessment WHERE assessment_id=%s",
            (row["assessment_id"],)) or {}).get("policy_action")
        if not (bool(pa) and pa != policy.AUTO):
            return False
        appr = db.query_one(
            """SELECT status FROM approval_request
                WHERE assessment_id = %s AND status IN ('approved','rejected')
                ORDER BY request_id DESC LIMIT 1""", (row["assessment_id"],))
        return (appr or {}).get("status") != "approved"
    except Exception as e:                      # noqa: BLE001
        print(f"[outbox] 读取策略判定失败（按可投递处理）：{e}")
        return False


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

        # --- Policy 闸门（硬伤 4）：**必须和同步路径用同一把闸** ---
        # 目的：`_writeback` 里拦住"需确认/需人工"的评估，但 drain 是**另一条**投递路径；
        # 只拦同步不拦异步 = 策略是个摆设（自欺程度比没有更糟）。
        # 处置同 `off`：**延后而不是作废**（保持 pending，等人工确认后仍可投递），
        # 这样 G6「回写不会永久丢失」的不变量依然成立。
        if _policy_hold(row):
            # 挂起的同时**保证审批队列里有它**（幂等）：改造前只挂起不登记，
            # 于是"等人工确认"永远等不到 —— 没人知道该确认谁。
            try:
                import task_runtime
                task_runtime.request_writeback_approval(
                    assessment_id=row["assessment_id"],
                    relationship_key=row["relationship_key"], staff_id=row["staff_id"],
                    external_user_id=row["target_id"], score=int(row["score"]),
                    reason="异步投递时策略要求人工确认")
            except Exception as e:                  # noqa: BLE001
                print(f"[outbox] 登记回写审批失败（不影响延后）：{type(e).__name__}: {e}")
            with db.tx() as conn:
                _defer(conn, row, OFF_DEFER_SECONDS,
                       "策略判定为需人工确认，暂缓投递（未丢弃）")
            stats["deferred"] += 1
            stats["policy_hold"] = stats.get("policy_hold", 0) + 1
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
