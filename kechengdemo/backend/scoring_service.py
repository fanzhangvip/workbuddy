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
import time
import traceback
from dataclasses import replace

import config
import db

import analyze                      # noqa: E402  根目录既有模块（大模型分析）
import confidence                   # noqa: E402  置信度三层合成（架构 第六章 E4）
import contracts                    # noqa: E402  模型输出的双层契约（阶段A）
import ctx_preprocess               # noqa: E402  上下文预处理层（统一「消息 → 文本」）
import hooks                        # noqa: E402  显式钩子（架构 §2.4 H3）；护栏判定本体在此
import llm                          # noqa: E402  异常分层：可重试 vs 不可重试
# 等级 → 微伴标签（决策 D2 第 1 条，独立链路，与分数回写完全分开）
import grade_tag                    # noqa: E402
import outbox                       # noqa: E402  回写 outbox（修 G6：回写与评分解耦）
import http_client                  # noqa: E402  统一退避（抖动 + Retry-After）
import audit                        # noqa: E402  工具调用审计（五问 + 脱敏）
import assessment_repo              # noqa: E402  评估结果表唯一写入口（P3）
import job_service                  # noqa: E402  批量任务骨架（与 SyncService 共用）
import policy                       # noqa: E402  Policy 三档策略（修硬伤 4）
import review_repo                  # noqa: E402  复核队列唯一写入口（P3）
import task_runtime                 # noqa: E402  任务运行时（外循环，修硬伤 3）
import writeback_repo               # noqa: E402  回写记账与水位唯一写入口（P3）
from job_service import HEARTBEAT_STALE_MINUTES, STALE_MINUTES   # noqa: F401 再导出
from scoring_context import (          # noqa: E402  再导出：调用点不变
    build_context, compute_facts, ctx_options as _ctx_options,
    recent_rows as _recent_rows)

#: Policy 拒绝回写时的中文文案（`hooks.REASON_CN` 里没有这两个原因）
_POLICY_WB_CN = {
    "policy_confirm": "策略：置信度处于中间档，需人工二次确认后才写入",
    "policy_human": "策略：置信度偏低或有阻断性复核原因，需人工复核后再写入",
}
import rules as rules_mod           # noqa: E402
import weiban                       # noqa: E402

#: 整轮护栏判定（G3 超时 / G4 预算）—— 本体在 `hooks`，此处 re-export 仅为兼容既有调用方
#: （`_run`、`tests/test_loop_guard.py` 都直接引用 ``ss.guard_reason``）。
guard_reason = hooks.guard_reason

# ============================================================ 规则版本

def sync_rules(version: str = None) -> dict:
    """把 rules.json 写入 scoring_rule（按版本覆盖），实现规则版本化。"""
    rules = rules_mod.load_rules()
    version = version or rules.get("rule_version") or "v1.0"
    rows = []
    for polarity, key in (("positive", "positive_rules"), ("negative", "negative_rules")):
        for r in rules.get(key, []):
            for opt in r.get("options", []):
                # 闸1/F5：把 examples / conflict_priority 一并同步（前端规则页与
                # 排障都要能看到「这条规则的边界示例到底是什么」）
                rows.append((version, r["rule_id"], polarity, r.get("dimension", ""),
                             int(opt.get("score", 0)), opt.get("criteria"),
                             r.get("engine"), "json",
                             db.as_json(opt.get("examples")) if opt.get("examples") else None,
                             (int(r["conflict_priority"])
                              if r.get("conflict_priority") is not None else None)))
    with db.tx() as conn:
        with conn.cursor() as cur:
            # ⚠️ 不再「先 DELETE 整个版本再 INSERT」：窗口期内 `/api/scoring/rules`
            # 会读到空列表，且并发跑第二轮会互相清表。改为逐行 upsert，
            # 最后只删掉「本版本里已不在 rules.json 中的档位」。
            for row in rows:
                cur.execute(
                    """INSERT INTO scoring_rule
                         (rule_version, rule_id, polarity, dimension, score, criteria,
                          engine, source_type, examples, conflict_priority)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (rule_version, rule_id, score) DO UPDATE SET
                         polarity = EXCLUDED.polarity,
                         dimension = EXCLUDED.dimension,
                         criteria = EXCLUDED.criteria,
                         engine = EXCLUDED.engine,
                         source_type = EXCLUDED.source_type,
                         examples = EXCLUDED.examples,
                         conflict_priority = EXCLUDED.conflict_priority""", row)
            if rows:
                cur.execute(
                    """DELETE FROM scoring_rule sr
                        WHERE sr.rule_version = %s
                          AND NOT EXISTS (
                              SELECT 1 FROM unnest(%s::text[], %s::int[])
                                AS k(rule_id, score)
                               WHERE k.rule_id = sr.rule_id AND k.score = sr.score)""",
                    (version, [r[1] for r in rows], [r[4] for r in rows]))
    return {"rule_version": version, "rules": len(rows)}


def current_rule_version() -> str:
    return rules_mod.load_rules().get("rule_version") or "v1.0"


# `setting*`（运行时设置读取）的**唯一实现已在 `db.py`**，本模块不再转发：
# 改造前它住在这里，于是 `sync_service` / `scheduler` / `audit` 为了读一个设置
# 得反过来 import 评分服务（依赖方向是反的）；现在各处直接调 `db.setting*`。


def _as_items(value) -> list:
    """JSONB 读回来可能是 list，也可能是 json 字符串 → 统一成「只含 dict 的 list」。

    实现已收敛到 `db.as_dict_list()`（原先这个知识在 5 处各写一遍）。
    保留本函数名是为了不动既有调用点（`db._as_items` 之类的外部引用）。
    """
    return db.as_dict_list(value)


#: 阈值实现的唯一位置已移到 `confidence.thresholds()`（它本就是该模块的常量）；
#: 这里保留旧名，调用点（`backfill` / 测试里的 `ss._confidence_thresholds()`）不变。
_confidence_thresholds = confidence.thresholds


def _last_assessment(rel_key: str) -> dict:
    """② 的数据源：该客户**最近一次**评估（架构 §4.5.1 的①段降级源）。

    本函数**不建新表** —— `customer_memory`（记忆方案 M0）尚未落地，按已拍板决议
    先用最新 `customer_intent_assessment` 拼「准档案」。等 M0 落地后，① 段改为读
    `customer_memory`，② 段仍读这里（两者语义不同：前者是"现在是什么"，后者是"上次判了什么"）。
    """
    row = db.query_one(
        """SELECT assessment_id, raw_score, positive_total, negative_total, grade,
                  positive_items, negative_items, profile_summary, sales_strategy,
                  msg_count, last_msg_time, rule_version, analyzed_at
             FROM customer_intent_assessment
            WHERE relationship_key = %s
            ORDER BY assessment_id DESC LIMIT 1""",
        (rel_key,))
    return row or {}


def build_conversation(staff_id: str, session_id: str, limit: int = 60) -> str:
    """取最近 limit 条消息拼成给大模型的文本（保留原签名，内部已走预处理层）。

    评分链路**不再调用**它（架构 V1）；这里保留为「老调用方 / 展示」的薄壳，
    语义就是「最近 N 条」，因此 ``delta=False``（不掺增量与关键历史）。
    """
    return build_context(staff_id, session_id, limit=limit,
                         key_history=False, delta=False)["text"]


def build_display_messages(staff_id: str, session_id: str, limit: int = 300) -> list:
    """**界面展示**用的结构化消息（含媒体地址），供前端渲染气泡。

    为什么不复用 ``build_conversation`` 的纯文本输出：
    那份文本是**喂给模型**的 —— 媒体会被压成 ``[图片]`` / ``[表情]`` 占位，且 URL 被
    **故意剥掉**（CDN 链接会过期，对语义判断也只是噪声）。人看的时候只看到 `[图片]`
    显然不对：「给模型看」和「给人看」是两种需求，不该共用一份渲染。

    返回字段：``{code, message_id, role, direction, msg_time, msgtype, type_name,
    text, is_media, media_url, media_name}`` —— 与 ``api_chat.messages`` 同构，
    前端可以复用同一套气泡/媒体渲染。
    """
    rows = _recent_rows(staff_id, session_id, int(limit or 300))
    ctx = ctx_preprocess.build_llm_context(
        rows, opts=replace(_ctx_options(), window=0))
    by_id = {str(r.get("message_id")): r for r in rows}
    out = []
    for m in ctx.get("messages") or []:
        row = by_id.get(str(m.get("message_id")), {})
        media = ctx_preprocess.media_of(row)
        out.append({
            "code": m.get("code"),
            "message_id": m.get("message_id"),
            "role": m.get("role"),
            "direction": m.get("direction"),
            "msg_time": m.get("msg_time"),
            "msgtype": m.get("msgtype"),
            "type_name": m.get("type_name"),
            "text": m.get("text"),
            "is_media": ctx_preprocess.is_media(m.get("msgtype")),
            "media_url": media.get("media_url") or "",
            "media_name": media.get("media_name") or "",
        })
    return out


# ============================================================ 服务

def _ledger_total(rel_key: str) -> int:
    """读客户级回写水位（D2-1）。实现已收敛到 `writeback_repo`（水位唯一写入口）。"""
    return writeback_repo.ledger_total(rel_key)


def _advance_ledger(rel_key: str, staff_id: str, target_id: str, aid: int, delta: int) -> None:
    """推进水位。实现已收敛到 `writeback_repo.advance_ledger`（与 outbox 同一份）。"""
    writeback_repo.advance_ledger(rel_key, staff_id, target_id, aid, delta)


def _already_written(aid: int) -> bool:
    """幂等闸。实现已收敛到 `writeback_repo.already_written`。"""
    return writeback_repo.already_written(aid)


def _customer_writes_today(target_id: str) -> int:
    """日配额闸用的计数。实现已收敛到 `writeback_repo.writes_today`。"""
    return writeback_repo.writes_today(target_id)


def _pending_writeback_score(aid: int, fallback: int) -> int:
    """这条评估**待投递**的分数（差值），读不到就退回正分整值。

    审批卡片上要显示"放行后会给客户加多少分"。同步路径入队时已经把差值**固化**
    进 `writeback_outbox.score`（出队重试不再重算，否则水位被别的任务推进后会算出不同的数），
    所以这里读那一行才是准的；读不到（未入队：配额/未开启 outbox）就用正分当参考值。
    """
    try:
        row = db.query_one(
            "SELECT score FROM writeback_outbox WHERE assessment_id=%s", (aid,))
        if row and row.get("score") is not None:
            return int(row["score"])
    except Exception as e:                          # noqa: BLE001
        print(f"[scoring] 读待投递分数失败（用正分代替）：{type(e).__name__}: {e}")
    return int(fallback or 0)


def repair_current_flags() -> dict:
    """自愈 `is_current` 的两种破损（**幂等**）。

    ⚠️ 实现已移到 `assessment_repo.repair_current_flags()`（该表唯一写入口）；
    这里保留同名函数是为了不动既有调用点（测试里有 32 处 `ss.repair_current_flags()`），
    且启动自愈（`main.py`）语义不变。
    """
    return assessment_repo.repair_current_flags()


class ScoringService(job_service.JobService):
    """评分批量任务。

    运行状态（本进程线程 / 跨进程 DB 行 / 心跳僵尸 / 绝对超时）与进度累加
    都在基类 `job_service.JobService` —— 与 `SyncService` **共用一份实现**，
    两边不再各写一遍（原先已经漂移：sync 版加锁读线程、scoring 版没加）。
    """

    TABLE = "scoring_run"

    def __init__(self):
        super().__init__()
        self._run_id = None
        #: 统一任务 id（外循环 / 检查点载体，修硬伤 3）。与 `_run_id` 一一对应。
        self._task_id = None

    # ---------------- 对外接口 ----------------

    # `is_running()` 继承自 `job_service.JobService`（跨进程 + 僵尸两级判定）

    def status(self) -> dict:
        with self._lock:
            run_id = self._run_id
        running = self.is_running()
        run = db.query_one("SELECT * FROM scoring_run WHERE id = %s", (run_id,)) if run_id else None
        if not run:
            # 后端可能刚重启（本进程没有 run_id）→ 取 DB 里在跑的 / 最新的一条，
            # 这样即便批量跑在别的进程，前端也能看到进度。
            run = (db.query_one("""SELECT * FROM scoring_run WHERE status='running'
                                    ORDER BY id DESC LIMIT 1""")
                   or db.query_one("SELECT * FROM scoring_run ORDER BY id DESC LIMIT 1"))
        return {"running": running, "stopping": self._stop, "run": run,
                "progress": dict(self._progress),
                "rule_version": current_rule_version(),
                "writeback_mode": self.writeback_mode()}

    def stop(self) -> dict:
        """发出停止信号。**含跨进程**：把 `stop_requested` 写进 DB，让跑在别的进程的
        `_run` 也能看到（只设进程内标志的话，后端点"停止"停不掉脚本/调度器里的批量）。"""
        if not self.is_running():
            return {"ok": False, "reason": "当前没有正在运行的评分任务"}
        self._stop = True
        with self._lock:
            rid = self._run_id
        if rid:
            db.execute("UPDATE scoring_run SET stop_requested=true WHERE id=%s", (rid,))
        else:
            db.execute("""UPDATE scoring_run SET stop_requested=true
                           WHERE id = (SELECT id FROM scoring_run WHERE status='running'
                                        ORDER BY id DESC LIMIT 1)""")
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
              triggered_by: str = "manual", grades: list = None) -> dict:
        if self.is_running():
            return {"ok": False, "reason": "已有评分任务在运行，请先等待完成"}
        # 互斥（修 H1）：同步正在跑时不得启动评分 —— 否则会读到「半截会话」
        # （消息数 / 最后时间都在变，结论不可复现）。
        # ⚠️ 判定走 `job_service.running_kinds()`（查库、跨进程可见），**不再 import
        # sync_service** —— 改造前两边互相 import（靠函数内延迟 import 才没炸），
        # 而「互斥」这个不变量本该由第三方中立处判定。
        if "sync" in job_service.running_kinds(exclude="scoring"):
            return {"ok": False, "reason": "同步任务进行中，请先等待同步完成再评分（避免读到半截数据）"}
        if not config.LLM_API_KEY or not config.LLM_BASE_URL:
            return {"ok": False, "reason": "缺少大模型配置（LLM_BASE_URL / LLM_API_KEY）"}

        max_sessions = int(max_sessions or db.setting_int("scoring_max_sessions",
                                                       "SCORING_MAX_SESSIONS", 200))
        min_messages = int(min_messages or db.setting_int("scoring_min_messages",
                                                       "SCORING_MIN_MESSAGES", 4))
        # ── Loop 护栏（架构 §2.2）：整轮总超时 + LLM 成本预算 ──────────────
        # 0 表示「不限」。实测教训：一次批量跑了 2.7 小时才跑完，中途无法中断 ——
        # 因为改造前只有「单会话内部重试耗时」自然累积，没有整轮总闸。
        deadline_min = db.setting_int("scoring_deadline_minutes", "SCORING_DEADLINE_MINUTES", 120)
        max_llm_calls = db.setting_int("scoring_max_llm_calls", "SCORING_MAX_LLM_CALLS", 500)
        rv = current_rule_version()
        # 闸6（架构 §5.4 / §2.7）：规则变了才需要过门禁 ——
        # 「改规则前必须跑评估集，指标不降才允许发布」。
        # ⚠️ 只在**规则版本真的变了**时检查（否则每次启动都查一遍纯属浪费）；
        #    且这里**不主动跑评估集**（那会真调模型、把启动拖成分钟级）——
        #    只读取已有基线 + 已有跑分结果判定，拿不到就如实说「无法判定」而不是假装通过。
        #    要「跑一遍再发布」请用 POST /api/scoring/rules/publish。
        prev_rv = db.query_one(
            """SELECT rule_version FROM scoring_run
                WHERE status IN ('success','partial') AND rule_version IS NOT NULL
                ORDER BY id DESC LIMIT 1""") or {}
        if (prev_rv.get("rule_version") and prev_rv["rule_version"] != rv
                and db.setting_bool("eval_gate_enabled", "EVAL_GATE_ENABLED", True)):
            try:
                import eval_runner
                dec = eval_runner.gate_decision(run_if_possible=False)
                if dec.get("blocked"):
                    audit.record_action("system", "rule_gate_blocked", f"rule_version:{rv}",
                           {"prev": prev_rv.get("rule_version"), "diffs": dec.get("diffs")})
                    return {"ok": False,
                            "reason": f"规则版本 {prev_rv.get('rule_version')} → {rv} "
                                      f"未通过回归门禁，已阻断启动（闸6）。"
                                      f"详见 /api/scoring/rules/gate",
                            "gate": dec}
            except Exception as e:              # 门禁自身异常不应拖垮评分启动
                print(f"[scoring] 门禁检查异常（放行）：{e}")
        sync_rules(rv)

        detail = db.as_json({"staff_ids": staff_ids, "max_sessions": max_sessions,
                             "min_messages": min_messages, "force": force,
                             "grades": grades})
        run_id = db.query_one(
            """INSERT INTO scoring_run (kind, status, triggered_by, rule_version, detail,
                                        deadline, max_llm_calls)
               VALUES (%s, 'running', %s, %s, %s,
                       CASE WHEN %s > 0 THEN now() + make_interval(mins => %s) END,
                       NULLIF(%s, 0)) RETURNING id""",
            ("full" if force else "incremental", triggered_by, rv, detail,
             deadline_min, deadline_min, max_llm_calls))["id"]

        # ── 外循环（修硬伤 3）：建统一任务，评分运行挂在它下面 ──────────────
        # 为什么现在建而不是"以后再说"：没有任务就没有检查点，
        # 进程崩溃后只能靠增量水位重来，**未完成的中间状态直接归零**。
        # 任务创建失败**不阻断**评分（外循环是加固，不是前置依赖）。
        self._task_id = None
        try:
            t = task_runtime.create(
                kind="scoring", trigger=triggered_by,
                scope={"staff_ids": staff_ids, "max_sessions": max_sessions,
                       "min_messages": min_messages, "force": force, "grades": grades,
                       "rule_version": rv},
                budget_calls=max_llm_calls or None, deadline_minutes=deadline_min)
            self._task_id = t.get("task_id")
            db.execute("UPDATE scoring_run SET task_id=%s WHERE id=%s",
                       (self._task_id, run_id))
        except Exception as e:                      # noqa: BLE001
            print(f"[scoring] 建任务失败（不影响评分，仅失去检查点）：{e}")

        self._stop = False
        self._partial = None                    # 护栏触发原因（deadline / max_llm_calls）
        self._run_id = run_id
        self._progress = {"stage": "启动中", "current_target": None, "session_total": 0,
                          "session_done": 0, "session_skipped": 0, "assessment_new": 0,
                          "writeback_ok": 0, "writeback_fail": 0, "review_new": 0,
                          "llm_calls": 0, "llm_failed": 0, "session_failed": 0,
                          "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                          "ctx_total": 0, "ctx_kept": 0, "ctx_available": 0,
                          "ctx_dropped": 0, "ctx_truncated": 0, "ctx_drop_reasons": {},
                          "ctx_seg_delta": 0, "ctx_seg_key": 0, "ctx_seg_fallback": 0,
                          "ctx_seg_profile": 0, "ctx_seg_last": 0,
                          "ctx_tokens_peak": 0, "ctx_tokens_total": 0, "ctx_tokens_budget": 0,
                          "ctx_budget_exceeded": 0, "ctx_trim": {}, "ctx_key_sources": {},
                          "tool_failures": 0,
                          # 钩子观测（§2.4 H3）：拒绝原因分布 + 模型输出体检
                          "hook_denials": {}, "hook_sessions": 0,
                          "hook_coverage_sum": 0.0, "hook_invalid_total": 0,
                          # H5 单会话耗时分布
                          "session_secs": []}
        self._spawn(self._run,
                    (run_id, staff_ids, max_sessions, min_messages, force, rv, grades),
                    name=f"scoring-{run_id}")
        # 审计三节点之一（架构 H2）：启动就留痕，便于追溯「谁在什么时候全量重评了」。
        # 主体由服务端推导（评审 P0-2）：这里仍在**请求上下文**内（线程刚启动、
        # contextvar 尚可见），故 `security.actor()` 拿得到认证用户名；
        # `triggered_by`（manual/schedule/public_class）是**触发来源**、语义不同，
        # 保留在 detail 里单独记录。
        try:
            import security as _sec
            _actor = _sec.actor()
        except Exception:                       # noqa: BLE001
            _actor = triggered_by
        audit.record_action(_actor, "scoring_start", f"run:{run_id}",
               {"triggered_by": triggered_by,
                "kind": "full" if force else "incremental", "rule_version": rv,
                "max_sessions": max_sessions, "min_messages": min_messages,
                "deadline_minutes": deadline_min, "max_llm_calls": max_llm_calls,
                "staff_ids": staff_ids})
        return {"ok": True, "run_id": run_id, "rule_version": rv,
                "writeback_mode": self.writeback_mode()}

    # ---------------- 内部：进度 ----------------

    # ---------------- 内部：外循环 / 检查点（修硬伤 3）----------------

    def _step_begin(self, cand):
        """为一个会话开一个检查点步。返回 step_id（无任务时返回 None）。"""
        if not self._task_id:
            return None
        try:
            name = f"session:{cand['staff_id']}:{cand['session_id']}"
            return task_runtime.step_begin(
                self._task_id, name,
                input_ref={"target_id": cand.get("target_id"),
                           "msg_count": cand.get("msg_count"),
                           "last_msg_time": cand.get("last_msg_time")})
        except Exception as e:                      # noqa: BLE001  检查点是加固，不阻断
            print(f"[scoring] 写检查点失败（继续评分）：{e}")
            return None

    def _step_end(self, step_id, ok, cand, outcome, error=None):
        """收尾检查点：落结果引用与**下一步该做什么**（失败不阻断）。"""
        if not self._task_id or not step_id:
            return
        try:
            # next_step 写"下一条待处理会话"的语义：这里给出可读的续跑指引，
            # 真正的续跑靠 `completed_step_names()` 过滤（名字即幂等键）。
            nxt = "continue:next_session" if ok else f"retry:{cand.get('session_id')}"
            task_runtime.step_end(
                step_id, ok,
                output_ref={"outcome": outcome, "session_id": cand.get("session_id")},
                next_step=nxt, error=error)
            task_runtime.bump_progress(
                self._task_id,
                **({"step_ok": 1} if ok else {"step_failed": 1}))
        except Exception as e:                      # noqa: BLE001
            print(f"[scoring] 收尾检查点失败（继续评分）：{e}")

    def _task_finish(self, status, partial_reason=None):
        """把评分运行的状态映射到任务状态并收尾（终态不可迁出，INV-5）。"""
        if not self._task_id:
            return
        # 映射：stopped→cancelled（人工中止）· partial→partial · success→succeeded
        target = {"stopped": "cancelled", "partial": "partial",
                  "success": "succeeded", "failed": "failed"}.get(status, "partial")
        try:
            task_runtime.transition(self._task_id, target, reason=partial_reason)
        except Exception as e:                      # noqa: BLE001
            print(f"[scoring] 任务收尾失败（不影响结果）：{e}")

    def _bump(self, run_id, *, detail=None, **deltas):
        # 累加部分与 SyncService 共用基类实现（`job_service.JobService._accumulate`）；
        # 这里只保留「本表有哪些列」这件本服务特有的事。
        snap = self._accumulate(deltas, detail)
        db.execute(
            """UPDATE scoring_run SET session_done=%s, session_skipped=%s,
                   assessment_new=%s, writeback_ok=%s, writeback_fail=%s,
                   review_new=%s, llm_calls=%s, llm_failed=%s, session_failed=%s, detail=%s,
                   heartbeat_at=now()
                 WHERE id=%s""",
            (snap.get("session_done", 0), snap.get("session_skipped", 0),
             snap.get("assessment_new", 0), snap.get("writeback_ok", 0),
             snap.get("writeback_fail", 0), snap.get("review_new", 0),
             snap.get("llm_calls", 0), snap.get("llm_failed", 0),
             # 修 G5 配套：`session_failed` 改造前只累加在内存 detail、**没落列**，
             # 导致「这轮挂了几个会话」在 SQL 层面查不到（监控脚本也因此踩过坑）。
             snap.get("session_failed", 0),
             db.as_json(snap), run_id))
        return snap

    def _inc(self, **deltas):
        """只累加内存进度，**不触发** scoring_run 的 UPDATE。

        与 `_score_one` 原来那段 ``with self._lock:`` 自增完全等价（阶段B 图化时
        把它抽成方法，供图节点复用，保证统计口径一字不差）。
        """
        self._accumulate(deltas)

    def _note_facts(self, facts: dict) -> None:
        """取标签失败留痕（修 G1）。只加内存计数，随后续 ``_bump`` 写进 run detail。

        与 ``_bump_ctx`` 同性质：属于「观测」而非「业务」——取标签失败不阻断评分，
        但必须留下痕迹（``tool_failures``），否则「标签一路挂了」在数字上看不出来。
        新旧两条路径（``_score_one`` / 图节点 ``n_facts``）都走这里，保证口径一致。
        """
        if facts.get("tool_failure"):
            self._inc(tool_failures=1)

    def _note_session_secs(self, secs: float) -> None:
        """记录单个会话的耗时（架构 §2.7 H5「单会话耗时分布」）。

        改造前只有**整轮** `elapsed_sec`，于是「这一轮为什么慢」无法回答：
        是普遍变慢，还是被某几个超长会话拖住？落一个耗时列表就能算 p50/p95 并定位长尾。
        列表直接放 `scoring_run.detail`（169 个数字约 1KB，可接受）。
        """
        with self._lock:
            lst = self._progress.setdefault("session_secs", [])
            if len(lst) < 5000:                     # 兜底上限，防止极端情况撑爆 detail
                lst.append(round(float(secs), 2))

    def _note_hook(self, result) -> None:
        """钩子拒绝留痕 + 模型输出体检（架构 §2.4 H3 / §2.7 可观测）。

        改造前「这个会话为什么被跳过」只能读代码、且两条路径各写一遍。现在：

        · **拒绝**按原因计数到 ``hook_denials``（可直接回答「多少会话因内容不足/配额被拦」）
        · ``after_llm_output`` 的**体检报告**累计成 ``hook_sessions`` / ``hook_coverage_sum``
          / ``hook_invalid_total`` → 可算「平均覆盖率」「无效项总量」，
          回答「模型判得干净不干净」（漏检多不多、证据回链失败多不多）

        **放行不计入 ``hook_denials``**：放行是常态，计进去只会淹没有用信号。
        """
        d = result.detail or {}
        if result.denied:
            denials = self._progress.setdefault("hook_denials", {})
            denials[result.reason] = denials.get(result.reason, 0) + 1
        # 体检指标（只有 ② after_llm_output 的 detail 带 coverage）
        if "coverage" in d:
            self._inc(hook_sessions=1)
            for key, val in (("hook_coverage_sum", d.get("coverage") or 0),
                             ("hook_invalid_total", d.get("invalid") or 0)):
                self._progress[key] = self._progress.get(key, 0) + val

    # ---------------- 内部：候选会话 ----------------

    def _candidates(self, staff_ids, limit, min_messages, force, rv, grades=None) -> list:
        """选出需要评分的会话。

        增量口径（``force=False``）：无评估 / 有新消息 / 规则版本变化；
        ``force=True`` 走**真全量**：忽略增量水位与规则版本，所有达标会话全部重评。

        修 A3：改造前 ``force`` 收了参数却**从未进 SQL**，「全量重评」点了等于没点
        （前端 force 开关是死参数）。现在它只去掉增量条件，保留「会话达标」的硬筛选
        （``session_type='ext_user' AND target_id IS NOT NULL AND msg_count>=min``）。
        """
        params = {"min_msgs": min_messages, "rv": rv, "limit": limit}
        staff_filter = ""
        if staff_ids:
            staff_filter = "AND c.staff_id = ANY(%(staff_ids)s)"
            params["staff_ids"] = list(staff_ids)
        # F2（架构 §2.1）：只扫指定等级的客户（早会前复核 S/A、公开课复核）。
        # ⚠️ 条件写在 `last.grade` 上 —— 即「**上次评出来的**等级」。
        #    这是刻意选择：要复核的是「上次判为高价值的客户」，而不是「这次判为高价值的」
        #    （后者要靠本次结果才知道，鸡生蛋）。
        grade_filter = ""
        if grades:
            grade_filter = "AND last.grade = ANY(%(grades)s)"
            params["grades"] = list(grades)

        # force = 真全量：整段去掉增量条件（rv 参数此时不再被引用，psycopg 会忽略多余键）
        incremental = "" if force else """
               AND (last.assessment_id IS NULL
                    OR last.a_last IS NULL
                    OR last.a_last < c.last_msg_time
                    OR last.rule_version IS DISTINCT FROM %(rv)s)"""

        # ── 候选优先级排序（架构 §2.2 G7）───────────────────────────────
        # 改造前只按 `last_msg_time DESC` 排 —— 于是 `max_sessions` 限流时，
        # **先被砍掉的是「沉默的高价值客户」**（他们最后发言早），而恰好这批人最该看。
        # 新口径：① S/A 等级（最该复核）→ 未评过 → B/C → 其他
        #         ② 有新消息  ③ 未评过  ④ 规则版本变更
        #         ⑤ 最后时间（兜底，保证稳定可复现）
        # 用 `scoring_priority_order` 可一键退回老口径（默认开）。
        if db.setting_bool("scoring_priority_order", "SCORING_PRIORITY_ORDER", True):
            order_by = """
                CASE WHEN last.grade = 'S' THEN 0
                     WHEN last.grade = 'A' THEN 1
                     WHEN last.assessment_id IS NULL THEN 2
                     WHEN last.grade = 'B' THEN 3
                     WHEN last.grade = 'C' THEN 4
                     ELSE 5 END,
                CASE WHEN last.a_last IS NULL OR last.a_last < c.last_msg_time THEN 0 ELSE 1 END,
                CASE WHEN last.assessment_id IS NULL THEN 0 ELSE 1 END,
                CASE WHEN last.rule_version IS DISTINCT FROM %(rv)s THEN 0 ELSE 1 END,
                c.last_msg_time DESC NULLS LAST"""
        else:
            order_by = "c.last_msg_time DESC NULLS LAST"

        sql = f"""
            SELECT c.staff_id, s.name AS staff_name, c.session_id, c.target_id,
                   c.name AS target_name, c.msg_count, c.last_msg_time,
                   last.assessment_id AS last_aid, last.a_last, last.rule_version AS last_rv,
                   last.grade AS last_grade
              FROM chat_session c
              LEFT JOIN staff s ON s.staff_id = c.staff_id
              LEFT JOIN LATERAL (
                   SELECT a.assessment_id, a.last_msg_time AS a_last, a.rule_version, a.grade
                     FROM customer_intent_assessment a
                    WHERE a.relationship_key = c.staff_id || ':' || COALESCE(c.target_id, c.session_id)
                    ORDER BY a.assessment_id DESC LIMIT 1
              ) last ON true
             WHERE c.session_type = 'ext_user'
               AND c.target_id IS NOT NULL
               AND c.msg_count >= %(min_msgs)s
               {staff_filter}{grade_filter}{incremental}
             ORDER BY {order_by}
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
        attempts = max(1, db.setting_int("llm_retry_attempts", "LLM_RETRY_ATTEMPTS", 3))
        temperature = db.setting_float("llm_temperature", "LLM_TEMPERATURE",
                                    analyze.DEFAULT_TEMPERATURE)
        max_tokens = db.setting_int("llm_max_tokens", "LLM_MAX_TOKENS", 0) or None
        max_repairs = db.setting_int("rejudge_retries", "REJUDGE_RETRIES",
                                  analyze.DEFAULT_MAX_REPAIRS)
        # 单次调用超时（架构 §7.3 `scoring_llm_timeout_sec`）：此前是 `llm.TIMEOUT=120`
        # **硬编码**，测试/弱网/慢模型都无法按环境调整。0 或空 → 用 llm 模块默认。
        timeout = db.setting_int("llm_timeout_sec", "LLM_TIMEOUT_SEC", 0) or None

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
                    timeout=timeout,
                    on_attempt=_on_attempt)
            except llm.RetryableLLMError as e:
                last_err = e
                # 优先听服务端的 `Retry-After`（llm.py 已解析并封顶 30s）；
                # 没给才退回指数退避，并叠加 ±30% 抖动 —— 否则整批会话会在
                # 同一时刻集体重试，把限流中的服务端再打一次（文章 B 的二次冲击）。
                wait = getattr(e, "retry_after", None)
                if wait is None:
                    wait = http_client.backoff_seconds(i, 1.2)
                time.sleep(wait)
            except (llm.FatalLLMError, contracts.ContractViolationError):
                raise                                  # 重试一万次也没用，直接转人工
        raise RuntimeError(f"大模型分析连续 {attempts} 次失败（临时故障）：{last_err}")

    # ---------------- 内部：回写微伴 ----------------

    def _writeback(self, run_id, rel_key, aid, staff_id, target_id, score, mode,
                   policy_action=None):
        """正分回写微伴（幂等：一次评估只写一次）。

        阶段B 起，live 模式**只同步试一次**：失败不再原地退避 3 次（最坏拖 5.6s/会话），
        而是交给 ``writeback_outbox`` 的 drain worker 按退避异步重试（修 G6）。
        这样批量评分不会被微伴抖动拖慢，同时回写也不会因为增量水位推进而永久丢失。
        """
        status, errcode, errmsg, resp = "skipped", None, "无正分，无需回写", None
        # ③ before_writeback（架构 §2.4 H3 + D2-1）：开关 / 幂等 / 配额 / **差值累加**。
        # D2-1（开 live 的硬前提）：微伴「客户评分」是**累计行为分、无覆盖接口**，
        # 所以必须只写增量 = 本次 positive_total − 客户已回写水位。
        # 整值回写在累加语义下会让分数虚高（同一次评估被反复累加）。
        # 两个查询**条件化**：只在 live 且配额上限 > 0 时才查，避免每会话白加 DB 往返。
        quota_run_limit = db.setting_int("writeback_quota_per_run", "WRITEBACK_QUOTA_PER_RUN", 0)
        quota_cust_limit = db.setting_int("writeback_quota_per_customer_per_day",
                                       "WRITEBACK_QUOTA_PER_CUSTOMER_PER_DAY", 0)
        delta_mode = db.setting_bool("writeback_delta_mode", "WRITEBACK_DELTA_MODE", True)
        # ④ Policy 策略门（架构 §7.4 / ADR-004，修硬伤 4）。
        # ⚠️ **只在 live 生效**：dry_run 不写外部系统、没有副作用，拦它没有意义，
        #    反而会改变默认配置下的行为（默认就是 dry_run）并把已有运维习惯打乱。
        #    即"策略门只拦真有副作用的动作"。
        policy_denied = (mode == "live" and policy_action
                         and policy_action != policy.AUTO)
        if policy_denied:
            decision = hooks.HookResult(
                False, f"policy_{policy_action}",
                {"policy_action": policy_action, "mode": mode, "score": score})
        else:
            decision = hooks.before_writeback(
                positive_total=score, mode=mode,
                already_written=_already_written(aid) if mode == "live" else False,
                quota_run=self._progress.get("writeback_ok", 0) + self._progress.get("writeback_fail", 0),
                quota_run_limit=quota_run_limit,
                quota_customer=_customer_writes_today(target_id) if quota_cust_limit else 0,
                quota_customer_limit=quota_cust_limit,
                # 水位查询也条件化：非 live / 非差值模式都不需要它
                written_total=(_ledger_total(rel_key) if (mode == "live" and delta_mode) else None),
                delta_mode=delta_mode)
        self._note_hook(decision)
        # D2-1：真正投递的是**差值**（放行时 detail 里有 delta）
        send_score = int((decision.detail or {}).get("delta") or score)
        self._bump(run_id, writeback_delta=(1 if send_score != int(score) else 0))
        if decision.denied:
            status = hooks.REASON_TO_WB_STATUS.get(decision.reason, "skipped")
            # policy_confirm / policy_human 不在 hooks 的原因表里 → 这里给中文文案
            errmsg = _POLICY_WB_CN.get(
                decision.reason, hooks.REASON_CN.get(decision.reason, decision.reason))
            # ⚠️ 「需人工二次确认后才写入」必须有人工确认的**入口**（2026-10-16 接线）：
            #    策略拦下的回写原来只是"跳过" —— 而 outbox 那条待办会一直挂着等确认，
            #    却没有任何地方能确认它（`approval_request` 那时只有测试在写）。
            #    现在把它登记进审批队列：评分中心的「回写审批」卡片能看到、能批准/驳回。
            #    只登记、**不阻塞批次**（task_id 留空）—— 卡住的是一条客户回写，不是整轮评分。
            # ⚠️ 只有 `confirm`（"人工确认后可写"）才登记审批；`human`（"只进人工复核"）
            #    不登记 —— 否则审批队列成了绕过复核的旁路：低置信度的结论只要有人点
            #    一下批准就能写进微伴。`human` 的处置交给 outbox：停投 + 说明下一步
            #    （先复核/改判并重评该客户）。
            if policy_denied and policy_action == policy.CONFIRM:
                try:
                    task_runtime.request_writeback_approval(
                        assessment_id=aid, relationship_key=rel_key, staff_id=staff_id,
                        external_user_id=target_id,
                        score=_pending_writeback_score(aid, score),
                        reason=f"策略判定 {decision.reason}")
                except Exception as e:              # noqa: BLE001
                    print(f"[scoring] 登记回写审批失败（回写仍被拦）：{type(e).__name__}: {e}")
        else:  # live：单次投递，重试交给 outbox
            try:
                # 工具调用审计（五问）：参数脱敏后落 tool_audit + JSONL（修硬伤 5）
                with audit.tool_call(
                        "weiban.add_customer_score",
                        {"staff_id": staff_id, "external_user_id": target_id,
                         "score": send_score},
                        task_id=run_id, decision="allow",
                        reason=f"policy={policy_action or 'n/a'}") as rec:
                    resp = weiban.add_customer_score(
                        staff_id, external_user_id=target_id,
                        score=send_score, use_header_token=True)
                    errcode = resp.get("errcode")
                    rec(resp, ok=(errcode == 0), errcode=errcode,
                        errmsg=resp.get("errmsg"))
                errmsg = resp.get("errmsg")
                status = "ok" if errcode == 0 else "failed"
            except Exception as e:
                errmsg, status = f"{type(e).__name__}: {e}", "failed"
        # D2-1：**只有真投递成功**才推进水位 —— dry_run 不推进（没真写就不算已写），
        # 失败也不推进（否则差值被吃掉，重试时无分可写 → 永久少写）。
        if status == "ok" and delta_mode:
            try:
                _advance_ledger(rel_key, staff_id, target_id, aid, send_score)
            except Exception as e:                  # noqa: BLE001
                print(f"[scoring] 推进回写水位失败（下次会重算差值）：{e}")
        try:
            writeback_repo.record_attempt(rel_key, aid, staff_id, target_id, score,
                                          mode, status, errcode, errmsg, resp)
            assessment_repo.set_writeback_status(aid, status)
        except Exception as e:
            print(f"[scoring] 回写记录失败：{e}")

        # 同步 outbox 状态（未入队则静默跳过）：成功置 done，失败进入退避待 drain 重试
        if status in ("ok", "failed"):
            outbox.mark_result(aid, status == "ok", errcode, errmsg, resp)
        return status

    # ---------------- 内部：人工复核队列 ----------------

    def _enqueue_review(self, aid, cand, neg_items, raw_score, grade, writeback_status):
        if not neg_items:
            assessment_repo.set_review_status(aid, "na")
            return False
        rel = f"{cand['staff_id']}:{cand['target_id']}"
        review_repo.enqueue(rel_key=rel, aid=aid, cand=cand,
                            ai_negative_total=sum(i.get("score", 0) for i in neg_items),
                            ai_negative_items=neg_items,
                            raw_score=raw_score, grade=grade)
        assessment_repo.set_review_status(aid, "pending")
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

            # 关键历史（架构 §4.6）：④ 段最终条数 + 命中了哪些维度 + 取数来源。
            # 「记忆到底有没有被读进去」只能靠这些数字回答，不是靠感觉。
            if stats.get("key"):
                self._progress["ctx_seg_key"] = self._progress.get("ctx_seg_key", 0) + stats["key"]
            if stats.get("key_rules"):
                kr = self._progress.setdefault("ctx_key_rules", {})
                for rid in stats["key_rules"]:
                    kr[rid] = kr.get(rid, 0) + 1
            if stats.get("key_source"):
                ks = self._progress.setdefault("ctx_key_sources", {})
                ks[stats["key_source"]] = ks.get(stats["key_source"], 0) + 1

            # ③ 增量 / ⑤ 兜底 / ①② 记忆段（架构 §4.6）：每段实际条数 —— 回答
            # 「模型看到的到底是"新消息"还是"最近窗口"」以及「记忆真的读进去了吗」（V2）。
            # ①② 记的是「非空的会话数」（1 = 该会话读回了上次结论）。
            for key in ("seg_delta", "seg_fallback", "seg_profile", "seg_last"):
                if stats.get(key):
                    self._progress["ctx_" + key] = self._progress.get("ctx_" + key, 0) + stats[key]

            # 预算（架构 §4.5.4 / V4）：峰值 + 累计 + 超限次数 + 裁剪明细。
            # 命名刻意避开设置项 ``ctx_tokens_max``（那是"预算上限"本身），
            # 这里 ``ctx_tokens_peak`` 是"实际用量的最大值"。
            if stats.get("tokens"):
                self._progress["ctx_tokens_peak"] = max(
                    self._progress.get("ctx_tokens_peak", 0), int(stats["tokens"]))
                self._progress["ctx_tokens_total"] = (
                    self._progress.get("ctx_tokens_total", 0) + int(stats["tokens"]))
                if stats.get("tokens_max"):
                    self._progress["ctx_tokens_budget"] = int(stats["tokens_max"])
            if stats.get("budget_exceeded"):
                self._progress["ctx_budget_exceeded"] = (
                    self._progress.get("ctx_budget_exceeded", 0) + 1)
            if stats.get("trim"):
                tr = self._progress.setdefault("ctx_trim", {})
                for k, v in stats["trim"].items():
                    tr[k] = tr.get(k, 0) + int(v or 0)

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
            max_attempts = db.setting_int("session_max_failures", "SESSION_MAX_FAILURES", 3)
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
            # ⚠️ 二次故障也要有数字（2026-10-16）：`session_failure` 写不进去时此前
            #    只有一行 print —— 运维看不出"这轮有多少失败明细丢了"。计数随 `_bump`
            #    落进 `scoring_run.detail`（与 `tool_failures` 同一套路）。
            self._inc(failure_record_errors=1)

    # ---------------- 内部：落库（assessment + outbox 同一事务）----------------

    def _persist_assessment(self, run_id, cand, res, score, grade, needs_review,
                            unchecked, pos_items, neg_items, mode,
                            session_confidence=None, review_reasons=None,
                            policy_action=None) -> int:
        """落 assessment，并在**同一事务**内把「待回写」写进 outbox（修 G6）。

        为什么必须同事务：``db.pool()`` 是 autocommit，``db.execute`` 每条各自一条连接、
        不具备事务性。若先落 assessment、再单独入队 outbox，中途崩溃就会出现
        「增量水位已推进，却没有待回写任务」—— 正是 G6（回写永久丢失）。

        注意 ``source_run_id`` 写 ``(run_id or None)``：人工单会话重评传 run_id=0，
        落 NULL 即绕开 ``uq_assessment_run_session``，允许对同一会话反复重评。

        `session_confidence` / `review_reasons` 为**可选参数**（默认 None）——
        这样图路径、旧路径、以及将来任何调用方都能平滑迁移，不会因签名变化炸掉。
        """
        rel = f"{cand['staff_id']}:{cand['target_id']}"
        # ④ before_persist（架构 §2.4 H3）：落库前完整性校验。
        # 改造前没有这层 —— 缺字段会一路走到 SQL 才炸（`NotNullViolation` 的堆栈里
        # 看不出是哪个会话、缺什么）。现在变成**明确的拒绝 + 原因**，且拒绝被当作
        # 「本会话失败」处理（计 `session_failed` + 写 `session_failure`），不会打断整轮。
        gate = hooks.before_persist(rel_key=rel, staff_id=cand["staff_id"],
                                    target_id=cand["target_id"], session_id=cand["session_id"],
                                    score=score, grade=grade,
                                    rule_version=res.get("rule_version"))
        if gate.denied:
            self._note_hook(gate)
            raise ValueError(f"落库前校验未通过（{gate.reason}）：{gate.detail}")

        # F6（架构 §7.2）：`old_raw_score` = 本次评估**之前**该客户的最新分数。
        # 必须在 INSERT 之前取（取自己的话永远等于新分）。
        # 目的：能直接答「这次比上次涨了还是跌了」，而不必回表做窗口函数。
        prev = _last_assessment(rel) or {}
        old_raw = prev.get("raw_score")
        with db.tx() as conn:
            aid = conn.execute(
                """INSERT INTO customer_intent_assessment
                     (relationship_key, staff_id, staff_name, target_id, target_name, session_id,
                      raw_score, positive_total, negative_total, grade,
                      positive_items, negative_items, invalid_items,
                      profile_summary, sales_strategy, confidence, needs_human_review,
                      rule_version, msg_count, last_msg_time,
                      unchecked_rules, prompt_version, schema_version, model_confidence,
                      source_run_id, contract_attempts,
                      session_confidence, review_reasons,
                      model_name, rule_checks, old_raw_score, profile_struct,
                      task_id, policy_action, data_as_of)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING assessment_id""",
                (rel, cand["staff_id"], cand.get("staff_name"), cand["target_id"],
                 cand.get("target_name"), cand["session_id"],
                 score["raw_score"], score["positive_total"], score["negative_total"],
                 grade["grade"], db.as_json(pos_items), db.as_json(neg_items),
                 db.as_json(res.get("invalid_items") or []),
                 res.get("profile_summary"), res.get("sales_strategy"),
                 # ⚠️ `confidence` 列**停用**（架构 §6.3 要求统一到 `model_confidence`）：
                 # 新记录写 NULL，历史数据保留不动。读取方一律改用 model_confidence。
                 None, needs_review,
                 res.get("rule_version"), cand.get("msg_count"), cand.get("last_msg_time"),
                 db.as_json(unchecked), res.get("prompt_version"), res.get("schema_version"),
                 res.get("model_confidence"),
                 # 人工单会话重评（run_id=0）不参与唯一约束，可反复重评
                 (run_id or None), int(res.get("contract_attempts") or 1),
                 session_confidence, db.as_json(review_reasons) if review_reasons else None,
                 # B1 归因 / 闸2 逐维度表态 / F6 上次分数
                 res.get("model_name"),
                 db.as_json(res.get("rule_checks") or []),
                 old_raw,
                 db.as_json(res.get("profile_struct") or []),
                 # 硬伤 3/4 的落库侧：结论可追溯到「哪次任务」与「当时策略怎么判的」
                 (run_id or None), policy_action,
                 # P2：本次判定用的时间锚点（来自 `compute_facts`，**不在这里重取
                 # `time.time()`** —— 从算事实到落库之间隔着一次 LLM 调用，重取会差几十秒，
                 # 「当时为什么这么判」就对不上了）
                 res.get("data_as_of"))
            ).fetchone()["assessment_id"]

            # E2（架构 §7.2）：新评估落库后，把**同一客户的旧记录**标记为被取代。
            # 为什么放在同一事务：若先插入再单独更新，中途崩溃会留下**两条 is_current=true**
            # → 列表出现重复行，正是 E2 要消除的问题。
            # 语义：只有最新一条是「当前有效」，历史行保留（可追溯客户是怎么变的）。
            assessment_repo.supersede_previous(conn, rel, aid)

            # 仅 live + 有正分才入队（dry_run / off 走同步记账，与改造前行为一致）
            if (score["positive_total"] >= 1 and mode == "live"
                    and db.setting_bool("use_outbox", "USE_OUTBOX", True)):
                # D2-1：**入队的就是差值**，不是正分整值。
                # 差值必须在这里算好并固化进任务 —— 出队重试时不能再算一次
                # （水位会被别的任务推进，重算会得到不同的数）。
                # 差值为 0（相对水位无增量）→ 不入队，省一次无意义的网络往返。
                send = int(score["positive_total"])
                if db.setting_bool("writeback_delta_mode", "WRITEBACK_DELTA_MODE", True):
                    send = send - _ledger_total(rel)
                if send > 0:
                    outbox.enqueue(conn, aid, rel, cand["staff_id"], cand["target_id"],
                                   send, mode)

            # ---- D2-1：等级 → 微伴标签（**与分数回写完全独立**的一条链路）----
            # 为什么等级必须独立：微伴「客户评分」只能累加，表达不了「降级」；
            # 而标签有增有删 → `tag/batch_update` 一次 add 新 + remove 旧 = 覆盖语义。
            # ⚠️ 开关独立（`grade_tag_mode`），且**默认 off** —— 打标签是「写外部系统」的
            #    副作用，必须由人显式打开，不能因为代码写好了就默认开始改微伴。
            # ⚠️ `remove_tag_id` 在入队时**固化**（取当前水位里的旧标签）：出队重试时
            #    不能再算一次（期间水位可能被别的任务推进 → 重算会摘错标签）。
            if grade_tag.enabled() and grade.get("grade"):
                led = grade_tag.ledger_state(rel)
                grade_tag.enqueue(conn, aid, rel, cand["staff_id"], cand["target_id"],
                                  grade["grade"], mode=grade_tag.mode(),
                                  remove_tag_id=(led.get("tag_id")
                                                 if led.get("grade") != grade["grade"] else None))
        return aid

    # ---------------- 内部：关键历史标签（架构 §4.4.1 的「写」侧）----------------

    def _tag_evidence_messages(self, staff_id, session_id, pos_items, neg_items) -> int:
        """把本轮采纳的 (message_id → rule_ids) 合并进 ``chat_message.rule_tags``。

        语义（架构 §4.4.1）：``rule_tags`` ＝ 「这条消息**曾经**作为哪些规则的证据
        被采纳过」，**只增不减** —— 规则改版后旧标签仍有检索价值（那确实是客户
        说过的关键话），而且标签只决定**候选集**，是否命中仍由模型基于证据重判。

        两条纪律：
        · **失败不阻断评分**：标签是「下一轮检索的键」，不是本轮结论 → 只记
          ``tool_failures``，评分与回写照常（与 G1 的处置一致）。
        · **幂等**：``ARRAY(SELECT DISTINCT unnest(...))`` 保证重复执行结果不变。
        """
        pairs = {}
        for it in list(pos_items or []) + list(neg_items or []):
            if not isinstance(it, dict):
                continue
            rid = it.get("rule_id")
            if not rid:
                continue
            for mid in (it.get("evidence_message_ids") or []):
                if mid:
                    pairs.setdefault(str(mid), set()).add(str(rid))
        if not pairs:
            return 0
        try:
            with db.tx() as conn:
                for mid, tags in pairs.items():
                    conn.execute(
                        """UPDATE chat_message m
                              SET rule_tags = (
                                    SELECT ARRAY(SELECT DISTINCT
                                                   unnest(m.rule_tags || %s::text[])
                                                 ORDER BY 1))
                            WHERE m.staff_id = %s AND m.session_id = %s
                              AND m.message_id = %s""",
                        (sorted(tags), staff_id, session_id, mid))
            return len(pairs)
        except Exception as e:
            print(f"[scoring] 写 rule_tags 失败（不阻断评分）：{e}")
            self._inc(tool_failures=1)
            return 0

    # ---------------- 内部：评一个会话 ----------------

    def _score_one(self, run_id, cand, rules, min_messages, mode, fetch_tags) -> str:
        staff_id, session_id = cand["staff_id"], cand["session_id"]
        rel = f"{staff_id}:{cand['target_id']}"

        ctx = build_context(staff_id, session_id,
                            session_meta={"staff_id": staff_id, "session_id": session_id},
                            rel_key=rel)
        # ① before_llm_call（架构 §2.4 H3）：有效信息量闸（B4 三口径）
        # 改造前是裸 `if kept < min_kept: return "skipped"`；现在统一走 hook，
        # 拒绝原因可观测（`hook_denials`），且与预算/超时共用同一判定入口。
        # B4：三个口径（保留条数 / 客户开口数 / 文本长度）—— 单看条数会被
        # 「销售单方面刷屏」钻空子（见 hooks.before_llm_call 的注释）。
        _st = ctx["stats"]
        gate = hooks.before_llm_call(
            kept=_st["kept"],
            min_kept=db.setting_int("ctx_min_kept", "CTX_MIN_KEPT", 4),
            customer_msgs=_st.get("customer_msgs"),
            min_customer_msgs=db.setting_int("ctx_min_customer_msgs", "CTX_MIN_CUSTOMER_MSGS", 1),
            text_len=_st.get("text_len"),
            min_effective_chars=db.setting_int("ctx_min_effective_chars",
                                            "CTX_MIN_EFFECTIVE_CHARS", 0))
        if gate.denied:
            self._note_hook(gate)
            return "skipped"
        self._bump_ctx(ctx["stats"])

        facts = compute_facts(staff_id, session_id, fetch_tags=fetch_tags)
        self._note_facts(facts)                  # 修 G1：取标签失败留痕
        self._bump(run_id, llm_calls=1)
        res = self._llm_analyze(ctx, facts["text"], rules)
        # 模型自报置信度被夹取（越界/非法）也要有数字：此前是纯静默改写，
        # 统计口径会被悄悄拉偏而没人知道（2026-10-16）。
        if res.get("confidence_clamped"):
            self._inc(confidence_clamped=1)
        # P2：把这次判定用的时间锚点随 `res` 带到落库处（与图路径 `n_items` 同口径）。
        # 引擎项就是按这个锚点算的，落库必须用同一个值。
        res["data_as_of"] = facts.get("as_of")

        pos_items = res.get("positive_items") or []
        # engine=rule 的规则已在 rules.derive_items 里排除，这里再兜一层（防御契约回退）
        engine_ids = rules_mod.engine_rule_ids(rules)
        neg_items = [i for i in (res.get("negative_items") or [])
                     if i.get("rule_id") not in engine_ids]
        neg_items += rules_mod.engine_negative_items(facts, rules)   # 规则引擎硬指标

        score = rules_mod.compute_score(pos_items, neg_items, rules)
        grade = rules_mod.grade_of(score["raw_score"], rules)

        unchecked = list(res.get("unchecked_rules") or [])

        # ② after_llm_output（架构 §2.4 H3）：结构/极性/证据/覆盖率的统一体检。
        # 这里**不重复**已有校验（契约在 contracts、极性在 rules、回链在 analyze、
        # 覆盖率在 rules.check_coverage），而是汇总成体检报告 + 做一次极性终检。
        # 极性终检失败 = 流程错误（负分规则混进加分项），必须拒而不是静默容忍。
        gate = hooks.after_llm_output(pos_items=pos_items, neg_items=neg_items,
                                      unchecked=unchecked,
                                      invalid_items=res.get("invalid_items"),
                                      rules=rules,
                                      total_llm=len(rules_mod.llm_rule_ids(rules)))
        self._note_hook(gate)
        if gate.denied:
            # 保守处置：丢弃全部语义命中项（保留引擎硬指标），并计一次失败转人工
            raise llm.FatalLLMError(
                f"命中项极性异常（规则表与输出不符）：{gate.detail.get('polarity_violations')}")

        # 「要不要人看一眼」+「整体可信度」由**代码算**（架构 第六章 E4）：
        # 不再采信模型自报的 needs_human_review（不能让可能出错的组件自证清白）。
        # 三个前置都已在位：证据回链（A1）· 覆盖率（闸2）· 上次结论读回（①② 记忆段）。
        last_grade = (_last_assessment(rel) or {}).get("grade")
        # ⚠️ 必须传**合并后**的 pos/neg_items（含引擎硬指标），不能传 res 的原始列表 ——
        # 引擎项（如 negative_no_reply_2d）是上面后加的，res 里没有；
        # 传错会导致「有负分却判定不需要复核」（实测踩到过）。
        conf = confidence.compute(pos_items, neg_items, res, score, grade, rules,
                                  last_grade=last_grade,
                                  thresholds=_confidence_thresholds())
        needs_review = conf["needs_human_review"]

        # ④ Policy 策略判定（架构 §7.4 / ADR-004，修硬伤 4）。
        #    `confidence` 回答"判得准不准"，Policy 回答"机器能不能自己动手" ——
        #    两者不是一回事：一个结论可以"值得人看一眼"而**绝不自动写外部系统**。
        #    判定留痕（含阈值快照）→ 三个月后能解释"这条当时为什么自动执行了"。
        pol = policy.decide(session_confidence=conf["session_confidence"],
                            review_reasons=conf["review_reasons"],
                            grade=grade.get("grade"))
        conf["policy_action"] = pol["action"]
        conf["policy_reasons"] = pol["action_reasons"]

        n_pos = len(pos_items)
        conf_items = conf.get("items") or []
        pos_items, neg_items = conf_items[:n_pos], conf_items[n_pos:]   # 带上 item_confidence

        aid = self._persist_assessment(run_id, cand, res, score, grade, needs_review,
                                       unchecked, pos_items, neg_items, mode,
                                       session_confidence=conf["session_confidence"],
                                       review_reasons=conf["review_reasons"],
                                       policy_action=pol["action"])
        policy.record(relationship_key=rel, action=pol["action"],
                      session_confidence=conf["session_confidence"],
                      review_reasons=conf["review_reasons"],
                      action_reasons=pol["action_reasons"],
                      thresholds_snapshot=pol["thresholds"],
                      assessment_id=aid, task_id=run_id or None)

        # 关键历史标签（架构 §4.4.1）：写「下一轮的检索键」，失败不阻断评分
        self._tag_evidence_messages(staff_id, session_id, pos_items, neg_items)

        # 分流①：正分回写微伴
        wb = self._writeback(run_id, rel, aid, staff_id, cand["target_id"],
                             score["positive_total"], mode,
                             policy_action=pol["action"])

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
        if db.setting_bool("use_graph_session", "USE_GRAPH_SESSION", False):
            import graph_session                        # 延迟导入：不图化时零开销
            return graph_session.run_session(self, run_id, cand, rules,
                                             min_messages, mode, fetch_tags)
        return self._score_one(run_id, cand, rules, min_messages, mode, fetch_tags)

    # ---------------- 内部：主流程 ----------------

    def _run(self, run_id, staff_ids, max_sessions, min_messages, force, rv, grades=None):
        t0 = time.time()
        try:
            db.init_db()
            # 外循环：任务进入 running（失败不阻断评分）
            if self._task_id:
                try:
                    task_runtime.transition(self._task_id, "running")
                except Exception as e:              # noqa: BLE001
                    print(f"[scoring] 任务转 running 失败（继续评分）：{e}")
            rules = rules_mod.load_rules()
            mode = self.writeback_mode()
            fetch_tags = db.setting_bool("scoring_fetch_tags", "SCORING_FETCH_TAGS", False)
            sleep_ms = db.setting_int("scoring_sleep_ms", "SCORING_SLEEP_MS", 0)

            self._bump(run_id, detail={"stage": "筛选候选会话"})
            cands = self._candidates(staff_ids, max_sessions, min_messages, force, rv,
                                     grades=grades)
            n_staff = len({c["staff_id"] for c in cands})
            db.execute("UPDATE scoring_run SET staff_count=%s, session_total=%s WHERE id=%s",
                       (n_staff, len(cands), run_id))
            self._bump(run_id, detail={"stage": f"开始评分（共 {len(cands)} 个会话）",
                                       "session_total": len(cands)})

            for i, cand in enumerate(cands, 1):
                if self._stop:
                    break
                # 护栏 + 跨进程停止：一次查询同时取回（避免每轮两次往返）
                #   · stop_requested：外部（后端 / 别人）发的停止请求
                #   · deadline / max_llm_calls：整轮总超时与成本预算（架构 G3/G4）
                guard = None
                try:
                    _sr = db.query_one(
                        "SELECT stop_requested, deadline, max_llm_calls "
                        "  FROM scoring_run WHERE id=%s", (run_id,)) or {}
                    if _sr.get("stop_requested"):
                        self._stop = True
                        break
                    _dl = _sr.get("deadline")
                    # 护栏判定统一走 hook（架构 §2.4 H3）：轮次级只看 G3/G4，不做内容量检查
                    guard = hooks.before_llm_call(
                        deadline=_dl.timestamp() if _dl else None,
                        max_llm_calls=_sr.get("max_llm_calls"),
                        llm_calls=self._progress.get("llm_calls", 0))
                except Exception:
                    pass          # 查询失败不应中断评分；护栏失效好过整轮挂掉
                if guard.denied:
                    self._partial = guard.reason
                    self._note_hook(guard)
                    print(f"[scoring] 触发护栏 `{guard.reason}`：已完成 {i - 1}/{len(cands)}，优雅退出")
                    break
                label = f"{cand.get('target_name') or cand['target_id']}"
                self._bump(run_id, detail={"stage": f"评分 ({i}/{len(cands)})",
                                           "current_target": label,
                                           "current_staff": cand.get("staff_name"),
                                           "session_total": len(cands)})
                try:
                    _t_sess = time.time()
                    # 检查点：每个会话一步（修硬伤 3）。
                    # 步骤名即幂等键 —— 续跑时用它跳过已完成的会话，而不是从头再来。
                    _sid = self._step_begin(cand)
                    r = self._score_cand(run_id, cand, rules, min_messages, mode, fetch_tags)
                    self._step_end(_sid, True, cand, r)
                    # H5：单会话耗时（含失败的会话 —— 失败往往正是慢的那几个）
                    self._note_session_secs(time.time() - _t_sess)
                    if r == "skipped":
                        self._bump(run_id, session_skipped=1)
                    else:
                        self._bump(run_id, session_done=1)
                except Exception as e:
                    # 修 G5：失败**不再算「已完成」**。改造前这里同时加 llm_failed 和
                    # session_done，完成率永远好看，从数字上看不出这轮挂了几个。
                    print(f"[scoring] 会话 {cand['session_id']} 评分失败：{e}")
                    self._step_end(locals().get("_sid"), False, cand, None, error=e)
                    self._bump(run_id, llm_failed=1, session_failed=1)
                    self._record_failure(run_id, cand, e)
                if sleep_ms:
                    time.sleep(sleep_ms / 1000.0)

            # 状态三分支（架构 B3）：人工停止 / 护栏触发 / 自然跑完
            #   · stopped —— 用户点了停止
            #   · partial —— 撞到总超时或成本预算（**未跑完，但已处理的都有效**）
            #   · success —— 候选全部处理完
            if self._stop:
                status, partial_reason = "stopped", None
            elif self._partial:
                status, partial_reason = "partial", self._partial
            else:
                status, partial_reason = "success", None
            elapsed = round(time.time() - t0, 1)
            # H5：把耗时列表折算成分布指标（列表本身也保留，便于事后查长尾）
            _secs = sorted(self._progress.get("session_secs") or [])
            _dist = {}
            if _secs:
                def _pct(p):
                    return _secs[min(len(_secs) - 1, int(len(_secs) * p))]
                _dist = {"session_secs_p50": _pct(0.5), "session_secs_p95": _pct(0.95),
                         "session_secs_max": _secs[-1], "session_secs_n": len(_secs)}
            db.execute(
                """UPDATE scoring_run SET status=%s, elapsed_sec=%s, finished_at=now(),
                       partial_reason=%s, detail=%s WHERE id=%s""",
                (status, elapsed, partial_reason,
                 db.as_json({**self._progress, **_dist, "stage": "已完成"}), run_id))
            # 外循环收尾：任务进入终态（映射见 _task_finish）
            self._task_finish(status, partial_reason)
            audit.record_action("system", "scoring_done", f"run:{run_id}",
                   {"status": status, "partial_reason": partial_reason,
                    "elapsed_sec": elapsed, "session_done": self._progress.get("session_done", 0),
                    "session_skipped": self._progress.get("session_skipped", 0),
                    "session_failed": self._progress.get("session_failed", 0),
                    "assessment_new": self._progress.get("assessment_new", 0),
                    "llm_calls": self._progress.get("llm_calls", 0)})
            print(f"[scoring] run={run_id} {status} 用时 {elapsed}s（评分 "
                  f"{self._progress.get('assessment_new', 0)}，复核 "
                  f"{self._progress.get('review_new', 0)}）")
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[scoring] run={run_id} 失败：{e}\n{tb}")
            db.execute("UPDATE scoring_run SET status='failed', error=%s, finished_at=now() WHERE id=%s",
                       (str(e)[:2000], run_id))
            self._task_finish("failed", partial_reason=str(e)[:200])
            self._bump(run_id, detail={"stage": "失败", "error": str(e)})
            audit.record_action("system", "scoring_failed", f"run:{run_id}",
                   {"error": str(e)[:500], "error_class": type(e).__name__})
        finally:
            self._stop = False
            self._partial = None

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
                                 db.setting_bool("scoring_fetch_tags", "SCORING_FETCH_TAGS", False))
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
