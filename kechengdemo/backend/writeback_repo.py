# -*- coding: utf-8 -*-
"""回写的**记账**与**水位**：唯一写入口（`score_writeback` / `writeback_ledger`）。

## 为什么要有它

回写有**两条路径**：

  · **评分内联**（`scoring_service._writeback`）：会话评分当下试一次投递；
  · **outbox 排空**（`outbox.drain`）：失败后按退避异步重投。

两条路径都要做同样两件事 —— 记一笔 `score_writeback`、推进 `writeback_ledger` 水位。
改造前这两件事的 SQL 在两边**各写一份**，其中水位 upsert 甚至**逐字相同**：

```
scoring_service._advance_ledger  ←→  outbox._advance_ledger     （两份一模一样的 SQL）
scoring_service._writeback 的 INSERT  ←→  outbox._record 的 upsert
```

这不是「看起来像」的重复，而是**有业务后果**的重复：水位是「微伴那边已经累加了多少分」
的镜像，两边一旦漂移（改了一处、忘另一处），结果就是**客户分数被重复累加或少写**
（水位虚高 → 差值变 0 → 分数永远补不上）。

## 两个记账函数的冲突语义**故意不同**（写在这里以免将来有人"统一"掉）

· `record_attempt`（评分内联）：`ON CONFLICT DO NOTHING` —— 它是**尝试记录**，
  真正的权威记录由 outbox 投递后写；先到的不该被后到的覆盖。
· `record_delivery`（outbox）：`ON CONFLICT DO UPDATE` —— 它是**权威交付记录**，
  每次重投都要把最新状态（attempts / errcode / response）写上去。
"""
import db


# ---------------- 记账（score_writeback） ----------------

def record_attempt(rel_key, aid, staff_id, target_id, score, mode, status,
                   errcode=None, errmsg=None, resp=None) -> int:
    """评分内联路径记一笔（**DO NOTHING**：权威记录留给 outbox）。"""
    return db.execute(
        """INSERT INTO score_writeback
             (relationship_key, assessment_id, staff_id, external_user_id, score,
              mode, status, attempts, errcode, errmsg, response)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (assessment_id) DO NOTHING""",
        (rel_key, aid, staff_id, target_id, score, mode, status,
         1 if status in ("ok", "failed") else 0, errcode, errmsg,
         db.as_json(resp) if resp else None))


def record_delivery(conn, *, rel_key, aid, staff_id, target_id, score, status,
                    attempts, errcode=None, errmsg=None, resp=None) -> None:
    """outbox 投递后记一笔（**DO UPDATE**：它才是权威交付记录）。"""
    conn.execute(
        """INSERT INTO score_writeback
             (relationship_key, assessment_id, staff_id, external_user_id, score,
              mode, status, attempts, errcode, errmsg, response)
           VALUES (%s,%s,%s,%s,%s,'live',%s,%s,%s,%s,%s)
           ON CONFLICT (assessment_id) DO UPDATE
              SET status=EXCLUDED.status, attempts=EXCLUDED.attempts,
                  errcode=EXCLUDED.errcode, errmsg=EXCLUDED.errmsg,
                  response=EXCLUDED.response, updated_at=now()""",
        (rel_key, aid, staff_id, target_id, score, status, attempts, errcode, errmsg,
         db.as_json(resp) if resp else None))


# ---------------- 水位（writeback_ledger，D2-1） ----------------

def advance_ledger(rel_key: str, staff_id: str, target_id: str, aid: int, delta: int,
                   *, conn=None) -> None:
    """回写成功后推进客户级水位（差值累加的依据）。

    ⚠️ 必须**只加已成功投递的增量**：水位是「微伴那边已经累加了多少分」的镜像，
    多写会少回写（水位虚高 → 下次差值变 0 → 分数永远补不上），
    少写会重复回写。两种错都会真实影响客户分数。

    ⚠️ 在 outbox 里调用时必须传 `conn`（与「写 score_writeback」同一事务）——
    否则会出现「记账说写成功了、水位没动」→ 下次差值重复算 → 重复回写。
    """
    sql = """INSERT INTO writeback_ledger
                 (relationship_key, staff_id, external_user_id, written_total,
                  last_assessment_id, last_write_at, writes)
               VALUES (%s,%s,%s,%s,%s,now(),1)
               ON CONFLICT (relationship_key) DO UPDATE
                 SET written_total = writeback_ledger.written_total + EXCLUDED.written_total,
                     last_assessment_id = EXCLUDED.last_assessment_id,
                     last_write_at = now(),
                     writes = writeback_ledger.writes + 1,
                     updated_at = now()"""
    params = (rel_key, staff_id, target_id, int(delta), aid)
    if conn is not None:
        conn.execute(sql, params)
    else:
        db.execute(sql, params)


def ledger_total(rel_key: str) -> int:
    """客户级已回写水位（D2-1）。不存在 / 查库失败 → 0（首次回写按整值算）。"""
    return db.query_int(
        "SELECT written_total AS n FROM writeback_ledger WHERE relationship_key=%s",
        (rel_key,))


def already_written(aid: int) -> bool:
    """该评估是否已成功回写过（`before_writeback` 的幂等闸）。

    为什么放在逻辑层而不只靠 `UNIQUE(assessment_id)`：数据库约束只保证「不重复记账」，
    **挡不住重复调用微伴接口** —— 微伴客户评分是累加的，重复投递会真的多加分。
    失败默认 False（宁可多投一次，也不要因为查库抖动而漏投）。
    """
    try:
        return bool(db.query_one(
            "SELECT 1 AS x FROM score_writeback WHERE assessment_id=%s AND status='ok'",
            (aid,)))
    except Exception:                                  # noqa: BLE001
        return False


def writes_today(target_id: str) -> int:
    """该客户今日已成功回写次数（`before_writeback` 的日配额闸，边界3）。"""
    return db.query_int(
        """SELECT count(*) AS n FROM score_writeback
            WHERE external_user_id=%s AND status='ok'
              AND created_at >= date_trunc('day', now())""", (target_id,))
