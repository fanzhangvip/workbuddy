# -*- coding: utf-8 -*-
"""大模型客户端（OpenAI 兼容接口，适配 DeepSeek）。

配置项来自 .env：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL

安全：API Key 仅在请求头中使用，不打印、不写入日志。

为什么异常要分层
------------------------------------------------------------------
调用方需要区分「等一会儿重试就好了」和「重试一万次也没用」：
  · 401/403/400 —— 认证或参数错误，**重试纯属浪费时间**（一次 401 白等 3.6 秒 × 200 会话）
  · 408/429/5xx / 超时 / 连接重置 —— 临时故障，值得退避重试
这一层分类是后续 LangGraph ``RetryPolicy(retry_on=...)`` 的基础。
"""
import json
import urllib.error
import urllib.request

import config

TIMEOUT = 120

#: 默认输出上限。必须显式设置：DeepSeek 的 JSON mode 在 max_tokens 过小时会**静默截断**，
#: 返回一个看起来正常、实际缺字段的 JSON。
#:
#: 实测定标（2026-10-14，40 条消息的真实会话）：14 条 rule_checks 每条都带 ``reason``
#: （契约上限 800 字）时，3072 会被截断（finish_reason=length）→ 整会话失败。
#: 提高到 8192（deepseek-chat 的输出上限）后稳定通过；这是**上限**而非目标长度，
#: 模型正常结束时不会用到。若仍触顶，优先考虑压缩 rule_checks 的 reason 而非继续加 token。
DEFAULT_MAX_TOKENS = 8192


class LLMError(Exception):
    """大模型调用相关错误的基类。"""


class RetryableLLMError(LLMError):
    """临时性故障：超时 / 限流 / 5xx / 连接重置。值得退避重试。

    ``retry_after``：服务端通过 ``Retry-After`` 头告知的等待秒数（可能为 None）。
    **服务端已经告诉我们要等多久时，就不该再按自己的指数退避猜** —— 见 `_classify_http_error`。
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class FatalLLMError(LLMError):
    """确定性故障：认证失败、参数错误、模型不存在。重试没有意义，应直接转人工。"""


class EmptyCompletionError(RetryableLLMError):
    """JSON mode 下模型未能产出合法 JSON 时，DeepSeek 会返回空串而不是报错。"""


class TruncatedCompletionError(RetryableLLMError):
    """``finish_reason == "length"``：输出被 max_tokens 截断，JSON 必然残缺。

    ``partial`` / ``max_tokens`` 是给调用方自救用的（文章 H 的两级处理）：
      ① 还没到模型上限 → **升 max_tokens 重试一次**
      ② 已经到上限（``escalate_max_tokens()`` 返回 None）→ **改用续写**，
         把 ``partial`` 作为 assistant 已产出的前缀接上继续生成
    """

    def __init__(self, message: str, partial: str = "", max_tokens: int | None = None):
        super().__init__(message)
        self.partial = partial
        self.max_tokens = max_tokens


#: 模型侧的输出上限（deepseek-chat）。**到顶后无法再靠升 max_tokens 自救** ——
#: 这正是 `escalate_max_tokens()` 返回 None 的判据，也是「截断只能靠续写」的原因。
MODEL_MAX_OUTPUT_TOKENS = 8192

#: 续写提示：让模型**接着**把没写完的 JSON 补完，而不是从头重写一遍
#: （从头重写会丢掉已生成的字段，且可能每次都恰好截在同一处 → 死循环）
CONTINUATION_PROMPT = (
    "你上一条输出因为达到 max_tokens 上限而被**截断**了（JSON 不完整）。\n"
    "请**紧接着**截断处继续输出剩余内容，把 JSON 补完整。\n"
    "要求：\n"
    "- 只输出**剩余部分**，不要重复已经输出过的内容，不要加任何解释\n"
    "- 不要重新开头，不要加 markdown 代码块\n"
    "- 保持 JSON 合法（补齐缺失的括号与引号）"
)


def escalate_max_tokens(current: int | None) -> int | None:
    """截断后应升到多少？已到模型上限 → 返回 ``None``（**升无可升，改走续写**）。"""
    cur = int(current or DEFAULT_MAX_TOKENS)
    if cur >= MODEL_MAX_OUTPUT_TOKENS:
        return None
    return MODEL_MAX_OUTPUT_TOKENS


_FATAL_CODES = {400, 401, 403, 404, 422}


def _classify_http_error(err: urllib.error.HTTPError) -> LLMError:
    """按 HTTP 状态码分流：确定性失败 vs 临时性失败。"""
    try:
        body = err.read().decode("utf-8", "ignore")[:300]
    except Exception:                                     # pragma: no cover
        body = ""
    msg = f"HTTP {err.code}：{body or err.reason}"
    if err.code in _FATAL_CODES:
        return FatalLLMError(msg)
    return RetryableLLMError(msg, retry_after=_retry_after_seconds(err))


#: `Retry-After` 的等待上限（秒）。文章 B：限流场景等待上限 ~30s，
#: 再往上等不如让上层转人工 —— 一个请求卡几分钟对批处理是灾难。
RETRY_AFTER_CAP = 30.0


def _retry_after_seconds(err: urllib.error.HTTPError) -> float | None:
    """解析 ``Retry-After`` 头 → 秒数（仅支持 delta-seconds 形式，且带上限）。

    只认数字形式：HTTP-date 形式（"Wed, 21 Oct 2015 07:28:00 GMT"）需要解析日期，
    且服务端极少数情况下才用；解析不了就返回 None，**让上层退回自己的指数退避**，
    而不是假装读懂了。
    """
    try:
        raw = (err.headers.get("Retry-After") or "").strip()
    except Exception:                                     # pragma: no cover
        return None
    if not raw:
        return None
    try:
        return min(max(float(raw), 0.0), RETRY_AFTER_CAP)
    except (TypeError, ValueError):
        return None


def chat(messages: list, temperature: float = 0.0, json_mode: bool = False,
         timeout: int = TIMEOUT, max_tokens: int = None, return_meta: bool = False):
    """调用对话补全，返回文本内容（``return_meta=True`` 时返回含 usage 的字典）。

    :param json_mode: 要求模型返回 JSON 对象。DeepSeek 只支持 ``{"type":"json_object"}``，
        传 ``json_schema`` 会得到 ``This response_format type is unavailable now``，
        所以 **schema 强约束只能靠提示词 + 客户端校验**（见 contracts.py）。
    :param max_tokens: 输出上限。``None`` 时用 :data:`DEFAULT_MAX_TOKENS`，避免静默截断。
    """
    if not config.LLM_BASE_URL or not config.LLM_API_KEY:
        raise FatalLLMError("缺少大模型配置：请在 .env 中设置 LLM_BASE_URL 与 LLM_API_KEY")

    url = config.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    body = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
        "max_tokens": int(max_tokens or DEFAULT_MAX_TOKENS),
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.LLM_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise _classify_http_error(e) from e
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise RetryableLLMError(f"网络错误：{type(e).__name__}: {e}") from e
    except json.JSONDecodeError as e:
        raise RetryableLLMError(f"响应不是合法 JSON：{e}") from e

    if not isinstance(res, dict) or "choices" not in res:
        raise RetryableLLMError(f"大模型返回异常：{json.dumps(res, ensure_ascii=False)[:300]}")

    choice = res["choices"][0] or {}
    finish = choice.get("finish_reason")
    content = (choice.get("message") or {}).get("content") or ""

    if finish == "length":
        raise TruncatedCompletionError(
            f"输出被 max_tokens({body['max_tokens']}) 截断，JSON 必然残缺",
            partial=content, max_tokens=int(body["max_tokens"]))
    if not content.strip():
        raise EmptyCompletionError("模型返回空内容（JSON mode 常见失败形态）")

    if return_meta:
        return {
            "content": content,
            "finish_reason": finish,
            "usage": res.get("usage") or {},
            "model": res.get("model"),
        }
    return content


if __name__ == "__main__":
    print("模型:", config.LLM_MODEL, "| 接口:", config.LLM_BASE_URL)
    out = chat([{"role": "user", "content": "只回复两个字：连通"}])
    print("返回:", out.strip()[:100])
