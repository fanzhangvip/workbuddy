"""抢票引擎与选择策略测试（GR-T1 ~ GR-T8）。"""

from __future__ import annotations

import time

import pytest

from ticket_helper.config.settings import TaskConfig, UserConfig
from ticket_helper.grab.engine import (
    STATE_ERROR,
    STATE_GRABBING,
    STATE_READY,
    STATE_STOPPED,
    STATE_SUCCESS,
    STATE_TEXT,
    GrabEngine,
)
from ticket_helper.grab.strategy import PickStrategy
from ticket_helper.inter.parser import parse_left_ticket_result
from ticket_helper.order.service import OrderService
from ticket_helper.query.engine import Candidate, QueryEngine
from ticket_helper.ratelimit.limiter import RateLimiter
from ticket_helper.verify.service import CallbackSolver
from mock_server.server import build_ticket_row


def _ticket(train_code="G6102", start="08:12", duration="02:35", **seats):
    return parse_left_ticket_result(
        build_ticket_row(
            train_code=train_code,
            secret_seed=f"s-{train_code}",
            from_code="GZQ",
            to_code="CWQ",
            start_time=start,
            arrive_time="10:47",
            duration=duration,
            train_date="2026-10-01",
            seats=seats or {"二等座": "5"},
        )
    )


# --------------------------------------------------------------------------- #
# GR-T4 选择策略（纯逻辑）
# --------------------------------------------------------------------------- #
def test_strategy_next_candidate_respects_priority(sample_task):
    task = sample_task
    task.trains_whitelist = ["G6140", "G6102"]
    task.seat_types = ["一等座", "二等座"]
    strategy = PickStrategy(task)

    fast = _ticket("G6102", duration="02:35", **{"一等座": "无", "二等座": "3"})
    slow = _ticket("G6140", duration="02:47", **{"一等座": "2", "二等座": "5"})
    engine = QueryEngine(api=None, task=task)  # type: ignore[arg-type]
    candidates = engine.build_candidates([fast, slow])

    first = strategy.next_candidate(candidates)
    assert first.ticket.train_code == "G6140"          # 白名单里排第一
    assert first.seat.seat_type == "一等座"             # 席别优先级第一
    # 已尝试过的最优候选会被跳过
    second = strategy.next_candidate(candidates, exclude={strategy.key_of(first)})
    assert (second.ticket.train_code, second.seat.seat_type) == ("G6140", "二等座")


def test_strategy_falls_back_to_fastest_when_no_priority(sample_task):
    task = sample_task
    task.trains_whitelist = []
    task.seat_types = ["二等座"]
    engine = QueryEngine(api=None, task=task)  # type: ignore[arg-type]
    a = _ticket("G100", duration="05:00", **{"二等座": "1"})
    b = _ticket("G200", duration="01:00", **{"二等座": "1"})
    first = PickStrategy(task).next_candidate(engine.build_candidates([a, b]))
    assert first.ticket.train_code == "G200"  # 同优先级取历时最短


def test_strategy_next_candidate_returns_none_when_exhausted(sample_task):
    engine = QueryEngine(api=None, task=sample_task)  # type: ignore[arg-type]
    candidates = engine.build_candidates([_ticket()])
    strategy = PickStrategy(sample_task)
    assert strategy.next_candidate(candidates, exclude={strategy.key_of(candidates[0])}) is None


def test_strategy_describe_and_apply_config(sample_task):
    strategy = PickStrategy(sample_task)
    assert strategy.describe()["whitelist_mode"] is True
    new_task = TaskConfig(
        name="x", left_dates=["2026-10-01"], from_stations=["广州"], to_stations=["长沙"],
        seat_types=["硬座"], trains_whitelist=[],
    )
    strategy.apply_config(new_task)
    assert strategy.describe()["whitelist_mode"] is False
    assert strategy.seat_priority == ["硬座"]


# --------------------------------------------------------------------------- #
# 引擎构建
# --------------------------------------------------------------------------- #
@pytest.fixture()
def engine_env(app_context, mock_server, tmp_path):
    app_context.verify_service.set_solver(CallbackSolver(lambda i, h, k: "112,45", name="fixed"))
    user = app_context.accounts.add_user(UserConfig(username="test_user", password="test_pass"))
    assert user.login() is True
    user.is_checked = True

    task = app_context.save_task(
        TaskConfig(
            name="E2E 抢票任务",
            left_dates=["2026-10-01"],
            from_stations=["广州"],
            to_stations=["长沙"],
            trains_whitelist=["G6102"],
            seat_types=["二等座"],
            passengers=["张三"],
            strategy="direct",
            query_interval=0.3,
            max_retry=1,
        )
    )
    return app_context, mock_server, user, task


def _build_engine(ctx, task, *, standby=None, rate_limiter=None):
    states: list[tuple[str, str]] = []
    engine = GrabEngine(
        task,
        account_provider=ctx.accounts.allocate,
        order_service=OrderService(ctx.db, queue_max_polls=3),
        notifier=ctx.notifier,
        db=ctx.db,
        rate_limiter=rate_limiter or RateLimiter(interval=0.05),
        station_map=ctx.station_map,
        standby_engine=standby,
        on_state_change=lambda name, state, status: states.append((name, state)),
    )
    return engine, states


# --------------------------------------------------------------------------- #
# GR-T1 状态机 / GR-T3 发现余票立即下单 / GR-T6 成功后停止
# --------------------------------------------------------------------------- #
def test_engine_state_machine_reaches_success(engine_env):
    ctx, mock_server, user, task = engine_env
    engine, states = _build_engine(ctx, task)
    engine.start()
    engine.wait(timeout=40)

    assert engine.state == STATE_SUCCESS, f"状态={engine.state} error={engine.error}"
    assert engine.result is not None and engine.result.ok
    assert states[0][1] == "idle" or any(s == STATE_GRABBING for _, s in states)
    assert STATE_TEXT[STATE_SUCCESS] == "已抢到"
    assert engine.stats.order_attempts >= 1
    # 出票成功 → 任务自动停止（GR-T6）
    assert engine.running is False
    engine.stop()


def test_engine_reports_no_ticket_and_keeps_looping(engine_env):
    ctx, mock_server, user, task = engine_env
    mock_server.set_scenario("sold_out")
    engine, _ = _build_engine(ctx, task)
    engine.start()
    time.sleep(2.5)
    assert engine.running is True
    assert engine.stats.rounds >= 1
    assert engine.state in (STATE_GRABBING, STATE_READY)
    engine.stop()
    assert engine.state == STATE_STOPPED
    assert engine.running is False


def test_engine_stop_is_prompt(engine_env):
    ctx, mock_server, user, task = engine_env
    mock_server.set_scenario("sold_out")
    task.query_interval = 5.0
    engine, _ = _build_engine(ctx, task)
    engine.start()
    time.sleep(1.2)
    started = time.perf_counter()
    engine.stop()
    assert time.perf_counter() - started < 3.0, "停止指令应即时生效（可中断睡眠）"


def test_engine_handles_risk_scenario(engine_env):
    ctx, mock_server, user, task = engine_env
    mock_server.set_scenario("risk")
    limiter = RateLimiter(interval=0.05)
    engine, _ = _build_engine(ctx, task, rate_limiter=limiter)
    engine.start()
    time.sleep(3.0)
    engine.stop()
    # 风控场景下引擎不应崩溃，且限速器应记录到信号
    assert engine.state in (STATE_GRABBING, STATE_STOPPED, STATE_SUCCESS)
    assert limiter.snapshot()["risk_count"] >= 1 or engine.stats.risk_events >= 1


def test_engine_without_account_reports_error(app_context, mock_server):
    task = app_context.save_task(
        TaskConfig(
            name="无账号任务", left_dates=["2026-10-01"], from_stations=["广州"],
            to_stations=["长沙"], seat_types=["二等座"], passengers=["张三"],
        )
    )
    engine, _ = _build_engine(app_context, task)
    engine.prepare_timeout = 0.3  # 别让测试等满默认的 60 秒
    started = time.perf_counter()
    assert engine._prepare() is False  # noqa: SLF001
    assert time.perf_counter() - started < 5
    assert engine.state == STATE_ERROR
    engine.stop()


# --------------------------------------------------------------------------- #
# GR-T2 准点开售等待
# --------------------------------------------------------------------------- #
def test_engine_waits_for_sale_open(engine_env, monkeypatch):
    ctx, mock_server, user, task = engine_env
    from datetime import datetime, timedelta

    # 开售时间设在 3 秒后，验证「到点才发起首单」
    future = datetime.now() + timedelta(seconds=3.0)
    task.sale_open_time = future.strftime("%Y-%m-%d %H:%M:%S")
    task.sale_prepare_seconds = 1

    engine, states = _build_engine(ctx, task)
    engine.start()

    # 轮询等待进入待命态（比固定 sleep 更稳，不受机器负载影响）
    deadline = time.time() + 6
    while time.time() < deadline and engine.state != STATE_READY:
        time.sleep(0.05)
    assert engine.state == STATE_READY
    assert engine.stats.queries == 0, "待命期间不应发起查询"

    engine.wait(timeout=45)
    assert engine.state == STATE_SUCCESS, engine.error
    assert any(s == STATE_READY for _, s in states)


def test_parse_sale_time_variants(sample_task):
    engine, _ = _build_engine_stub(sample_task)
    sample_task.sale_open_time = "2026-10-01 08:00:00"
    assert engine._parse_sale_time() is not None  # noqa: SLF001
    sample_task.sale_open_time = "2026-10-01 08:00"
    assert engine._parse_sale_time() is not None  # noqa: SLF001
    sample_task.sale_open_time = "乱七八糟"
    assert engine._parse_sale_time() is None  # noqa: SLF001
    sample_task.sale_open_time = ""
    assert engine._parse_sale_time() is None  # noqa: SLF001


def _build_engine_stub(task):
    engine = GrabEngine(
        task,
        account_provider=lambda t: None,
        order_service=OrderService(None),
    )
    return engine, []


# --------------------------------------------------------------------------- #
# GR-T5 失败重试与退避
# --------------------------------------------------------------------------- #
def test_engine_marks_candidate_failed_after_retry(engine_env):
    ctx, mock_server, user, task = engine_env
    mock_server.set_scenario("sold_out")
    engine, _ = _build_engine(ctx, task)
    engine.start()
    time.sleep(2.0)
    engine.stop()
    # 无票 → 没有任何候选，因此不应产生下单尝试（这是「不空转」的关键断言）
    assert engine.stats.order_attempts == 0


def test_engine_switches_candidate_on_queue_failure(engine_env):
    ctx, mock_server, user, task = engine_env
    task.trains_whitelist = ["G6102", "G6140"]
    mock_server.set_scenario("queue_fail")
    engine, _ = _build_engine(ctx, task)
    engine.start()
    time.sleep(3.0)
    engine.stop()
    # 排队明确失败时应标记该候选并换下一个，而不是死磕
    assert engine.stats.order_failures >= 1
    assert engine.stats.order_attempts >= 1


def test_engine_latest_candidates_snapshot(engine_env):
    ctx, mock_server, user, task = engine_env
    engine, _ = _build_engine(ctx, task)
    engine.start()
    engine.wait(timeout=40)
    # 无论成功与否，候选快照都应是可序列化的列表（供看板展示）
    candidates = engine.latest_candidates()
    assert isinstance(candidates, list)
    engine.stop()


def test_failed_candidate_is_retried_after_cooldown(engine_env, monkeypatch):
    """回归测试：失败的候选只能「冷却」一段时间，不能被永久拉黑。

    场景：任务只配了一个车次，若该候选被永久拉黑，
    那么即使之后真的放了票，引擎也永远不会再下单 —— 表现就是
    「有票却一直不抢」，同时查询请求还在持续消耗。
    """
    ctx, mock_server, user, task = engine_env
    engine, _ = _build_engine(ctx, task)
    ticket = _ticket()
    key = engine.strategy.key_of(Candidate(ticket=ticket, seat=ticket.seats["二等座"], train_rank=0, seat_rank=0))

    # 刚失败 → 处于冷却期，会被跳过
    engine._failed_keys[key] = time.time()  # noqa: SLF001
    assert engine._active_failed_keys() == {key}  # noqa: SLF001

    # 冷却时间调为 0 → 立刻恢复可尝试（对应「票回来了要能再抢」）
    monkeypatch.setattr("ticket_helper.grab.engine.FAILED_CANDIDATE_COOLDOWN", 0.0)
    assert engine._active_failed_keys() == set()  # noqa: SLF001

    # 冷却只影响候选选择，不影响任务状态
    assert engine.state in ("idle", "stopped")
    engine.stop()


class _StubOrderService:
    """只返回预设结果的假下单服务，用于精确验证引擎的分支处理。"""

    def __init__(self, result) -> None:
        self.result = result
        self.calls = 0

    def reset_dedup(self) -> None:
        self.calls = 0

    def submit(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls += 1
        return self.result

    def on_success(self, callback) -> None:  # noqa: ANN001
        pass


def test_order_stage_risk_marks_candidate_and_throttles(engine_env):
    """下单阶段撞上风控：候选进入冷却 + 计入风控次数 + 驱动限速器降频。

    注：查询阶段的风控由主循环的 RiskControlError 分支处理；
    这里专门覆盖「查询正常但锁票/排队被风控」这条容易被漏掉的路径。
    """
    from ticket_helper.order.service import ORDER_FAILED, OrderResult

    ctx, mock_server, user, task = engine_env
    limiter = RateLimiter(interval=0.05)
    engine, _ = _build_engine(ctx, task, rate_limiter=limiter)
    engine.orders = _StubOrderService(
        OrderResult(ok=False, status=ORDER_FAILED, message="触发风控: 操作过于频繁，请稍后重试", retryable=True)
    )
    engine.user = user
    engine._passenger_cache = [  # noqa: SLF001 - 避免真的去调接口取乘车人
        {
            "passenger_name": "张三",
            "passenger_id_type_code": "1",
            "passenger_id_no": "110101199001011234",
            "passenger_type": "1",
        }
    ]

    ticket = _ticket()
    candidate = Candidate(ticket=ticket, seat=ticket.seats["二等座"], train_rank=0, seat_rank=0)
    engine._try_order(candidate)  # noqa: SLF001

    assert engine.stats.order_failures == 1
    assert engine.stats.risk_events == 1, "下单阶段的风控也必须计入"
    assert engine._active_failed_keys() == {engine.strategy.key_of(candidate)}  # noqa: SLF001
    assert limiter.snapshot()["risk_count"] >= 1, "下单风控必须驱动限速器降频"
    assert engine._pause_until > time.time()  # noqa: SLF001 - 会短暂暂停，避免继续撞墙
    engine.stop()


def test_order_stage_non_retryable_also_cools_candidate(engine_env):
    """非可重试失败（如排队明确失败）同样让候选冷却，避免原地反复重试。"""
    from ticket_helper.order.service import ORDER_FAILED, OrderResult

    ctx, mock_server, user, task = engine_env
    engine, _ = _build_engine(ctx, task)
    engine.orders = _StubOrderService(
        OrderResult(ok=False, status=ORDER_FAILED, message="排队失败: 余票已被抢完", retryable=False)
    )
    engine.user = user
    engine._passenger_cache = [{"passenger_name": "张三", "passenger_id_type_code": "1",
                                "passenger_id_no": "1", "passenger_type": "1"}]  # noqa: SLF001
    ticket = _ticket()
    candidate = Candidate(ticket=ticket, seat=ticket.seats["二等座"], train_rank=0, seat_rank=0)
    engine._try_order(candidate)  # noqa: SLF001

    assert engine.stats.order_failures == 1
    assert engine.stats.risk_events == 0
    assert engine._active_failed_keys() == {engine.strategy.key_of(candidate)}  # noqa: SLF001
    assert engine._pause_until == 0.0  # noqa: SLF001 - 非风控失败不需要全局暂停
    engine.stop()


def test_failed_keys_cleared_on_restart(engine_env):
    ctx, mock_server, user, task = engine_env
    engine, _ = _build_engine(ctx, task)
    engine._failed_keys["G6102|2026-10-01|GZQ|CWQ|二等座"] = time.time()  # noqa: SLF001
    engine.start()
    time.sleep(0.4)
    assert engine._failed_keys == {}  # noqa: SLF001
    engine.stop()


def test_engine_apply_config_hot_update(engine_env):
    ctx, mock_server, user, task = engine_env
    engine, _ = _build_engine(ctx, task)
    new_task = TaskConfig(
        name=task.name, id=task.id, left_dates=["2026-10-02"], from_stations=["广州"],
        to_stations=["长沙"], seat_types=["一等座"], passengers=["李四"], query_interval=2.0,
        trains_whitelist=["G6140"],
    )
    engine.apply_config(new_task)
    assert engine.task.query_interval == 2.0
    assert engine.strategy.seat_priority == ["一等座"]
    engine.stop()


def test_engine_status_payload_shape(engine_env):
    ctx, mock_server, user, task = engine_env
    engine, _ = _build_engine(ctx, task)
    status = engine.status()
    for key in ("task_id", "task_name", "state", "state_text", "running", "stats"):
        assert key in status
    engine.stop()


# --------------------------------------------------------------------------- #
# 策略语义：standby 必须真的是「仅候补」（回归：曾经等价于 dual，会下直购单）
# --------------------------------------------------------------------------- #
def _paths(mock_server) -> list[str]:
    return [r["path"] for r in mock_server.state.requests]


def _spy_ticks(engine) -> list[int]:
    """记下「直购取数→下单」那一轮（``_tick``）被调用的次数。

    这是判断「有没有跑捡漏循环」最直接的判据：``_tick`` 是直购路径的唯一入口，
    而它**与 mock 场景无关** —— 用「有没有真的下成单」来判断会因为
    「候补场景下本来就没票」而漏判（那正是这个 bug 藏了很久的原因）。
    """
    ticks: list[int] = []
    original = engine._tick  # noqa: SLF001

    def _spy() -> None:
        ticks.append(1)
        original()

    engine._tick = _spy  # type: ignore[method-assign]  # noqa: SLF001
    return ticks


def test_standby_strategy_never_enters_direct_loop(engine_env):
    """``strategy="standby"`` = 界面上的「仅候补」，**不允许跑直购捡漏循环**。

    回归：``GrabEngine._main_loop`` 曾经无条件进入捡漏循环，而全项目只有
    一处读 ``strategy``、且把 ``standby`` 与 ``dual`` 当成完全一样
    —— 结果是选了「仅候补」的任务照样会去下直购单，
    与两端界面上的文案正面冲突（选它的人往往正是不想产生直购订单）。
    """
    ctx, mock_server, user, task = engine_env
    # 候补场景：车次仅有「候补」席别，候补轨才找得到目标、链路才跑得起来
    mock_server.set_scenario("standby")
    task.strategy = "standby"

    from ticket_helper.standby.engine import StandbyEngine

    standby = StandbyEngine(task, account_provider=ctx.accounts.allocate,
                            notifier=ctx.notifier, db=ctx.db, poll_interval=15.0)
    engine, _ = _build_engine(ctx, task, standby=standby)
    ticks = _spy_ticks(engine)

    engine.start()
    time.sleep(4.0)
    engine.stop()

    assert ticks == [], "仅候补策略下不应进入直购捡漏循环（曾经就是在这里下错单）"
    assert engine.stats.order_attempts == 0, "仅候补策略不应发起任何直购下单"
    assert "/otn/leftTicket/submitOrderRequest" not in _paths(mock_server), \
        "仅候补策略下出现了直购锁票请求"
    # 必须仍然真的去跑了候补链路，否则「只跑候补」就变成了「什么都不跑」
    assert any(p.startswith("/otn/afterNate/") for p in _paths(mock_server)), \
        "仅候补策略下没有任何 afterNate 请求，候补轨没有真的跑起来"


def test_standby_strategy_skips_direct_even_when_tickets_are_available(engine_env):
    """有余票也不碰直购 —— 这才是原始缺陷会「真下单」的那个场景。"""
    ctx, mock_server, user, task = engine_env
    mock_server.set_scenario("has_ticket")     # 一等/二等座都有票，直购必然买得到
    task.strategy = "standby"

    from ticket_helper.standby.engine import StandbyEngine

    standby = StandbyEngine(task, account_provider=ctx.accounts.allocate,
                            notifier=ctx.notifier, db=ctx.db, poll_interval=15.0)
    engine, _ = _build_engine(ctx, task, standby=standby)
    ticks = _spy_ticks(engine)

    engine.start()
    time.sleep(4.0)
    engine.stop()

    assert ticks == [], "仅候补策略在有票时更不该去下直购单"
    assert "/otn/leftTicket/submitOrderRequest" not in _paths(mock_server)


def test_dual_strategy_still_places_direct_order(engine_env):
    """``strategy="dual"`` = 「候补 + 捡漏双轨」，直购必须照常发生。"""
    ctx, mock_server, user, task = engine_env
    mock_server.set_scenario("has_ticket")
    task.strategy = "dual"

    from ticket_helper.standby.engine import StandbyEngine

    standby = StandbyEngine(task, account_provider=ctx.accounts.allocate,
                            notifier=ctx.notifier, db=ctx.db, poll_interval=15.0)
    engine, _ = _build_engine(ctx, task, standby=standby)
    engine.start()
    engine.wait(timeout=40)
    engine.stop()

    assert engine.stats.order_attempts >= 1, "双轨策略必须照常走直购"
    assert engine.state == STATE_SUCCESS, f"状态={engine.state} error={engine.error}"


def test_standby_only_without_standby_engine_reports_error(engine_env):
    """配置成仅候补却没有候补轨 → 必须**报错**，不能静默空转。

    静默空转会被使用者当成「程序卡住了」，比直接报错难排查得多。
    """
    ctx, mock_server, user, task = engine_env
    task.strategy = "standby"
    engine, _ = _build_engine(ctx, task, standby=None)
    engine.start()
    engine.wait(timeout=15)

    assert engine.state == STATE_ERROR
    assert "仅候补" in engine.error or "候补" in engine.error


def test_direct_strategy_does_not_start_standby_track(engine_env):
    """``direct`` 不该启动候补轨（少打一路 afterNate 请求）。"""
    ctx, mock_server, user, task = engine_env
    mock_server.set_scenario("has_ticket")
    task.strategy = "direct"

    from ticket_helper.standby.engine import StandbyEngine

    standby = StandbyEngine(task, account_provider=ctx.accounts.allocate,
                            notifier=ctx.notifier, db=ctx.db, poll_interval=15.0)
    engine, _ = _build_engine(ctx, task, standby=standby)
    engine.start()
    engine.wait(timeout=40)
    engine.stop()

    assert not any(p.startswith("/otn/afterNate/") for p in _paths(mock_server)), \
        "direct 策略不应触发候补链路"


# --------------------------------------------------------------------------- #
# 乘车日期已过去：**拒绝启动**，而不是报"风控"并空转
#
# 真机上 12306 对「已过去 / 还没开售」的日期回的是与风控**完全相同**的
# HTML 错误页，所以旧代码把「任务日期填成去年」报成了「🚨 风控告警」：
# 使用者去降频、换账号、干等，全部无效。判据落在「有没有发请求」上，
# 因为「报错文案对不对」这件事光看状态码是看不出来的。
# --------------------------------------------------------------------------- #
def test_all_dates_past_refuses_to_start_with_actionable_message(engine_env):
    """全部日期都已过去 → 直接报错，且**一次接口都不打**。"""
    ctx, mock_server, user, task = engine_env
    task.left_dates = ["2025-10-03"]
    engine, _ = _build_engine(ctx, task)
    engine.prepare_timeout = 0.3

    assert engine._prepare() is False  # noqa: SLF001
    assert engine.state == STATE_ERROR
    assert "已经过去" in engine.error, engine.error
    assert "2025-10-03" in engine.error
    # 关键：没有去查（不是"查了但被当成风控"）。
    # 只看余票相关请求 —— 夹具里的登录请求与这件事无关。
    queried = [r for r in mock_server.state.requests if "leftTicket" in r["path"]]
    assert queried == [], f"不该发任何余票请求，实际 {queried}"
    assert engine.stats.queries == 0
    assert engine.stats.risk_events == 0
    engine.stop()


def test_partially_past_dates_still_start_and_skip(engine_env):
    """只有部分日期过期 → 照常启动，只跳过过期的那天。"""
    ctx, mock_server, user, task = engine_env
    task.left_dates = ["2025-10-03", "2026-10-01"]
    engine, _ = _build_engine(ctx, task)
    assert engine._prepare() is True  # noqa: SLF001
    engine.stop()


def test_not_on_sale_does_not_count_as_risk(engine_env):
    """还没开售 → 不记风控、不降频（它不是限流，等到开售自然就好了）。

    走**真正的主循环**：这条行为是 ``_main_loop`` 的分支，只调 ``_tick`` 测不到
    （那样异常会直接冒出来，等于什么都没验证）。
    """
    ctx, mock_server, user, task = engine_env
    task.left_dates = ["2026-11-01"]
    task.query_interval = 0.05
    limiter = RateLimiter(interval=0.05)
    engine, _ = _build_engine(ctx, task, rate_limiter=limiter)

    engine.start()
    time.sleep(2.0)
    engine.stop()

    assert engine.stats.queries >= 1, "应当真的查过"
    assert engine.stats.risk_events == 0, "日期未开售不是风控"
    assert "还没开售" in engine.stats.last_error, engine.stats.last_error
    assert limiter.snapshot()["risk_count"] == 0, "不该让限速器降频"
    # 也不该按 query_interval 猛冲：等开售是分钟级的事，一秒一次只是白挨拒绝
    assert engine.stats.queries <= 3, f"不该高频重试，实际查了 {engine.stats.queries} 次"
    engine.stop()
