# -*- coding: utf-8 -*-
"""统一 HTTP 客户端包装（架构 §2.5 工具层）。

## 为什么需要它

文档要求 `weiban.py` / `ww_chatdata.py` / `llm.py` / `wb_keepalive.py`
「统一 client wrapper（超时 / 分类重试 / 结果校验 / 审计 / metrics）」。
改造前每个模块**各写一套** `urlopen`：

· `weiban.py` —— 裸 `urlopen`，**没有任何重试**，一次瞬时抖动就直接抛异常
· `ww_chatdata.py` —— 自己一套
· `llm.py` —— 自己一套**异常分层**（`RetryableLLMError` / `FatalLLMError`），做得最好
· `wb_keepalive.py` —— 又一套

后果：同样是「网络抖一下」，LLM 调用会自动退避重试，而同步会话**直接失败**；
且失败率没有统一口径（`tool_failures` 只能靠各模块自己记）。

## 本模块提供

· **统一超时**（默认 30s，可传）
· **分类重试**：只重试「临时故障」（网络异常 / 408 / 429 / 5xx），
  认证与参数错误（400/401/403）**立即失败** —— 重试一万次也没用
· **结果校验**：必须是合法 JSON（可选：要求 `errcode == 0`）
· **审计与 metrics**：失败计数可被调用方取走（`stats()`），不打印正文

## 与 llm.py 的关系

`llm.py` 的异常分层**保留不动**（它有 LLM 特有的 `TruncatedCompletionError` 等语义）。
本模块是**更底层的通用件**；`weiban.py` 这类「普通 HTTP + JSON + errcode」的接口
直接用它即可，不必各自复制重试逻辑。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

#: 默认超时（秒）。微伴 OpenAPI 与企微接口通用。
DEFAULT_TIMEOUT = 30

#: 可重试的 HTTP 状态码：408 超时 / 429 限流 / 5xx 服务端
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

#: 进程内 metrics（键 → 计数）。纯内存，不落库；由上层按需取走写进 scoring_run。
_METRICS = {}


def _inc(key: str, n: int = 1) -> None:
    _METRICS[key] = _METRICS.get(key, 0) + n


def stats() -> dict:
    """取进程内调用统计（供 `tool_failures` 口径使用）。"""
    return dict(_METRICS)


def reset_stats() -> None:
    _METRICS.clear()


class HttpFailure(Exception):
    """统一失败类型。``retryable`` 告诉调用方「值得再试一次吗」。"""

    def __init__(self, message: str, *, retryable: bool = False, status: int = None,
                 body: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.body = body


def request_json(method: str, url: str, *, params: dict = None, body: dict = None,
                 headers: dict = None, timeout: int = DEFAULT_TIMEOUT,
                 retries: int = 2, backoff: float = 1.2,
                 expect_errcode: bool = False, label: str = "") -> dict:
    """发一次 JSON 请求，返回解析后的字典。

    :param retries: **额外**重试次数（``retries=2`` → 最多发 3 次）。只对可重试故障生效。
    :param expect_errcode: 为真时，响应里 ``errcode != 0`` 视为失败（微伴/企微的惯例）。
        这类失败**不重试**（业务错误重试无意义），直接抛 ``HttpFailure(retryable=False)``。
    :param label: 用于 metrics 的可读标签（如 ``"weiban.chat.session.list"``）。
    """
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"Content-Type": "application/json"} if data else {}
    if headers:
        hdrs.update(headers)

    tag = label or url.split("/open-api/")[-1][:40] or "http"
    attempts = max(1, int(retries) + 1)
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            try:
                res = json.loads(raw)
            except json.JSONDecodeError as e:
                # 返回了非 JSON（网关 HTML 错误页等）→ 视为临时故障，值得重试
                raise HttpFailure(f"响应不是合法 JSON：{e}；前 200 字：{raw[:200]}",
                                  retryable=True) from e
            if expect_errcode and isinstance(res, dict) and int(res.get("errcode") or 0) != 0:
                # 业务错误：不重试（重试一万次也不会变对），但记 metrics
                _inc(f"fail.{tag}")
                raise HttpFailure(f"接口返回错误 errcode={res.get('errcode')} "
                                  f"errmsg={res.get('errmsg')}", retryable=False,
                                  body=raw[:500])
            _inc(f"ok.{tag}")
            return res
        except urllib.error.HTTPError as e:
            retryable = e.code in RETRYABLE_STATUS
            last = HttpFailure(f"HTTP {e.code} {e.reason}", retryable=retryable,
                               status=e.code)
            if not retryable:
                _inc(f"fail.{tag}")
                raise last from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = HttpFailure(f"网络错误：{type(e).__name__}: {e}", retryable=True)
        except HttpFailure as e:
            if not e.retryable:
                raise
            last = e
        # 退避（指数）：1.2s → 2.4s …
        if i < attempts - 1:
            time.sleep(backoff * (2 ** i))
    _inc(f"fail.{tag}")
    raise last or HttpFailure("未知失败")


def request_json_safe(*args, **kwargs) -> dict:
    """:func:`request_json` 的**不抛异常**版本，失败返回 ``{"ok": False, "error": ...}``。

    用于「失败可恢复」的调用点（例如取标签），避免每个调用方都写一遍 try/except。
    """
    try:
        res = request_json(*args, **kwargs)
        if isinstance(res, dict):
            return {**res, "ok": True}
        return {"ok": True, "data": res}
    except Exception as e:                      # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
