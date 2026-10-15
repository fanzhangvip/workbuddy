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
import json
import math
import threading
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
import grade_tag                    # noqa: E402  D2-1 等级→微伴标签（独立链路）
import outbox                       # noqa: E402  回写 outbox（修 G6：回写与评分解耦）
import rules as rules_mod           # noqa: E402
import weiban                       # noqa: E402

#: 整轮护栏判定（G3 超时 / G4 预算）—— 本体在 `hooks`，此处 re-export 仅为兼容既有调用方
#: （`_run`、`tests/test_loop_guard.py` 都直接引用 ``ss.guard_reason``）。
guard_reason = hooks.guard_reason

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

    tags, tag_text, tag_error = [], "", None
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
        except Exception as e:
            # 修 G1：不再 ``except: pass`` 静默吞掉。
            # 静默的后果很隐蔽 —— 标签取不到时，「实际到课」这类靠标签佐证的维度
            # 会被当成「没有证据」→ 分数被悄悄降级，而日志里什么都不留。
            # 现在：① 事实文本里显式标注降级（模型能看见，不会据缺失推定未到课）；
            #       ② 把失败写进 tool_failure（由调用方计入 scoring_run.detail，可观测）。
            tag_error = f"{type(e).__name__}: {e}"
            lines.append("【降级】微伴客户标签/自定义字段获取失败，本次未纳入标签证据；"
                         "请勿据其缺失推定「未到课 / 无标签」。")

    return {"text": "\n".join(lines) or "无额外事实。", "hours": hours,
            "no_reply_48h": no_reply, "last_direction": (last or {}).get("direction"),
            "tags": tags, "tag_text": tag_text, "tool_failure": tag_error}


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


# ============================================================ 关键历史检索（架构 §4.4）

# 与「近期窗口」逐字一致的可读条件：有企微明文，或有非空正文。
_READABLE = "(plain IS NOT NULL OR (content IS NOT NULL AND content <> ''))"
_MSG_COLS = ("message_id, msgid, direction, msg_time, from_name, content, "
             "msgtype, content_state, plain")


def _key_rule_ids(rel_key: str, staff_id: str, session_id: str) -> list:
    """「本次相关规则集」＝ K2（架构 §4.4.2）。

    K2 = **上次评估命中的 rule_id** ∪ **本会话 rule_tags 全集**。
    不选 K1（全部 14 条非引擎规则）是因为每个维度都拉 3 条会把 30 条配额吃光，
    而大部分维度该客户从未表现过信号。
    """
    ids = set()
    last = db.query_one(
        """SELECT positive_items, negative_items FROM customer_intent_assessment
            WHERE relationship_key = %s ORDER BY assessment_id DESC LIMIT 1""",
        (rel_key,))
    if last:
        for col in ("positive_items", "negative_items"):
            items = last.get(col)
            if isinstance(items, str):
                try:
                    items = json.loads(items)
                except Exception:
                    items = []
            for it in (items or []):
                if isinstance(it, dict) and it.get("rule_id"):
                    ids.add(str(it["rule_id"]))
    rows = db.query(
        """SELECT DISTINCT unnest(rule_tags) AS rule_id FROM chat_message
            WHERE staff_id = %s AND session_id = %s AND cardinality(rule_tags) > 0""",
        (staff_id, session_id))
    ids.update(str(r["rule_id"]) for r in rows if r.get("rule_id"))
    return sorted(ids)


def _key_history_rows(staff_id: str, session_id: str, rule_ids: list,
                      max_total: int) -> list:
    """④ 段取数：两阶段，避免「高频规则吃光配额」（架构 §4.4.3）。

    阶段 1 每个相关 rule 保底最近 1 条（≤ 规则条数）；阶段 2 余额度按时间倒序补齐；
    同一消息命中多规则 → 合并成一行（``tags`` 带出命中的维度）。
    最终按时间**升序**返回，便于合并后统一编号（C2）。
    """
    if not rule_ids:
        return []
    return db.query(
        f"""WITH hit AS (
               SELECT {_MSG_COLS}, t.rule_id
                 FROM chat_message
                 CROSS JOIN LATERAL unnest(rule_tags) AS t(rule_id)
                WHERE staff_id = %s AND session_id = %s
                  AND rule_tags && %s::text[]
             ),
             first_per_rule AS (            -- 阶段 1：每 rule 最近 1 条
               SELECT DISTINCT ON (rule_id) * FROM hit
                ORDER BY rule_id, msg_time DESC, message_id DESC
             ),
             rest AS (                      -- 阶段 2：其余按时间倒序
               SELECT h.* FROM hit h
                WHERE NOT EXISTS (SELECT 1 FROM first_per_rule f
                                   WHERE f.message_id = h.message_id
                                     AND f.rule_id = h.rule_id)
                ORDER BY h.msg_time DESC, h.message_id DESC
             ),
             merged AS (
               SELECT * FROM first_per_rule
               UNION ALL
               SELECT * FROM (SELECT * FROM rest
                               LIMIT GREATEST(%s - %s, 0)) r
             ),
             dedup AS (                     -- 同一消息命中多规则 → 合并成一行
               SELECT message_id, msg_time, array_agg(DISTINCT rule_id) AS tags
                 FROM merged GROUP BY message_id, msg_time
             )
        SELECT d.message_id, d.msg_time, d.tags,
               m.msgid, m.direction, m.from_name, m.content, m.msgtype,
               m.content_state, m.plain
          FROM dedup d
          JOIN chat_message m
            ON m.staff_id = %s AND m.session_id = %s AND m.message_id = d.message_id
         ORDER BY d.msg_time ASC, d.message_id ASC
         LIMIT %s""",
        (staff_id, session_id, list(rule_ids), int(max_total), len(rule_ids),
         staff_id, session_id, int(max_total)))


def _early_anchor_rows(staff_id: str, session_id: str, n: int) -> list:
    """④′ 早期锚点（架构 §4.4.4）：④ 命中为 0 时，取会话**最早**的 n 条。

    理由是「岗位 / 需求 / 预算」这类最重要的事实几乎总在会话开头。
    与 ④ **互斥**（有 ④ 就不启用），并计入同一配额。
    """
    n = int(n or 0)
    if n <= 0:
        return []
    return db.query(
        f"""SELECT {_MSG_COLS} FROM chat_message
             WHERE staff_id = %s AND session_id = %s AND {_READABLE}
             ORDER BY msg_time ASC, message_id ASC LIMIT %s""",
        (staff_id, session_id, n))


# ============================================================ 上下文装配（架构 §4.5）

_TRIM_BATCH = 20        # 裁剪 ③ 时每批砍掉的最早条数（架构 §4.5.4(3) 第 4 条）


def _recent_rows(staff_id: str, session_id: str, n: int) -> list:
    """取「最近 n 条」可读消息（时间升序）。⑤ 兜底与展示壳共用。"""
    n = int(n or 0)
    if n <= 0:
        return []
    rows = db.query(
        f"""SELECT {_MSG_COLS} FROM chat_message
             WHERE staff_id = %s AND session_id = %s AND {_READABLE}
             ORDER BY msg_time DESC, message_id DESC LIMIT %s""",
        (staff_id, session_id, n))
    rows.reverse()
    return rows


def _watermark(rel_key: str):
    """③ 段的起点：该客户**最近一次评估**记录的 ``last_msg_time``（架构 §4.5.2）。

    直接复用既有增量水位口径（``customer_intent_assessment.last_msg_time``），
    因此**不需要新表**（架构 §4.8 C-B 的关键点）。查不到 → ``None``。
    """
    row = db.query_one(
        """SELECT last_msg_time FROM customer_intent_assessment
            WHERE relationship_key = %s AND last_msg_time IS NOT NULL
            ORDER BY assessment_id DESC LIMIT 1""",
        (rel_key,))
    if not row or row.get("last_msg_time") is None:
        return None
    return int(row["last_msg_time"])


def _delta_rows(staff_id: str, session_id: str, wm, soft_cap: int) -> list:
    """③ 增量消息：自水位之后新增的可读消息（架构 §4.5.1）。

    - 水位为空（首次评分 / 无历史评估）→ **增量 = 全量**，与现状等价（§4.4.4）
    - 超出 ``soft_cap`` → **保留最新的**那批：被砍的应该是"最早"的消息
      （与 §4.5.4 裁剪方向一致；早期事实由 ④ / ④′ 承担）
    """
    soft_cap = int(soft_cap or 0)
    if soft_cap <= 0:
        return []
    if wm:
        sql = (f"""SELECT {_MSG_COLS} FROM chat_message
                    WHERE staff_id = %s AND session_id = %s AND {_READABLE}
                      AND msg_time > %s
                    ORDER BY msg_time DESC, message_id DESC LIMIT %s""")
        params = (staff_id, session_id, int(wm), soft_cap)
    else:
        sql = (f"""SELECT {_MSG_COLS} FROM chat_message
                    WHERE staff_id = %s AND session_id = %s AND {_READABLE}
                    ORDER BY msg_time DESC, message_id DESC LIMIT %s""")
        params = (staff_id, session_id, soft_cap)
    rows = db.query(sql, params)
    rows.reverse()
    return rows


def _dedup_rows(*groups) -> list:
    """按 ``message_id`` 去重合并（**先到先得**：先传入的段优先保留，如 ④ 的 ``_pinned``）。

    合并后按 ``(msg_time, message_id)`` 升序。tie-breaker 必须有：同一秒的消息
    只靠 ``msg_time`` 排序不稳定，V3（确定性）会挂（架构 §4.5.2）。
    """
    seen, out = set(), []
    for g in groups:
        for r in (g or []):
            mid = r.get("message_id")
            if mid in seen:
                continue
            seen.add(mid)
            out.append(r)
    out.sort(key=lambda x: (x.get("msg_time") or 0, str(x.get("message_id") or "")))
    return out


def _estimate_tokens(text: str) -> int:
    """零依赖 token 估算：``len / 1.5``（中文保守系数，**宁可高估**；架构 §4.5.4(1)）。"""
    return int(math.ceil(len(text or "") / 1.5))


# ============================================================ ①② 记忆段（架构 §4.5.3 / §4.8 C-C）

_MEM_TEXT_CAP = 400      # 画像摘要 / 销售策略各自的字符上限（≈267 token），防止记忆段吃掉预算
_MEM_ITEM_CAP = 12       # ①② 各列命中的 item 数上限（超出只列前 N 条 + 合计）


def _as_items(value) -> list:
    """JSONB 读回来可能是 list，也可能是 json 字符串（不同驱动行为不一），统一成 list。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    return [i for i in (value or []) if isinstance(i, dict)]


def _confidence_thresholds() -> dict:
    """置信度与复核触发的运行时阈值（app_setting > .env > 代码默认）。

    全部**可配**：现在的默认值是按 52 条真实评估标定的（见 `confidence.py` 的常量注释），
    业务若觉得复核队列量不合适，可在此调，不必改代码。
    """
    return {
        "conf_min": setting_float("confidence_review_min", "CONFIDENCE_REVIEW_MIN",
                                  confidence.REVIEW_CONF_MIN),
        "boundary": setting_int("confidence_boundary_delta", "CONFIDENCE_BOUNDARY_DELTA",
                               confidence.GRADE_BOUNDARY_DELTA),
        "flip_steps": setting_int("confidence_flip_steps", "CONFIDENCE_FLIP_STEPS",
                                  confidence.GRADE_FLIP_STEPS),
        "weak_weight": setting_int("confidence_weak_weight", "CONFIDENCE_WEAK_WEIGHT",
                                   confidence.WEAK_EVIDENCE_WEIGHT),
        # ⚠️ 默认 true = 保持改造前行为（不放松）。实测「仅引擎负分」占 69%，
        # 关掉它可把复核率从 90% 降到 60%（见 confidence.py 中该常量的说明）。
        "engine_negative": setting_bool("review_engine_negative", "REVIEW_ENGINE_NEGATIVE",
                                        confidence.REVIEW_ENGINE_NEGATIVE),
    }


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


def _fmt_score(total: int) -> str:
    """分值带符号：+4 / -1 / 0。"""
    return f"{int(total):+d}" if total else "0"


def _fmt_items(items: list, cap: int = _MEM_ITEM_CAP) -> str:
    """把命中项渲染成 ``rule_id(+2)、rule_id(-1)`` 形式（超出 cap 只列前 cap 条）。"""
    parts = []
    for it in items[:cap]:
        rid = it.get("rule_id") or "?"
        sc = it.get("score")
        parts.append(f"{rid}({_fmt_score(sc)})" if sc is not None else str(rid))
    if len(items) > cap:
        parts.append(f"…等 {len(items)} 项")
    return "、".join(parts) or "无"


def _clip(text, cap: int = _MEM_TEXT_CAP) -> str:
    """截断长文本（画像摘要 / 销售策略可能很长）。按字符截，避免整段吃掉预算。"""
    s = (text or "").strip().replace("\n", " ")
    return s if len(s) <= cap else s[:cap] + "…"


def _render_memory(last: dict) -> tuple:
    """渲染 ①② 两段「记忆块」（架构 §4.5.3）。

    **口径要点**：①② 是"上一次评估的结论"，**不是本次证据** —— 因此：
    - 用 ``【】`` 段标，**不带 ``#编号``** → 不参与 ``index``，模型无法把它们当证据回链（决议 M-C）；
    - 段标里明写「仅供参考、非本次证据」，与 §4.5.6 的提示词说明呼应（双保险）；
    - ①② 由调用方保证**永不裁剪**（超预算时照原样发送，只砍 ③⑤）。
    """
    if not last:
        return "", ""
    ts = last.get("analyzed_at")
    when = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts or "未知")
    raw, grade = int(last.get("raw_score") or 0), (last.get("grade") or "未定")
    pos, neg = _as_items(last.get("positive_items")), _as_items(last.get("negative_items"))
    pos_total = int(last.get("positive_total") or 0)
    neg_total = int(last.get("negative_total") or 0)

    prof = [f"【客户状态档案】（读回自上次评估 {when}，仅供参考、非本次证据）",
            f" 上次等级={grade}｜上次总分={_fmt_score(raw)}｜"
            f"上次评估消息数={last.get('msg_count') or '未知'}｜规则版本={last.get('rule_version') or '未知'}"]
    if last.get("profile_summary"):
        prof.append(f" 上次画像摘要：{_clip(last['profile_summary'])}")
    if last.get("sales_strategy"):
        prof.append(f" 上次销售策略：{_clip(last['sales_strategy'])}")

    detail = [f"【上次评分明细】（上次命中项合计 +{pos_total} / {_fmt_score(neg_total)}，非本次证据）",
              f" 上次加分：{_fmt_items(pos)}｜合计 {_fmt_score(pos_total)}",
              f" 上次扣分：{_fmt_items(neg)}｜合计 {_fmt_score(neg_total)}"]
    return "\n".join(prof), "\n".join(detail)


def build_context(staff_id: str, session_id: str, limit: int = None,
                  session_meta: dict = None, rel_key: str = None,
                  key_history: bool = True, delta: bool = True) -> dict:
    """上下文装配（架构 §4.5）：① 档案 + ② 上次明细 + ③ 增量 + ④ 关键历史 + ⑤ 兜底。

    分工（§4.5.5）：本函数只决定「**选哪些消息**」与「**放哪些记忆块**」
    （取数 + 合并 + 预算 + 裁剪）；清洗 / 降噪 / 编号 / 时间 / 证据回链全部交给
    ``ctx_preprocess``，**不自己拼消息文本**。

    - **①② 是"记忆块"不是"消息行"**（§4.5.3）：用【】段标、**不带 ``#编号``**、
      故不进 ``index`` → 模型无法把它们当证据（决议 M-C：记忆能影响理解，改不了分数）；
      且**永不裁剪**（超预算时只砍 ③⑤，宁可略超预算也不退回"只看最近 60 条"）
    - ``delta=False``：展示壳语义「最近 N 条」（``build_conversation`` 用），
      不掺增量、关键历史与记忆块
    - 选择权在本函数 → 渲染时**把窗口关掉**（``window=0``），否则 ③ 的「200 条软上限」
      会被预处理层的 60 条窗口又剪掉一次；裁剪统一由下面的预算逻辑做
    """
    opts = _ctx_options()
    if limit:
        opts = replace(opts, window=int(limit))
    window = max(int(opts.window or 60), 1)
    rel = rel_key or f"{staff_id}:{session_id}"

    # ── 展示壳：只取最近 N 条 ────────────────────────────────────────────
    if not delta:
        ctx = ctx_preprocess.build_llm_context(
            _recent_rows(staff_id, session_id, window * 3),
            session_meta=session_meta, opts=opts)
        st = ctx["stats"]
        st.update({"key_source": None, "key_rules": [], "watermark": None,
                   "seg_delta": 0, "seg_fallback": 0, "seg_profile": 0, "seg_last": 0,
                   "tokens": _estimate_tokens(ctx["text"]),
                   "tokens_max": 0, "budget_exceeded": False, "trim": {}})
        return ctx

    render_opts = replace(opts, window=0)          # 选择已在上层完成，预处理层不再裁剪

    # ── ③ 增量消息（水位 = 上次评估的 last_msg_time；空 → 全量）──────────
    wm = _watermark(rel)
    d_rows = _delta_rows(staff_id, session_id, wm,
                         setting_int("ctx_delta_max", "CTX_DELTA_MAX", 200))
    for r in d_rows:
        r["_seg"] = "delta"

    # ── ④ 关键历史（架构 §4.4）：K2 定规则集 → 两阶段取数 → 空则退 ④′ 早期锚点 ──
    key_rows, key_source = [], None
    if key_history and setting_bool("ctx_key_history", "CTX_KEY_HISTORY", True):
        max_total = setting_int("ctx_key_history_max", "CTX_KEY_HISTORY_MAX", 30)
        rule_ids = _key_rule_ids(rel, staff_id, session_id)
        if rule_ids:
            key_rows = _key_history_rows(staff_id, session_id, rule_ids, max_total)
            if key_rows:
                key_source = "tags"
        if not key_rows:
            key_rows = _early_anchor_rows(
                staff_id, session_id,
                setting_int("ctx_cold_start_early", "CTX_COLD_START_EARLY", 10))
            if key_rows:
                key_source = "cold_start"
    for r in key_rows:
        r["_pinned"] = True                        # 关键历史不受窗口约束（stats["key"] 靠它计数）
        r["_seg"] = "key"

    rows = _dedup_rows(key_rows, d_rows)
    ctx = ctx_preprocess.build_llm_context(rows, session_meta=session_meta, opts=render_opts)

    # ── ⑤ 兜底窗口：③④ 有效条数低于阈值时补齐到「窗口」规模 ──────────────
    # 触发阈值 `ctx_fallback_min`（默认 4 = 文档 §4.5.2 伪码的 min_kept 口径）：
    #   水位旧（很久没评）→ ③ = 水位后全部消息，**本身已涵盖最近 60 条**，无需 ⑤；
    #   水位新（刚评过）  → ③ 很薄，但那些"最近的消息"上一轮刚看过，结论已在 ①② 里；
    # 而若把阈值取「窗口」(60)，则系统跑起来后 ③ 永远 < 60 → ⑤ 永远被补上 →
    # **③ 被 ⑤ 完全吞并，「增量」名存实亡**（这正是 C-B 要解决的）。
    # 前提是 ①② 已落地（C-C），连续性由记忆承担；若要退回保守行为，把该值设成 60 即可。
    fb_rows = []
    fb_min = setting_int("ctx_fallback_min", "CTX_FALLBACK_MIN", 4)
    if ctx["stats"]["kept"] < fb_min:
        fb_rows = _recent_rows(staff_id, session_id, window)   # 补的量仍是「窗口」规模
        for r in fb_rows:
            r["_seg"] = "fallback"
        merged = _dedup_rows(rows, fb_rows)
        if len(merged) > len(rows):
            rows = merged
            ctx = ctx_preprocess.build_llm_context(rows, session_meta=session_meta, opts=render_opts)

    # ── ①② 记忆段（架构 §4.5.3 / C-C）：读回上次结论，前置为「记忆块」──────
    # ①② 用【】段标、**不带 #编号** → 不参与 index，模型无法把它们当证据（决议 M-C）。
    seg1 = seg2 = ""
    if setting_bool("ctx_memory", "CTX_MEMORY", True):
        seg1, seg2 = _render_memory(_last_assessment(rel))

    def compose(msg_text: str) -> str:
        """把记忆块与消息行拼成最终 text。**①② 永远在最前，且不参与任何裁剪。**"""
        return "\n\n".join(p for p in (seg1, seg2, msg_text) if p)

    # ── 预算与裁剪（架构 §4.5.4）：先整段砍 ⑤ → ④ 分组收缩 → 砍 ③ 最早 ────
    tokens_max = setting_int("ctx_tokens_max", "CTX_TOKENS_MAX", 12000)
    trim = {}
    tokens = _estimate_tokens(compose(ctx["text"]))
    if tokens > tokens_max:
        # ⑤ 的「整段砍」只砍**⑤ 独有**的行：与 ③ 重叠的那几条在合并时已被
        # 标记为 ``_seg="delta"``（先到先得），按 id 整批删会误伤 ③。
        if any(r.get("_seg") == "fallback" for r in rows):
            drop_n = sum(1 for r in rows if r.get("_seg") == "fallback")
            rows = [r for r in rows if r.get("_seg") != "fallback"]
            trim["d5"] = drop_n
            ctx = ctx_preprocess.build_llm_context(rows, session_meta=session_meta, opts=render_opts)
            tokens = _estimate_tokens(compose(ctx["text"]))

        # ④ 分组收缩（架构 §4.5.4(3) 第 2 步）：同一 rule 保留**最新 1 条**，
        # 只对「该 rule 命中 >1 条」的行动手。不需要权重口径，故 C-C 可做。
        while tokens > tokens_max:
            by_rule = {}
            for r in rows:
                for rid in (r.get("tags") or []):
                    by_rule.setdefault(rid, []).append(r)
            shrink = set()
            for rid, rs in by_rule.items():
                if len(rs) <= 1:
                    continue
                rs = sorted(rs, key=lambda x: (x.get("msg_time") or 0, str(x.get("message_id") or "")))
                shrink |= {r.get("message_id") for r in rs[:-1]}       # 每组留最新 1 条
            # 保守：同时命中多条规则的消息不参与收缩（它属于多个组，删了会误伤别的组）
            shrink -= {r.get("message_id") for r in rows if len(r.get("tags") or []) > 1}
            if not shrink:
                break
            rows = [r for r in rows if r.get("message_id") not in shrink]
            trim["shrunk4"] = trim.get("shrunk4", 0) + len(shrink)
            ctx = ctx_preprocess.build_llm_context(rows, session_meta=session_meta, opts=render_opts)
            tokens = _estimate_tokens(compose(ctx["text"]))

        # ③ 从【最早】开始按批砍（架构 §4.5.4(3) 第 3 步）：最新的是本次判断最相关的。
        # ⚠️ 文档原「第 3 步：按 rule 权重升序整组砍 ④」**已按 2026-10-14 拍板取消**（D4）：
        #   ① `rules.json` 各档本没有 weight 字段，加它要先由业务定 14 个维度的相对重要性，
        #      且全局权重表达不了"对 X 类客户地域更重要"这种条件性；
        #   ② 更根本的是——**维度是不可再生资源，消息是可再生资源**。做完第 2 步（④ 每组留
        #      最新 1 条）后，④ 最多剩「规则数」条（≤14，约 1.6k token）；而砍一批 ③（20 条
        #      约 2.2k token）腾出的空间就**大于**砍光整个 ④。砍 ③ 只损失冗余消息，砍 ④ 却
        #      会让某个维度彻底没证据（可能直接漏判一个加分项）。
        #   → 故 ④ 收缩后不再动，继续砍 ③；③ 砍完仍超才如实上报 budget_exceeded。
        while tokens > tokens_max:
            d_sorted = sorted([r for r in rows if r.get("_seg") == "delta"],
                              key=lambda x: (x.get("msg_time") or 0, str(x.get("message_id") or "")))
            if not d_sorted:
                break
            drop_ids = {r.get("message_id") for r in d_sorted[:_TRIM_BATCH]}
            rows = [r for r in rows if r.get("message_id") not in drop_ids]
            trim["d3"] = trim.get("d3", 0) + len(drop_ids)
            ctx = ctx_preprocess.build_llm_context(rows, session_meta=session_meta, opts=render_opts)
            tokens = _estimate_tokens(compose(ctx["text"]))

    ctx["text"] = compose(ctx["text"])          # 记忆块前置（①② 永不裁剪）

    # ── 观测（架构 §4.6）：每段实际条数 + 预算 + 裁剪明细 ────────────────
    # 段的归属按「先到先得」算：④ > ③ > ⑤（与 ``_dedup_rows`` 的合并顺序一致），
    # 否则同一条消息会被两段重复计数。
    key_ids = {r.get("message_id") for r in key_rows}
    delta_ids = {r.get("message_id") for r in d_rows} - key_ids
    fb_ids = {r.get("message_id") for r in fb_rows} - key_ids - {r.get("message_id") for r in d_rows}
    kept_ids = set(ctx["index"].values())
    st = ctx["stats"]
    st["key_source"] = key_source
    st["key_rules"] = sorted({t for r in key_rows for t in (r.get("tags") or [])})
    st["watermark"] = wm
    st["seg_delta"] = len(kept_ids & delta_ids)
    st["seg_fallback"] = len(kept_ids & fb_ids)
    st["seg_profile"] = 1 if seg1 else 0        # V2 的断言对象：「记忆真的被读进去」
    st["seg_last"] = 1 if seg2 else 0
    st["tokens"] = tokens
    st["tokens_max"] = tokens_max
    st["budget_exceeded"] = bool(tokens > tokens_max)
    st["trim"] = trim
    return ctx


def build_conversation(staff_id: str, session_id: str, limit: int = 60) -> str:
    """取最近 limit 条消息拼成给大模型的文本（保留原签名，内部已走预处理层）。

    评分链路**不再调用**它（架构 V1）；这里保留为「老调用方 / 展示」的薄壳，
    语义就是「最近 N 条」，因此 ``delta=False``（不掺增量与关键历史）。
    """
    return build_context(staff_id, session_id, limit=limit,
                         key_history=False, delta=False)["text"]


# ============================================================ 服务

#: 僵尸 run 判定阈值（分钟）：`scoring_run` 停在 running 超过这么久 → 视为进程异常退出。
#: 必须大于最长批量耗时（170 个长会话 × ~58 秒 ≈ 2.7 小时），故取 4 小时。
STALE_MINUTES = 240


def _ledger_total(rel_key: str) -> int:
    """读客户级回写水位（D2-1）。不存在 → 0（首次回写全量）。"""
    try:
        row = db.query_one("SELECT written_total FROM writeback_ledger WHERE relationship_key=%s",
                           (rel_key,))
        return int((row or {}).get("written_total") or 0)
    except Exception:
        return 0


def _advance_ledger(rel_key: str, staff_id: str, target_id: str, aid: int, delta: int) -> None:
    """回写成功后推进水位（D2-1）。

    ⚠️ 必须**只加已成功投递的增量**：水位是「微伴那边已经累加了多少分」的镜像，
    多写会少回写（水位虚高 → 下次差值变 0 → 分数永远补不上），
    少写会重复回写。两种错都会真实影响客户分数。
    """
    db.execute(
        """INSERT INTO writeback_ledger
             (relationship_key, staff_id, external_user_id, written_total,
              last_assessment_id, last_write_at, writes)
           VALUES (%s,%s,%s,%s,%s,now(),1)
           ON CONFLICT (relationship_key) DO UPDATE
             SET written_total = writeback_ledger.written_total + EXCLUDED.written_total,
                 last_assessment_id = EXCLUDED.last_assessment_id,
                 last_write_at = now(),
                 writes = writeback_ledger.writes + 1,
                 updated_at = now()""",
        (rel_key, staff_id, target_id, int(delta), aid))


def _audit(actor, action, target, detail=None):
    """写审计日志（架构 §2.2 H2）。**失败绝不影响主流程**（审计是旁路观测）。"""
    try:
        db.execute("INSERT INTO audit_log (actor, action, target, detail) VALUES (%s,%s,%s,%s)",
                   (actor, action, target, db.as_json(detail) if detail else None))
    except Exception as e:                      # noqa: BLE001
        print(f"[scoring] 写 audit_log 失败（不影响评分）：{e}")


def _already_written(aid: int) -> bool:
    """该评估是否已成功回写过（供 `before_writeback` 的幂等闸）。

    为什么放在逻辑层而不只靠 `UNIQUE(assessment_id)`：数据库唯一约束只保证
    「不重复记账」，但**挡不住重复调用微伴接口**（微伴的客户评分是累加的，
    重复投递会真的多加分）。这里显式挡在投递之前。

    失败默认返回 False（宁可多投一次，也不要因为查库抖动而漏投）。
    """
    try:
        return bool(db.query_one(
            "SELECT 1 AS x FROM score_writeback WHERE assessment_id=%s AND status='ok'",
            (aid,)))
    except Exception:
        return False


def _customer_writes_today(target_id: str) -> int:
    """该客户今日已成功回写次数（供 `before_writeback` 的日配额闸，边界3）。"""
    try:
        row = db.query_one(
            """SELECT count(*) AS n FROM score_writeback
                WHERE external_user_id=%s AND status='ok'
                  AND created_at >= date_trunc('day', now())""", (target_id,))
        return int(row["n"]) if row else 0
    except Exception:
        return 0


def repair_current_flags() -> dict:
    """自愈 `is_current` 的两种破损（**幂等**，可反复调用）。

    背景（E2 的配套缺口）：落库时「新评估把同客户旧行标记为被取代」**只发生在插入那一刻**。
    若那个新行**之后被删除**（测试清理、人工删数据），旧行的 `is_current` 就再也回不来
    → 该客户在评分列表（默认过滤 `is_current`）里**彻底消失**。实测已出现 1 例。

    两种破损：
    1. 某客户**一行 current 都没有** → 把 `assessment_id` 最大的那行恢复为 current（清 `superseded_by`）
    2. `superseded_by` 指向**已不存在的行** → 清空（悬空引用）

    放在启动时调用，是刻意的：破损由**外部删除**造成，插入路径补不了，只能靠自愈。
    """
    fixed = db.execute(
        """UPDATE customer_intent_assessment a
              SET is_current = true, superseded_by = NULL
            WHERE NOT a.is_current
              AND a.assessment_id = (
                    SELECT max(b.assessment_id) FROM customer_intent_assessment b
                     WHERE b.relationship_key = a.relationship_key)
              AND NOT EXISTS (
                    SELECT 1 FROM customer_intent_assessment c
                     WHERE c.relationship_key = a.relationship_key AND c.is_current)""")
    dangling = db.execute(
        """UPDATE customer_intent_assessment a
              SET superseded_by = NULL
            WHERE a.superseded_by IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM customer_intent_assessment b
                               WHERE b.assessment_id = a.superseded_by)""")
    return {"no_current_fixed": fixed, "dangling_cleared": dangling}


class ScoringService:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = False
        self._run_id = None
        self._progress = {}

    # ---------------- 对外接口 ----------------

    def is_running(self) -> bool:
        """是否有评分在跑。**含跨进程检测**。

        为什么不能只看 ``self._thread``：后端服务、调度器、手工脚本可能跑在**不同进程**，
        ``self._thread`` 只是本进程的状态，跨进程永远看不到 → 会出现**两个批量同时在跑**
        （实测踩到过：脚本里的 run#22 正在跑，另起后端又能启动 run#23）。

        所以再查一次 DB：有 ``status='running'`` 的记录就算在跑。

        **僵尸处理**：进程崩溃来不及改状态时，记录会永久停在 ``running``，
        从而**永久阻塞**后续所有启动。故加超时判定 —— 超过 ``scoring_stale_minutes``
        （默认 240 分钟，需大于最长批量耗时）仍为 running 的，视为僵尸：
        标记为 failed 并放行。DB 不可用时**宁可放行也不误拦**。
        """
        if self._thread is not None and self._thread.is_alive():
            return True
        try:
            row = db.query_one(
                """SELECT id, EXTRACT(EPOCH FROM (now() - started_at)) AS el
                     FROM scoring_run WHERE status='running' ORDER BY id DESC LIMIT 1""")
        except Exception:
            return False
        if not row:
            return False
        stale = setting_int("scoring_stale_minutes", "SCORING_STALE_MINUTES",
                            STALE_MINUTES) * 60
        if row.get("el") is not None and float(row["el"]) > stale:
            db.execute(
                """UPDATE scoring_run SET status='failed', finished_at=now(),
                          error=COALESCE(error, %s) WHERE id=%s AND status='running'""",
                (f"进程异常退出：超过 {stale // 60} 分钟仍为 running，自动标记为失败", row["id"]))
            return False
        return True

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
        # （消息数 / 最后时间都在变，结论不可复现）。与 scheduler.guard_can_start 同一不变量。
        import sync_service
        if sync_service.service.is_running():
            return {"ok": False, "reason": "同步任务进行中，请先等待同步完成再评分（避免读到半截数据）"}
        if not config.LLM_API_KEY or not config.LLM_BASE_URL:
            return {"ok": False, "reason": "缺少大模型配置（LLM_BASE_URL / LLM_API_KEY）"}

        max_sessions = int(max_sessions or setting_int("scoring_max_sessions",
                                                       "SCORING_MAX_SESSIONS", 200))
        min_messages = int(min_messages or setting_int("scoring_min_messages",
                                                       "SCORING_MIN_MESSAGES", 4))
        # ── Loop 护栏（架构 §2.2）：整轮总超时 + LLM 成本预算 ──────────────
        # 0 表示「不限」。实测教训：一次批量跑了 2.7 小时才跑完，中途无法中断 ——
        # 因为改造前只有「单会话内部重试耗时」自然累积，没有整轮总闸。
        deadline_min = setting_int("scoring_deadline_minutes", "SCORING_DEADLINE_MINUTES", 120)
        max_llm_calls = setting_int("scoring_max_llm_calls", "SCORING_MAX_LLM_CALLS", 500)
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
                and setting_bool("eval_gate_enabled", "EVAL_GATE_ENABLED", True)):
            try:
                import eval_runner
                dec = eval_runner.gate_decision(run_if_possible=False)
                if dec.get("blocked"):
                    _audit("system", "rule_gate_blocked", f"rule_version:{rv}",
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
        self._thread = threading.Thread(
            target=self._run,
            args=(run_id, staff_ids, max_sessions, min_messages, force, rv, grades),
            daemon=True, name=f"scoring-{run_id}")
        self._thread.start()
        # 审计三节点之一（架构 H2）：启动就留痕，便于追溯「谁在什么时候全量重评了」
        _audit(triggered_by, "scoring_start", f"run:{run_id}",
               {"kind": "full" if force else "incremental", "rule_version": rv,
                "max_sessions": max_sessions, "min_messages": min_messages,
                "deadline_minutes": deadline_min, "max_llm_calls": max_llm_calls,
                "staff_ids": staff_ids})
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
                   review_new=%s, llm_calls=%s, llm_failed=%s, session_failed=%s, detail=%s
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
        with self._lock:
            for k, v in deltas.items():
                if v:
                    self._progress[k] = self._progress.get(k, 0) + v

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
        if setting_bool("scoring_priority_order", "SCORING_PRIORITY_ORDER", True):
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
        attempts = max(1, setting_int("llm_retry_attempts", "LLM_RETRY_ATTEMPTS", 3))
        temperature = setting_float("llm_temperature", "LLM_TEMPERATURE",
                                    analyze.DEFAULT_TEMPERATURE)
        max_tokens = setting_int("llm_max_tokens", "LLM_MAX_TOKENS", 0) or None
        max_repairs = setting_int("rejudge_retries", "REJUDGE_RETRIES",
                                  analyze.DEFAULT_MAX_REPAIRS)
        # 单次调用超时（架构 §7.3 `scoring_llm_timeout_sec`）：此前是 `llm.TIMEOUT=120`
        # **硬编码**，测试/弱网/慢模型都无法按环境调整。0 或空 → 用 llm 模块默认。
        timeout = setting_int("llm_timeout_sec", "LLM_TIMEOUT_SEC", 0) or None

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
                time.sleep(1.2 * (2 ** i))
            except (llm.FatalLLMError, contracts.ContractViolationError):
                raise                                  # 重试一万次也没用，直接转人工
        raise RuntimeError(f"大模型分析连续 {attempts} 次失败（临时故障）：{last_err}")

    @staticmethod
    def _engine_rule_ids(rules: dict) -> set:
        """由规则引擎硬算、不允许大模型判定的规则（如 48h 未回复）。"""
        return {r["rule_id"] for r in rules.get("negative_rules", [])
                if r.get("engine") == "rule"}

    @staticmethod
    def _rule_tier_score(rules: dict, rule_id: str, default: int = 0) -> int:
        """取某条规则首个档位的分值 —— **从规则表读，不硬编码**。

        曾经把 ``negative_no_reply_2d`` 的分值写死成 ``-2``：业务若把它改成 ``-3``，
        代码仍产出 ``-2`` → ``validate_items()`` 会把它判为**非法项静默丢弃**
        （表现为「该扣的分没扣」，日志里只剩一条 ``invalid_items``）。故改为查表。
        """
        for group in ("negative_rules", "positive_rules"):
            for r in rules.get(group, []) or []:
                if r.get("rule_id") == rule_id:
                    opts = r.get("options") or []
                    if opts and opts[0].get("score") is not None:
                        return int(opts[0]["score"])
        return default

    def _engine_negative_items(self, facts: dict, rules: dict) -> list:
        """规则引擎补充的硬指标扣分项（不由大模型判断）。"""
        items = []
        if facts.get("no_reply_48h"):
            items.append({
                "rule_id": "negative_no_reply_2d",
                "score": self._rule_tier_score(rules, "negative_no_reply_2d", -2),
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
        # ③ before_writeback（架构 §2.4 H3 + D2-1）：开关 / 幂等 / 配额 / **差值累加**。
        # D2-1（开 live 的硬前提）：微伴「客户评分」是**累计行为分、无覆盖接口**，
        # 所以必须只写增量 = 本次 positive_total − 客户已回写水位。
        # 整值回写在累加语义下会让分数虚高（同一次评估被反复累加）。
        # 两个查询**条件化**：只在 live 且配额上限 > 0 时才查，避免每会话白加 DB 往返。
        quota_run_limit = setting_int("writeback_quota_per_run", "WRITEBACK_QUOTA_PER_RUN", 0)
        quota_cust_limit = setting_int("writeback_quota_per_customer_per_day",
                                       "WRITEBACK_QUOTA_PER_CUSTOMER_PER_DAY", 0)
        delta_mode = setting_bool("writeback_delta_mode", "WRITEBACK_DELTA_MODE", True)
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
            errmsg = hooks.REASON_CN.get(decision.reason, decision.reason)
        else:  # live：单次投递，重试交给 outbox
            try:
                resp = weiban.add_customer_score(staff_id, external_user_id=target_id,
                                                 score=send_score, use_header_token=True)
                errcode = resp.get("errcode")
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
                            unchecked, pos_items, neg_items, mode,
                            session_confidence=None, review_reasons=None) -> int:
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
                      model_name, rule_checks, old_raw_score, profile_struct)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                 db.as_json(res.get("profile_struct") or []))
            ).fetchone()["assessment_id"]

            # E2（架构 §7.2）：新评估落库后，把**同一客户的旧记录**标记为被取代。
            # 为什么放在同一事务：若先插入再单独更新，中途崩溃会留下**两条 is_current=true**
            # → 列表出现重复行，正是 E2 要消除的问题。
            # 语义：只有最新一条是「当前有效」，历史行保留（可追溯客户是怎么变的）。
            conn.execute(
                """UPDATE customer_intent_assessment
                      SET is_current = false, superseded_by = %s
                    WHERE relationship_key = %s AND assessment_id < %s AND is_current""",
                (aid, rel, aid))

            # 仅 live + 有正分才入队（dry_run / off 走同步记账，与改造前行为一致）
            if (score["positive_total"] >= 1 and mode == "live"
                    and setting_bool("use_outbox", "USE_OUTBOX", True)):
                # D2-1：**入队的就是差值**，不是正分整值。
                # 差值必须在这里算好并固化进任务 —— 出队重试时不能再算一次
                # （水位会被别的任务推进，重算会得到不同的数）。
                # 差值为 0（相对水位无增量）→ 不入队，省一次无意义的网络往返。
                send = int(score["positive_total"])
                if setting_bool("writeback_delta_mode", "WRITEBACK_DELTA_MODE", True):
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
            min_kept=setting_int("ctx_min_kept", "CTX_MIN_KEPT", 4),
            customer_msgs=_st.get("customer_msgs"),
            min_customer_msgs=setting_int("ctx_min_customer_msgs", "CTX_MIN_CUSTOMER_MSGS", 1),
            text_len=_st.get("text_len"),
            min_effective_chars=setting_int("ctx_min_effective_chars",
                                            "CTX_MIN_EFFECTIVE_CHARS", 0))
        if gate.denied:
            self._note_hook(gate)
            return "skipped"
        self._bump_ctx(ctx["stats"])

        facts = compute_facts(staff_id, session_id, fetch_tags=fetch_tags)
        self._note_facts(facts)                  # 修 G1：取标签失败留痕
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
        n_pos = len(pos_items)
        conf_items = conf.get("items") or []
        pos_items, neg_items = conf_items[:n_pos], conf_items[n_pos:]   # 带上 item_confidence

        aid = self._persist_assessment(run_id, cand, res, score, grade, needs_review,
                                       unchecked, pos_items, neg_items, mode,
                                       session_confidence=conf["session_confidence"],
                                       review_reasons=conf["review_reasons"])

        # 关键历史标签（架构 §4.4.1）：写「下一轮的检索键」，失败不阻断评分
        self._tag_evidence_messages(staff_id, session_id, pos_items, neg_items)

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

    def _run(self, run_id, staff_ids, max_sessions, min_messages, force, rv, grades=None):
        t0 = time.time()
        try:
            db.init_db()
            rules = rules_mod.load_rules()
            mode = self.writeback_mode()
            fetch_tags = setting_bool("scoring_fetch_tags", "SCORING_FETCH_TAGS", False)
            sleep_ms = setting_int("scoring_sleep_ms", "SCORING_SLEEP_MS", 0)

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
                    r = self._score_cand(run_id, cand, rules, min_messages, mode, fetch_tags)
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
            _audit("system", "scoring_done", f"run:{run_id}",
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
            self._bump(run_id, detail={"stage": "失败", "error": str(e)})
            _audit("system", "scoring_failed", f"run:{run_id}",
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
