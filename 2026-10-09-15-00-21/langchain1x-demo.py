# -*- coding: utf-8 -*-
"""
LangChain 1.x 核心 API 可运行参考（已在 langchain 1.4.2 + langgraph 1.2.14 实测通过）

用假模型驱动，不需要 API Key，不需要联网。直接跑：
    <项目>/.venv/Scripts/python.exe langchain1x-demo.py

覆盖点：
  1. create_agent       —— 1.x 唯一推荐的建 Agent 入口
  2. @tool              —— 工具定义（docstring 就是给模型看的描述）
  3. AgentMiddleware    —— 自定义中间件，6 个 hook
  4. checkpointer       —— 短时记忆（thread_id 隔离会话）
  5. response_format    —— 结构化输出
  6. content_blocks     —— 1.x 新增，跨厂商统一的内容块
  7. stream_mode        —— 流式输出
  8. 预置中间件          —— 重试 / 限流 / 摘要 / PII
"""

import warnings

warnings.simplefilter("ignore")

from typing import List, Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ModelRequest,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver


# ---------------------------------------------------------------- 1. 工具
@tool
def get_weather(city: str) -> str:
    """查询指定城市的天气。当用户询问天气时调用。"""
    return f"{city}：晴，25℃"


@tool
def search_knowledge(query: str) -> str:
    """在本地知识库中检索资料。"""
    return f"关于「{query}」的检索结果：LangChain 1.x 于 2025 年 10 月发布正式版。"


# ---------------------------------------------------- 2. 自定义中间件
class AuditMiddleware(AgentMiddleware):
    """演示 4 个最常用的 hook。生产里可用于审计、限流、动态改 prompt。"""

    def before_agent(self, state, runtime):
        """整个 Agent 启动前跑一次：可做输入校验、加载记忆。"""
        print("  [before_agent] 本次会话消息数 =", len(state["messages"]))
        return None

    def before_model(self, state, runtime):
        """每次调 LLM 前：可改 prompt、裁剪历史消息。"""
        print("  [before_model] 准备第 N 次 LLM 调用")
        return None

    def wrap_model_call(self, request: ModelRequest, handler):
        """包裹 LLM 调用：可换模型、换工具集（成本优化常用）。"""
        print("  [wrap_model_call] 拦截模型调用")
        return handler(request)

    def wrap_tool_call(self, request, handler):
        """包裹工具调用：可做审批、结果脱敏、异常兜底。"""
        tool_name = request.tool_call["name"]
        print(f"  [wrap_tool_call] 即将执行工具：{tool_name}")
        return handler(request)


# ---------------------------------------------------- 3. 假模型（免 API Key）
class ScriptedModel(BaseChatModel):
    """按脚本依次返回消息的最小可运行模型。真项目换成 ChatOpenAI 即可。"""

    script: List[AIMessage] = []
    i: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        # Agent 一定会调 bind_tools 把工具绑到模型上，必须实现
        return self

    def _generate(self, messages: Sequence[BaseMessage], stop=None,
                  run_manager=None, **kwargs) -> ChatResult:
        msg = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


def build_agent(script, middleware, checkpointer=None, response_format=None):
    return create_agent(
        model=ScriptedModel(script=script),
        tools=[get_weather, search_knowledge],
        system_prompt="你是一个乐于助人的助手。",
        middleware=middleware,
        checkpointer=checkpointer,
        response_format=response_format,
    )


# ================================================================ 演示
def demo_1_tool_loop():
    print("\n【1】最小 Agent：模型 → 调工具 → 再回答")
    script = [
        AIMessage(content="", tool_calls=[
            {"name": "get_weather", "args": {"city": "上海"}, "id": "c1", "type": "tool_call"}
        ]),
        AIMessage(content="上海今天晴，25℃。"),
    ]
    agent = build_agent(script, [AuditMiddleware()])
    print("  agent 实际类型 =", type(agent).__name__, "（底层是 LangGraph 图）")
    state = agent.invoke({"messages": [{"role": "user", "content": "上海天气怎么样？"}]})
    print("  消息轨迹：")
    for m in state["messages"]:
        print(f"    {m.type:<8} {getattr(m, 'name', '') or '':<14} {str(m.content)[:40]}")
    print("  最终答案 =", state["messages"][-1].content)


def demo_2_memory():
    print("\n【2】短时记忆：checkpointer + thread_id")
    script = [AIMessage(content="好的，我记住了。"), AIMessage(content="你叫小明。")]
    agent = build_agent(script, [], checkpointer=InMemorySaver())
    cfg_a = {"configurable": {"thread_id": "user-1:conv-1"}}
    cfg_b = {"configurable": {"thread_id": "user-2:conv-1"}}  # 另一个会话

    agent.invoke({"messages": [{"role": "user", "content": "我叫小明"}]}, cfg_a)
    # 同一 thread：能拿到上文
    s1 = agent.invoke({"messages": [{"role": "user", "content": "我叫什么？"}]}, cfg_a)
    print("  同一 thread_id 内的消息数 =", len(s1["messages"]), "（含历史）")

    s2 = agent.invoke({"messages": [{"role": "user", "content": "我叫什么？"}]}, cfg_b)
    print("  切换 thread_id 后消息数 =", len(s2["messages"]), "（全新会话，互不串味）")


def demo_3_structured_output():
    print("\n【3】结构化输出：response_format")
    from pydantic import BaseModel, Field

    class Weather(BaseModel):
        city: str = Field(description="城市名")
        temp_c: int = Field(description="温度，摄氏度")

    # 关键：response_format 会把 schema 变成一个同名工具绑给模型，
    # 模型必须回一个名为 Weather 的 tool_call，Agent 才会填充 structured_response
    script = [
        AIMessage(content="", tool_calls=[
            {"name": "get_weather", "args": {"city": "北京"}, "id": "c2", "type": "tool_call"}
        ]),
        AIMessage(content="", tool_calls=[
            {"name": "Weather", "args": {"city": "北京", "temp_c": 25},
             "id": "c3", "type": "tool_call"}
        ]),
    ]
    agent = build_agent(script, [], response_format=Weather)
    state = agent.invoke({"messages": [{"role": "user", "content": "北京天气"}]})
    print("  结构化结果 =", state.get("structured_response"))
    print("  类型 =", type(state.get("structured_response")).__name__)


def demo_4_stream():
    print("\n【4】流式输出：stream_mode='messages'")
    script = [AIMessage(content="你好，我是助手。")]
    agent = build_agent(script, [])
    chunks = []
    for event in agent.stream(
        {"messages": [{"role": "user", "content": "你好"}]},
        stream_mode="messages",
    ):
        msg = event[0] if isinstance(event, tuple) else event
        if getattr(msg, "content", ""):
            chunks.append(msg.content)
    print("  收到分片 =", chunks)


def demo_5_content_blocks():
    print("\n【5】content_blocks：1.x 跨厂商统一内容块")
    m = AIMessage(content="hello")
    print("  AIMessage.content_blocks =", m.content_blocks)


def demo_6_prebuilt_middleware():
    print("\n【6】预置中间件：限流 / 重试 / 摘要 / PII（1.4 已内置 15+ 个）")
    from langchain.agents.middleware import PIIMiddleware, SummarizationMiddleware

    script = [AIMessage(content="已处理。")]
    agent = build_agent(
        script,
        [
            ModelCallLimitMiddleware(run_limit=5),                      # 单次运行最多调 5 次模型
            PIIMiddleware("email", strategy="redact", apply_to_input=True),  # 邮箱自动脱敏
            SummarizationMiddleware(
                model=ScriptedModel(script=[AIMessage(content="（历史摘要）")]),
                trigger=("tokens", 4000),                                # 超 4000 token 触发摘要
                keep=("messages", 20),                                   # 保留最近 20 条
            ),
        ],
    )
    state = agent.invoke({"messages": [{"role": "user", "content": "帮我看看 test@example.com 这个邮箱"}]})
    print("  最终答案 =", state["messages"][-1].content)


def demo_7_middleware_limits():
    """限流与错误兜底中间件——三个必须实测才知道的行为差异。"""
    print("\n【7】限流 / 错误兜底中间件实测")
    from langchain.agents.middleware import ToolCallLimitMiddleware, ToolErrorMiddleware

    @tool
    def good() -> str:
        """一个正常返回的工具。"""
        return "ok"

    @tool
    def broken() -> str:
        """一个总会抛异常的工具。"""
        raise RuntimeError("外部服务 500")

    # 模型一直要求调工具，用来触发限流
    loop_script = [
        AIMessage(content="", tool_calls=[{"name": "good", "args": {}, "id": f"i{n}", "type": "tool_call"}])
        for n in range(40)
    ]

    def run_limit_case(label, mw):
        try:
            agent = create_agent(
                model=ScriptedModel(script=list(loop_script)),
                tools=[good], middleware=list(mw),
            )
            state = agent.invoke(
                {"messages": [{"role": "user", "content": "开始"}]},
                {"recursion_limit": 200},
            )
            n_tool = sum(1 for m in state["messages"] if m.type == "tool")
            print(f"  {label:<28} → 正常结束，工具执行 {n_tool} 次")
        except Exception as exc:
            print(f"  {label:<28} → {type(exc).__name__}")

    # 关键：ToolCallLimit 默认 exit_behavior="continue" 只挡这一次调用，
    # 模型会继续请求同一工具，最终撞 GraphRecursionError —— 等于没设限。
    run_limit_case("ToolCallLimit continue(默认)", [ToolCallLimitMiddleware(tool_name="good", run_limit=3)])
    run_limit_case("ToolCallLimit end", [ToolCallLimitMiddleware(tool_name="good", run_limit=3, exit_behavior="end")])
    run_limit_case("ToolCallLimit error", [ToolCallLimitMiddleware(tool_name="good", run_limit=3, exit_behavior="error")])
    run_limit_case("ModelCallLimit run_limit=4", [ModelCallLimitMiddleware(run_limit=4)])

    # ToolError：把工具异常转成 ToolMessage 交给模型自愈；不返回内容则异常上抛
    script = [
        AIMessage(content="", tool_calls=[{"name": "broken", "args": {}, "id": "x", "type": "tool_call"}]),
        AIMessage(content="抱歉，工具暂时不可用。"),
    ]
    print("  无 ToolError 兜底 →", end=" ")
    try:
        create_agent(model=ScriptedModel(script=list(script)), tools=[broken]).invoke(
            {"messages": [{"role": "user", "content": "试试"}]})
        print("未抛错")
    except Exception as exc:
        print(f"{type(exc).__name__}（运行被终止）")

    # on_error 签名是 (exception, ToolCallRequest) 两个参数
    print("  有 ToolError 兜底 →", end=" ")
    agent = create_agent(
        model=ScriptedModel(script=list(script)), tools=[broken],
        middleware=[ToolErrorMiddleware(
            on_error=lambda exc, req: f"工具 {req.tool_call['name']} 失败（{type(exc).__name__}）"
        )],
    )
    state = agent.invoke({"messages": [{"role": "user", "content": "试试"}]})
    print("Agent 自愈并收尾：", state["messages"][-1].content)


if __name__ == "__main__":
    print("=" * 62)
    print("LangChain 1.x API 演示（假模型驱动，无需 API Key）")
    print("=" * 62)
    demo_1_tool_loop()
    demo_2_memory()
    demo_3_structured_output()
    demo_4_stream()
    demo_5_content_blocks()
    demo_6_prebuilt_middleware()
    demo_7_middleware_limits()
    print("\n全部演示完成。")
