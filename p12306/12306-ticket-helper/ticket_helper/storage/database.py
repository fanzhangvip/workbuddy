"""SQLite 持久化层（设计文档 4.9 / 第五节）。

设计要点：
- **可回滚迁移**：``schema_migrations`` 记录版本，每次升级在一个事务内完成，
  失败自动回滚，不会留下半截表结构；
- **并发安全**：开启 WAL + ``busy_timeout``，写入统一走内置锁，避免
  「database is locked」把挂机任务打断（ST-T3）；
- **连接不跨线程共享写操作**：读连接每次按需创建（轻量），写操作串行化。
"""

from __future__ import annotations

import importlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from ticket_helper.runtime.paths import get_app_paths

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# 状态值的「词表出处」
#
# ⚠️ 这里存的是**模块路径 + 常量名**，不是状态字面量 —— 这一点是有意的：
# 建表语句原先写 `status TEXT DEFAULT 'submitted'` 这类默认值，等于让 storage
# 层**自己持有一份状态语义**。同一个值于是散在三处（DDL / 应用层缺省 / 词表），
# 改一处另外两处静默不变。更糟的是那个默认值会被
# `tests/test_contracts.py` 的僵尸状态守卫当成「产生点」，
# 把全仓从不赋值的 `submitted` 伪装成有来源（真实事故，见 `STANDBY_STATES`）。
#
# 现在 DDL 不带任何状态默认值（列一律 NOT NULL），写入统一走 `_require_status`
# 到这里取词表校验：既保证「未知状态不得入库」，又保证状态语义只有一份。
# --------------------------------------------------------------------------- #
_STATUS_SOURCES: dict[str, tuple[str, str, str]] = {
    # 表名: (词表所在模块, 全部取值的常量名, 缺省值常量名)
    "users": ("ticket_helper.account.manager", "USER_STATUSES", "STATUS_OFFLINE"),
    "tasks": ("ticket_helper.config.settings", "TASK_STATUSES", "TASK_STATUS_STOPPED"),
    "orders": ("ticket_helper.order.service", "ORDER_STATUSES", "ORDER_SUBMITTED"),
    "standby_orders": ("ticket_helper.standby.engine", "STANDBY_STATES", "STANDBY_PENDING"),
}


def _status_constant(table: str, name: str) -> str:
    """按**常量名**取某张表词表里的一个字面量。

    SQL 里因此不必再写 `status IN ('submitted','queuing')` 这种字面量 ——
    写死的后果是：词表改了，SQL 不会报错，只会**静默地永远匹配不到**。
    """
    module_name = _STATUS_SOURCES[table][0]
    return str(getattr(importlib.import_module(module_name), name))


def _require_status(table: str, value: Any) -> str:
    """把 `status` 规整成「词表里一定有的值」，否则**拒绝入库**。

    为什么要在持久化边界拦：状态值是跨模块契约。写进来的若是拼错或过期的
    字符串，读的一侧**不会报错** —— 过滤条件永远不匹配、看板上少一行、
    配额判断永远算成「还有额度」，全程一个异常都没有。

    `value` 为空时退回该表词表里声明的缺省值（同样是常量，不是字面量）。
    """
    module_name, allowed_name, default_name = _STATUS_SOURCES[table]
    module = importlib.import_module(module_name)
    allowed = tuple(getattr(module, allowed_name))
    default = str(getattr(module, default_name))
    text = default if value is None or str(value) == "" else str(value)
    if text not in allowed:
        raise ValueError(
            f"{table}.status 收到未知状态 {value!r}；合法取值：{', '.join(allowed)}"
        )
    return text

_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_enc TEXT NOT NULL DEFAULT '',
            is_checked INTEGER DEFAULT 0,
            status TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            heartbeat INTEGER DEFAULT 120,
            last_active DATETIME,
            remark TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            left_dates TEXT NOT NULL DEFAULT '[]',
            from_stations TEXT NOT NULL DEFAULT '[]',
            to_stations TEXT NOT NULL DEFAULT '[]',
            train_filter TEXT DEFAULT '{}',
            train_types TEXT DEFAULT '[]',
            seat_types TEXT DEFAULT '[]',
            depart_time_range TEXT DEFAULT '[]',
            passengers TEXT DEFAULT '[]',
            accounts TEXT DEFAULT '[]',
            strategy TEXT DEFAULT 'direct',
            query_interval REAL DEFAULT 1.0,
            sale_open_time TEXT DEFAULT '',
            sale_prepare_seconds INTEGER DEFAULT 30,
            max_retry INTEGER DEFAULT 20,
            priority INTEGER DEFAULT 0,
            remark TEXT DEFAULT '',
            status TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS query_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            query_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            train_no TEXT, seat_type TEXT, remain TEXT,
            latency_ms INTEGER,
            hit INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            order_no TEXT,
            train_no TEXT,
            depart_date TEXT,
            from_station TEXT,
            to_station TEXT,
            seat_type TEXT,
            passenger TEXT,
            amount REAL DEFAULT 0,
            status TEXT NOT NULL,
            message TEXT DEFAULT '',
            pay_url TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT, title TEXT, body TEXT,
            channel TEXT, ok INTEGER DEFAULT 1,
            error TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS config_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT, old_value TEXT, new_value TEXT,
            changed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS config_store (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS standby_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            account TEXT,
            order_no TEXT,
            combos TEXT DEFAULT '[]',
            seat_types TEXT DEFAULT '[]',
            status TEXT NOT NULL,
            result TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, value REAL, tags TEXT DEFAULT '{}',
            recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_query_records_task ON query_records(task_id, query_at);
        CREATE INDEX IF NOT EXISTS idx_orders_task ON orders(task_id);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at);
        CREATE INDEX IF NOT EXISTS idx_standby_task ON standby_orders(task_id);
        """,
    ),
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Database:
    """统一数据访问层。"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else get_app_paths().db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._local = threading.local()

    # ------------------------------------------------------------------ #
    # 连接与迁移
    # ------------------------------------------------------------------ #
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> "Database":
        """建表 / 迁移。幂等，可反复调用。"""
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
            applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, script in _MIGRATIONS:
                if version in applied:
                    continue
                log.info("执行数据库迁移 v%s", version)
                try:
                    conn.executescript("BEGIN;" + script + ";")
                    conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
                    conn.commit()
                except sqlite3.Error:
                    conn.rollback()
                    log.exception("数据库迁移 v%s 失败，已回滚", version)
                    raise
        return self

    # ------------------------------------------------------------------ #
    # 通用
    # ------------------------------------------------------------------ #
    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._write_lock, self.connect() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.rowcount

    def execute_returning_id(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._write_lock, self.connect() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return int(cursor.lastrowid or 0)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            return [dict(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> int:
        with self._write_lock, self.connect() as conn:
            cursor = conn.executemany(sql, list(seq))
            conn.commit()
            return cursor.rowcount

    def transaction(self):
        """上下文管理器：多步写入事务。"""
        return _Transaction(self)

    # ------------------------------------------------------------------ #
    # 账号
    # ------------------------------------------------------------------ #
    def save_user(self, username: str, password_enc: str, **kwargs: Any) -> int:
        existing = self.query_one("SELECT id FROM users WHERE username = ?", (username,))
        now = _now()
        if existing:
            fields = {"password_enc": password_enc, "last_active": now}
            for key in ("is_checked", "status", "enabled", "heartbeat", "remark"):
                if key in kwargs:
                    fields[key] = kwargs[key]
            assignments = ", ".join(f"{k} = ?" for k in fields)
            self.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*fields.values(), existing["id"]))
            return int(existing["id"])
        return self.execute_returning_id(
            "INSERT INTO users(username, password_enc, is_checked, status, enabled, heartbeat, remark, last_active)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                username,
                password_enc,
                int(kwargs.get("is_checked", 0)),
                _require_status("users", kwargs.get("status")),
                int(kwargs.get("enabled", 1)),
                int(kwargs.get("heartbeat", 120)),
                kwargs.get("remark", ""),
                now,
            ),
        )

    def list_users(self, *, with_secret: bool = False) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM users ORDER BY id")
        for row in rows:
            row["enabled"] = bool(row.get("enabled"))
            row["is_checked"] = bool(row.get("is_checked"))
            if not with_secret:
                row.pop("password_enc", None)
        return rows

    def get_user(self, user_id: int, *, with_secret: bool = False) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row and not with_secret:
            row.pop("password_enc", None)
        return row

    def get_user_by_name(self, username: str, *, with_secret: bool = False) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM users WHERE username = ?", (username,))
        if row and not with_secret:
            row.pop("password_enc", None)
        return row

    def update_user_state(self, user_id: int, **fields: Any) -> int:
        allowed = {"status", "is_checked", "enabled", "last_active", "password_enc", "heartbeat", "remark"}
        payload = {k: v for k, v in fields.items() if k in allowed}
        if not payload:
            return 0
        assignments = ", ".join(f"{k} = ?" for k in payload)
        return self.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*payload.values(), user_id))

    def delete_user(self, user_id: int) -> int:
        return self.execute("DELETE FROM users WHERE id = ?", (user_id,))

    # ------------------------------------------------------------------ #
    # 任务
    # ------------------------------------------------------------------ #
    TASK_FIELDS = (
        "name", "left_dates", "from_stations", "to_stations", "train_filter", "train_types",
        "seat_types", "depart_time_range", "passengers", "accounts", "strategy",
        "query_interval", "sale_open_time", "sale_prepare_seconds", "max_retry",
        "priority", "remark", "status", "enabled",
    )

    def save_task(self, data: dict[str, Any], task_id: int | None = None) -> int:
        payload = dict(data)
        for key in (
            "left_dates", "from_stations", "to_stations", "train_filter", "train_types",
            "seat_types", "depart_time_range", "passengers", "accounts",
        ):
            if key in payload and not isinstance(payload[key], str):
                payload[key] = _dumps(payload[key])
        if "enabled" in payload:
            payload["enabled"] = int(bool(payload["enabled"]))
        if "status" in payload:
            payload["status"] = _require_status("tasks", payload["status"])
        payload["updated_at"] = _now()

        fields = [f for f in self.TASK_FIELDS if f in payload]
        if task_id:
            assignments = ", ".join(f"{f} = ?" for f in fields)
            self.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?",
                (*[payload[f] for f in fields], task_id),
            )
            return task_id
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        return self.execute_returning_id(
            f"INSERT INTO tasks({columns}) VALUES ({placeholders})",
            [payload[f] for f in fields],
        )

    def _hydrate_task(self, row: dict[str, Any]) -> dict[str, Any]:
        for key in (
            "left_dates", "from_stations", "to_stations", "train_types",
            "seat_types", "depart_time_range", "passengers", "accounts",
        ):
            row[key] = _loads(row.get(key), [])
        row["train_filter"] = _loads(row.get("train_filter"), {})
        row["enabled"] = bool(row.get("enabled"))
        return row

    def list_tasks(self) -> list[dict[str, Any]]:
        return [self._hydrate_task(r) for r in self.query("SELECT * FROM tasks ORDER BY priority DESC, id")]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return self._hydrate_task(row) if row else None

    def update_task_status(self, task_id: int, status: str) -> int:
        return self.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), task_id))

    def delete_task(self, task_id: int) -> int:
        return self.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    # ------------------------------------------------------------------ #
    # 余票记录
    # ------------------------------------------------------------------ #
    def save_query_records(self, task_id: int, records: list[dict[str, Any]]) -> int:
        if not records:
            return 0
        payload = [
            (
                task_id,
                _now(),
                r.get("train_no", ""),
                r.get("seat_type", ""),
                str(r.get("remain", "")),
                int(r.get("latency_ms", 0)),
                int(bool(r.get("hit"))),
            )
            for r in records
        ]
        return self.executemany(
            "INSERT INTO query_records(task_id, query_at, train_no, seat_type, remain, latency_ms, hit)"
            " VALUES (?,?,?,?,?,?,?)",
            payload,
        )

    def query_records(self, task_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if task_id:
            return self.query(
                "SELECT * FROM query_records WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, limit)
            )
        return self.query("SELECT * FROM query_records ORDER BY id DESC LIMIT ?", (limit,))

    def prune_query_records(self, keep_days: int) -> int:
        cutoff = (datetime.now() - timedelta(days=max(1, keep_days))).strftime("%Y-%m-%d %H:%M:%S")
        return self.execute("DELETE FROM query_records WHERE query_at < ?", (cutoff,))

    # ------------------------------------------------------------------ #
    # 订单
    # ------------------------------------------------------------------ #
    def save_order(self, data: dict[str, Any]) -> int:
        order_no = data.get("order_no") or ""
        existing = None
        if order_no:
            existing = self.query_one("SELECT id FROM orders WHERE order_no = ?", (order_no,))
        payload = {
            "task_id": data.get("task_id"),
            "order_no": order_no,
            "train_no": data.get("train_no", ""),
            "depart_date": data.get("depart_date", ""),
            "from_station": data.get("from_station", ""),
            "to_station": data.get("to_station", ""),
            "seat_type": data.get("seat_type", ""),
            "passenger": data.get("passenger", ""),
            "amount": float(data.get("amount", 0) or 0),
            "status": _require_status("orders", data.get("status")),
            "message": data.get("message", ""),
            "pay_url": data.get("pay_url", ""),
        }
        if existing:
            assignments = ", ".join(f"{k} = ?" for k in payload if k != "order_no")
            values = [v for k, v in payload.items() if k != "order_no"]
            self.execute(
                f"UPDATE orders SET {assignments}, updated_at = ? WHERE id = ?",
                (*values, _now(), existing["id"]),
            )
            return int(existing["id"])
        columns = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        return self.execute_returning_id(
            f"INSERT INTO orders({columns}) VALUES ({placeholders})", list(payload.values())
        )

    def list_orders(self, status: str | None = None, task_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self.query(f"SELECT * FROM orders {where} ORDER BY id DESC LIMIT ?", params)

    # ------------------------------------------------------------------ #
    # 通知记录
    # ------------------------------------------------------------------ #
    def save_notification(self, event_type: str, title: str, body: str, channel: str, ok: bool, error: str = "") -> int:
        return self.execute_returning_id(
            "INSERT INTO notifications(event_type, title, body, channel, ok, error) VALUES (?,?,?,?,?,?)",
            (event_type, title, body, channel, int(bool(ok)), error),
        )

    def list_notifications(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["ok"] = bool(row.get("ok"))
        return rows

    # ------------------------------------------------------------------ #
    # 候补
    # ------------------------------------------------------------------ #
    def save_standby_order(self, data: dict[str, Any]) -> int:
        payload = {
            "task_id": data.get("task_id"),
            "account": data.get("account", ""),
            "order_no": data.get("order_no", ""),
            "combos": _dumps(data.get("combos", [])),
            "seat_types": _dumps(data.get("seat_types", [])),
            "status": _require_status("standby_orders", data.get("status")),
            "result": data.get("result", ""),
            "updated_at": _now(),
        }
        existing = None
        if payload["order_no"]:
            existing = self.query_one("SELECT id FROM standby_orders WHERE order_no = ?", (payload["order_no"],))
        elif payload["task_id"]:
            existing = self.query_one(
                "SELECT id FROM standby_orders WHERE task_id = ? AND status = ?"
                " ORDER BY id DESC LIMIT 1",
                (payload["task_id"], _status_constant("standby_orders", "STANDBY_QUEUING")),
            )
        if existing:
            assignments = ", ".join(f"{k} = ?" for k in payload)
            self.execute(
                f"UPDATE standby_orders SET {assignments} WHERE id = ?",
                (*payload.values(), existing["id"]),
            )
            return int(existing["id"])
        columns = ", ".join(payload)
        return self.execute_returning_id(
            f"INSERT INTO standby_orders({columns}) VALUES ({', '.join('?' for _ in payload)})",
            list(payload.values()),
        )

    def list_standby_orders(self, task_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.query(f"SELECT * FROM standby_orders {where} ORDER BY id DESC", params)
        for row in rows:
            row["combos"] = _loads(row.get("combos"), [])
            row["seat_types"] = _loads(row.get("seat_types"), [])
        return rows

    def count_active_standby(self) -> int:
        row = self.query_one(
            "SELECT COUNT(DISTINCT COALESCE(NULLIF(order_no,''), CAST(id AS TEXT))) AS c"
            " FROM standby_orders WHERE status = ?",
            (_status_constant("standby_orders", "STANDBY_QUEUING"),),
        )
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------ #
    # 配置存储 / 状态 / 指标
    # ------------------------------------------------------------------ #
    def set_config_blob(self, key: str, value: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO config_store(key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, _dumps(value), _now()),
        )

    def get_config_blob(self, key: str) -> dict[str, Any] | None:
        row = self.query_one("SELECT value FROM config_store WHERE key = ?", (key,))
        return _loads(row["value"], {}) if row else None

    def log_config_change(self, key: str, old_value: Any, new_value: Any) -> int:
        return self.execute_returning_id(
            "INSERT INTO config_history(key, old_value, new_value) VALUES (?,?,?)",
            (key, _dumps(old_value), _dumps(new_value)),
        )

    def list_config_history(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM config_history ORDER BY id DESC LIMIT ?", (limit,))

    def set_state(self, key: str, value: Any) -> None:
        """保存运行期状态（用于 LG-02 重启恢复）。"""
        self.execute(
            "INSERT INTO app_state(key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, _dumps(value), _now()),
        )

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM app_state WHERE key = ?", (key,))
        return _loads(row["value"], default) if row else default

    def delete_state(self, key: str) -> int:
        return self.execute("DELETE FROM app_state WHERE key = ?", (key,))

    def record_metric(self, name: str, value: float, tags: dict[str, Any] | None = None) -> int:
        return self.execute_returning_id(
            "INSERT INTO metrics(name, value, tags) VALUES (?,?,?)",
            (name, float(value), _dumps(tags or {})),
        )

    def stats(self) -> dict[str, Any]:
        """看板指标（WB-04 / LG-04）。"""
        total = self.query_one("SELECT COUNT(*) AS c FROM query_records") or {"c": 0}
        today = self.query_one(
            "SELECT COUNT(*) AS c FROM query_records WHERE date(query_at) = date('now','localtime')"
        ) or {"c": 0}
        hits = self.query_one("SELECT COUNT(*) AS c FROM query_records WHERE hit = 1") or {"c": 0}
        latency = self.query_one(
            "SELECT AVG(latency_ms) AS a, MAX(latency_ms) AS m FROM query_records WHERE latency_ms > 0"
        ) or {"a": 0, "m": 0}
        orders = self.query("SELECT status, COUNT(*) AS c FROM orders GROUP BY status")
        tasks = self.query("SELECT status, COUNT(*) AS c FROM tasks GROUP BY status")
        users = self.query("SELECT status, COUNT(*) AS c FROM users GROUP BY status")
        return {
            "queries_total": int(total["c"]),
            "queries_today": int(today["c"]),
            "hits_total": int(hits["c"]),
            "latency_avg_ms": round(float(latency["a"] or 0), 1),
            "latency_max_ms": int(latency["m"] or 0),
            "success_rate": round(int(hits["c"]) / int(total["c"]) * 100, 2) if int(total["c"]) else 0.0,
            "orders_by_status": orders,
            "tasks_by_status": tasks,
            "users_by_status": users,
        }

    def vacuum(self) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute("VACUUM")

    def table_names(self) -> list[str]:
        rows = self.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [r["name"] for r in rows]


class _Transaction:
    """多步写入事务上下文。"""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.db._write_lock.acquire()  # noqa: SLF001 - 同一把写锁保证串行
        self.conn = self.db.connect()
        self.conn.execute("BEGIN")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        assert self.conn is not None
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
            self.db._write_lock.release()  # noqa: SLF001
        return False


# --------------------------------------------------------------------------- #
_db: Database | None = None
_db_lock = threading.Lock()


def get_database(db_path: Path | str | None = None, *, refresh: bool = False) -> Database:
    """获取全局数据库实例（单例）。"""
    global _db
    with _db_lock:
        if _db is None or refresh or db_path is not None:
            _db = Database(db_path).init()
        return _db
