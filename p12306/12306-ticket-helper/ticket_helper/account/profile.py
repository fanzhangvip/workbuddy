"""账号档案（个人中心）：账号信息 / 乘车人 / 我的订单。

为什么单独抽一层而不是让 Web 路由自己拼
----------------------------------------
这三块数据的**失败概率差异很大**：账号信息基本不会失败，乘车人偶尔被风控，
「我的订单」则经常因为日期范围、风控、账号未实名等原因被拒。

如果串成一个方法、共用一个 try，那么「订单查不到」会把「账号信息」一起变成空白 ——
使用者看到一片空，完全无从判断到底哪一块出了问题。

所以这里逐块**独立取数、独立记错**：某一块失败只影响它自己，
返回值里逐块带 ``ok`` / ``error``，界面据此显示「这一块没取到，原因是 X」。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ticket_helper.errors import InterError, TicketHelperError

log = logging.getLogger(__name__)

#: 乘车人缓存有效期（秒）。乘车人变动很少，而个人中心会被反复打开，
#: 每次重开都打一遍接口纯属浪费配额（还会顺便把自己推到风控里）。
PASSENGERS_TTL = 300.0


class AccountProfileService:
    """聚合账号信息 / 乘车人 / 我的订单。"""

    def __init__(self, db: Any | None = None, *, passengers_ttl: float = PASSENGERS_TTL) -> None:
        self.db = db
        self.passengers_ttl = passengers_ttl
        #: username → (时间戳, 乘车人列表)
        self._passenger_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    def _cached_passengers(self, username: str) -> list[dict[str, Any]] | None:
        if self.passengers_ttl <= 0:
            return None
        with self._lock:
            hit = self._passenger_cache.get(username)
        if hit is None:
            return None
        age = time.time() - hit[0]
        if age > self.passengers_ttl:
            return None
        return hit[1]

    def cache_passengers(self, username: str, passengers: list[dict[str, Any]]) -> None:
        with self._lock:
            self._passenger_cache[username] = (time.time(), passengers)

    def invalidate(self, username: str = "") -> None:
        """清缓存（账号重新登录、或乘车人可能变化时调用）。"""
        with self._lock:
            if username:
                self._passenger_cache.pop(username, None)
            else:
                self._passenger_cache.clear()

    # ------------------------------------------------------------------ #
    # 逐块取数
    # ------------------------------------------------------------------ #
    def profile_of(self, user: Any) -> dict[str, Any]:
        """账号信息（个人中心首页）。"""
        if user is None or getattr(user, "api", None) is None:
            return _failed("账号未绑定接口层，无法获取账号信息")
        if not getattr(user, "is_login", False):
            # 未登录时官方会回登录页 HTML，解析失败后报一句「响应格式不对」——
            # 那对使用者毫无帮助。直接说明「需要先登录」。
            return _failed("账号未登录，请先登录后再拉取账号信息")
        try:
            data = user.api.get_user_profile()
        except TicketHelperError as exc:
            return _failed(_message_of(exc))
        if not isinstance(data, dict) or not data.get("username"):
            return _failed("账号信息接口没有返回账号名，可能会话已失效")
        data = dict(data)
        data["username_source"] = user.username
        return _ok(data)

    def passengers_of(self, user: Any, *, refresh: bool = False) -> dict[str, Any]:
        """乘车人列表（个人中心链路，证件号已打码）。"""
        if user is None or getattr(user, "api", None) is None:
            return _failed("账号未绑定接口层，无法获取乘车人")
        if not getattr(user, "is_login", False):
            return _failed("账号未登录，请先登录后再拉取乘车人")
        if not refresh:
            cached = self._cached_passengers(user.username)
            if cached is not None:
                return _ok(cached, cached=True)
        try:
            data = user.api.query_passengers()
        except TicketHelperError as exc:
            return _failed(_message_of(exc))
        self.cache_passengers(user.username, data)
        return _ok(data)

    def orders_of(
        self,
        user: Any,
        *,
        where: str = "G",
        query_type: int = 1,
        start_date: str = "",
        end_date: str = "",
        page_index: int = 1,
        page_size: int = 10,
        keyword: str = "",
    ) -> dict[str, Any]:
        """「我的订单」分页查询。"""
        if user is None or getattr(user, "api", None) is None:
            return _failed("账号未绑定接口层，无法查询订单")
        if not getattr(user, "is_login", False):
            return _failed("账号未登录，请先登录后再查询订单")
        try:
            data = user.api.query_order_history(
                where=where,
                query_type=query_type,
                start_date=start_date,
                end_date=end_date,
                page_index=page_index,
                page_size=page_size,
                keyword=keyword,
            )
        except TicketHelperError as exc:
            return _failed(_message_of(exc))
        return _ok(data)

    def pending_orders_of(self, user: Any) -> dict[str, Any]:
        """未完成（待支付）订单。"""
        if user is None or getattr(user, "api", None) is None:
            return _failed("账号未绑定接口层，无法查询未完成订单")
        if not getattr(user, "is_login", False):
            return _failed("账号未登录，请先登录后再查询未完成订单")
        try:
            orders = user.api.query_my_order_no_complete()
        except TicketHelperError as exc:
            return _failed(_message_of(exc))
        for order in orders:
            order["pay_url"] = user.api.pay_url_of(
                str(order.get("sequence_no") or order.get("order_no") or "")
            )
        return _ok(orders)

    # ------------------------------------------------------------------ #
    def fetch(
        self,
        user: Any,
        *,
        with_passengers: bool = True,
        with_orders: bool = True,
        with_pending: bool = True,
        where: str = "G",
        page_index: int = 1,
        page_size: int = 10,
        refresh_passengers: bool = False,
    ) -> dict[str, Any]:
        """一次拿齐个人中心需要的内容。

        返回结构（每块都带 ``ok`` / ``error``，界面按块渲染）::

            {
              "account":   {"username": ..., "online": True},
              "profile":   {"ok": True, "data": {...}},
              "passengers":{"ok": True, "data": [...]},
              "orders":    {"ok": True, "data": {"orders": [...], ...}},
              "pending":   {"ok": True, "data": [...]},
            }
        """
        result: dict[str, Any] = {
            "account": {
                "username": getattr(user, "username", ""),
                "online": bool(getattr(user, "is_login", False)),
            },
            "profile": self.profile_of(user),
        }
        if with_passengers:
            result["passengers"] = self.passengers_of(user, refresh=refresh_passengers)
        if with_pending:
            result["pending"] = self.pending_orders_of(user)
        if with_orders:
            result["orders"] = self.orders_of(user, where=where, page_index=page_index, page_size=page_size)
        return result


def _ok(data: Any, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": "", **extra}


def _failed(message: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": message or "未知错误"}


def _message_of(exc: BaseException) -> str:
    """把类型化异常转成一句给使用者看的话。"""
    if isinstance(exc, InterError):
        return exc.message
    return str(exc) or "未知错误"
