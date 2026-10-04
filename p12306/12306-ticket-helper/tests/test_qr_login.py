"""扫码登录测试（AC-08）。

分三层：
1. **协议解析**（纯函数）——状态码映射、异常响应的兜底；
2. **接口层**——create / check / finish 三个调用；
3. **会话与账号集成**——状态流转、账号采纳、落库、幂等、过期、取消、无密码账号行为。
全部跑在 mock 上，不触碰真实 12306。
"""

from __future__ import annotations

import time

import pytest

from ticket_helper.account.qrlogin import QrLoginSession, QrLoginStatus
from ticket_helper.config.settings import UserConfig
from ticket_helper.errors import ResponseParseError, ValidationError
from ticket_helper.inter.parser import (
    QR_STATE_CONFIRMED,
    QR_STATE_EXPIRED,
    QR_STATE_FAILED,
    QR_STATE_SCANNED,
    QR_STATE_UNKNOWN,
    QR_STATE_WAITING,
    parse_qr_check,
    parse_qr_create,
)


def _build_api_user(app_context, username: str = "qr_tmp"):
    """构造一个不注册进账号表的临时 User（扫码会话内部就是这么做的）。"""
    return app_context.accounts.build_user(UserConfig(username=username, password=""))


# =========================================================================== #
# 一、协议解析
# =========================================================================== #
@pytest.mark.parametrize(
    "code, expected",
    [
        ("0", QR_STATE_WAITING),
        ("1", QR_STATE_SCANNED),
        ("2", QR_STATE_CONFIRMED),
        ("3", QR_STATE_EXPIRED),
    ],
)
def test_parse_qr_check_maps_official_codes(code, expected):
    state, uamtk, message = parse_qr_check({"result_code": code, "result_message": f"msg-{code}"})
    assert state == expected
    assert message == f"msg-{code}"


def test_parse_qr_check_treats_unknown_code_as_unknown_not_failed():
    """未知状态码必须是 unknown（非终态），不能判失败。

    回归用例：线上真实故障 —— 官方 ``checkqr`` 返回
    ``{"status": false, "messages": ["操作失败"]}``（没有 result_code），
    早期实现把它归为 failed，导致**用户还没扫码就被提示"扫码登录失败"**，
    扫完之后界面再也不会有任何变化。
    """
    state, _, message = parse_qr_check({"status": False, "messages": ["操作失败"]})
    assert state == QR_STATE_UNKNOWN
    assert "操作失败" in message

    # 完全没有状态字段同样按 unknown 处理
    assert parse_qr_check({"result_message": "操作失败"})[0] == QR_STATE_UNKNOWN
    assert parse_qr_check({})[0] == QR_STATE_UNKNOWN
    assert parse_qr_check(None)[0] == QR_STATE_UNKNOWN


def test_qr_state_of_recognizes_known_codes_only():
    from ticket_helper.inter.parser import qr_state_of

    assert qr_state_of({"result_code": "1"}) == QR_STATE_SCANNED
    assert qr_state_of({"status": "2"}) == QR_STATE_CONFIRMED
    assert qr_state_of({"status": False}) is None      # 错误信封，不可识别
    assert qr_state_of({"result_code": "9"}) is None   # 未知码
    assert qr_state_of({}) is None
    assert qr_state_of(None) is None


def test_qr_check_uses_post_then_falls_back_to_get(app_context, mock_server):
    """官方 checkqr 是 POST；响应不可识别时应回退 GET。"""
    user = _build_api_user(app_context)
    ticket = user.api.create_qr_login()

    user.api.check_qr_login(ticket.uuid)
    methods = [
        r["payload"].get("method")
        for r in mock_server.state.requests
        if r["path"] == "/passport/web/checkqr" and isinstance(r.get("payload"), dict)
    ]
    assert methods == ["POST"], f"正常响应只应发一次 POST，实际={methods}"

    # 让服务端返回不可识别的错误信封 → 应回退 GET 再试一次
    mock_server.state.requests.clear()
    mock_server.set_scenario("qr_bad_response")
    state, _, _ = user.api.check_qr_login(ticket.uuid)
    methods = [
        r["payload"].get("method")
        for r in mock_server.state.requests
        if r["path"] == "/passport/web/checkqr" and isinstance(r.get("payload"), dict)
    ]
    assert methods == ["POST", "GET"], f"不可识别时应回退 GET，实际={methods}"
    assert state == QR_STATE_UNKNOWN


# =========================================================================== #
# 三之二、异常响应不能终结会话（线上故障回归）
# =========================================================================== #
def test_bad_response_keeps_session_alive(app_context, mock_server):
    """服务端返回异常信封时，会话必须存活 —— 这是「还没扫就提示失败」的根因。"""
    mock_server.set_scenario("qr_bad_response")
    status = app_context.start_qr_login()

    for _ in range(3):
        result = app_context.qr_login_status(status.session_id)
        assert result.done is False, "异常响应不应终结会话"
        assert result.state == QR_STATE_UNKNOWN
        assert "操作失败" in result.message, "应把服务端原始说明透出，便于排查"

    session = app_context.accounts.get_qr_session(status.uuid)
    assert session.unknown_responses == 3


def test_session_recovers_after_transient_bad_responses(app_context, mock_server):
    """异常响应过后服务端恢复正常 → 依然能扫码成功并建号。

    这正是线上问题的关键：一旦被误判为 failed，用户扫完码也永远不会有反应。
    """
    mock_server.set_scenario("qr_bad_response")
    status = app_context.start_qr_login()
    for _ in range(3):
        assert app_context.qr_login_status(status.session_id).done is False

    mock_server.set_scenario("has_ticket")
    mock_server.set_qr_state("confirmed")
    final = app_context.qr_login_status(status.session_id)

    assert final.state == QR_STATE_CONFIRMED
    assert final.username
    user = app_context.accounts.get(final.username)
    assert user is not None and user.is_login is True


def test_bad_responses_eventually_fail_with_actionable_message(app_context, mock_server):
    """但也不能无限空转：连续异常超过上限要给出可操作的结论。"""
    from ticket_helper.account.qrlogin import MAX_UNKNOWN_RESPONSES

    mock_server.set_scenario("qr_bad_response")
    status = app_context.start_qr_login()

    result = None
    for _ in range(MAX_UNKNOWN_RESPONSES + 2):
        result = app_context.qr_login_status(status.session_id)
        if result.done:
            break

    assert result is not None and result.state == QR_STATE_FAILED
    assert "刷新二维码" in result.message
    assert result.extra.get("server_message"), "服务端原始说明应保留在 extra 里"
    # 提示里不应该把服务端说明再嵌一遍，否则会出现一长串套娃括号
    assert "服务端返回：操作失败" not in result.message


def test_unknown_counter_resets_after_normal_response(app_context, mock_server):
    """中间夹一次正常响应后，未知计数要归零，不能累加成"偶发也失败"。"""
    mock_server.set_scenario("qr_bad_response")
    status = app_context.start_qr_login()
    session = app_context.accounts.get_qr_session(status.uuid)

    app_context.qr_login_status(status.session_id)
    app_context.qr_login_status(status.session_id)
    assert session.unknown_responses == 2

    mock_server.set_scenario("has_ticket")      # 恢复正常（mock 默认返回 waiting）
    app_context.qr_login_status(status.session_id)
    assert session.unknown_responses == 0


def test_parse_qr_check_accepts_numeric_code():
    """官方有时返回 int 而不是字符串，两种都要认。"""
    assert parse_qr_check({"result_code": 2})[0] == QR_STATE_CONFIRMED


def test_parse_qr_check_falls_back_to_status_field():
    state, _, _ = parse_qr_check({"status": "1"})
    assert state == QR_STATE_SCANNED


def test_parse_qr_check_extracts_newapptk_as_uamtk():
    assert parse_qr_check({"result_code": "2", "newapptk": "tk-9"})[1] == "tk-9"


def test_parse_qr_check_handles_empty_payload():
    state, uamtk, message = parse_qr_check(None)
    assert state == QR_STATE_UNKNOWN
    assert uamtk == ""
    assert message


def test_parse_qr_create_returns_uuid_and_image():
    uuid, image = parse_qr_create({"uuid": "u-1", "image": "QUJD", "result_code": 0})
    assert uuid == "u-1"
    assert image == "QUJD"


def test_parse_qr_create_raises_without_uuid():
    with pytest.raises(ResponseParseError):
        parse_qr_create({"result_code": 1, "result_message": "生成失败"})
    with pytest.raises(ResponseParseError):
        parse_qr_create(None)


# =========================================================================== #
# 二、状态对象
# =========================================================================== #
def test_qr_status_to_dict_shapes():
    status = QrLoginStatus(state=QR_STATE_WAITING, uuid="u1", image_base64="abc", expires_at=time.time() + 30)
    plain = status.to_dict()
    assert plain["uuid"] == "u1"
    assert plain["done"] is False
    assert "image" not in plain          # 默认不带图片，避免无关接口把大字段带上
    assert plain["message"]              # 没给 message 时用默认文案兜底

    with_image = status.to_dict(include_image=True)
    assert with_image["image"] == "abc"


@pytest.mark.parametrize(
    "state, done, succeeded",
    [
        (QR_STATE_WAITING, False, False),
        (QR_STATE_SCANNED, False, False),
        (QR_STATE_CONFIRMED, True, True),
        (QR_STATE_EXPIRED, True, False),
        (QR_STATE_FAILED, True, False),
    ],
)
def test_qr_status_terminal_flags(state, done, succeeded):
    status = QrLoginStatus(state=state)
    assert status.done is done
    assert status.succeeded is succeeded


def test_qr_status_remaining_never_negative():
    assert QrLoginStatus(expires_at=time.time() - 100).remaining == 0
    assert QrLoginStatus().remaining == 0


# =========================================================================== #
# 三、接口层
# =========================================================================== #
def test_api_create_and_check_qr(app_context):
    user = _build_api_user(app_context)
    ticket = user.api.create_qr_login()
    assert ticket.ok and ticket.uuid
    assert ticket.image_base64

    state, _, _ = user.api.check_qr_login(ticket.uuid)
    assert state == QR_STATE_WAITING


def test_api_check_qr_requires_uuid(app_context, mock_server):
    user = _build_api_user(app_context)
    with pytest.raises(ValidationError):
        user.api.check_qr_login("")


def test_api_create_qr_records_requests(app_context, mock_server):
    user = _build_api_user(app_context)
    user.api.create_qr_login()
    paths = [r["path"] for r in mock_server.state.requests]
    assert "/passport/web/create-qr64" in paths


# =========================================================================== #
# 四、会话与账号集成
# =========================================================================== #
def test_qr_session_full_flow(app_context, mock_server):
    """完整流转：待扫码 → 已扫码 → 确认 → 账号被创建并在线。"""
    status = app_context.start_qr_login()
    assert status.state == QR_STATE_WAITING
    assert status.uuid and status.image_base64
    assert status.remaining > 0

    seen: list[str] = []
    current = status
    for _ in range(10):
        current = app_context.qr_login_status(status.session_id)
        seen.append(current.state)
        if current.done:
            break
        time.sleep(0.02)

    assert QR_STATE_SCANNED in seen, f"应经历「已扫码」阶段，实际={seen}"
    assert seen[-1] == QR_STATE_CONFIRMED

    user = app_context.accounts.get(current.username)
    assert user is not None, "扫码成功后应自动创建账号"
    assert user.is_login is True
    assert user.status == "online"
    assert user.id is not None


def test_qr_session_creates_passwordless_account(app_context, mock_server):
    """扫码账号不保存密码，且落库时 password_enc 为空、备注标明登录方式。"""
    status = app_context.start_qr_login()
    current = status
    for _ in range(10):
        current = app_context.qr_login_status(status.session_id)
        if current.done:
            break
        time.sleep(0.02)

    row = app_context.db.get_user_by_name(current.username, with_secret=True)
    assert row is not None
    assert (row.get("password_enc") or "") == "", "扫码账号不应写入密码"
    assert row.get("remark") == "扫码登录"


def test_qr_session_is_idempotent_after_success(app_context, mock_server):
    """两个页面同时轮询时，不能重复完成登录。"""
    status = app_context.start_qr_login()
    current = status
    for _ in range(10):
        current = app_context.qr_login_status(status.session_id)
        if current.done:
            break
        time.sleep(0.02)

    user = app_context.accounts.get(current.username)
    successes = user.stats.successes

    for _ in range(3):
        again = app_context.qr_login_status(status.session_id)
        assert again.state == QR_STATE_CONFIRMED
        assert again.username == current.username
    assert user.stats.successes == successes, "重复轮询不应重复计成功"


def test_qr_session_expired(app_context, mock_server):
    """关掉自动刷新后，过期就是终态。

    （默认开启自动刷新：官方二维码只有约 20 秒寿命，不换码用户根本来不及扫。
    不换码时的终态行为仍要保留，见 test_expired_qr_is_auto_refreshed 之后的用例。）
    """
    mock_server.set_scenario("qr_expired")
    status = app_context.start_qr_login()
    session = app_context.accounts.get_qr_session(status.session_id)
    session.auto_refresh = False
    result = app_context.qr_login_status(status.session_id)
    assert result.state == QR_STATE_EXPIRED
    assert result.done is True
    # 过期后不再创建账号
    assert app_context.accounts.get("qr_user_001") is None


def test_qr_session_waiting_scenario(app_context, mock_server):
    mock_server.set_scenario("qr_timeout")
    status = app_context.start_qr_login()
    for _ in range(3):
        result = app_context.qr_login_status(status.session_id)
        assert result.state == QR_STATE_WAITING
        assert result.done is False


def test_qr_session_cancel_stops_polling(app_context, mock_server):
    status = app_context.start_qr_login()
    assert app_context.cancel_qr_login(status.uuid) is True
    result = app_context.qr_login_status(status.session_id)
    assert result.state == QR_STATE_EXPIRED
    assert result.message == "已取消扫码登录"


def test_qr_status_unknown_uuid_returns_none(app_context, mock_server):
    assert app_context.qr_login_status("not-a-real-uuid") is None
    assert app_context.cancel_qr_login("not-a-real-uuid") is False


def test_qr_session_local_ttl_expiry(app_context, mock_server):
    """本地超时兜底：服务端一直返回 waiting 时也不能无限轮询下去。

    默认行为是**换一张新码**（20 秒的码不换根本扫不上），
    所以这里把自动刷新关掉，验证终态那条路径仍然成立。
    """
    status = app_context.start_qr_login(ttl=60.0)
    session = app_context.accounts.get_qr_session(status.session_id)
    assert session is not None
    session.auto_refresh = False
    session.status.expires_at = time.time() - 1     # 人为把二维码置为过期
    result = app_context.qr_login_status(status.session_id)
    assert result.state == QR_STATE_EXPIRED


def test_qr_session_confirmed_without_uamtk_still_logs_in(app_context, mock_server):
    """官方有时不在 checkqr 响应里给 uamtk；应回退到 uamtk 接口完成登录。"""
    mock_server.set_qr_username("fallback_user")
    mock_server.set_qr_state("confirmed")
    status = app_context.start_qr_login()
    result = app_context.qr_login_status(status.session_id)
    assert result.state == QR_STATE_CONFIRMED
    assert result.username == "fallback_user"


def test_qr_login_preserves_existing_password(app_context, mock_server):
    """同一账号原先有密码时，扫码登录不能把密码清掉。"""
    from ticket_helper.config.settings import UserConfig

    app_context.upsert_user(UserConfig(username="qr_user_001", password="keep-me", heartbeat=120))
    before = app_context.db.get_user_by_name("qr_user_001", with_secret=True)
    assert before and before.get("password_enc")

    status = app_context.start_qr_login()
    for _ in range(10):
        if app_context.qr_login_status(status.session_id).done:
            break
        time.sleep(0.02)

    after = app_context.db.get_user_by_name("qr_user_001", with_secret=True)
    assert (after.get("password_enc") or "") == before.get("password_enc"), "原有密码必须保留"
    # 内存里的密码也要保留，否则密码登录会被误判为「无密码账号」
    assert app_context.accounts.get("qr_user_001").password == "keep-me"


def test_qr_login_preserves_existing_remark(app_context, mock_server):
    from ticket_helper.config.settings import UserConfig

    app_context.upsert_user(UserConfig(username="qr_user_001", password="pw"))
    app_context.db.save_user("qr_user_001", "", remark="我的主账号")

    status = app_context.start_qr_login()
    for _ in range(10):
        if app_context.qr_login_status(status.session_id).done:
            break
        time.sleep(0.02)

    row = app_context.db.get_user_by_name("qr_user_001", with_secret=True)
    assert row.get("remark") == "我的主账号", "不应覆盖使用者自己的备注"


def test_qr_account_cannot_password_login(app_context, mock_server):
    status = app_context.start_qr_login()
    current = status
    for _ in range(10):
        current = app_context.qr_login_status(status.session_id)
        if current.done:
            break
        time.sleep(0.02)
    user = app_context.accounts.get(current.username)

    # 已在线时普通 login() 直接返回成功（无需密码）
    assert user.login() is True

    # 强制用密码重新登录则会明确失败，并给出可操作的原因
    assert user.login(force=True) is False
    assert "扫码登录" in user.stats.last_error
    # 已在线的情况下不应被打成异常状态（否则界面显示异常但账号可用，更难判断）
    assert user.status == "online"


def test_qr_account_offline_after_restart_reports_clearly(app_context, mock_server):
    """重启后扫码账号无法自动登录，必须给出明确提示而不是「密码错误」。"""
    from ticket_helper.config.settings import UserConfig

    user = app_context.upsert_user(UserConfig(username="qr_only", password=""))
    assert user.password == ""
    assert user.login() is False
    assert "扫码登录" in user.stats.last_error
    assert user.status == "error"


def test_qr_sessions_are_garbage_collected(app_context, mock_server):
    """终态会话不能无限堆积。"""
    mock_server.set_qr_state("confirmed")
    for _ in range(30):
        status = app_context.start_qr_login()
        app_context.qr_login_status(status.session_id)

    removed = app_context.accounts.gc_qr_sessions(keep=5)
    assert removed > 0
    remaining = len(app_context.accounts._qr_sessions)  # noqa: SLF001
    assert remaining <= 8, f"会话表应被收敛，实际还剩 {remaining}"


def test_qr_session_allows_multiple_concurrent_scans(app_context, mock_server):
    """多个人/多标签页可以同时开扫，互不影响。"""
    mock_server.set_qr_state("confirmed")
    first = app_context.start_qr_login()
    second = app_context.start_qr_login()
    assert first.uuid != second.uuid

    s1 = app_context.qr_login_status(first.uuid)
    s2 = app_context.qr_login_status(second.uuid)
    assert s1.state == QR_STATE_CONFIRMED
    assert s2.state == QR_STATE_CONFIRMED


def test_start_qr_login_reports_failure_instead_of_raising(app_context, monkeypatch):
    """发起扫码失败时要返回 failed 状态，便于前端统一展示，而不是抛异常。"""
    bad_user = _build_api_user(app_context, "no_api_user")
    bad_user.api = None
    monkeypatch.setattr(QrLoginSession, "_new_user", lambda self: bad_user)

    session = QrLoginSession(app_context.accounts)
    result = session.start()
    assert result.state == QR_STATE_FAILED
    assert result.message
    assert session.finished is True


def test_qr_login_risk_signal_keeps_session_alive(app_context, mock_server):
    """轮询撞上风控时不能把会话打成终态。

    否则用户刚扫完码、还没来得及在手机上确认，界面就报「登录失败」，
    而实际上二维码还是有效的 —— 这会直接导致用户重复操作。
    """
    status = app_context.start_qr_login()
    assert status.state == QR_STATE_WAITING

    mock_server.set_scenario("risk")
    result = app_context.qr_login_status(status.session_id)
    assert result.done is False, "风控属于暂时不通，不应终结会话"
    assert "风控" in result.message

    # 风控解除后应能继续轮询并走到成功
    mock_server.set_scenario("has_ticket")
    mock_server.set_qr_state("confirmed")
    final = app_context.qr_login_status(status.session_id)
    assert final.state == QR_STATE_CONFIRMED


# --------------------------------------------------------------------------- #
# 真机协议回归：这两条是拿真实 12306 试出来的，改动前先看注释
# --------------------------------------------------------------------------- #
def test_checkqr_sends_appid_otn(app_context, mock_server):
    """``checkqr`` **必须带 ``appid=otn``**。

    真机上不带它官方不会报错，而是稳定返回
    ``{"result_message":"操作失败","result_code":9}`` —— 看起来像服务端故障，
    实际只是少了个参数。漏掉时的症状就是「还没扫码就提示失败」，
    而且用户完全无从判断原因。
    """
    user = app_context.accounts.build_user(UserConfig(username="qr_appid"))
    ticket = user.api.create_qr_login()
    user.api.check_qr_login(ticket.uuid)

    calls = [r for r in mock_server.state.requests if r["path"] == "/passport/web/checkqr"]
    assert calls, "应当请求过 checkqr"
    assert calls[-1]["payload"].get("appid") == "otn"


def test_checkqr_without_appid_would_be_rejected(app_context, mock_server):
    """反向断言：不带 appid 时服务端返回的是「操作失败」，而解析器不能把它当成正常状态。

    mock 在这一条上如实复现了真机行为，所以以后谁把 appid 去掉，
    测试会立刻失败，而不是等到用户报「又提示操作失败了」。
    """
    user = app_context.accounts.build_user(UserConfig(username="qr_no_appid"))
    ticket = user.api.create_qr_login()
    response = user.api.client.post(
        "/passport/web/checkqr", data={"uuid": ticket.uuid}, allow_retry=False
    )
    payload = response.json or {}
    assert str(payload.get("result_code")) == "9"
    assert payload.get("result_message") == "操作失败"


# --------------------------------------------------------------------------- #
# 二维码 20 秒寿命 → 必须自动换码
# --------------------------------------------------------------------------- #
def test_expired_qr_is_auto_refreshed(app_context, mock_server):
    """官方二维码真机实测只有约 20 秒寿命，过期必须自动换一张新的。

    否则用户「打开弹窗 → 拿起手机 → 解锁 → 找到扫一扫」这套动作做完，
    码早就失效了，体验就是「怎么扫都没反应」。
    """
    mock_server.state.qr_expire_after_polls = 1
    status = app_context.start_qr_login()
    first_uuid = status.uuid
    assert first_uuid

    # 第一次轮询拿到 waiting，第二次会被判过期 → 会话应自动换新码
    for _ in range(4):
        app_context.qr_login_status(first_uuid)
        current = app_context.qr_login_status(first_uuid)
        if current.uuid != first_uuid:
            break

    assert current.uuid != first_uuid, "过期的二维码应被自动换成新码"
    assert current.state == QR_STATE_WAITING, f"换码后应回到等待扫码，实际 {current.state}"
    assert current.image_base64, "新码必须带图片，否则界面还是旧图"
    assert current.refresh_count >= 1
    assert current.done is False, "换码不是终态，会话要继续"


def test_auto_refresh_keeps_session_alive_then_succeeds(app_context, mock_server):
    """自动换码之后，用户仍然能扫新的码完成登录。"""
    mock_server.state.qr_expire_after_polls = 1
    status = app_context.start_qr_login()
    first_qr = status.uuid          # 注意：status 是快照，后续轮询不会改它
    for _ in range(4):
        app_context.qr_login_status(status.session_id)

    refreshed = app_context.qr_login_status(status.session_id)
    assert refreshed.uuid != first_qr and not refreshed.done

    # 新码上用户完成扫码确认
    mock_server.state.qr_expire_after_polls = 0
    mock_server.set_qr_state("confirmed", refreshed.uuid)
    final = app_context.qr_login_status(status.session_id)
    assert final.state == QR_STATE_CONFIRMED
    assert final.username
    assert app_context.accounts.get(final.username) is not None


def test_auto_refresh_stops_after_limit(app_context, mock_server):
    """一直没人扫时要停下，不能无限刷新打接口。"""
    from ticket_helper.account.qrlogin import MAX_QR_REFRESHES

    mock_server.state.qr_expire_after_polls = 1
    status = app_context.start_qr_login()
    session = app_context.accounts.get_qr_session(status.uuid)
    assert session is not None
    session.max_refreshes = 2  # 缩短上限，避免测试真的刷 30 次

    last = None
    for _ in range(30):
        last = app_context.qr_login_status(status.session_id)
        if last.done:
            break
    assert last is not None and last.done, "达到刷新上限后应给出结论"
    assert last.state == QR_STATE_EXPIRED
    assert "刷新" in last.message
    assert MAX_QR_REFRESHES >= 10, "上限过低会让正常扫码时间不够"


def test_scanned_qr_is_not_auto_refreshed(app_context, mock_server):
    """已经扫码（用户正在手机上确认）时**不能**换码，否则会把这一步作废。"""
    status = app_context.start_qr_login()
    mock_server.set_qr_state("scanned")
    scanned = app_context.qr_login_status(status.session_id)
    assert scanned.state == QR_STATE_SCANNED

    # 服务端此刻报过期（手机没点确认就走了），也不该换码
    mock_server.set_qr_state("expired")
    after = app_context.qr_login_status(status.session_id)
    assert after.uuid == status.uuid, "已扫码过就不应再换码"
    assert after.state == QR_STATE_EXPIRED


# --------------------------------------------------------------------------- #
# 真机协议回归：这两条是拿真实 12306 试出来的，改动前先看注释
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 真机协议回归：登录会话必须先建立，再换取票据
# --------------------------------------------------------------------------- #
def _login_call_order(mock_server) -> list[str]:
    watched = {"/otn/login/userLogin", "/passport/web/auth/uamtk", "/otn/uamauthclient"}
    return [r["path"] for r in mock_server.state.requests if r["path"] in watched]


def test_qr_login_exchanges_ticket_before_auth(app_context, mock_server):
    """扫码收尾必须是 ``uamtk`` 换票 → ``uamauthclient`` 换身份。"""
    status = app_context.start_qr_login()
    current = status
    for _ in range(10):
        current = app_context.qr_login_status(status.session_id)
        if current.done:
            break
        time.sleep(0.02)

    assert current.state == QR_STATE_CONFIRMED, current.message
    order = _login_call_order(mock_server)
    assert "/passport/web/auth/uamtk" in order, f"应先换票，实际={order}"
    assert "/otn/uamauthclient" in order
    assert order.index("/passport/web/auth/uamtk") < order.index("/otn/uamauthclient")


def test_qr_login_follows_reference_order(app_context, mock_server):
    """扫码收尾顺序对齐上游 py12306 的 ``qr_login``：

    ``userLogin``（GET 带跳转）→ ``uamtk`` 换票 → ``uamauthclient`` 换身份。

    曾经我误判为「扫码流程不能调 userLogin，会打断会话」——
    那是在鉴权请求头还没修好时做的实验，换票无论如何都会失败，原因无法区分。
    修正请求头后做过对照实验：调与不调、POST 与 GET，换票都正常返回 JSON。
    """
    status = app_context.start_qr_login()
    for _ in range(10):
        if app_context.qr_login_status(status.session_id).done:
            break
        time.sleep(0.02)

    order = _login_call_order(mock_server)
    assert order, "应当发生登录收尾请求"
    assert order[0] == "/otn/login/userLogin", f"第一步应是 userLogin，实际={order}"
    assert order.index("/otn/login/userLogin") < order.index("/passport/web/auth/uamtk")
    assert order.index("/passport/web/auth/uamtk") < order.index("/otn/uamauthclient")


def test_qr_userlogin_uses_get(app_context, mock_server):
    """扫码流程的 userLogin 用 GET（上游做法），密码流程用 POST。"""
    status = app_context.start_qr_login()
    for _ in range(10):
        if app_context.qr_login_status(status.session_id).done:
            break
        time.sleep(0.02)

    calls = [r for r in mock_server.state.requests if r["path"] == "/otn/login/userLogin"]
    assert calls, "扫码流程也应当调用 userLogin（与上游一致）"
    assert calls[0]["payload"].get("method") == "GET"


def test_qr_login_uses_ticket_from_uamtk_endpoint(app_context, mock_server):
    """必须用换票接口发的 ``newapptk``，而不是 checkqr 响应里同名的 ``uamtk``。

    真机上两个字段同名但不等价：直接拿 checkqr 那个去换身份会被判
    「uamtk票据内容为空」（已实测）。
    """
    status = app_context.start_qr_login()
    for _ in range(10):
        if app_context.qr_login_status(status.session_id).done:
            break
        time.sleep(0.02)

    calls = [r for r in mock_server.state.requests if r["path"] == "/otn/uamauthclient"]
    assert calls, "应当调用 uamauthclient"
    sent = calls[-1]["payload"].get("tk", "")
    assert sent, "必须带票据"
    assert sent == mock_server.state.apptk, "用的必须是换票接口发出来的 newapptk"


def test_password_login_establishes_session_before_ticket_exchange(app_context, mock_server):
    """密码登录是同一条收尾链路，顺序同样不能反。"""
    user = app_context.accounts.build_user(UserConfig(username="pw_order", password="pw"))
    result = user.api.login("pw_order", "pw")
    assert result.ok is True, result.message

    order = _login_call_order(mock_server)
    assert order[0] == "/otn/login/userLogin", f"第一步必须是 userLogin，实际顺序={order}"


def test_client_auth_failure_surfaces_server_reason(app_context, mock_server):
    """鉴权失败时必须带出服务端的原话，而不是替换成笼统的「鉴权失败」。

    「uamtk票据内容为空」虽然误导，但它至少指向了票据环节；
    换成「客户端鉴权失败」等于把唯一线索删掉，排查只能靠猜。
    """
    mock_server.set_scenario("uamauth_fail")
    user = app_context.accounts.build_user(UserConfig(username="pw_reason", password="pw"))

    result = user.api.login("pw_reason", "pw")
    assert result.ok is False
    assert "票据" in result.message, f"应带出服务端原话，实际={result.message!r}"


def test_qr_client_auth_failure_surfaces_server_reason(app_context, mock_server):
    """扫码链路同样要把服务端的失败原因透出来。"""
    mock_server.set_scenario("uamauth_fail")
    status = app_context.start_qr_login()
    current = status
    for _ in range(10):
        current = app_context.qr_login_status(status.session_id)
        if current.done:
            break
        time.sleep(0.02)

    assert current.done and current.state != QR_STATE_CONFIRMED
    assert "票据" in current.message, f"应带出服务端原话，实际={current.message!r}"


# --------------------------------------------------------------------------- #
# 真机协议回归：登录鉴权接口必须带同源头
# --------------------------------------------------------------------------- #
def test_auth_endpoints_send_same_origin_headers(app_context, mock_server):
    """换票/换身份接口必须带 ``Origin`` + passport 跳转页 ``Referer``。

    真机上不带它服务端**不报错**，而是返回 HTML 错误页
    （"网络可能存在问题，请您重试一下！"），于是「换票永远拿不到 newapptk」，
    看起来却像票据有问题。客户端以前用登录页 Referer，扫码后必然失败。

    mock 现在照真机行为强制校验这两个头，所以这条用例挂了就说明又写回去了。
    """
    status = app_context.start_qr_login()
    for _ in range(10):
        if app_context.qr_login_status(status.session_id).done:
            break
        time.sleep(0.02)

    seen = mock_server.state.auth_headers
    assert seen, "鉴权接口应当被调用过"
    for headers in seen:
        assert headers["origin"], f"缺少 Origin: {headers}"
        assert "passport?redirect=" in headers["referer"], f"Referer 不对: {headers}"


def test_password_login_sends_same_origin_headers(app_context, mock_server):
    """密码登录链路同样是鉴权接口，头也不能少。"""
    user = app_context.accounts.build_user(UserConfig(username="pw_headers", password="pw"))
    assert user.api.login("pw_headers", "pw").ok is True

    seen = mock_server.state.auth_headers
    assert seen
    assert all(h["origin"] for h in seen)
    assert all("passport?redirect=" in h["referer"] for h in seen)


# --------------------------------------------------------------------------- #
# 真机协议回归：登录鉴权接口必须带同源头
# --------------------------------------------------------------------------- #
