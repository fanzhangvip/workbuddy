"""端到端验收场景（对应测试方案「第四节 关键端到端验收场景」）。

这些用例全部跑在 mock-12306 上，**不触碰真实 12306**，
因此可以反复执行、放进 CI、也不会带来任何风控风险。
"""

from __future__ import annotations

import time

import pytest

from ticket_helper.config.settings import TaskConfig, UserConfig
from ticket_helper.order.service import OrderService
from ticket_helper.ratelimit.limiter import RateLimiter
from ticket_helper.scheduler.scheduler import TaskScheduler
from ticket_helper.standby.engine import STANDBY_QUEUING, STANDBY_SUCCESS, StandbyEngine
from ticket_helper.grab.engine import GrabEngine
from ticket_helper.verify.service import CallbackSolver

pytestmark = pytest.mark.e2e


# --------------------------------------------------------------------------- #
# 公共装配
# --------------------------------------------------------------------------- #
@pytest.fixture()
def env(app_context, mock_server):
    app_context.verify_service.set_solver(CallbackSolver(lambda i, h, k: "112,45", name="fixed"))
    # 走 AppContext 的生产路径：账号会加密落库（AC-01），场景 6 会校验这一点
    user = app_context.upsert_user(UserConfig(username="test_user", password="test_pass"))
    assert user.login() is True
    user.is_checked = True
    return app_context, mock_server, user


def _scheduler(ctx, *, standby: bool = True) -> TaskScheduler:
    def engine_factory(task: TaskConfig) -> GrabEngine:
        return GrabEngine(
            task,
            account_provider=ctx.accounts.allocate,
            order_service=OrderService(ctx.db, queue_max_polls=3),
            notifier=ctx.notifier,
            db=ctx.db,
            rate_limiter=RateLimiter(interval=0.05),
            station_map=ctx.station_map,
            on_state_change=ctx._on_engine_state_change,
        )

    def standby_factory(task: TaskConfig):
        if not standby or task.strategy not in ("standby", "dual"):
            return None
        engine = StandbyEngine(
            task, account_provider=ctx.accounts.allocate, notifier=ctx.notifier, db=ctx.db, poll_interval=0.3
        )
        # 生产的轮询间隔有**下限**（防风控，见 standby.engine.MIN_POLL_INTERVAL），
        # 照字面用会让本用例白等 45 秒以上。按项目既有做法（test_standby.py）
        # 在**启动前**把两个间隔压小 —— 只影响「多久问一次」，
        # 兑现判据、要轮询几次才兑现都不变。
        engine.poll_interval = 0.3
        engine.order_check_interval = 0.3
        return engine

    return TaskScheduler(
        config_provider=lambda: ctx.config,
        engine_factory=engine_factory,
        standby_factory=standby_factory,
        db=ctx.db,
        notifier=ctx.notifier,
        account_provider=ctx.accounts,
        enable_watchdog=False,
    )


# =========================================================================== #
# 场景 1：准点抢票
# =========================================================================== #
def test_scenario_1_onsale_grab(env):
    """配置任务 → 开售前启动 → 到点首次下单 → 出票 → 通知 → 自动停止。"""
    ctx, mock_server, user = env
    from datetime import datetime, timedelta

    # 开售时间留出足够余量（3s），避免机器繁忙时测试自己把窗口睡过去
    sale_time = datetime.now() + timedelta(seconds=3.0)
    task = ctx.save_task(
        TaskConfig(
            name="场景1-准点抢票",
            left_dates=["2026-10-01"],
            from_stations=["广州"],
            to_stations=["长沙"],
            trains_whitelist=["G6102", "G6140"],
            seat_types=["二等座", "一等座"],
            passengers=["张三"],
            strategy="direct",
            query_interval=0.3,
            sale_open_time=sale_time.strftime("%Y-%m-%d %H:%M:%S"),
            sale_prepare_seconds=1,
            max_retry=2,
        )
    )
    notifications: list[dict] = []
    ctx.notifier.subscribe_web(notifications.append)

    scheduler = _scheduler(ctx, standby=False)
    scheduler.submit(task)

    wrapper = scheduler.get(task.id)
    # 开售前：进入待命状态，且**没有发起任何查询**（不空转 → 避免无意义请求触发风控）
    deadline = time.time() + 6
    while time.time() < deadline and wrapper.grab_engine.state != "ready":
        time.sleep(0.05)
    assert wrapper.grab_engine.state == "ready", f"未进入待命，state={wrapper.grab_engine.state}"
    assert wrapper.grab_engine.stats.queries == 0, "待命期间不应发起查询"

    wrapper.grab_engine.wait(timeout=40)
    assert wrapper.grab_engine.state == "success", wrapper.grab_engine.error
    result = wrapper.grab_engine.result
    assert result and result.ok
    assert result.order_no

    # 出票后强提醒（NT-02 / OD-02）
    time.sleep(0.5)
    assert any(item.get("critical") for item in notifications)
    assert any("抢票成功" in (item.get("title") or "") for item in notifications)

    # 订单流水完整（OD-04）
    orders = ctx.db.list_orders(task_id=task.id)
    assert any(o["status"] == "success" for o in orders)
    # 任务自动停止（GR-06 / GR-08）
    assert wrapper.grab_engine.running is False
    scheduler.shutdown()


# =========================================================================== #
# 场景 2：余票捡漏
# =========================================================================== #
def test_scenario_2_remain_hunting(env):
    """任务指向无票车次 → 无票时空转 → 有票瞬间立即抢到。"""
    ctx, mock_server, user = env
    task = ctx.save_task(
        TaskConfig(
            name="场景2-捡漏",
            left_dates=["2026-10-01"],
            from_stations=["广州"],
            to_stations=["长沙"],
            trains_whitelist=["G6102"],
            seat_types=["二等座"],
            passengers=["张三"],
            strategy="direct",
            query_interval=0.3,
            max_retry=2,
        )
    )
    mock_server.set_scenario("sold_out")

    scheduler = _scheduler(ctx, standby=False)
    scheduler.submit(task)
    wrapper = scheduler.get(task.id)

    time.sleep(1.5)
    assert wrapper.grab_engine.state in ("grabbing", "ready")
    assert wrapper.grab_engine.stats.order_attempts == 0, "无票时不应发起下单"

    # 官方放票 / 有人退票 → 瞬间有票
    mock_server.set_scenario("has_ticket")
    wrapper.grab_engine.wait(timeout=40)

    assert wrapper.grab_engine.state == "success", wrapper.grab_engine.error
    assert wrapper.grab_engine.stats.order_attempts >= 1
    scheduler.shutdown()


# =========================================================================== #
# 场景 3：候补 + 捡漏双轨
# =========================================================================== #
def test_scenario_3_dual_track(env):
    """策略=dual 且余票为 0 → 自动提交候补；候补兑现 → 通知 + 停另一轨。"""
    ctx, mock_server, user = env
    task = ctx.save_task(
        TaskConfig(
            name="场景3-双轨",
            left_dates=["2026-10-01"],
            from_stations=["广州"],
            to_stations=["长沙"],
            trains_whitelist=["G6102"],
            seat_types=["二等座"],
            passengers=["张三"],
            strategy="dual",
            query_interval=0.4,
            max_retry=1,
        )
    )
    mock_server.set_scenario("standby")
    notifications: list[dict] = []
    ctx.notifier.subscribe_web(notifications.append)

    scheduler = _scheduler(ctx, standby=True)
    scheduler.submit(task)
    wrapper = scheduler.get(task.id)

    # 把候补轨的节奏调快：构造器里的 15s/300s 下限是**给真机用的**
    # （防突发、且订单接口连打 4 次就会被 WAF 拦），mock 不需要等这么久。
    # 不调的话这条用例要等 5 分钟才做第一次兑现检查。
    if wrapper.standby_engine is not None:
        wrapper.standby_engine.poll_interval = 0.05
        wrapper.standby_engine.order_check_interval = 0.05
        wrapper.standby_engine._next_order_check = 0.0  # noqa: SLF001

    # 候补轨应自动提交候补单。
    #
    # 判据是 **status 进入 QUEUING**，不是 ``hb_order_id`` 非空：
    # 真机上官方**不再返回候补单号**，只以「排队 status==1 = 候补单已生效」为准，
    # 所以 ``confirm_hb`` 刻意不给假单号（见 TicketApi.confirm_hb 的注释）。
    # 这条断言以前写的是 ``hb_order_id``，在实现改成「不造假单号」之后就一直是红的 ——
    # 而它红的原因和候补链路本身毫无关系，属于典型的误导性失败。
    deadline = time.time() + 25
    while time.time() < deadline and not (
        wrapper.standby_engine and wrapper.standby_engine.status in (STANDBY_QUEUING, STANDBY_SUCCESS)
    ):
        time.sleep(0.3)
    assert wrapper.standby_engine is not None
    assert wrapper.standby_engine.status in (STANDBY_QUEUING, STANDBY_SUCCESS), (
        f"dual 策略必须自动提交候补，实际 status={wrapper.standby_engine.status} "
        f"error={wrapper.standby_engine.error!r}"
    )
    # 候补单生效必须有官方排队结论作依据（mock 返回 status=1）
    assert wrapper.standby_engine.queue.get("status") == 1, "候补单生效必须由官方排队结论支撑"

    # 等候补兑现（mock 第 3 次轮询兑现）
    deadline = time.time() + 30
    while time.time() < deadline and wrapper.standby_engine.status != STANDBY_SUCCESS:
        time.sleep(0.3)

    assert wrapper.standby_engine.status == STANDBY_SUCCESS
    time.sleep(0.5)
    assert any(item.get("critical") for item in notifications), "候补兑现必须即时通知"

    rows = ctx.db.list_standby_orders(task_id=task.id)
    assert any(r["status"] == STANDBY_SUCCESS for r in rows), "候补状态应落库"

    # 成其一即停另一轨（HB-05）
    assert wrapper.grab_engine.running is False
    scheduler.shutdown()


# =========================================================================== #
# 场景 4：多账号 + 多任务
# =========================================================================== #
def test_scenario_4_multi_account_multi_task(env):
    """2 账号 + 3 任务并行，状态独立、分配正确、互不污染。"""
    ctx, mock_server, user = env
    second = ctx.upsert_user(UserConfig(username="second_user", password="whatever"))
    assert second.login() is True
    second.is_checked = True
    assert len(ctx.accounts.online_users()) == 2

    mock_server.set_scenario("sold_out")  # 让任务持续运行，便于观察并行
    tasks = [
        ctx.save_task(
            TaskConfig(
                name=f"场景4-任务{i}",
                left_dates=["2026-10-01"],
                from_stations=["广州"],
                to_stations=["长沙"],
                trains_whitelist=["G6102"],
                seat_types=["二等座"],
                passengers=["张三"],
                strategy="direct",
                query_interval=0.4,
            )
        )
        for i in range(3)
    ]

    scheduler = _scheduler(ctx, standby=False)
    for task in tasks:
        scheduler.submit(task)
    time.sleep(2.5)

    summary = scheduler.summary()
    assert summary["total"] == 3
    assert summary["running"] == 3

    assigned = {scheduler.get(t.id).grab_engine.user.username for t in tasks}
    assert len(assigned) == 2, f"两个账号都应参与分配，实际={assigned}"

    # 每个任务的统计互不共享
    stats = [scheduler.get(t.id).grab_engine.stats for t in tasks]
    assert len({id(s) for s in stats}) == 3

    scheduling = {scheduler.get(t.id).grab_engine.task.name for t in tasks}
    assert scheduling == {f"场景4-任务{i}" for i in range(3)}

    scheduler.shutdown()


# =========================================================================== #
# 场景 5：异常自愈
# =========================================================================== #
def test_scenario_5_self_healing(env):
    """运行中注入服务端故障 / 会话失效 → 自动恢复，不丢任务。"""
    ctx, mock_server, user = env
    task = ctx.save_task(
        TaskConfig(
            name="场景5-自愈",
            left_dates=["2026-10-01"],
            from_stations=["广州"],
            to_stations=["长沙"],
            trains_whitelist=["G6102"],
            seat_types=["二等座"],
            passengers=["张三"],
            strategy="direct",
            query_interval=0.3,
        )
    )

    scheduler = _scheduler(ctx, standby=False)
    scheduler.submit(task)
    wrapper = scheduler.get(task.id)

    # 阶段一：服务端 500
    mock_server.set_scenario("error500")
    time.sleep(1.5)
    assert wrapper.grab_engine.running is True, "服务端异常不应把引擎打挂"
    assert wrapper.grab_engine.stats.rounds >= 1

    # 阶段二：会话失效 / 风控 → 引擎必须自行降频并保持存活。
    # 注意：风控退避是累加的（10s * 次数，上限 60s），这是「别继续撞墙」的刻意设计，
    # 所以这里只短暂注入；并且用轮询等待信号（风控可能落在查询阶段，也可能落在下单阶段）。
    mock_server.set_scenario("risk")
    deadline = time.time() + 30
    risk_seen = False
    while time.time() < deadline:
        if wrapper.grab_engine.stats.risk_events >= 1 or ctx.rate_limiter.snapshot()["risk_count"] >= 1:
            risk_seen = True
            break
        time.sleep(0.3)
    assert risk_seen, "风控信号应被引擎捕获并驱动限速器降频"
    assert wrapper.grab_engine.running is True, "风控不能把引擎打挂"

    # 阶段三：环境恢复 → 引擎在退避结束后自动抢到票（无需人工干预）
    mock_server.set_scenario("has_ticket")
    wrapper.grab_engine.wait(timeout=150)
    assert wrapper.grab_engine.state == "success", (
        f"自愈失败：state={wrapper.grab_engine.state} error={wrapper.grab_engine.error} "
        f"risk_events={wrapper.grab_engine.stats.risk_events}"
    )

    scheduler.shutdown()


def test_scenario_5b_task_thread_crash_is_detected(env):
    """引擎线程意外退出 → 看门狗发现并重启（任务不丢）。"""
    ctx, mock_server, user = env
    task = ctx.save_task(
        TaskConfig(
            name="场景5b-看门狗",
            left_dates=["2026-10-01"],
            from_stations=["广州"],
            to_stations=["长沙"],
            trains_whitelist=["G6102"],
            seat_types=["二等座"],
            passengers=["张三"],
            strategy="direct",
            query_interval=0.4,
        )
    )
    mock_server.set_scenario("sold_out")
    scheduler = _scheduler(ctx, standby=False)
    scheduler.submit(task)
    wrapper = scheduler.get(task.id)
    time.sleep(0.8)

    # 模拟线程意外死亡
    wrapper.grab_engine._stop_event.set()  # noqa: SLF001
    wrapper.grab_engine._thread.join(timeout=5)  # noqa: SLF001
    wrapper.grab_engine._set_state("error", "模拟崩溃")  # noqa: SLF001
    assert wrapper.grab_engine.running is False

    scheduler._watchdog_tick()  # noqa: SLF001
    time.sleep(1.2)
    assert wrapper.grab_engine.running is True
    assert wrapper.restarts == 1
    scheduler.shutdown()


# =========================================================================== #
# 场景 6：重启后恢复（数据不丢）
# =========================================================================== #
def test_scenario_6_restart_recovers_state(env):
    """进程重启后：任务配置、订单流水、运行状态都能恢复。"""
    ctx, mock_server, user = env
    task = ctx.save_task(
        TaskConfig(
            name="场景6-恢复",
            left_dates=["2026-10-01"],
            from_stations=["广州"],
            to_stations=["长沙"],
            trains_whitelist=["G6102"],
            seat_types=["二等座"],
            passengers=["张三"],
            strategy="direct",
            query_interval=0.3,
        )
    )
    scheduler = _scheduler(ctx, standby=False)
    scheduler.submit(task)
    wrapper = scheduler.get(task.id)
    wrapper.grab_engine.wait(timeout=40)
    assert wrapper.grab_engine.state == "success"
    scheduler.shutdown()

    # ---- 模拟进程重启：全新 Database / 仓储读取同一份文件 ----
    from ticket_helper.storage.database import Database
    from ticket_helper.storage.repository import TaskRepository, UserRepository

    fresh_db = Database(ctx.paths.db_path).init()
    restored_task = TaskRepository(fresh_db).get(task.id)
    assert restored_task is not None
    assert restored_task.name == "场景6-恢复"
    assert restored_task.seat_types == ["二等座"]

    users = UserRepository(fresh_db).list(with_secret=True)
    assert any(u["username"] == "test_user" for u in users)
    # 密码必须是密文
    secret_row = next(u for u in users if u["username"] == "test_user")
    assert "test_pass" not in (secret_row["password_enc"] or "")

    orders = fresh_db.list_orders(task_id=task.id)
    assert any(o["status"] == "success" for o in orders)

    # 运行中任务可被恢复
    fresh_db.update_task_status(task.id, "running")
    fresh_scheduler = _scheduler(ctx, standby=False)
    assert task.id in fresh_scheduler.restore_running_tasks()
    fresh_scheduler.shutdown()


# =========================================================================== #
# 长时间挂机（缩短版）
# =========================================================================== #
def test_long_running_does_not_accumulate_invalid_requests(env):
    """连续跑一段时间后：无崩溃、无无效请求堆积。

    真实验收要求 ≥1h / ≥72h，这里跑一段可接受的时间，验证关键性质：
    1) 引擎不崩；2) 无票时不会产生下单请求；3) 限速器不失控。
    """
    ctx, mock_server, user = env
    mock_server.set_scenario("sold_out")
    task = ctx.save_task(
        TaskConfig(
            name="长跑任务",
            left_dates=["2026-10-01"],
            from_stations=["广州"],
            to_stations=["长沙"],
            trains_whitelist=["G6102"],
            seat_types=["二等座"],
            passengers=["张三"],
            strategy="direct",
            query_interval=0.35,
        )
    )
    scheduler = _scheduler(ctx, standby=False)
    scheduler.submit(task)
    wrapper = scheduler.get(task.id)

    time.sleep(6.0)
    engine = wrapper.grab_engine
    assert engine.running is True
    assert engine.error == ""
    assert engine.stats.rounds >= 8
    assert engine.stats.order_attempts == 0, "无票期间绝不应发起下单，否则就是无意义的无效请求"
    assert ctx.rate_limiter.interval <= ctx.config.ratelimit.max_interval

    before_requests = mock_server.state.requests
    scheduler.shutdown()
    after_requests = mock_server.state.requests
    assert len(after_requests) >= len(before_requests)
