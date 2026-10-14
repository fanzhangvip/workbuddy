# -*- coding: utf-8 -*-
"""客户意向评分智能体 —— 规则引擎 + 大模型，稳定 / 可靠 / 可评估 / 可观测。

对每个「员工—客户」关系，按需求文档 §5 的规则打分：

    ① 选候选：有足够消息，且「无评估 / 有新消息 / 规则版本变化」才需要重评
    ② 组输入：规则表 + 近期消息 + 规则引擎事实（连续 48h 未回复等）
    ③ 大模型：结构化输出命中的加分项 / 扣分项（含证据）
    ④ 校验：rule_id 与分值必须合法，非法项丢弃并留痕
    ⑤ 规则引擎：补充硬指标扣分项（48h 未回复），不由模型判断
    ⑥ 本地计分：rules.py 算总分与等级（**不让模型给总分**）
    ⑦ 落库：customer_intent_assessment
    ⑧ 分流：
         · 正分（加分合计 ≥1）→ **回写微伴评分接口**（默认 dry_run，幂等）
         · 负分项非空        → 写入 negative_review **人工复核队列**（未打分）
    ⑨ scoring_run：全程记录，可观测

可靠性设计
    · 大模型 / 微伴接口均带指数退避重试
    · 单会话失败不阻塞整体（计数 + 继续）
    · 回写幂等：UNIQUE(assessment_id)
    · 规则版本变化自动触发全量重评
    · 增量水位：assessment.last_msg_time 与会话 last_msg_time 比较
"""
import threading
import time
import traceback
from dataclasses import replace

import config
import db

import analyze                      # noqa: E402  根目录既有模块（大模型分析）
import contracts                    # noqa: E402  模型输出的双层契约（阶段A）
import ctx_preprocess               # noqa: E402  上下文预处理层（统一「消息 → 文本」）
import llm                          # noqa: E402  异常分层：可重试 vs 不可重试
import outbox                       # noqa: E402  回写 outbox（修 G6：回写与评分解耦）
import rules as rules_mod           # noqa: E402
import weiban                       # noqa: E402

KNOWN_TAGS_ATTENDED = ("到课", "完课", "已上课", "已到课", "已完课")
SLEEP_BETWEEN_CALLS = 0.0           # 秒；由 config 覆盖


# ============================================================ 规则版本

def sync_rules(version: str = None) -> dict:
    """把 rules.json 写入 scoring_rule（按版本覆盖），实现规则版本化。"""
    rules = rules_mod.load_rules()
    version = version or rules.get("rule_version") or "v1.0"
    rows = []
    for polarity, key in (("positive", "positive_rules"), ("negative", "negative_rules")):
        for r in rules.get(key, []):
            for opt in r.get("options", []):
                rows.append((version, r["rule_id"], polarity, r.get("dimension", ""),
                             int(opt.get("score", 0)), opt.get("criteria"),
                             r.get("engine"), "json"))
    with db.tx() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM scoring_rule WHERE rule_version = %s", (version,))
            for row in rows:
                cur.execute(
                    """INSERT INTO scoring_rule
                         (rule_version, rule_id, polarity, dimension, score, criteria,
                          engine, source_type)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (rule_version, rule_id, score) DO NOTHING""", row)
    return {"rule_version": version, "rules": len(rows)}


def current_rule_version() -> str:
    return rules_mod.load_rules().get("rule_version") or "v1.0"


def setting(key: str, env_key: str, default=None):
    """运行时设置（app_setting）优先，其次 .env，最后默认值。"""
    v = db.get_setting(key)
    if v is None or v == "":
        v = config.get(env_key)
    return default if v is None or v == "" else v


def setting_int(key: str, env_key: str, default: int = 0) -> int:
    try:
        return int(setting(key, env_key, default))
    except Exception:
        return default


def setting_bool(key: str, env_key: str, default: bool = False) -> bool:
    return str(setting(key, env_key, str(default))).strip().lower() in ("1", "true", "yes", "on")


def setting_float(key: str, env_key: str, default: float = 0.0) -> float:
    try:
        return float(setting(key, env_key, default))
    except Exception:
        return default


# ============================================================ 事实（规则引擎）

def compute_facts(staff_id: str, session_id: str, fetch_tags: bool = False) -> dict:
    """计算给大模型/规则引擎用的事实。

    - 连续未回复时长：由后台按消息时间计算，不由大模型判断（需求 §5.5）
    - 标签（到课/完课）：优先取微伴自定义字段，作为「邀课成功」的佐证
    """
    last = db.query_one(
        """SELECT direction, msg_time, from_name FROM chat_message
            WHERE staff_id = %s AND session_id = %s
            ORDER BY msg_time DESC, message_id DESC LIMIT 1""",
        (staff_id, session_id))
    now = int(time.time())
    hours, no_reply = None, False
    if last and last.get("msg_time"):
        hours = round((now - int(last["msg_time"])) / 3600, 1)
        # 最后一条是员工发出、且超过 48 小时无客户回复
        no_reply = last.get("direction") == "send" and hours >= 48

    lines = []
    if hours is not None:
        lines.append(f"距最后一条消息 {hours} 小时；最后发言方为"
                     f"{'员工' if last.get('direction') == 'send' else '客户'}。")
    lines.append(f"是否满足「连续 48 小时未回复」（规则引擎判定）：{'是' if no_reply else '否'}。")

    tags, tag_text = [], ""
    if fetch_tags:
        try:
            fields = weiban.get_custom_fields(staff_id, session_id)  # 会话 id 即客户 id
            for f in fields:
                name = f.get("field_name") or ""
                val = f.get("field_value")
                opts = "、".join(o.get("option_name", "") for o in (f.get("option_list") or []))
                tags.append({"name": name, "value": val or opts, "type": f.get("field_type")})
            if tags:
                tag_text = "；".join(f"{t['name']}={t['value']}" for t in tags if t["value"])
                lines.append(f"微伴客户标签/自定义字段：{tag_text}")
        except Exception:
            pass

    return {"text": "\n".join(lines) or "无额外事实。", "hours": hours,
            "no_reply_48h": no_reply, "last_direction": (last or {}).get("direction"),
            "tags": tags, "tag_text": tag_text}


# ============================================================ 上下文预处理（委托 ctx_preprocess）

def _ctx_options() -> "ctx_preprocess.CtxOptions":
    """从运行时设置构造预处理层选项（app_setting > .env > 默认）。"""
    return ctx_preprocess.CtxOptions(
        window=setting_int("ctx_window_size", "CTX_WINDOW_SIZE", 60),
        drop_msgtypes=ctx_preprocess.parse_msgtypes(
            setting("ctx_drop_msgtypes", "CTX_DROP_MSGTYPES", "31,9")),
        drop_pure_url=setting_bool("ctx_drop_pure_url", "CTX_DROP_PURE_URL", True),
        drop_system=setting_bool("ctx_drop_system", "CTX_DROP_SYSTEM", True),
        display_relative=setting_bool("ctx_display_relative", "CTX_DISPLAY_RELATIVE", True),
    )


def build_context(staff_id: str, session_id: str, limit: int = None,
                  session_meta: dict = None) -> dict:
    """取会话消息 → 交给预处理层规范化 → 返回完整上下文契约。

    本函数只负责「取数」（纯 SQL）；清洗、降噪、编号、时间、窗口全部由
    ``ctx_preprocess`` 决定，避免「消息 → 文本」逻辑再次散落。
    多取 3 倍原始行：规范化阶段会丢弃纯链接等噪声，避免丢完不够窗口条数。
    """
    opts = _ctx_options()
    if limit:
        opts = replace(opts, window=int(limit))

    rows = db.query(
        """SELECT message_id, msgid, direction, msg_time, from_name, content,
                  msgtype, content_state, plain
             FROM chat_message
            WHERE staff_id = %s AND session_id = %s
              AND (plain IS NOT NULL
                   OR (content IS NOT NULL AND content <> ''))
            ORDER BY msg_time DESC, message_id DESC LIMIT %s""",
        (staff_id, session_id, max(int(opts.window or 60), 1) * 3))
    rows.reverse()
    return ctx_preprocess.build_llm_context(rows, session_meta=session_meta, opts=opts)


def build_conversation(staff_id: str, session_id: str, limit: int = 60) -> str:
    """取最近 limit 条消息拼成给大模型的文本（保留原签名，内部已走预处理层）。"""
    return build_context(staff_id, session_id, limit=limit)["text"]


# ============================================================ 服务

class ScoringService:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = False
        self._run_id = None
        self._progress = {}

    # ---------------- 对外接口 ----------------

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            run_id = self._run_id
        run = db.query_one("SELECT * FROM scoring_run WHERE id = %s", (run_id,)) if run_id else None
        if not run:
            run = db.query_one("SELECT * FROM scoring_run ORDER BY id DESC LIMIT 1")
        return {"running": running, "stopping": self._stop, "run": run,
                "progress": dict(self._progress),
                "rule_version": current_rule_version(),
                "writeback_mode": self.writeback_mode()}

    def stop(self) -> dict:
        if not self.is_running():
            return {"ok": False, "reason": "当前没有正在运行的评分任务"}
        self._stop = True
        return {"ok": True, "message": "已发出停止信号，将在当前会话处理完后退出"}

    @staticmethod
    def writeback_mode() -> str:
        """回写模式：live（真实写入）/ dry_run（只记录）/ off（关闭）。

        优先级：前端运行时设置（app_setting）> .env > 默认 dry_run。
        """
        m = (db.get_setting("score_writeback_mode") or config.get("SCORE_WRITEBACK")
             or "dry_run").strip().lower()
        return m if m in ("live", "dry_run", "off") else "dry_run"

    def start(self, staff_ids: list = None, max_sessions: int = 0,
              min_messages: int = 0, force: bool = False,
              triggered_by: str = "manual") -> dict:
        if self.is_running():
            return {"ok": False, "reason": "已有评分任务在运行，请先等待完成"}
        if not config.LLM_API_KEY or not config.LLM_BASE_URL:
            return {"ok": False, "reason": "缺少大模型配置（LLM_BASE_URL / LLM_API_KEY）"}

        max_sessions = int(max_sessions or setting_int("scoring_max_sessions",
                                                       "SCORING_MAX_SESSIONS", 200))
        min_messages = int(min_messages or setting_int("scoring_min_messages",
                                                       "SCORING_MIN_MESSAGES", 4))
        rv = current_rule_version()
        sync_rules(rv)

        detail = db.as_json({"staff_ids": staff_ids, "max_sessions": max_sessions,
                             "min_messages": min_messages, "force": force})
        run_id = db.query_one(
            """INSERT INTO scoring_run (kind, status, triggered_by, rule_version, detail)
               VALUES (%s, 'running', %s, %s, %s) RETURNING id""",
            ("full" if force else "incremental", triggered_by, rv, detail))["id"]

        self._stop = False
        self._run_id = run_id
        self._progress = {"stage": "启动中", "current_target": None, "session_total": 0,
                          "session_done": 0, "session_skipped": 0, "assessment_new": 0,
                          "writeback_ok": 0, "writeback_fail": 0, "review_new": 0,
                          "llm_calls": 0, "llm_failed": 0, "session_failed": 0,
                          "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                          "ctx_total": 0, "ctx_kept": 0, "ctx_available": 0,
                          "ctx_dropped": 0, "ctx_truncated": 0, "ctx_drop_reasons": {}}
        self._thread = threading.Thread(
            target=self._run, args=(run_id, staff_ids, max_sessions, min_messages, force, rv),
            daemon=True, name=f"scoring-{run_id}")
        self._thread.start()
        return {"ok": True, "run_id": run_id, "rule_version": rv,
                "writeback_mode": self.writeback_mode()}

    # ---------------- 内部：进度 ----------------

    def _bump(self, run_id, *, detail=None, **deltas):
        with self._lock:
            for k, v in deltas.items():
                if v:
                    self._progress[k] = self._progress.get(k, 0) + v
            if detail:
                self._progress.update(detail)
            snap = dict(self._progress)
        db.execute(
            """UPDATE scoring_run SET session_done=%s, session_skipped=%s,
                   assessment_new=%s, writeback_ok=%s, writeback_fail=%s,
                   review_new=%s, llm_calls=%s, llm_failed=%s, detail=%s
                 WHERE id=%s""",
            (snap.get("session_done", 0), snap.get("session_skipped", 0),
             snap.get("assessment_new", 0), snap.get("writeback_ok", 0),
             snap.get("writeback_fail", 0), snap.get("review_new", 0),
             snap.get("llm_calls", 0), snap.get("llm_failed", 0),
             db.as_json(snap), run_id))
        return snap

    def _inc(self, **deltas):
        """只累加内存进度，**不触发** scoring_run 的 UPDATE。

        与 `_score_one` 原来那段 ``with self._lock:`` 自增完全等价（阶段B 图化时
        把它抽成方法，供图节点复用，保证统计口径一字不差）。
        """
        with self._lock:
            for k, v in deltas.items():
                if v:
                    self._progress[k] = self._progress.get(k, 0) + v

    # ---------------- 内部：候选会话 ----------------

    def _candidates(self, staff_ids, limit, min_messages, force, rv) -> list:
        """选出需要评分的会话（增量：无评估 / 有新消息 / 规则版本变化）。"""
        params = {"min_msgs": min_messages, "rv": rv, "limit": limit}
        staff_filter = ""
        if staff_ids:
            staff_filter = "AND c.staff_id = ANY(%(staff_ids)s)"
            params["staff_ids"] = list(staff_ids)

        sql = f"""
            SELECT c.staff_id, s.name AS staff_name, c.session_id, c.target_id,
                   c.name AS target_name, c.msg_count, c.last_msg_time,
                   last.assessment_id AS last_aid, last.a_last, last.rule_version AS last_rv
              FROM chat_session c
              LEFT JOIN staff s ON s.staff_id = c.staff_id
              LEFT JOIN LATERAL (
                   SELECT a.assessment_id, a.last_msg_time AS a_last, a.rule_version
                     FROM customer_intent_assessment a
                    WHERE a.relationship_key = c.staff_id || ':' || COALESCE(c.target_id, c.session_id)
                    ORDER BY a.assessment_id DESC LIMIT 1
              ) last ON true
             WHERE c.session_type = 'ext_user'
               AND c.target_id IS NOT NULL
               AND c.msg_count >= %(min_msgs)s
               {staff_filter}
               AND (last.assessment_id IS NULL
                    OR last.a_last IS NULL
                    OR last.a_last < c.last_msg_time
                    OR last.rule_version IS DISTINCT FROM %(rv)s)
             ORDER BY c.last_msg_time DESC NULLS LAST
             LIMIT %(limit)s
        """
        return db.query(sql, params)

    # ---------------- 内部：大模型 ----------------

    def _llm_analyze(self, ctx: dict, facts_text: str, rules: dict) -> dict:
        """结构化分析：**按异常类型分流重试**（修 A2）。

        改造前是 ``except Exception`` 一把抓重试 3 次 —— 一次 401 会白等
        1.2 + 2.4 + 4.8 秒，200 个会话就是近半小时纯浪费，最后整轮全挂。
        现在：临时性故障（网络/限流/5xx/输出被截断）退避重试；
        确定性故障（认证/参数错误/契约连续不满足）立即上抛，由 ``_run`` 记失败明细转人工。

        同时把 token 用量累加进 ``_progress``（修 H5：此前完全没有成本指标）。
        """
        attempts = max(1, setting_int("llm_retry_attempts", "LLM_RETRY_ATTEMPTS", 3))
        temperature = setting_float("llm_temperature", "LLM_TEMPERATURE",
                                    analyze.DEFAULT_TEMPERATURE)
        max_tokens = setting_int("llm_max_tokens", "LLM_MAX_TOKENS", 0) or None
        max_repairs = setting_int("rejudge_retries", "REJUDGE_RETRIES",
                                  analyze.DEFAULT_MAX_REPAIRS)

        def _on_attempt(_n, meta):
            usage = (meta or {}).get("usage") or {}
            with self._lock:
                for key, src in (("prompt_tokens", "prompt_tokens"),
                                 ("completion_tokens", "completion_tokens"),
                                 ("total_tokens", "total_tokens")):
                    self._progress[key] = self._progress.get(key, 0) + int(usage.get(src) or 0)

        last_err = None
        for i in range(attempts):
            try:
                return analyze.analyze(
                    ctx.get("text", ""), facts_text, rules,
                    evidence_index=ctx.get("index"),
                    labels=ctx.get("labels"),
                    current_date=ctx.get("current_date"),
                    temperature=temperature,
                    max_tokens=max_tokens,
                    max_repairs=max_repairs,
                    on_attempt=_on_attempt)
            except llm.RetryableLLMError as e:
                last_err = e
                time.sleep(1.2 * (2 ** i))
            except (llm.FatalLLMError, contracts.ContractViolationError):
                raise                                  # 重试一万次也没用，直接转人工
        raise RuntimeError(f"大模型分析连续 {attempts} 次失败（临时故障）：{last_err}")

    @staticmethod
    def _engine_rule_ids(rules: dict) -> set:
        """由规则引擎硬算、不允许大模型判定的规则（如 48h 未回复）。"""
        return {r["rule_id"] for r in rules.get("negative_rules", [])
                if r.get("engine") == "rule"}

    def _engine_negative_items(self, facts: dict, rules: dict) -> list:
        """规则引擎补充的硬指标扣分项（不由大模型判断）。"""
        items = []
        if facts.get("no_reply_48h"):
            items.append({
                "rule_id": "negative_no_reply_2d", "score": -2,
                "evidence": [f"距最后一条员工消息已 {facts.get('hours')} 小时无客户回复"],
                "reason": "规则引擎：销售正常跟进后，客户连续 48 小时未回复（如客户已说明忙/晚点聊，人工可驳回）",
            })
        valid, _bad = rules_mod.validate_items(items, rules)
        return valid

    # ---------------- 内部：回写微伴 ----------------

    def _writeback(self, run_id, rel_key, aid, staff_id, target_id, score, mode, review_needed):
        """正分回写微伴（幂等：一次评估只写一次）。

        阶段B 起，live 模式**只同步试一次**：失败不再原地退避 3 次（最坏拖 5.6s/会话），
        而是交给 ``writeback_outbox`` 的 drain worker 按退避异步重试（修 G6）。
        这样批量评分不会被微伴抖动拖慢，同时回写也不会因为增量水位推进而永久丢失。
        """
        status, errcode, errmsg, resp = "skipped", None, "无正分，无需回写", None
        if score >= 1:
            if mode == "off":
                status, errmsg = "skipped", "回写功能已关闭"
            elif mode == "dry_run":
                status, errmsg = "dry_run", "演练模式：仅记录，未真实写入"
            else:  # live：单次投递，重试交给 outbox
                try:
                    resp = weiban.add_customer_score(staff_id, external_user_id=target_id,
                                                     score=int(score), use_header_token=True)
                    errcode = resp.get("errcode")
                    errmsg = resp.get("errmsg")
                    status = "ok" if errcode == 0 else "failed"
                except Exception as e:
                    errmsg, status = f"{type(e).__name__}: {e}", "failed"
        try:
            db.execute(
                """INSERT INTO score_writeback
                     (relationship_key, assessment_id, staff_id, external_user_id, score,
                      mode, status, attempts, errcode, errmsg, response)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (assessment_id) DO NOTHING""",
                (rel_key, aid, staff_id, target_id, score, mode, status,
                 1 if status in ("ok", "failed") else 0, errcode, errmsg,
                 db.as_json(resp) if resp else None))
            db.execute("UPDATE customer_intent_assessment SET writeback_status=%s WHERE assessment_id=%s",
                       (status, aid))
        except Exception as e:
            print(f"[scoring] 回写记录失败：{e}")

        # 同步 outbox 状态（未入队则静默跳过）：成功置 done，失败进入退避待 drain 重试
        if status in ("ok", "failed"):
            outbox.mark_result(aid, status == "ok", errcode, errmsg, resp)
        return status

    # ---------------- 内部：人工复核队列 ----------------

    def _enqueue_review(self, aid, cand, neg_items, raw_score, grade, writeback_status):
        if not neg_items:
            db.execute("UPDATE customer_intent_assessment SET review_status='na' WHERE assessment_id=%s", (aid,))
            return False
        rel = f"{cand['staff_id']}:{cand['target_id']}"
        db.execute(
            """INSERT INTO negative_review
                 (relationship_key, assessment_id, staff_id, staff_name, target_id,
                  target_name, session_id, ai_negative_total, ai_negative_items,
                  status, raw_score, grade, last_msg_time)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s)
               ON CONFLICT (assessment_id) DO NOTHING""",
            (rel, aid, cand["staff_id"], cand.get("staff_name"), cand["target_id"],
             cand.get("target_name"), cand["session_id"],
             sum(i.get("score", 0) for i in neg_items), db.as_json(neg_items),
             raw_score, grade, cand.get("last_msg_time")))
        db.execute("UPDATE customer_intent_assessment SET review_status='pending' WHERE assessment_id=%s", (aid,))
        return True

    # ---------------- 内部：预处理层观测 ----------------

    def _bump_ctx(self, stats: dict):
        """累计预处理层的观测指标（丢弃条数 / 原因 / 是否截断）。

        随 ``scoring_run.detail`` 落库，用来回答「这轮到底丢了多少、为什么丢」。
        """
        with self._lock:
            for key, val in (("ctx_kept", stats.get("kept", 0)),
                             ("ctx_available", stats.get("available", 0)),
                             ("ctx_dropped", stats.get("dropped", 0))):
                self._progress[key] = self._progress.get(key, 0) + (val or 0)
            if stats.get("truncated"):
                self._progress["ctx_truncated"] = self._progress.get("ctx_truncated", 0) + 1
            if stats.get("total"):
                self._progress["ctx_total"] = self._progress.get("ctx_total", 0) + stats["total"]
            reasons = self._progress.setdefault("ctx_drop_reasons", {})
            for k, v in (stats.get("drop_reasons") or {}).items():
                reasons[k] = reasons.get(k, 0) + v

    # ---------------- 内部：单会话失败明细 ----------------

    def _record_failure(self, run_id, cand, err):
        """把单会话失败写进 ``session_failure``（修 G5：失败要有载体）。

        改造前失败只 ``print`` 到控制台——没有一张表记录"哪个会话失败、失败几次"，
        于是「连续失败太多次就交给人工」这条护栏根本无法实现。
        现在每次失败落一条明细，``attempts`` 按 (staff_id, session_id) 累计，
        超过阈值把旧记录标 dead，供后续的重投 worker 或人工接手。
        """
        try:
            rel_staff, rel_sid = cand["staff_id"], cand["session_id"]
            stage = ("validate_contract" if isinstance(err, contracts.ContractViolationError)
                     else "llm_judge" if isinstance(err, llm.LLMError)
                     else "prepare_context")
            prev = db.query_one(
                """SELECT COALESCE(MAX(attempts), 0) AS n FROM session_failure
                    WHERE staff_id = %s AND session_id = %s""",
                (rel_staff, rel_sid)) or {}
            attempts = int(prev.get("n") or 0) + 1
            max_attempts = setting_int("session_max_failures", "SESSION_MAX_FAILURES", 3)
            status = "dead" if attempts >= max_attempts else "pending"
            payload = {}
            if isinstance(err, contracts.ContractViolationError):
                payload = {"errors": getattr(err, "errors_text", ""),
                           "raw_head": (getattr(err, "raw", "") or "")[:500]}
            db.execute(
                """INSERT INTO session_failure
                     (run_id, staff_id, session_id, stage, error_class, error_message,
                      payload, attempts, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (run_id or None, rel_staff, rel_sid, stage, type(err).__name__,
                 str(err)[:2000], db.as_json(payload) if payload else None,
                 attempts, status))
        except Exception as e:                        # 记失败本身失败，绝不能拖垮整轮
            print(f"[scoring] 写入失败明细时出错：{e}")

    # ---------------- 内部：落库（assessment + outbox 同一事务）----------------

    def _persist_assessment(self, run_id, cand, res, score, grade, needs_review,
                            unchecked, pos_items, neg_items, mode) -> int:
        """落 assessment，并在**同一事务**内把「待回写」写进 outbox（修 G6）。

        为什么必须同事务：``db.pool()`` 是 autocommit，``db.execute`` 每条各自一条连接、
        不具备事务性。若先落 assessment、再单独入队 outbox，中途崩溃就会出现
        「增量水位已推进，却没有待回写任务」—— 正是 G6（回写永久丢失）。

        注意 ``source_run_id`` 写 ``(run_id or None)``：人工单会话重评传 run_id=0，
        落 NULL 即绕开 ``uq_assessment_run_session``，允许对同一会话反复重评。
        """
        rel = f"{cand['staff_id']}:{cand['target_id']}"
        with db.tx() as conn:
            aid = conn.execute(
                """INSERT INTO customer_intent_assessment
                     (relationship_key, staff_id, staff_name, target_id, target_name, session_id,
                      raw_score, positive_total, negative_total, grade,
                      positive_items, negative_items, invalid_items,
                      profile_summary, sales_strategy, confidence, needs_human_review,
                      rule_version, msg_count, last_msg_time,
                      unchecked_rules, prompt_version, schema_version, model_confidence,
                      source_run_id, contract_attempts)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s)
                   RETURNING assessment_id""",
                (rel, cand["staff_id"], cand.get("staff_name"), cand["target_id"],
                 cand.get("target_name"), cand["session_id"],
                 score["raw_score"], score["positive_total"], score["negative_total"],
                 grade["grade"], db.as_json(pos_items), db.as_json(neg_items),
                 db.as_json(res.get("invalid_items") or []),
                 res.get("profile_summary"), res.get("sales_strategy"),
                 # confidence 列保留写模型自报值仅为兼容前端现有渲染；
                 # 代码计算的置信度是后续项，届时这一列改由本地算（见《落地实施方案》）。
                 res.get("model_confidence"), needs_review,
                 res.get("rule_version"), cand.get("msg_count"), cand.get("last_msg_time"),
                 db.as_json(unchecked), res.get("prompt_version"), res.get("schema_version"),
                 res.get("model_confidence"),
                 # 人工单会话重评（run_id=0）不参与唯一约束，可反复重评
                 (run_id or None), int(res.get("contract_attempts") or 1))
            ).fetchone()["assessment_id"]

            # 仅 live + 有正分才入队（dry_run / off 走同步记账，与改造前行为一致）
            if (score["positive_total"] >= 1 and mode == "live"
                    and setting_bool("use_outbox", "USE_OUTBOX", True)):
                outbox.enqueue(conn, aid, rel, cand["staff_id"], cand["target_id"],
                               score["positive_total"], mode)
        return aid

    # ---------------- 内部：评一个会话 ----------------

    def _score_one(self, run_id, cand, rules, min_messages, mode, fetch_tags) -> str:
        staff_id, session_id = cand["staff_id"], cand["session_id"]
        rel = f"{staff_id}:{cand['target_id']}"

        ctx = build_context(staff_id, session_id,
                            session_meta={"staff_id": staff_id, "session_id": session_id})
        min_kept = setting_int("ctx_min_kept", "CTX_MIN_KEPT", 4)
        if ctx["stats"]["kept"] < min_kept:      # 规范化后有效条数太少，跳过（不计入 llm 调用）
            return "skipped"
        self._bump_ctx(ctx["stats"])

        facts = compute_facts(staff_id, session_id, fetch_tags=fetch_tags)
        self._bump(run_id, llm_calls=1)
        res = self._llm_analyze(ctx, facts["text"], rules)

        pos_items = res.get("positive_items") or []
        # engine=rule 的规则已在 rules.derive_items 里排除，这里再兜一层（防御契约回退）
        engine_ids = self._engine_rule_ids(rules)
        neg_items = [i for i in (res.get("negative_items") or [])
                     if i.get("rule_id") not in engine_ids]
        neg_items += self._engine_negative_items(facts, rules)   # 规则引擎硬指标

        score = rules_mod.compute_score(pos_items, neg_items, rules)
        grade = rules_mod.grade_of(score["raw_score"], rules)

        unchecked = list(res.get("unchecked_rules") or [])
        # 「要不要人看一眼」由代码判定（负分项存在 / 有规则被漏检），
        # 不再采信模型自报的 needs_human_review（修 E4：不能让可能出错的组件自证清白）
        needs_review = bool(neg_items or unchecked)

        aid = self._persist_assessment(run_id, cand, res, score, grade, needs_review,
                                       unchecked, pos_items, neg_items, mode)

        # 分流①：正分回写微伴
        wb = self._writeback(run_id, rel, aid, staff_id, cand["target_id"],
                             score["positive_total"], mode, bool(neg_items))

        # 分流②：负分进人工复核队列
        need_review = self._enqueue_review(aid, cand, neg_items, score["raw_score"],
                                           grade["grade"], wb)

        with self._lock:
            self._progress["assessment_new"] = self._progress.get("assessment_new", 0) + 1
            if wb in ("ok", "dry_run"):
                self._progress["writeback_ok"] = self._progress.get("writeback_ok", 0) + 1
            elif wb == "failed":
                self._progress["writeback_fail"] = self._progress.get("writeback_fail", 0) + 1
            if need_review:
                self._progress["review_new"] = self._progress.get("review_new", 0) + 1
        return "scored"

    # ---------------- 内部：灰度分派（旧串行 vs 新图）----------------

    def _score_cand(self, run_id, cand, rules, min_messages, mode, fetch_tags) -> str:
        """按运行时开关分派：新图路径（阶段B）或旧串行实现（一键回滚点）。

        ``use_graph_session`` 默认 False → 行为与改造前完全一致；
        开启后走 ``graph_session.run_session``，节点是旧逻辑的逐字搬运。
        """
        if setting_bool("use_graph_session", "USE_GRAPH_SESSION", False):
            import graph_session                        # 延迟导入：不图化时零开销
            return graph_session.run_session(self, run_id, cand, rules,
                                             min_messages, mode, fetch_tags)
        return self._score_one(run_id, cand, rules, min_messages, mode, fetch_tags)

    # ---------------- 内部：主流程 ----------------

    def _run(self, run_id, staff_ids, max_sessions, min_messages, force, rv):
        t0 = time.time()
        try:
            db.init_db()
            rules = rules_mod.load_rules()
            mode = self.writeback_mode()
            fetch_tags = setting_bool("scoring_fetch_tags", "SCORING_FETCH_TAGS", False)
            sleep_ms = setting_int("scoring_sleep_ms", "SCORING_SLEEP_MS", 0)

            self._bump(run_id, detail={"stage": "筛选候选会话"})
            cands = self._candidates(staff_ids, max_sessions, min_messages, force, rv)
            n_staff = len({c["staff_id"] for c in cands})
            db.execute("UPDATE scoring_run SET staff_count=%s, session_total=%s WHERE id=%s",
                       (n_staff, len(cands), run_id))
            self._bump(run_id, detail={"stage": f"开始评分（共 {len(cands)} 个会话）",
                                       "session_total": len(cands)})

            for i, cand in enumerate(cands, 1):
                if self._stop:
                    break
                label = f"{cand.get('target_name') or cand['target_id']}"
                self._bump(run_id, detail={"stage": f"评分 ({i}/{len(cands)})",
                                           "current_target": label,
                                           "current_staff": cand.get("staff_name"),
                                           "session_total": len(cands)})
                try:
                    r = self._score_cand(run_id, cand, rules, min_messages, mode, fetch_tags)
                    if r == "skipped":
                        self._bump(run_id, session_skipped=1)
                    else:
                        self._bump(run_id, session_done=1)
                except Exception as e:
                    # 修 G5：失败**不再算「已完成」**。改造前这里同时加 llm_failed 和
                    # session_done，完成率永远好看，从数字上看不出这轮挂了几个。
                    print(f"[scoring] 会话 {cand['session_id']} 评分失败：{e}")
                    self._bump(run_id, llm_failed=1, session_failed=1)
                    self._record_failure(run_id, cand, e)
                if sleep_ms:
                    time.sleep(sleep_ms / 1000.0)

            status = "stopped" if self._stop else "success"
            elapsed = round(time.time() - t0, 1)
            db.execute("UPDATE scoring_run SET status=%s, elapsed_sec=%s, finished_at=now(), detail=%s WHERE id=%s",
                       (status, elapsed, db.as_json({**self._progress, "stage": "已完成"}), run_id))
            print(f"[scoring] run={run_id} {status} 用时 {elapsed}s（评分 "
                  f"{self._progress.get('assessment_new', 0)}，复核 "
                  f"{self._progress.get('review_new', 0)}）")
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[scoring] run={run_id} 失败：{e}\n{tb}")
            db.execute("UPDATE scoring_run SET status='failed', error=%s, finished_at=now() WHERE id=%s",
                       (str(e)[:2000], run_id))
            self._bump(run_id, detail={"stage": "失败", "error": str(e)})
        finally:
            self._stop = False

    # ---------------- 单个会话即时评分（人工触发重算）----------------

    def score_session(self, staff_id: str, session_id: str) -> dict:
        """对单个会话立即评分（供「重新评分」按钮使用）。"""
        cand = db.query_one(
            """SELECT c.staff_id, s.name AS staff_name, c.session_id, c.target_id,
                      c.name AS target_name, c.msg_count, c.last_msg_time
                 FROM chat_session c LEFT JOIN staff s ON s.staff_id = c.staff_id
                WHERE c.staff_id = %s AND c.session_id = %s""",
            (staff_id, session_id))
        if not cand:
            return {"ok": False, "reason": "会话不存在"}
        if not cand.get("target_id"):
            return {"ok": False, "reason": "该会话缺少客户 id，无法评分"}
        rules = rules_mod.load_rules()
        try:
            r = self._score_cand(0, cand, rules, 1, self.writeback_mode(),
                                 setting_bool("scoring_fetch_tags", "SCORING_FETCH_TAGS", False))
        except Exception as e:
            return {"ok": False, "reason": f"评分失败：{e}"}
        if r == "skipped":
            return {"ok": False, "reason": "正文内容过少，无法评分"}
        row = db.query_one(
            """SELECT * FROM customer_intent_assessment WHERE relationship_key=%s
                ORDER BY assessment_id DESC LIMIT 1""",
            (f"{cand['staff_id']}:{cand['target_id']}",))
        return {"ok": True, "assessment": row}


service = ScoringService()
