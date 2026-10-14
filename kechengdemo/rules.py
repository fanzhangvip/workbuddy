# -*- coding: utf-8 -*-
"""评分规则加载与计分逻辑（规则引擎部分）。

规则表来自 rules.json，可人工修改；本模块负责：
- 加载规则与等级阈值
- 校验大模型返回的命中项是否合法（rule_id 必须存在、分数必须属于该规则的档位）
- 计算总分（同维度取最高、跨维度累加、上下限截断）与等级映射
"""
import json
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parent / "rules.json"


def load_rules(path: Path = RULES_PATH) -> dict:
    """加载规则表。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _index(rules: dict) -> dict:
    """建立 rule_id -> 规则定义 的索引。"""
    idx = {}
    for r in rules.get("positive_rules", []):
        idx[r["rule_id"]] = {**r, "polarity": "positive"}
    for r in rules.get("negative_rules", []):
        idx[r["rule_id"]] = {**r, "polarity": "negative"}
    return idx


def validate_items(items: list, rules: dict, expect_polarity: str = None) -> tuple:
    """校验并规范化模型返回的命中项。

    返回 (合法项列表, 非法项列表)。合法项会补齐 polarity / dimension。

    :param expect_polarity: 传 ``"positive"`` / ``"negative"`` 时做**跨列表极性校验** ——
        模型把扣分规则误放进 ``positive_items`` 会直接污染 ``positive_total``，
        而原来的实现只看 rule_id 与分值，发现不了这种错位。
    """
    idx = _index(rules)
    valid, invalid = [], []
    for it in items or []:
        rid = it.get("rule_id")
        rule = idx.get(rid)
        if not rule:
            invalid.append({**it, "reason": f"未知规则 {rid}"})
            continue
        if expect_polarity and rule["polarity"] != expect_polarity:
            invalid.append({**it, "reason": f"{rid} 是{rule['polarity']}规则，"
                                            f"不应出现在 {expect_polarity}_items 中"})
            continue
        allowed = {opt["score"] for opt in rule.get("options", [])}
        if it.get("score") not in allowed:
            invalid.append({**it, "reason": f"{rid} 的分值 {it.get('score')} 不在允许档位 {sorted(allowed)}"})
            continue
        valid.append({
            "rule_id": rid,
            "dimension": rule.get("dimension"),
            "polarity": rule["polarity"],
            "score": it["score"],
            "evidence": it.get("evidence") or [],
            # 模型给出的「#编号」引用，由 analyze 回链成 message_id（需求 §6.3）
            "evidence_ids": it.get("evidence_ids") or [],
            "reason": it.get("reason", ""),
        })
    return valid, invalid


# ===========================================================================
# 逐维度表态（契约层）：覆盖率检查 + 由表态派生命中项
# ===========================================================================

def llm_rule_ids(rules: dict) -> list:
    """需要大模型逐条表态的规则 id（**排除 engine=rule 的规则**）。

    目前 15 条规则里只有 ``negative_no_reply_2d``（48h 未回复）是 ``engine=rule``，
    它由 ``compute_facts`` + 规则引擎算，**不允许模型判定** —— 要求模型对它表态，
    既浪费 token，又会与引擎计算的扣分重复。
    """
    return sorted(rid for rid, r in _index(rules).items() if r.get("engine") != "rule")


def _pick(rc, key):
    """从表态项里取值，同时兼容 pydantic 对象与 dict。

    不能写成 ``getattr(rc, key, None) or rc.get(key)`` —— ``hit=False`` / ``score=None``
    都是 falsy，会继续去求值右边，而 pydantic 模型没有 ``.get``，直接 AttributeError。
    """
    if isinstance(rc, dict):
        return rc.get(key)
    return getattr(rc, key, None)


def check_coverage(rule_checks: list, rules: dict) -> list:
    """返回「模型漏表态」的规则 id 列表（覆盖率的分母 = :func:`llm_rule_ids`）。

    这是把「漏判」从不可检测变成可计数的那一步：以前「模型没看这条规则」与
    「这条规则确实没命中」的输出长得一模一样。
    """
    expected = set(llm_rule_ids(rules))
    seen = {_pick(rc, "rule_id") for rc in (rule_checks or [])}
    return sorted(expected - {s for s in seen if s})


def derive_items(rule_checks: list, rules: dict) -> tuple:
    """由「逐维度表态」派生命中项。

    返回 ``(加分项, 扣分项, 非法项)``；item 结构与改造前**完全一致**，
    因此前端评分中心 / 负分复核页的渲染无需任何改动。
    """
    idx = _index(rules)
    pos, neg, invalid = [], [], []
    for rc in rule_checks or []:
        rid = _pick(rc, "rule_id")
        hit = bool(_pick(rc, "hit"))
        score = _pick(rc, "score")
        reason = _pick(rc, "reason") or ""
        eids = list(_pick(rc, "evidence_ids") or [])

        rule = idx.get(rid)
        if rule is None:
            invalid.append({"rule_id": rid, "score": score, "evidence_ids": eids,
                            "reason": f"未知规则 {rid}"})
            continue
        if rule.get("engine") == "rule":
            continue                       # 引擎规则：模型的表态一律忽略，不计入命中也不计漏检
        if not hit:
            continue

        allowed = {opt["score"] for opt in rule.get("options", [])}
        if score not in allowed:
            invalid.append({"rule_id": rid, "score": score, "evidence_ids": eids,
                            "reason": f"{rid} 的分值 {score} 不在允许档位 {sorted(allowed)}"})
            continue

        item = {"rule_id": rid, "dimension": rule.get("dimension"),
                "polarity": rule["polarity"], "score": score,
                "evidence": [], "evidence_ids": eids, "reason": reason}
        (pos if rule["polarity"] == "positive" else neg).append(item)
    return pos, neg, invalid


def compute_score(positive_items: list, negative_items: list, rules: dict) -> dict:
    """计算总分：加分维度取最高后累加（上限 +13），扣分可叠加（下限 -12）。"""
    bounds = rules.get("score_bounds", {"min": -12, "max": 13})

    # 加分：同一 rule_id 只取最高档
    best_pos = {}
    for it in positive_items:
        best_pos[it["rule_id"]] = max(best_pos.get(it["rule_id"], it["score"]), it["score"])
    pos_total = sum(best_pos.values())
    pos_total = min(pos_total, bounds["max"])

    # 扣分：可叠加
    neg_total = sum(it["score"] for it in negative_items)
    neg_total = max(neg_total, bounds["min"])

    raw = pos_total + neg_total
    raw = max(bounds["min"], min(bounds["max"], raw))
    return {"positive_total": pos_total, "negative_total": neg_total, "raw_score": raw}


def grade_of(raw_score: int, rules: dict) -> dict:
    """按等级阈值映射等级。"""
    for th in rules.get("grade_thresholds", []):
        if raw_score >= th["min"]:
            return th
    return {"grade": "D", "priority": "暂停", "advice": ""}


def rules_brief_for_prompt(rules: dict) -> str:
    """把规则表转成给大模型看的精简文本。"""
    lines = []
    lines.append(f"规则版本：{rules.get('rule_version')}")
    lines.append("")
    lines.append("【加分规则】")
    for r in rules.get("positive_rules", []):
        opts = "；".join(f"+{o['score']}：{o['criteria']}" for o in r.get("options", []))
        lines.append(f"- {r['rule_id']}（{r['dimension']}）：{opts}")
    lines.append("")
    lines.append("【扣分规则】")
    for r in rules.get("negative_rules", []):
        opts = "；".join(f"{o['score']}：{o['criteria']}" for o in r.get("options", []))
        extra = "（此项由系统规则引擎计算，你不需要判断）" if r.get("engine") == "rule" else ""
        lines.append(f"- {r['rule_id']}（{r['dimension']}）：{opts}{extra}")
    lines.append("")
    lines.append("【冲突规则】")
    lines.append("- 客户在具备学习或业务需求时主动问价格，按「主动问课」判断；仅在未了解价值的前提下反复只比价，才计「追着问价格」。")
    lines.append("- 「实际到课 +2」须有到课事件或到课标签佐证；仅凭聊天记录不得给出 +2，最多按「已确认参加但尚未到课 +1」。")
    return "\n".join(lines)
