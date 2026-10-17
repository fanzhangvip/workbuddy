# -*- coding: utf-8 -*-
"""兴趣点提取：**受控一级分类 + 自由二级标签**（词表 / 提示词 / 契约 / 归一 / 证据回链）。

## 技术路线（为什么不是 embedding / 聚类 / 微调）

本仓库**没有任何向量基础设施**（无 embedding 服务、无 sklearn/torch），而需求是
"从聊天记录里找出用户关心什么、形成可对比的分布"。三种常见做法的代价：

· **向量 + 聚类**（BERTopic 那套）：要新引依赖 + 外部 embedding 服务；且几百条会话的
  簇数极不稳定、跨月不可比 —— 而"可对比的分布"正是本功能的目的。
· **微调分类器**：没有标注数据，且需求是"发现新兴趣"而不是"把已知类别分类"。
· **关键词/正则**：漏判严重（"想转行"有几百种说法），也不懂否定（"我不打算考证"）。

所以走**大模型受控分类**这条路：一级分类由 `interests.json` 受控（模型选别的会被契约
拒绝并触发修复重试），二级标签自由生成用于出选题；证据必须回链到给定行号，
回链不上的**丢弃并计数**。这套机制与评分判定**完全同源**（复用
`contracts.prompt_schema` 注入 schema、`analyze.judge_with_repair` 回灌修复、
`ctx_preprocess.resolve_evidence` 证据回链）—— 不新造第二套。

⚠️ 一个硬约束（`contracts.py` 开头也记着）：DeepSeek 只支持
`response_format={"type":"json_object"}`，**不支持 API 侧的 json_schema 强制**。
所以结构化的强制力只能来自「提示词内嵌 schema + 客户端严格校验 + 失败回灌重来」。

改本文件（提示词）或 `interests.json`（词表）= 改契约，需重启后端；
两者各有版本号并随结果落库，便于回答"这张图是哪版提示词/词表跑出来的"。
"""
import json
import re
from pathlib import Path
from typing import Any, List

from pydantic import BaseModel, ConfigDict, Field, model_validator

import contracts as contracts_mod
import ctx_preprocess

ROOT = Path(__file__).resolve().parent.parent
TAXONOMY_PATH = ROOT / "interests.json"

#: 提示词版本（与词表版本分开：提示词改了要能识别出来）
PROMPT_VERSION = "interest-v1"

#: 二级标签的长度上限（字符）。超长会被截断 —— 它要进图表，不能是一句话。
SECONDARY_MAX_CHARS = 12

#: 一次调用最多输出多少个兴趣点（防止模型把整段对话抄成标签）
MAX_INTERESTS = 12

_CACHE: dict = {"mtime": None, "taxonomy": None}

#: 全角 → 半角（数字/字母/常见符号），让 "Ｐｙｔｈｏｎ" 与 "Python" 归到一起。
#: ⚠️ 键必须是**整数码点**：`str.translate` 只认 int 键，写成 `{"Ａ": "A"}` 会**静默不生效**
#: （实测踩到：转换看起来"写了"，实际一个字符都没换，同义标签因此归不到一起）。
_FULLWIDTH = {0xFF01 + i: 0x21 + i for i in range(0x5E)}

#: 归一前剥掉的成对包裹符号（模型常把标签包起来：`「转行」`、`"Python"`、`《课程》`）
_WRAPPERS = "\"'“”‘’「」『』《》<>()（）[]【】"


def load_taxonomy(path: Path = TAXONOMY_PATH) -> dict:
    """读词表（带 mtime 缓存：文件没变就不重复解析）。"""
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {"taxonomy_version": "", "primaries": [], "aliases": {}}
    if _CACHE["mtime"] == mtime and _CACHE["taxonomy"] is not None:
        return _CACHE["taxonomy"]
    data = json.loads(p.read_text(encoding="utf-8"))
    _CACHE.update({"mtime": mtime, "taxonomy": data})
    return data


def validate_taxonomy(t: dict) -> list:
    """加载期校验（**静态数据同样是代码**，一个 key 写错单测很难发现，必须在这里拦）。

    返回错误清单（空 = 通过）。规则：
      · `taxonomy_version` 非空；`primaries` 至少 3 类；
      · 每类有非空 `key`（形如 `job_title`）、非空 `name`、`desc`；
      · `key` 不重复、必须是 `^[a-z][a-z0-9_]*$`（它会进提示词与枚举，不能带怪字符）；
      · 至少有一个 `other` 兜底类（否则模型遇到"说不清"的兴趣只能硬塞或漏掉）；
      · `aliases` 的值必须也是词表里认识的 key（写错等于静默不生效）。
    """
    errs = []
    if not str(t.get("taxonomy_version") or "").strip():
        errs.append("taxonomy_version 不能为空")
    prim = t.get("primaries")
    if not isinstance(prim, list):
        return errs + ["primaries 必须是数组"]
    if len(prim) < 3:
        errs.append(f"primaries 至少 3 类（当前 {len(prim)}）")
    keys, names = [], []
    for i, p in enumerate(prim):
        k = str((p or {}).get("key") or "").strip()
        n = str((p or {}).get("name") or "").strip()
        if not k:
            errs.append(f"第 {i + 1} 类缺 key")
        elif not re.match(r"^[a-z][a-z0-9_]*$", k):
            errs.append(f"key 只能是小写字母/数字/下划线且以字母开头：{k!r}")
        elif k in keys:
            errs.append(f"key 重复：{k}")
        if not n:
            errs.append(f"{k or i + 1} 缺 name")
        elif n in names:
            errs.append(f"name 重复：{n}")
        keys.append(k)
        names.append(n)
    if "other" not in keys:
        errs.append("必须有一个 key=other 的兜底类（说不清的兴趣点归它）")
    aliases = t.get("aliases") or {}
    if not isinstance(aliases, dict):
        errs.append("aliases 必须是对象（二级标签 → 标准写法）")
    return errs


def primary_keys(t: dict = None) -> list:
    t = t or load_taxonomy()
    return [str(p.get("key") or "") for p in (t.get("primaries") or [])]


def primary_names(t: dict = None) -> dict:
    t = t or load_taxonomy()
    return {str(p.get("key") or ""): str(p.get("name") or "") for p in (t.get("primaries") or [])}


# ---------------------------------------------------------------- 二级标签归一

def normalize_secondary(label: Any, taxonomy: dict = None) -> str:
    """把模型的自由二级标签**确定性**归一（不用 embedding，也不做语义合并）。

    归一动作（都对"同一件事的不同写法"有效）：
      ① 全角转半角、去首尾空白、压缩内部空白；
      ② 去掉包裹的引号/书名号与**尾部**标点（"学 Python。" → "学 Python"）；
      ③ 截断到 `SECONDARY_MAX_CHARS`；
      ④ 查 `aliases`（人工维护的同义合并表）→ 命中就换成标准写法。

    ④ 是**人工**的：语义合并靠 embedding 才能自动做，而本仓库没有向量基础设施 ——
    与其引入一套不可解释的相似度阈值，不如把"合并哪两个"写成配置，看得见、可回滚。
    """
    s = "" if label is None else str(label)
    s = s.translate(_FULLWIDTH)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(_WRAPPERS)
    s = re.sub(r"[。，,、；;！!？?.．：:·…~—-]+$", "", s).strip()
    if len(s) > SECONDARY_MAX_CHARS:
        s = s[:SECONDARY_MAX_CHARS]
    t = taxonomy if taxonomy is not None else load_taxonomy()
    aliases = {str(k): str(v) for k, v in ((t or {}).get("aliases") or {}).items()}
    return aliases.get(s, s)


# ---------------------------------------------------------------- 契约

class InterestItem(BaseModel):
    """一个兴趣点。**一级受控、二级自由**。"""

    model_config = ConfigDict(extra="forbid")

    primary: str = Field(description="必须从系统给出的一级分类 key 里选一个")
    secondary: str = Field(description="自由填写的具体关注点，≤12 字，不要写整句话")
    confidence: float = Field(default=0.5, description="0~1，模型自评这条兴趣点的把握")
    evidence_ids: List[str] = Field(default_factory=list,
                                   description="支撑这条兴趣点的对话行号，形如 [\"#012\"]")

    @model_validator(mode="after")
    def _check_primary(self):
        """一级必须是受控 key —— **在这里拒绝**才能触发"校验失败回灌重来"。

        为什么不在提示词里"叮嘱"：那样模型写了别的值会一路流进库里，图上就会多出
        一个从没定义过的分类，而且没人知道。校验层拒绝 + 回灌修复才是硬约束。
        """
        keys = primary_keys()
        if self.primary not in keys:
            raise ValueError(f"primary={self.primary!r} 不在受控一级分类里，"
                             f"只能选：{'/'.join(keys)}")
        return self


class InterestOut(BaseModel):
    """兴趣点提取的输出契约。"""

    model_config = ConfigDict(extra="forbid")

    interests: List[InterestItem] = Field(default_factory=list,
                                          description=f"最多 {MAX_INTERESTS} 条；没有则给空数组")
    summary: str = Field(default="", description="一句话概括这位客户关心什么（≤60 字）")

    @model_validator(mode="after")
    def _cap(self):
        if len(self.interests) > MAX_INTERESTS:
            self.interests = self.interests[:MAX_INTERESTS]
        return self


def parse_interests(raw: str) -> tuple:
    """把原始响应解析成 ``(InterestOut, None)`` 或 ``(None, ValidationError)``。

    容错与评分链路同款：模型偶尔会包一层解释文字或 markdown 代码块，
    截取第一个 ``{`` 到最后一个 ``}`` 再试一次。
    """
    from pydantic import ValidationError
    try:
        return InterestOut.model_validate_json(raw), None
    except ValidationError as err:
        s, e = (raw or "").find("{"), (raw or "").rfind("}")
        if s >= 0 and e > s:
            try:
                return InterestOut.model_validate_json(raw[s:e + 1]), None
            except ValidationError as err2:
                return None, err2
        return None, err


# ---------------------------------------------------------------- 提示词

def build_system_prompt(taxonomy: dict = None) -> str:
    """系统提示词：**词表 + schema + 纪律**三件套。

    schema 由 `contracts.prompt_schema` 从契约模型注入（与评分链路同一实现），
    所以"模型看到的字段"和"客户端校验的字段"物理上不可能漂移。
    """
    t = taxonomy or load_taxonomy()
    lines = [
        "你是课程销售团队的选题分析师。任务：从下面这位**客户自己说过的话**里，"
        "提炼出他/她**真正关心什么**，供内容团队做自媒体选题。",
        "",
        "⚠️ 给你的段落里**只有客户本人的发言**（销售/老师的发言已被剔除）。"
        "所以：不要替客户补全没说过的话，也不要把课程卖点当成客户的兴趣 —— "
        "每一条都必须能指回客户自己的某一句原话。",
        "",
        "## 一级分类（受控）",
        "`primary` 只能从下面这些 key 里选一个，**不许自创**：",
    ]
    for p in (t.get("primaries") or []):
        ex = "、".join(p.get("examples") or []) or "—"
        lines.append(f"- `{p.get('key')}` {p.get('name')}：{p.get('desc')}（例如：{ex}）")
    lines += [
        "",
        "## 二级标签（自由）",
        f"- 写**具体的关注点**，≤{SECONDARY_MAX_CHARS} 个字，不要写整句、不要带引号；",
        "- 用客户自己的说法（如「想转行做算法」「想知道学多久」），这是内容团队的选题来源；",
        "- **不要**出现人名、手机号、微信号、公司名等可识别信息；",
        "",
        "## 硬性纪律",
        f"- 最多 {MAX_INTERESTS} 条；同一件事只写一条（不要换个说法重复）；",
        "- 每条都要给 `evidence_ids`：**从给定对话里抄行号**（形如 `#012`），"
        "必须是真实存在、且确实支撑这条兴趣点的行；**不许编**；",
        "- 客户没提到的兴趣**不要猜**（宁缺勿滥）；找不到就少给几条；",
        "- `confidence` 是你对这条兴趣点的把握（0~1），不是客户意向强度；",
        "- `summary` 一句话概括（≤60 字），只描述「关心什么」，不要写销售建议。",
        "",
        "## 输出格式",
        "只输出一个 json 对象（不要 markdown 代码块、不要解释文字），字段如下：",
        contracts_mod.prompt_schema(InterestOut),
    ]
    return "\n".join(lines)


def build_user_prompt(conversation: str) -> str:
    """用户提示词：把带编号的**客户发言**文本放进来。

    对话文本由 `ctx_preprocess` 渲染（唯一实现），行号就是 `evidence_ids` 的引用键。
    文本内容由 `build_context(customer_only=True)` 过滤 —— 这里只负责渲染措辞。
    """
    return ("下面是这位客户说过的话（每行以 `#编号` 开头，引用时抄这个编号；"
            "行首的角色一律是「客户」——销售额外的上下文不在其中）：\n\n"
            "----- 客户发言开始 -----\n"
            f"{conversation}\n"
            "----- 客户发言结束 -----\n\n"
            "请按系统提示的 schema 输出这位客户的兴趣点 json。")


# ---------------------------------------------------------------- 回链与落库前归一

def to_rows(interests: list, taxonomy: dict, index: dict, labels: dict) -> dict:
    """把契约里的兴趣点列表转成**可落库的行**：回链证据 + 归一二级标签 + 丢弃无证据项。

    返回 ``{"items": [...], "dropped": n}``：
      · 证据全部回链不上的兴趣点 → **丢弃并计数**（`dropped`）。宁可少一条，
        也不能让图上出现一条点开看不到原话的"兴趣"——那等于不可核对。
      · `index`/`labels` 来自 `build_context`（编号 → message_id / 编号 → 原文行）。
    """
    items, dropped = [], 0
    for it in (interests or []):
        res = ctx_preprocess.resolve_evidence(it.evidence_ids or [], index or {})
        mids = res.get("message_ids") or []
        if not mids:
            dropped += 1
            continue
        sec = normalize_secondary(it.secondary, taxonomy)
        if not sec:
            dropped += 1
            continue
        items.append({
            "primary": it.primary,
            "secondary": sec,
            "confidence": float(it.confidence or 0),
            "evidence_message_ids": mids,
            # 原文片段：界面点开就能核对（仅内部选题用）
            "evidence": [(labels or {}).get(c) for c in (res.get("codes") or [])
                         if (labels or {}).get(c)],
            "invalid_evidence_ids": res.get("invalid") or [],
        })
    return {"items": items, "dropped": dropped}


# ---------------------------------------------------------------- 一次提取（模型调用）

def extract(conversation: str, *, temperature: float = None, max_tokens: int = None,
            max_repairs: int = None, timeout: int = None, on_attempt=None) -> dict:
    """调模型提取兴趣点 —— **复用评分链路的调用与修复机制**（不另造一套）。

    复用点（`analyze` / `llm` 里已有的）：
      · `analyze.judge_with_repair`：契约校验失败 → 把错误回灌重来；
      · `llm.chat` 的 `json_mode` / 超时 / `Retry-After` / 截断自救（升 tokens → 续写）；
      · 异常分类 `RetryableLLMError`（上层退避重试）/ `FatalLLMError`（直接上抛转人工）。

    ⚠️ 失败**不在这里吞掉**：契约连续不满足会抛 `ContractViolationError`，
    由调用方（`interest_service`）落失败明细 —— 静默返回空兴趣点等于"这条会话没兴趣"，
    而那是两件完全不同的事。

    返回 ``{"interests": [InterestItem], "summary": str, "llm_calls": int, "model_name": str}``。
    """
    import analyze

    messages = [{"role": "system", "content": build_system_prompt()},
                {"role": "user", "content": build_user_prompt(conversation)}]
    out, calls, meta = analyze.judge_with_repair(
        messages, temperature=temperature, max_tokens=max_tokens, max_repairs=max_repairs,
        on_attempt=on_attempt, timeout=timeout, model_cls=InterestOut)
    return {"interests": out.interests, "summary": out.summary, "llm_calls": calls,
            "model_name": ((meta or {}).get("model") or "")}
