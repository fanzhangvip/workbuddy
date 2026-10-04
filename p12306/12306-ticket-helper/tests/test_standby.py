"""智能候补测试（HB-T1 ~ HB-T5）。"""

from __future__ import annotations

import time

import pytest

from ticket_helper.config.settings import (
    STANDBY_MAX_COMBOS_PER_ORDER,
    STANDBY_MAX_DATES_PER_ORDER,
    STANDBY_MAX_ORDERS,
    TaskConfig,
    UserConfig,
)
from ticket_helper.errors import StandbyLimitError
from ticket_helper.standby.engine import (
    STANDBY_FAILED,
    STANDBY_QUEUING,
    STANDBY_SUCCESS,
    StandbyEngine,
)
from ticket_helper.verify.service import CallbackSolver


def _task(**kwargs) -> TaskConfig:
    base = dict(
        name="候补任务",
        left_dates=["2026-10-01"],
        from_stations=["广州"],
        to_stations=["长沙"],
        trains_whitelist=["G6102", "G6140"],
        seat_types=["二等座"],
        passengers=["张三"],
        strategy="dual",
    )
    base.update(kwargs)
    return TaskConfig(**base)


# --------------------------------------------------------------------------- #
# HB-T1 上限约束（纯逻辑）
# --------------------------------------------------------------------------- #
def test_plan_combos_trims_dates_to_official_limit():
    task = _task(left_dates=[f"2026-10-0{i}" for i in range(1, 6)])  # 5 个日期
    engine = StandbyEngine(task, account_provider=lambda t: None)
    plan = engine.plan_combos(["G6102"])
    assert len(plan.dates) == STANDBY_MAX_DATES_PER_ORDER
    assert any("乘车日期" in w for w in plan.warnings)
    assert plan.combo_count <= STANDBY_MAX_COMBOS_PER_ORDER


def test_plan_combos_trims_combos_to_official_limit():
    task = _task(
        left_dates=["2026-10-01", "2026-10-02", "2026-10-03"],
        trains_whitelist=[f"G{i:04d}" for i in range(1, 26)],  # 25 车次
    )
    engine = StandbyEngine(task, account_provider=lambda t: None)
    plan = engine.plan_combos(task.trains_whitelist)
    assert plan.combo_count == STANDBY_MAX_COMBOS_PER_ORDER
    assert any("组合" in w for w in plan.warnings)


def test_plan_combos_empty_raises():
    task = _task(left_dates=[], from_stations=[], to_stations=[])
    engine = StandbyEngine(task, account_provider=lambda t: None)
    with pytest.raises(StandbyLimitError):
        engine.plan_combos(["G6102"])


def test_quota_blocks_when_six_orders_active(app_context, mock_server):
    for i in range(STANDBY_MAX_ORDERS):
        app_context.db.save_standby_order({"task_id": i + 1, "order_no": f"HB{i}", "status": "queuing"})

    engine = StandbyEngine(_task(), account_provider=lambda t: None, db=app_context.db)
    with pytest.raises(StandbyLimitError) as exc:
        engine.check_quota()
    assert str(STANDBY_MAX_ORDERS) in str(exc.value)


def test_quota_returns_remaining(app_context, mock_server):
    app_context.db.save_standby_order({"task_id": 1, "order_no": "HB1", "status": "queuing"})
    engine = StandbyEngine(_task(), account_provider=lambda t: None, db=app_context.db)
    assert engine.check_quota() == STANDBY_MAX_ORDERS - 1


# --------------------------------------------------------------------------- #
# 构建带账号的环境
# --------------------------------------------------------------------------- #
@pytest.fixture()
def standby_env(app_context, mock_server):
    app_context.verify_service.set_solver(CallbackSolver(lambda i, h, k: "112,45", name="fixed"))
    user = app_context.accounts.add_user(UserConfig(username="test_user", password="test_pass"))
    assert user.login() is True
    user.is_checked = True
    task = app_context.save_task(_task())
    return app_context, mock_server, user, task


# --------------------------------------------------------------------------- #
# HB-T2 自动创建候补订单
# --------------------------------------------------------------------------- #
def test_standby_creates_order(standby_env):
    ctx, mock_server, user, task = standby_env
    mock_server.set_scenario("standby")

    engine = StandbyEngine(task, account_provider=ctx.accounts.allocate, notifier=ctx.notifier, db=ctx.db,
                           poll_interval=15)
    assert engine._create_order() is True  # noqa: SLF001
    assert engine.status == STANDBY_QUEUING

    # 官方**不再返回候补单号**（afterNate/confirmHB 只回 flag+isAsync），
    # 生效判据是排队接口的 status==1。所以这里不能断言 hb_order_id 有值 ——
    # 以前那个 "HB..." 是 mock 编出来的，真机上根本没有。
    assert engine.hb_order_id == "", "官方不给候补单号，不要造一个假的出来"
    assert engine.queue.get("status") == 1, engine.queue
    # 核验真信号（chechFace.face_flag）与成功率都要落下来，界面上要看
    assert engine.face.get("face_flag") is True, engine.face
    assert engine.success_rate.get("info") == "候补人数较少", engine.success_rate

    rows = ctx.db.list_standby_orders(task_id=task.id)
    assert rows and rows[0]["status"] == STANDBY_QUEUING
    assert rows[0]["combos"], "候补组合应一并落库"


def test_standby_notifies_on_submit(standby_env):
    ctx, mock_server, user, task = standby_env
    mock_server.set_scenario("standby")
    seen: list[dict] = []
    ctx.notifier.subscribe_web(seen.append)

    engine = StandbyEngine(task, account_provider=ctx.accounts.allocate, notifier=ctx.notifier, db=ctx.db)
    engine._create_order()  # noqa: SLF001
    time.sleep(0.4)
    assert any("候补" in (item.get("title") or "") for item in seen)


# --------------------------------------------------------------------------- #
# AC-07 未核验账号被拦截
# --------------------------------------------------------------------------- #
def test_standby_rejects_unverified_account(standby_env):
    ctx, mock_server, user, task = standby_env
    user.is_checked = False
    # 让 detect_verified 也无法确认核验（mock 的 initQueryUserInfoApi 返回 Y，
    # 这里通过替换 api 来模拟「未核验」的真实响应）
    user.api.get_user_info = lambda: {"user_name": "", "isUserVerified": "N"}  # type: ignore[method-assign]

    engine = StandbyEngine(task, account_provider=ctx.accounts.allocate, notifier=ctx.notifier, db=ctx.db)
    assert engine._create_order() is False  # noqa: SLF001
    assert engine.status == STANDBY_FAILED
    assert "核验" in engine.error


# --------------------------------------------------------------------------- #
# HB-T3 / HB-T4 进度轮询与兑现
# --------------------------------------------------------------------------- #
def test_standby_monitor_fulfills_and_notifies(standby_env):
    ctx, mock_server, user, task = standby_env
    mock_server.set_scenario("standby")
    seen: list[dict] = []
    ctx.notifier.subscribe_web(seen.append)

    engine = StandbyEngine(task, account_provider=ctx.accounts.allocate, notifier=ctx.notifier, db=ctx.db,
                           poll_interval=0.2)
    # 构造器里的 15s 下限是**给真机用的**（防止把突发做大）；测试里直接改属性绕开，
    # 否则一条用例要跑 45 秒。
    engine.poll_interval = 0.05
    engine.order_check_interval = 0.05
    engine._create_order()  # noqa: SLF001
    engine._monitor_loop()  # noqa: SLF001 - 同步跑，mock 第 3 次查询即兑现

    assert engine.status == STANDBY_SUCCESS
    assert engine.stats["success"] == 1
    rows = ctx.db.list_standby_orders(task_id=task.id)
    assert any(r["status"] == STANDBY_SUCCESS for r in rows)
    time.sleep(0.3)
    assert any(item.get("critical") for item in seen), "候补兑现应触发重大事件通知"


def test_standby_monitor_stops_when_grab_succeeded(standby_env):
    ctx, mock_server, user, task = standby_env
    mock_server.set_scenario("standby")

    class FakeGrab:
        state = "success"

    engine = StandbyEngine(task, account_provider=ctx.accounts.allocate, notifier=ctx.notifier, db=ctx.db,
                           poll_interval=0.2)
    engine.poll_interval = 0.05
    engine._grab_engine = FakeGrab()  # noqa: SLF001
    engine.hb_order_id = "HB-FAKE"
    engine._monitor_loop()  # noqa: SLF001
    # 捡漏已成功 → 监控循环应直接返回，不做任何状态变更
    assert engine.status != STANDBY_SUCCESS


# --------------------------------------------------------------------------- #
# HB-T5 双轨：成其一即停，并取消候补避免重复占票
# --------------------------------------------------------------------------- #
def test_dual_track_cancels_standby_when_grab_wins(standby_env):
    ctx, mock_server, user, task = standby_env
    mock_server.set_scenario("standby")

    engine = StandbyEngine(task, account_provider=ctx.accounts.allocate, notifier=ctx.notifier, db=ctx.db,
                           poll_interval=5, cancel_on_stop=True)
    assert engine._create_order() is True  # noqa: SLF001
    # 候补单号只能从「未完成订单」里认领（官方提交时不返回），
    # 这里直接取 mock 侧生成的那一单来验证取消逻辑
    order_id = next(iter(mock_server.state.standby_orders))
    engine.hb_order_id = order_id

    engine.stop(reason="捡漏已成功出票")
    assert engine.status == "cancelled"
    rows = ctx.db.list_standby_orders(task_id=task.id)
    assert any(r["order_no"] == order_id and r["status"] == "cancelled" for r in rows)

    # mock 侧订单也应为已取消状态
    state = mock_server.state.standby_orders.get(order_id)
    assert state is not None and state["status"] == "cancelled"


def test_dual_track_keeps_standby_when_configured(standby_env):
    ctx, mock_server, user, task = standby_env
    mock_server.set_scenario("standby")
    engine = StandbyEngine(task, account_provider=ctx.accounts.allocate, notifier=ctx.notifier, db=ctx.db,
                           cancel_on_stop=False)
    engine._create_order()  # noqa: SLF001
    order_id = next(iter(mock_server.state.standby_orders))
    engine.stop(reason="手动停止")
    state = mock_server.state.standby_orders.get(order_id)
    assert state is not None and state["status"] == "queuing", "配置为不取消时不应动候补单"


def test_start_only_for_standby_or_dual(standby_env):
    ctx, mock_server, user, task = standby_env
    direct = _task(strategy="direct")
    engine = StandbyEngine(direct, account_provider=ctx.accounts.allocate)
    assert engine.start(grab_engine=None) is False


def test_standby_status_dict(standby_env):
    ctx, mock_server, user, task = standby_env
    engine = StandbyEngine(task, account_provider=ctx.accounts.allocate, db=ctx.db)
    payload = engine.status_dict()
    for key in ("task_id", "status", "plan", "stats", "poll_interval"):
        assert key in payload


# --------------------------------------------------------------------------- #
# AC-07 补充：「判不出来」绝不能被当成「未核验」
# --------------------------------------------------------------------------- #
def test_standby_proceeds_when_verify_state_unknown(standby_env):
    """官方不返回核验字段时必须**放行**，而不是拦下。

    真机实测（scripts/diagnose_verify.py，2026-09-29）：
    ``initQueryUserInfoApi`` 与 ``initMy12306Api`` 的响应里**都没有**任何核验字段，
    所以 ``verify_known`` 永远是 False。原实现写成「not is_checked 就拦」，
    于是这个「判不出来」被当成「未核验」，一个完全正常的账号被永久挡在候补之外，
    提示还写着「未完成人证核验」—— 使用者只会以为自己的账号被 12306 限制了。
    """
    ctx, mock_server, user, task = standby_env
    mock_server.set_scenario("standby")
    user.is_checked = False
    # 复刻真机：接口正常返回，但**没有**任何核验字段
    user.api.get_user_info = lambda: {  # type: ignore[method-assign]
        "user_name": "樊章", "sexCode": "M", "isCheckEmail": "Y"
    }

    engine = StandbyEngine(task, account_provider=ctx.accounts.allocate, notifier=ctx.notifier, db=ctx.db)
    assert engine._create_order() is True, "判不出来就放行，让官方去判"
    assert user.verify_known is False
    assert engine.status == STANDBY_QUEUING


def test_standby_block_message_tells_where_to_click(standby_env):
    """真被拦时的文案必须**能照着做**：要写清 12306 App 里的路径。

    原来只有一句「请先在 12306 完成核验」——使用者不知道去哪儿点，
    于是只能回来问「这个人证核验要怎么做」。
    """
    ctx, mock_server, user, task = standby_env
    user.is_checked = False
    user.api.get_user_info = lambda: {"user_name": "樊章", "isUserVerified": "N"}  # type: ignore[method-assign]
    seen: list[dict] = []
    ctx.notifier.subscribe_web(seen.append)

    engine = StandbyEngine(task, account_provider=ctx.accounts.allocate, notifier=ctx.notifier, db=ctx.db)
    assert engine._create_order() is False  # noqa: SLF001
    assert engine.status == STANDBY_FAILED
    assert "核验" in engine.error

    time.sleep(0.4)
    bodies = [item.get("body", "") for item in seen]
    assert any("人证核验" in body for body in bodies), bodies
    assert any("我的" in body and "人证核验" in body for body in bodies), (
        f"拦截通知要写清 App 里的入口，而不是只说「请先完成核验」：{bodies}"
    )
    assert any("kyfw.12306.cn" in body for body in bodies), "顺带给官方说明链接"


def test_verify_state_module_distinguishes_four_states():
    """四态语义的单一来源：两个端 + 候补引擎都必须走这里。"""
    from ticket_helper.account import verify

    assert verify.state_of({"is_checked": True}) == verify.STATE_VERIFIED
    assert verify.state_of({"is_checked": False, "verified_checked": False}) == verify.STATE_UNCHECKED
    # 检测过、但官方没给字段 → 无法判定（**不是**未核验）
    assert verify.state_of({"is_checked": False, "verified_checked": True, "verify_known": False}) == (
        verify.STATE_UNKNOWN
    )
    assert verify.state_of({"is_checked": False, "verified_checked": True, "verify_known": True}) == (
        verify.STATE_UNVERIFIED
    )

    # 只有「明确未核验」才拦
    assert verify.should_block({"is_checked": False, "verified_checked": True, "verify_known": True}) is True
    for info in (
        {"is_checked": True},
        {"is_checked": False, "verified_checked": False},
        {"is_checked": False, "verified_checked": True, "verify_known": False},
        {},
    ):
        assert verify.should_block(info) is False, f"{info} 不该被拦"

    # 也接受直接传 User 对象（候补引擎就是那么调的）
    class _FakeUser:
        is_checked = False
        verified_checked = True
        verify_known = False

    assert verify.state_of(_FakeUser()) == verify.STATE_UNKNOWN
    assert verify.should_block(_FakeUser()) is False

    assert verify.label_of({"is_checked": False, "verified_checked": True, "verify_known": False}) == "无法判定"


# --------------------------------------------------------------------------- #
# afterNate 链路：顺序、核验真信号、参数格式（2026-09-29 真机对齐）
# --------------------------------------------------------------------------- #
def test_standby_follows_the_real_after_nate_chain(standby_env):
    """候补必须按真机的接口顺序打，一步都不能少、也不能换路径。

    原来的实现只打一个 `/otn/confirmPassenger/confirmHB`，真机回 **HTTP 405**
    （该路径不存在）；正确家族是 `/otn/afterNate/`，而且是一串调用：

        chechFace → getSuccessRate → submitOrderRequest
        → passengerInitApi → getQueueNum → confirmHB → queryQueue

    顺带钉死：**绝不碰旧的 confirmPassenger/confirmHB**（那是 405）。
    """
    ctx, mock_server, user, task = standby_env
    mock_server.set_scenario("standby")
    mock_server.state.requests.clear()

    engine = StandbyEngine(task, account_provider=ctx.accounts.allocate, notifier=ctx.notifier, db=ctx.db)
    assert engine._create_order() is True  # noqa: SLF001

    paths = [r["path"] for r in mock_server.state.requests if "afterNate" in r["path"] or "confirmHB" in r["path"]]
    order = [p for p in paths if p.startswith("/otn/afterNate") or p.endswith("confirmHB")]
    expected = [
        "/otn/afterNate/chechFace",
        "/otn/afterNate/getSuccessRate",
        "/otn/afterNate/submitOrderRequest",
        "/otn/afterNate/passengerInitApi",
        "/otn/afterNate/getQueueNum",
        "/otn/afterNate/confirmHB",
        "/otn/afterNate/queryQueue",
    ]
    assert order == expected, f"链路顺序不对：{order}"
    assert "/otn/confirmPassenger/confirmHB" not in paths, "不许再打旧路径（真机 405）"


def test_legacy_confirm_hb_path_is_really_405(standby_env):
    """把「旧路径会 405」这件事钉在 mock 上。

    这样万一日后有人把路径改回去，**离线测试立刻红**，
    而不是等到真机上收到一句「响应不是合法 JSON (HTTP 405)」——
    那句提示完全指不出问题在哪。
    """
    ctx, mock_server, user, task = standby_env
    response = user.client.post("/otn/confirmPassenger/confirmHB", data={"_json_att": ""}, allow_retry=False)
    assert response.status_code == 405


def test_face_flag_false_blocks_standby_with_guidance(standby_env):
    """`chechFace.face_flag=false` 才是「确实需要核验」的真凭证。

    用户信息接口根本不返回核验字段（真机实测），所以核验的最终判据只能是这里。
    被拦时的文案必须**能照着做**（写清 12306 App 里的入口），否则重试一万次也没用。
    """
    ctx, mock_server, user, task = standby_env
    mock_server.set_scenario("face_required")
    seen: list[dict] = []
    ctx.notifier.subscribe_web(seen.append)

    engine = StandbyEngine(task, account_provider=ctx.accounts.allocate, notifier=ctx.notifier, db=ctx.db)
    assert engine._create_order() is False  # noqa: SLF001
    assert engine.status == STANDBY_FAILED
    assert "核验" in engine.error
    assert mock_server.state.face_flag is False

    time.sleep(0.4)
    bodies = [item.get("body", "") for item in seen]
    assert any("人证核验" in b and "我的" in b for b in bodies), f"拦截通知要给出可照做的入口：{bodies}"


def test_self_backend_passenger_info_is_6_segments_legacy(monkeypatch):
    """**回退后端**（``self``）的 ``passengerInfo`` 是「6 段 + 分号」。

    ⚠️ 这条**不再自称「matches official shape」**（2026-09-29 改）。
    原因：上游 ``testerSunshine/12306`` 的候补分支拼的是 **5 段**
    （``1#name#1#id#allEncStr;``，见 ``reference/upstream/testerSunshine_standby/
    GetPassengerDTOs.py``），本项目旧写法是 6 段，**谁对尚无真机证据**。
    把「我们的猜测」说成「官方格式」，正是本项目吃过亏的那种自证。

    所以本用例只做一件事：**钉住回退后端的输出**，保证「一键回退真的能回到从前」。
    段数之争交给真机探针（``docs/内核替换对照报告.md`` §4.3 的 B 级信号）。
    """
    from ticket_helper import kernel

    monkeypatch.setenv(kernel.ENV_VAR, "self")
    """候补的 passengerInfo 是「6 段 + 分号」，和普通下单的格式完全不同。

    官方格式：``<票种>#<姓名>#<证件类型>#<证件号>#<allEncStr>#<是否60岁 0/1>;``
    照抄普通购票的 ``passengerTicketStr`` 会被判「乘车人信息格式不正确」。
    """
    from ticket_helper.errors import InterError
    from ticket_helper.inter.api import TicketApi

    info = TicketApi.build_hb_passenger_info(
        [
            {"passenger_name": "张三", "passenger_id_type_code": "1", "passenger_id_no": "110101199001011234",
             "passenger_type": "1", "born_date": "1988-07-23", "allEncStr": "enc-1"},
            {"passenger_name": "李四", "passenger_id_type_code": "1", "passenger_id_no": "110101196002022345",
             "passenger_type": "1", "born_date": "1960-02-11", "allEncStr": "enc-2"},
        ]
    )
    chunks = [c for c in info.split(";") if c]
    assert len(chunks) == 2
    for chunk in chunks:
        assert len(chunk.split("#")) == 6, chunk
    assert info.endswith(";")
    assert chunks[0] == "1#张三#1#110101199001011234#enc-1#0"
    assert chunks[1].endswith("#1"), "1960 年生 → 60 岁以上标记位应为 1"

    # 缺官方加密串时必须**明确报错**，而不是提交一个官方必然拒绝的串
    with pytest.raises(InterError) as exc:
        TicketApi.build_hb_passenger_info([{"passenger_name": "王五", "allEncStr": ""}])
    assert "allEncStr" in str(exc.value)


def test_order_query_failure_is_visible_instead_of_swallowed(standby_env, caplog):
    """★ 防的是：把「查订单失败」降回 `debug` 级。

    2026-09-30 的真实事故链（写下来，因为它每一环都静默）：

    1. 我往 `TicketApi.__init__` 插方法时，把 `self._order_combo_hint = None`
       挤进了新方法体（`return` 之后），成了**永不执行的死代码**；
    2. 于是 `query_order_history` 每次调用都抛 `AttributeError`；
    3. 而这行 `log.debug` 把它吞了 —— 默认日志级别下**完全看不见**；
    4. 候补监控**永远等不到「已兑现」，也不会报错** →
       同步跑的 `_monitor_loop()` 变成死循环，一条用例卡 20 分钟才被发现。

    「查订单失败」等价于「兑现判定失效」，必须是 WARNING 才可能被人看见。
    """
    import logging

    ctx, mock_server, user, task = standby_env
    engine = StandbyEngine(
        task, account_provider=ctx.accounts.allocate,
        notifier=ctx.notifier, db=ctx.db, poll_interval=0.2,
    )

    def boom(*args, **kwargs):
        raise RuntimeError("模拟：订单接口炸了")

    user.api.query_order_history = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        fulfilled = engine._check_fulfilled_by_order(user)

    assert fulfilled is False
    assert any("候补兑现检查" in record.getMessage() for record in caplog.records), (
        "查订单失败必须留下 WARNING 级痕迹，否则它会变成「无声地永不兑现」"
    )


def test_order_query_failure_is_visible_instead_of_swallowed(standby_env, caplog):
    """★ 防的是：把「查订单失败」降回 `debug` 级。

    2026-09-30 的真实事故链（写下来，因为它每一环都静默）：

    1. 我往 `TicketApi.__init__` 插方法时，把 `self._order_combo_hint = None`
       挤进了新方法体（`return` 之后），成了**永不执行的死代码**；
    2. 于是 `query_order_history` 每次调用都抛 `AttributeError`；
    3. 而这行 `log.debug` 把它吞了 —— 默认日志级别下**完全看不见**；
    4. 候补监控**永远等不到「已兑现」，也不会报错** →
       同步跑的 `_monitor_loop()` 变成死循环，一条用例卡 20 分钟才被发现。

    「查订单失败」等价于「兑现判定失效」，必须是 WARNING 才可能被人看见。
    """
    import logging

    ctx, mock_server, user, task = standby_env
    engine = StandbyEngine(
        task, account_provider=ctx.accounts.allocate,
        notifier=ctx.notifier, db=ctx.db, poll_interval=0.2,
    )

    def boom(*args, **kwargs):
        raise RuntimeError("模拟：订单接口炸了")

    user.api.query_order_history = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        fulfilled = engine._check_fulfilled_by_order(user)

    assert fulfilled is False
    assert any("候补兑现检查" in record.getMessage() for record in caplog.records), (
        "查订单失败必须留下 WARNING 级痕迹，否则它会变成「无声地永不兑现」"
    )
