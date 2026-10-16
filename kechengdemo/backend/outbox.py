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
    # ⚠️ 这里与标签队列**刻意不同**：微伴评分是**累加、不可撤销**的，卡在 sending
    #    说明"请求发出去了但结果不知道" → 重投等于赌一把再多加一次分。所以置 dead
    #    并要求人工对账（`reconcile()`），而不是回 failed 自动重投。
    db.reclaim_stuck_sending(conn, "writeback_outbox", STUCK_SECONDS,
                             " [投递中断：结果未知，禁止自动重投，需人工对账]",
                             unknown_outcome=True)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT outbox_id, assessment_id, relationship_key, staff_id, target_id,
                      score, attempts, max_attempts
                 FROM writeback_outbox
                WHERE status IN ('pending', 'failed')
                  AND next_attempt_at <= now()
                  AND NOT needs_reconcile
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


def _unknown_outcome(err: BaseException) -> bool:
    """这次投递失败是否**结果未知**（请求可能已经到达微伴）。

    为什么必须区分（本函数的全部价值）：
    微伴「客户评分」是**累加**、没有覆盖/回滚接口。若请求其实已经写入、只是我们
    没拿到结果（超时、连接被重置、进程被杀），**重投就是多加一次分**，且事后无法
    撤销。所以只有"对端明确回话"的失败才允许自动重投。

    判据（保守优先——拿不准就算未知）：
    · `HttpFailure` 带 `status`：对端明确给了 HTTP 状态码 → **明确失败**，可重投；
    · 业务 errcode（"接口返回错误 errcode="）：对端明确拒绝 → 明确失败，可重投；
    · 其余异常（网络错误/超时/断连/JSON 解析中途断）→ **结果未知**，禁止自动重投。
    """
    try:
        import http_client
        if isinstance(err, http_client.HttpFailure):
            if getattr(err, "status", None):
                return False
            return "接口返回错误 errcode=" not in str(err)
    except Exception:                                   # noqa: BLE001
        pass
    return True


def _policy_gate(row) -> str:
    """按策略判定决定这条待办怎么处置。返回 ``deliver`` / ``hold`` / ``review``。

    | 策略 | 处置 | 为什么 |
    |---|---|---|
    | `auto` | `deliver` | 允许自动写 |
    | `confirm` | `hold` | 等人工签字，批准即投递（审批队列） |
    | `human` | `review` | **不进自动链路**：待办置 dead，先完成复核/重评再重评出新结论 |

    「`human` 也发审批、批准就能写」与设计相矛盾（human 的定义是"只进人工复核"），
    而且会让低置信度的错判只要有人点一下就能写进微伴。现在它直接停投并说清原因。
    """
    try:
        import policy
        pa = (db.query_one(
            "SELECT policy_action FROM customer_intent_assessment WHERE assessment_id=%s",
            (row["assessment_id"],)) or {}).get("policy_action")
        if not pa or pa == policy.AUTO:
            return "deliver"
        if pa == policy.CONFIRM:
            appr = db.query_one(
                """SELECT status FROM approval_request
                    WHERE assessment_id = %s AND status IN ('approved','rejected')
                    ORDER BY request_id DESC LIMIT 1""", (row["assessment_id"],))
            if (appr or {}).get("status") == "approved":
                return "deliver"
            return "hold"
        return "review"                                  # human
    except Exception as e:                      # noqa: BLE001
        print(f"[outbox] 读取策略判定失败（按可投递处理）：{e}")
        return "deliver"


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
        gate = _policy_gate(row)
        if gate == "review":
            # `human`：不进自动链路 —— 直接停投（终态），并说清"下一步该做什么"，
            # 而不是让它挂在 pending 里等一个永远不会来的确认。
            with db.tx() as conn:
                _record(conn, row, "dead", False, int(row["attempts"]),
                        dead=True,
                        errmsg="策略判定为需人工复核：本条不自动写入。"
                               "请先完成复核/改判并重评该客户，新结论达标后会自动入队")
            stats["review_hold"] = stats.get("review_hold", 0) + 1
            continue
        if gate == "hold":
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
        unknown = False
        try:
            resp = weiban.add_customer_score(row["staff_id"], external_user_id=row["target_id"],
                                             score=int(row["score"]), use_header_token=True)
            errcode = resp.get("errcode")
            errmsg = resp.get("errmsg")
            ok = errcode == 0
        except Exception as e:
            errmsg = f"{type(e).__name__}: {e}"
            unknown = _unknown_outcome(e)

        if unknown and not ok:
            # **结果未知**（超时/断连/进程被杀）：请求可能已经写进微伴了。
            # 微伴评分累加且不可撤销 → **绝不自动重投**，置 dead + 待人工对账。
            with db.tx() as conn:
                _record(conn, row, "dead", False, attempts, errcode,
                        f"{errmsg}｜⚠️ 结果未知（请求可能已送达）：已停投，需人工对账",
                        resp, dead=True)
                conn.execute(
                    "UPDATE writeback_outbox SET needs_reconcile=true WHERE outbox_id=%s",
                    (row["outbox_id"],))
            stats["unknown"] = stats.get("unknown", 0) + 1
            continue

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


# ============================================================ 人工对账（结果未知的行）

def pending_reconcile(limit: int = 50) -> list:
    """列出**结果未知、已停投**的待办（等人工判定"到底写没写"）。"""
    return db.query(
        """SELECT o.outbox_id, o.assessment_id, o.relationship_key, o.staff_id,
                  o.target_id, o.score, o.attempts, o.last_errmsg, o.updated_at,
                  a.grade, a.target_name, a.policy_action
             FROM writeback_outbox o
             LEFT JOIN customer_intent_assessment a ON a.assessment_id = o.assessment_id
            WHERE o.needs_reconcile
            ORDER BY o.updated_at DESC LIMIT %s""", (max(1, min(int(limit or 50), 200)),))


def reconcile(assessment_id: int, *, written: bool, note: str = "") -> dict:
    """人工对账结论：这条"结果未知"的回写**到底写进微伴了没有**。

    为什么必须由人判定：微伴评分是累加、无覆盖接口，且**没有幂等键**——系统无法
    自己回答"我刚才那次调用生效了吗"。唯一可靠的判据是**读回客户当前累计分**
    （`weiban.list_external_users()`，这也正是它被文档称作"对账手段"的用途）。

    · ``written=True``  → 补记账：score_writeback 置 ok、**推进水位**（不推就会在下次
      重算差值时把这笔分再写一遍）、outbox 置 done、清 needs_reconcile；
    · ``written=False`` → 确认没写：outbox 退回 pending（允许重投），清 needs_reconcile。

    两种情况都写审计（谁判的、依据什么）。
    """
    row = db.query_one(
        """SELECT * FROM writeback_outbox WHERE assessment_id=%s""", (assessment_id,))
    if not row:
        return {"ok": False, "reason": f"待办不存在：assessment_id={assessment_id}"}
    if not row.get("needs_reconcile"):
        return {"ok": False, "reason": "该待办不在「待对账」状态，无需对账"}
    if written:
        with db.tx() as conn:
            _record(conn, row, "ok", True, int(row["attempts"]),
                    errmsg=f"人工对账：确认已写入｜{note}"[:300])
            conn.execute(
                "UPDATE writeback_outbox SET needs_reconcile=false WHERE outbox_id=%s",
                (row["outbox_id"],))
    else:
        db.execute(
            """UPDATE writeback_outbox
                  SET status='pending', needs_reconcile=false, next_attempt_at=now(),
                      last_errmsg=%s, updated_at=now()
                WHERE outbox_id=%s""",
            (f"人工对账：确认**未**写入，恢复投递｜{note}"[:300], row["outbox_id"]))
    import audit
    import security
    audit.record_action(security.actor(), "writeback_reconcile", f"assessment:{assessment_id}",
                        {"written": bool(written), "score": int(row["score"] or 0),
                         "relationship_key": row["relationship_key"], "note": note})
    return {"ok": True, "assessment_id": assessment_id, "written": bool(written),
            "score": int(row["score"] or 0)}
