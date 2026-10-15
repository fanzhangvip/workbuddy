# -*- coding: utf-8 -*-
"""数据库访问层：连接池 + 表初始化 + 便捷查询。

连接串优先取环境变量 ``DATABASE_URL``，其次 ``.env`` 里的 ``DATABASE_URL``，
最后回落到本地独立实例（5433）。
"""
import json
import sys
from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

BACKEND_DIR = Path(__file__).resolve().parent
ROOT = BACKEND_DIR.parent
if str(ROOT) not in sys.path:          # 复用根目录下已验证的 weiban / ww_chatdata 模块
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402

# ⚠️ 回退连接串**不含口令**：口令只能来自 .env / 环境变量（`DATABASE_URL`）。
# 曾把真实口令写死在这里，等于仓库里带着一套可用凭据（部署时会直接沿用弱口令）。
DEFAULT_DSN = "postgresql://postgres@127.0.0.1:5433/weiban_chat"
DSN = config.get("DATABASE_URL") or config.get("PG_DSN") or DEFAULT_DSN

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            DSN, min_size=1, max_size=8, open=True,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
    return _pool


def init_db() -> None:
    """建表（幂等）。"""
    sql = (BACKEND_DIR / "schema.sql").read_text(encoding="utf-8")
    with pool().connection() as conn:
        conn.execute(sql)


@contextmanager
def tx():
    """显式事务（批量写入用，比逐条 autocommit 快得多）。"""
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


def query(sql: str, params=None) -> list:
    with pool().connection() as conn:
        return conn.execute(sql, params or ()).fetchall()


def query_one(sql: str, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=None) -> int:
    with pool().connection() as conn:
        cur = conn.execute(sql, params or ())
        return cur.rowcount


def execute_many(sql: str, seq) -> None:
    with tx() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, seq)


def as_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---------------- 运行时设置（app_setting 表，优先于 .env）----------------

def get_setting(key: str, default=None):
    try:
        row = query_one("SELECT value FROM app_setting WHERE key = %s", (key,))
    except Exception:
        return default
    return row["value"] if row and row.get("value") is not None else default


def set_setting(key: str, value) -> None:
    execute(
        """INSERT INTO app_setting (key, value, updated_at) VALUES (%s, %s, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        (key, None if value is None else str(value)))
    if key == "trace_enabled":
        # trace 的开关做了进程内缓存（避免每节点一次 DB 查询）→ 变更时必须立即失效，
        # 否则「改了设置却不生效」会持续到缓存过期。用 sys.modules 取模块，避开
        # backend/trace.py 与标准库 trace 的重名问题。
        try:
            import sys as _sys
            _m = _sys.modules.get("trace")
            if _m is not None and hasattr(_m, "invalidate_enabled_cache"):
                _m.invalidate_enabled_cache()
        except Exception:
            pass


def all_settings() -> dict:
    try:
        return {r["key"]: r["value"] for r in query("SELECT key, value FROM app_setting")}
    except Exception:
        return {}
