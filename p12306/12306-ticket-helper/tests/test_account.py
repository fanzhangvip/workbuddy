"""账号与会话测试（AC-T1 ~ AC-T8）。"""

from __future__ import annotations

import time

import pytest

from ticket_helper.account.manager import (
    STATUS_ERROR,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_RISK,
    AccountManager,
    User,
)
from ticket_helper.config.settings import TaskConfig, UserConfig
from ticket_helper.errors import LoginError, RiskControlError
from ticket_helper.inter.base import HttpClient


@pytest.fixture(autouse=True)
def install_fixed_solver(app_context):
    """所有账号用例都注入固定答案的识别器，避免测试依赖真实打码能力。"""
    app_context.verify_service.set_solver(_fixed())
    yield


def _make_user(app_context, mock_server, config: UserConfig | None = None) -> User:
    cfg = config or UserConfig(username="test_user", password="test_pass")
    user = User(
        cfg,
        base_url=mock_server.base_url,
        station_map=app_context.station_map,
        verify_service=app_context.verify_service,
    )
    app_context.verify_service.set_solver(_fixed())
    return user


def _fixed():
    from ticket_helper.verify.service import CallbackSolver

    return CallbackSolver(lambda image, hint, kind: "112,45", name="fixed")


# --------------------------------------------------------------------------- #
# AC-T1 初始状态
# --------------------------------------------------------------------------- #
def test_user_initial_state(app_context, mock_server):
    user = _make_user(app_context, mock_server)
    assert user.status == STATUS_OFFLINE
    assert user.is_login is False
    assert user.is_checked is False
    assert user.enabled is True
    assert user.heartbeat_interval >= 30
    payload = user.to_dict()
    assert payload["status"] == "offline"
    assert "password" not in payload  # 绝不外泄密码字段


# --------------------------------------------------------------------------- #
# AC-T2 登录成功
# --------------------------------------------------------------------------- #
def test_login_success(app_context, mock_server):
    user = _make_user(app_context, mock_server)
    assert user.login() is True
    assert user.is_login is True
    assert user.status == STATUS_ONLINE
    assert user.stats.successes == 1
    assert user.last_active > 0


# --------------------------------------------------------------------------- #
# AC-T3 登录失败不污染会话
# --------------------------------------------------------------------------- #
def test_login_failure_keeps_session_clean(app_context, mock_server):
    mock_server.set_scenario("login_fail")
    user = _make_user(app_context, mock_server)
    assert user.login() is False
    assert user.is_login is False
    assert user.status in (STATUS_ERROR, STATUS_RISK)
    assert user.stats.failures >= 1
    assert user.stats.last_error


def test_login_requires_station_map():
    """未绑定站点表时应抛出明确错误，而不是 NoneType 崩溃。"""
    user = User(UserConfig(username="u", password="p"))
    with pytest.raises(LoginError):
        user.login()


# --------------------------------------------------------------------------- #
# AC-T4 / AC-T5 心跳保活与掉线恢复
# --------------------------------------------------------------------------- #
def test_check_alive_when_online(app_context, mock_server):
    user = _make_user(app_context, mock_server)
    user.login()
    assert user.check_alive() is True
    assert user.keep_alive_once() is True


def test_keep_alive_triggers_restore_when_session_lost(app_context, mock_server):
    """AC-T5：心跳发现掉线 → 自动 restore 重登成功。"""
    user = _make_user(app_context, mock_server)
    user.login()
    stats_before = user.stats.attempts

    # 模拟会话失效：让 checkUser 返回未登录
    mock_server.set_scenario("risk")
    assert user.check_alive() is False

    # 恢复网络后 restore 应成功
    mock_server.set_scenario("has_ticket")
    assert user.restore() is True
    assert user.is_login is True
    assert user.status == STATUS_ONLINE
    assert user.stats.attempts > stats_before


def test_heartbeat_thread_starts_and_stops(app_context, mock_server):
    user = _make_user(app_context, mock_server)
    user.heartbeat_interval = 30
    user.login()
    user.start_heartbeat()
    assert user._heartbeat_thread is not None and user._heartbeat_thread.is_alive()  # noqa: SLF001
    user.stop_heartbeat()
    assert user._heartbeat_thread is None  # noqa: SLF001


# --------------------------------------------------------------------------- #
# AC-T6 / AC-T7 核验状态与多账号
# --------------------------------------------------------------------------- #
def test_detect_verified(app_context, mock_server):
    user = _make_user(app_context, mock_server)
    user.login()
    assert user.detect_verified() is True
    assert user.is_checked is True


def test_account_manager_crud_and_allocate(app_context, mock_server):
    manager = AccountManager(
        base_url=mock_server.base_url,
        station_map=app_context.station_map,
        verify_service=app_context.verify_service,
        auto_heartbeat=False,
    )
    app_context.verify_service.set_solver(_fixed())

    manager.add_user(UserConfig(username="u1", password="p1"))
    manager.add_user(UserConfig(username="u2", password="p2"))
    assert manager.count() == 2

    # 未登录时无法分配
    task = TaskConfig(name="t", left_dates=["2026-10-01"], from_stations=["广州"],
                      to_stations=["长沙"], seat_types=["二等座"])
    assert manager.allocate(task) is None

    results = manager.login_all(parallel=True)
    assert results == {"u1": True, "u2": True}
    assert len(manager.online_users()) == 2

    # 轮询分配：连续两次应分到不同账号
    first = manager.allocate(task)
    second = manager.allocate(task)
    assert first is not None and second is not None
    assert {first.username, second.username} == {"u1", "u2"}

    # 任务指定账号优先
    task.accounts = ["u2"]
    assert manager.allocate(task).username == "u2"

    # 停用后不再参与分配
    manager.enable("u2", False)
    assert manager.allocate(task) is None

    # 停用 + 移除
    assert manager.enable("u1", False) is True
    assert manager.remove_user("u1") is True
    assert manager.count() == 1
    assert manager.remove_user("不存在") is False


def test_account_manager_snapshot(app_context, mock_server):
    manager = AccountManager(
        base_url=mock_server.base_url,
        station_map=app_context.station_map,
        verify_service=app_context.verify_service,
        auto_heartbeat=False,
    )
    manager.add_user(UserConfig(username="u1", password="p1"))
    manager.login_all()
    snap = manager.snapshot()
    assert snap["total"] == 1
    assert snap["online"] == 1
    assert snap["users"][0]["username"] == "u1"


# --------------------------------------------------------------------------- #
# AC-T8 风控
# --------------------------------------------------------------------------- #
def test_risk_marks_user_and_emits_event(app_context, mock_server):
    events: list[tuple[str, dict]] = []

    class RiskOnLogin(User):
        def _fetch_login_captcha(self):
            raise RiskControlError("登录过于频繁")

    user = RiskOnLogin(
        UserConfig(username="risk_user", password="p"),
        base_url=mock_server.base_url,
        station_map=app_context.station_map,
        verify_service=app_context.verify_service,
    )
    user.on_event(lambda event, payload: events.append((event, payload)))
    assert user.login() is False
    assert user.status == STATUS_RISK
    assert any(e == "risk" for e, _ in events)


def test_user_enable_disable(app_context, mock_server):
    manager = AccountManager(
        base_url=mock_server.base_url,
        station_map=app_context.station_map,
        verify_service=app_context.verify_service,
        auto_heartbeat=False,
    )
    user = manager.add_user(UserConfig(username="u1", password="p1"))
    manager.login_all()
    assert user.is_login is True

    manager.enable("u1", False)
    assert user.enabled is False
    assert user.login() is False  # 被禁用的账号拒绝登录

    manager.enable("u1", True)
    assert user.enabled is True


# --------------------------------------------------------------------------- #
# 会话隔离
# --------------------------------------------------------------------------- #
def test_sessions_are_isolated(app_context, mock_server):
    """多账号必须各自持有独立 Cookie 容器（DS-01 的基础）。"""
    manager = AccountManager(
        base_url=mock_server.base_url,
        station_map=app_context.station_map,
        verify_service=app_context.verify_service,
        auto_heartbeat=False,
    )
    a = manager.add_user(UserConfig(username="a", password="1"))
    b = manager.add_user(UserConfig(username="b", password="2"))
    assert a.client is not b.client
    assert a.client.session is not b.client.session
    manager.close()


# --------------------------------------------------------------------------- #
# 验证码「接口侧拿不到图」= 不重试（真机现状：登录验证码接口已不接受程序化请求）
# --------------------------------------------------------------------------- #
def test_login_does_not_retry_when_captcha_interface_is_blocked(app_context, mock_server):
    """接口侧拿不到验证码时**不能重试**。

    重试只是把同一个被拦的接口再打一遍（每轮还会同时打两个路径），
    既不会变好，又加重风控；而且最终报「验证码识别失败」会把人引向
    「是不是识别源有问题」这个错误方向 —— 真因是接口本身拿不到图。

    这里断言只尝试一次，并且结论里要出现「扫码登录」这条出路。
    """
    mock_server.set_scenario("login_captcha_blocked")
    user = app_context.accounts.build_user(UserConfig(username="cap_blocked", password="pw"))

    assert user.login() is False
    assert user.stats.attempts == 1, f"不应重试，实际尝试 {user.stats.attempts} 次"
    assert user.status == STATUS_ERROR
    assert "扫码登录" in (user.stats.last_error or ""), user.stats.last_error


def test_captcha_unavailable_is_a_distinct_error_type():
    """「接口拿不到图」与「识别失败」必须是不同类型，调用方才能分流。

    两者都继承 ``VerifyCodeError``，既保证老的捕获逻辑不受影响，
    又能让登录循环把「不重试」这条分支单独拎出来。
    """
    from ticket_helper.errors import CaptchaUnavailableError, VerifyCodeError

    assert issubclass(CaptchaUnavailableError, VerifyCodeError)
    assert CaptchaUnavailableError.code != VerifyCodeError.code
    assert CaptchaUnavailableError.code == "captcha_unavailable"
