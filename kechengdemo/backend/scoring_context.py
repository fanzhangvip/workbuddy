# -*- coding: utf-8 -*-
"""评分**输入**的装配：事实（`compute_facts`）+ 上下文（`build_context`）。

## 为什么单独一个模块

这两件事是「喂给规则引擎与大模型的东西」，`eval_runner`（评估集）与
`graph_session`（图编排）都要用。改造前它们住在 `scoring_service.py`，于是：

    eval_runner   ──import──▶ scoring_service
    scoring_service ──延迟 import──▶ eval_runner      ← 环
    graph_session ──import──▶ scoring_service
    scoring_service ──延迟 import──▶ graph_session    ← 环

搬到这里之后，两个调用方直接依赖本模块（本模块只依赖 db / rules / ctx_preprocess /
weiban，**不依赖任何服务**），环就断了；`scoring_service` 再导出同名符号，
既有调用点（含 30 处测试）一行都不用改。

## 口径（都是硬约束，别改）

· ①② 记忆段用 `【】` 段标、**不带 `#编号``** → 不参与 `index`，模型无法把它们当证据回链；
· 评估集必须 `delta=False`（可复现），生产才走水位增量 —— 见 `build_context` 注释；
· 取标签失败**不静默吞**（修 G1）：事实文本里显式标注降级 + 把错误交给调用方计入观测。
"""
import math
import time
from dataclasses import replace

import db
import ctx_preprocess
import weiban
from assessment_repo import last_assessment

#: `_render_memory` 里逐字沿用的旧名字（实现已收敛到 `db.as_dict_list`）
_as_items = db.as_dict_list

# ============================================================ 事实（规则引擎）

def compute_facts(staff_id: str, session_id: str, fetch_tags: bool = False,
                  as_of: int = None, window_days: int = None) -> dict:
    """计算给大模型/规则引擎用的事实。

    - 连续未回复时长：由后台按消息时间计算，不由大模型判断（需求 §5.5）
    - 标签（到课/完课）：优先取微伴自定义字段，作为「邀课成功」的佐证

    ## 时间锚点 `as_of`（修 P2）

    默认 `now()` —— **生产行为逐字不变**。评估集必须传**固定锚点**（样本的
    `anchor_ts`），否则同一批样本换个日子跑，`no_reply_2d` 会自己出现/消失
    （实测：同一批样本两个时刻跑，含该项的样本数 17 → 21），指标就没法比。
    返回值里带 `as_of`，调用方**照原样落库**（`assessment.data_as_of`），
    这样"当时为什么这么判"事后可解释。

    ## 上窗 `window_days`（修 P3）

    改造前只判 `>= 48 小时`，**没有上限**：一条半年前就结束、最后一句是员工发的
    会话，永远扣 -2（实测样本里出现过「距最后一条员工消息已 2015.5 小时」= 84 天）。
    后果是这条规则命中率约 **90%**（229 条评估里 207 条带它），等于对所有人都扣一样的分，
    没有区分度；而且它会随日历自己触发，让"同一个客户昨天 3 分今天 1 分"无法解释。

    现在改成**区间**：`48 小时 ≤ gap ≤ window_days 天`（默认 14 天，见
    `no_reply_window_days`）。语义是"一直在跟进、客户突然不回"才扣分；超过窗口
    视为线索已冷，不再扣（这类客户本来就该靠别的维度评价）。
    """
    as_of = int(as_of) if as_of else int(time.time())
    if window_days is None:
        window_days = db.setting_int("no_reply_window_days", "NO_REPLY_WINDOW_DAYS", 14)
    last = db.query_one(
        """SELECT direction, msg_time, from_name FROM chat_message
            WHERE staff_id = %s AND session_id = %s
            ORDER BY msg_time DESC, message_id DESC LIMIT 1""",
        (staff_id, session_id))
    hours, no_reply = None, False
    if last and last.get("msg_time"):
        gap = as_of - int(last["msg_time"])
        hours = round(gap / 3600, 1)
        # 最后一条是员工发出、且落在「48 小时 ~ 窗口」之间
        no_reply = (last.get("direction") == "send"
                    and gap >= 48 * 3600
                    and (not window_days or gap <= int(window_days) * 86400))

    lines = []
    if hours is not None:
        lines.append(f"距最后一条消息 {hours} 小时；最后发言方为"
                     f"{'员工' if last.get('direction') == 'send' else '客户'}。")
    lines.append(f"是否满足「连续 48 小时未回复（且在该会话仍在跟进的 {window_days} 天窗口内）」"
                 f"（规则引擎判定）：{'是' if no_reply else '否'}。")

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
            "as_of": as_of, "window_days": window_days,
            "tags": tags, "tag_text": tag_text, "tool_failure": tag_error}


# ============================================================ 上下文预处理（委托 ctx_preprocess）

def ctx_options() -> "ctx_preprocess.CtxOptions":
    """从运行时设置构造预处理层选项（app_setting > .env > 默认）。"""
    return ctx_preprocess.CtxOptions(
        window=db.setting_int("ctx_window_size", "CTX_WINDOW_SIZE", 60),
        drop_msgtypes=ctx_preprocess.parse_msgtypes(
            db.setting("ctx_drop_msgtypes", "CTX_DROP_MSGTYPES", "31,9")),
        drop_pure_url=db.setting_bool("ctx_drop_pure_url", "CTX_DROP_PURE_URL", True),
        drop_system=db.setting_bool("ctx_drop_system", "CTX_DROP_SYSTEM", True),
        display_relative=db.setting_bool("ctx_display_relative", "CTX_DISPLAY_RELATIVE", True),
    )


# ============================================================ 关键历史检索（架构 §4.4）

# 与「近期窗口」逐字一致的可读条件：有企微明文，或有非空正文。
READABLE = "(plain IS NOT NULL OR (content IS NOT NULL AND content <> ''))"


MSG_COLS = ("message_id, msgid, direction, msg_time, from_name, content, "
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
            for it in db.as_dict_list(last.get(col)):
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
               SELECT {MSG_COLS}, t.rule_id
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
        f"""SELECT {MSG_COLS} FROM chat_message
             WHERE staff_id = %s AND session_id = %s AND {READABLE}
             ORDER BY msg_time ASC, message_id ASC LIMIT %s""",
        (staff_id, session_id, n))


# ============================================================ 上下文装配（架构 §4.5）

_TRIM_BATCH = 20        # 裁剪 ③ 时每批砍掉的最早条数（架构 §4.5.4(3) 第 4 条）


def recent_rows(staff_id: str, session_id: str, n: int) -> list:
    """取「最近 n 条」可读消息（时间升序）。⑤ 兜底与展示壳共用。"""
    n = int(n or 0)
    if n <= 0:
        return []
    rows = db.query(
        f"""SELECT {MSG_COLS} FROM chat_message
             WHERE staff_id = %s AND session_id = %s AND {READABLE}
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
        sql = (f"""SELECT {MSG_COLS} FROM chat_message
                    WHERE staff_id = %s AND session_id = %s AND {READABLE}
                      AND msg_time > %s
                    ORDER BY msg_time DESC, message_id DESC LIMIT %s""")
        params = (staff_id, session_id, int(wm), soft_cap)
    else:
        sql = (f"""SELECT {MSG_COLS} FROM chat_message
                    WHERE staff_id = %s AND session_id = %s AND {READABLE}
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


def _render_memory(last: dict, memories: list = None) -> tuple:
    """渲染 ①② 两段「记忆块」（架构 4.3）。

    **口径要点**：①② 是"上一次评估的结论"，**不是本次证据** —— 因此：
    - 用 ``【】`` 段标，**不带 ``#编号``** → 不参与 ``index``，模型无法把它们当证据回链（决议 M-C）；
    - 段标里明写「仅供参考、非本次证据」，与 §4.5.6 的提示词说明呼应（双保险）；
    - ①② 由调用方保证**永不裁剪**（超预算时照原样发送，只砍 ③⑤）。

    ``memories``：该客户的**跨次稳定事实**（`memory_repo.active_rows`）。给了就用它渲染
    "已知事实"块（这是 M0 之后的主路径）；没给/为空则退回"只读上次评估"的老行为，
    保证老库（记忆表为空）也能正常评分。

    ⚠️ "上次会话未提及"的标注：事实来自更早的会话、而**最近一次评估**对该槽位说的是
    "未提及" —— 说明客户这次没再提，但**不等于事实失效**。标注出来是为了让模型别把它
    当成本轮的新信息，也让人一眼看出"这条是旧结论"。
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
    if memories:
        # 最近一次评估里"未提及"的槽位 —— 这些事实这次没被复述，但仍有效
        missed = {str(r.get("slot")) for r in _as_items(last.get("profile_struct"))
                  if str(r.get("value") or "").strip() in ("", "未提及")}
        prof.append(" 已知事实（来自历史会话，客户本次没提也仍然有效）：")
        for m in memories[:8]:
            label = m.get("slot_cn") or m.get("slot")
            conf = float(m.get("confidence") or 0)
            tags = []
            if m.get("source") == "human":
                tags.append("人工确认")
            elif conf <= 0.4:
                tags.append("无原话")
            if m.get("slot") in missed:
                tags.append("上次会话未提及")
            prof.append(f"   {label}：{_clip(m.get('value'), 60)}"
                        + (f"（{'；'.join(tags)}）" if tags else ""))
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
                  key_history: bool = True, delta: bool = True,
                  customer_only: bool = False) -> dict:
    """上下文装配（架构 §4.5）：① 档案 + ② 上次明细 + ③ 增量 + ④ 关键历史 + ⑤ 兜底。

    分工（§4.5.5）：本函数只决定「**选哪些消息**」与「**放哪些记忆块**」
    （取数 + 合并 + 预算 + 裁剪）；清洗 / 降噪 / 编号 / 时间 / 证据回链全部交给
    ``ctx_preprocess``，**不自己拼消息文本**。

    - **①② 是"记忆块"不是"消息行"**（§4.5.3）：用【】段标、**不带 ``#编号``**、
      故不进 ``index`` → 模型无法把它们当证据（决议 M-C：记忆能影响理解，改不了分数）；
      且**永不裁剪**（超预算时只砍 ③⑤，宁可略超预算也不退回"只看最近 60 条"）
    - ``delta=False``：展示壳语义「最近 N 条」（``build_conversation`` 用），
      不掺增量、关键历史与记忆块
    - ``customer_only=True``：**只把客户自己说的话**交给模型（员工发言整体剔除）。
      给"提取客户兴趣点"这类链路用（``interest_service``）—— 员工话术
      （"我们这门课…""现在报名有优惠"）会让模型把**销售的主张**当成客户的兴趣，
      而图上每个点都要能点开客户原话。⚠️ 只支持 ``delta=False``：①②③④ 段混着
      档案、上次结论与关键历史（不是消息行），按方向筛只会得到半截上下文 ——
      所以这里的做法是**直接拒绝**，而不是悄悄给出一份错的结果。
    - 选择权在本函数 → 渲染时**把窗口关掉**（``window=0``），否则 ③ 的「200 条软上限」
      会被预处理层的 60 条窗口又剪掉一次；裁剪统一由下面的预算逻辑做
    """
    if customer_only and delta:
        raise ValueError("customer_only 只适用于 delta=False（'最近 N 条'这一段）")
    opts = ctx_options()
    if limit:
        opts = replace(opts, window=int(limit))
    window = max(int(opts.window or 60), 1)
    rel = rel_key or f"{staff_id}:{session_id}"

    # ── 展示壳：只取最近 N 条 ────────────────────────────────────────────
    if not delta:
        rows = recent_rows(staff_id, session_id, window * 8 if customer_only else window * 3)
        if customer_only:
            # 判定"谁说的"只有一处实现（`ctx_preprocess.role_of`），这里不另写一份。
            # 池子放宽再筛：员工刷屏会把客户的话挤出"最近 N 条"，而**限量按客户消息
            # 条数算**才对 —— 交出去的 token 预算本来就该花在客户自己说的话上。
            rows = [r for r in rows
                    if ctx_preprocess.role_of(r.get("direction")) == "客户"][-window:]
        ctx = ctx_preprocess.build_llm_context(
            rows, session_meta=session_meta, opts=opts)
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
                         db.setting_int("ctx_delta_max", "CTX_DELTA_MAX", 200))
    for r in d_rows:
        r["_seg"] = "delta"

    # ── ④ 关键历史（架构 §4.4）：K2 定规则集 → 两阶段取数 → 空则退 ④′ 早期锚点 ──
    key_rows, key_source = [], None
    if key_history and db.setting_bool("ctx_key_history", "CTX_KEY_HISTORY", True):
        max_total = db.setting_int("ctx_key_history_max", "CTX_KEY_HISTORY_MAX", 30)
        rule_ids = _key_rule_ids(rel, staff_id, session_id)
        if rule_ids:
            key_rows = _key_history_rows(staff_id, session_id, rule_ids, max_total)
            if key_rows:
                key_source = "tags"
        if not key_rows:
            key_rows = _early_anchor_rows(
                staff_id, session_id,
                db.setting_int("ctx_cold_start_early", "CTX_COLD_START_EARLY", 10))
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
    fb_min = db.setting_int("ctx_fallback_min", "CTX_FALLBACK_MIN", 4)
    if ctx["stats"]["kept"] < fb_min:
        fb_rows = recent_rows(staff_id, session_id, window)   # 补的量仍是「窗口」规模
        for r in fb_rows:
            r["_seg"] = "fallback"
        merged = _dedup_rows(rows, fb_rows)
        if len(merged) > len(rows):
            rows = merged
            ctx = ctx_preprocess.build_llm_context(rows, session_meta=session_meta, opts=render_opts)

    # ── ①② 记忆段（架构 §4.5.3 / C-C）：读回上次结论，前置为「记忆块」──────
    # ①② 用【】段标、**不带 #编号** → 不参与 index，模型无法把它们当证据（决议 M-C）。
    seg1 = seg2 = ""
    if db.setting_bool("ctx_memory", "CTX_MEMORY", True):
        # ① 的两种来源（开关 `ctx_memory_source`）：
        #   · table      —— 读 `customer_memory`（跨次稳定事实，M0 默认）
        #   · assessment —— 退回"只读上次评估"（零代码回滚；老库/排查时用）
        # 无论哪种，语义都不变：记忆不是证据、不带编号、不进 index。
        mems = None
        if str(db.setting("ctx_memory_source", "CTX_MEMORY_SOURCE", "table")).lower() == "table":
            import memory_repo
            mems = memory_repo.active_rows(rel)
        seg1, seg2 = _render_memory(last_assessment(rel), mems)

    def compose(msg_text: str) -> str:
        """把记忆块与消息行拼成最终 text。**①② 永远在最前，且不参与任何裁剪。**"""
        return "\n\n".join(p for p in (seg1, seg2, msg_text) if p)

    # ── 预算与裁剪（架构 §4.5.4）：先整段砍 ⑤ → ④ 分组收缩 → 砍 ③ 最早 ────
    tokens_max = db.setting_int("ctx_tokens_max", "CTX_TOKENS_MAX", 12000)
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
            for _rid, rs in by_rule.items():
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
