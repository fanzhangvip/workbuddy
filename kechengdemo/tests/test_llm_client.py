# -*- coding: utf-8 -*-
"""模型客户端异常分层单测（不依赖网络：用假响应替换 urlopen）。

    C:/Users/EDY/.workbuddy/binaries/python/envs/default/Scripts/python.exe tests/test_llm_client.py
"""
import io
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm                                     # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if detail else ""))


class FakeResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(payload=None, error=None):
    def _open(req, timeout=None):
        if error:
            raise error
        return FakeResp(payload)
    return _open


def with_urlopen(fn):
    original = llm.urllib.request.urlopen
    llm.urllib.request.urlopen = fn
    try:
        return llm.chat([{"role": "user", "content": "x"}], json_mode=True)
    finally:
        llm.urllib.request.urlopen = original


def expect(name, error, want_exc, retryable):
    try:
        with_urlopen(fake_urlopen(error=error))
    except want_exc as e:
        is_retryable = isinstance(e, llm.RetryableLLMError) and not isinstance(e, llm.FatalLLMError)
        check(name, is_retryable == retryable, type(e).__name__)
        return
    except Exception as e:                     # 抛了别的类型
        check(name, False, f"抛出了 {type(e).__name__}")
        return
    check(name, False, "没有抛异常")


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b'{"error":"x"}'))


def t_http_classification():
    expect("HTTP 401 认证失败 → 不可重试", http_error(401), llm.FatalLLMError, False)
    expect("HTTP 403 → 不可重试", http_error(403), llm.FatalLLMError, False)
    expect("HTTP 400 参数错误 → 不可重试", http_error(400), llm.FatalLLMError, False)
    expect("HTTP 429 限流 → 可重试", http_error(429), llm.RetryableLLMError, True)
    expect("HTTP 500 → 可重试", http_error(500), llm.RetryableLLMError, True)
    expect("HTTP 503 → 可重试", http_error(503), llm.RetryableLLMError, True)
    expect("网络不可达 → 可重试", urllib.error.URLError("boom"), llm.RetryableLLMError, True)
    expect("连接超时 → 可重试", TimeoutError("slow"), llm.RetryableLLMError, True)


def t_response_shapes():
    # 正常
    out = with_urlopen(fake_urlopen({"choices": [{"message": {"content": '{"a":1}'},
                                                  "finish_reason": "stop"}]}))
    check("正常响应返回内容", out == '{"a":1}')

    # 截断
    try:
        with_urlopen(fake_urlopen({"choices": [{"message": {"content": '{"a":'}, "finish_reason": "length"}]}))
        check("finish_reason=length 抛截断异常", False)
    except llm.TruncatedCompletionError as e:
        check("finish_reason=length 抛截断异常", isinstance(e, llm.RetryableLLMError))

    # 空串（DeepSeek JSON mode 失败形态）
    try:
        with_urlopen(fake_urlopen({"choices": [{"message": {"content": "   "}, "finish_reason": "stop"}]}))
        check("空内容抛 EmptyCompletionError", False)
    except llm.EmptyCompletionError:
        check("空内容抛 EmptyCompletionError", True)

    # 返回体缺 choices
    try:
        with_urlopen(fake_urlopen({"error": "bad"}))
        check("返回体缺 choices → 可重试", False)
    except llm.RetryableLLMError:
        check("返回体缺 choices → 可重试", True)

    # 缺配置 → 不可重试
    saved = llm.config.LLM_API_KEY
    llm.config.LLM_API_KEY = None
    try:
        llm.chat([{"role": "user", "content": "x"}])
        check("缺配置 → FatalLLMError", False)
    except llm.FatalLLMError:
        check("缺配置 → FatalLLMError", True)
    finally:
        llm.config.LLM_API_KEY = saved


def t_request_payload():
    """验证 max_tokens 被显式写入请求体（防静默截断）。"""
    captured = {}

    class Req:
        def __init__(self, url, data=None, headers=None, method=None):
            captured["body"] = json.loads(data.decode("utf-8"))

    original_req = llm.urllib.request.Request
    llm.urllib.request.Request = Req
    original_open = llm.urllib.request.urlopen
    llm.urllib.request.urlopen = fake_urlopen({"choices": [{"message": {"content": "{}"},
                                                             "finish_reason": "stop"}]})
    try:
        llm.chat([{"role": "user", "content": "x"}], max_tokens=1234)
        check("请求体带显式 max_tokens", captured["body"].get("max_tokens") == 1234,
              str(captured["body"].get("max_tokens")))
        llm.chat([{"role": "user", "content": "x"}])
        check("未指定时用默认 max_tokens", captured["body"].get("max_tokens") == llm.DEFAULT_MAX_TOKENS)
    finally:
        llm.urllib.request.Request = original_req
        llm.urllib.request.urlopen = original_open


def main():
    for fn in (t_http_classification, t_response_shapes, t_request_payload):
        print(f"\n--- {fn.__name__} ---")
        fn()
    print(f"\n===== {len(PASSED)} passed / {len(FAILED)} failed =====")
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
