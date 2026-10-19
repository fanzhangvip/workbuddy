# -*- coding: utf-8 -*-
"""`customer_intent_assessment`（评估结果表）的**唯一读写入口**。

## 为什么要有它

改造前这张表被 **4 个模块**直接写（`scoring_service` / `outbox` / `api_review` /
`backfill`），17 处 SQL 散在四处、各写各的列：

  · `writeback_status` 被**两条回写路径**写（评分内联 + outbox 异步排空）；
  · `review_status` 被评分（`na` / `pending`）与复核接口（`done` / `pending` / …）写；
  · `is_current` / `superseded_by` 被评分（落库时取代旧行）与自愈函数写。

没有单一写入者的后果很具体：改一列语义要翻 4 个文件；而且**「谁能改哪些列」
没有任何一处写清楚** —— 这正是「回写状态被两条路径写」这类隐患的温床。

现在按**列组**给出唯一入口（函数名即语义，`set_writeback_status` 不碰 `review_status`）。

## 约定

· 需要在**调用方事务里**执行的语句，签名带 `conn`（`conn` 由调用方的 `db.tx()` 给）；
  其余走 `db.execute` 自带连接。
· INSERT 仍然只在 `scoring_service._persist_assessment` 里（**单写入者**，未搬）——
  本模块管的是那些被多方写的列。
"""
import db


# ---------------- 读 ----------------

def last_assessment(rel_key: str) -> dict:
    """该客户**最近一次**评估（② 段记忆 + ① 段"本次是否提及"的数据源）。无记录 → 空 dict。

    与 `customer_memory` 的分工：① 段读**跨次稳定事实**（`memory_repo.active_rows`），
    ② 段读这里的"上次判了什么"。`profile_struct` 一并取出，是为了在 ① 里标注
    "这条事实上次会话没提到"（记忆不因此失效，只是提示模型别当成新信息）。
    """
    row = db.query_one(
        """SELECT assessment_id, raw_score, positive_total, negative_total, grade,
                  positive_items, negative_items, profile_summary, sales_strategy,
                  profile_struct, msg_count, last_msg_time, rule_version, analyzed_at
             FROM customer_intent_assessment
            WHERE relationship_key = %s
            ORDER BY assessment_id DESC LIMIT 1""",
        (rel_key,))
    return row or {}


# ---------------- 写：E2「当前有效」语义 ----------------

def supersede_previous(conn, rel_key: str, aid: int) -> None:
    """把同一客户的旧记录标记为被取代（**必须在写新行的同一事务里**调用）。

    为什么必须同事务：若先插入再单独更新，中途崩溃会留下**两条 `is_current=true`**
    → 列表出现重复行，正是 E2 要消除的问题。
    """
    conn.execute(
        """UPDATE customer_intent_assessment
              SET is_current = false, superseded_by = %s
            WHERE relationship_key = %s AND assessment_id < %s AND is_current""",
        (aid, rel_key, aid))


def repair_current_flags() -> dict:
    """自愈 `is_current` 的两种破损（**幂等**，可反复调用）。

    背景（E2 的配套缺口）：落库时「新评估把同客户旧行标记为被取代」**只发生在插入那一刻**。
    若那个新行**之后被删除**（测试清理、人工删数据），旧行的 `is_current` 就再也回不来
    → 该客户在评分列表（默认过滤 `is_current`）里**彻底消失**。实测已出现 1 例。

    两种破损：
      ① 某客户**一行 current 都没有** → 把 `assessment_id` 最大的那行恢复为 current
      ② `superseded_by` 指向**已不存在的行** → 清空（悬空引用）

    ⚠️ ① 里那个 `AND NOT EXISTS (… c.is_current)` **不能省**：少了它，当某客户
    已经有一条 current 行时会把「最大 id 的那行」也置成 current → **两条 current**
    （列表出现重复行，正是 E2 要消除的问题）。
    """
    fixed = db.execute(
        """UPDATE customer_intent_assessment a
              SET is_current = true, superseded_by = NULL
            WHERE NOT a.is_current
              AND a.assessment_id = (
                    SELECT max(b.assessment_id) FROM customer_intent_assessment b
                     WHERE b.relationship_key = a.relationship_key)
              AND NOT EXISTS (
                    SELECT 1 FROM customer_intent_assessment c
                     WHERE c.relationship_key = a.relationship_key AND c.is_current)""")
    dangling = db.execute(
        """UPDATE customer_intent_assessment a
              SET superseded_by = NULL
            WHERE a.superseded_by IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM customer_intent_assessment b
                               WHERE b.assessment_id = a.superseded_by)""")
    return {"no_current_fixed": fixed, "dangling_cleared": dangling}


# ---------------- 写：回写状态（评分内联 + outbox 两条路径共用） ----------------

def set_writeback_status(aid: int, status: str, *, conn=None) -> int:
    """回写状态。**两条回写路径（评分内联 / outbox 排空）都走这里**，
    避免一边改了取值、另一边还在写旧值。"""
    if conn is not None:                               # 调用方在事务里（outbox 的排空）
        return conn.execute(
            "UPDATE customer_intent_assessment SET writeback_status=%s"
            " WHERE assessment_id=%s", (status, aid)).rowcount
    return db.execute(
        "UPDATE customer_intent_assessment SET writeback_status=%s"
        " WHERE assessment_id=%s", (status, aid))


# ---------------- 写：复核状态（评分建单 + 复核接口流转） ----------------

def set_review_status(aid: int, status: str) -> int:
    """复核状态（`na` = 无负分不必复核 / `pending` 待打分 / `done` 已完成 / …）。"""
    return db.execute(
        "UPDATE customer_intent_assessment SET review_status=%s WHERE assessment_id=%s",
        (status, aid))


def mark_review_done(aid: int, verification_status: str, *, conn=None) -> int:
    """人工复核提交：状态置 `done` + 落**人工验证状态**（E4）。

    `verification_status` 三种语义必须分开，不能合成一个布尔：
      · `verified`  人工确认且与模型一致
      · `corrected` 人工改判过（meta-loop 的误报样本来源）
      · `rejected`  人工认为不该扣分

    ``conn``：复核提交要「队列 done + 结论状态 + 分数修正」三处**原子**落库，
    否则中途崩溃会留下「队列说做完了、结论仍 pending」这种可读但矛盾的状态。
    """
    sql = ("""UPDATE customer_intent_assessment
                 SET review_status='done', verification_status=%s WHERE assessment_id=%s""",
           (verification_status, aid))
    if conn is not None:
        return conn.execute(*sql)
    return db.execute(*sql)


# ---------------- 写：历史回填（backfill / rollback 专用） ----------------

def apply_backfill(conn, aid: int, *, session_confidence, review_reasons,
                   needs_human_review, positive_items, negative_items) -> None:
    """回填置信度与复核语义（`backfill.run` / `backfill.rollback` 共用同一组列）。

    两处原先各写一份 SQL：一旦列清单改了，只改一处就会让「回填」与「回滚」
    写回不同的列 —— 回滚对不上，等于没有备份。
    """
    conn.execute(
        """UPDATE customer_intent_assessment
              SET session_confidence=%s, review_reasons=%s,
                  needs_human_review=%s, positive_items=%s, negative_items=%s
            WHERE assessment_id=%s""",
        (session_confidence, review_reasons, needs_human_review,
         positive_items, negative_items, aid))


def set_review_semantics(conn, aid: int, *, needs_human_review, review_reasons) -> None:
    """只修「需复核 / 原因」两列（backfill 的 flag_only 分支：不可算行的自洽性修复）。"""
    conn.execute(
        """UPDATE customer_intent_assessment
              SET needs_human_review=%s, review_reasons=%s
            WHERE assessment_id=%s""",
        (needs_human_review, review_reasons, aid))


def apply_human_correction(aid: int, *, human_negative_total: int,
                           human_items: list = None, actor: str = "manual",
                           conn=None) -> dict:
    """把**人工复核的改判**落到这条结论上：重算扣分合计、总分与等级（可审计）。

    ## 为什么需要它

    复核提交原来只写 `negative_review`（人工扣分）+ `assessment.verification_status`
    （verified/corrected/rejected），**结论行的 raw_score / grade 一个字都不改**。
    于是"人工改判"只对以后的规则有用（回流成评估样本），对这个客户**毫无纠正**：
    微伴上的分数与等级标签仍然是 AI 的错判。复核的价值有一半没落地。

    ## 边界（刻意如此）

    · **纯本地确定性重算**，不重跑模型、不碰外部系统：`raw = positive_total + 人工负分`，
      再按 `rules.json` 的上下限截断、按 `grade_thresholds` 定级；
    · **不动** `negative_items`（那是**AI 当时的判定**，误报率/误报排行要靠 ai↔human
      的差值算）——人工结论存在 `negative_review.human_negative_items`；
    · **不动** `last_msg_time` / `rule_version`（否则会触发增量重评，把一个"修正动作"
      变成"重新推理"）；
    · **不动回写水位与账本**：`positive_total` 不变 → 下次差值不变 → 不会重复累加。
      已写进微伴的分数不会被撤回（微伴无覆盖接口），修正后由下一轮同步/对齐收敛。
    """
    row = db.query_one(
        """SELECT assessment_id, relationship_key, positive_total, raw_score, grade,
                  negative_total, is_current
             FROM customer_intent_assessment WHERE assessment_id=%s""", (aid,))
    if not row:
        return {"ok": False, "reason": f"结论不存在：{aid}"}
    import rules as rules_mod
    r = rules_mod.load_rules()
    lo = int((r.get("score_bounds") or {}).get("min", -999))
    hi = int((r.get("score_bounds") or {}).get("max", 999))
    neg = int(human_negative_total or 0)
    new_raw = max(lo, min(hi, int(row["positive_total"] or 0) + neg))
    new_grade = (rules_mod.grade_of(new_raw, r) or {}).get("grade") or "D"
    before = {"raw_score": row["raw_score"], "grade": row["grade"],
              "negative_total": row["negative_total"]}
    sql = """UPDATE customer_intent_assessment
                SET raw_score=%s, grade=%s, negative_total=%s
              WHERE assessment_id=%s"""
    params = (new_raw, new_grade, neg, aid)
    if conn is not None:
        conn.execute(sql, params)
    else:
        db.execute(sql, params)
    return {"ok": True, "assessment_id": aid,
            "before": before,
            "after": {"raw_score": new_raw, "grade": new_grade, "negative_total": neg},
            "changed": bool(before["raw_score"] != new_raw or before["grade"] != new_grade),
            "actor": actor, "human_items": human_items or []}
