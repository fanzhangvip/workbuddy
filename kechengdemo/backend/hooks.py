# -*- coding: utf-8 -*-
"""显式钩子（架构 §2.4 的 H3）：把散落各处的「护栏 if」收敛为 4 个统一判定点。

## 为什么需要它

改造前，护栏是散在各处的 `if`：预算在 `_run` 循环里、正文长度在 `_score_one` 开头、
回写开关在 `_writeback` 内部、落库前则**完全没有检查**。后果：
· 新增一条护栏要动主流程，且很容易漏配（图路径与旧路径各写一遍）；
· 「这个会话为什么被跳过」没有一个统一的、可断言的答案（只能看代码）；
· 无法单独测试某一条护栏（要跑整条链路）。

## 统一契约

每个 hook 返回 `HookResult(allow, reason, detail)`：
· ``allow=True``  → 放行；``detail`` 携带体检报告（**只用于观测，不参与决策**）
· ``allow=False`` → 调用方**必须**拒绝，并按 ``reason`` 决定后续（跳过 / 转人工 / 降级）

## 两条设计纪律

1. **不复制已有实现**。pydantic 双层契约、极性校验、证据回链、覆盖率都已实现且被
   数十条测试盯着；hook 只做「统一入口 + 汇总 + 留痕」，判定仍委托原实现
   （`guard_reason` 是本模块唯一的判定本体，其余为读取已有结果）。
2. **保持纯函数**。所有 I/O（查库、读配置）由调用方完成后**作为参数传入** ——
   这样 hook 可以被穷举边界测试，也不会因 import `scoring_service` 造成循环依赖。

四条护栏与文档编号的对应：
· `before_llm_call`   整轮超时 G3 · 成本预算 G4 · 有效信息量 B4
· `after_llm_output`  结构 闸4 · 极性 B2 · 证据回链 A1 · 覆盖率 闸2 · 修正重试 G2
· `before_writeback`  回写开关 D2 · 幂等 · 配额 边界3
· `before_persist`    落库前完整性校验 H3
"""
import time
from typing import NamedTuple

# ============================================================ 统一契约


class HookResult(NamedTuple):
    """钩子判定结果。

    ``reason`` 在放行时恒为 ``"ok"``；拒绝时是**稳定的机器可读标识**
    （不要用它做展示 —— 展示文案见 `REASON_CN`，两者刻意分开，
    便于前端本地化而不必改判定逻辑）。
    """

    allow: bool
    reason: str = "ok"
    detail: dict = None

    @property
    def denied(self) -> bool:
        return not self.allow

    def __bool__(self) -> bool:                     # 允许 `if hook(...)` 直接判真
        return self.allow


#: 拒绝原因的展示文案（前端/日志用；判定逻辑不依赖它）
REASON_CN = {
    "ok": "放行",
    "deadline": "达到整轮总超时",
    "max_llm_calls": "达到大模型调用次数上限",
    "insufficient_content": "有效消息条数不足",
    "no_positive": "无正分，无需回写",
    "no_delta": "相对已回写水位无增量（D2-1 差值累加）",
    "mode_off": "回写功能已关闭",
    "mode_dry_run": "演练模式：仅记录，未真实写入",
    "already_written": "该评估已回写过（幂等）",
    "quota_run": "本轮回写配额已用尽",
    "quota_customer": "该客户今日回写次数已用尽",
    "polarity_violation": "命中项极性与规则不符",
    "missing_field": "落库必填字段缺失",
    "bad_relation_key": "关系键不合法",
}

# `_writeback` 的 status 与 hook reason 的映射（保持与改造前完全一致）
REASON_TO_WB_STATUS = {
    "ok": "ok",                       # 仅「放行」才真投递
    "no_positive": "skipped",
    "mode_off": "skipped",
    "mode_dry_run": "dry_run",
    "already_written": "skipped",
    "quota_run": "skipped",
    "quota_customer": "skipped",
    "no_delta": "skipped",
}


# ============================================================ 判定本体（纯函数）

def guard_reason(deadline_ts, max_llm_calls, llm_calls, now=None):
    """整轮护栏（架构 §2.2 的 G3 总超时 / G4 成本预算）。

    返回 ``None``（可继续）或原因字符串（``"deadline"`` / ``"max_llm_calls"``）。
    **纯函数**：不读库、不看系统时间（``now`` 可注入），便于单测。

    - ``deadline_ts`` 为空（None 或 0）→ 不限时
    - ``max_llm_calls`` 为空或 ≤0 → 不限量
    """
    if deadline_ts:
        now = time.time() if now is None else now
        if float(now) >= float(deadline_ts):
            return "deadline"
    if max_llm_calls and int(max_llm_calls) > 0 and int(llm_calls or 0) >= int(max_llm_calls):
        return "max_llm_calls"
    return None


# ============================================================ ① before_llm_call

def before_llm_call(*, kept=None, min_kept=None, deadline=None, max_llm_calls=None,
                    llm_calls=0, now=None,
                    customer_msgs=None, min_customer_msgs=None,
                    text_len=None, min_effective_chars=None) -> HookResult:
    """调模型之前的所有闸门。

    文档对应：**G3** 整轮超时 · **G4** 成本预算 · **B4** 有效信息量。
    改造前这三项分散在 `_run` 循环与 `_score_one` 开头两处，且图路径各写一遍。

    ``kept`` / ``min_kept`` 传 None 表示「本次不做内容量检查」（如轮次级调用只看预算）。

    **B4 为什么是三个口径**（架构 §2.3）：单看「保留条数」会被钻空子 ——
    销售单方面刷屏也能把条数刷上去，而客户一个字没说；单看「字符数」又会被
    长语音转写骗过。三个判据**全部满足**才算有效信息量足够：
    · ``kept`` ≥ ``min_kept``                 —— 规范化后条数（原来的唯一口径）
    · ``customer_msgs`` ≥ ``min_customer_msgs`` —— **客户真的开口**（0 = 不限）
    · ``text_len`` ≥ ``min_effective_chars``   —— 文本总长度（0 = 不限）
    """
    # G3 / G4：整轮级别的护栏（优先判定 —— 撞了总闸就不该再考虑单个会话）
    g = guard_reason(deadline, max_llm_calls, llm_calls, now=now)
    if g:
        return HookResult(False, g, {"llm_calls": int(llm_calls or 0),
                                     "max_llm_calls": max_llm_calls})
    # B4：会话级「有效信息量」—— 三个口径都过才评（省一次模型调用）
    detail = {"kept": kept, "customer_msgs": customer_msgs, "text_len": text_len}
    if kept is not None and min_kept is not None and int(kept) < int(min_kept):
        detail.update({"min_kept": int(min_kept), "failed_on": "kept"})
        return HookResult(False, "insufficient_content", detail)
    if (customer_msgs is not None and min_customer_msgs
            and int(customer_msgs) < int(min_customer_msgs)):
        detail.update({"min_customer_msgs": int(min_customer_msgs), "failed_on": "customer_msgs"})
        return HookResult(False, "insufficient_content", detail)
    if (text_len is not None and min_effective_chars
            and int(text_len) < int(min_effective_chars)):
        detail.update({"min_effective_chars": int(min_effective_chars),
                       "failed_on": "text_len"})
        return HookResult(False, "insufficient_content", detail)
    return HookResult(True, "ok", detail)


# ============================================================ ② after_llm_output

def after_llm_output(*, pos_items=None, neg_items=None, unchecked=None,
                     invalid_items=None, rules=None, total_llm=None) -> HookResult:
    """模型输出之后的体检：结构 / 极性 / 证据 / 覆盖率。

    文档对应：**闸4** 结构 · **B2** 极性 · **A1** 证据回链 · **闸2** 覆盖率。

    ⚠️ 这些校验**已经在别处执行**（pydantic 契约在 `contracts`，极性在
    `rules.validate_items`，证据回链在 `analyze._attach_evidence`，
    覆盖率在 `rules.check_coverage`）—— 本 hook **不重复校验**，
    而是把结果**汇总成一份体检报告**并做一次「极性终检」：
    若负分规则混进了加分项（B2 的典型失效），这是**流程错误**而非模型错误，
    必须拒绝而不是静默容忍。

    ``rules`` 与 ``total_llm`` 都由调用方传入（**本模块不 import 项目模块**）：
    ``rules`` 用于极性终检，``total_llm`` = 应表态的规则数（用于算覆盖率）。

    返回 ``allow=True`` 时 ``detail`` 即体检报告（用于观测「模型判得干净不干净」）。
    """
    pos = list(pos_items or [])
    neg = list(neg_items or [])
    unchecked = list(unchecked or [])
    invalid = list(invalid_items or [])

    # 极性终检（B2）：按规则表的极性分类，负分规则不得出现在加分项里
    bad = []
    if rules:
        idx = {}
        for grp, pol in (("positive_rules", "positive"), ("negative_rules", "negative")):
            for r in (rules.get(grp) or []):
                if r.get("rule_id"):
                    idx[r["rule_id"]] = pol
        for it in pos:
            rid = (it or {}).get("rule_id")
            if rid and idx.get(rid) == "negative":
                bad.append(rid)
        for it in neg:
            rid = (it or {}).get("rule_id")
            if rid and idx.get(rid) == "positive":
                bad.append(rid)

    total = int(total_llm or 0)
    n_items = len(pos) + len(neg)

    detail = {
        "hits": n_items,
        "unchecked": len(unchecked),
        "coverage": round(1 - len(unchecked) / total, 4) if total else None,
        "invalid": len(invalid),
        "invalid_ratio": round(len(invalid) / n_items, 4) if n_items else 0.0,
        "polarity_violations": bad,
    }
    if bad:
        return HookResult(False, "polarity_violation", detail)
    return HookResult(True, "ok", detail)


# ============================================================ ③ before_writeback

def before_writeback(*, positive_total=0, mode="off", already_written=False,
                     quota_run=0, quota_run_limit=0,
                     quota_customer=0, quota_customer_limit=0,
                     written_total=None, delta_mode=True) -> HookResult:
    """回写微伴之前的判定：开关 / 幂等 / 配额 / **差值累加（D2-1）**。

    文档对应：**D2** 副作用开关 · **边界3** 副作用配额 · **D2-1** 差值累加 · 幂等。

    ## D2-1（差值累加）为什么在这里判定

    微伴「客户评分」是**累计行为分、无覆盖接口**（架构 §10 D2）。所以每次回写必须是
    **增量** ``本次 positive_total − 已回写累计(watermark)``，而不是整值 ——
    整值回写在累加语义下会让客户分数虚高（同一次评估被反复累加）。

    · ``written_total`` = 客户级回写水位（`writeback_ledger.written_total`），
      由调用方查好传入（**保持本模块纯函数**）
    · 差值为 0 或不正 → ``no_delta`` 直接跳过（**一次接口都不调**，
      避免无意义的网络往返，也避免微伴侧产生 0 分记录）
    · ``delta_mode=False`` → 退回改造前的整值语义（回滚开关）

    调用方需先查好 ``already_written`` 与两个配额计数（**仅在对应上限 > 0 时才查**，
    默认 0 = 不限 → 不产生额外 DB 往返）。
    """
    if int(positive_total or 0) < 1:
        return HookResult(False, "no_positive", {"positive_total": positive_total})
    if mode == "off":
        return HookResult(False, "mode_off", {"mode": mode})
    if already_written:
        return HookResult(False, "already_written", {})
    if quota_run_limit and int(quota_run) >= int(quota_run_limit):
        return HookResult(False, "quota_run", {"used": quota_run, "limit": quota_run_limit})
    if quota_customer_limit and int(quota_customer) >= int(quota_customer_limit):
        return HookResult(False, "quota_customer",
                          {"used": quota_customer, "limit": quota_customer_limit})
    # D2-1：差值（放在配额之后、dry_run 之前 —— 差值判定与是否演练无关）
    delta = int(positive_total or 0)
    if delta_mode and written_total is not None:
        delta = int(positive_total or 0) - int(written_total or 0)
        if delta <= 0:
            return HookResult(False, "no_delta",
                              {"positive_total": int(positive_total or 0),
                               "written_total": int(written_total or 0), "delta": delta})
    if mode == "dry_run":
        return HookResult(False, "mode_dry_run",
                          {"mode": mode, "delta": delta, "written_total": written_total})
    return HookResult(True, "ok", {"mode": mode, "delta": delta,
                                   "written_total": written_total})


# ============================================================ ④ before_persist

def before_persist(*, rel_key=None, staff_id=None, target_id=None, session_id=None,
                   score=None, grade=None, rule_version=None) -> HookResult:
    """落库前的完整性校验（H3）。

    改造前**没有这层**：缺字段会一路走到 SQL 才炸（报 `NotNullViolation`，
    堆栈里看不出是「哪个会话、缺什么」）。本 hook 把它变成**明确的拒绝 + 原因**，
    并且拒绝是**安全的** —— 调用方按「失败」处理（计 `session_failed`、写
    `session_failure` 明细），比整轮 traceback 好得多。

    只校验「结构性必填项」，不校验业务合理性（那是规则层的职责）。
    """
    if not rel_key or ":" not in str(rel_key):
        return HookResult(False, "bad_relation_key", {"rel_key": rel_key})
    if not staff_id or not target_id or not session_id:
        return HookResult(False, "missing_field",
                          {"staff_id": staff_id, "target_id": target_id,
                           "session_id": session_id})
    score = score or {}
    for k in ("raw_score", "positive_total", "negative_total"):
        if score.get(k) is None:
            return HookResult(False, "missing_field", {"score": score, "missing": k})
    if not (grade or {}).get("grade"):
        return HookResult(False, "missing_field", {"grade": grade})
    if not rule_version:
        return HookResult(False, "missing_field", {"rule_version": rule_version})
    return HookResult(True, "ok", {})
