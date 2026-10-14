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
def staffs(q: str = "", only_with_session: int = 0, limit: int = 500):
    """员工列表（默认只返回有会话的，按最近活跃排序）。"""
    sql = """
        SELECT s.staff_id, s.name, s.alias, s.avatar, s.status,
               count(c.session_id)          AS session_count,
               max(c.last_msg_time)         AS last_msg_time,
               sum(c.msg_count)             AS msg_count
          FROM staff s
          LEFT JOIN chat_session c ON c.staff_id = s.staff_id
         WHERE (%(q)s = '' OR s.name ILIKE '%%' || %(q)s || '%%'
                OR s.alias ILIKE '%%' || %(q)s || '%%')
         GROUP BY s.staff_id, s.name, s.alias, s.avatar, s.status
        HAVING (%(only)s = 0 OR count(c.session_id) > 0)
         ORDER BY max(c.last_msg_time) DESC NULLS LAST
         LIMIT %(limit)s
    """
    return {"items": db.query(sql, {"q": q, "only": only_with_session, "limit": limit})}


@router.get("/sessions")
def sessions(staff_id: str = Query(...), session_type: str = "",
             q: str = "", has_msg: int = 0, limit: int = 200, offset: int = 0):
    """某员工的会话列表。``has_msg=1`` 只看已同步到消息的会话。"""
    sql = """
        SELECT c.session_id, c.staff_id, c.session_type, c.name, c.remark, c.avatar,
               c.agree_msgaudit, c.last_msg_time, c.last_msg_thumb, c.msg_count,
               (SELECT count(*) FROM chat_message m
                 WHERE m.staff_id = c.staff_id AND m.session_id = c.session_id
                   AND m.content_state = 'ok')            AS decoded_count
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
    total = db.query_one(
        """SELECT count(*) AS n FROM chat_session c
            WHERE c.staff_id = %s AND (%s = '' OR c.session_type = %s)
              AND (%s = 0 OR c.msg_count > 0)""",
        (staff_id, session_type, session_type, has_msg))
    return {"items": items, "total": (total or {}).get("n", 0)}


@router.get("/session")
def session_detail(staff_id: str, session_id: str):
    row = db.query_one(
        """SELECT * FROM chat_session WHERE staff_id = %s AND session_id = %s""",
        (staff_id, session_id))
    return row or {}


@router.get("/messages")
def messages(staff_id: str, session_id: str, limit: int = 60,
             before_time: int = 0, after_time: int = 0, q: str = ""):
    """会话消息（默认返回最新 limit 条，按时间正序）。"""
    cond = ["staff_id = %s", "session_id = %s"]
    params = [staff_id, session_id]
    if before_time:
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
