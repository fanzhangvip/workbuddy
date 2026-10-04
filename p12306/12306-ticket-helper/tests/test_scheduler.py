"""任务调度测试（SC-T1 ~ SC-T4）。"""

from __future__ import annotations

import time

import pytest

from ticket_helper.config.settings import TaskConfig, UserConfig
from ticket_helper.grab.engine import STATE_ERROR, STATE_SUCCESS
from ticket_helper.order.service import OrderService
from ticket_helper.ratelimit.limiter import RateLimiter
from ticket_helper.scheduler.scheduler import MAX_RESTART_ATTEMPTS, TaskScheduler
from ticket_helper.grab.engine import GrabEngine
from ticket_helper.standby.engine import StandbyEngine
from ticket_helper.verify.service import CallbackSolver


@pytest.fixture()
def scheduler_env(app_context, mock_server):
    app_context.verify_service.set_solver(CallbackSolver(lambda i, h, k: "112,45", name="fixed"))
    user = app_context.accounts.add_user(UserConfig(username="test_user", password="test_pass"))
    assert user.login() is True
    user.is_checked = True
    return app_context, mock_server, user


def _make_scheduler(ctx):
    def engine_factory(task: TaskConfig) -> GrabEngine:
        return GrabEngine(
            task,
            account_provider=ctx.accounts.allocate,
            order_service=OrderService(ctx.db, queue_max_polls=3),
            notifier=ctx.notifier,
            db=ctx.db,
            rate_limiter=RateLimiter(interval=0.05),
            station_map=ctx.station_map,
            # 与生产一致：状态变化要回写数据库（任务列表页据此显示状态）
            on_state_change=ctx._on_engine_state_change,
        )

    def standby_factory(task: TaskConfig):
        if task.strategy not in ("standby", "dual"):
            return None
        return StandbyEngine(task, account_provider=ctx.accounts.allocate, notifier=ctx.notifier, db=ctx.db)

    return TaskScheduler(
        config_provider=lambda: ctx.config,
        engine_factory=engine_factory,
        standby_factory=standby_factory,
        db=ctx.db,
        notifier=ctx.notifier,
        account_provider=ctx.accounts,
        enable_watchdog=False,
    )


def _task(ctx, name: str, trains=None) -> TaskConfig:
    return ctx.save_task(
        TaskConfig(
            name=name,
            left_dates=["2026-10-01"],
            from_stations=["广州"],
            to_stations=["长沙"],
            trains_whitelist=trains or ["G6102"],
            seat_types=["二等座"],
            passengers=["张三"],
            strategy="direct",
            query_interval=0.3,
            max_retry=1,
        )
    )


# --------------------------------------------------------------------------- #
# SC-T1 多任务并行、状态互不干扰
# --------------------------------------------------------------------------- #
def test_multiple_tasks_run_independently(scheduler_env):
    ctx, mock_server, user = scheduler_env
    # 全部无票 → 两个任务都会持续运行，便于验证「并行且互不干扰」
    mock_server.set_scenario("sold_out")
    scheduler = _make_scheduler(ctx)
    task_a = _task(ctx, "任务A", ["G6102"])
    task_b = _task(ctx, "任务B", ["G6140"])

    scheduler.submit(task_a)
    scheduler.submit(task_b)
    time.sleep(3.0)

    summary = scheduler.summary()
    assert summary["total"] == 2
    assert summary["running"] == 2
    assert len(scheduler.all()) == 2

    # 两个任务的运行时状态对象互不共享
    a = scheduler.get(task_a.id)
    b = scheduler.get(task_b.id)
    assert a.grab_engine is not b.grab_engine
    assert a.config.name != b.config.name

    scheduler.shutdown()


def test_task_status_payload_is_json_friendly(scheduler_env):
    ctx, mock_server, user = scheduler_env
    scheduler = _make_scheduler(ctx)
    task = _task(ctx, "任务C")
    scheduler.submit(task)
    time.sleep(1.0)

    import json

    payload = scheduler.status_all()
    json.dumps(payload, ensure_ascii=False)  # 必须可序列化（要经 WS 推给前端）
    assert payload[0]["task_name"] == "任务C"
    assert "stats" in payload[0]
    scheduler.shutdown()


# --------------------------------------------------------------------------- #
# SC-T2 启停与资源释放
# --------------------------------------------------------------------------- #
def test_cancel_releases_task_and_persists_status(scheduler_env):
    ctx, mock_server, user = scheduler_env
    scheduler = _make_scheduler(ctx)
    task = _task(ctx, "任务D")
    scheduler.submit(task)
    time.sleep(0.8)

    assert scheduler.is_running(task.id) is True
    assert scheduler.cancel(task.id) is True
    assert scheduler.get(task.id) is None
    assert scheduler.is_running(task.id) is False

    row = ctx.db.get_task(task.id)
    assert row["status"] == "stopped"
    assert ctx.db.get_state(f"task_runtime:{task.id}") is None

    # 取消不存在的任务返回 False，而不是抛错
    assert scheduler.cancel(99999) is False


def test_restart_task_reloads_config(scheduler_env):
    ctx, mock_server, user = scheduler_env
    scheduler = _make_scheduler(ctx)
    task = _task(ctx, "任务E")
    scheduler.submit(task)
    time.sleep(0.8)
    assert scheduler.restart(task.id) is True
    time.sleep(0.8)
    assert scheduler.is_running(task.id) is True
    scheduler.shutdown()


def test_disabled_task_is_not_restarted(scheduler_env):
    ctx, mock_server, user = scheduler_env
    scheduler = _make_scheduler(ctx)
    task = _task(ctx, "任务F")
    task.enabled = False
    ctx.save_task(task)
    assert scheduler.restart(task.id) is False


# --------------------------------------------------------------------------- #
# SC-T3 看门狗重启
# --------------------------------------------------------------------------- #
def test_watchdog_restarts_crashed_task(scheduler_env, monkeypatch):
    ctx, mock_server, user = scheduler_env
    scheduler = _make_scheduler(ctx)
    task = _task(ctx, "任务G")
    wrapper = scheduler.submit(task)
    time.sleep(0.3)

    # 模拟引擎异常退出：直接停掉线程并把状态改成 error
    wrapper.grab_engine._stop_event.set()  # noqa: SLF001
    wrapper.grab_engine._thread.join(timeout=5)  # noqa: SLF001
    wrapper.grab_engine._set_state(STATE_ERROR, "模拟崩溃")  # noqa: SLF001

    assert wrapper.grab_engine.running is False
    scheduler._watchdog_tick()  # noqa: SLF001
    assert wrapper.restarts == 1
    time.sleep(1.0)
    assert wrapper.grab_engine.running is True, "看门狗应把崩溃的任务拉起来"

    scheduler.shutdown()
    scheduler.cancel(task.id, persist=False)


def test_watchdog_gives_up_after_max_restarts(scheduler_env):
    ctx, mock_server, user = scheduler_env
    scheduler = _make_scheduler(ctx)
    task = _task(ctx, "任务H")
    wrapper = scheduler.submit(task)
    wrapper.restarts = MAX_RESTART_ATTEMPTS
    wrapper.grab_engine._stop_event.set()  # noqa: SLF001
    wrapper.grab_engine._thread.join(timeout=5)  # noqa: SLF001
    wrapper.grab_engine._set_state(STATE_ERROR, "反复崩溃")  # noqa: SLF001

    scheduler._watchdog_tick()  # noqa: SLF001
    assert wrapper.restarts == MAX_RESTART_ATTEMPTS + 1  # 标记为「已放弃」
    scheduler.shutdown()


def test_watchdog_ignores_successful_tasks(scheduler_env):
    ctx, mock_server, user = scheduler_env
    scheduler = _make_scheduler(ctx)
    task = _task(ctx, "任务I")
    wrapper = scheduler.submit(task)
    # 先让线程真正退出，再标记为成功（模拟「出票成功后引擎自然结束」）
    wrapper.grab_engine._stop_event.set()  # noqa: SLF001
    wrapper.grab_engine._thread.join(timeout=5)  # noqa: SLF001
    wrapper.grab_engine._set_state(STATE_SUCCESS, "出票成功")  # noqa: SLF001

    scheduler._watchdog_tick()  # noqa: SLF001
    assert wrapper.restarts == 0, "已成功的任务绝不能被看门狗重启（否则会重复下单）"
    assert ctx.db.get_task(task.id)["status"] == "finished"
    scheduler.shutdown()


def test_watchdog_backoff_prevents_immediate_retry(scheduler_env):
    ctx, mock_server, user = scheduler_env
    scheduler = _make_scheduler(ctx)
    task = _task(ctx, "任务J")
    wrapper = scheduler.submit(task)
    wrapper.grab_engine._stop_event.set()  # noqa: SLF001
    wrapper.grab_engine._thread.join(timeout=5)  # noqa: SLF001
    wrapper.grab_engine._set_state(STATE_ERROR, "崩溃")  # noqa: SLF001
    wrapper.next_retry_at = time.time() + 60  # 模拟已在退避窗口内
    before = wrapper.restarts
    scheduler._watchdog_tick()  # noqa: SLF001
    assert wrapper.restarts == before, "退避窗口内不应重复重启"
    scheduler.shutdown()


# --------------------------------------------------------------------------- #
# SC-T4 查询进度恢复
# --------------------------------------------------------------------------- #
def test_restore_running_tasks_after_restart(scheduler_env):
    ctx, mock_server, user = scheduler_env
    task = _task(ctx, "任务K")
    ctx.db.update_task_status(task.id, "running")

    # 模拟进程重启：全新的调度器实例读库恢复
    fresh = _make_scheduler(ctx)
    restored = fresh.restore_running_tasks()
    assert task.id in restored
    time.sleep(1.0)
    assert fresh.is_running(task.id) is True
    fresh.shutdown()


def test_restore_skips_disabled_tasks(scheduler_env):
    ctx, mock_server, user = scheduler_env
    task = _task(ctx, "任务L")
    ctx.db.update_task_status(task.id, "running")
    ctx.db.execute("UPDATE tasks SET enabled = 0 WHERE id = ?", (task.id,))

    fresh = _make_scheduler(ctx)
    assert fresh.restore_running_tasks() == []
    fresh.shutdown()


def test_heartbeats_check_all_accounts(scheduler_env):
    ctx, mock_server, user = scheduler_env
    scheduler = _make_scheduler(ctx)
    results = scheduler.heartbeats()
    assert results == {"test_user": True}
    scheduler.shutdown()


def test_scheduler_snapshot_shape(scheduler_env):
    ctx, mock_server, user = scheduler_env
    scheduler = _make_scheduler(ctx)
    snap = scheduler.snapshot()
    for key in ("total", "states", "running", "success", "tasks", "watchdog"):
        assert key in snap
    scheduler.shutdown()
