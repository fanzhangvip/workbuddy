"""数据持久化测试（ST-T1 ~ ST-T4）。"""

from __future__ import annotations

import threading
import time

import pytest

from ticket_helper.account.manager import STATUS_OFFLINE
from ticket_helper.account.manager import STATUS_OFFLINE
from ticket_helper.config.settings import TaskConfig
from ticket_helper.storage.repository import TaskRepository, UserRepository


# --------------------------------------------------------------------------- #
# ST-T1 建表 / 迁移
# --------------------------------------------------------------------------- #
def test_init_creates_all_tables(database):
    tables = set(database.table_names())
    expected = {
        "users",
        "tasks",
        "query_records",
        "orders",
        "notifications",
        "config_history",
        "app_state",
        "config_store",
        "standby_orders",
        "metrics",
        "schema_migrations",
    }
    assert expected.issubset(tables)


def test_init_is_idempotent(database, app_paths):
    from ticket_helper.storage.database import Database

    database.init()
    fresh = Database(app_paths.db_path).init()
    assert set(database.table_names()) == set(fresh.table_names())
    # 迁移记录不重复
    rows = database.query("SELECT COUNT(*) AS c FROM schema_migrations")
    assert rows[0]["c"] >= 1


# --------------------------------------------------------------------------- #
# ST-T2 CRUD
# --------------------------------------------------------------------------- #
def test_user_crud(database, cipher):
    user_id = database.save_user("u1", cipher.encrypt("pw1"), heartbeat=100)
    row = database.get_user(user_id, with_secret=True)
    assert row["username"] == "u1"
    assert cipher.decrypt(row["password_enc"]) == "pw1"

    # 同一用户名再存 = 更新而非插入
    same_id = database.save_user("u1", cipher.encrypt("pw2"))
    assert same_id == user_id
    assert len(database.list_users()) == 1
    assert cipher.decrypt(database.get_user(user_id, with_secret=True)["password_enc"]) == "pw2"

    # 默认不返回密码
    assert "password_enc" not in database.get_user(user_id)

    assert database.update_user_state(user_id, status="online", is_checked=1) == 1
    refreshed = database.get_user(user_id)
    assert refreshed["status"] == "online"
    assert refreshed["is_checked"] == 1

    assert database.delete_user(user_id) == 1
    assert database.get_user(user_id) is None


def test_user_unique_constraint(database, cipher):
    database.save_user("dup", cipher.encrypt("a"))
    database.save_user("dup", cipher.encrypt("b"))
    assert len(database.list_users()) == 1


def test_task_crud_with_json_fields(database):
    repo = TaskRepository(database)
    task = repo.save(
        TaskConfig(
            name="任务A",
            left_dates=["2026-10-01", "2026-10-02"],
            from_stations=["广州南"],
            to_stations=["长沙南"],
            trains_whitelist=["G6102"],
            trains_blacklist=["G0001"],
            seat_types=["二等座", "一等座"],
            passengers=["张三", "李四"],
            strategy="dual",
            query_interval=1.5,
        )
    )
    assert task.id

    loaded = repo.get(task.id)
    assert loaded.left_dates == ["2026-10-01", "2026-10-02"]
    assert loaded.trains_whitelist == ["G6102"]
    assert loaded.trains_blacklist == ["G0001"]
    assert loaded.passengers == ["张三", "李四"]
    assert loaded.strategy == "dual"
    assert loaded.query_interval == 1.5

    loaded.name = "任务A-改名"
    loaded.enabled = False
    repo.save(loaded)
    assert repo.get(task.id).name == "任务A-改名"
    assert repo.get(task.id).enabled is False

    assert repo.delete(task.id) == 1
    assert repo.get(task.id) is None


def test_order_dedup_by_order_no(database):
    first = database.save_order({"task_id": 1, "order_no": "E100", "train_no": "G1", "status": "queued"})
    second = database.save_order({"task_id": 1, "order_no": "E100", "train_no": "G1", "status": "success"})
    assert first == second
    orders = database.list_orders()
    assert len(orders) == 1
    assert orders[0]["status"] == "success"


def test_query_records_and_prune(database):
    database.save_query_records(
        1,
        [
            {"train_no": "G1", "seat_type": "二等座", "remain": "5", "latency_ms": 120, "hit": 1},
            {"train_no": "G1", "seat_type": "一等座", "remain": "无", "latency_ms": 120, "hit": 0},
        ],
    )
    assert len(database.query_records(task_id=1)) == 2

    stats = database.stats()
    assert stats["queries_total"] == 2
    assert stats["hits_total"] == 1
    assert stats["latency_avg_ms"] == 120.0

    # 把记录改成 10 天前，验证按保留期清理
    database.execute("UPDATE query_records SET query_at = '2020-01-01 00:00:00'")
    assert database.prune_query_records(7) == 2
    assert database.query_records(task_id=1) == []


def test_config_blob_and_history(database):
    database.set_config_blob("app_config", {"a": 1})
    database.set_config_blob("app_config", {"a": 2})
    assert database.get_config_blob("app_config") == {"a": 2}

    database.log_config_change("ratelimit", {"min": 1}, {"min": 2})
    history = database.list_config_history()
    assert history[0]["key"] == "ratelimit"
    assert "2" in history[0]["new_value"]


def test_app_state_roundtrip(database):
    database.set_state("task_runtime:1", {"started_at": 123})
    assert database.get_state("task_runtime:1") == {"started_at": 123}
    assert database.get_state("缺失", default="x") == "x"
    database.delete_state("task_runtime:1")
    assert database.get_state("task_runtime:1") is None


def test_standby_order_quota_count(database):
    database.save_standby_order({"task_id": 1, "order_no": "HB1", "status": "queuing"})
    database.save_standby_order({"task_id": 1, "order_no": "HB2", "status": "queuing"})
    database.save_standby_order({"task_id": 2, "order_no": "HB3", "status": "cancelled"})
    assert database.count_active_standby() == 2


# --------------------------------------------------------------------------- #
# ST-T3 并发写
# --------------------------------------------------------------------------- #
def test_concurrent_writes_do_not_corrupt(database):
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            for i in range(15):
                database.save_query_records(
                    index, [{"train_no": f"G{index}", "seat_type": "二等座", "remain": str(i), "latency_ms": 10}]
                )
                database.save_notification("info", f"t{index}-{i}", "body", "web", True)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(database.query_records(limit=1000)) == 90
    assert len(database.list_notifications(limit=1000)) == 90


def test_transaction_commit_and_rollback(database):
    before = len(database.list_users())

    # status 列已**不允许**缺省（DDL 里不再有默认值），所以连裸 SQL 都必须显式给 ——
    # 这正是「未知/缺失状态不得入库」想要的：要么给对，要么当场 IntegrityError，
    # 不要静默落一个谁也没写过的默认值。
    # 正常退出 → 提交
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO users(username, password_enc, status) VALUES ('tx-ok', 'x', ?)",
            (STATUS_OFFLINE,),
        )
    assert len(database.list_users()) == before + 1

    # 抛异常 → 回滚，不应留下半个写入
    try:
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO users(username, password_enc, status) VALUES ('tx-rollback', 'x', ?)",
                (STATUS_OFFLINE,),
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    users = {u["username"] for u in database.list_users()}
    assert "tx-ok" in users
    assert "tx-rollback" not in users


# --------------------------------------------------------------------------- #
# ST-T4 重启数据不丢
# --------------------------------------------------------------------------- #
def test_data_survives_reconnect(app_paths):
    from ticket_helper.storage.database import Database

    db1 = Database(app_paths.db_path).init()
    db1.save_user("persist_user", "enc")
    db1.save_order({"task_id": 9, "order_no": "E999", "status": "success"})

    # 模拟进程重启：全新的 Database 实例指向同一文件
    db2 = Database(app_paths.db_path).init()
    assert db2.get_user_by_name("persist_user") is not None
    assert len(db2.list_orders(status="success")) == 1

    # 文件真实存在且非空
    assert app_paths.db_path.exists()
    assert app_paths.db_path.stat().st_size > 0


def test_unknown_status_is_rejected_at_the_persistence_boundary(database) -> None:
    """★ 防的是：拼错或已过期的状态值**静默入库**。

    状态值是跨模块契约。写进来的字符串若读的一侧不认识，**不会有任何报错** ——
    过滤条件永远不匹配、看板上少一行、候补配额永远算成「还有额度」，
    结果是官方拒单而本地日志一切正常。所以持久化边界必须当场拒绝。

    DDL 里那些 `status TEXT DEFAULT 'x'` 被拿掉之后，这一层校验就成了
    唯一闸门：既拦住未知值，也拦住「漏传」（漏传会撞 NOT NULL）。

    最后一段是**反向对照**：把刚删掉的僵尸值 `"submitted"` 喂给候补表，
    必须被拒 —— 否则说明「删常量」只是删了个名字，值照旧能进库。
    """

    from ticket_helper.account.manager import STATUS_OFFLINE
    from ticket_helper.order.service import ORDER_SUBMITTED
    from ticket_helper.standby.engine import STANDBY_QUEUING, STANDBY_STATES

    # ① 词表里的值：正常入库（顺带证明校验没把合法值也拦掉）
    assert database.save_user("status-ok", "x", status=STATUS_OFFLINE) > 0
    assert database.save_order({"order_no": "ST-1", "status": ORDER_SUBMITTED}) > 0
    assert database.save_standby_order({"order_no": "ST-2", "status": STANDBY_QUEUING}) > 0

    # ② 空值 → 退回词表里声明的缺省值，而不是写个空串进去
    assert database.save_user("status-default", "x") > 0
    saved = database.get_user_by_name("status-default")
    assert saved["status"] == STATUS_OFFLINE, saved

    # ③ 未知值 → 当场拒绝，且错误信息里必须带**合法取值**（否则等于没说）
    for table, call in (
        ("users", lambda: database.save_user("status-bad", "x", status="COMPLETED")),
        ("orders", lambda: database.save_order({"order_no": "ST-3", "status": "COMPLETED"})),
        (
            "standby_orders",
            lambda: database.save_standby_order({"order_no": "ST-4", "status": "COMPLETED"}),
        ),
    ):
        with pytest.raises(ValueError, match=rf"{table}\.status.*合法取值"):
            call()

    # ④ 反向对照：删掉的那个僵尸值不许再能进候补表
    assert "submitted" not in STANDBY_STATES
    with pytest.raises(ValueError, match=r"standby_orders\.status"):
        database.save_standby_order({"order_no": "ST-5", "status": "submitted"})
