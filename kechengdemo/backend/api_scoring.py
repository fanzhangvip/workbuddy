# -*- coding: utf-8 -*-
"""客户意向评分接口（评分中心：可观测 / 可评估）。"""
from fastapi import APIRouter, Body, Query

import config
import db
import grade_tag
import idempotency
import retention
import scheduler
import scoring_service as ss
from scoring_service import service

import analyze                       # noqa: E402  提示词/契约版本常量
import confidence                    # noqa: E402  置信度默认阈值（架构 第六章）
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
    request_key 可选幂等键（H2）：同一 key 重复提交只真正启动一次，
                第二次直接返回首次结果并带 `_replayed=true`（前端重试/手抖双击都安全）
    """
    key = payload.get("request_key") or None
    return idempotency.run_once(key, "scoring.start", lambda: service.start(
        staff_ids=payload.get("staff_ids") or None,
        max_sessions=int(payload.get("max_sessions") or 0),
        min_messages=int(payload.get("min_messages") or 0),
        force=bool(payload.get("force")),
        triggered_by="manual"))


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
                include_history: bool = False,
                page: int = 1, page_size: int = 20):
    """评分列表。

    **E2（架构 §7.2）：默认只返回「当前有效」的一条**（`is_current`）——
    同一客户被重评后，旧记录不再出现在列表里（否则同一客户会有多行）。
    需要看历史时传 `include_history=true`（用于「这个客户是怎么一路变过来的」）。
    """
    cond, params = ["1=1"], {}
    if not include_history:
        # ⚠️ 用 `IS TRUE` 而不是 `= true`：历史行（本列新增前写入的）在极端情况下
        # 可能是 NULL，`= true` 会把它们整体排除掉，而 `IS TRUE` 同样排除 NULL ——
        # 两者一致，但这里显式用 IS TRUE 表达「只认明确的当前行」的意图。
        cond.append("a.is_current IS TRUE")
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
               a.negative_total, a.grade, a.model_confidence, a.session_confidence,
               a.review_reasons, a.needs_human_review,
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


# ---------------- D2-1 等级 → 微伴标签（独立开关的读写入口）----------------

@router.get("/grade-tags")
def grade_tags_status():
    """等级标签回写状态：开关 / 标签组 / 已缓存 tag_id / outbox 流水 / 最近错误。

    与 `/scoring/settings` 里的分数回写状态**完全分开**，便于"到底谁在写微伴"一眼可辨。
    """
    return grade_tag.stats()


@router.post("/grade-tags/sync")
def grade_tags_sync(payload: dict = Body(default={})):
    """扫描微伴标签库，找出「意向等级」组下的 S/A/B/C/D 并缓存 `tag_id`。

    ⚠️ `create_missing=true` 会在微伴后台**真的创建缺失的标签**（副作用）——
    故默认 `dry_run=true` 只报告。**建标签是人工动作，不由评分链路隐式完成**
    （`tag/add` 的「不存在则自动创建」意味着一次拼写错误就能留下垃圾标签组）。
    """
    return grade_tag.sync_tags(create_missing=bool(payload.get("create_missing")),
                               dry_run=bool(payload.get("dry_run", True)))


@router.post("/grade-tags/align")
def grade_tags_align(payload: dict = Body(default={})):
    """**人工触发**：把当前等级对齐到微伴标签（补历史 / 修正漂移）。

    - 默认 `dry_run=true`：只报告会改哪些客户，不调接口
    - `remove_all_others=true`：把其它 4 个等级标签一并摘掉（修正人工改过标签的漂移）
    """
    return idempotency.run_once(
        payload.get("request_key"), "grade_tag.align",
        lambda: grade_tag.align(relationship_keys=payload.get("relationship_keys"),
                                limit=int(payload.get("limit") or 0),
                                dry_run=bool(payload.get("dry_run", True)),
                                remove_all_others=bool(payload.get("remove_all_others"))))


@router.post("/grade-tags/drain")
def grade_tags_drain(payload: dict = Body(default={})):
    """手动跑一次标签 outbox（排障用；正常由 scheduler 每 30s 自动跑）。"""
    return grade_tag.drain(limit=int(payload.get("limit") or 50))


@router.get("/repair-current")
def repair_current():
    """E2 自愈：修「有行但无 current」与「superseded_by 悬空」。只读式汇报 + 幂等修复。"""
    return ss.repair_current_flags()


@router.get("/rules/gate")
def rules_gate(kind: str = "", limit: int = 0):
    """闸6 门禁判定（只读）：看不改规则的前提下，当前指标是否够格发布。

    `decidable=false` 表示**无法判定**（没基线或没标注样本）—— 那不是「通过」，
    只是没有门禁保护，前端要如实展示。
    """
    import eval_runner
    return eval_runner.gate_decision(kind=kind, limit=limit, run_if_possible=False)


@router.post("/rules/publish")
def rules_publish(payload: dict = Body(default={})):
    """发布规则（闸6 硬拦截）：**先跑评估集，指标不降才允许生效**。

    - `force=true` 可强行发布（会写审计留痕，便于事后追责）
    - 无基线 / 无标注样本时返回 `decidable=false` 并放行，但**明确告知没有门禁保护**
    """
    import eval_runner
    key = payload.get("request_key")
    force = bool(payload.get("force"))

    def _do():
        dec = eval_runner.gate_decision(kind=payload.get("kind") or "",
                                        limit=int(payload.get("limit") or 0),
                                        run_if_possible=True)
        if dec.get("blocked") and not force:
            ss._audit("manual", "rule_publish_blocked", f"rv:{ss.current_rule_version()}",
                      {"diffs": dec.get("diffs")})
            return {"ok": False, "reason": dec.get("reason"), "gate": dec}
        res = ss.sync_rules()
        ss._audit("manual", "rule_publish", f"rv:{res.get('rule_version')}",
                  {"gate": {k: dec.get(k) for k in ("enabled", "has_baseline",
                                                    "decidable", "gate_passed", "blocked")},
                   "forced": force, "rules": res.get("rules")})
        return {"ok": True, "published": res, "gate": dec,
                "warning": None if dec.get("decidable") else dec.get("reason")}

    return idempotency.run_once(key, "rules.publish", _do)


@router.get("/sa-review")
def sa_review(scope: str = ""):
    """早会复核名单（架构 §2.1 F2 / D3）。

    返回「当前有效」的 S/A 客户 + 他们的待复核状态 —— 早会要的是**一份名单**，
    而不是一个启动按钮。默认等级范围取 `public_class_grade_scope`（S,A）。
    """
    grades = [g.strip().upper() for g in (scope or ss.setting(
        "public_class_grade_scope", "PUBLIC_CLASS_GRADE_SCOPE", "S,A")).split(",") if g.strip()]
    items = db.query(
        """SELECT a.assessment_id, a.staff_id, a.staff_name, a.target_id, a.target_name,
                  a.grade, a.raw_score, a.session_confidence, a.review_reasons,
                  a.needs_human_review, a.review_status, a.analyzed_at, a.last_msg_time
             FROM customer_intent_assessment a
            WHERE a.is_current IS TRUE AND a.grade = ANY(%s)
            ORDER BY a.grade, a.raw_score DESC, a.assessment_id DESC
            LIMIT 500""", (grades,))
    return {"scope": grades, "count": len(items), "items": items}


@router.post("/public-class-review")
def public_class_review(payload: dict = Body(default={})):
    """公开课复核（架构 §2.1 F2，D3：**不定期 → 手动按钮**）。

    `mode`：
    · `before` 课前 —— 扫 S/A 走**增量刷新**（有新消息的才重评），保证名单是最新的
    · `after`  课后 —— 同样增量刷新，用于看公开课之后的意向变化

    两种模式的区别在业务语义，实现上都是「限定等级范围的增量刷新」——
    这符合 D3 拍板的「复核走增量刷新」。返回里带**刷新后的名单**，按钮点完直接能看。
    """
    mode = str(payload.get("mode") or "before").strip().lower()
    if mode not in ("before", "after"):
        return {"ok": False, "reason": "mode 只能是 before / after"}
    grades = [g.strip().upper() for g in str(
        payload.get("scope") or ss.setting("public_class_grade_scope",
                                           "PUBLIC_CLASS_GRADE_SCOPE", "S,A")).split(",")
        if g.strip()]
    key = payload.get("request_key")

    def _do():
        res = service.start(staff_ids=payload.get("staff_ids") or None,
                            max_sessions=int(payload.get("max_sessions") or 0),
                            min_messages=int(payload.get("min_messages") or 0),
                            force=False,                    # 增量刷新（D3）
                            triggered_by="public_class", grades=grades)
        ss._audit("manual", "public_class_review", f"mode:{mode}",
                  {"mode": mode, "grades": grades, "started": bool(res.get("ok")),
                   "reason": res.get("reason")})
        return {"ok": bool(res.get("ok")), "mode": mode, "scope": grades,
                "run": res, "name_list": sa_review(",".join(grades)),
                "reason": res.get("reason")}

    return idempotency.run_once(key, f"public_class.{mode}", _do)


@router.get("/retention")
def retention_plan(days: int = None):
    """数据保留预演（边界4）：算出各表会被删多少行。**只读，不删任何数据。**"""
    d = days if days is not None else ss.setting_int("data_retention_days",
                                                     "DATA_RETENTION_DAYS", 0)
    return retention.plan(int(d or 0))


@router.post("/retention/purge")
def retention_purge(payload: dict = Body(default={})):
    """执行数据保留清理。**必须显式传 `confirm=true`** 才会真删。"""
    days = int(payload.get("days") or ss.setting_int("data_retention_days",
                                                     "DATA_RETENTION_DAYS", 0))
    if not payload.get("confirm"):
        return {**retention.plan(days), "dry_run": True,
                "hint": "预演结果。要真正执行请传 confirm=true"}
    return idempotency.run_once(payload.get("request_key"), "retention.purge",
                                lambda: retention.purge(days, dry_run=False))


@router.get("/settings")
def get_settings():
    return {
        "writeback_mode": service.writeback_mode(),
        "env_writeback_mode": config.get("SCORE_WRITEBACK") or "dry_run",
        "runtime": db.all_settings(),
        "max_sessions": ss.setting_int("scoring_max_sessions", "SCORING_MAX_SESSIONS", 200),
        "min_messages": ss.setting_int("scoring_min_messages", "SCORING_MIN_MESSAGES", 4),
        "fetch_tags": ss.setting_bool("scoring_fetch_tags", "SCORING_FETCH_TAGS", False),
        # Loop 护栏（架构 §2.2 G3/G4/G7）：0 = 不限
        "deadline_minutes": ss.setting_int("scoring_deadline_minutes",
                                           "SCORING_DEADLINE_MINUTES", 120),
        "max_llm_calls": ss.setting_int("scoring_max_llm_calls",
                                        "SCORING_MAX_LLM_CALLS", 500),
        "priority_order": ss.setting_bool("scoring_priority_order",
                                          "SCORING_PRIORITY_ORDER", True),
        # 单次 LLM 调用超时（0 = 用 llm 模块默认 120s）
        "llm_timeout_sec": ss.setting_int("llm_timeout_sec", "LLM_TIMEOUT_SEC", 0),
        # 边界4 数据保留：**0 = 不清理**（保留多久是业务决策，代码不替业务拍板）
        "data_retention_days": ss.setting_int("data_retention_days", "DATA_RETENTION_DAYS", 0),
        # 闸6 回归门禁：改规则前必须跑评估集、指标不降才允许发布
        "eval_gate_enabled": ss.setting_bool("eval_gate_enabled", "EVAL_GATE_ENABLED", True),
        "eval_baseline_path": ss.setting("eval_baseline_path", "EVAL_BASELINE_PATH", ""),
        # F2（D3 已拍板）：早会复核触发时刻（HH:MM，**空 = 关闭**）+ 公开课复核等级范围
        "morning_cron": ss.setting("scoring_morning_cron", "SCORING_MORNING_CRON", ""),
        "public_class_grade_scope": ss.setting("public_class_grade_scope",
                                               "PUBLIC_CLASS_GRADE_SCOPE", "S,A"),
        # D2-1 差值累加：微伴「客户评分」是累计行为分、无覆盖接口 →
        # 必须只写增量。false = 退回改造前的整值语义（回滚开关，**开 live 前不要关**）
        "writeback_delta_mode": ss.setting_bool("writeback_delta_mode",
                                                "WRITEBACK_DELTA_MODE", True),
        # ---- D2-1 等级 → 微伴标签：**与分数回写开关完全独立** ----
        # ⚠️ 默认 off：打标签是「写外部系统」的副作用，必须人显式打开
        "grade_tag_mode": ss.setting("grade_tag_mode", "GRADE_TAG_MODE", "off"),
        "grade_tag_group": ss.setting("grade_tag_group", "GRADE_TAG_GROUP",
                                      grade_tag.DEFAULT_TAG_GROUP),
        "grade_tag_ids": grade_tag.get_tag_ids(),
        # 建标签载体（专用测试客户，`staff_id:external_user_id`）—— 空 = 不允许自动建标签
        "grade_tag_probe_customer": db.get_setting(grade_tag.PROBE_KEY) or "",
        # 回写副作用配额（架构 边界3）：**默认 0 = 不限**（不改变既有行为，
        # 上线前由业务确认后再收紧；文档建议单客户单日 3 次）
        "writeback_quota_per_run": ss.setting_int("writeback_quota_per_run",
                                                  "WRITEBACK_QUOTA_PER_RUN", 0),
        "writeback_quota_per_customer_per_day": ss.setting_int(
            "writeback_quota_per_customer_per_day", "WRITEBACK_QUOTA_PER_CUSTOMER_PER_DAY", 0),
        # 上下文预处理层（ctx_preprocess）运行时设置
        "ctx_window_size": ss.setting_int("ctx_window_size", "CTX_WINDOW_SIZE", 60),
        "ctx_drop_msgtypes": ss.setting("ctx_drop_msgtypes", "CTX_DROP_MSGTYPES", "31,9"),
        "ctx_drop_pure_url": ss.setting_bool("ctx_drop_pure_url", "CTX_DROP_PURE_URL", True),
        "ctx_drop_system": ss.setting_bool("ctx_drop_system", "CTX_DROP_SYSTEM", True),
        "ctx_display_relative": ss.setting_bool("ctx_display_relative", "CTX_DISPLAY_RELATIVE", True),
        "ctx_min_kept": ss.setting_int("ctx_min_kept", "CTX_MIN_KEPT", 4),
        # B4（架构 §2.3）有效信息量的另两个口径：客户开口数 / 文本总长度
        # （0 = 不限；min_customer_msgs 默认 1 = 客户至少说一句话）
        "ctx_min_customer_msgs": ss.setting_int("ctx_min_customer_msgs",
                                                "CTX_MIN_CUSTOMER_MSGS", 1),
        "ctx_min_effective_chars": ss.setting_int("ctx_min_effective_chars",
                                                  "CTX_MIN_EFFECTIVE_CHARS", 0),
        # 关键历史检索（架构 §4.4）：④ 段总量上限 / ④′ 冷启动早期锚点条数
        "ctx_key_history": ss.setting_bool("ctx_key_history", "CTX_KEY_HISTORY", True),
        "ctx_key_history_max": ss.setting_int("ctx_key_history_max", "CTX_KEY_HISTORY_MAX", 30),
        "ctx_cold_start_early": ss.setting_int("ctx_cold_start_early", "CTX_COLD_START_EARLY", 10),
        # ③ 增量段 / 预算（架构 §4.5）：增量条数软上限（0 = 关闭增量，退回「最近窗口」）
        # 与整段文本的 token 硬预算（超限先砍 ⑤ 再砍 ③ 最早）
        "ctx_delta_max": ss.setting_int("ctx_delta_max", "CTX_DELTA_MAX", 200),
        "ctx_tokens_max": ss.setting_int("ctx_tokens_max", "CTX_TOKENS_MAX", 12000),
        # ①② 记忆段（架构 §4.5.3 / C-C）：读回上次评估结论（档案 + 上次明细）
        "ctx_memory": ss.setting_bool("ctx_memory", "CTX_MEMORY", True),
        # ⑤ 兜底窗口的触发阈值（架构 §4.5.2）：③④ 有效条数低于它就补最近 60 条。
        # 默认 4 = 文档原设计（①② 已落地，连续性由记忆承担）；设 60 可退回保守行为。
        "ctx_fallback_min": ss.setting_int("ctx_fallback_min", "CTX_FALLBACK_MIN", 4),
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
        # 置信度与复核触发（架构 第六章 E4）：默认值按 52 条真实评估标定，全部可调
        "confidence_review_min": ss.setting_float("confidence_review_min", "CONFIDENCE_REVIEW_MIN",
                                                  confidence.REVIEW_CONF_MIN),
        "confidence_boundary_delta": ss.setting_int("confidence_boundary_delta",
                                                    "CONFIDENCE_BOUNDARY_DELTA",
                                                    confidence.GRADE_BOUNDARY_DELTA),
        "confidence_flip_steps": ss.setting_int("confidence_flip_steps", "CONFIDENCE_FLIP_STEPS",
                                                confidence.GRADE_FLIP_STEPS),
        "confidence_weak_weight": ss.setting_int("confidence_weak_weight", "CONFIDENCE_WEAK_WEIGHT",
                                                 confidence.WEAK_EVIDENCE_WEIGHT),
        # ⚠️ 默认 true = 不放松（保持改造前的行为）；实测关掉可把复核率 90% → 60%
        "review_engine_negative": ss.setting_bool("review_engine_negative", "REVIEW_ENGINE_NEGATIVE",
                                                  confidence.REVIEW_ENGINE_NEGATIVE),
    }


@router.post("/settings")
def set_settings(payload: dict = Body(default={})):
    """运行时调整：回写模式 / 单次会话上限 / 最少消息数 / 是否取标签 / 预处理层 / 契约参数。

    **每个变更的 key 都写 audit_log**（架构 §2.2 H2）。

    ⚠️ 修正文档不符：文档 §2.2 曾称「key 级留痕已在 api_scoring 实现」，但实况**只做了
    `writeback_mode` 一个键** —— 其余约 30 个键（`ctx_*` / `scoring_*` / `confidence_*` /
    `llm_*` / 配额）只改 `app_setting`、不留痕，导致「改了什么」完全不可追溯。
    现在循环内逐个留痕，并记录**旧值 → 新值**（只记新值无法回答「原本是多少」）。
    """
    # H2 幂等：切 `live` / 改参数属于「关键操作」，给 request_key 就能防重复提交。
    # 注意只在**调用方给了 key** 时才走幂等（不带 key 的调用保持原行为，零影响）。
    key = payload.pop("request_key", None) if isinstance(payload, dict) else None
    if key:
        return idempotency.run_once(key, "scoring.settings",
                                    lambda: _apply_settings(payload))
    return _apply_settings(payload)


def _apply_settings(payload: dict):
    changed = []
    if "writeback_mode" in payload:
        m = str(payload["writeback_mode"]).strip().lower()
        if m not in ("live", "dry_run", "off"):
            return {"ok": False, "reason": "writeback_mode 只能是 live / dry_run / off"}
        # ⚠️ 保留原有 action 名（`writeback_mode`）**不改**成 `set_setting` ——
        # 已有审计查询/前端可能按这个名字过滤，改名字是行为变更而非重构。
        db.set_setting("score_writeback_mode", m)
        db.execute("INSERT INTO audit_log (actor, action, target, detail) VALUES (%s,%s,%s,%s)",
                   ("manual", "writeback_mode", m, db.as_json({"mode": m})))
        changed.append("score_writeback_mode")
    # ---- D2-1 等级标签开关：**独立于分数回写**，且默认 off ----
    # 为什么单独一个分支（而不是并进下面的通用循环）：需要校验取值，
    # 且要在审计里明确写「这是标签开关」—— 与分数开关混在一起事后查不清是谁开的。
    if "grade_tag_mode" in payload:
        gm = str(payload["grade_tag_mode"]).strip().lower()
        if gm not in ("live", "dry_run", "off"):
            return {"ok": False, "reason": "grade_tag_mode 只能是 live / dry_run / off"}
        old = db.get_setting("grade_tag_mode")
        if str(old or "off") != gm:
            db.set_setting("grade_tag_mode", gm)
            db.execute("INSERT INTO audit_log (actor, action, target, detail) "
                       "VALUES (%s,%s,%s,%s)",
                       ("manual", "grade_tag_mode", gm,
                        db.as_json({"mode": gm, "old": old or "off",
                                    "scope": "仅等级标签，不影响分数回写"})))
            changed.append("grade_tag_mode")
    for key, skey in (("max_sessions", "scoring_max_sessions"),
                      ("min_messages", "scoring_min_messages"),
                      ("fetch_tags", "scoring_fetch_tags"),
                      ("deadline_minutes", "scoring_deadline_minutes"),
                      ("max_llm_calls", "scoring_max_llm_calls"),
                      ("priority_order", "scoring_priority_order"),
                      ("llm_timeout_sec", "llm_timeout_sec"),
                      ("data_retention_days", "data_retention_days"),
                      ("eval_gate_enabled", "eval_gate_enabled"),
                      ("eval_baseline_path", "eval_baseline_path"),
                      ("morning_cron", "scoring_morning_cron"),
                      ("public_class_grade_scope", "public_class_grade_scope"),
                      ("writeback_delta_mode", "writeback_delta_mode"),
                      ("grade_tag_mode", "grade_tag_mode"),
                      ("grade_tag_group", "grade_tag_group"),
                      ("grade_tag_probe_customer", "grade_tag_probe_customer"),
                      ("writeback_quota_per_run", "writeback_quota_per_run"),
                      ("writeback_quota_per_customer_per_day",
                       "writeback_quota_per_customer_per_day"),
                      ("ctx_window_size", "ctx_window_size"),
                      ("ctx_drop_msgtypes", "ctx_drop_msgtypes"),
                      ("ctx_drop_pure_url", "ctx_drop_pure_url"),
                      ("ctx_drop_system", "ctx_drop_system"),
                      ("ctx_display_relative", "ctx_display_relative"),
                      ("ctx_min_kept", "ctx_min_kept"),
                      ("ctx_min_customer_msgs", "ctx_min_customer_msgs"),
                      ("ctx_min_effective_chars", "ctx_min_effective_chars"),
                      ("ctx_key_history", "ctx_key_history"),
                      ("ctx_key_history_max", "ctx_key_history_max"),
                      ("ctx_cold_start_early", "ctx_cold_start_early"),
                      ("ctx_delta_max", "ctx_delta_max"),
                      ("ctx_tokens_max", "ctx_tokens_max"),
                      ("ctx_memory", "ctx_memory"),
                      ("ctx_fallback_min", "ctx_fallback_min"),
                      ("llm_temperature", "llm_temperature"),
                      ("llm_max_tokens", "llm_max_tokens"),
                      ("llm_retry_attempts", "llm_retry_attempts"),
                      ("rejudge_retries", "rejudge_retries"),
                      ("session_max_failures", "session_max_failures"),
                      ("use_graph_session", "use_graph_session"),
                      ("use_outbox", "use_outbox"),
                      ("trace_enabled", "trace_enabled"),
                      ("confidence_review_min", "confidence_review_min"),
                      ("confidence_boundary_delta", "confidence_boundary_delta"),
                      ("confidence_flip_steps", "confidence_flip_steps"),
                      ("confidence_weak_weight", "confidence_weak_weight"),
                      ("review_engine_negative", "review_engine_negative")):
        if key in payload:
            old = db.get_setting(skey)
            db.set_setting(skey, payload[key])
            # 值没变则不留痕（避免「点一次保存刷一堆无意义记录」）
            if str(old) != str(payload[key]):
                _audit_setting(skey, old, payload[key])
                changed.append(skey)
    # 早会时刻 / 保留期 改了要**立即生效**（否则得重启后端，用户会以为设置没生效）
    if any(k in changed for k in ("scoring_morning_cron", "data_retention_days")):
        try:
            scheduler.reschedule_all()
        except Exception as e:                      # noqa: BLE001
            print(f"[settings] 重排定时任务失败（不影响配置保存）：{e}")
    return {"ok": True, "changed": changed, **get_settings()}


def _audit_setting(key: str, old, new) -> None:
    """写一条配置变更审计（**失败不影响配置生效** —— 审计是旁路观测）。"""
    try:
        db.execute("INSERT INTO audit_log (actor, action, target, detail) VALUES (%s,%s,%s,%s)",
                   ("manual", "set_setting", key,
                    db.as_json({"key": key, "old": None if old is None else str(old),
                                "new": None if new is None else str(new)})))
    except Exception as e:                      # noqa: BLE001
        print(f"[settings] 写 audit_log 失败（不影响配置）：{e}")
