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
from fastapi import APIRouter, Body, Depends

import api_common
import api_guard
import assessment_repo
import audit
import db
import review_repo
import rules as rules_mod
import scoring_service as ss
import security

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

    return api_common.paginate(
        """
        SELECT r.*, a.raw_score AS a_raw_score, a.positive_total,
               a.model_confidence, a.session_confidence, a.review_reasons,
               (SELECT count(*) FROM chat_message m
                 WHERE m.staff_id = r.staff_id AND m.session_id = r.session_id)
                 AS session_msg_count
          FROM negative_review r
          LEFT JOIN customer_intent_assessment a ON a.assessment_id = r.assessment_id""",
        "SELECT count(*) AS n FROM negative_review r",
        " AND ".join(cond), params,
        order_by="(r.status = 'pending') DESC, r.updated_at DESC",
        page=page, page_size=page_size)


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
    msgs = []
    if r.get("staff_id") and r.get("session_id"):
        conv = ss.build_conversation(r["staff_id"], r["session_id"], limit=msg_limit)
        msgs = ss.build_display_messages(r["staff_id"], r["session_id"], limit=msg_limit)
    return {"review": r, "assessment": a, "conversation": conv,
            "messages": msgs,
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

@router.post("/submit", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
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
    # 审计与署名主体**由服务端从凭据推导**（评审 P0-2）；客户端自报值只进
    # `detail.reported_actor` 且明确标记不可信 —— 否则 `audit_log` 无法作为追责依据。
    reported = payload.get("reviewer")
    reviewer = security.actor()

    # E4（架构 §7.2 / §6.3）：人工验证状态落列 —— 此前**这一列建了但没有任何写入方**，
    # 于是「模型算的置信度」与「人到底认同没有」永远分不清。
    # 三种结果语义不同，必须分开：
    #   · verified  —— 人工确认，且与模型结论一致（总分相同）
    #   · corrected —— 人工改判过（总分不同）→ 这是 meta-loop 的**误报样本来源**
    #   · rejected  —— 人工认为该条不该扣分（修正后合计为 0）
    ai_total = int(r.get("ai_negative_total") or 0)
    if total == 0:
        vstatus = "rejected"
    elif total == ai_total:
        vstatus = "verified"
    else:
        vstatus = "corrected"
    # ⚠️ 三处写在**同一个事务**里（2026-10-16）：复核队列落人工结论、结论行落
    #    review_status/verification_status、以及（改判时）重算后的分数与等级。
    #    分开写的话，中途崩溃会留下「队列说做完了、结论还是 pending」这种
    #    可读但矛盾的状态；而且看板的「待复核 / 已完成复核」两个口径会长期对不上。
    correction = {}
    with db.tx() as conn:
        review_repo.submit(rid, human_items=valid, human_total=total, note=note,
                           reviewer=reviewer, conn=conn)
        assessment_repo.mark_review_done(r["assessment_id"], vstatus, conn=conn)
        # ⚠️ 改判要**真的修正这个客户**（2026-10-16）：此前复核提交只写复核队列与
        #    verification_status，结论行的 raw_score/grade 一个字都不改 —— 于是
        #    「人工改判」只对以后的规则有用，客户在微伴上的分数与等级标签仍是 AI 的错判。
        #    这里用人工结论**本地确定性重算**总分与等级（不重跑模型、不碰外部系统、
        #    不动回写水位）；`verified`（人工与 AI 一致）不需要改，直接跳过。
        if vstatus in ("corrected", "rejected"):
            correction = assessment_repo.apply_human_correction(
                r["assessment_id"], human_negative_total=total, human_items=valid,
                actor=reviewer, conn=conn)
    audit.record_action(reviewer, "review_submit", str(rid), security.actor_detail(
                    reported, human_negative_total=total, items=valid,
                    ai_negative_total=r["ai_negative_total"],
                    verification_status=vstatus, note=note,
                    correction=correction))
    # E5 meta-loop：改判过的复核**立刻回流评估集**（人工判断是最贵的资源，不该只用一次）。
    # 失败不影响复核提交本身 —— 回流是「后续改进」，不是「本次结论」。
    backflow = {}
    if vstatus in ("corrected", "rejected"):
        try:
            import meta_loop
            backflow = meta_loop.link_review_to_case(rid)
        except Exception as e:
            backflow = {"error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "human_negative_total": total, "verification_status": vstatus,
            "invalid_items": bad, "status": "done", "backflow": backflow,
            "correction": correction}


# ---------------- 修改状态 ----------------

@router.post("/status", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
def set_status(payload: dict = Body(default={})):
    """修改复核状态：pending / done / ignored。"""
    rid = payload.get("review_id")
    st = str(payload.get("status") or "").strip()
    if st not in STATUSES:
        return {"ok": False, "reason": f"状态只能是 {', '.join(STATUSES)}"}
    r = db.query_one("SELECT * FROM negative_review WHERE id=%s", (rid,))
    if not r:
        return {"ok": False, "reason": "复核记录不存在"}

    reported = payload.get("reviewer")
    reviewer = security.actor()          # 服务端推导，不采信请求体（评审 P0-2）
    review_repo.set_status(rid, status=st, reviewer=reviewer,
                           note=(payload.get("note") or "").strip(),
                           reviewed=st in ("done", "ignored"))
    assessment_repo.set_review_status(r["assessment_id"], st)
    audit.record_action(reviewer, "review_status", str(rid), security.actor_detail(reported, status=st))
    return {"ok": True, "status": st}


# ---------------- 批量操作 ----------------

@router.post("/batch-status", dependencies=[Depends(api_guard.require("reviewer", "operator", "admin"))])
def batch_status(payload: dict = Body(default={})):
    ids = payload.get("review_ids") or []
    st = str(payload.get("status") or "").strip()
    if st not in STATUSES or not ids:
        return {"ok": False, "reason": "参数不合法"}
    reported = payload.get("reviewer")
    reviewer = security.actor()          # 服务端推导（评审 P0-2）
    n = review_repo.set_status_bulk(ids, status=st)
    audit.record_action(reviewer, "review_batch_status", f"{len(ids)} 条", security.actor_detail(reported, status=st, ids=list(ids)))
    return {"ok": True, "updated": n}


# ---------------- 审计 ----------------

@router.get("/audit")
def audit_log(limit: int = 30):
    """最近 N 条**业务动作**审计（URL 仍是 `/review/audit`）。

    ⚠️ 函数名不能叫 `audit` —— 那会把本模块顶部的 `import audit` 覆盖掉，
    于是同一个文件里 `audit.record_action(...)` 变成「函数没有 record_action 属性」。
    改名只动 Python 里的名字，**HTTP 路径不变**（前端 `api.reviewAudit` 不受影响）。
    """
    rows = db.query(
        """SELECT id, actor, action, target, detail, created_at
             FROM audit_log ORDER BY id DESC LIMIT %s""", (limit,))
    return {"items": rows}
