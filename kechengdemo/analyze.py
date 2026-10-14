# -*- coding: utf-8 -*-
"""客户意向分析（无数据库版）。

流程：取聊天记录 → 组装提示词（规则表 + 对话）→ DeepSeek 结构化输出 → 规则引擎计分 → 输出结果。

用法：
  # 用内置样例（无需微伴数据，验证规则与提示词效果）
  python analyze.py --sample high
  python analyze.py --sample low

  # 用真实微伴会话（需消息正文可获取）
  python analyze.py --staff-id <员工id> --session-id <会话id> --max-messages 100

注意：总分与等级由本地 rules.py 计算，不交由大模型，避免其凭感觉给总分。
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

import config
import contracts
import ctx_preprocess
import llm
import rules as rules_mod
import weiban

OUT_FILE = Path(__file__).resolve().parent / "analyze_result.json"

# ---- 契约与提示词的运行时默认值（调用方可覆盖；评分链路从 app_setting 传入）----
# 本场景是"照规则判定"，不需要任何创造性，温度固定 0 以求可复现（闸5）
DEFAULT_TEMPERATURE = float(config.get("LLM_TEMPERATURE", 0) or 0)
# 契约校验失败后的修复重试次数（G2：校验不能只"丢弃"，还要能"重来"）
DEFAULT_MAX_REPAIRS = int(config.get("REJUDGE_RETRIES", 1) or 0)
#: 提示词版本，落库到 assessment.prompt_version，用于归因「结论为何变了」
#: v3 = C-C 起加入「①② 记忆段不是本次证据」说明（架构 §4.5.6）
PROMPT_VERSION = config.get("PROMPT_VERSION", "v3")

# ---------------------------------------------------------------------------
# 内置样例对话（用于在真实消息正文可用前，验证规则与提示词）
# ---------------------------------------------------------------------------
SAMPLE_CHATS = {
    "high": {
        "name": "样例-高意向客户",
        "messages": [
            {"role": "客户", "text": "你好，我是做 Java 后端的，想问下你们的大模型课程怎么上"},
            {"role": "销售", "text": "您好！我们是线上直播课，从大模型基础到 Agent 落地都有，方便了解下您目前的情况吗"},
            {"role": "客户", "text": "我们公司下季度要上 AI 客服项目，我需要在 2 个月内能独立上手做出来"},
            {"role": "客户", "text": "课程都包含什么内容？大概多少钱？"},
            {"role": "销售", "text": "包含提示词、RAG、Agent 实战，价格我发您，另外今晚有一节公开课"},
            {"role": "客户", "text": "可以，我之前自己学过一点 LangChain，也调过 API"},
            {"role": "客户", "text": "我在杭州，公开课几点？我尽量参加"},
        ],
    },
    "low": {
        "name": "样例-低意向客户",
        "messages": [
            {"role": "销售", "text": "您好，关于课程有什么想了解的？"},
            {"role": "客户", "text": "多少钱？直接说价格"},
            {"role": "销售", "text": "我们的课程包含完整实战体系，价格是……"},
            {"role": "客户", "text": "太贵了，别家才几百块，你们这不值"},
            {"role": "客户", "text": "算了，网上免费教程多的是，我自己学就行"},
            {"role": "客户", "text": "不用了，不考虑了，别再发消息了"},
        ],
    },
}


def messages_to_text(msgs: list, plain: dict = None) -> str:
    """把消息列表转成给大模型的文本（转发给 ctx_preprocess 统一处理）。

    ``plain`` 为 {msgid: 企微明文条目}；微伴 OpenAPI 的 content 恒为 null，
    正文一律走企微 getchatdata（见 ww_chatdata.py）。

    保留原签名，但内部不再自行拼接——清洗、降噪、编号、时间全部由预处理层决定，
    避免「消息 → 文本」在这条 CLI 链路与评分链路之间出现第二份实现。
    """
    rows = []
    for m in msgs or []:
        rows.append({
            "message_id": m.get("message_id") or m.get("id") or m.get("original_id"),
            "msgid": m.get("original_id"),
            "direction": m.get("direction"),
            "msg_time": m.get("time") or m.get("msg_time"),
            "from_name": m.get("from_name"),
            "content": m.get("text_content") or _extract_text(m),
            "plain": (plain or {}).get(m.get("original_id")),
        })
    return ctx_preprocess.build_llm_context(rows)["text"]


def fetch_conversation(staff_id: str, session_id: str, max_messages: int = 100) -> str:
    """取真实会话正文（元数据来自微伴 OpenAPI，正文来自企微接口）。"""
    import ww_chatdata

    res = weiban.list_messages(staff_id, session_id, limit=min(max_messages, 100))
    msgs = res.get("message_list", [])
    if not msgs:
        return ""
    plain = ww_chatdata.WecomChatData(verbose=False).decrypt(msgs)
    msgs = sorted(msgs, key=lambda m: m.get("time") or 0)
    return messages_to_text(msgs, plain)


def _extract_text(m: dict) -> str:
    """从 content 中提取可读文本（不同消息类型结构不同）。"""
    c = m.get("content")
    if isinstance(c, dict):
        return c.get("content") or c.get("text") or ""
    if isinstance(c, str):
        return c
    return ""


def sample_to_text(messages: list) -> str:
    """内置样例同样走预处理层，保证 --sample 的输出与真实评分链路一致。"""
    rows = [{
        "message_id": f"sample-{i:03d}",
        "direction": "send" if m.get("role") == "销售" else "recv",
        "msg_time": 0,
        "content": m.get("text"),
    } for i, m in enumerate(messages or [], 1)]
    return ctx_preprocess.build_llm_context(rows)["text"]


SYSTEM_PROMPT = (
    "你是企业微信销售场景下的「客户意向分析助手」。"
    "你的任务是：依据系统给定的评分规则，从客户与销售的聊天记录中识别加分/扣分信号，"
    "并按要求输出一个 json 对象。\n"
    "聊天记录中每一行都带编号与时间【形如 #012 [10-08 09:12｜6天前] 客户: 内容】，请严格据此判断。\n"
    "严格要求：\n"
    "1. 只输出一个 json 对象，不要输出任何解释性文字、不要加 markdown 代码块。\n"
    "2. 键名必须严格等于给定 schema，禁止新增键、禁止改名、禁止遗漏必填键。\n"
    "3. rule_checks 必须覆盖系统列出的**每一条规则**：命中的给 hit=true 与分值，"
    "未命中的也要给 hit=false（score 为 null），并在 reason 里用一句话说明为什么不算命中。\n"
    "4. 命中的项必须用 evidence_ids 引用聊天记录的编号（如 [\"#012\"]，可引用多条）；"
    "证据不足就不要命中。\n"
    "5. 不得捏造聊天记录中不存在的信息（如预算、职位、到课、时间节点）。\n"
    "6. 判断「1 个月内」「1-3 个月」「已学习 1 个月以上」等时效类规则时，"
    "必须依据每条消息标注的时间与【当前日期】推算，不得凭感觉。\n"
    "7. customer_profile_summary 与 sales_strategy 使用中文。\n"
    "8. 不要自己计算总分，也不要输出总分类字段，总分由系统计算。\n"
    "9. 上下文里可能含有【客户状态档案】与【上次评分明细】两段——它们**是上一次评估的结论**，"
    "用于理解客户状态的变化（例如上次等级 S，本次客户说\"自己学\"）；"
    "**只有带 #编号 的消息行才是本次判断的证据**，不得用这两段里的内容命中规则或作为 evidence_ids。"
    "若你认为客户态度发生了变化，请在 customer_profile_summary 中说明该变化，"
    "但不要因此把上次的加减分项重复计入本次命中项。\n"
)


def build_user_prompt(rules: dict, conversation: str, facts: str,
                      current_date: str = "") -> str:
    from datetime import date
    current_date = current_date or date.today().isoformat()
    required = rules_mod.llm_rule_ids(rules)
    return (
        f"【当前日期】{current_date}\n\n"
        f"【评分规则】\n{rules_mod.rules_brief_for_prompt(rules)}\n\n"
        f"【必须逐条表态的规则 id（共 {len(required)} 条，每条都要在 rule_checks 里出现一次）】\n"
        f"{'、'.join(required)}\n\n"
        f"【上下文】（【客户状态档案】/【上次评分明细】= 上次评估的结论，仅供理解状态变化；"
        f"**只有带 #编号 的行是本次证据**）\n{conversation}\n\n"
        f"【补充事实】\n{facts}\n\n"
        f"【输出 schema】（json，键名必须与此完全一致）\n{contracts.prompt_schema()}\n\n"
        "【字段说明】\n"
        "- rule_checks[].rule_id：必须来自上面列出的规则 id\n"
        "- rule_checks[].hit：该规则是否命中（true/false）\n"
        "- rule_checks[].score：命中时给出档位分值；未命中必须为 null\n"
        "- rule_checks[].evidence_ids：命中时引用聊天记录编号，如 [\"#012\"]\n"
        "- rule_checks[].reason：一句话理由，未命中时说明为什么不算命中\n"
        "- customer_profile_summary：客户背景、需求、紧迫性、顾虑与当前阶段摘要\n"
        "- sales_strategy：下一步行动、沟通重点与建议跟进时间\n"
        "- model_confidence：你对本次判断的整体把握，0~1"
    )


def _attach_evidence(items: list, index: dict, labels: dict) -> tuple:
    """把命中项的 ``#编号`` 回链成 message_id，并用原文行回填证据。

    返回 ``(保留项, 因证据无法回链而丢弃的项)``。
    · 有 index 才校验：编号对不上（含模型干脆没给编号）的项会被丢弃并留痕，
      这就是「证据不足则不要命中」落到代码里的强制力。
    · ``evidence`` 一律取预处理层的原文行，不接受模型自述，杜绝编造证据。
    · 没有 index（老调用方）时原样返回，保持向后兼容。
    """
    if not index:
        return list(items or []), []
    labels = labels or {}
    kept, dropped = [], []
    for it in items or []:
        row = dict(it)
        res = ctx_preprocess.resolve_evidence(row.get("evidence_ids") or [], index)
        codes = res["codes"]
        if not codes:
            given = row.get("evidence_ids") or "未提供编号"
            dropped.append({**row, "invalid_evidence_ids": given,
                            "reason": f"证据无法回链到原文：{given}"})
            continue
        row["evidence_message_ids"] = res["message_ids"]
        if res["invalid"]:
            row["invalid_evidence_ids"] = res["invalid"]
        row["evidence"] = [labels[c] for c in codes if labels.get(c)]
        kept.append(row)
    return kept, dropped


def _parse_judgment(raw: str):
    """把原始响应解析成 ``JudgmentOut``；返回 ``(judgment, None)`` 或 ``(None, ValidationError)``。"""
    try:
        return contracts.JudgmentOut.model_validate_json(raw), None
    except ValidationError as err:
        # 容错：模型偶尔会包一层解释文字或 markdown 代码块，截取第一个 { 到最后一个 } 再试
        s, e = raw.find("{"), raw.rfind("}")
        if s >= 0 and e > s:
            try:
                return contracts.JudgmentOut.model_validate_json(raw[s:e + 1]), None
            except ValidationError as err2:
                return None, err2
        return None, err


def _repair_prompt(err: Exception) -> str:
    """把校验错误回灌给模型（这是「校验 → 通过或者重来」的那次"重来"）。"""
    return (
        "你上一次的输出不满足要求的 json schema，请修正后重新输出**完整**的 json 对象。\n"
        f"校验错误：\n{contracts.render_validation_errors(err)}\n\n"
        "注意：\n"
        "- 只输出 json 本身，不要加解释文字、不要加 markdown 代码块\n"
        "- 键名不得增删改；rule_checks 必须覆盖系统列出的全部规则 id\n"
        "- hit=false 的项 score 必须为 null；hit=true 的项必须给出分值"
    )


def _judge_with_repair(messages: list, *, temperature, max_tokens, max_repairs,
                       on_attempt=None):
    """调模型 + 契约校验；不合格就把校验错误回灌重来。

    这是「校验 → 通过或者重来」这条内循环的落地：改造前校验只决定"丢弃"，
    没有"重来"，模型偶发少字段就只能整条会话算失败、分数静默偏小。

    临时性网络故障（``RetryableLLMError``）不由这里重试，交给上层按可重试/不可重试分流。
    """
    convo = list(messages)
    attempts = 0
    last_err = None
    last_raw = ""
    while attempts <= max_repairs:
        attempts += 1
        meta = llm.chat(convo, temperature=temperature, json_mode=True,
                        max_tokens=max_tokens, return_meta=True)
        last_raw = meta.get("content") or ""
        if on_attempt:
            on_attempt(attempts, meta)
        judgment, err = _parse_judgment(last_raw)
        if judgment is not None:
            return judgment, attempts, meta
        last_err = err
        if attempts > max_repairs:
            break
        convo = convo + [
            {"role": "assistant", "content": last_raw[:2000]},
            {"role": "user", "content": _repair_prompt(err)},
        ]
    raise contracts.ContractViolationError(
        f"模型输出连续 {attempts} 次不满足契约：{last_err}",
        raw=last_raw,
        errors_text=contracts.render_validation_errors(last_err),
        attempts=attempts)


def analyze(conversation: str, facts: str, rules: dict, *,
            evidence_index: dict = None, labels: dict = None,
            current_date: str = None, temperature: float = None,
            max_tokens: int = None, max_repairs: int = None,
            on_attempt=None) -> dict:
    """调用大模型判断 → 契约校验（失败回灌重来）→ 派生命中项 → 证据回链 → 本地计分。

    ``evidence_index`` / ``labels`` 来自 ``ctx_preprocess.build_llm_context()``，
    用于把模型输出的 ``#编号`` 回链成 message_id（需求 §6.3）。

    返回 ``contracts.AssessmentOut`` 的字典形式。**总分与等级一律本地计算**，
    模型只负责"哪条规则命中、证据在哪"。
    """
    current_date = current_date or datetime.now().strftime("%Y-%m-%d")
    temperature = DEFAULT_TEMPERATURE if temperature is None else temperature
    max_repairs = DEFAULT_MAX_REPAIRS if max_repairs is None else max_repairs

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(rules, conversation, facts, current_date)},
    ]
    judgment, attempts, _meta = _judge_with_repair(
        messages, temperature=temperature, max_tokens=max_tokens,
        max_repairs=max_repairs, on_attempt=on_attempt)

    # 逐维度表态 → 命中项（item 结构与此前完全一致，前端与复核页零改动）
    pos_items, neg_items, derive_bad = rules_mod.derive_items(judgment.rule_checks, rules)
    # 漏检的规则（覆盖率 = 1 - 漏检/应表态），从此"模型漏看了哪条"是可数的
    unchecked = rules_mod.check_coverage(judgment.rule_checks, rules)

    # 证据回链（编号 → message_id），回链不上的项丢弃并留痕
    pos_items, pos_noev = _attach_evidence(pos_items, evidence_index, labels)
    neg_items, neg_noev = _attach_evidence(neg_items, evidence_index, labels)

    score = rules_mod.compute_score(pos_items, neg_items, rules)
    grade = rules_mod.grade_of(score["raw_score"], rules)

    out = contracts.AssessmentOut(
        raw_score=score["raw_score"],
        positive_total=score["positive_total"],
        negative_total=score["negative_total"],
        grade=grade["grade"],
        priority=grade.get("priority") or "",
        advice=grade.get("advice") or "",
        positive_items=pos_items,
        negative_items=neg_items,
        invalid_items=list(derive_bad) + list(pos_noev) + list(neg_noev),
        unchecked_rules=unchecked,
        profile_summary=judgment.customer_profile_summary,
        sales_strategy=judgment.sales_strategy,
        model_confidence=judgment.model_confidence,
        # 需要人工看一眼的判据由代码给：有规则被漏检就置真（模型不再自报"要不要人看"）
        needs_human_review=bool(unchecked),
        rule_version=rules.get("rule_version") or "",
        prompt_version=PROMPT_VERSION,
        schema_version=contracts.SCHEMA_VERSION,
        current_date=current_date,
        analyzed_at=int(time.time()),
        contract_attempts=attempts,
    )
    return out.model_dump()


def print_report(result: dict, title: str):
    print(f"\n===== 分析结果：{title} =====")
    print(f"总分：{result['raw_score']}（加分 {result['positive_total']} / 扣分 {result['negative_total']}）")
    print(f"等级：{result['grade']}（{result['priority']}优先级）")
    print(f"建议：{result['advice']}")
    print("\n加分项：")
    for it in result["positive_items"]:
        print(f"  +{it['score']}  {it['dimension']}（{it['rule_id']}）：{it['reason']}")
        for ev in it["evidence"][:2]:
            print(f"        证据：{ev}")
    print("扣分项：")
    for it in result["negative_items"]:
        print(f"  {it['score']}  {it['dimension']}（{it['rule_id']}）：{it['reason']}")
        for ev in it["evidence"][:2]:
            print(f"        证据：{ev}")
    if result["invalid_items"]:
        print("\n被判为非法的命中项（已忽略）：")
        for it in result["invalid_items"]:
            print(f"  {it.get('rule_id')}：{it.get('reason')}")
    if result.get("unchecked_rules"):
        print(f"\n模型漏表态的规则（覆盖率不足）：{result['unchecked_rules']}")
    print(f"\n客户画像：{result['profile_summary']}")
    print(f"销售策略：{result['sales_strategy']}")
    print(f"模型自报置信度：{result['model_confidence']}  需人工复核：{result['needs_human_review']}"
          f"  契约调用次数：{result['contract_attempts']}")


def _fetch_context(staff_id: str, session_id: str, max_messages: int = 100) -> dict:
    """取真实会话的完整上下文契约（text + index + labels），供证据回链使用。"""
    import ww_chatdata

    res = weiban.list_messages(staff_id, session_id, limit=min(max_messages, 100))
    msgs = res.get("message_list", [])
    if not msgs:
        return {}
    plain = ww_chatdata.WecomChatData(verbose=False).decrypt(msgs)
    msgs = sorted(msgs, key=lambda m: m.get("time") or 0)
    rows = []
    for m in msgs:
        rows.append({
            "message_id": m.get("message_id") or m.get("id") or m.get("original_id"),
            "msgid": m.get("original_id"),
            "direction": m.get("direction"),
            "msg_time": m.get("time") or m.get("msg_time"),
            "from_name": m.get("from_name"),
            "content": m.get("text_content") or _extract_text(m),
            "plain": (plain or {}).get(m.get("original_id")),
        })
    return ctx_preprocess.build_llm_context(rows)


def sample_context(messages: list) -> dict:
    """内置样例同样走预处理层（含 index/labels），保证 --sample 与真实链路一致。"""
    rows = [{
        "message_id": f"sample-{i:03d}",
        "direction": "send" if m.get("role") == "销售" else "recv",
        "msg_time": 0,
        "content": m.get("text"),
    } for i, m in enumerate(messages or [], 1)]
    return ctx_preprocess.build_llm_context(rows)


def main():
    ap = argparse.ArgumentParser(description="客户意向分析（无数据库版）")
    ap.add_argument("--sample", choices=list(SAMPLE_CHATS.keys()), help="使用内置样例")
    ap.add_argument("--staff-id", help="员工 id（真实会话模式）")
    ap.add_argument("--session-id", help="会话 id（真实会话模式）")
    ap.add_argument("--staff-name", default="", help="员工名称（展示用）")
    ap.add_argument("--target-name", default="", help="客户名称（展示用）")
    ap.add_argument("--max-messages", type=int, default=100, help="最多取多少条消息")
    ap.add_argument("--facts", default="无额外事实。", help="补充事实文本")
    ap.add_argument("--out", default=str(OUT_FILE), help="结果输出 JSON 路径")
    args = ap.parse_args()

    rules = rules_mod.load_rules()

    if args.sample:
        s = SAMPLE_CHATS[args.sample]
        ctx = sample_context(s["messages"])
        title = s["name"]
    elif args.staff_id and args.session_id:
        ctx = _fetch_context(args.staff_id, args.session_id, args.max_messages)
        title = f"{args.staff_name or args.staff_id} → {args.target_name or args.session_id}"
        if not (ctx.get("text") or "").strip():
            print("警告：该会话未取到可读消息正文（content 为空），无法分析。")
            return
    else:
        ap.error("请指定 --sample，或同时指定 --staff-id 与 --session-id")

    print(f"正在分析：{title}（规则版本 {rules.get('rule_version')}）...")
    try:
        result = analyze(ctx["text"], args.facts, rules,
                         evidence_index=ctx.get("index"), labels=ctx.get("labels"),
                         current_date=ctx.get("current_date"))
    except contracts.ContractViolationError as e:
        print(f"模型输出连续 {e.attempts} 次不满足契约，已放弃：\n{e.errors_text}")
        print("（评分链路会把这类会话写入 session_failure 并转人工复核）")
        return
    except llm.FatalLLMError as e:
        print(f"模型配置/认证错误（重试无意义）：{e}")
        return
    except llm.RetryableLLMError as e:
        print(f"模型临时故障，请稍后重试：{e}")
        return
    result["conversation_source"] = title

    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print_report(result, title)
    print(f"\n结果已写入：{args.out}")


if __name__ == "__main__":
    main()
