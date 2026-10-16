# -*- coding: utf-8 -*-
"""置信度三层合成（架构 第六章 E4）。

铁律：**置信度必须「算出来」，不能「问出来」。**
    改造前 `analyze.analyze()` 把模型自报的 `confidence` 直接落库并展示 —— 等于
    **让可能出错的组件自己申报可信度**。本模块把它收回，改为由代码合成：

    ① 命中项级 `item_confidence`  —— 证据有多硬
    ② 会话级   `session_confidence` —— 整体可信度
    ③ 复核触发 `review_reasons`    —— 要不要人看一眼（**不再听模型**）

三层都刻意**只依赖代码可见的事实**（证据回链结果、覆盖率、无效项、与上次的一致性），
**不读模型的任何自述** —— 这是它相对"问模型"唯一的价值所在。
"""
import re

import db                         # 阈值读运行时设置（app_setting > .env > 常量默认）
import rules as rules_mod       # 同 `_index` / `llm_rule_ids` 口径，避免第二份实现

# ============================================================ 常量（口径集中，便于调参）

#: 证据质量分档（架构 §6.2 的"按证据回链结果分档"）
#:   engine    → 代码硬算，无判断误差
#:   exact     → 证据里能找到规则关键词（"逐字命中"）
#:   multi     → 无字面依据，但有 ≥2 条原文证据（多项互证）
#:   single    → 无字面依据，仅 1 条原文证据（文档的"模糊命中"基线）
#:   no_text   → 回链上了但没带出原文行
#:   none      → 没有任何证据（正常已在 `_attach_evidence` 阶段被丢弃）
#:
#: ⚠️ **`multi` 是本实现的细化**：文档只写了「逐字 1.0 / 模糊 0.7 / 无证据 0」三档，
#: 但实测（25 条真实会话、129 个命中项）字面命中只占 **5%** —— 中文近义改写太普遍
#: （规则写"暂不考虑"，客户说"暂时不考虑"；规则写"不报"，客户说"还不想买课"）。
#: 若只用三档，`item_confidence` 会退化成常数、失去区分度。
#: 故按文档"**按证据回链结果分档**"的同一原则，在"模糊"一档内按**证据条数**再分
#: （多条独立证据互相印证，可信度确实更高）。这是**可观测、可回滚**的口径：
#: 分布会随 `ctx_seg_*` 一起落 `scoring_run.detail`，若发现不合理可调下面的常量。
CONF_EXACT = 1.0      # 逐字命中
CONF_MULTI = 0.8      # 多条证据互证
CONF_FUZZY = 0.7      # 单条证据的模糊命中（文档基线）
CONF_NO_TEXT = 0.5    # 回链上了但没带出原文行
CONF_ENGINE = 1.0     # engine=rule
CONF_NO_ITEM = 0.5    # 没有任何**语义**命中项时的中性值（"没命中"不等于"判错了"）
ENGINE_BONUS = 0.05   # 另有 engine 硬指标佐证时的小幅确定加成（不参与均值，只做微调）

#: 会话级合成权重（架构 §6.2 只给了结构，系数由实现确定并在此集中声明）
PENALTY_INVALID = 0.5     # 无效项比例惩罚系数
BONUS_CONSISTENT = 0.10   # 与上次等级一致 → 加成
PENALTY_FLIP = 0.10       # 与上次等级明显不一致 → 减成

#: 复核触发阈值
REVIEW_CONF_MIN = 0.5         # 低于它 → low_confidence
#: 距等级门槛 ±N 分 → grade_boundary。
#: ⚠️ 实测：`≤1` 会命中 **90%** —— 等级门槛密集在 0/2/4/6，±1 覆盖了 {−1,0,1,2,3,4,5,6}
#: 共 8 个整数值，而真实分数恰好集中在这一带 → 等于"全量触发"。
#: 故默认取 **0 = 恰好卡在门槛上**（"差 1 分就换级"，这才是真正敏感的位置）。
GRADE_BOUNDARY_DELTA = 0
GRADE_FLIP_STEPS = 2          # 等级跨 N 档以上 → grade_flip
WEAK_EVIDENCE_WEIGHT = 2      # |score| ≥ N 的高权重项，若证据不硬 → weak_evidence
#: 「仅引擎硬指标负分」是否单独构成复核理由。**默认 true = 保持改造前的行为**（不放松）。
#:
#: ⚠️ 为什么要有这个开关：实测（51 条真实评估）`negative_no_reply_2d`（客户连续 48h 未回复，
#: 由**引擎硬算**，非模型判断）命中率 **90%**，导致 71% 的复核是"仅因客户很久没回"，
#: `needs_human_review` 高达 **92%** —— **复核队列等于全量，失去分流意义**。
#: 而"客户沉默"在销售场景是常态（历史会话更是几乎恒真），复核员对此也做不了什么。
#: 但**收窄触发条件是业务决策，不能由实现擅自放松**，故默认保持原行为，只把原因**细分**出来
#: （落 `review_reasons`），业务若觉得队列被淹没，把 `review_engine_negative` 设 false 即可降噪。
REVIEW_ENGINE_NEGATIVE = True

#: 关键词提取时丢弃的泛词（它们在任何销售对话里都出现，没有区分度）
_STOPWORDS = {
    "客户", "销售", "老师", "消息", "内容", "聊天", "记录", "情况", "进行", "表示",
    "明确", "相关", "可能", "可以", "以及", "或者", "并且", "但是", "是否", "如果",
    "已", "的", "等", "及", "或", "和", "与", "在", "有", "无", "不", "为", "是",
    "AI", "等相", "上述", "以下", "其他", "一定", "需要", "提供", "给出",
}

#: 功能字：含这些字的 3 字子串不做容忍匹配（否则"的/了/是"会带来大量误命中）
_FUNC = set("的了是在有和与或及等就都还很更最把被让给对向从到也又并")

_SPLIT = re.compile(r"[、，,。；;：:（）()\[\]【】\s/｜|]")
#: 中文/英文/数字词组（长度 ≥2 才可能是有区分度的关键词）
_WORD = re.compile(r"[A-Za-z]{2,}|[\u4e00-\u9fa5]{2,}|\d{2,}")
#: 证据行的前缀 `#004 [06-10 15:52｜126天前] 客户: ` —— 匹配前必须剥掉，
#: 否则「客户 / 时间」这些前缀噪声会干扰判断（还可能让泛词误命中）
_LINE_PREFIX = re.compile(r"^#\d+\s*\[[^\]]*\]\s*[^:：]{0,12}[:：]\s*")

#: 关键词长度上限：超过它的片段其实是"整句描述"而非"词"，会带来噪声匹配
_KW_MAX_LEN = 10


def _keywords(rule: dict) -> set:
    """从规则的 `dimension` + 各档 `criteria` + `hint` 提取关键词。

    为什么不新增 `keywords` 字段：**要业务去为 14 条规则手工定关键词，成本高且易漏**。
    规则里已有的这三段文本本来就是"什么算命中"的自然语言描述（如
    `negative_refuse` 的 `criteria = "明确表示不报、不学、暂不考虑、没有需求等"`），
    从中切词是**零额外维护**的做法。

    做法：按常见标点切分，再做词组切分；丢弃泛词、长度 <2 或 >``_KW_MAX_LEN`` 的碎片。
    """
    text = " ".join([rule.get("dimension") or ""]
                    + [o.get("criteria") or "" for o in (rule.get("options") or [])]
                    + [rule.get("hint") or ""])
    out = set()
    for seg in _SPLIT.split(text):
        seg = seg.strip()
        # 整个片段作候选（"在职编程技术岗" 这种完整短语最有区分度），但别太长
        if 2 <= len(seg) <= _KW_MAX_LEN and seg not in _STOPWORDS:
            out.add(seg)
        # 再切出其中的词组（处理 "非编程岗但明确转型 AI/大模型" 这类长句）
        for w in _WORD.findall(seg):
            if 2 <= len(w) <= _KW_MAX_LEN and w not in _STOPWORDS:
                out.add(w)
    return out


def _strip_prefix(line: str) -> str:
    """剥掉证据行的 `#编号 [时间] 角色: ` 前缀，只留正文（大小写不敏感比较用）。"""
    return _LINE_PREFIX.sub("", (line or "").strip())


def _evidence_text(item: dict) -> str:
    """把一项的 `evidence`（预处理层回填的**原文行**）拼成小写文本供匹配。"""
    ev = item.get("evidence")
    parts = [str(x) for x in ev] if isinstance(ev, list) else ([str(ev)] if ev else [])
    return " ".join(_strip_prefix(p) for p in parts).lower()


def _match_kw(kw: str, text: str) -> bool:
    """关键词命中判定（`text` 已是小写）。

    两级：
    1. **整词包含**（大小写不敏感）—— 严格意义
    2. 关键词长度 ≥4 时，允许其**长度 3 的子串**命中 —— 容忍中文近义改写。
       真实数据里这类改写很常见（规则写"暂不考虑"，客户说"暂时不考虑"；
       规则写"不报"，客户说"不想买课"）。子串含功能字（的/了/是…）则跳过，
       否则会大量误命中。

    第 2 级刻意**只对长词开放**：短词（"不学"）本身就容易与其它语境撞车，
    放宽反而失真。
    """
    k = (kw or "").lower()
    if not k:
        return False
    if k in text:
        return True
    if len(kw) >= 4:
        for i in range(len(kw) - 2):
            sub = kw[i:i + 3].lower()
            if any(ch in _FUNC for ch in sub):
                continue
            if sub in text:
                return True
    return False


def item_confidence(item: dict, rules_index: dict = None) -> tuple:
    """① 命中项级置信度。返回 `(分值, 档位名)`。

    分档依据**只有证据**（架构 §6.2），并按证据条数细化：

    ==================  =====  ==================================================
    档位                  分值   判据
    ==================  =====  ==================================================
    ``engine``           1.0    `engine=rule`：代码硬算，无判断误差
    ``exact``            1.0    证据文本里能直接找到规则关键词（"逐字命中"）
    ``multi``            0.8    无字面依据，但有 **≥2 条**原文证据（多项互证）
    ``fuzzy``            0.7    无字面依据，仅 1 条原文证据
    ``no_text``          0.5    回链上了但没带出原文行
    ``none``             0.0    没有任何证据（正常不会走到，留作兜底）
    ==================  =====  ==================================================

    注意：本函数**不读模型的 `reason`** —— 理由也是模型写的，拿它自证没有意义。
    """
    rid = item.get("rule_id") or ""
    rule = (rules_index or {}).get(rid) or {}
    if rule.get("engine") == "rule":
        return CONF_ENGINE, "engine"

    codes = item.get("evidence_ids") or []
    msg_ids = item.get("evidence_message_ids") or []
    if not (codes or msg_ids):
        return 0.0, "none"

    text = _evidence_text(item)
    if not text.strip():
        return CONF_NO_TEXT, "no_text"      # 回链上了但没带出原文行
    kws = _keywords(rule) if rule else set()
    if kws and any(_match_kw(k, text) for k in kws):
        return CONF_EXACT, "exact"
    # 无字面依据 → 按**证据条数**再分：多条独立证据互相印证，可信度更高
    n_ev = len(codes) or len(msg_ids)
    return (CONF_MULTI, "multi") if n_ev >= 2 else (CONF_FUZZY, "fuzzy")


def item_confidences(items: list, rules_index: dict = None) -> list:
    """给每个命中项附加 `item_confidence` 与 `confidence_kind`（不改原对象）。"""
    out = []
    for it in items or []:
        v, kind = item_confidence(it, rules_index)
        out.append({**it, "item_confidence": round(v, 4), "confidence_kind": kind})
    return out


# ============================================================ ② 会话级

def grade_rank(grade: str) -> int:
    """等级序号（S=5 … D=1），用于算"跨了几档"。未知返回 0。"""
    return {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1}.get((grade or "").upper(), 0)


def session_confidence(items: list, res: dict, rules: dict, last_grade: str = None,
                       current_grade: str = None) -> dict:
    """② 会话级置信度 = 命中项置信均值 × 维度覆盖率 − 无效项比例惩罚 + 与上次一致性加成。

    **为什么乘覆盖率**：14 条规则里模型只对 5 条表态（另外 9 条"漏检"），
    说明它没把整份规则过完 —— 这种"部分判断"整体可信度必须打折。
    **为什么看与上次的一致性**：同一位客户隔几天结论从 S 跳到 D，多半有问题；
    一致则小幅加成。**这是"记忆"第一次真正参与决策之外的质量评估**（决议 M-C 仍成立：
    记忆不参与计分，但可以影响"要不要人看一眼"）。
    """
    rules_index = rules_mod._index(rules)
    conf_items = item_confidences(items, rules_index)

    # --- 命中项置信均值 ---
    # ⚠️ **语义命中项与 engine 项要分开算**：`engine=rule` 的项是代码硬算的（恒 1.0），
    # 它**不能代表模型判得准**。若把 engine 项混进均值，会出现
    # 「模型一条语义规则都没命中、只有 `no_reply_2d` 命中」→ 均值 1.0 → `session_confidence` 1.0
    # 这种**过度自信**的结果（实测踩到过：一条只含 no_reply_2d 的评估算出 1.0）。
    # 所以：均值只取语义项；有 engine 项时给一个小幅确定加成；**一条语义项都没有时用中性值**。
    semantic = [i for i in conf_items if i.get("confidence_kind") != "engine"]
    if semantic:
        mean_conf = sum(i["item_confidence"] for i in semantic) / len(semantic)
        if len(semantic) < len(conf_items):          # 另有 engine 硬指标佐证
            mean_conf = min(1.0, mean_conf + ENGINE_BONUS)
    else:
        mean_conf = CONF_NO_ITEM

    # --- 维度覆盖率：模型表态了的规则占比 ---
    required = rules_mod.llm_rule_ids(rules)
    unchecked = list(res.get("unchecked_rules") or [])
    coverage = 1.0 if not required else max(0.0, 1.0 - len(unchecked) / len(required))

    # --- 无效项比例惩罚（回链失败 = 模型编了证据）---
    invalid = len(res.get("invalid_items") or [])
    denom = invalid + len(conf_items)
    invalid_ratio = (invalid / denom) if denom else 0.0

    # --- 与上次评估的一致性 ---
    bonus = 0.0
    if last_grade and current_grade:
        steps = abs(grade_rank(current_grade) - grade_rank(last_grade))
        if steps == 0:
            bonus = BONUS_CONSISTENT
        elif steps >= GRADE_FLIP_STEPS:
            bonus = -PENALTY_FLIP

    raw = mean_conf * coverage - PENALTY_INVALID * invalid_ratio + bonus
    return {
        "session_confidence": round(min(1.0, max(0.0, raw)), 4),
        "mean_item_confidence": round(mean_conf, 4),
        "coverage": round(coverage, 4),
        "invalid_ratio": round(invalid_ratio, 4),
        "consistency_bonus": round(bonus, 4),
        "items": conf_items,
    }


# ============================================================ ③ 复核触发

def review_triggers(conf: dict, score: dict, res: dict, rules: dict,
                    neg_items: list = None,
                    last_grade: str = None, current_grade: str = None,
                    thresholds: dict = None) -> list:
    """③ 是否要人工复核 —— **由规则决定，不再听模型**（架构 §6.2）。

    返回触发原因列表（空 = 不需要人工）。本函数是改造前 `neg_items or unchecked_rules`
    的**超集**（只增不减，不会放松）；唯一的例外是 `review_engine_negative=false`
    （业务显式降噪时才会去掉「仅引擎负分」这一条，见该常量的说明）。
    """
    th = thresholds or {}
    conf_min = th.get("conf_min", REVIEW_CONF_MIN)
    boundary = th.get("boundary", GRADE_BOUNDARY_DELTA)
    flip_steps = th.get("flip_steps", GRADE_FLIP_STEPS)
    weak_weight = th.get("weak_weight", WEAK_EVIDENCE_WEIGHT)
    engine_neg = th.get("engine_negative", REVIEW_ENGINE_NEGATIVE)

    reasons = []
    # --- 负分：把「模型判的语义负分」与「引擎硬指标」分开 ---
    # ⚠️ 必须用**调用方合并后的** neg_items：评分链路里 engine 硬指标（如
    # `negative_no_reply_2d`）是在 `_score_one` 里**后加**到 neg_items 的，
    # `res["negative_items"]` 里根本没有它 —— 这里若读 res 就会漏判，
    # 导致"有负分却不需要复核"（实测踩到过）。
    engine_ids = {r["rule_id"] for r in rules.get("negative_rules", [])
                  if r.get("engine") == "rule"}
    src_neg = neg_items if neg_items is not None else (res.get("negative_items") or [])
    neg_ids = {i.get("rule_id") for i in src_neg}
    if neg_ids - engine_ids:
        reasons.append("has_negative")           # 有语义负分（最该复核的一种）
    if neg_ids and neg_ids <= engine_ids and engine_neg:
        reasons.append("engine_negative_only")   # 仅引擎硬指标（默认也触发，可关）

    if res.get("unchecked_rules"):
        reasons.append("unchecked_rules")        # 有漏检

    sc = conf.get("session_confidence")
    if sc is not None and sc < conf_min:
        reasons.append("low_confidence")

    # 等级边界 ±N：正好压在门槛上，差 1 分就换级 —— 这种最该人看一眼
    raw = int(score.get("raw_score") or 0)
    for t in rules.get("grade_thresholds", []):
        m = t.get("min")
        if m is None or m <= -999:
            continue
        if abs(raw - int(m)) <= boundary:
            reasons.append("grade_boundary")
            break

    # 高权重项但证据弱：分值大（直接影响等级）却**拿不出像样的证据**
    # ⚠️ 判据只取 `fuzzy`（单条证据的模糊命中）。`multi`（多条互相印证）**不算弱** ——
    # 实测若把 multi 也算进去，该条件会命中 75%，成为"噪音触发器"。
    for it in conf.get("items") or []:
        if (abs(int(it.get("score") or 0)) >= weak_weight
                and it.get("confidence_kind") == "fuzzy"):
            reasons.append("weak_evidence")
            break

    # 与上次等级明显翻转
    if last_grade and current_grade:
        if abs(grade_rank(current_grade) - grade_rank(last_grade)) >= flip_steps:
            reasons.append("grade_flip")

    # 去重但保持稳定顺序（便于断言与前端展示）
    order = ["has_negative", "engine_negative_only", "unchecked_rules", "low_confidence",
             "grade_boundary", "weak_evidence", "grade_flip"]
    return [r for r in order if r in set(reasons)]


# ============================================================ 对外主入口

def thresholds() -> dict:
    """置信度与复核触发的运行时阈值（app_setting > .env > 代码默认）。

    全部**可配**：现在的默认值是按 52 条真实评估标定的（见本模块顶部的常量注释），
    业务若觉得复核队列量不合适，可在此调，不必改代码。

    ⚠️ 它原先住在 `scoring_service._confidence_thresholds()`，于是
    `graph_session` / `backfill` 为了读阈值都得 `import scoring_service` ——
    而阈值本来就是这个模块自己的常量。搬回来之后依赖方向才顺。
    """
    return {
        "conf_min": db.setting_float("confidence_review_min", "CONFIDENCE_REVIEW_MIN",
                                     REVIEW_CONF_MIN),
        "boundary": db.setting_int("confidence_boundary_delta", "CONFIDENCE_BOUNDARY_DELTA",
                                   GRADE_BOUNDARY_DELTA),
        "flip_steps": db.setting_int("confidence_flip_steps", "CONFIDENCE_FLIP_STEPS",
                                     GRADE_FLIP_STEPS),
        "weak_weight": db.setting_int("confidence_weak_weight", "CONFIDENCE_WEAK_WEIGHT",
                                      WEAK_EVIDENCE_WEIGHT),
        # ⚠️ 默认 true = 保持改造前行为（不放松）。实测「仅引擎负分」占 69%，
        # 关掉它可把复核率从 90% 降到 60%（见本模块 REVIEW_ENGINE_NEGATIVE 的说明）。
        "engine_negative": db.setting_bool("review_engine_negative", "REVIEW_ENGINE_NEGATIVE",
                                           REVIEW_ENGINE_NEGATIVE),
    }


def compute(pos_items: list, neg_items: list, res: dict, score: dict, grade: dict,
            rules: dict, last_grade: str = None, thresholds: dict = None) -> dict:
    """一次算完三层。评分链路只需调这一个函数。

    ⚠️ **`pos_items` / `neg_items` 必须是「最终命中项」**（即引擎硬指标已合并进来的版本），
    而不是 `res["positive_items"] / res["negative_items"]`。原因见 `review_triggers` 的注释 ——
    引擎项只存在于调用方合并后的列表里，读 `res` 会漏判，导致
    「有负分却不需要复核」并且低估 `session_confidence`。

    返回::

        {
          "session_confidence": 0.62,
          "needs_human_review": True,
          "review_reasons": ["has_negative", "low_confidence"],
          "items": [...],                       # 已附加 item_confidence（正 + 负）
          ...各分量，便于观测与调试
        }
    """
    current_grade = (grade or {}).get("grade")
    items = list(pos_items or []) + list(neg_items or [])
    conf = session_confidence(items, res, rules, last_grade=last_grade,
                              current_grade=current_grade)
    reasons = review_triggers(conf, score, res, rules, neg_items=neg_items,
                              last_grade=last_grade, current_grade=current_grade,
                              thresholds=thresholds)
    return {**conf,
            "needs_human_review": bool(reasons),
            "review_reasons": reasons}
