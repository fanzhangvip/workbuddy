"""下单服务（4.5 / GR-06 / OD-01 ~ OD-04）。

把「提交订单请求 → 校验订单 → 查排队人数 → 确认排队 → 轮询出票」串成一条
可重试的流程，并把每一步的状态写入 orders 表形成流水。

设计取舍：
- **不做自动扣款**（OD-03）。出票后只记录支付跳转地址并强提醒，
  资金操作交由使用者本人在 12306 官方 App/网站完成，规避合规与安全风险；
- **幂等**：同一 (任务, 车次, 日期, 席别, 乘客集合) 组合在成功前不会重复提交
  （GR-08 防重复下单），由 :class:`OrderService` 内部去重表保证。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ticket_helper.account.manager import User
from ticket_helper.errors import (
    InterError,
    LoginError,
    QueueError,
    ResponseParseError,
    RiskControlError,
    SoldOutError,
    VerifyCodeError,
)
from ticket_helper.inter.parser import SEAT_CODE, SeatInfo, TrainTicket
from ticket_helper.logger import metrics

log = logging.getLogger(__name__)

#: 下单流程状态
ORDER_SUBMITTED = "submitted"
ORDER_QUEUED = "queued"
ORDER_SUCCESS = "success"
ORDER_FAILED = "failed"
ORDER_CANCELLED = "cancelled"

#: 订单状态 → 展示文案。**唯一的一份定义**，由 ``web/api.py`` 随订单一起下发，
#: 两端界面直接渲染 ``status_text``，不再各自维护一份映射表。
#:
#: 为什么必须收在一处（真实故障）：桌面端原先自己写了一份
#: ``{"success": "已出票", "failed": "失败", "pending": "进行中"}``，
#: 而本模块实际写入的状态是 ``submitted`` / ``queued`` —— 和 ``pending``
#: **没有任何交集**。于是「已提交 / 排队中」的订单在界面上显示的是英文原文，
#: 且不会有任何报错。网页端没有映射表，直接把 ``o.status`` 塞进徽标，同样露英文。
ORDER_STATUS_TEXT: dict[str, str] = {
    ORDER_SUBMITTED: "进行中",
    ORDER_QUEUED: "进行中",
    ORDER_SUCCESS: "已出票",
    ORDER_FAILED: "失败",
    ORDER_CANCELLED: "已取消",
}

#: 全部合法取值（契约测试用：界面上的状态判断必须落在这个集合里）
ORDER_STATUSES: tuple[str, ...] = tuple(ORDER_STATUS_TEXT)


@dataclass
class OrderResult:
    """一次下单尝试的结果。"""

    ok: bool = False
    status: str = ORDER_FAILED
    order_no: str = ""
    message: str = ""
    pay_url: str = ""
    train_code: str = ""
    depart_date: str = ""
    seat_type: str = ""
    from_station: str = ""
    to_station: str = ""
    passenger: str = ""
    wait_count: int = 0
    latency_ms: int = 0
    retryable: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "order_no": self.order_no,
            "message": self.message,
            "pay_url": self.pay_url,
            "train_code": self.train_code,
            "depart_date": self.depart_date,
            "seat_type": self.seat_type,
            "from_station": self.from_station,
            "to_station": self.to_station,
            "passenger": self.passenger,
            "wait_count": self.wait_count,
            "latency_ms": self.latency_ms,
            "retryable": self.retryable,
        }


class OrderService:
    """下单流程编排。"""

    def __init__(self, db: Any | None = None, *, queue_max_polls: int = 60) -> None:
        self.db = db
        self.queue_max_polls = queue_max_polls
        self._inflight: set[str] = set()
        self._succeeded: dict[str, OrderResult] = {}
        self._lock = threading.RLock()
        #: 出票成功回调（用于抢票引擎 / 调度器联动停止）
        self._success_listeners: list[Any] = []

    # ------------------------------------------------------------------ #
    def on_success(self, callback: Any) -> None:
        with self._lock:
            self._success_listeners.append(callback)

    def _emit_success(self, result: OrderResult) -> None:
        with self._lock:
            listeners = list(self._success_listeners)
        for callback in listeners:
            try:
                callback(result)
            except Exception as exc:  # noqa: BLE001
                log.error("下单成功回调失败: %s", exc)

    # ------------------------------------------------------------------ #
    # 幂等键
    # ------------------------------------------------------------------ #
    @staticmethod
    def dedup_key(ticket: TrainTicket, seat: SeatInfo, passengers: list[dict[str, Any]], task_id: int | None) -> str:
        names = ",".join(sorted(str(p.get("passenger_name", "")) for p in passengers))
        return f"{task_id}|{ticket.train_code}|{ticket.train_date}|{seat.seat_type}|{ticket.from_station_code}|{ticket.to_station_code}|{names}"

    def already_succeeded(self, key: str) -> OrderResult | None:
        with self._lock:
            return self._succeeded.get(key)

    def is_inflight(self, key: str) -> bool:
        with self._lock:
            return key in self._inflight

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def submit(
        self,
        user: User,
        ticket: TrainTicket,
        seat: SeatInfo,
        passengers: list[dict[str, Any]],
        *,
        task_id: int | None = None,
        task_name: str = "",
        need_captcha: bool = True,
        max_retry: int = 2,
    ) -> OrderResult:
        """执行一次完整下单。"""
        started = time.perf_counter()
        key = self.dedup_key(ticket, seat, passengers, task_id)

        # GR-08：同一票成功后不再重复提交
        done = self.already_succeeded(key)
        if done is not None:
            log.info("该票已出票，跳过重复提交: %s %s %s", done.train_code, done.depart_date, done.order_no)
            done.retryable = False
            return done

        with self._lock:
            if key in self._inflight:
                log.info("该票正在提交中，跳过重复请求")
                return OrderResult(ok=False, status=ORDER_QUEUED, message="该票正在提交中", retryable=False)
            self._inflight.add(key)

        result = OrderResult(
            train_code=ticket.train_code,
            depart_date=ticket.train_date,
            seat_type=seat.seat_type,
            from_station=ticket.from_station,
            to_station=ticket.to_station,
            passenger=",".join(str(p.get("passenger_name", "")) for p in passengers),
        )

        try:
            for attempt in range(1, max(1, max_retry) + 1):
                try:
                    result = self._submit_once(
                        user, ticket, seat, passengers, result=result, need_captcha=need_captcha
                    )
                    if result.ok:
                        break
                    if not result.retryable:
                        break
                    log.warning("下单失败(第%d次)，将重试: %s", attempt, result.message)
                except SoldOutError as exc:
                    result.status = ORDER_FAILED
                    result.message = f"余票不足: {exc.message}"
                    result.retryable = True
                    break
                except QueueError as exc:
                    # 排队明确失败：针对这张票再重试没有意义，
                    # 交给引擎换下一个候选（retryable=False）
                    result.status = ORDER_FAILED
                    result.message = f"排队失败: {exc.message}"
                    result.retryable = False
                    break
                except RiskControlError as exc:
                    result.status = ORDER_FAILED
                    result.message = f"触发风控: {exc.message}"
                    result.retryable = True
                    break
                except LoginError as exc:
                    result.status = ORDER_FAILED
                    result.message = f"会话失效: {exc.message}"
                    result.retryable = True
                    break
                except (InterError, ResponseParseError) as exc:
                    result.status = ORDER_FAILED
                    result.message = str(exc)
                    result.retryable = True
                if attempt < max_retry:
                    # 退避重试（GR-07）
                    time.sleep(min(3.0, 0.6 * attempt))
        finally:
            with self._lock:
                self._inflight.discard(key)

        result.latency_ms = int((time.perf_counter() - started) * 1000)
        metrics.observe("order.latency", result.latency_ms)
        metrics.incr("order.success" if result.ok else "order.failed")

        if result.ok:
            with self._lock:
                self._succeeded[key] = result
            self._emit_success(result)

        self._persist(task_id, result)
        return result

    def _submit_once(
        self,
        user: User,
        ticket: TrainTicket,
        seat: SeatInfo,
        passengers: list[dict[str, Any]],
        *,
        result: OrderResult,
        need_captcha: bool,
    ) -> OrderResult:
        api = user.api
        if api is None:
            raise InterError("账号未绑定接口层")

        seat_code = SEAT_CODE.get(seat.seat_type, seat.code or "O")

        # 1) 提交订单请求（锁票）
        api.submit_order_request(ticket, from_name=ticket.from_station, to_name=ticket.to_station)
        result.status = ORDER_SUBMITTED
        result.message = "已提交订单请求"
        log.info("锁票成功: %s %s %s %s", ticket.train_code, ticket.train_date, seat.seat_type, result.passenger)

        # 2) 初始化下单页（拿 REPEAT_SUBMIT_TOKEN）
        dc = api.init_dc()
        token = dc.get("repeat_submit_token", "")

        # 3) 验证码（VC-02）
        rand_code = ""
        if need_captcha:
            try:
                rand_code = self._solve_order_captcha(user)
            except VerifyCodeError as exc:
                # 识别链已用尽（含人工兜底超时）→ 这一轮到此为止，**不往上抛**。
                #
                # 为什么不能交给 ``submit`` 的重试：人工兜底已经等过一个完整超时
                # （``ManualSolver.timeout``，默认 120 秒），上层再重试 ``max_retry``
                # 次只是让他**再白等 N 个 2 分钟**，且不会有任何新信息 ——
                # 与 ``verify/service.py`` 里「人工兜底不再重试」是同一个判断，
                # 那次只改了识别链内部，漏了上层这层重试（回流 #51 附带）。
                #
                # ⚠️ 只包住**识别**这一步：``check_order_info`` 抛的
                # 「官方说验证码错误」依旧走上层重试 —— 那时候换一张新图再试
                # 是有意义的，两者不能混为一谈。
                result.status = ORDER_FAILED
                result.message = f"验证码未通过：{exc.message}"
                result.retryable = False
                log.warning("下单验证码未通过，本轮不再重试: %s", exc.message)
                return result

        # 4) 校验订单信息
        api.check_order_info(passengers, seat_code, token=token, rand_code=rand_code)

        # 5) 查排队人数
        queue_info = api.get_queue_count(ticket, seat_code, token=token)
        result.wait_count = int(queue_info.get("count") or 0)
        result.raw["queue"] = queue_info

        key_check = (
            (api.ticket_info or {}).get("key_check_isChange")
            or (api.ticket_info or {}).get("keyCheckIsChange")
            or ""
        )
        # 兜底必须用 ``yp_info``（余票行下标 12）：老写法拿的是
        # ``seat_types_raw``（下标 15，值是 ``O9M3`` 这种**席别列表**），
        # 那根本不是 ``leftTicketStr``（回流 #61）。
        left_ticket = (api.ticket_info or {}).get("leftTicketStr") or ticket.yp_info

        # 6) 确认排队
        api.confirm_single_for_queue(
            passengers,
            seat_code,
            ticket,
            key_check_is_change=key_check,
            left_ticket=left_ticket,
            token=token,
            rand_code=rand_code,
        )
        result.status = ORDER_QUEUED
        result.message = f"已进入排队（前方 {result.wait_count} 人）"
        log.info("进入排队队列: %s（前方 %d 人）", ticket.train_code, result.wait_count)

        # 7) 轮询出票结果（GR-06）
        queue_result = api.query_order_wait_time(token=token, max_polls=self.queue_max_polls)
        result.raw["queue_result"] = queue_result.to_dict()
        if queue_result.status == "success" and queue_result.order_id:
            result.ok = True
            result.status = ORDER_SUCCESS
            result.order_no = queue_result.order_id
            result.pay_url = api.pay_url_of(queue_result.order_id)
            result.message = "出票成功，请尽快完成支付"
            result.retryable = False
            log.info("🎫 出票成功: %s %s 订单号=%s", ticket.train_code, ticket.train_date, queue_result.order_id)
            return result

        if queue_result.status == "failed":
            result.status = ORDER_FAILED
            result.message = queue_result.message or "排队失败"
            result.retryable = False
            raise QueueError(result.message)

        # 轮询耗尽（timeout）：订单可能仍在服务端处理，标记为「排队中」等待人工确认，
        # 绝不能误报成失败 —— 否则会出现「实际出了票但工具说失败」的致命体验问题
        result.status = ORDER_QUEUED
        result.message = queue_result.message or "排队超时，请到未完成订单中查看"
        result.retryable = False
        return result

    def _solve_order_captcha(self, user: User) -> str:
        """下单阶段验证码识别（VC-02 / VC-03）。"""
        api = user.api
        assert api is not None
        try:
            captcha = api.get_order_captcha()
        except VerifyCodeError as exc:
            log.warning("获取下单验证码失败: %s", exc.message)
            return ""
        if not captcha.image_bytes:
            return ""
        answer = user.verify.recognize(captcha.image_bytes, kind="order")
        log.info("下单验证码识别结果已获取")
        return answer

    # ------------------------------------------------------------------ #
    def _persist(self, task_id: int | None, result: OrderResult) -> None:
        if self.db is None:
            return
        try:
            self.db.save_order(
                {
                    "task_id": task_id,
                    "order_no": result.order_no or f"pending-{int(time.time() * 1000)}-{result.train_code}",
                    "train_no": result.train_code,
                    "depart_date": result.depart_date,
                    "from_station": result.from_station,
                    "to_station": result.to_station,
                    "seat_type": result.seat_type,
                    "passenger": result.passenger,
                    "status": result.status,
                    "message": result.message,
                    "pay_url": result.pay_url,
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.error("订单流水落库失败: %s", exc)

    # ------------------------------------------------------------------ #
    # 订单查询（OD-01 / OD-04）
    # ------------------------------------------------------------------ #
    def query_unfinished(self, user: User) -> list[dict[str, Any]]:
        """拉取待支付订单（未完成订单）。"""
        if user.api is None:
            return []
        try:
            orders = user.api.query_my_order_no_complete()
        except InterError as exc:
            log.warning("查询未完成订单失败: %s", exc)
            return []
        for order in orders:
            order["pay_url"] = user.api.pay_url_of(
                str(order.get("sequence_no") or order.get("order_no") or "")
            )
        return orders

    def history(self, *, task_id: int | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        return self.db.list_orders(status=status, task_id=task_id, limit=limit)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "inflight": len(self._inflight),
                "succeeded_keys": len(self._succeeded),
            }

    def reset_dedup(self) -> None:
        """清空去重表（任务重新启动时调用，允许新一轮尝试）。"""
        with self._lock:
            self._inflight.clear()
            self._succeeded.clear()
