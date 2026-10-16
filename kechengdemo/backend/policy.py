# -*- coding: utf-8 -*-
"""Policy 策略层：把「已经算出来的置信度」接成「允许执行到什么程度」。修硬伤 4。

## 为什么需要它

`confidence.py` 已经把置信度算得很扎实（命中项级 → 会话级 → 复核触发），
但改造前下游**只用了 `needs_human_review` 一个布尔**，于是只剩两种结局：
「自动」或「进人工队列」。后果代码注释里已经写明：**实测复核率 90%~92%，
队列等于全量，分流意义丧失**；同时高置信度结论也没有获得任何自动执行的加速。

文章 G 的核心公式 `AI → Decision + Confidence → Policy → Action` 里，
本项目以前只走到了 Decision 和 Confidence。本模块补上后两段。

## 两个刻意的设计取舍

1. **只拦「真有副作用」的动作**。dry_run 不写外部系统，没有拦的必要 ——
   否则会改变默认配置下的行为、把已有测试与运维习惯全部打乱。
   调用方只在 `mode == "live"` 时才查 Policy。
2. **阈值随判定一起落库**（`policy_decision.thresholds`）。阈值会随业务调整，
   只存结论不存判据的话，三个月后没人能解释「这条当时为什么自动执行了」。

## 与 `review_reasons` 的关系

`review_reasons` 回答「要不要人看一眼」；Policy 回答「机器能不能自己动手」。
两者不是一回事：一个结论可以「值得人看一眼」而**绝不自动写外部系统**。
"""
import json

import db

AUTO, CONFIRM, HUMAN = "auto", "confirm", "human"

#: 默认阈值（可用 `policy_auto_min` / `policy_confirm_min` 覆盖）
DEFAULT_AUTO_MIN = 0.95
DEFAULT_CONFIRM_MIN = 0.80

#: 风险等级达到多少就必须人工签字（架构 §7.2：1低 2中 3高 4禁区）
CONFIRM_RISK_LEVEL = 3

#: 阻断「自动执行」的复核原因 —— 这些原因说明判据本身不稳，或含需人判定的成分。
#: 刻意**不含** `engine_negative_only`：它是「客户连续 48h 未回复」这类业务信号，
#: 命中率约 90%（`confidence.py` 注释自陈），若也阻断自动执行则等于「永远不自动」，
#: Policy 层就退化成一个关闭开关。它仍会让结论进复核队列（`needs_human_review`），
#: 只是不阻断「机器自己动手」这一层。
DEFAULT_BLOCKING = ("has_negative", "grade_flip", "weak_evidence", "polarity_violation")


def thresholds() -> dict:
    """当前阈值（app_setting > .env > 默认）。"""
    return {
        "auto": db.setting_float("policy_auto_min", "POLICY_AUTO_MIN", DEFAULT_AUTO_MIN),
        "confirm": db.setting_float("policy_confirm_min", "POLICY_CONFIRM_MIN",
                                    DEFAULT_CONFIRM_MIN),
        "blocking": list(DEFAULT_BLOCKING),
        "confirm_risk_level": CONFIRM_RISK_LEVEL,
    }


def decide(*, session_confidence, review_reasons=None, risk_level: int = 1,
           grade: str | None = None, thresholds_override: dict | None = None) -> dict:
    """三档判定。**纯函数**（阈值可注入），便于穷举边界测试。

    返回 ``{"action", "action_reasons", "thresholds"}``：

    - ``action``：``auto`` / ``confirm`` / ``human``
    - ``action_reasons``：机器可读的原因列表（与展示文案分离，
      这样前端本地化不必改判定逻辑）
    - ``thresholds``：本次判定用的阈值快照（落库用，保证结论可复现）

    判定顺序（**顺序本身是语义**）：

    1. 高风险动作 → 一律要人确认，**无论置信度多高**。
       置信度衡量的是「判得准不准」，不是「这个动作能不能自己做主」——两件事。
    2. 命中阻断性复核原因 → 不能自动执行。
    3. 无置信度 → 保守走人工（**不能默认放行**）。
    4. 按阈值三档。
    """
    th = thresholds_override or thresholds()
    auto_min = float(th.get("auto", DEFAULT_AUTO_MIN))
    confirm_min = float(th.get("confirm", DEFAULT_CONFIRM_MIN))
    blocking = set(th.get("blocking") or DEFAULT_BLOCKING)
    risk_confirm = int(th.get("confirm_risk_level", CONFIRM_RISK_LEVEL))

    reasons = list(review_reasons or [])
    conf = session_confidence
    hit_blocking = sorted(set(reasons) & blocking)
    out_reasons = []

    if int(risk_level or 1) >= risk_confirm:
        out_reasons.append("high_risk_action")
    if hit_blocking:
        out_reasons.append("blocking_review_reason")
        out_reasons += [f"reason:{r}" for r in hit_blocking]

    # ⚠️ 顺序即语义（2026-10-16 修正）：**低置信度优先于"高风险/阻断原因"**。
    # 之前 `out_reasons` 先判，于是 conf=0.1 且高风险会得到 `confirm` —— 也就是
    # "有人点一下批准就能把一条 0.1 置信度的结论写进微伴"，与 human 的定义
    # （"只进人工复核，不进自动链路"）自相矛盾。
    if conf is None:
        out_reasons.append("no_confidence")
        action = HUMAN
    elif float(conf) < confirm_min:
        out_reasons.append("confidence_low")
        action = HUMAN
    elif out_reasons:
        # 置信度够，但有高风险动作或阻断性复核原因 → 必须人工二次确认
        action = CONFIRM
    elif float(conf) >= auto_min:
        out_reasons.append("confidence_high")
        action = AUTO
    else:
        out_reasons.append("confidence_mid")
        action = CONFIRM

    return {"action": action, "action_reasons": out_reasons, "thresholds": th}


def record(*, relationship_key: str, action: str, session_confidence=None,
           review_reasons=None, action_reasons=None, thresholds_snapshot=None,
           assessment_id: int | None = None, task_id: int | None = None):
    """把一次判定落 `policy_decision`（含阈值快照）。失败不抛（旁路观测）。"""
    try:
        row = db.query_one(
            """INSERT INTO policy_decision
                 (task_id, relationship_key, assessment_id, session_confidence,
                  review_reasons, action, action_reasons, thresholds)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s::jsonb)
               RETURNING decision_id""",
            (task_id, relationship_key, assessment_id, session_confidence,
             json.dumps(list(review_reasons or []), ensure_ascii=False),
             action,
             json.dumps(list(action_reasons or []), ensure_ascii=False),
             json.dumps(thresholds_snapshot or {}, ensure_ascii=False)))
        return (row or {}).get("decision_id")
    except Exception as e:                      # noqa: BLE001
        print(f"[policy] 写 policy_decision 失败（不影响评分）：{type(e).__name__}: {e}")
        return None


def stats(days: int = 30) -> dict:
    """策略分流观测：三档占比 + 原因分布（回答「复核队列为什么这么大」）。"""
    rows = db.query(
        """SELECT action, count(*) AS n FROM policy_decision
            WHERE created_at >= now() - (%s || ' days')::interval
            GROUP BY 1 ORDER BY 2 DESC""", (str(int(days)),))
    total = sum(int(r["n"]) for r in rows) or 0
    dist = {r["action"]: int(r["n"]) for r in rows}
    reasons = db.query(
        """SELECT r AS reason, count(*) AS n
             FROM policy_decision, jsonb_array_elements_text(action_reasons) AS r
            WHERE created_at >= now() - (%s || ' days')::interval
            GROUP BY 1 ORDER BY 2 DESC LIMIT 15""", (str(int(days)),))
    return {"days": days, "total": total, "distribution": dist,
            "rates": {k: (round(v / total, 4) if total else 0.0)
                      for k, v in dist.items()},
            "reasons": reasons, "thresholds": thresholds()}
