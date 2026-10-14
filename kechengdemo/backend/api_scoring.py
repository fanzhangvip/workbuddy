# -*- coding: utf-8 -*-
"""客户意向评分接口（评分中心：可观测 / 可评估）。"""
from fastapi import APIRouter, Body, Query

import config
import db
import scheduler
import scoring_service as ss
from scoring_service import service

import analyze                       # noqa: E402  提示词/契约版本常量
import contracts                     # noqa: E402

router = APIRouter(prefix="/api/scoring", tags=["scoring"])


# ---------------- 运行控制 ----------------

@router.post("/start")
def start(payload: dict = Body(default={})):
    """启动评分任务。

    staff_ids   限定员工（空=全部）
    max_sessions 本次最多评分会话数（0=取配置）
    min_messages 少于该消息数不评（0=取配置）
    force       忽略水位强制重评
    """
    return service.start(
        staff_ids=payload.get("staff_ids") or None,
        max_sessions=int(payload.get("max_sessions") or 0),
        min_messages=int(payload.get("min_messages") or 0),
        force=bool(payload.get("force")),
        triggered_by="manual")


@router.post("/stop")
def stop():
    return service.stop()


@router.get("/status")
def status():
    return service.status()


@router.get("/runs")
def runs(limit: int = 20):
    rows = db.query(
        """SELECT id, kind, status, triggered_by, rule_version, staff_count,
                  session_total, session_done, session_skipped, assessment_new,
                  writeback_ok, writeback_fail, review_new, llm_calls, llm_failed,
                  elapsed_sec, detail, error, started_at, finished_at
             FROM scoring_run ORDER BY id DESC LIMIT %s""", (limit,))
    return {"items": rows}


# ---------------- 可评估：指标 ----------------

@router.get("/metrics")
def metrics():
    """评分总体指标（含人工复核与 AI 的一致性，用于评估模型效果）。"""
    row = db.query_one("""
        SELECT
          (SELECT count(*) FROM customer_intent_assessment)                          AS assessments,
          (SELECT count(DISTINCT relationship_key) FROM customer_intent_assessment)  AS customers,
          (SELECT round(avg(raw_score), 2) FROM customer_intent_assessment)          AS avg_score,
          (SELECT count(*) FROM customer_intent_assessment WHERE grade='S')          AS g_s,
          (SELECT count(*) FROM customer_intent_assessment WHERE grade='A')          AS g_a,
          (SELECT count(*) FROM customer_intent_assessment WHERE grade='B')          AS g_b,
          (SELECT count(*) FROM customer_intent_assessment WHERE grade='C')          AS g_c,
          (SELECT count(*) FROM customer_intent_assessment WHERE grade='D')          AS g_d,
          (SELECT count(*) FROM negative_review)                                     AS reviews,
          (SELECT count(*) FROM negative_review WHERE status='pending')              AS review_pending,
          (SELECT count(*) FROM negative_review WHERE status='done')                 AS review_done,
          (SELECT count(*) FROM negative_review WHERE status='ignored')              AS review_ignored,
          (SELECT count(*) FROM score_writeback WHERE status='ok')                   AS wb_ok,
          (SELECT count(*) FROM score_writeback WHERE status='dry_run')              AS wb_dry,
          (SELECT count(*) FROM score_writeback WHERE status='failed')               AS wb_fail,
          (SELECT count(*) FROM score_writeback WHERE status='skipped')              AS wb_skip,
          (SELECT count(*) FROM negative_review
            WHERE status IN ('done','ignored') AND human_negative_total = ai_negative_total)
                                                                                     AS review_agree,
          (SELECT count(*) FROM negative_review WHERE status IN ('done','ignored'))  AS review_decided
    """)
    decided = (row or {}).get("review_decided") or 0
    agree = (row or {}).get("review_agree") or 0
    row["agree_rate"] = round(agree / decided, 3) if decided else None
    row["writeback_mode"] = service.writeback_mode()
    return row


# ---------------- 评分结果列表 / 详情 ----------------

@router.get("/assessments")
def assessments(grade: str = "", staff_id: str = "", min_score: int = None,
                max_score: int = None, review_status: str = "",
                writeback_status: str = "", q: str = "",
                date_from: str = "", date_to: str = "",
                page: int = 1, page_size: int = 20):
    cond, params = ["1=1"], {}
    if grade:
        cond.append("a.grade = %(grade)s"); params["grade"] = grade
    if staff_id:
        cond.append("a.staff_id = %(staff_id)s"); params["staff_id"] = staff_id
    if min_score is not None:
        cond.append("a.raw_score >= %(min_score)s"); params["min_score"] = min_score
    if max_score is not None:
        cond.append("a.raw_score <= %(max_score)s"); params["max_score"] = max_score
    if review_status:
        cond.append("a.review_status = %(review_status)s"); params["review_status"] = review_status
    if writeback_status:
        cond.append("a.writeback_status = %(writeback_status)s"); params["writeback_status"] = writeback_status
    if q:
        cond.append("(a.target_name ILIKE %(q)s OR a.staff_name ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    if date_from:
        cond.append("a.analyzed_at >= %(date_from)s"); params["date_from"] = date_from
    if date_to:
        cond.append("a.analyzed_at < (%(date_to)s::date + 1)"); params["date_to"] = date_to

    where = " AND ".join(cond)
    params["limit"] = page_size
    params["offset"] = (max(1, page) - 1) * page_size
    items = db.query(f"""
        SELECT a.assessment_id, a.relationship_key, a.staff_id, a.staff_name,
               a.target_id, a.target_name, a.session_id, a.raw_score, a.positive_total,
               a.negative_total, a.grade, a.confidence, a.needs_human_review,
               a.rule_version, a.writeback_status, a.review_status, a.analyzed_at,
               a.last_msg_time,
               (SELECT count(*) FROM chat_message m
                 WHERE m.staff_id = a.staff_id AND m.session_id = a.session_id) AS session_msg_count
          FROM customer_intent_assessment a
         WHERE {where}
         ORDER BY a.assessment_id DESC
         LIMIT %(limit)s OFFSET %(offset)s""", params)
    total = db.query_one(
        f"SELECT count(*) AS n FROM customer_intent_assessment a WHERE {where}",
        {k: v for k, v in params.items() if k not in ("limit", "offset")})
    return {"items": items, "total": (total or {}).get("n", 0), "page": page, "page_size": page_size}


@router.get("/assessment")
def assessment_detail(assessment_id: int):
    """单条评分详情：逐项加减分 + 证据 + 对应人工复核 / 回写记录。"""
    a = db.query_one("""SELECT * FROM customer_intent_assessment WHERE assessment_id=%s""",
                     (assessment_id,))
    if not a:
        return {}
    a["review"] = db.query_one(
        "SELECT * FROM negative_review WHERE assessment_id=%s", (assessment_id,))
    a["writeback"] = db.query_one(
        "SELECT * FROM score_writeback WHERE assessment_id=%s", (assessment_id,))
    return a


@router.post("/session")
def score_session(payload: dict = Body(default={})):
    """对单个会话立即评分（「重新评分」按钮）。"""
    return service.score_session(payload.get("staff_id"), payload.get("session_id"))


# ---------------- 规则表（版本化，可观测）----------------

@router.get("/rules")
def rules_list(rule_version: str = ""):
    rv = rule_version or ss.current_rule_version()
    rows = db.query(
        """SELECT rule_id, polarity, dimension, score, criteria, engine, status
             FROM scoring_rule WHERE rule_version=%s
            ORDER BY polarity DESC, rule_id, score DESC""", (rv,))
    return {"rule_version": rv, "items": rows}


# ---------------- 定时 / 设置 ----------------

@router.get("/schedule")
def get_schedule():
    return scheduler.schedule_info()


@router.post("/schedule")
def set_schedule(payload: dict = Body(default={})):
    return scheduler.set_scoring_interval(int(payload.get("interval_minutes") or 0))


@router.get("/settings")
def get_settings():
    return {
        "writeback_mode": service.writeback_mode(),
        "env_writeback_mode": config.get("SCORE_WRITEBACK") or "dry_run",
        "runtime": db.all_settings(),
        "max_sessions": ss.setting_int("scoring_max_sessions", "SCORING_MAX_SESSIONS", 200),
        "min_messages": ss.setting_int("scoring_min_messages", "SCORING_MIN_MESSAGES", 4),
        "fetch_tags": ss.setting_bool("scoring_fetch_tags", "SCORING_FETCH_TAGS", False),
        # 上下文预处理层（ctx_preprocess）运行时设置
        "ctx_window_size": ss.setting_int("ctx_window_size", "CTX_WINDOW_SIZE", 60),
        "ctx_drop_msgtypes": ss.setting("ctx_drop_msgtypes", "CTX_DROP_MSGTYPES", "31,9"),
        "ctx_drop_pure_url": ss.setting_bool("ctx_drop_pure_url", "CTX_DROP_PURE_URL", True),
        "ctx_drop_system": ss.setting_bool("ctx_drop_system", "CTX_DROP_SYSTEM", True),
        "ctx_display_relative": ss.setting_bool("ctx_display_relative", "CTX_DISPLAY_RELATIVE", True),
        "ctx_min_kept": ss.setting_int("ctx_min_kept", "CTX_MIN_KEPT", 4),
        # 输出契约（阶段A）：模型调用与修复重试的运行时设置
        "llm_temperature": ss.setting_float("llm_temperature", "LLM_TEMPERATURE", 0.0),
        "llm_max_tokens": ss.setting_int("llm_max_tokens", "LLM_MAX_TOKENS", 0),
        "llm_retry_attempts": ss.setting_int("llm_retry_attempts", "LLM_RETRY_ATTEMPTS", 3),
        "rejudge_retries": ss.setting_int("rejudge_retries", "REJUDGE_RETRIES",
                                          analyze.DEFAULT_MAX_REPAIRS),
        "session_max_failures": ss.setting_int("session_max_failures", "SESSION_MAX_FAILURES", 3),
        "prompt_version": analyze.PROMPT_VERSION,
        "schema_version": contracts.SCHEMA_VERSION,
        # 阶段B：图化灰度开关与回写 outbox（均为加性能力，默认关/开见下）
        "use_graph_session": ss.setting_bool("use_graph_session", "USE_GRAPH_SESSION", False),
        "use_outbox": ss.setting_bool("use_outbox", "USE_OUTBOX", True),
        "trace_enabled": ss.setting_bool("trace_enabled", "TRACE_ENABLED", False),
    }


@router.post("/settings")
def set_settings(payload: dict = Body(default={})):
    """运行时调整：回写模式 / 单次会话上限 / 最少消息数 / 是否取标签 / 预处理层 / 契约参数。"""
    if "writeback_mode" in payload:
        m = str(payload["writeback_mode"]).strip().lower()
        if m not in ("live", "dry_run", "off"):
            return {"ok": False, "reason": "writeback_mode 只能是 live / dry_run / off"}
        db.set_setting("score_writeback_mode", m)
        db.execute("INSERT INTO audit_log (actor, action, target, detail) VALUES (%s,%s,%s,%s)",
                   ("manual", "writeback_mode", m, db.as_json({"mode": m})))
    for key, skey in (("max_sessions", "scoring_max_sessions"),
                      ("min_messages", "scoring_min_messages"),
                      ("fetch_tags", "scoring_fetch_tags"),
                      ("ctx_window_size", "ctx_window_size"),
                      ("ctx_drop_msgtypes", "ctx_drop_msgtypes"),
                      ("ctx_drop_pure_url", "ctx_drop_pure_url"),
                      ("ctx_drop_system", "ctx_drop_system"),
                      ("ctx_display_relative", "ctx_display_relative"),
                      ("ctx_min_kept", "ctx_min_kept"),
                      ("llm_temperature", "llm_temperature"),
                      ("llm_max_tokens", "llm_max_tokens"),
                      ("llm_retry_attempts", "llm_retry_attempts"),
                      ("rejudge_retries", "rejudge_retries"),
                      ("session_max_failures", "session_max_failures"),
                      ("use_graph_session", "use_graph_session"),
                      ("use_outbox", "use_outbox"),
                      ("trace_enabled", "trace_enabled")):
        if key in payload:
            db.set_setting(skey, payload[key])
    return {"ok": True, **get_settings()}
