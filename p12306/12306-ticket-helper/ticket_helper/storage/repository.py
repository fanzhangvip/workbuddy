"""repository：把 dataclass 配置对象与数据库行互相转换，隔离存储细节。"""

from __future__ import annotations

from typing import Any

from ticket_helper.config.settings import TASK_STATUS_STOPPED, TaskConfig, UserConfig
from ticket_helper.storage.database import Database, get_database


class TaskRepository:
    """任务仓储：只负责与 tasks 表交互，不含业务规则。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_database()

    def to_row(self, task: TaskConfig, *, enabled: bool | None = None) -> dict[str, Any]:
        return {
            "name": task.name,
            "left_dates": task.left_dates,
            "from_stations": task.from_stations,
            "to_stations": task.to_stations,
            "train_filter": {
                "whitelist": task.trains_whitelist,
                "blacklist": task.trains_blacklist,
            },
            "train_types": task.train_types,
            "seat_types": task.seat_types,
            "depart_time_range": task.depart_time_range,
            "passengers": task.passengers,
            "accounts": task.accounts,
            "strategy": task.strategy,
            "query_interval": task.query_interval,
            "sale_open_time": task.sale_open_time,
            "sale_prepare_seconds": task.sale_prepare_seconds,
            "max_retry": task.max_retry,
            "priority": task.priority,
            "remark": task.remark,
            "status": task.status,
            "enabled": task.enabled if enabled is None else enabled,
        }

    def to_entity(self, row: dict[str, Any]) -> TaskConfig:
        train_filter = row.get("train_filter") or {}
        return TaskConfig(
            id=row.get("id"),
            name=row.get("name", ""),
            left_dates=list(row.get("left_dates") or []),
            from_stations=list(row.get("from_stations") or []),
            to_stations=list(row.get("to_stations") or []),
            trains_whitelist=list(train_filter.get("whitelist") or []),
            trains_blacklist=list(train_filter.get("blacklist") or []),
            train_types=list(row.get("train_types") or []),
            seat_types=list(row.get("seat_types") or []),
            depart_time_range=list(row.get("depart_time_range") or []),
            passengers=list(row.get("passengers") or []),
            accounts=list(row.get("accounts") or []),
            strategy=row.get("strategy") or "direct",
            query_interval=float(row.get("query_interval") or 1.0),
            sale_open_time=row.get("sale_open_time") or "",
            sale_prepare_seconds=int(row.get("sale_prepare_seconds") or 30),
            max_retry=int(row.get("max_retry") or 20),
            priority=int(row.get("priority") or 0),
            remark=row.get("remark") or "",
            enabled=bool(row.get("enabled", True)),
            status=row.get("status") or TASK_STATUS_STOPPED,
        )

    def save(self, task: TaskConfig) -> TaskConfig:
        task.id = self.db.save_task(self.to_row(task), task.id)
        return task

    def list(self) -> list[TaskConfig]:
        return [self.to_entity(r) for r in self.db.list_tasks()]

    def get(self, task_id: int) -> TaskConfig | None:
        row = self.db.get_task(task_id)
        return self.to_entity(row) if row else None

    def delete(self, task_id: int) -> int:
        return self.db.delete_task(task_id)

    def set_status(self, task_id: int, status: str) -> int:
        return self.db.update_task_status(task_id, status)


class UserRepository:
    """账号仓储。加解密由上层注入的 cipher 完成（此处只存密文）。"""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_database()

    def save(self, user: UserConfig, password_enc: str, **kwargs: Any) -> int:
        return self.db.save_user(user.username, password_enc, heartbeat=user.heartbeat, enabled=int(user.enabled), **kwargs)

    def list(self, *, with_secret: bool = False) -> list[dict[str, Any]]:
        return self.db.list_users(with_secret=with_secret)

    def to_entity(self, row: dict[str, Any], password: str = "") -> UserConfig:
        return UserConfig(
            username=row.get("username", ""),
            password=password,
            heartbeat=int(row.get("heartbeat") or 120),
            enabled=bool(row.get("enabled", True)),
        )

    def set_state(self, user_id: int, *, status: str | None = None, is_checked: bool | None = None,
                  last_active: str | None = None) -> int:
        fields: dict[str, Any] = {}
        if status is not None:
            fields["status"] = status
        if is_checked is not None:
            fields["is_checked"] = int(is_checked)
        if last_active is not None:
            fields["last_active"] = last_active
        return self.db.update_user_state(user_id, **fields)

    def set_enabled(self, user_id: int, enabled: bool) -> int:
        return self.db.update_user_state(user_id, enabled=int(bool(enabled)))

    def delete(self, user_id: int) -> int:
        return self.db.delete_user(user_id)
