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
import security
import settings_registry
import audit
import policy
import task_runtime
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


# ---------------- Schema 漂移检测（部分替代迁移框架，评审 P1-3）----------------

@router.get("/schema-check")
def schema_check_ep():
    """对比 `schema.sql`（权威）与当前库的实际列，报告漂移。

    为什么需要它：DDL 曾散在三处（`schema.sql` + `failure_worker.ensure_columns()` +
    `idempotency._ensure_table()`），「新库建对了、老库缺列」只有跑到那行代码才暴露。
    现在把「漂移」从「运行时才炸」变成「一条查询可见」。
    """
    import schema_check
    return schema_check.check()


@router.get("/defaults-manifest")
def defaults_manifest_ep():
    """默认值清单：**默认开启的保护** vs **默认关闭的观测/优化**（评审 §5.5）。

    评审原话：「『防御措施存在但未接线』这类状态比没写更危险，
    因为看代码的人会以为已经防住了。」本端点把「谁默认开、属于哪一类、
    哪个定时任务真的接了线」变成可查的事实，不用读 50 个键的源码去猜。
    """
    import defaults_manifest
    return defaults_manifest.verify()


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
            ss._audit(security.actor(), "rule_publish_blocked", f"rv:{ss.current_rule_version()}",
                      {"diffs": dec.get("diffs")})
            return {"ok": False, "reason": dec.get("reason"), "gate": dec}
        res = ss.sync_rules()
        ss._audit(security.actor(), "rule_publish", f"rv:{res.get('rule_version')}",
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
        ss._audit(security.actor(), "public_class_review", f"mode:{mode}",
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
    """运行时配置全量读取。

    ⚠️ **键/类型/默认值一律来自 `settings_registry`**（代码评审 P2-10）：
    改造前这里是 62 行手写 `ss.setting_int("ctx_window_size", "CTX_WINDOW_SIZE", 60)`，
    与写侧的 46 项白名单元组**各存一份** → 新增配置极易只改一侧。
    现在两侧同源，无法漂移。取值仍走 `ss.setting_*`（app_setting > .env > 默认），
    **行为逐字不变**（`tests/test_settings_registry.py` 做逐键等值对比）。

    少数「计算值 / 非 app_setting 来源」的键仍在此显式给出（见下方注释）。
    """
    out = {
        # ---- 计算值与非 app_setting 来源（刻意不进注册表）----
        "writeback_mode": service.writeback_mode(),      # 综合 app_setting + .env
        "env_writeback_mode": config.get("SCORE_WRITEBACK") or "dry_run",
        "runtime": db.all_settings(),                    # 原始运行时设置（排障用）
        "grade_tag_ids": grade_tag.get_tag_ids(),        # 标签 id 缓存（由 sync 写入）
        "grade_tag_probe_customer": db.get_setting(grade_tag.PROBE_KEY) or "",
        "prompt_version": analyze.PROMPT_VERSION,        # 代码常量（只读）
        "schema_version": contracts.SCHEMA_VERSION,      # 代码常量（只读）
    }
    # ---- 注册表派生的通用键（类型/默认值来自声明）----
    # ⚠️ 写进响应用的键名是 `api_key`（历史对外名），**不是** app_setting 键名 ——
    # 少数键两者不同（如 max_sessions ← scoring_max_sessions），用 app_setting 键名
    # 会直接破坏前端读取（改名是破坏性变更，不是重构）。
    fn = {settings_registry.INT: ss.setting_int,
          settings_registry.BOOL: ss.setting_bool,
          settings_registry.FLOAT: ss.setting_float,
          settings_registry.STR: ss.setting}
    for spec in settings_registry.SETTINGS:
        out[spec.api_key] = settings_registry.typed_get(spec, fn[spec.type])
    return out


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
                   (security.actor(), "writeback_mode", m, db.as_json({"mode": m})))
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
                       (security.actor(), "grade_tag_mode", gm,
                        db.as_json({"mode": gm, "old": old or "off",
                                    "scope": "仅等级标签，不影响分数回写"})))
            changed.append("grade_tag_mode")
    # ⚠️ 白名单**不再手写** —— 与读侧同源（`settings_registry.WRITABLE`）。
    # 改造前这里是 46 项手写元组、读侧另有 62 行手工取值，两份清单靠约定同步：
    # 新增配置只改一侧就会「改了不生效」或「改完看不见」（评审 P2-10）。
    for key, skey in settings_registry.WRITABLE:
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
                   (security.actor(), "set_setting", key,
                    db.as_json({"key": key, "old": None if old is None else str(old),
                                "new": None if new is None else str(new)})))
    except Exception as e:                      # noqa: BLE001
        print(f"[settings] 写 audit_log 失败（不影响配置）：{e}")


# ============================================================================
# 生产化加固：任务运行时 / 审批 / 工具审计 / 策略分流（硬伤 3·4·5）
#   —— 这些能力**必须有接口**，否则就是"写好了但没接线"，
#      而那正是代码评审里点名的反模式（看代码的人以为防住了，实际没生效）。
# ============================================================================

@router.get("/tasks")
def list_tasks(status: str = "", kind: str = "", limit: int = 30):
    """任务列表（统一状态机视图：进度 / 检查点 / 预算消耗）。"""
    cond, params = ["1=1"], {}
    if status:
        cond.append("status = %(status)s")
        params["status"] = status
    if kind:
        cond.append("kind = %(kind)s")
        params["kind"] = kind
    params["limit"] = max(1, min(int(limit or 30), 200))
    items = db.query(
        f"""SELECT task_id, kind, status, actor_id, trigger_src, priority,
                   budget_calls, cost_calls, cost_tokens, deadline_at,
                   last_step_seq, progress, partial_reason, error_message,
                   resumed_from, heartbeat_at, created_at, started_at, finished_at
              FROM task WHERE {' AND '.join(cond)}
             ORDER BY task_id DESC LIMIT %(limit)s""", params)
    return {"items": items, "total": len(items)}


@router.get("/tasks/stats")
def task_stats(days: int = 30):
    """任务运行时观测：状态分布 / 待审批 / 步骤成败 / 允许的状态迁移表。

    ⚠️ 必须声明在 `/tasks/{task_id}` **之前**：FastAPI 按声明顺序匹配，
    否则 "stats" 会被当成 `task_id` 去转 int → 422。
    """
    return task_runtime.stats(days=days)


@router.get("/tasks/{task_id}")
def task_detail(task_id: int, steps: int = 50):
    """任务详情 + 步骤（检查点）明细 —— 回答「走到哪一步、下一步做什么」。"""
    t = task_runtime.get(task_id)
    if not t:
        return {"ok": False, "reason": f"任务 {task_id} 不存在"}
    rows = db.query(
        """SELECT seq, name, status, output_ref, next_step, attempts,
                  error_class, error_message, started_at, finished_at
             FROM task_step WHERE task_id=%s ORDER BY seq DESC LIMIT %s""",
        (task_id, max(1, min(int(steps or 50), 500))))
    pend = db.query(
        """SELECT request_id, tool_name, params_redacted, status, created_at
             FROM approval_request WHERE task_id=%s ORDER BY request_id DESC""",
        (task_id,))
    return {"task": t, "steps": rows, "approvals": pend}


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: int):
    """从检查点**新建**任务续跑（源任务历史不变）。

    返回里的 `done_steps` 是已成功的步骤名 —— 调用方据此跳过已完成的会话，
    这正是「续跑而不是从头再来」的落地。
    """
    return idempotency.run_once(None, "task.resume",
                                lambda: task_runtime.resume(task_id))


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, payload: dict = Body(default={})):
    """取消任务（终态，不可迁出）。"""
    try:
        task_runtime.transition(task_id, "cancelled",
                                reason=payload.get("reason") or "人工取消")
        return {"ok": True, "task_id": task_id, "status": "cancelled"}
    except task_runtime.TransitionError as e:
        return {"ok": False, "reason": str(e)}


@router.get("/approvals")
def list_approvals(status: str = "pending", limit: int = 50):
    """待审批列表（高风险动作被物理阻塞在这里，等人工签字）。"""
    rows = db.query(
        """SELECT request_id, task_id, step_id, tool_name, params_redacted,
                  requested_by, status, decided_by, note, created_at, expires_at
             FROM approval_request
            WHERE (%(status)s = '' OR status = %(status)s)
            ORDER BY (status = 'pending') DESC, request_id DESC
            LIMIT %(limit)s""",
        {"status": status, "limit": max(1, min(int(limit or 50), 200))})
    return {"items": rows}


@router.post("/approvals/{request_id}")
def decide_approval(request_id: int, payload: dict = Body(default={})):
    """审批：`{"approved": true, "note": "..."}`。通过 → 任务回 running；驳回 → cancelled。"""
    approve = bool(payload.get("approved"))
    return idempotency.run_once(
        payload.get("request_key"), f"approval.{request_id}",
        lambda: task_runtime.decide_approval(
            request_id, approve=approve, note=(payload.get("note") or "").strip()))


@router.get("/audit/tool")
def tool_audit_list(tool_name: str = "", actor_id: str = "",
                    decision: str = "", result_status: str = "",
                    days: int = 7, limit: int = 100):
    """工具调用审计查询（五问：谁 / 什么 / 何时 / 为何 / 结果）。参数已脱敏。"""
    rows = db.query(
        """SELECT call_id, created_at, actor_id, tool_name, params_redacted,
                  decision, decision_reason, approval_source, result_status,
                  errcode, errmsg, elapsed_ms, task_id, step_id
             FROM tool_audit
            WHERE created_at >= now() - (%(days)s || ' days')::interval
              AND (%(tool)s = '' OR tool_name = %(tool)s)
              AND (%(actor)s = '' OR actor_id = %(actor)s)
              AND (%(dec)s = '' OR decision = %(dec)s)
              AND (%(res)s = '' OR result_status = %(res)s)
            ORDER BY call_id DESC LIMIT %(limit)s""",
        {"days": str(int(days or 7)), "tool": tool_name, "actor": actor_id,
         "dec": decision, "res": result_status,
         "limit": max(1, min(int(limit or 100), 500))})
    return {"items": rows, "counts": audit.stats().get("counts")}


@router.post("/audit/export")
def tool_audit_export(payload: dict = Body(default={})):
    """导出某天的工具审计为 JSONL（合规留存：追加写、按天轮转、可离线校验）。

    导出**不删除** —— 删除属于保留期策略，两者解耦（避免"导出失败却已删库"）。
    """
    res = audit.export_day(payload.get("day"))
    res["verify"] = audit.verify_export(payload.get("day"))
    return res


@router.get("/audit/stats")
def audit_stats():
    """审计观测：总量 / 被拒数 / 失败数 / 今日量 / 各档保留期。"""
    return {**audit.stats(), "retention_tiers": audit.tiers()}


@router.get("/policy/stats")
def policy_stats(days: int = 30):
    """策略分流观测：三档占比 + 原因分布（回答「复核队列为什么这么大」）。"""
    return policy.stats(days=days)
