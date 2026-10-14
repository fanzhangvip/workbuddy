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


class JudgmentOut(BaseModel):
    """**模型层契约** —— 模型必须原样满足这个结构。

    只有这 4 个字段由模型提供；总分/等级/证据文本一律由代码计算或回填。
    """

    model_config = ConfigDict(extra="forbid")

    rule_checks: List[RuleCheck] = Field(min_length=1)
    customer_profile_summary: str = ""
    sales_strategy: str = ""
    # 模型自报置信度：仅留档，**不参与任何决策**（决策用覆盖率与规则命中）
    model_confidence: float = 0.5

    @field_validator("customer_profile_summary", "sales_strategy", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return "" if v is None else v

    @field_validator("model_confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v: Any) -> float:
        """越界不报错，直接夹到 [0,1] —— 这个字段不参与决策，不值得为它触发一次重试。"""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.5
        return min(1.0, max(0.0, f))

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

    # ---- 代码判定 ----
    #: 负分项存在、或存在漏检规则时置 true（收回了模型的自证权）
    needs_human_review: bool = False

    # ---- 元数据 ----
    rule_version: str = ""
    prompt_version: str = ""
    schema_version: str = SCHEMA_VERSION
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


def normalize_evidence_id(raw: Any) -> Optional[str]:
    """把模型可能写出的 "#12" / "12" / "#012" 统一成 "#012"（与预处理层同一实现）。"""
    return ctx_preprocess.normalize_code(raw)


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
