"""任务调度与编排（4.8 / DS-02 / SC-T1 ~ SC-T4）。

- 每个任务 = 独立线程（GrabEngine）+ 可选候补线程（StandbyEngine），互不干扰；
- **看门狗**：定时巡检任务线程，发现异常退出自动重启（SC-T3），
  并以指数退避避免「崩溃 → 立刻重启 → 再崩溃」的抖动循环；
- **进度恢复**：任务状态与查询进度写入 app_state / tasks 表，
  进程重启后自动恢复上一次仍在运行的任务（LG-02 / SC-T4）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ticket_helper.config.settings import (
    TASK_STATUS_FINISHED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_STOPPED,
    AppConfig,
    TaskConfig,
)
from ticket_helper.grab.engine import STATE_ERROR, STATE_STOPPED, STATE_SUCCESS, GrabEngine
from ticket_helper.logger import metrics
from ticket_helper.standby.engine import StandbyEngine

log = logging.getLogger(__name__)

#: 看门狗巡检间隔（秒）
WATCHDOG_INTERVAL = 20.0
#: 单个任务重启退避上限（秒）
MAX_RESTART_BACKOFF = 120.0
#: 连续崩溃多少次后放弃自动重启
MAX_RESTART_ATTEMPTS = 5


@dataclass
class GrabTask:
    """一个抢票任务的运行时封装。"""

    config: TaskConfig
    grab_engine: GrabEngine
    standby_engine: StandbyEngine | None = None
    restarts: int = 0
    next_retry_at: float = 0.0
    created_at: float = field(default_factory=time.time)

    @property
    def id(self) -> int | None:
        return self.config.id

    @property
    def name(self) -> str:
        return self.config.name

    def status(self) -> dict[str, Any]:
        payload = self.grab_engine.status()
        payload.update(
            {
                "restarts": self.restarts,
                "standby": self.standby_engine.status_dict() if self.standby_engine else None,
                "created_at": self.created_at,
            }
        )
        return payload


class TaskScheduler:
    """任务调度器。"""

    def __init__(
        self,
        *,
        config_provider: Callable[[], AppConfig],
        engine_factory: Callable[[TaskConfig], GrabEngine],
        standby_factory: Callable[[TaskConfig], StandbyEngine | None] | None = None,
        db: Any = None,
        notifier: Any = None,
        account_provider: Any = None,
        enable_watchdog: bool = True,
    ) -> None:
        self.config_provider = config_provider
        self.engine_factory = engine_factory
        self.standby_factory = standby_factory
        self.db = db
        self.notifier = notifier
        self.account_provider = account_provider

        self._tasks: dict[int, GrabTask] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._watchdog: threading.Thread | None = None
        self.enable_watchdog = enable_watchdog
        self._state_listeners: list[Callable[[str, dict[str, Any]], None]] = []

    # ------------------------------------------------------------------ #
    def on_event(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        self._state_listeners.append(callback)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        for callback in list(self._state_listeners):
            try:
                callback(event, payload)
            except Exception as exc:  # noqa: BLE001
                log.error("调度器事件回调失败: %s", exc)

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    def start(self) -> None:
        if self.enable_watchdog and (self._watchdog is None or not self._watchdog.is_alive()):
            self._stop_event.clear()
            self._watchdog = threading.Thread(target=self._watchdog_loop, name="scheduler-watchdog", daemon=True)
            self._watchdog.start()
            log.info("调度器看门狗已启动（间隔 %.0fs）", WATCHDOG_INTERVAL)

    def shutdown(self) -> None:
        self._stop_event.set()
        thread = self._watchdog
        if thread and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=3.0)
        self.stop_all()
        log.info("调度器已关闭")

    # ================================================================== #
    # 任务管理
    # ================================================================== #
    def submit(self, task: TaskConfig, *, persist: bool = True) -> GrabTask:
        """提交并启动一个任务（已存在则先停旧的）。"""
        if task.id is None:
            raise ValueError("任务必须先保存到数据库并获得 id 才能提交")

        with self._lock:
            existing = self._tasks.get(task.id)
        if existing is not None:
            self.cancel(task.id, persist=False)

        engine = self.engine_factory(task)
        standby = self.standby_factory(task) if self.standby_factory else None
        if standby is not None and engine.standby_engine is None:
            engine.standby_engine = standby

        wrapper = GrabTask(config=task, grab_engine=engine, standby_engine=standby)
        with self._lock:
            self._tasks[task.id] = wrapper

        engine.start()
        if persist and self.db is not None:
            self.db.update_task_status(task.id, TASK_STATUS_RUNNING)
            self.db.set_state(f"task_runtime:{task.id}", {"name": task.name, "started_at": time.time()})
        self._emit("task_started", {"task_id": task.id, "name": task.name})
        log.info("任务[%s]已提交调度 (id=%s)", task.name, task.id)
        return wrapper

    def cancel(self, task_id: int, *, persist: bool = True) -> bool:
        """停止任务并释放资源。"""
        with self._lock:
            wrapper = self._tasks.pop(task_id, None)
        if wrapper is None:
            return False
        try:
            wrapper.grab_engine.stop(reason="任务被停止")
            if wrapper.standby_engine is not None:
                wrapper.standby_engine.stop(reason="任务被停止")
        finally:
            if persist and self.db is not None:
                self.db.update_task_status(task_id, TASK_STATUS_STOPPED)
                self.db.delete_state(f"task_runtime:{task_id}")
        self._emit("task_stopped", {"task_id": task_id, "name": wrapper.name})
        log.info("任务[%s]已停止 (id=%s)", wrapper.name, task_id)
        return True

    def restart(self, task_id: int) -> bool:
        """重启任务（配置从数据库重新读取）。"""
        self.cancel(task_id, persist=False)
        if self.db is None:
            return False
        row = self.db.get_task(task_id)
        if row is None:
            return False
        from ticket_helper.storage.repository import TaskRepository

        task = TaskRepository(self.db).to_entity(row)
        if not task.enabled:
            log.warning("任务[%s]已禁用，不重启", task.name)
            return False
        self.submit(task)
        return True

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._tasks.keys())
        for task_id in ids:
            self.cancel(task_id)

    def get(self, task_id: int) -> GrabTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def all(self) -> list[GrabTask]:
        with self._lock:
            return list(self._tasks.values())

    def is_running(self, task_id: int) -> bool:
        wrapper = self.get(task_id)
        return bool(wrapper and wrapper.grab_engine.running)

    # ------------------------------------------------------------------ #
    def status_all(self) -> list[dict[str, Any]]:
        return [t.status() for t in self.all()]

    def summary(self) -> dict[str, Any]:
        tasks = self.all()
        states: dict[str, int] = {}
        for task in tasks:
            states[task.grab_engine.state] = states.get(task.grab_engine.state, 0) + 1
        return {
            "total": len(tasks),
            "states": states,
            "running": sum(1 for t in tasks if t.grab_engine.running),
            "success": sum(1 for t in tasks if t.grab_engine.state == STATE_SUCCESS),
        }

    # ================================================================== #
    # 看门狗 / 恢复
    # ================================================================== #
    def _watchdog_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._watchdog_tick()
            except Exception as exc:  # noqa: BLE001 - 看门狗自身必须存活
                log.error("看门狗巡检异常: %s", exc)
            self._stop_event.wait(WATCHDOG_INTERVAL)

    def _watchdog_tick(self) -> None:
        """SC-T3：发现异常退出的任务并重启（带指数退避）。"""
        now = time.time()
        for wrapper in self.all():
            engine = wrapper.grab_engine
            if engine.running:
                continue
            if engine.state == STATE_SUCCESS:
                if self.db is not None:
                    self.db.update_task_status(wrapper.id or 0, TASK_STATUS_FINISHED)
                continue
            if engine.state == STATE_STOPPED:
                continue
            if not wrapper.config.enabled:
                continue

            if wrapper.restarts >= MAX_RESTART_ATTEMPTS:
                if wrapper.restarts == MAX_RESTART_ATTEMPTS:
                    wrapper.restarts += 1
                    log.error(
                        "任务[%s]连续重启 %d 次仍失败，停止自动重启（请检查账号/网络）",
                        wrapper.name,
                        MAX_RESTART_ATTEMPTS,
                    )
                    if self.notifier is not None:
                        self.notifier.notify_error(
                            wrapper.name, f"任务连续 {MAX_RESTART_ATTEMPTS} 次异常退出，已停止自动重启"
                        )
                continue

            if now < wrapper.next_retry_at:
                continue

            backoff = min(MAX_RESTART_BACKOFF, 5.0 * (2 ** wrapper.restarts))
            wrapper.restarts += 1
            wrapper.next_retry_at = now + backoff
            log.warning(
                "看门狗发现任务[%s]已退出(state=%s, error=%s)，第 %d 次重启（%.0fs 后生效）",
                wrapper.name,
                engine.state,
                engine.error,
                wrapper.restarts,
                backoff,
            )
            metrics.incr("scheduler.watchdog_restart")
            try:
                engine.start()
                wrapper.next_retry_at = 0.0
            except Exception as exc:  # noqa: BLE001
                log.error("任务[%s]重启失败: %s", wrapper.name, exc)

    # ------------------------------------------------------------------ #
    def restore_running_tasks(self) -> list[int]:
        """LG-02 / SC-T4：重启进程后恢复上次仍在运行的任务。"""
        if self.db is None:
            return []
        # SQL 里也必须用同一个常量拼：改了状态值却漏了这里，恢复逻辑会静默查不到
        rows = self.db.query(
            f"SELECT id FROM tasks WHERE status = '{TASK_STATUS_RUNNING}' AND enabled = 1"
        )
        restored: list[int] = []
        from ticket_helper.storage.repository import TaskRepository

        repo = TaskRepository(self.db)
        for row in rows:
            task = repo.get(int(row["id"]))
            if task is None:
                continue
            try:
                self.submit(task, persist=False)
                restored.append(int(row["id"]))
            except Exception as exc:  # noqa: BLE001
                log.error("恢复任务 %s 失败: %s", row["id"], exc)
        if restored:
            log.info("已恢复上次运行中的任务: %s", restored)
        return restored

    # ------------------------------------------------------------------ #
    def apply_config(self, config: AppConfig) -> None:
        """配置热更新：把新的任务配置下发到已运行的引擎（CF-04）。"""
        if self.db is None:
            return
        by_name = {t.name: t for t in config.tasks}
        for wrapper in self.all():
            updated = by_name.get(wrapper.name)
            if updated is None:
                continue
            updated.id = wrapper.id
            wrapper.config = updated
            wrapper.grab_engine.apply_config(updated)
            if wrapper.standby_engine is not None:
                wrapper.standby_engine.apply_config(updated)
        log.info("调度器已接收配置热更新（%d 个任务配置刷新）", len(by_name))

    def heartbeats(self) -> dict[str, bool]:
        """账号心跳巡检（供看门狗或 Web 端定期调用）。"""
        if self.account_provider is None:
            return {}
        results: dict[str, bool] = {}
        for user in self.account_provider.all():
            if not user.enabled:
                continue
            try:
                results[user.username] = user.keep_alive_once()
            except Exception as exc:  # noqa: BLE001
                log.error("账号 %s 心跳巡检失败: %s", user.username, exc)
                results[user.username] = False
        return results

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "tasks": self.status_all(),
            "watchdog": bool(self._watchdog and self._watchdog.is_alive()),
        }
