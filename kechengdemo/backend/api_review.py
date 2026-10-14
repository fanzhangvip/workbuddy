# -*- coding: utf-8 -*-
"""负分人工复核接口。

设计（对应需求：正分自动回写微伴，负分需人工打分）：
  · 大模型 / 规则引擎识别出的**扣分项**不直接生效，进入 ``negative_review`` 队列；
  · 工作人员在后台逐条确认、修正或驳回，并支持状态流转与查询：
        pending   未打分（待处理）
        done      已完成（人工已给出最终扣分）
        ignored   已忽略（AI 误判，无需扣分）
  · 每次提交 / 改状态都写 ``audit_log``，便于追溯与评估模型准确率。
"""
from fastapi import APIRouter, Body

import db
import rules as rules_mod
import scoring_service as ss

router = APIRouter(prefix="/api/review", tags=["review"])

STATUSES = ("pending", "done", "ignored")
STATUS_CN = {"pending": "未打分", "done": "已完成", "ignored": "已忽略"}


# ---------------- 统计 ----------------

@router.get("/stats")
def stats():
    row = db.query_one("""
        SELECT
          (SELECT count(*) FROM negative_review)                          AS total,
          (SELECT count(*) FROM negative_review WHERE status='pending')   AS pending,
          (SELECT count(*) FROM negative_review WHERE status='done')      AS done,
          (SELECT count(*) FROM negative_review WHERE status='ignored')   AS ignored,
          (SELECT count(*) FROM negative_review
            WHERE status='done' AND human_negative_total = ai_negative_total) AS agree_done
    """)
    return row


# ---------------- 列表 ----------------

@router.get("/list")
def list_reviews(status: str = "", staff_id: str = "", grade: str = "",
                 q: str = "", date_from: str = "", date_to: str = "",
                 page: int = 1, page_size: int = 20):
    cond, params = ["1=1"], {}
    if status:
        cond.append("r.status = %(status)s"); params["status"] = status
    if staff_id:
        cond.append("r.staff_id = %(staff_id)s"); params["staff_id"] = staff_id
    if grade:
        cond.append("r.grade = %(grade)s"); params["grade"] = grade
    if q:
        cond.append("(r.target_name ILIKE %(q)s OR r.staff_name ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    if date_from:
        cond.append("r.created_at >= %(date_from)s"); params["date_from"] = date_from
    if date_to:
        cond.append("r.created_at < (%(date_to)s::date + 1)"); params["date_to"] = date_to

    where = " AND ".join(cond)
    params["limit"] = page_size
    params["offset"] = (max(1, page) - 1) * page_size
    items = db.query(f"""
        SELECT r.*, a.raw_score AS a_raw_score, a.positive_total, a.confidence,
               (SELECT count(*) FROM chat_message m
                 WHERE m.staff_id = r.staff_id AND m.session_id = r.session_id) AS session_msg_count
          FROM negative_review r
          LEFT JOIN customer_intent_assessment a ON a.assessment_id = r.assessment_id
         WHERE {where}
         ORDER BY (r.status = 'pending') DESC, r.updated_at DESC
         LIMIT %(limit)s OFFSET %(offset)s""", params)
    total = db.query_one(
        f"SELECT count(*) AS n FROM negative_review r WHERE {where}",
        {k: v for k, v in params.items() if k not in ("limit", "offset")})
    return {"items": items, "total": (total or {}).get("n", 0), "page": page, "page_size": page_size}


# ---------------- 详情（含证据消息）----------------

@router.get("/detail")
def detail(review_id: int, msg_limit: int = 60):
    """复核详情：AI 扣分项 + 证据 + 该会话近期聊天记录（供人工判断）。"""
    r = db.query_one("SELECT * FROM negative_review WHERE id=%s", (review_id,))
    if not r:
        return {}
    a = db.query_one("SELECT * FROM customer_intent_assessment WHERE assessment_id=%s",
                     (r["assessment_id"],))
    conv = ""
    if r.get("staff_id") and r.get("session_id"):
        conv = ss.build_conversation(r["staff_id"], r["session_id"], limit=msg_limit)
    return {"review": r, "assessment": a, "conversation": conv,
            "status_cn": STATUS_CN.get(r["status"], r["status"])}


# ---------------- 可选项（负分规则清单）----------------

@router.get("/options")
def options():
    """人工可勾选的扣分项清单（来自当前规则版本）。"""
    rules = rules_mod.load_rules()
    out = []
    for r in rules.get("negative_rules", []):
        out.append({
            "rule_id": r["rule_id"],
            "dimension": r["dimension"],
            "engine": r.get("engine"),
            "options": [{"score": o["score"], "criteria": o["criteria"]}
                        for o in r.get("options", [])],
        })
    return {"rule_version": rules.get("rule_version"), "items": out}


# ---------------- 提交人工打分 ----------------

@router.post("/submit")
def submit(payload: dict = Body(default={})):
    """提交人工打分：确认/修正扣分项 → 状态置为「已完成」。

    payload: { review_id, items:[{rule_id, score, reason}], note, reviewer }
    """
    rid = payload.get("review_id")
    r = db.query_one("SELECT * FROM negative_review WHERE id=%s", (rid,))
    if not r:
        return {"ok": False, "reason": "复核记录不存在"}

    rules = rules_mod.load_rules()
    valid, bad = rules_mod.validate_items(payload.get("items") or [], rules)
    valid = [i for i in valid if i["polarity"] == "negative"]
    total = sum(i["score"] for i in valid)
    note = (payload.get("note") or "").strip()
    reviewer = (payload.get("reviewer") or "工作人员").strip()

    db.execute(
        """UPDATE negative_review SET human_negative_items=%s, human_negative_total=%s,
               human_note=%s, reviewer=%s, status='done', reviewed_at=now(), updated_at=now()
             WHERE id=%s""",
        (db.as_json(valid), total, note, reviewer, rid))
    db.execute("UPDATE customer_intent_assessment SET review_status='done' WHERE assessment_id=%s",
               (r["assessment_id"],))
    db.execute("INSERT INTO audit_log (actor, action, target, detail) VALUES (%s,%s,%s,%s)",
               (reviewer, "review_submit", str(rid),
                db.as_json({"human_negative_total": total, "items": valid,
                            "ai_negative_total": r["ai_negative_total"],
                            "note": note})))
    return {"ok": True, "human_negative_total": total,
            "invalid_items": bad, "status": "done"}


# ---------------- 修改状态 ----------------

@router.post("/status")
def set_status(payload: dict = Body(default={})):
    """修改复核状态：pending / done / ignored。"""
    rid = payload.get("review_id")
    st = str(payload.get("status") or "").strip()
    if st not in STATUSES:
        return {"ok": False, "reason": f"状态只能是 {', '.join(STATUSES)}"}
    r = db.query_one("SELECT * FROM negative_review WHERE id=%s", (rid,))
    if not r:
        return {"ok": False, "reason": "复核记录不存在"}

    reviewer = (payload.get("reviewer") or "工作人员").strip()
    reviewed = "now()" if st in ("done", "ignored") else "NULL"
    db.execute(
        f"""UPDATE negative_review SET status=%s, reviewer=%s,
                reviewed_at={reviewed}, updated_at=now(),
                human_note = COALESCE(NULLIF(%s,''), human_note)
              WHERE id=%s""",
        (st, reviewer, (payload.get("note") or "").strip(), rid))
    db.execute("UPDATE customer_intent_assessment SET review_status=%s WHERE assessment_id=%s",
               (st, r["assessment_id"]))
    db.execute("INSERT INTO audit_log (actor, action, target, detail) VALUES (%s,%s,%s,%s)",
               (reviewer, "review_status", str(rid), db.as_json({"status": st})))
    return {"ok": True, "status": st}


# ---------------- 批量操作 ----------------

@router.post("/batch-status")
def batch_status(payload: dict = Body(default={})):
    ids = payload.get("review_ids") or []
    st = str(payload.get("status") or "").strip()
    if st not in STATUSES or not ids:
        return {"ok": False, "reason": "参数不合法"}
    reviewer = (payload.get("reviewer") or "工作人员").strip()
    n = db.execute(
        "UPDATE negative_review SET status=%s, updated_at=now() WHERE id = ANY(%s)",
        (st, list(ids)))
    db.execute("INSERT INTO audit_log (actor, action, target, detail) VALUES (%s,%s,%s,%s)",
               (reviewer, "review_batch_status", f"{len(ids)} 条",
                db.as_json({"status": st, "ids": list(ids)})))
    return {"ok": True, "updated": n}


# ---------------- 审计 ----------------

@router.get("/audit")
def audit(limit: int = 30):
    rows = db.query(
        """SELECT id, actor, action, target, detail, created_at
             FROM audit_log ORDER BY id DESC LIMIT %s""", (limit,))
    return {"items": rows}
