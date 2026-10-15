# -*- coding: utf-8 -*-
"""本地链路追踪 —— 逐节点把「图跑到哪一步、花了多久」落进 ``trace_span``。

设计约束（都是硬的）
    1. **默认关闭**：``trace_enabled``（app_setting）/ ``TRACE_ENABLED``（.env），
       默认 false。关闭时装饰器直接透传，几乎零开销。
    2. **绝不落原文**：只写规范化后的 **sha256 结构指纹** + 结构字段投影。
       聊天正文、facts 文本、客户姓名一律不落库（PII + 体积控制）。
    3. **写库失败不得影响主流程**：``_write`` 自己 try/except 吞掉，只 print。

LangSmith 暂不启用：只提供 ``redact_for_langsmith()`` 作为将来上报前的脱敏闸门。
本项目**绝不设置** ``LANGCHAIN_*`` / ``LANGSMITH_*`` 环境变量（默认不外发）。
"""
import functools
import hashlib
import json
import time
import traceback

import config
import db

# 允许写进 trace 的结构字段白名单（不含任何自由文本）
_SAFE_KEYS = ("outcome", "aid", "wb_status", "needs_review", "review_enqueued", "error")


# 进程内开关缓存：`enabled()` 原先**每次调用**都查一次 `app_setting`，
# 而图有 11 个节点、每个节点外层都套一层 `traced` → 开启追踪后凭空多出 11 次 DB 查询。
# 这里缓存 5 秒；设置变更会经 `db.set_setting` 主动调 `invalidate_enabled_cache()` 立即失效。
_ENABLED_CACHE = {"at": 0.0, "val": False}
_ENABLED_TTL = 5.0


def invalidate_enabled_cache() -> None:
    """让开关缓存立即失效（设置变更后调用）。"""
    _ENABLED_CACHE["at"] = 0.0


def enabled() -> bool:
    """是否开启链路追踪。app_setting > .env > 默认(false)。带进程内短缓存。"""
    now = time.time()
    if _ENABLED_CACHE["at"] and (now - _ENABLED_CACHE["at"]) < _ENABLED_TTL:
        return _ENABLED_CACHE["val"]
    try:
        v = db.get_setting("trace_enabled")
        if v is None or v == "":
            v = config.get("TRACE_ENABLED", "false")
        val = str(v).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        val = False
    _ENABLED_CACHE["at"] = now
    _ENABLED_CACHE["val"] = val
    return val


def digest(value) -> str:
    """规范化摘要：排序键 + 不转义非 ASCII + fallback repr → sha256[:16]。

    用「哈希」而非原文，是为了能比对「两次运行这一步的输入是否一致」，
    同时不把聊天内容写进 trace 表。
    """
    try:
        s = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        s = repr(value)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _project(state: dict) -> dict:
    """只投影结构字段（白名单），其余一律丢弃。"""
    if not isinstance(state, dict):
        return {}
    return {k: state.get(k) for k in _SAFE_KEYS if k in state}


def _write(row: dict) -> None:
    try:
        db.execute(
            """INSERT INTO trace_span
                 (run_id, session_id, node, attempt, ok, error_class, elapsed_ms,
                  input_digest, output_digest, prompt_tokens, completion_tokens,
                  total_tokens, detail)
               VALUES (%(run_id)s,%(session_id)s,%(node)s,%(attempt)s,%(ok)s,%(error_class)s,
                       %(elapsed_ms)s,%(input_digest)s,%(output_digest)s,%(prompt_tokens)s,
                       %(completion_tokens)s,%(total_tokens)s,%(detail)s)""", row)
    except Exception:
        # 追踪是「观测」不是「业务」：写不进去只留痕，绝不 re-raise
        traceback.print_exc()


def traced(name: str):
    """节点装饰器：记录耗时 / 成败 / 结构指纹。关闭时零开销透传。"""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(state, *args, **kwargs):
            if not enabled():
                return fn(state, *args, **kwargs)
            t0 = time.time()
            row = {
                "run_id": (state or {}).get("run_id"),
                "session_id": ((state or {}).get("cand") or {}).get("session_id"),
                "node": name,
                "attempt": 1,
                "ok": True,
                "error_class": None,
                "elapsed_ms": None,
                "input_digest": digest(_project(state)),
                "output_digest": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "detail": None,
            }
            try:
                out = fn(state, *args, **kwargs)
                merged = dict(state or {})
                if isinstance(out, dict):
                    merged.update(out)
                row["output_digest"] = digest(_project(merged))
                return out
            except Exception as e:
                row["ok"] = False
                row["error_class"] = type(e).__name__
                raise
            finally:
                row["elapsed_ms"] = int((time.time() - t0) * 1000)
                _write(row)

        return wrapper

    return deco


def redact_for_langsmith(payload: dict) -> dict:
    """将来上报 LangSmith 前的最后一道脱敏闸门：白名单外的键**直接丢弃**。

    白名单里只有结构指标，没有任何文本、姓名、客户标识。
    当前不启用 LangSmith，这个函数先留在这里作为唯一出口。
    """
    allowed = ("node", "elapsed_ms", "ok", "error_class",
               "input_digest", "output_digest", "attempt")
    if not isinstance(payload, dict):
        return {}
    return {k: payload.get(k) for k in allowed if k in payload}
