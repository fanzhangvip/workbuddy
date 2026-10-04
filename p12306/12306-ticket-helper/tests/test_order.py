"""下单与订单流水测试（OD-T1 ~ OD-T5）。"""

from __future__ import annotations

import pytest

from ticket_helper.account.manager import User
from ticket_helper.config.settings import UserConfig
from ticket_helper.errors import VerifyCodeError
from ticket_helper.inter.api import TicketApi
from ticket_helper.inter.base import HttpClient
from ticket_helper.inter.parser import SEAT_CODE, parse_left_ticket_result
from ticket_helper.order.service import (
    ORDER_FAILED,
    ORDER_QUEUED,
    ORDER_SUCCESS,
    OrderService,
)
from ticket_helper.verify.service import CallbackSolver
from mock_server.server import build_ticket_row

MOCK_PASSENGERS = [
    {
        "passenger_name": "张三",
        "passenger_id_type_code": "1",
        "passenger_id_no": "110101199001011234",
        "passenger_type": "1",
        "mobile_no": "13800000000",
        # 真机 getPassengerDTOs 会下发它；缺了它 checkOrderInfo 必被拒（回流 #51）
        "allEncStr": "mock-all-enc-zhangsan",
    }
]


@pytest.fixture()
def logged_in_user(app_context, mock_server) -> User:
    app_context.verify_service.set_solver(CallbackSolver(lambda i, h, k: "112,45", name="fixed"))
    user = User(
        UserConfig(username="test_user", password="test_pass"),
        base_url=mock_server.base_url,
        station_map=app_context.station_map,
        verify_service=app_context.verify_service,
    )
    assert user.login() is True
    user.is_checked = True
    yield user
    user.close()


def _ticket(app_context, mock_server, *, seats=None):
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)
    row = build_ticket_row(
        train_code="G6102",
        secret_seed="mock-secret-1",
        from_code="GZQ",
        to_code="CWQ",
        start_time="08:12",
        arrive_time="10:47",
        duration="02:35",
        train_date="2026-10-01",
        seats=seats or {"二等座": "5"},
    )
    return api, parse_left_ticket_result(row, station_map=app_context.station_map)


# --------------------------------------------------------------------------- #
# OD-T1 全流程：提交 → 排队 → 出票
# --------------------------------------------------------------------------- #
def test_submit_order_full_flow(app_context, mock_server, logged_in_user):
    _, ticket = _ticket(app_context, mock_server)
    seat = ticket.seats["二等座"]
    service = OrderService(app_context.db)

    result = service.submit(
        logged_in_user, ticket, seat, MOCK_PASSENGERS, task_id=1, task_name="t1", max_retry=1
    )
    assert result.ok is True
    assert result.status == ORDER_SUCCESS
    assert result.order_no.startswith("E")
    assert result.pay_url and result.order_no in result.pay_url
    assert result.latency_ms >= 0

    # OD-T4：订单流水入库（OD-T4）
    orders = app_context.db.list_orders()
    assert any(o["order_no"] == result.order_no and o["status"] == "success" for o in orders)


def test_success_callback_fires(app_context, mock_server, logged_in_user):
    _, ticket = _ticket(app_context, mock_server)
    service = OrderService(app_context.db)
    fired: list = []
    service.on_success(fired.append)

    result = service.submit(logged_in_user, ticket, ticket.seats["二等座"], MOCK_PASSENGERS, task_id=2, max_retry=1)
    assert result.ok
    assert len(fired) == 1
    assert fired[0].order_no == result.order_no


# --------------------------------------------------------------------------- #
# GR-T7 / OD-T2：防重复下单 与 失败处理
# --------------------------------------------------------------------------- #
def test_dedup_prevents_duplicate_submission(app_context, mock_server, logged_in_user):
    _, ticket = _ticket(app_context, mock_server)
    seat = ticket.seats["二等座"]
    service = OrderService(app_context.db)

    first = service.submit(logged_in_user, ticket, seat, MOCK_PASSENGERS, task_id=3, max_retry=1)
    assert first.ok

    second = service.submit(logged_in_user, ticket, seat, MOCK_PASSENGERS, task_id=3, max_retry=1)
    assert second.ok is True
    assert second.order_no == first.order_no          # 返回同一张票
    assert second.retryable is False

    # 数据库里只应有一条该订单（不会因为重复提交产生两条记录）
    same = [o for o in app_context.db.list_orders() if o["order_no"] == first.order_no]
    assert len(same) == 1


def test_redis_dedup_reset_allows_new_round(app_context, mock_server, logged_in_user):
    _, ticket = _ticket(app_context, mock_server)
    service = OrderService(app_context.db)
    service.submit(logged_in_user, ticket, ticket.seats["二等座"], MOCK_PASSENGERS, task_id=4, max_retry=1)
    assert service.stats()["succeeded_keys"] == 1
    service.reset_dedup()
    assert service.stats()["succeeded_keys"] == 0


def test_sold_out_reports_retryable(app_context, mock_server, logged_in_user):
    api, ticket = _ticket(app_context, mock_server)
    mock_server.set_scenario("sold_out")
    service = OrderService(app_context.db)
    result = service.submit(logged_in_user, ticket, ticket.seats["二等座"], MOCK_PASSENGERS, task_id=5, max_retry=1)
    assert result.ok is False
    assert result.status == ORDER_FAILED
    assert "余票不足" in result.message
    assert result.retryable is True  # 无票是可重试的（继续捡漏）


def test_queue_failure_is_terminal(app_context, mock_server, logged_in_user):
    mock_server.set_scenario("queue_fail")
    _, ticket = _ticket(app_context, mock_server)
    service = OrderService(app_context.db, queue_max_polls=2)
    result = service.submit(logged_in_user, ticket, ticket.seats["二等座"], MOCK_PASSENGERS, task_id=6, max_retry=1)
    assert result.ok is False
    assert result.retryable is False
    assert "排队" in result.message


def test_queue_timeout_is_reported(app_context, mock_server, logged_in_user):
    mock_server.set_scenario("queue_timeout")
    _, ticket = _ticket(app_context, mock_server)
    service = OrderService(app_context.db, queue_max_polls=2)
    result = service.submit(logged_in_user, ticket, ticket.seats["二等座"], MOCK_PASSENGERS, task_id=7, max_retry=1)
    assert result.ok is False
    assert result.status == ORDER_QUEUED
    assert "排队" in result.message


def test_captcha_failure_does_not_retry(app_context, mock_server, logged_in_user, monkeypatch):
    """★ 验证码没通过时**不再重试**（回流 #51 附带）。

    识别链下端的人工兜底已经等过一个完整超时（默认 120 秒），
    ``submit`` 再按 ``max_retry`` 重来一遍，只是让使用者**再白等 N 个 2 分钟**，
    而且不会得到任何新信息 —— 与 ``verify/service.py`` 里
    「人工兜底不再重试」是同一个判断，那次漏了上层这层重试。

    判据是**尝试次数**：``max_retry=3`` 之下也只允许调一次识别。
    """
    _, ticket = _ticket(app_context, mock_server)
    service = OrderService(app_context.db)

    attempts: list[str] = []

    def boom(image, *, hint="", kind="login"):
        attempts.append(kind)
        raise VerifyCodeError(
            "验证码识别失败，已用尽全部识别源（人工录入已超时，可在界面中重新登录）"
        )

    monkeypatch.setattr(logged_in_user.verify, "recognize", boom)

    result = service.submit(
        logged_in_user, ticket, ticket.seats["二等座"], MOCK_PASSENGERS, task_id=9, max_retry=3
    )
    assert result.ok is False
    assert result.retryable is False, "验证码失败不该被标成可重试"
    assert len(attempts) == 1, f"max_retry=3 之下也只应尝试一次，实际 {len(attempts)} 次"
    assert "验证码" in result.message


def test_dedup_key_is_stable_and_distinct(app_context, mock_server):
    _, ticket_a = _ticket(app_context, mock_server)
    key_a = OrderService.dedup_key(ticket_a, ticket_a.seats["二等座"], MOCK_PASSENGERS, 1)
    key_a2 = OrderService.dedup_key(ticket_a, ticket_a.seats["二等座"], MOCK_PASSENGERS, 1)
    assert key_a == key_a2

    # 换乘客 / 换席别 / 换任务都应产生不同的键
    other_passenger = [{**MOCK_PASSENGERS[0], "passenger_name": "李四"}]
    assert OrderService.dedup_key(ticket_a, ticket_a.seats["二等座"], other_passenger, 1) != key_a
    assert OrderService.dedup_key(ticket_a, ticket_a.seats["一等座"], MOCK_PASSENGERS, 1) != key_a
    assert OrderService.dedup_key(ticket_a, ticket_a.seats["二等座"], MOCK_PASSENGERS, 2) != key_a


# --------------------------------------------------------------------------- #
# OD-T3 未完成订单
# --------------------------------------------------------------------------- #
def test_query_unfinished_orders(app_context, mock_server, logged_in_user):
    _, ticket = _ticket(app_context, mock_server)
    service = OrderService(app_context.db)
    service.submit(logged_in_user, ticket, ticket.seats["二等座"], MOCK_PASSENGERS, task_id=8, max_retry=1)

    pending = service.query_unfinished(logged_in_user)
    assert isinstance(pending, list)
    assert pending, "出票后应能在未完成订单里看到待支付记录"
    assert pending[0]["pay_url"]


def test_order_history_and_stats(app_context, mock_server, logged_in_user):
    _, ticket = _ticket(app_context, mock_server)
    service = OrderService(app_context.db)
    service.submit(logged_in_user, ticket, ticket.seats["二等座"], MOCK_PASSENGERS, task_id=9, max_retry=1)

    assert service.history(status="success")
    assert service.history(task_id=9)
    assert service.history(task_id=999) == []


# --------------------------------------------------------------------------- #
# 席别编码映射（下单参数正确性）
# --------------------------------------------------------------------------- #
def test_seat_code_mapping_covers_configured_seats():
    from ticket_helper.config.settings import SEAT_TYPES

    for seat in SEAT_TYPES:
        assert seat in SEAT_CODE, f"席别 {seat} 缺少下单编码映射"
