# -*- coding: utf-8 -*-
"""数据库访问层：连接池 + 表初始化 + 便捷查询。

连接串优先取环境变量 ``DATABASE_URL``，其次 ``.env`` 里的 ``DATABASE_URL``，
最后回落到本地独立实例（5433）。
"""
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import atexit

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


def close_pool() -> None:
    """关闭连接池（幂等；退出时自动调用）。

    **为什么必须有它**：不显式关闭的话，解释器退出时 `ConnectionPool` 的
    3 个 worker 线程 + 1 个 scheduler 线程**各要等 5 秒**才被强杀 ——
    每个进程多花 **20 秒**墙钟。实测：一个只做「import db + 一次查询 +
    init_db」的脚本，三步合计 0.34 秒，进程整体却要 20.76 秒；
    加上 `close_pool()` 后整个进程 **0.69 秒**。

    代价落在**测试**这条路上最重：`tests/run_all.py` 每个测试起一个子进程，
    24 个需要数据库的测试 × 20 秒 ≈ **8 分钟纯浪费**（占实测总时长 600s 的 80%）。
    生产侧（uvicorn）在进程退出时才触发，行为不变。
    """
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        except Exception:                      # noqa: BLE001
            pass                               # 退出路径：关不掉也不该抛
        _pool = None


atexit.register(close_pool)


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


def query_int(sql: str, params=None, default: int = 0) -> int:
    """读一个**整数标量**（SQL 里把值 `AS n`）。

    「查一下 → 取数 → 空/炸了都当 0」这个套路原先在 5 处各写一遍
    （`scoring_service._ledger_total` / `_customer_writes_today`、
    `grade_tag` 的配额与水位、`scheduler` 的计数）。写在这里的理由：
    它的**语义**（宁可当 0 也不抛，因为这些值都用在「能不能继续写」的闸门上）
    只该有一处定义，改一次口径要动 5 个文件的写法不该存在。
    """
    try:
        row = query_one(sql, params)
        return int((row or {}).get("n") or 0)
    except Exception:                              # noqa: BLE001
        return default


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


def as_list(value) -> list:
    """把 JSONB 字段归一成 list。

    **为什么需要它**：同一个 JSONB 列读回来可能是 `list`，也可能是 `json 字符串`
    （psycopg 的行为、以及"写入方传了字符串"这两条路径不一致）。这个知识原先在
    5 处各写了一遍（`scoring_service._as_items`、`meta_loop._items_of`、
    `eval_runner._item_map`，外加两处内联），于是同一个坑要记得改 5 次。

    非 list 的输入一律返回 `[]`（**不抛异常**）：调用方几乎都是"遍历渲染/统计"，
    给空列表比让它炸到界面上更好；真出现脏数据时看日志的空结果也能追。
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:                      # noqa: BLE001  脏数据 → 空列表，不抛
            return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def as_dict_list(value) -> list:
    """`as_list` 的加强版：只保留 dict 元素 —— 渲染/统计代码普遍假设元素是对象。"""
    return [x for x in as_list(value) if isinstance(x, dict)]


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


def setting(key: str, env_key: str, default=None):
    """运行时设置：``app_setting`` 优先，其次环境变量 / ``.env``，最后 ``default``。

    空串等同于「没设置」（前端把输入框清空保存时写的就是空串）。
    """
    v = get_setting(key)
    if v is None or v == "":
        v = config.get(env_key)
    return default if v is None or v == "" else v


def _setting_cast(key: str, env_key: str, default, cast):
    """读一个设置并转型；**读不到或转不动都退回 default**。

    设置读失败不该让主流程炸 —— 它几乎都用在「间隔/阈值/开关」这类参数上。
    """
    try:
        return cast(setting(key, env_key, default))
    except Exception:                              # noqa: BLE001
        return default


def setting_int(key: str, env_key: str, default: int = 0) -> int:
    return _setting_cast(key, env_key, default, int)


def setting_float(key: str, env_key: str, default: float = 0.0) -> float:
    return _setting_cast(key, env_key, default, float)


def setting_bool(key: str, env_key: str, default: bool = False) -> bool:
    return str(setting(key, env_key, str(default))).strip().lower() in (
        "1", "true", "yes", "on")


# ---------------- 「至少一次」投递队列的公共动作 ----------------

#: 允许被 :func:`reclaim_stuck_sending` 引用的队列表（表名要拼进 SQL，故用白名单）
QUEUE_TABLES = frozenset({"writeback_outbox", "grade_tag_outbox"})


def reclaim_stuck_sending(conn, table: str, stuck_seconds: int, note: str,
                          unknown_outcome: bool = False) -> None:
    """把卡在 ``sending`` 超时的行收回成 ``failed``（在**同一个事务**里调用）。

    为什么必须有这一步：进程被 kill 时，已经标成 ``sending`` 的行没人负责 ——
    不回收就**永久卡住**（既不重投、也不算失败），而队列的实现前提是
    「至少一次投递」。改造前 `outbox` 与 `grade_tag` 各写了一份**逐字相同**的
    UPDATE（连 `COALESCE(last_errmsg,'')` 都一样），改一处必忘另一处。

    :param table: 只接受白名单里的队列表（杜绝把表名当参数拼 SQL）。
    :param unknown_outcome: **结果未知**（投递中断：进程被杀 / 网络断）时置
        ``dead`` + ``needs_reconcile=true`` 而**不重投**。只对**不可撤销**的队列表用
        （`writeback_outbox`：微伴评分是累加语义，重投 = 多加一次分）；标签队列
        （`grade_tag_outbox`）是覆盖语义，重投无害，保持 ``failed`` 可重试。
    """
    if table not in QUEUE_TABLES:
        raise ValueError(f"未知队列表：{table}")
    if unknown_outcome:
        conn.execute(
            f"""UPDATE {table}
                   SET status='dead', needs_reconcile=true, updated_at=now(),
                       last_errmsg = COALESCE(last_errmsg, '') || %s
                 WHERE status='sending'
                   AND updated_at < now() - make_interval(secs => %s)""",
            (note, int(stuck_seconds)))
        return
    conn.execute(
        f"""UPDATE {table}
               SET status='failed', next_attempt_at=now(), updated_at=now(),
                   last_errmsg = COALESCE(last_errmsg, '') || %s
             WHERE status='sending'
               AND updated_at < now() - make_interval(secs => %s)""",
        (note, int(stuck_seconds)))
