# -*- coding: utf-8 -*-
"""`interest_scan`（兴趣点提取结果）的**唯一写入口** + 分布聚合查询。

## 为什么要有它

与 `assessment_repo` / `review_repo` / `eval_case_repo` 同一个理由：一张表只能有一个
写入口，否则"这条数据是谁写的、什么时候写的、水位推到哪了"永远说不清。批量服务写、
接口只读 —— 读写两侧的 SQL 都收在这里。

## 聚合口径（图上每个数字的含义）

· **`customers`** —— `count(DISTINCT relationship_key)`：多少**客户**关心这件事。
  同一个客户在多个会话里反复提到同一兴趣，只算一次（这是产品选定口径）。
· **`mentions`** —— 兴趣点条数（提及次数）：被聊了多少次，用来区分"很多人偶尔问"
  与"少数人反复问"。默认排序按 `customers`，`mentions` 作为第二指标展示。
"""
import db


#: 「这个会话里客户说了几句（可读的）」——
#: 候选筛选与覆盖度**共用同一段 SQL**：口径只有一处，不会出现"候选说够、覆盖度说不够"。
#:
#: 为什么按**客户发言条数**筛，而不是总会话消息数：实测 685 个有正文的会话里，
#: 员工发出 28,727 条、客户只回了 5,731 条（中位 4 条），其中 **42 个会话客户一句话
#: 都没说**。兴趣点提取只看客户自己说的话（见 `scoring_context.build_context` 的
#: `customer_only`），所以"员工刷屏"不算语料；不设这道闸，那 42 个会话会每轮都被选中、
#: 每轮都因为没有语料而跳过，水位永远推不动（白烧调用且永远显示"还剩 N 个待分析"）。
#: `direction IS DISTINCT FROM 'send'` 与 `ctx_preprocess.role_of` 的判定一致
#: （非 send 即客户，含 direction 为空的历史脏数据）。
_CUSTOMER_MSGS = """(SELECT count(*) FROM chat_message m
                      WHERE m.staff_id = c.staff_id AND m.session_id = c.session_id
                        AND m.content_state = 'ok'
                        AND m.direction IS DISTINCT FROM 'send')"""


# ============================================================ 写（批量服务专用）

def upsert_scan(*, staff_id: str, session_id: str, relationship_key: str, staff_name: str,
                target_id: str, target_name: str, interests: list, summary: str,
                taxonomy_version: str, prompt_version: str, model_name: str,
                msg_count: int, last_msg_time: int) -> None:
    """按 `(staff_id, session_id)` 覆盖写入一次提取结果（含水位）。

    水位用 `GREATEST` 只增不减：重跑同一会话时若消息没变，水位不动（避免把
    "已分析到哪"往回退 —— 那样会让同一条会话被反复重跑）。
    """
    db.execute(
        """INSERT INTO interest_scan
             (staff_id, session_id, relationship_key, staff_name, target_id, target_name,
              interests, summary, taxonomy_version, prompt_version, model_name,
              msg_count, last_msg_time, analyzed_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,now())
           ON CONFLICT (staff_id, session_id) DO UPDATE
             SET relationship_key = EXCLUDED.relationship_key,
                 staff_name = EXCLUDED.staff_name,
                 target_id = EXCLUDED.target_id,
                 target_name = EXCLUDED.target_name,
                 interests = EXCLUDED.interests,
                 summary = EXCLUDED.summary,
                 taxonomy_version = EXCLUDED.taxonomy_version,
                 prompt_version = EXCLUDED.prompt_version,
                 model_name = EXCLUDED.model_name,
                 msg_count = EXCLUDED.msg_count,
                 last_msg_time = GREATEST(interest_scan.last_msg_time,
                                          EXCLUDED.last_msg_time),
                 analyzed_at = now()""",
        (staff_id, session_id, relationship_key, staff_name, target_id, target_name,
         db.as_json(interests or []), summary, taxonomy_version, prompt_version, model_name,
         int(msg_count or 0), int(last_msg_time or 0)))


def delete_scan(staff_id: str, session_id: str) -> int:
    """删一条（测试清理 / 将来的人工重跑用）。"""
    return db.execute("DELETE FROM interest_scan WHERE staff_id=%s AND session_id=%s",
                      (staff_id, session_id))


def scan_of(staff_id: str, session_id: str) -> dict:
    return db.query_one("SELECT * FROM interest_scan WHERE staff_id=%s AND session_id=%s",
                        (staff_id, session_id)) or {}


# ============================================================ 候选（增量）

def candidates(*, min_messages: int = 8, min_customer_msgs: int = 2,
               staff_ids: list = None, limit: int = 0) -> list:
    """待分析的会话：**客户说过话** 且 (没分析过 或 分析水位落后于会话最新时间)。

    两道语料闸（缺一不可）：
      · 总会话消息数 ≥ `min_messages`（便宜的前置过滤，滤掉零星的几轮问答）；
      · **客户自己说的话 ≥ `min_customer_msgs`**（见 `_CUSTOMER_MSGS` 的说明）。
    排序：先没分析过的，再按最新活跃倒序（结果稳定）。
    """
    params = {"min_msgs": int(min_messages or 0),
              "min_cust": max(int(min_customer_msgs or 0), 1)}
    staff_filter = ""
    if staff_ids:
        staff_filter = "AND c.staff_id = ANY(%(staff_ids)s)"
        params["staff_ids"] = list(staff_ids)
    sql = f"""
        SELECT c.staff_id, c.session_id, c.target_id, c.name AS target_name,
               c.msg_count, c.last_msg_time, s.name AS staff_name,
               c.staff_id || ':' || c.target_id AS relationship_key,
               (sc.staff_id IS NOT NULL) AS analyzed
          FROM chat_session c
          LEFT JOIN staff s ON s.staff_id = c.staff_id
          LEFT JOIN interest_scan sc ON sc.staff_id = c.staff_id
                                    AND sc.session_id = c.session_id
         WHERE c.session_type = 'ext_user'
           AND c.target_id IS NOT NULL
           AND c.msg_count >= %(min_msgs)s
           AND {_CUSTOMER_MSGS} >= %(min_cust)s
           AND (sc.staff_id IS NULL OR sc.last_msg_time < c.last_msg_time)
           {staff_filter}
         ORDER BY (sc.staff_id IS NULL) DESC, c.last_msg_time DESC NULLS LAST,
                  c.session_id ASC
    """
    if limit:
        sql += " LIMIT %(limit)s"
        params["limit"] = int(limit)
    return db.query(sql, params)


def coverage(*, session_type: str = "ext_user", min_messages: int = 8,
             min_customer_msgs: int = 2) -> dict:
    """覆盖度：**可分析的会话**里分析了多少、还剩多少、涉及多少客户。

    ⚠️ 分母与 `candidates` 同口径（同一段 `_CUSTOMER_MSGS`）：否则"客户一句话没说过"
    的会话会永远算在"待分析"里，界面上那条进度条永远到不了 100%。
    """
    base = """FROM chat_session c
               WHERE c.session_type=%s AND c.target_id IS NOT NULL AND c.msg_count >= %s
                 AND """ + _CUSTOMER_MSGS + """ >= %s"""
    params = (session_type, int(min_messages or 0), max(int(min_customer_msgs or 0), 1))
    total = db.query_one(f"SELECT count(*) AS n {base}", params) or {}
    pending = db.query_one(
        f"""SELECT count(*) AS n {base}
              AND NOT EXISTS (SELECT 1 FROM interest_scan sc
                               WHERE sc.staff_id=c.staff_id AND sc.session_id=c.session_id
                                 AND sc.last_msg_time >= c.last_msg_time)""", params) or {}
    done = db.query_one("SELECT count(*) AS n FROM interest_scan") or {}
    cust = db.query_one("SELECT count(DISTINCT relationship_key) AS n FROM interest_scan") or {}
    return {"sessions_available": int(total.get("n") or 0),
            "sessions_analyzed": int(done.get("n") or 0),
            "sessions_pending": int(pending.get("n") or 0),
            "customers": int(cust.get("n") or 0)}


# ============================================================ 读（分布图）

def _filter_sql(*, scope: str = "all", staff_id: str = "", alias: str = "t") -> tuple:
    """按等级 / 员工切片。

    等级取自**当前有效**的评估结论（客户级，与评分中心同口径）—— 分布图的价值一半在
    「S/A 客户关心什么 vs C/D 客户关心什么」，那是排选题优先级最直接的依据。
    """
    cond, params = ["1=1"], {}
    if staff_id:
        cond.append(f"{alias}.staff_id = %(staff)s")
        params["staff"] = staff_id
    if scope and scope != "all":
        cond.append("""EXISTS (SELECT 1 FROM customer_intent_assessment a
                                WHERE a.relationship_key = t.relationship_key
                                  AND a.is_current AND a.grade = %(grade)s)""")
        params["grade"] = scope
    return " AND ".join(cond), params


def distribution(*, scope: str = "all", staff_id: str = "", top: int = 8,
                 secondary_top: int = 40) -> dict:
    """一级分类分布 + 二级标签 + 等级分布 + 覆盖面（图上全部数字都出自这里）。"""
    where, params = _filter_sql(scope=scope, staff_id=staff_id)
    primaries = db.query(
        f"""SELECT i->>'primary' AS prim,          -- ⚠️ 别名不能叫 primary（SQL 关键字）
                   count(DISTINCT t.relationship_key) AS customers,
                   count(*) AS mentions
              FROM interest_scan t
              CROSS JOIN LATERAL jsonb_array_elements(t.interests) AS i
             WHERE {where}
             GROUP BY 1
             ORDER BY customers DESC, mentions DESC, prim ASC""", params)
    secondaries = db.query(
        f"""SELECT i->>'primary' AS prim, i->>'secondary' AS secondary,
                   count(DISTINCT t.relationship_key) AS customers,
                   count(*) AS mentions
              FROM interest_scan t
              CROSS JOIN LATERAL jsonb_array_elements(t.interests) AS i
             WHERE {where} AND COALESCE(i->>'secondary','') <> ''
             GROUP BY 1,2
             ORDER BY customers DESC, mentions DESC, secondary ASC
             LIMIT %(sec_top)s""", {**params, "sec_top": int(secondary_top or 40)})
    by_grade = db.query(
        """SELECT COALESCE(a.grade, '-') AS grade,
                  count(DISTINCT t.relationship_key) AS customers
             FROM interest_scan t
             LEFT JOIN customer_intent_assessment a
                    ON a.relationship_key = t.relationship_key AND a.is_current
            GROUP BY 1 ORDER BY 1""")
    last = db.query_one("SELECT max(analyzed_at) AS at, count(*) AS n FROM interest_scan") or {}
    def _p(r):
        return {"primary": r["prim"], "customers": int(r["customers"] or 0),
                "mentions": int(r["mentions"] or 0)}

    def _s(r):
        return {"primary": r["prim"], "secondary": r["secondary"],
                "customers": int(r["customers"] or 0), "mentions": int(r["mentions"] or 0)}

    plist = [_p(r) for r in primaries]
    return {"primaries": plist[:max(1, int(top or 8))],
            "primaries_all": plist,
            "secondaries": [_s(r) for r in secondaries],
            "by_grade": by_grade,
            "coverage": coverage(),
            "analyzed_at": last.get("at"),
            "scans": int(last.get("n") or 0)}


def customers(*, primary: str = "", secondary: str = "", scope: str = "all",
              staff_id: str = "", limit: int = 50) -> list:
    """下钻：关心某类（或某个二级标签）的客户是谁 —— 带二级标签与**证据原话**。

    这是"图上每个数字都能核对"的关键：点开就能看到是哪些客户、他们原话怎么说。
    """
    where, params = _filter_sql(scope=scope, staff_id=staff_id)
    extra, match_cond = "", ["TRUE"]
    if primary:
        extra += " AND t.interests @> %(pjson)s::jsonb"
        params["pjson"] = db.as_json([{"primary": primary}])
        match_cond.append("x->>'primary' = %(primary)s")
        params["primary"] = primary
    if secondary:
        extra += " AND t.interests @> %(sjson)s::jsonb"
        params["sjson"] = db.as_json([{"secondary": secondary}])
        match_cond.append("x->>'secondary' = %(secondary)s")
        params["secondary"] = secondary
    matched = " AND ".join(match_cond)
    return db.query(
        f"""SELECT t.staff_id, t.session_id, t.staff_name, t.target_id, t.target_name,
                   t.relationship_key, t.summary, t.analyzed_at, t.msg_count,
                   (SELECT COALESCE(jsonb_agg(DISTINCT x->>'secondary'), '[]'::jsonb)
                      FROM jsonb_array_elements(t.interests) x
                     WHERE COALESCE(x->>'secondary','') <> '') AS secondaries,
                   (SELECT COALESCE(jsonb_agg(x), '[]'::jsonb)
                      FROM jsonb_array_elements(t.interests) x
                     WHERE {matched}) AS matched,
                   (SELECT a.grade FROM customer_intent_assessment a
                     WHERE a.relationship_key = t.relationship_key AND a.is_current
                     LIMIT 1) AS grade
              FROM interest_scan t
             WHERE {where} {extra}
             ORDER BY t.analyzed_at DESC
             LIMIT %(limit)s""", {**params, "limit": max(1, min(int(limit or 50), 200))})
