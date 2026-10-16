# -*- coding: utf-8 -*-
"""聊天记录查询接口（供前端「会话管理」页使用）。"""
from fastapi import APIRouter, Query

import db
import ctx_preprocess                 # noqa: E402  上下文预处理层（全项目唯一映射源）

router = APIRouter(prefix="/api", tags=["chat"])


def _media_of(row: dict) -> dict:
    """从企微明文里抽出媒体地址（委托预处理层，全项目唯一实现）。

    msgtype 实测映射：2 图片 / 3 表情 / 6 语音 / 7 视频 / 8 文件
    """
    return ctx_preprocess.media_of(row)


@router.get("/stats")
def stats():
    row = db.query_one("""
        SELECT
          (SELECT count(*) FROM staff)                              AS staff_count,
          (SELECT count(*) FROM chat_session)                       AS session_count,
          (SELECT count(*) FROM chat_message)                       AS msg_count,
          (SELECT count(*) FROM chat_message WHERE content_state = 'ok')
                                                                    AS decrypted_count,
          (SELECT max(msg_time) FROM chat_message)                  AS latest_msg_time,
          (SELECT count(DISTINCT staff_id) FROM chat_session)       AS staff_with_session
    """)
    return row


@router.get("/staffs")
def staffs(q: str = "", only_with_session: int = 0, limit: int = 500,
           session_type: str = ""):
    limit = max(1, min(int(limit or 2000), 2000))   # 上限护栏：limit 是用户可控入参，不夹会一次拉爆内存
    """员工列表（默认只返回有会话的，按最近活跃排序）。

    ``session_type``（修 2026-10-16）：**只影响展示的计数**，不影响"这个员工出不出现在列表里"。
    为什么这么分：员工下拉原来显示的是**不分类型**的会话总数（实测"智泊AI大模型-千雅老师 20058 个会话"），
    而右侧列表按 `session_type` 过滤 —— 切到「群聊」时下拉仍写 2 万、列表却是 0 条，看起来像坏了。
    若把类型也放进 `HAVING`，则该类型没有会话的员工会从下拉里**消失**，用户再想切回来就找不到人 ——
    所以：**"出不出现在列表"看全部会话，"显示几个"看当前类型**。
    """
    sql = """
        SELECT s.staff_id, s.name, s.alias, s.avatar, s.status,
               count(c.session_id) FILTER (
                   WHERE %(stype)s = '' OR c.session_type = %(stype)s)   AS session_count,
               max(c.last_msg_time)         AS last_msg_time,
               coalesce(sum(c.msg_count) FILTER (
                   WHERE %(stype)s = '' OR c.session_type = %(stype)s), 0) AS msg_count
          FROM staff s
          LEFT JOIN chat_session c ON c.staff_id = s.staff_id
         WHERE (%(q)s = '' OR s.name ILIKE '%%' || %(q)s || '%%'
                OR s.alias ILIKE '%%' || %(q)s || '%%')
         GROUP BY s.staff_id, s.name, s.alias, s.avatar, s.status
        HAVING (%(only)s = 0 OR count(c.session_id) > 0)
         ORDER BY max(c.last_msg_time) DESC NULLS LAST
         LIMIT %(limit)s
    """
    return {"items": db.query(sql, {"q": q, "only": only_with_session,
                                    "limit": limit, "stype": session_type})}


#: `/sessions`（列表）与 `/session`（单个）**共用同一份列清单** —— 同一个会话对象在
#: 两个接口里必须同形。曾经 `/session` 用 `SELECT *`，缺了子查询算出来的 `decoded_count`：
#: 于是「深链单独打开」的那个会话，页头显示「已解码 0%」，而它就在左侧列表里躺着、
#: 百分比正常 —— 同一条数据两种长相。改列清单就改这一处。
_SESSION_COLS = """
           c.session_id, c.staff_id, c.session_type, c.name, c.remark, c.avatar,
           c.agree_msgaudit, c.last_msg_time, c.last_msg_thumb, c.msg_count,
           (SELECT count(*) FROM chat_message m
             WHERE m.staff_id = c.staff_id AND m.session_id = c.session_id
               AND m.content_state = 'ok')            AS decoded_count
    """


@router.get("/sessions")
def sessions(staff_id: str = Query(...), session_type: str = "",
             q: str = "", has_msg: int = 0, limit: int = 200, offset: int = 0):
    limit = max(1, min(int(limit or 500), 500))   # 上限护栏：limit 是用户可控入参，不夹会一次拉爆内存
    """某员工的会话列表。``has_msg=1`` 只看已同步到消息的会话。

    返回两个总数（修 2026-10-16，供前端区分空状态）：

    · ``total``     —— 当前**全部过滤条件**下的条数（含 `q`、`has_msg`）
    · ``total_all`` —— 同类型 + 同关键词，但**忽略 `has_msg`** 的条数

    为什么要 ``total_all``：页面默认勾着「只看已同步」，当某员工有 2 万个会话却**一条消息都还没同步**
    时，`items` 与 `total` 都是 0 —— 旧文案据此显示「**该员工暂无会话**」，
    与事实完全相反（实测就是这次"看不到数据"的直接原因）。有了它，前端才能说清
    「有 N 个会话，但都还没同步消息」并给出"显示全部"的出口。
    """
    sql = f"""
        SELECT {_SESSION_COLS}
          FROM chat_session c
         WHERE c.staff_id = %(staff_id)s
           AND (%(stype)s = '' OR c.session_type = %(stype)s)
           AND (%(q)s = '' OR c.name ILIKE '%%' || %(q)s || '%%'
                OR c.remark ILIKE '%%' || %(q)s || '%%')
           AND (%(has_msg)s = 0 OR c.msg_count > 0)
         ORDER BY c.last_msg_time DESC NULLS LAST
         LIMIT %(limit)s OFFSET %(offset)s
    """
    items = db.query(sql, {"staff_id": staff_id, "stype": session_type, "q": q,
                           "has_msg": has_msg, "limit": limit, "offset": offset})
    # 两条计数都用**同一份条件**（原来 total 漏了 `q`：搜索时"共 N 个会话"显示的是全量，
    # 而列表只有命中的几条 —— 实测 `has_msg=0&q=zzz` → items=19、total=20058）。
    base = """FROM chat_session c
               WHERE c.staff_id = %(staff_id)s
                 AND (%(stype)s = '' OR c.session_type = %(stype)s)
                 AND (%(q)s = '' OR c.name ILIKE '%%' || %(q)s || '%%'
                      OR c.remark ILIKE '%%' || %(q)s || '%%')"""
    p = {"staff_id": staff_id, "stype": session_type, "q": q, "has_msg": has_msg}
    total = db.query_one(
        f"SELECT count(*) AS n {base} AND (%(has_msg)s = 0 OR c.msg_count > 0)", p)
    total_all = db.query_one(f"SELECT count(*) AS n {base}", p)
    return {"items": items, "total": (total or {}).get("n", 0),
            "total_all": (total_all or {}).get("n", 0)}


@router.get("/session")
def session_detail(staff_id: str, session_id: str):
    """单个会话（深链兜底用，见 `frontend/src/views/ChatView.vue` 的 `openFromQuery()`）。

    列清单与 `/sessions` 共用 `_SESSION_COLS`，因此返回结构与列表项**完全一致**
    —— 前端把这一条插进列表头部之后，页面各处拿到的字段与其它会话没有差别。
    """
    row = db.query_one(
        f"""SELECT {_SESSION_COLS}
              FROM chat_session c WHERE c.staff_id = %s AND c.session_id = %s""",
        (staff_id, session_id))
    return row or {}


@router.get("/messages")
def messages(staff_id: str, session_id: str, limit: int = 60,
             before_time: int = 0, after_time: int = 0, q: str = "",
             before_id: str = ""):
    limit = max(1, min(int(limit or 200), 200))   # 上限护栏：limit 是用户可控入参，不夹会一次拉爆内存
    """会话消息（默认返回最新 limit 条，按时间正序）。

    ``before_time`` + ``before_id``（修 2026-10-16）：**向上翻页的复合游标**。

    为什么必须带 id：`msg_time` 是**秒级**的，同一秒里可以有多条消息 ——
    实测 804 个有正文的会话里 **636 个（79%）存在"同一秒多条消息"**（共 3203 条，单会话最多 46 条）。
    只用 `msg_time < before_time`（严格小于）翻页，页边界落进同一秒时，
    那一秒里更早的几条会被**直接跳过**（用户点"加载更早"就看不到它们）。
    带上 `message_id` 变成 `(msg_time, message_id) < (t, id)`，与 `ORDER BY` 的口径一致，
    翻页既不丢也不重。

    兼容：只传 `before_time` 时按旧语义（`msg_time < t`）走 —— 老前端/脚本不会被这次改动弄坏。
    """
    cond = ["staff_id = %s", "session_id = %s"]
    params = [staff_id, session_id]
    if before_time and before_id:
        cond.append("(msg_time, message_id) < (%s, %s)")
        params += [before_time, before_id]
    elif before_time:
        cond.append("msg_time < %s")
        params.append(before_time)
    if after_time:
        cond.append("msg_time > %s")
        params.append(after_time)
    if q:
        cond.append("content ILIKE %s")
        params.append(f"%{q}%")

    sql = f"""
        SELECT message_id, msgid, direction, msg_time, msg_type, from_id, to_id,
               from_name, from_avatar, file_url, content, msgtype, content_state, plain
          FROM chat_message
         WHERE {' AND '.join(cond)}
         ORDER BY msg_time DESC, message_id DESC
         LIMIT %s
    """
    params.append(limit)
    rows = db.query(sql, params)
    rows.reverse()
    for r in rows:
        r.update(_media_of(r))
        r["type_name"] = ctx_preprocess.type_label(r.get("msgtype"), r.get("msg_type"))
        r["is_media"] = ctx_preprocess.is_media(r.get("msgtype"))
        r["role"] = ctx_preprocess.role_of(r.get("direction"))
    return {"items": rows, "has_more": len(rows) == limit}


@router.get("/search")
def search(q: str = Query(...), staff_id: str = "", limit: int = 50):
    limit = max(1, min(int(limit or 200), 200))   # 上限护栏：limit 是用户可控入参，不夹会一次拉爆内存
    """按正文全文搜索（pg_trgm 加速的 ILIKE）。"""
    if not q.strip():
        return {"items": []}
    sql = """
        SELECT m.staff_id, m.session_id, m.message_id, m.msg_time, m.direction,
               m.from_name, m.content, m.msgtype,
               s.name AS staff_name, c.name AS session_name
          FROM chat_message m
          LEFT JOIN staff s ON s.staff_id = m.staff_id
          LEFT JOIN chat_session c ON c.staff_id = m.staff_id
                                  AND c.session_id = m.session_id
         WHERE m.content ILIKE %s
           AND (%s = '' OR m.staff_id = %s)
         ORDER BY m.msg_time DESC
         LIMIT %s
    """
    like = f"%{q}%"
    return {"items": db.query(sql, (like, staff_id, staff_id, limit))}


@router.get("/sessions/recent")
def recent_sessions(limit: int = 50, session_type: str = "ext_user"):
    limit = max(1, min(int(limit or 200), 200))   # 上限护栏：limit 是用户可控入参，不夹会一次拉爆内存
    """全公司最近有消息的会话（首页总览用）。"""
    sql = """
        SELECT c.staff_id, c.session_id, c.name, c.remark, c.session_type,
               c.last_msg_time, c.last_msg_thumb, c.msg_count, s.name AS staff_name
          FROM chat_session c
          LEFT JOIN staff s ON s.staff_id = c.staff_id
         WHERE c.session_type = %s
         ORDER BY c.last_msg_time DESC NULLS LAST
         LIMIT %s
    """
    return {"items": db.query(sql, (session_type, limit))}
