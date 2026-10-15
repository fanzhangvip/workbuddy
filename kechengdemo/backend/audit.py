# -*- coding: utf-8 -*-
"""工具调用审计（五问）+ 参数脱敏 + JSONL 留存导出。修硬伤 5。

## 解决什么

改造前只有 `audit_log` 一张「业务动作」审计表，且**工具参数不落库** →
文章 B 的审计五问（谁做的 / 做了什么 / 什么时候 / 为什么 / 结果是什么）里
「为什么」根本答不了。出事后只能看到「调用成功」四个字。

本模块补上**工具调用**这一层，与 `audit_log` 分工明确：

  · `audit_log`  —— 业务动作（提交复核、发布规则、改配置）
  · `tool_audit` —— 工具调用（调了谁、传什么参数、谁批的、耗时多少、结果如何）

## 两条硬纪律

1. **参数必须脱敏后才能落库**：`access_token` 会出现在微伴接口的 query 里，
   密钥会出现在 headers 里。不脱敏的话，「用来保护安全的日志」本身就成了新的泄露源。
2. **响应正文不落库，只存指纹**：聊天正文是 PII 且体积不可控；存 sha256 前 16 位
   既能比对「两次响应是否一致」，又不落内容。

## JSONL 导出的取舍

PG 表供在线查询，JSONL 供**合规留存**：追加写、中断最多丢一行、可按天轮转、可离线校验。
两者关系：**PG 是权威，JSONL 是副本**。
"""
import hashlib
import json
import re
import threading
import time
from datetime import date as _date
from datetime import datetime, timezone
from pathlib import Path

import db
import security

ROOT = Path(__file__).resolve().parent.parent
EXPORT_DIR = ROOT / "backups" / "audit-jsonl"

#: 单文件上限（文章 B 的经验值 100MB）。超过则当天的文件加序号后缀。
MAX_BYTES = 100 * 1024 * 1024

#: 命中这些键名的值一律替换成 ***（大小写不敏感）
_SECRET_KEYS = re.compile(
    r"(token|secret|key|password|passwd|pwd|authorization|cookie|open_sid|sid|credential)",
    re.I)

#: 字符串里内联的敏感键值对（如 URL query `?access_token=abc`）
_INLINE_SECRET = re.compile(
    r"((?:access_token|access-key|secret|secret_key|api_?key|password|token)"
    r"\s*[=:]\s*)([^\s&\"',;}]+)", re.I)

REDACTED = "***"
MAX_STR = 300
_LOCK = threading.Lock()


def redact(value, _depth: int = 0):
    """递归脱敏：命中敏感键名的值替换成 ``***``，并清掉字符串里内联的密钥。

    ``_depth`` 是防御性参数：**外部传入的对象可能自引用**（环形结构），
    无限递归会栈溢出。超过 6 层就不再深入。
    """
    if _depth > 6:
        return "<max-depth>"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _SECRET_KEYS.search(str(k)):
                out[str(k)] = REDACTED
            else:
                out[str(k)] = redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str):
        s = _INLINE_SECRET.sub(lambda m: m.group(1) + REDACTED, value)
        return s if len(s) <= MAX_STR else s[:MAX_STR] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value), _depth + 1)


def digest(value) -> str:
    """结构化摘要（sha256 前 16 位）。用于比对，不落原文。"""
    try:
        s = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        s = repr(value)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_tool_call(*, tool: str, params=None, actor_id: str | None = None,
                     task_id: int | None = None, step_id: int | None = None,
                     decision: str = "allow", reason: str | None = None,
                     approval_source: str | None = None,
                     idempotency_key: str | None = None,
                     result_status: str | None = None, errcode: int | None = None,
                     errmsg: str | None = None, response=None,
                     elapsed_ms: int | None = None) -> int | None:
    """写一条工具调用审计。**失败绝不抛出** —— 审计是旁路观测。

    ``actor_id`` 缺省时由 :func:`security.actor` 推导（不采信请求体）。
    返回 ``call_id``；写库失败返回 ``None``（打印一行，不阻断主流程）。

    幂等：``idempotency_key`` 上有唯一约束（三层幂等的第一层），
    重复 key 走 ``ON CONFLICT DO NOTHING`` 并取回已有行的 id —— **不新增行**。
    """
    who = actor_id or security.actor()
    row = {
        "tool_name": tool,
        "actor_id": who,
        "task_id": task_id,
        "step_id": step_id,
        "params_redacted": json.dumps(redact(params or {}), ensure_ascii=False),
        "decision": decision,
        "decision_reason": reason,
        "approval_source": approval_source,
        "idempotency_key": idempotency_key,
        "result_status": result_status,
        "errcode": errcode,
        "errmsg": (str(errmsg)[:500] if errmsg else None),
        "response_digest": digest(response) if response is not None else None,
        "elapsed_ms": elapsed_ms,
    }
    try:
        got = db.query_one(
            """INSERT INTO tool_audit
                 (tool_name, actor_id, task_id, step_id, params_redacted, decision,
                  decision_reason, approval_source, idempotency_key, result_status,
                  errcode, errmsg, response_digest, elapsed_ms)
               VALUES (%(tool_name)s, %(actor_id)s, %(task_id)s, %(step_id)s,
                       %(params_redacted)s::jsonb, %(decision)s, %(decision_reason)s,
                       %(approval_source)s, %(idempotency_key)s, %(result_status)s,
                       %(errcode)s, %(errmsg)s, %(response_digest)s, %(elapsed_ms)s)
               ON CONFLICT (idempotency_key) DO NOTHING
               RETURNING call_id""", row)
        if got is None and idempotency_key:
            old = db.query_one(
                "SELECT call_id FROM tool_audit WHERE idempotency_key=%s",
                (idempotency_key,))
            return (old or {}).get("call_id")
        if got:
            _append_jsonl(_date.today(), {**row, "call_id": got["call_id"],
                                          "created_at": _now_iso()})
        return (got or {}).get("call_id")
    except Exception as e:                      # noqa: BLE001  审计失败不得影响主流程
        print(f"[audit] 写 tool_audit 失败（不影响主流程）：{type(e).__name__}: {e}")
        return None


class tool_call:
    """上下文管理器：包住一次外部调用，自动记「参数 → 结果 + 耗时」。

    用法::

        with audit.tool_call("weiban.add_customer_score", {"score": 3}) as rec:
            resp = weiban.add_customer_score(...)
            rec(resp, ok=resp.get("errcode") == 0,
                errcode=resp.get("errcode"), errmsg=resp.get("errmsg"))

    为什么是上下文管理器而不是装饰器：调用点需要**自己决定**什么算成功
    （微伴约定 ``errcode == 0``、LLM 约定"有内容"），装饰器猜不到这个判据。
    """

    def __init__(self, tool, params=None, *, task_id=None, step_id=None,
                 decision="allow", reason=None, idempotency_key=None,
                 actor_id=None):
        self.tool = tool
        self.params = params
        self.task_id = task_id
        self.step_id = step_id
        self.decision = decision
        self.reason = reason
        self.idempotency_key = idempotency_key
        self.actor_id = actor_id
        self.call_id = None
        self._t0 = None
        self._result = None

    def __enter__(self):
        self._t0 = time.time()
        return self

    def __call__(self, response=None, *, ok=None, errcode=None, errmsg=None,
                 status=None):
        """登记本次调用的结果（可调用一次；不调则在 ``__exit__`` 里按异常判定）。"""
        self._result = {"response": response, "ok": ok, "errcode": errcode,
                        "errmsg": errmsg, "status": status}

    def __exit__(self, exc_type, exc, tb):
        r = self._result or {}
        if r.get("status"):
            status = r["status"]
        elif exc_type is not None or r.get("ok") is False:
            status = "failed"
        else:
            status = "ok"
        errmsg = r.get("errmsg")
        if errmsg is None and exc is not None:
            errmsg = f"{exc_type.__name__}: {exc}"
        self.call_id = record_tool_call(
            tool=self.tool, params=self.params, actor_id=self.actor_id,
            task_id=self.task_id, step_id=self.step_id, decision=self.decision,
            reason=self.reason, result_status=status, errcode=r.get("errcode"),
            errmsg=errmsg, response=r.get("response"),
            idempotency_key=self.idempotency_key,
            elapsed_ms=int((time.time() - (self._t0 or time.time())) * 1000))
        return False                            # 不吞异常


def _jsonl_path(day: _date) -> Path:
    return EXPORT_DIR / f"tool_audit-{day.isoformat()}.jsonl"


def _append_jsonl(day: _date, record: dict) -> None:
    """追加一行 JSONL。失败只打印（PG 已是权威，JSONL 是副本）。"""
    try:
        with _LOCK:
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            path = _jsonl_path(day)
            if path.exists() and path.stat().st_size > MAX_BYTES:
                i = 1
                while True:
                    alt = EXPORT_DIR / f"tool_audit-{day.isoformat()}.{i}.jsonl"
                    if not alt.exists() or alt.stat().st_size <= MAX_BYTES:
                        path = alt
                        break
                    i += 1
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:                      # noqa: BLE001
        print(f"[audit] 写 JSONL 失败（PG 已落库，仅副本缺失）：{type(e).__name__}: {e}")


def export_day(day=None, limit: int = 0) -> dict:
    """把某天（默认今天）的 tool_audit 导出成 JSONL，返回统计。

    **只导出、不删除**：删除属于保留期策略，与导出解耦（避免"导出失败却已删库"）。
    """
    d = day or _date.today()
    if isinstance(d, str):
        d = _date.fromisoformat(d)
    sql = ("SELECT call_id, created_at, actor_id, tool_name, params_redacted, decision,"
           "       decision_reason, approval_source, idempotency_key, result_status,"
           "       errcode, errmsg, response_digest, elapsed_ms, task_id, step_id"
           "  FROM tool_audit WHERE created_at::date = %s ORDER BY call_id")
    params = (d,)
    if limit:
        sql += " LIMIT %s"
        params = (d, int(limit))
    rows = db.query(sql, params)
    path = _jsonl_path(d)
    try:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    except Exception as e:                      # noqa: BLE001
        return {"ok": False, "day": d.isoformat(), "rows": len(rows),
                "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "day": d.isoformat(), "rows": len(rows), "path": str(path),
            "bytes": path.stat().st_size if path.exists() else 0}


def verify_export(day=None, path: str | None = None) -> dict:
    """自检：导出文件里**不应出现任何密钥明文**（脱敏的回归防线）。

    返回 ``{"ok": bool, "hits": [...]}``，`hits` 列出疑似泄漏的行号与片段。
    """
    d = day or _date.today()
    if isinstance(d, str):
        d = _date.fromisoformat(d)
    p = Path(path) if path else _jsonl_path(d)
    if not p.exists():
        return {"ok": True, "path": str(p), "rows": 0, "hits": [], "note": "文件不存在"}
    hits = []
    rows = 0
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        rows += 1
        for m in _INLINE_SECRET.finditer(line):
            if m.group(2) != REDACTED:
                hits.append({"line": i, "fragment": m.group(0)[:120]})
    return {"ok": not hits, "path": str(p), "rows": rows, "hits": hits[:10]}


def stale_exports(days: int) -> list:
    """列出超过 ``days`` 天的导出文件（供保留期策略决定是否清理）。"""
    if days <= 0 or not EXPORT_DIR.exists():
        return []
    cutoff = time.time() - int(days) * 86400
    return [str(p) for p in sorted(EXPORT_DIR.glob("tool_audit-*.jsonl"))
            if p.stat().st_mtime < cutoff]


def stats() -> dict:
    """审计观测（供前端 / 排障）。"""
    row = db.query_one(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE decision <> 'allow') AS not_allowed,
                  count(*) FILTER (WHERE result_status = 'failed') AS failed,
                  count(*) FILTER (WHERE created_at::date = current_date) AS today
             FROM tool_audit""") or {}
    top = db.query(
        """SELECT tool_name, count(*) AS n FROM tool_audit
            GROUP BY 1 ORDER BY 2 DESC LIMIT 10""")
    return {"counts": row, "top_tools": top, "export_dir": str(EXPORT_DIR),
            "retention_days": tier_days("audit"),
            "jsonl_files": (len(list(EXPORT_DIR.glob("*.jsonl")))
                            if EXPORT_DIR.exists() else 0)}


#: 分层保留期（ADR-009）。**0 = 不清理** —— 保守默认，等业务拍板。
_TIER_DEFAULTS = {
    "observe": 0,      # 观测：scoring_run / trace_span / session_failure —— 建议 30 天
    "idempotency": 7,  # 幂等：request_guard —— 覆盖任何重试窗口即可
    "audit": 0,        # 审计：audit_log / tool_audit —— 建议 180 天（合规）
    "task": 0,         # 任务：task / task_step —— 建议 90 天
}

_TIER_KEYS = {
    "observe": ("retention_days_observe", "RETENTION_DAYS_OBSERVE"),
    "idempotency": ("retention_days_idempotency", "RETENTION_DAYS_IDEMPOTENCY"),
    "audit": ("retention_days_audit", "RETENTION_DAYS_AUDIT"),
    "task": ("retention_days_task", "RETENTION_DAYS_TASK"),
}


def tier_days(tier: str) -> int:
    """读某档保留天数（app_setting > .env > 默认）。0 = 不清理。"""
    import scoring_service as ss                       # 延迟导入避免循环依赖
    key, env = _TIER_KEYS[tier]
    return ss.setting_int(key, env, _TIER_DEFAULTS[tier])


def tiers() -> dict:
    """四档保留期一览（含默认值与「0 = 不清理」的语义）。"""
    return {t: {"days": tier_days(t), "enabled": tier_days(t) > 0,
                "default": _TIER_DEFAULTS[t]} for t in _TIER_DEFAULTS}
