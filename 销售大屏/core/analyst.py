"""
AI 数据分析师：基于 LangChain 的 create_agent + DeepSeek 模型。

设计要点
--------
1. 模型只被授予一个工具 `query_sales_data`，该工具只接受**结构化参数**，
   模型无法执行任意代码，也无法绕过工具直接读文件。
2. 系统提示词强制「先调工具、后作答」，并禁止编造数字。
3. 每次回答都会返回**工具调用轨迹**，便于核对结论是否来自真实数据。
"""

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
import re

import config
from core.analyst_tools import TOOLS

SYSTEM_PROMPT = """你是「AI 数据分析师」，服务于一个美妆销售数据大屏。

【最高优先级的规则】
1. 只要用户的问题涉及数据（销量、销售额、利润、利润率、省份、产品、渠道、日期、
   趋势、排名、对比、占比、最高/最低、多少、几个 等），
   你必须**先调用 query_sales_data 工具**拿到真实结果，然后**只依据工具返回的内容**作答。
   绝对不允许在没有调用工具的情况下给出任何具体数字。
2. 你没有任何"记忆中的"业务数据。严禁猜测、编造、估算、举例数字。
3. 只有用户明确在闲聊、问与数据无关的问题时，才可以不调用工具。
4. 如果工具返回「无数据」，或返回的数据不足以回答用户的问题，
   必须**直接说明查不到/数据不足**，并说明缺什么；
   可以建议放宽筛选条件，或先用 analysis=options 查看有哪些可选取值。
   绝对不要用近似值或"大概"来糊弄。
5. 不确定有哪些省份/产品/渠道/日期范围时，先用 analysis=options 查询，不要凭猜测填参数。
6. 你只能通过 query_sales_data 访问数据。你不能执行代码，也不要声称自己运行了代码。

【数据说明】
数据表字段：日期、省份、产品、渠道、销量、销售额、利润（美妆销售流水）。
利润率 = 利润 ÷ 销售额 × 100%。

【工具用法要点】
- 看整体情况：analysis=summary
- 排名：analysis=rank，并指定 dimension（省份/产品/渠道）与 metric
- 对比：analysis=compare，会给出占比与相对平均水平的偏离
- 趋势：analysis=trend，dimension 用 月份 或 日期（默认按月）
- 查可选取值：analysis=options + dimension
- 筛选条件：start_date / end_date / provinces / products / channels（多个用逗号分隔）

【回答要求】
- 用简体中文回答，结构清晰；多组数据用表格或短列表呈现。
- 先给结论，再给关键数据支撑。
- 金额保留两位小数；大额可用「万」简化，但要同时给出精确值。
- 不要输出工具返回的原始文本或 JSON，只提炼成结论。
"""

# 模型与 agent 都做懒加载：首次提问时才创建，避免启动即请求外部接口
_model = None
_agent = None
_brief_agent = None


def _parse_brief(text: str) -> dict:
    """
    从固定格式的回答中拆出「结论」与「依据」。

    格式不稳定时降级处理：整段文本作为结论，不丢内容。
    """
    conclusion, evidence = "", []
    collecting = False

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue

        # 结论行：  结论：xxx
        matched = re.match(r"^(?:简短)?结论\s*[:：]\s*(.+)$", line)
        if matched:
            conclusion = matched.group(1).strip()
            collecting = False
            continue

        # 依据标题行：  依据：
        if re.match(r"^(?:关键)?(?:数据)?依据\s*[:：]?\s*$", line):
            collecting = True
            continue

        # 依据条目：以 - * · • 或 1. 1、 1) 开头
        if collecting:
            item = re.sub(r"^(?:[-*·•]\s*|\d+[.、)]\s*)", "", line).strip()
            if item:
                evidence.append(item)

    if not conclusion:
        # 没按格式输出时，整段作为结论，保证不丢信息
        conclusion = (text or "").strip()

    return {"conclusion": conclusion, "evidence": evidence[:6]}


def is_configured() -> bool:
    """是否已配置 API Key。"""
    return bool(config.DEEPSEEK_API_KEY)


def _get_model():
    """创建 DeepSeek 对话模型（OpenAI 兼容接口）。"""
    global _model
    if _model is None:
        if not is_configured():
            raise RuntimeError("未配置 DEEPSEEK_API_KEY，请在项目根目录的 .env 中填写")
        _model = ChatOpenAI(
            model=config.DEEPSEEK_MODEL,
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
            temperature=0,                  # 数据分析场景要稳定，不要发挥
            max_tokens=config.AI_MAX_TOKENS,
            timeout=120,
            max_retries=2,
        )
    return _model


def _get_agent():
    """创建 agent（LangChain 1.x 的 create_agent）。"""
    global _agent
    if _agent is None:
        _agent = create_agent(
            model=_get_model(),
            tools=TOOLS,
            system_prompt=SYSTEM_PROMPT,
        )
    return _agent


# ---------------- 简报模式（大屏右下角悬浮机器人用） ----------------

BRIEF_SYSTEM_PROMPT = """你是嵌入在销售大屏里的「AI 数据分析助手」。

【是否调用工具】
- 只要问题涉及数据（销量、销售额、利润、利润率、省份、产品、渠道、日期、
  趋势、排名、对比、占比、最高/最低、多少 等），必须**先调用 query_sales_data**，
  再依据工具返回的真实结果作答。绝不凭记忆给数字。
- 如果用户只是打招呼、闲聊、问你是谁这类与数据无关的话，
  **不要调用工具**，直接用一两句话友好回应即可。

【回答格式】
数据类问题必须严格用下面这个格式，不要用 Markdown 标题、表格或加粗：

结论：<一句话结论，必须包含具体数字>
依据：
- <关键数据点，含具体数字>
- <关键数据点，含具体数字>

非数据类问题则不受格式限制，正常简短回复。

【边界的绝对约束】
1. 你是**只读**的：只能查询和分析数据，
   **绝不能**修改、删除销售数据文件（CSV），
   **绝不能**改动大屏上的图表、指标或页面内容。
   不要声称自己做了任何写操作，也不要建议用户让你去改数据。
2. 如果工具返回「无数据」，或当前还没有上传数据文件，必须直接说明无法分析，
   并提示去「数据管理后台」上传包含「日期、省份、产品、渠道、销量、销售额、利润」的文件。
   绝不用估算值或"大概"来搪塞。
3. 不确定有哪些省份/产品/渠道时，先用 analysis=options 查询再取数。
4. 你不能执行代码。工具是唯一的数据入口。

【数据说明】
字段：日期、省份、产品、渠道、销量、销售额、利润（美妆销售流水）。
利润率 = 利润 ÷ 销售额 × 100%。"""


def _get_brief_agent():
    """简报模式的 agent：工具相同，但回答格式与约束更严格。"""
    global _brief_agent
    if _brief_agent is None:
        _brief_agent = create_agent(
            model=_get_model(),
            tools=TOOLS,
            system_prompt=BRIEF_SYSTEM_PROMPT,
        )
    return _brief_agent


def _as_text(content) -> str:
    """把消息内容规整成纯文本（兼容字符串与分块列表两种形态）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content or "")


def reset_agent():
    """清空缓存的模型与 agent（改了 .env 后调用）。"""
    global _model, _agent, _brief_agent
    _model = None
    _agent = None
    _brief_agent = None


def _build_messages(question: str, history: list) -> list:
    """组装消息列表：历史 + 本轮提问（只保留 user/assistant 两类）。"""
    messages = [
        {"role": item["role"], "content": item["content"]}
        for item in (history or [])
        if item.get("role") in ("user", "assistant") and item.get("content")
    ]
    messages.append({"role": "user", "content": question})
    return messages


def _extract_trace(outputs: list) -> tuple:
    """
    从消息轨迹里提取「工具调用」与「实际执行过的 Pandas 表达式」。

    表达式来自工具的执行回传（artifact），不是模型自己说的，可信。
    """
    tool_calls, pandas_ops = [], []
    for msg in outputs:
        for call in getattr(msg, "tool_calls", None) or []:
            tool_calls.append({"tool": call.get("name"), "args": call.get("args", {})})
        artifact = getattr(msg, "artifact", None)
        if isinstance(artifact, dict) and artifact.get("pandas"):
            expr = artifact["pandas"]
            if expr not in pandas_ops:      # 去重，保留先后顺序
                pandas_ops.append(expr)
    return tool_calls, pandas_ops


def _last_ai_text(outputs: list) -> str:
    """取最后一条有内容的 AI 消息文本。"""
    for msg in reversed(outputs):
        if getattr(msg, "type", "") == "ai":
            text = _as_text(msg.content).strip()
            if text:
                return text
    return ""


def ask(question: str, history: list = None) -> dict:
    """
    向 AI 数据分析师提问。

    :param question: 用户问题
    :param history: 历史对话，形如 [{"role": "user"/"assistant", "content": "..."}]
    :return: {"answer": str, "toolCalls": [...], "steps": int}
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")

    result = _get_agent().invoke(
        {"messages": _build_messages(question, history)},
        config={"recursion_limit": config.AI_RECURSION_LIMIT},
    )

    outputs = result.get("messages", [])
    tool_calls, _ = _extract_trace(outputs)
    answer = _last_ai_text(outputs)

    if not answer:
        answer = "抱歉，模型没有返回有效内容。请重试，或换一种问法。"

    return {
        "answer": answer,
        "toolCalls": tool_calls,
        "steps": len(outputs),
    }


def ask_brief(question: str, history: list = None) -> dict:
    """
    简报模式：给大屏右下角悬浮机器人用。

    与 ask() 的区别是回答被拆成「简短结论 + 关键数据依据」，
    并额外回传本次实际执行过的 Pandas 表达式。

    :return: {"conclusion", "evidence", "toolCalls", "pandasOps", "usedTool", "steps"}
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")

    result = _get_brief_agent().invoke(
        {"messages": _build_messages(question, history)},
        config={"recursion_limit": config.AI_RECURSION_LIMIT},
    )

    outputs = result.get("messages", [])
    tool_calls, pandas_ops = _extract_trace(outputs)
    parsed = _parse_brief(_last_ai_text(outputs))

    return {
        "conclusion": parsed["conclusion"] or "抱歉，模型没有返回有效内容。",
        "evidence": parsed["evidence"],
        "toolCalls": tool_calls,
        "pandasOps": pandas_ops,
        # 未调用工具说明是闲聊类问题，前端可据此调整展示
        "usedTool": len(tool_calls) > 0,
        "steps": len(outputs),
    }
