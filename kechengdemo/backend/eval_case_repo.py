# -*- coding: utf-8 -*-
"""`eval_case`（评估样本/金标准）的**唯一写入口**。

改造前这张表被 3 个模块直接写：

  · `eval_runner` —— 抽样插入（`sample`）、AI 预标注（`prefill`）；
  · `api_eval` —— 人工标注、标争议、冻结/解冻、启用/停用；
  · `meta_loop` —— 把改判过的复核回流成样本（`E5`）。

三个写入方各有各的 SQL，于是「冻结后还能不能被改」这种**跨写入方的约束**
只能靠调用方自觉。收在这里之后，约束写在函数里（例如 `set_annotation`
不碰 `frozen`，冻结与否由 `api_eval` 的守卫决定 —— 那是业务规则，留在接口层）。
"""
import audit
import db


def insert_sample(*, kind: str, staff_id: str, session_id: str, target_id: str,
                  staff_name, target_name, session_name, messages_hash: str,
                  msg_count: int, last_msg_time,
                  baseline_items=None, baseline_raw_score=None, baseline_grade=None,
                  baseline_rule_version=None, baseline_prompt_version=None,
                  baseline_at=None, anchor_ts=None):
    """抽样插入一条样本（`source='sample'`）。返回新 `case_id`，冲突则返回 None。

    基线字段来自「抽样时系统判的那一份」，供后续对比 `人标 vs 系统判`。
    ``anchor_ts``（P2）：**跑分时用的时间锚点**，取 baseline 那次评估的时刻 ——
    这样"同一个样本换一天跑"结论不变（引擎项按锚点算，不按当前时间）。
    冲突目标是 `(kind, staff_id, session_id)` —— **同一会话在同一类样本里只留一条**。
    """
    row = db.query_one(
        """INSERT INTO eval_case
             (kind, staff_id, session_id, target_id, staff_name, target_name,
              session_name, messages_hash, msg_count, last_msg_time, source,
              baseline_items, baseline_raw_score, baseline_grade,
              baseline_rule_version, baseline_prompt_version, baseline_at, anchor_ts)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'sample',%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (kind, staff_id, session_id) DO NOTHING
           RETURNING case_id""",
        (kind, staff_id, session_id, target_id, staff_name, target_name,
         session_name, messages_hash, msg_count, last_msg_time,
         baseline_items, baseline_raw_score, baseline_grade,
         baseline_rule_version, baseline_prompt_version, baseline_at, anchor_ts))
    return (row or {}).get("case_id")


def insert_from_review(conn, *, kind: str, staff_id: str, session_id: str, target_id: str,
                       staff_name, target_name, messages_hash: str, msg_count: int,
                       last_msg_time, items: list, raw_score: int, grade: str) -> int:
    """把改判过的复核**回流**成样本（E5）：金标准取人工结论，`source='review'`。"""
    return conn.execute(
        """INSERT INTO eval_case
             (kind, staff_id, session_id, target_id, staff_name, target_name,
              messages_hash, msg_count, last_msg_time,
              expected_items, expected_raw_score, expected_grade, source, active)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'review',true)
           RETURNING case_id""",
        (kind, staff_id, session_id, target_id, staff_name, target_name,
         messages_hash, msg_count, last_msg_time,
         db.as_json(items), raw_score, grade)).fetchone()["case_id"]



# ============================================================ 写入门禁（P1）

#: 审计快照只留**判定相关**的字段，**不留正文**（聊天正文另有出处，审计不该复制一份）
_SNAP_FIELDS = ("grade", "prefilled", "frozen", "active", "disputed", "annotator")


def _row_brief(row: dict) -> dict:
    """把一行压成审计用的摘要：`{rule_id: score}` 集合 + 几个开关位（**不含正文**）。"""
    items = [{"rule_id": i.get("rule_id"), "score": int(i.get("score") or 0)}
             for i in db.as_dict_list(row.get("expected_items"))]
    brief = {"items": items, "expected_raw_score": row.get("expected_raw_score")}
    for f in _SNAP_FIELDS:
        brief[f] = row.get(f)
    return brief


def _write_with_audit(cid: int, *, action: str, actor: str,
                      set_sql: str, params: tuple, detail: dict = None,
                      where_extra: str = "", detail_from_old=None) -> int:
    """**同一事务**里：锁行 → 取旧值 → 更新 → 写审计（失败即回滚）。

    这是本模块所有写入的公共骨架，解决的是「改了但没留痕」这个**机制性**问题：

    · 改造前审计由**调用方**各自写，`eval_runner.prefill()` 里还带 `if total <= 30`
      的条件 —— 样本越多越不留痕（2026-10-15 有 21 行被整体替换，审计里查不到）；
    · 现在审计写在**写入方内部**，与 UPDATE 同事务：审计写不进去 → 整体回滚，
      "写不进去就不许改"（`audit.record_action_in` 刻意不吞异常）。

    返回受影响行数（0 = 样本不存在 / 条件不满足）。

    ``where_extra``：附加到 UPDATE 的 WHERE 上（如 `AND frozen = false`）——
    **约束写在 SQL 里而不是只靠调用方自觉**（与 `delete_case` 同一原则：
    任何未来的调用方都绕不过）。
    ``detail_from_old``：由旧行**推导**出来的审计字段（如"这次是不是覆盖了人工确认的内容"），
    比调用方自报更可靠。
    """
    with db.tx() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM eval_case WHERE case_id = %s FOR UPDATE", (cid,))
            old = cur.fetchone()
            if not old:
                return 0
            cur.execute(f"UPDATE eval_case SET {set_sql} "
                        f"WHERE case_id = %s {where_extra} RETURNING *", (*params, cid))
            new = cur.fetchone()
            if not new:
                return 0                      # 条件不满足 → 没改也没留痕（不留误导性审计）
            # 审计记 **before → after**（`after` 用 RETURNING 拿，不多一次查询）。
            # 只留判定相关字段：规则命中集 + 分数 + 几个开关位，**不留聊天正文**。
            payload = {"before": _row_brief(old), "after": _row_brief(new)}
            if detail_from_old:
                payload.update(detail_from_old(old) or {})
            payload.update(detail or {})
            audit.record_action_in(conn, actor, action, str(cid), payload)
    return 1


def set_annotation(cid: int, *, items: list, raw_score: int, grade: str,
                   annotator: str, note: str, disputed: bool,
                   detail: dict = None) -> int:
    """人工标注落库：**`prefilled` 置 false** —— 这一列是「是否仍为 AI 草稿」的开关，
    保存后就不再计入「待复核草稿」队列。

    ⚠️ 这一行曾经是**改了一半**的状态：`prefilled=false` 是 2026-10-15 13:06 才落盘的，
    而当天 07:30 保存的 30 条因此全部仍是草稿态，门禁的 `confirmed` 恒为 0
    （"确认过却等于没确认"）。`tests/test_eval_write_guard.py` 专门钉住这一行。
    """
    return _write_with_audit(
        cid, action="eval_annotate", actor=annotator, detail=detail,
        set_sql=("expected_items=%s, expected_raw_score=%s, expected_grade=%s, "
                 "annotator=%s, note=%s, disputed=%s, prefilled=false, updated_at=now()"),
        params=(db.as_json(items), raw_score, grade, annotator, note, disputed))


def mark_dispute(cid: int, note: str, *, actor: str = "system") -> int:
    """只打争议标记：**不动标注内容**（错误内容一旦以 prefilled=false 落库就会被
    当成金标准计入指标，比不标更糟）。"""
    return _write_with_audit(
        cid, action="eval_dispute", actor=actor,
        set_sql="disputed = true, note = COALESCE(NULLIF(%s, ''), note), updated_at = now()",
        params=(note,))


def set_frozen(cid: int, frozen: bool, *, actor: str = "system") -> int:
    """冻结 / 解冻（金标准一经冻结，除仲裁外不得修改）。"""
    return _write_with_audit(
        cid, action="eval_freeze", actor=actor,
        set_sql="frozen=%s, updated_at=now()", params=(frozen,),
        detail={"frozen": frozen})


def set_active(cid: int, active: bool, *, actor: str = "system") -> int:
    """启用 / 停用（停用的样本不参与指标）。"""
    return _write_with_audit(
        cid, action="eval_active", actor=actor,
        set_sql="active=%s, updated_at=now()", params=(active,),
        detail={"active": active})


def set_prefill(cid: int, *, items: list, raw_score: int, grade: str,
                annotator: str, actor: str = "system") -> int:
    """AI 预标注：填入草稿并**置 `prefilled=true`**（复核保存后才会转 false）。

    **冻结的样本写不进去**（`AND frozen = false` 写在 SQL 里）—— 改造前 `prefill`
    完全不看 `frozen`，只有 `/save` 拦得住。

    审计里的 ``overwrote_annotated`` 由**旧行推导**（旧值是 `prefilled=false`
    且已有内容 = 这次冲掉的是人工确认过的），不靠调用方自报。
    """
    return _write_with_audit(
        cid, action="eval_prefill", actor=actor,
        set_sql=("expected_items=%s, expected_raw_score=%s, expected_grade=%s, "
                 "prefilled=true, annotator=%s, updated_at=now()"),
        params=(db.as_json(items), raw_score, grade, annotator),
        where_extra="AND frozen = false",
        detail_from_old=lambda old: {
            "overwrote_annotated": bool(old.get("expected_items") is not None
                                        and old.get("prefilled") is False)})


def delete_case(cid: int, *, actor: str = "system") -> int:
    """删除样本。**已冻结的不许删**（金标准一经冻结，除仲裁外不得修改 —— 与冻结同一道闸）。

    条件写在 SQL 里而不是只靠接口层判断：任何未来的调用方都绕不过这条约束。
    审计在**删之前**取快照（删完就查不到了）。
    """
    with db.tx() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM eval_case WHERE case_id = %s FOR UPDATE", (cid,))
            old = cur.fetchone()
            if not old:
                return 0
            cur.execute("DELETE FROM eval_case WHERE case_id = %s AND frozen = false", (cid,))
            n = cur.rowcount
            if not n:
                return 0                      # 已冻结 → 不删（也不留误导性审计）
            audit.record_action_in(conn, actor, "eval_delete", str(cid),
                                   {"before": _row_brief(old)})
    return n
