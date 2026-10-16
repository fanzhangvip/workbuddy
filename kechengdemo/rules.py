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
    """加载规则表，并做**加载期合法性校验**（INV-12 / INV-13）。

    为什么必须在加载期拦（而不是等到运行时）：
    `rules.json` 是**静态数据**，但静态数据同样会有 Bug —— 一个分值放到错误档位、
    一条规则漏了 options，编译器不报错、单测也测不到，只会在某次真实评分里
    **静默算错分**（甚至把加分算成扣分）。所以这里"宁可响亮地失败"：
    违反约束立即抛 `RulesInvalidError` 并指出是哪条规则、哪条约束。
    """
    rules = json.loads(Path(path).read_text(encoding="utf-8"))
    errs = validate_rules(rules)
    if errs:
        raise RulesInvalidError(
            f"规则表 {Path(path).name} 不合法，共 {len(errs)} 处：\n  - "
            + "\n  - ".join(errs))
    return rules


class RulesInvalidError(ValueError):
    """规则表违反加载期合法性约束（INV-12 / INV-13）。"""


def validate_rules(rules: dict) -> list:
    """返回违规清单（空列表 = 合法）。**纯函数**，便于对样例规则做测试。

    对应 ARCHITECTURE.md §6.7：
      INV-12  `grade_thresholds` 的 min 严格递减且覆盖分数下界；`score_bounds.min < max`
      INV-13  同一规则的 option 分值互不相同；`polarity` 由分组决定（**不得写在规则体里**）；
              `engine='rule'` 的规则不出现在 `llm_rule_ids()`
    """
    errs = []

    # ---- INV-12a：分值域 ----
    bounds = rules.get("score_bounds") or {}
    if bounds.get("min") is None or bounds.get("max") is None:
        errs.append("score_bounds 缺少 min/max：总分截断无依据")
    elif int(bounds["min"]) >= int(bounds["max"]):
        errs.append(f"score_bounds.min({bounds['min']}) 必须严格小于 max({bounds['max']})")

    # ---- INV-12b：等级阈值 ----
    ths = rules.get("grade_thresholds") or []
    if not ths:
        errs.append("grade_thresholds 为空：任何分数都映射不到等级")
    else:
        mins = [t.get("min") for t in ths]
        if any(m is None for m in mins):
            errs.append("grade_thresholds 存在缺少 min 的档位")
        else:
            nums = [int(m) for m in mins]
            for prev, cur in zip(nums, nums[1:]):
                if prev <= cur:
                    errs.append(f"grade_thresholds 的 min 必须**严格递减**，实际 {nums}")
                    break
            floor = int(bounds.get("min")) if bounds.get("min") is not None else None
            if floor is not None and nums[-1] > floor:
                errs.append(f"grade_thresholds 最低档 min={nums[-1]} 未覆盖分数下界 "
                            f"{floor}：{floor}~{nums[-1] - 1} 这段分数映射不到等级")
        if any(not t.get("grade") for t in ths):
            errs.append("grade_thresholds 存在缺少 grade 的档位")
        grades = [t.get("grade") for t in ths if t.get("grade")]
        if len(grades) != len(set(grades)):
            errs.append(f"grade_thresholds 的等级重复：{grades}")

    # ---- INV-13：逐条规则 ----
    for key, polarity in (("positive_rules", "positive"), ("negative_rules", "negative")):
        seen = set()
        for i, r in enumerate(rules.get(key) or []):
            rid = r.get("rule_id") or f"{key}[{i}]"
            if not r.get("rule_id"):
                errs.append(f"{key}[{i}] 缺少 rule_id")
            if rid in seen:
                errs.append(f"{rid} 在同组内重复定义")
            seen.add(rid)
            if not r.get("dimension"):
                errs.append(f"{rid} 缺少 dimension")
            # polarity 必须由**分组**决定，不得写进规则体（否则同一规则可能两处不一致）
            if "polarity" in r:
                errs.append(f"{rid} 不应包含 polarity 字段：极性由所在分组决定")
            if r.get("engine") not in (None, "rule"):
                errs.append(f"{rid} 的 engine={r.get('engine')!r} 非法（只允许 'rule' 或省略）")
            if r.get("engine") == "rule" and polarity != "negative":
                # `_engine_negative_items()` 只实现了负向硬指标；正向引擎规则会被静默忽略
                errs.append(f"{rid} 是正向规则但 engine=rule：当前只支持负向引擎规则，"
                            f"会被静默忽略")
            opts = r.get("options") or []
            if not opts:
                errs.append(f"{rid} 没有 options：这条规则永远不会命中")
                continue
            scores = [o.get("score") for o in opts]
            if any(s is None for s in scores):
                errs.append(f"{rid} 存在缺少 score 的档位")
            else:
                nums = [int(s) for s in scores]
                if len(nums) != len(set(nums)):
                    errs.append(f"{rid} 的档位分值重复：{nums}（同一规则内必须唯一）")
                # 分值符号必须与极性一致 —— 这是**最致命**的一类静态数据错误：
                # 符号写反会让"扣分"变成"加分"，而单测很难覆盖到每条规则。
                if polarity == "positive" and any(n <= 0 for n in nums):
                    errs.append(f"{rid} 是加分规则但存在非正分值：{nums}")
                if polarity == "negative" and any(n >= 0 for n in nums):
                    errs.append(f"{rid} 是扣分规则但存在非负分值：{nums}")
            if any(not (o.get("criteria") or "").strip() for o in opts):
                errs.append(f"{rid} 存在缺少 criteria 的档位：模型无法判断该档")
    return errs


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


# ---------------- 引擎硬指标（不由大模型判断） ----------------
# 原先这三个住在 `ScoringService` 里（`_engine_rule_ids` 是 staticmethod、
# `_engine_negative_items` 是实例方法但**不用任何实例状态**）。它们属于**规则引擎**
# 的职责，不属于「跑批任务」的职责；而 `eval_runner` 为了调它们得去拿
# `scoring_service.service` —— 那正是 eval_runner ↔ scoring_service 那组环的一半。

def engine_rule_ids(rules: dict) -> set:
    """由规则引擎硬算、不允许大模型判定的规则（如 48h 未回复）。"""
    return {r["rule_id"] for r in rules.get("negative_rules", [])
            if r.get("engine") == "rule"}


def rule_tier_score(rules: dict, rule_id: str, default: int = 0) -> int:
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


def engine_negative_items(facts: dict, rules: dict) -> list:
    """规则引擎补充的硬指标扣分项（不由大模型判断）。

    ⚠️ 只处理「销售正常跟进、客户连续 48 小时未回复」这一条硬指标；
    它**不会**因为销售单方面群发而加分 —— 需求 4.2 明确要求
    「销售主动发送但客户未回复时，消息必须入库、可更新上下文，
    但不应仅因销售单方面群发而大幅提高客户分数」。
    """
    items = []
    if facts.get("no_reply_48h"):
        items.append({
            "rule_id": "negative_no_reply_2d",
            "score": rule_tier_score(rules, "negative_no_reply_2d", -2),
            "evidence": [f"距最后一条员工消息已 {facts.get('hours')} 小时无客户回复"],
            "reason": "规则引擎：销售正常跟进后，客户连续 48 小时未回复（如客户已说明忙/晚点聊，人工可驳回）",
        })
    valid, _bad = validate_items(items, rules)
    return valid


def rules_brief_for_prompt(rules: dict) -> str:
    """把规则表转成给大模型看的精简文本。

    **闸1/F5（架构 §2.3）**：规则除了自然语言 ``criteria``，还可带
    ``examples``（``positive`` / ``negative`` / ``ambiguous`` 三档）。
    为什么加示例：``criteria`` 一句话在边界情况上必然有歧义（"在职编程技术岗"
    对"运维算不算"没有答案），而示例把边界钉死 —— 这是提升判定一致率最便宜的手段。
    没有 ``examples`` 时输出与改造前**逐字一致**（向后兼容）。
    """
    def _opt(o, sign=""):
        t = f"{sign}{o['score']}：{o['criteria']}"
        ex = o.get("examples") or {}
        if not ex:
            return t
        parts = []
        # 三档示例：分别代表「典型命中」「明确不命中」「看着像但不算」
        for key, label in (("positive", "命中示例"), ("negative", "不命中示例"),
                           ("ambiguous", "边界示例")):
            vals = [str(x).strip() for x in (ex.get(key) or []) if str(x).strip()]
            if vals:
                parts.append(f"{label}：" + "｜".join(vals))
        return t + ("（" + "；".join(parts) + "）" if parts else "")

    lines = []
    lines.append(f"规则版本：{rules.get('rule_version')}")
    lines.append("")
    lines.append("【加分规则】")
    for r in rules.get("positive_rules", []):
        opts = "；".join(_opt(o, "+") for o in r.get("options", []))
        lines.append(f"- {r['rule_id']}（{r['dimension']}）：{opts}")
    lines.append("")
    lines.append("【扣分规则】")
    for r in rules.get("negative_rules", []):
        opts = "；".join(_opt(o) for o in r.get("options", []))
        extra = "（此项由系统规则引擎计算，你不需要判断）" if r.get("engine") == "rule" else ""
        lines.append(f"- {r['rule_id']}（{r['dimension']}）：{opts}{extra}")
    lines.append("")
    lines.append("【冲突规则】")
    # 闸1/F5：`conflict_priority` 让「两条规则同时像命中时先算哪条」可配置，
    # 而不是写死在提示词里（原来只有两条硬编码的说明文字）。
    prios = sorted([r for r in rules.get("positive_rules", []) + rules.get("negative_rules", [])
                    if r.get("conflict_priority")],
                   key=lambda x: -int(x["conflict_priority"]))
    for r in prios[:8]:
        tip = r.get("conflict_note") or r.get("hint") or ""
        lines.append(f"- 优先级 {r['conflict_priority']}：{r['rule_id']}"
                     + (f"（{tip}）" if tip else ""))
    lines.append("- 客户在具备学习或业务需求时主动问价格，按「主动问课」判断；仅在未了解价值的前提下反复只比价，才计「追着问价格」。")
    lines.append("- 「实际到课 +2」须有到课事件或到课标签佐证；仅凭聊天记录不得给出 +2，最多按「已确认参加但尚未到课 +1」。")
    return "\n".join(lines)


def examples_coverage(rules: dict) -> dict:
    """闸1/F5 的验收口径：多少条规则带了 ``examples``。

    架构 §8 的 G1 锚点要求「被丢弃证据项占比 < 15%」这类**可统计**的判据，
    示例覆盖率同理 —— 不统计就永远不知道「边界是不是真的钉住了」。
    同时统计 ``conflict_priority`` 覆盖（冲突规则可配置化程度）。
    """
    total = with_ex = with_prio = with_any_ex = 0
    missing = []
    by_slot = {"positive": 0, "negative": 0, "ambiguous": 0}
    for key in ("positive_rules", "negative_rules"):
        for r in rules.get(key, []):
            total += 1
            opts = r.get("options") or []
            covered = [o for o in opts if (o.get("examples") or {})]
            if len(covered) == len(opts) and opts:
                with_ex += 1
            else:
                missing.append(r.get("rule_id"))
            for o in opts:
                ex = o.get("examples") or {}
                if ex:
                    with_any_ex += 1
                for slot in by_slot:
                    if [x for x in (ex.get(slot) or []) if str(x).strip()]:
                        by_slot[slot] += 1
            if r.get("conflict_priority"):
                with_prio += 1
    return {"rules_total": total, "rules_with_examples": with_ex,
            "options_with_examples": with_any_ex,
            "rules_with_conflict_priority": with_prio,
            "examples_by_kind": by_slot,
            "missing_rules": missing,
            "coverage": round(with_ex / total, 4) if total else 0.0}
