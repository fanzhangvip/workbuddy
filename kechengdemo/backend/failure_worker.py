# -*- coding: utf-8 -*-
"""单会话失败的重投 worker（架构 §2.2 G5「连续失败 ≥N 转人工」）。

## 解决什么

`session_failure`（阶段A）已经能记录「哪个会话失败了、失败几次」，
但**记录之后没人处理**：失败明细躺在表里，会话就一直没被评过。
文档里这条护栏（G5）当时的状态是「明细已齐备，重投 worker 尚未做」。

## 状态机

```
                ┌────────── 失败一次 ──────────┐
   （新失败）→ pending ──重投成功──→ resolved
                 │  ↑
                 │  └── 重投失败但未达阈值 → pending（退避后重试）
                 └── attempts ≥ session_max_failures → dead + manual_required
```

· 重投用 ``service.score_session``（单会话、绕过增量水位）——
  因为水位可能已推进，走候选筛选会直接跳过它，那样重投永远不会发生
· **退避**：`RETRY_BACKOFF_MINUTES * 2^(attempts-1)`，避免快速空转
· **转人工**：达阈值标 `dead` 且 `manual_required=true`，由复核页接手

## 与 outbox 的区别

`writeback_outbox` 重试的是「**回写微伴**」这一件事（评分已完成）；
本 worker 重试的是「**评分本身**」。两者互不替代：
评分失败时根本没有 assessment，也就无从回写。
"""

import audit
import db

#: 单次重投的最大条数（防止一轮把后端占满）
DEFAULT_LIMIT = 20

#: 首次退避基数（分钟）；后续指数增长
BACKOFF_BASE_MINUTES = 15


def _max_attempts() -> int:
    return max(1, db.setting_int("session_max_failures", "SESSION_MAX_FAILURES", 3))


def ensure_columns() -> None:
    """补 worker 需要的列（幂等）。

    ⚠️ 这些列/索引的**权威定义在 `schema.sql`**（新库由 `init_db()` 一次建全）。
    这里保留一份是为了兼容「建表后才加列」的老库 —— 属于兼容兜底，
    新增字段请先改 `schema.sql`，再决定是否需要在这里补。
    """
    for ddl in (
        "ALTER TABLE session_failure ADD COLUMN IF NOT EXISTS manual_required BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE session_failure ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ",
        "ALTER TABLE session_failure ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ",
        "ALTER TABLE session_failure ADD COLUMN IF NOT EXISTS resolution TEXT",  # auto / manual
        "CREATE INDEX IF NOT EXISTS ix_session_failure_manual ON session_failure (manual_required) WHERE manual_required",
    ):
        db.execute(ddl)


def due(limit: int = DEFAULT_LIMIT) -> list:
    """到期待重投的失败明细（`pending` 且退避时间已到）。"""
    ensure_columns()
    return db.query(
        """SELECT failure_id, staff_id, session_id, attempts, error_class
             FROM session_failure
            WHERE status = 'pending'
              AND (next_retry_at IS NULL OR next_retry_at <= now())
            ORDER BY failure_id
            LIMIT %s""", (int(limit),))


def mark_manual(limit: int = 200) -> int:
    """把已达阈值的 ``dead`` 明细标为「需人工」并返回条数。

    ⚠️ PostgreSQL 的 ``UPDATE ... LIMIT`` **不合法**（那是 MySQL 语法），
    必须用 ``ctid IN (SELECT ... LIMIT n)`` 的子查询形式（踩到过）。
    """
    ensure_columns()
    return db.execute(
        """UPDATE session_failure SET manual_required = true
            WHERE ctid IN (SELECT ctid FROM session_failure
                            WHERE status = 'dead' AND NOT manual_required
                            LIMIT %s)""", (int(limit),))


def drain(limit: int = DEFAULT_LIMIT, verbose: bool = True) -> dict:
    """重投到期的失败会话。返回统计。"""
    ensure_columns()
    import scoring_service as ss

    todo = due(limit)
    out = {"checked": len(todo), "resolved": 0, "still_failed": 0, "dead": 0}
    if not todo:
        # 没有待重投的也顺手把 dead 标人工（否则 dead 会一直不被标注）
        out["marked_manual"] = mark_manual()
        return out

    max_attempts = _max_attempts()
    for r in todo:
        fid, staff_id, session_id = r["failure_id"], r["staff_id"], r["session_id"]
        attempts = int(r.get("attempts") or 0)
        db.execute("UPDATE session_failure SET last_attempt_at = now() WHERE failure_id=%s",
                   (fid,))
        try:
            # ⚠️ 用 `score_session`（单会话、不走候选筛选）：增量水位可能已推进，
            # 走 `_candidates` 会把该会话直接跳过 → 重投永远不可能成功。
            res = ss.service.score_session(staff_id, session_id)
        except Exception as e:                          # noqa: BLE001
            res = {"ok": False, "reason": f"{type(e).__name__}: {e}"}

        if res.get("ok"):
            db.execute(
                """UPDATE session_failure SET status='resolved', resolved_at=now(),
                       resolution='auto' WHERE failure_id=%s""", (fid,))
            out["resolved"] += 1
            continue

        attempts += 1
        if attempts >= max_attempts:
            db.execute(
                """UPDATE session_failure SET attempts=%s, status='dead',
                       manual_required=true, error_message=%s,
                       next_retry_at=NULL WHERE failure_id=%s""",
                (attempts, str(res.get("reason"))[:2000], fid))
            out["dead"] += 1
        else:
            minutes = BACKOFF_BASE_MINUTES * (2 ** max(0, attempts - 1))
            db.execute(
                """UPDATE session_failure SET attempts=%s, status='pending',
                       error_message=%s,
                       next_retry_at = now() + (%s || ' minutes')::interval
                     WHERE failure_id=%s""",
                (attempts, str(res.get("reason"))[:2000], str(int(minutes)), fid))
            out["still_failed"] += 1

    out["marked_manual"] = mark_manual()
    if verbose and (out["resolved"] or out["still_failed"] or out["dead"]):
        print(f"[failure-worker] {out}")
    return out


def resolve_manually(failure_id: int, note: str = "", actor: str = "manual") -> dict:
    """人工标记某条失败已处理（重投多次仍失败的兜底出口）。"""
    ensure_columns()
    n = db.execute(
        """UPDATE session_failure SET status='resolved', resolved_at=now(),
               resolution='manual', manual_required=false,
               error_message = COALESCE(error_message,'') || %s
             WHERE failure_id=%s""",
        (f"\n[人工] {note}" if note else "", int(failure_id)))
    audit.record_action(actor, "failure_resolve", f"session_failure:{failure_id}",
                        {"note": note})
    return {"ok": bool(n), "updated": n}


def stats() -> dict:
    ensure_columns()
    rows = db.query("SELECT status, count(*) AS n FROM session_failure GROUP BY status")
    by = {r["status"]: r["n"] for r in rows}
    man = db.query_one("SELECT count(*) AS n FROM session_failure WHERE manual_required")
    return {"by_status": by, "manual_required": (man or {}).get("n", 0),
            "max_attempts": _max_attempts(),
            "backoff_base_minutes": BACKOFF_BASE_MINUTES}
