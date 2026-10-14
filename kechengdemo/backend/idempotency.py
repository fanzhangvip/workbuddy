# -*- coding: utf-8 -*-
"""请求幂等（架构 §2.1 H2「关键操作写 request_key 幂等」）。

## 解决什么

手动端点是「点一次执行一次」的。但真实使用中会出现：
· 用户手抖点了两次「开始评分」→ 起了两轮批量（互斥守卫能拦住第二轮，但那是**副作用之后**的拦截）
· 前端超时重试 / 网关重试 → 同一个操作被投递两次
· 切 `live` 这类**有外部副作用**的操作被执行两次

互斥守卫（`guard_can_start`）解决的是「并发冲突」；幂等解决的是「同一请求重复到达」。
两者**互补且都要有**：守卫管「不能同时跑两个」，幂等管「同一个不要跑两遍」。

## 语义

· 调用方提供 ``request_key``（前端生成 UUID，或由参数指纹生成）
· 首次调用：执行 → 把结果落库 → 返回
· 重复调用（**同一个 key**）：**不执行**，直接返回第一次的结果，并标记 ``replayed=True``
· ``request_key`` 为空 → 不做幂等（保持向后兼容；不是所有调用方都有 key）

## 与 audit_log 的关系

审计回答「发生了什么」，幂等表回答「这个请求是不是已经处理过了」。
两者都写：审计是给人看的，幂等表是给程序判断的。
"""
import time

import db

#: 幂等记录保留时长（秒）。7 天足够覆盖任何「重试窗口」，又不会无限增长。
RETENTION_SECONDS = 7 * 24 * 3600


def _ensure_table() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS request_guard (
                    request_key TEXT PRIMARY KEY,
                    endpoint    TEXT NOT NULL,
                    response    JSONB,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                  )""")


def make_key(*parts) -> str:
    """由参数生成稳定的 request_key（调用方没给 key 时用）。

    用 sha1 而不是内置 ``hash()``：后者在同一进程内才有意义（PYTHONHASHSEED 随机化），
    跨进程/重启会变，而幂等必须跨重启有效。
    """
    import hashlib
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def run_once(request_key: str, endpoint: str, fn, *, ttl_seconds: int = None):
    """执行 ``fn``，同一 ``request_key`` 只真正执行一次。

    返回 ``fn()`` 的结果；重复请求返回首次结果并附加 ``_replayed=True``。

    ⚠️ **判重与写入之间的竞态**：两个请求同时到达时可能都判定为「未处理过」。
    这里用 ``INSERT ... ON CONFLICT DO NOTHING`` 做**原子占位**：
    谁先插入成功谁执行，另一个拿到「已存在」→ 等待并复用结果。
    这比「先查后插」可靠（后者有经典 TOCTOU 窗口）。
    """
    if not request_key:
        return fn()

    _ensure_table()
    ttl = RETENTION_SECONDS if ttl_seconds is None else int(ttl_seconds)

    # ① 原子占位：插入成功 = 我是第一个
    got = db.execute(
        """INSERT INTO request_guard (request_key, endpoint, response)
           VALUES (%s, %s, NULL) ON CONFLICT (request_key) DO NOTHING""",
        (request_key, endpoint))
    first = bool(got)

    if not first:
        # ② 已有记录：复用（可能还在执行中 → 短暂等待）
        for _ in range(20):
            row = db.query_one("SELECT response FROM request_guard WHERE request_key=%s",
                               (request_key,))
            if row is None:
                break
            if row.get("response") is not None:
                res = row["response"]
                if isinstance(res, dict):
                    return {**res, "_replayed": True}
                return res
            time.sleep(0.25)                    # 首次调用还没写完结果
        # 超时仍未拿到结果：放行执行（宁可重复，也不要让用户等到失败）
        return fn()

    # ③ 首次：执行并落结果
    try:
        result = fn()
    except Exception as e:
        # 失败**不留幂等记录** —— 否则用户重试会被「重放」成同样的失败，
        # 明明一次重试就能成功的事被永久钉死。
        db.execute("DELETE FROM request_guard WHERE request_key=%s", (request_key,))
        raise
    try:
        db.execute("UPDATE request_guard SET response=%s WHERE request_key=%s",
                   (db.as_json(result) if isinstance(result, (dict, list)) else None,
                    request_key))
    except Exception as e:                      # noqa: BLE001
        print(f"[idempotency] 写幂等结果失败（不影响本次执行）：{e}")
    return result


def reap(ttl_seconds: int = None) -> int:
    """清理过期幂等记录（由定时任务调用）。"""
    _ensure_table()
    ttl = RETENTION_SECONDS if ttl_seconds is None else int(ttl_seconds)
    return db.execute(
        "DELETE FROM request_guard WHERE created_at < now() - (%s || ' seconds')::interval",
        (str(int(ttl)),))


def stats() -> dict:
    _ensure_table()
    row = db.query_one("SELECT count(*) AS n FROM request_guard")
    return {"records": (row or {}).get("n", 0), "retention_seconds": RETENTION_SECONDS}
