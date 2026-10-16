# -*- coding: utf-8 -*-
"""模型输出的双层契约（唯一权威定义）。

分层原则：**模型只负责「判断」，代码负责「算术与落库」**。

  ① 模型层  ``JudgmentOut`` / ``RuleCheck``    —— 模型必须满足的结构（严格校验、禁止多余键）
  ② 落库层  ``AssessmentOut`` / ``ItemOut``    —— 代码补全后写入 customer_intent_assessment 的结构

为什么需要这一层
------------------------------------------------------------------
· DeepSeek 只支持 ``response_format={"type":"json_object"}``，**不支持 json_schema**
  （会返回 ``This response_format type is unavailable now``）。所以 schema 的强制力
  不可能来自 API 侧，只能来自「提示词内嵌 schema + 客户端严格校验 + 校验失败回灌修复」。
· 在此之前全项目没有任何 schema 校验，模型少给字段会静默降级成默认值
  （``analyze.py`` 里的 ``data.get(..., "")``），错了也不报错、不留痕。

改本文件 = 改契约，需同步 ``app_setting`` 里的 ``prompt_version`` / ``schema_version``。
"""
import json
import re
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

import ctx_preprocess

#: 契约版本（落库到 customer_intent_assessment.schema_version，用于归因「结论为何变了」）
SCHEMA_VERSION = "1"

#: 给模型看的“必须逐条表态”的规则上限（超过则说明规则表已膨胀到需要重构检索方式）
DEFAULT_MAX_REASON_CHARS = 200


# ===========================================================================
# ① 模型层契约
# ===========================================================================

class RuleCheck(BaseModel):
    """单条规则的表态。

    「命中才输出」改成「每条规则都要表态」是本次最关键的一处改动：
    从此"模型压根没看这条规则"与"这条规则确实没命中"不再长得一样 —— 漏判变得可计数。
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1)
    hit: bool
    # hit=false 时必须为空；hit=true 时必须给出档位分值
    score: Optional[int] = None
    evidence_ids: List[str] = Field(default_factory=list)
    reason: str = Field(default="", max_length=DEFAULT_MAX_REASON_CHARS * 4)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _normalize_ids(cls, v: Any) -> List[str]:
        """把 "#12" / "12" / 12 统一成 "#012"；完全无法识别的原样保留（后续进 invalid）。

        归一化放在校验里而不是报错，是为了少触发无意义的修复重试 —— 模型把编号写成
        ``#12`` 是格式问题，不是判断错误。
        """
        if v is None:
            return []
        if isinstance(v, (str, int)):
            v = [v]
        out: List[str] = []
        for raw in v:
            code = ctx_preprocess.normalize_code(raw)
            out.append(code if code else str(raw).strip())
        return out

    @field_validator("evidence_ids")
    @classmethod
    def _check_ids(cls, v: List[str]) -> List[str]:
        bad = [x for x in v if not re.fullmatch(r"#\d{3,}", x or "")]
        if bad:
            raise ValueError(f"evidence_ids 格式非法（应形如 #012）: {bad[:5]}")
        return v

    @model_validator(mode="after")
    def _hit_score_consistent(self) -> "RuleCheck":
        if self.hit and self.score is None:
            raise ValueError(f"{self.rule_id}: hit=true 时必须给出 score")
        if (not self.hit) and self.score is not None:
            raise ValueError(f"{self.rule_id}: hit=false 时 score 必须为 null")
        return self


class ProfileField(BaseModel):
    """客户画像的**单个结构化槽位**（架构 §2.3 E3）。

    改造前画像只有一段自由文本 `customer_profile_summary`：无法查询、无法对比变化、
    也无法回答「这个结论依据哪句话」。现在每个槽位都带 ``evidence_ids``。

    槽位沿用记忆方案 M-B（固定 8 个，缺值写「未提及」而**不是**空串）——
    固定槽位让「上次说有预算、这次没提」这种**信息消失**变得可检测（详见 §4.5.1 ①）。
    """

    model_config = ConfigDict(extra="forbid")

    #: 槽位名，必须是 PROFILE_SLOTS 之一
    slot: str = Field(min_length=1)
    #: 槽位取值（客户原话归纳；未提及写「未提及」）
    value: str = ""
    #: 依据的聊天记录编号，如 ["#012"]（与命中项同一套回链机制）
    evidence_ids: List[str] = Field(default_factory=list)


#: E3 画像固定槽位（记忆方案 M-B）：不随模型自由发挥增减，便于跨次对比
PROFILE_SLOTS = ("position", "company", "city", "need", "budget",
                 "timeline", "concerns", "competitor")

#: 槽位的中文名（提示词与前端展示共用）
PROFILE_SLOT_CN = {
    "position": "岗位", "company": "公司", "city": "城市/地域", "need": "需求",
    "budget": "预算", "timeline": "时间要求", "concerns": "顾虑", "competitor": "竞品/对比",
}


class JudgmentOut(BaseModel):
    """**模型层契约** —— 模型必须原样满足这个结构。

    只有这 4 个字段由模型提供；总分/等级/证据文本一律由代码计算或回填。
    """

    model_config = ConfigDict(extra="forbid")

    rule_checks: List[RuleCheck] = Field(min_length=1)
    customer_profile_summary: str = ""
    sales_strategy: str = ""
    #: E3（架构 §2.3）结构化画像；**可为空列表**（老提示词/模型不给时不报错，
    #: 避免为了一个可选字段触发修复重试）
    customer_profile: List[ProfileField] = Field(default_factory=list)
    # 模型自报置信度：仅留档，**不参与任何决策**（决策用覆盖率与规则命中）
    model_confidence: float = 0.5
    #: 该字段被**夹取过**（越界/非法值）—— 观测用。
    #: 用 `SkipJsonSchema` **刻意不进 JSON schema**：模型看不到它（提示词里的 schema
    #: 一个字都不变），但代码能读到"这次模型又输出了脏值"。
    confidence_clamped: SkipJsonSchema[bool] = False

    @field_validator("customer_profile_summary", "sales_strategy", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return "" if v is None else v

    @model_validator(mode="before")
    @classmethod
    def _clamp_confidence(cls, data: Any) -> Any:
        """越界/非法**不报错**（这不参与决策，不值得为它触发一次修复重试），
        但**必须留痕**（2026-10-16）。

        此前这里是纯静默夹取：非法值直接变成 0.5 —— 于是「模型自报置信度」这一列的
        统计口径会被悄悄拉偏，而且没有任何地方能看到"模型又输出了脏值"。
        现在把事实记在 `confidence_clamped`（不进 schema，模型看不到），
        由 `analyze` 带进结果、批量评分累加进进度。
        """
        if not isinstance(data, dict) or "model_confidence" not in data:
            return data
        raw = data["model_confidence"]
        try:
            f = float(raw)
        except (TypeError, ValueError):
            return {**data, "model_confidence": 0.5, "confidence_clamped": True}
        if f < 0.0 or f > 1.0:
            return {**data, "model_confidence": min(1.0, max(0.0, f)),
                    "confidence_clamped": True}
        return data

    def checked_rule_ids(self) -> List[str]:
        """模型表态过的 rule_id（含重复，去重在调用方做）。"""
        return [rc.rule_id for rc in self.rule_checks]


# ===========================================================================
# ② 落库层契约
# ===========================================================================

class ItemOut(BaseModel):
    """一条命中项。字段形状与前端 / 复核页现有渲染保持一致。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    dimension: str
    polarity: Literal["positive", "negative"]
    score: int
    reason: str = ""
    #: 证据原文行（本地回填，**绝不上报第三方**）
    evidence: List[str] = Field(default_factory=list)
    #: 模型给出的编号引用，形如 ["#012"]
    evidence_ids: List[str] = Field(default_factory=list)
    #: 编号回链后的 message_id 列表（chat_message.message_id 是 TEXT）
    evidence_message_ids: List[str] = Field(default_factory=list)
    #: 部分编号回链不上时的留痕（对应不上就丢掉，但不装作没发生过）
    invalid_evidence_ids: List[str] = Field(default_factory=list)
    #: A1（架构 §7.2）逐项证据回链结果：1=全部成功 / 2=部分成功 / 0=未校验
    evidence_verified: int = 0


class AssessmentOut(BaseModel):
    """**落库层契约** —— 代码补全后的完整评估结果。

    字段名与 ``customer_intent_assessment`` 的列名保持一致，改名只发生一次
    （``customer_profile_summary`` → ``profile_summary``），不下沉到 DB 或前端。
    """

    model_config = ConfigDict(extra="forbid")

    # ---- 代码计算（绝不取模型返回值）----
    raw_score: int = 0
    positive_total: int = 0
    negative_total: int = 0
    grade: Literal["S", "A", "B", "C", "D"] = "D"
    priority: str = ""
    advice: str = ""

    # ---- 由 rule_checks 派生 ----
    positive_items: List[ItemOut] = Field(default_factory=list)
    negative_items: List[ItemOut] = Field(default_factory=list)
    invalid_items: List[dict] = Field(default_factory=list)
    #: 模型没表态的规则（覆盖率分母 = engine != rule 的规则数）
    unchecked_rules: List[str] = Field(default_factory=list)

    # ---- 模型透传（仅 3 项）----
    profile_summary: str = ""
    sales_strategy: str = ""
    model_confidence: float = 0.0
    #: 模型自报置信度**被夹取过**（越界/非法值）—— 纯观测标记：
    #: 不参与任何决策，但让"模型又输出了脏值"这件事可计数、可告警。
    confidence_clamped: bool = False
    #: E3（架构 §2.3）：结构化画像（8 槽位 + 逐槽位证据编号）。
    #: `profile_summary` 降级为「自由文本补充」，结构化字段才是可查询、可对比的那一份。
    profile_struct: List[dict] = Field(default_factory=list)

    # ---- 代码判定 ----
    #: 负分项存在、或存在漏检规则时置 true（收回了模型的自证权）
    needs_human_review: bool = False

    # ---- 元数据 ----
    rule_version: str = ""
    prompt_version: str = ""
    schema_version: str = SCHEMA_VERSION
    #: B1（架构 §7.2）：实际返回的模型名（取响应里的 ``model``，非配置值）
    model_name: str = ""
    #: 闸2（架构 §7.2）：模型的**逐维度原始表态**落库（此前只在契约内校验、不落库）。
    #: 有了它可回答「模型对哪条规则表了什么态、给的分值是多少」，
    #: 而 `unchecked_rules` 只能回答「漏了哪些」。元素形如
    #: ``{"rule_id": str, "hit": bool, "score": int|None, "evidence_ids": [...], "reason": str}``
    rule_checks: List[dict] = Field(default_factory=list)
    current_date: str = ""
    analyzed_at: int = 0
    #: 契约实际消耗的 LLM 调用次数：1 = 一次通过，>1 = 触发过修复重试
    contract_attempts: int = 1

    def coverage(self) -> float:
        """逐维度表态的覆盖率（1.0 = 每条 llm 规则都表态了）。"""
        total = len(self.unchecked_rules) + len({i.rule_id for i in self.positive_items}
                                                 | {i.rule_id for i in self.negative_items})
        if total <= 0:
            return 1.0
        return round(1.0 - len(self.unchecked_rules) / total, 4)


class ContractViolationError(Exception):
    """模型输出连续多次都不满足契约。调用方应落 ``session_failure`` 并转人工，而不是继续重试。"""

    def __init__(self, message: str, raw: str = "", errors_text: str = "", attempts: int = 1):
        super().__init__(message)
        self.raw = raw
        self.errors_text = errors_text
        self.attempts = attempts


# ===========================================================================
# 工具：把 schema 注入提示词（替代手写 JSON 示例）
# ===========================================================================

def prompt_schema(model: type = JudgmentOut, strip_description: bool = True) -> str:
    """把 pydantic 模型压成「给模型看的 JSON Schema」。

    展开 ``$defs`` 引用、去掉 ``title``、把 ``anyOf: [T, null]`` 折成 ``type: [T, "null"]``，
    让模型能一遍读懂，不必理解 JSON Schema 的组合语义。

    ``strip_description=True``（默认）会去掉 ``description`` —— 本项目模型的 docstring 是
    写给开发者的代码注释（如「这次改动的动机」），灌进提示词只会让模型困惑。
    要写「给模型看的字段说明」请放到 ``build_user_prompt`` 里，不要写进 docstring。
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node

        if "$ref" in node and len(node) == 1:
            name = str(node["$ref"]).rsplit("/", 1)[-1]
            return walk(defs.get(name, {}))

        out = {}
        for key, val in node.items():
            if key == "title" or (strip_description and key == "description"):
                continue
            if key == "anyOf":
                variants = [walk(x) for x in (val or [])]
                null = [v for v in variants if v.get("type") == "null"]
                rest = [v for v in variants if v.get("type") != "null"]
                if null and len(rest) == 1:            # Optional[T] → type: [T, "null"]
                    base = dict(rest[0])
                    t = base.get("type")
                    base["type"] = [t, "null"] if t else "null"
                    out.update(base)
                    continue
                out["anyOf"] = variants
                continue
            out[key] = walk(val)
        return out

    return json.dumps(walk(schema), ensure_ascii=False, indent=2)


def render_validation_errors(err: Exception, limit: int = 8) -> str:
    """把 ValidationError 压成给模型看的可读清单（用于回灌修复提示）。"""
    errors = getattr(err, "errors", None)
    if not callable(errors):
        return str(err)[:800]
    lines = []
    for item in errors()[:limit]:
        loc = ".".join(str(x) for x in item.get("loc", ())) or "(根)"
        lines.append(f"- {loc}: {item.get('msg')} [{item.get('type')}]")
    if not lines:
        return str(err)[:800]
    return "\n".join(lines)
