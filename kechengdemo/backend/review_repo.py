# -*- coding: utf-8 -*-
"""`negative_review`（负分人工复核队列）的**唯一写入口**。

改造前这张表被 3 个模块直接写：

  · `scoring_service._enqueue_review` —— 建单（`pending`）+ 把评估标 `na`/`pending`；
  · `api_review` —— 提交（人工项 / 合计 / 备注 / 状态）、状态流转、批量改状态；
  · `meta_loop` —— 回流后回填 `eval_case_id`（可追溯「这条复核进了哪个样本」）。

三处的 SQL 各写各的，没有一处能回答「这条队列记录的一生由谁改」。现在收在这里。
"""
import db


def enqueue(*, rel_key: str, aid: int, cand: dict, ai_negative_total: int,
            ai_negative_items: list, raw_score: int, grade: str) -> None:
    """建一条待人工打分的复核单（`ON CONFLICT DO NOTHING`：一次评估只建一次）。

    ⚠️ `ai_negative_items` 存的是**AI 的原始扣分项**，人工结论写在
    `human_negative_items`（两个字段语义不同，不要互相覆盖 —— 否则
    「AI 判错什么」再也查不出来，误报率就没法算）。
    """
    db.execute(
        """INSERT INTO negative_review
             (relationship_key, assessment_id, staff_id, staff_name, target_id,
              target_name, session_id, ai_negative_total, ai_negative_items,
              status, raw_score, grade, last_msg_time)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s)
           ON CONFLICT (assessment_id) DO NOTHING""",
        (rel_key, aid, cand["staff_id"], cand.get("staff_name"), cand["target_id"],
         cand.get("target_name"), cand["session_id"], ai_negative_total,
         db.as_json(ai_negative_items), raw_score, grade, cand.get("last_msg_time")))


def submit(rid: int, *, human_items: list, human_total: int, note: str,
           reviewer: str, conn=None) -> int:
    """人工提交打分：落人工项 / 合计 / 备注 / 处理人，状态置 `done`。

    ``conn``：由调用方传入事务连接（复核提交要「队列 done + 结论修正」原子落库，
    否则中途崩溃会出现「队列说做完了、结论还是 pending」）。
    """
    _run = conn.execute if conn is not None else db.execute
    return _run(
        """UPDATE negative_review SET human_negative_items=%s, human_negative_total=%s,
               human_note=%s, reviewer=%s, status='done', reviewed_at=now(), updated_at=now()
             WHERE id=%s""",
        (db.as_json(human_items), human_total, note, reviewer, rid))


def set_status(rid: int, *, status: str, reviewer: str, note: str,
               reviewed: bool) -> int:
    """状态流转（`pending` / `done` / `ignored`）。

    `reviewed` 决定 `reviewed_at` 写 `now()` 还是 `NULL` —— 原先这行 SQL 是
    **f-string 拼 `now()`/`NULL`** 进语句里的；这里改成布尔参数由本函数决定，
    调用方无法再往 SQL 里塞片段。
    """
    reviewed_sql = "now()" if reviewed else "NULL"
    return db.execute(
        f"""UPDATE negative_review SET status=%s, reviewer=%s,
                reviewed_at={reviewed_sql}, updated_at=now(),
                human_note = COALESCE(NULLIF(%s,''), human_note)
              WHERE id=%s""",
        (status, reviewer, note, rid))


def set_status_bulk(ids: list, *, status: str) -> int:
    """批量改状态（复核列表的「批量处理」）。"""
    return db.execute(
        "UPDATE negative_review SET status=%s, updated_at=now() WHERE id = ANY(%s)",
        (status, list(ids)))


def link_eval_case(rid: int, case_id: int) -> int:
    """回填 `eval_case_id`：这条复核被回流成了哪个评估样本（可追溯）。"""
    return db.execute("UPDATE negative_review SET eval_case_id=%s WHERE id=%s",
                      (case_id, rid))
