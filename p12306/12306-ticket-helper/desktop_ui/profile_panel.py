"""个人中心面板：账号信息 / 乘车人 / 我的订单 / 待支付。

**这是一个可复用控件**，而不是弹窗本体：
- 「个人中心」页面（左侧导航）内嵌它 —— 这是主要入口；
- 账号列表的「详情」按钮用弹窗包一层它 —— 这是快捷入口。

两处共用同一份实现，避免「页面改了、弹窗忘了改」的双份逻辑。

三个设计要点（都是踩出来的）
----------------------------
1. **四块数据独立取、独立报错**。它们分散在不同接口上，失败概率差别很大
   （订单接口最容易因日期范围 / 风控被拒）。若串成一次 try，
   「订单查不到」会把账号信息一起变空白，使用者完全无从判断问题在哪。
2. **未登录也要把界面显示出来**，用横幅说明原因并就地提供扫码入口。
   扫码账号重启后未登录是**常态不是异常**；把人挡在门外只会让人以为功能没做。
3. **取数放后台线程**，渲染只在主线程。这里会连打好几个真机接口，
   慢起来十几秒，放 GUI 线程会让窗口失去响应。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient, ApiError

from ticket_helper.edition import display_train_date

log = logging.getLogger(__name__)

STATUS_TEXT = {
    "online": "在线",
    "offline": "离线",
    "risk": "风控",
    "logining": "登录中",
    "error": "异常",
    "disabled": "已禁用",
}

#: 账号信息页签要展示的字段：(中文名, profile 里的键)
PROFILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("账号", "username"),
    ("真实姓名", "real_name"),
    ("用户类型", "user_type"),
    ("性别", "sex"),
    ("手机号", "mobile"),
    ("邮箱", "email"),
    ("证件类型", "id_type"),
    ("证件号", "id_no"),
    ("积分", "points"),
    ("注册时间", "register_time"),
)


def verified_text(profile: dict[str, Any]) -> str:
    """人证核验三态。

    ``verified`` 为 ``None`` 表示接口没给明确字段 —— 显示「无法判定」而不是
    「未核验」。后者会让人以为账号被 12306 限制了，属于有害的误导。
    """
    value = profile.get("verified")
    if value is True:
        return "已核验"
    if value is False:
        return "未核验（候补会被拦）"
    return "无法判定"


def _cell(text: Any) -> QTableWidgetItem:
    item = QTableWidgetItem("" if text is None else str(text))
    item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
    return item


def _section_ok(section: Any) -> bool:
    return bool(isinstance(section, dict) and section.get("ok"))


def _section_error(section: Any) -> str:
    if not isinstance(section, dict):
        return "后台没有返回这块数据"
    return str(section.get("error") or "未知错误")


def make_table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


class ProfilePanel(QWidget):
    """账号档案的内容区（不含外层窗口）。"""

    profile_ready = Signal(object)
    orders_ready = Signal(object)
    load_failed = Signal(str)
    #: 「扫码登录」被点击（由外层决定怎么弹扫码框）
    relogin_requested = Signal()
    #: 扫码成功后通知外层刷新账号列表
    relogin_succeeded = Signal(str)
    #: 切换到另一个账号（页面用它同步标题等）
    user_changed = Signal(object)

    def __init__(self, api: ApiClient, *, embedded: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        #: True = 嵌在页面里（不显示「关闭」按钮，标题由页面负责）
        self.embedded = embedded
        self.user: dict[str, Any] = {}
        self.user_id = 0
        self.username = ""

        self._order_where = "G"
        self._order_page = 1
        self._order_pages = 1
        self._orders_busy = False
        self.logged_in_username = ""

        self._build()
        self._wire()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        #: 嵌入模式下才有的提示标签（外层负责标题时用它显示「账号：xxx（未登录）」）。
        #: 必须两个分支都先置 None，否则非嵌入模式下访问它会 AttributeError。
        self.hint_label: QLabel | None = None

        if not self.embedded:
            header = QHBoxLayout()
            self.title_label = QLabel("账号详情")
            self.header_status = QLabel("")
            self.header_status.setObjectName("muted")
            self.btn_relogin = QPushButton("扫码登录")
            self.btn_relogin.setToolTip("该账号当前没有有效会话，扫码后即可拉取个人中心信息")
            self.btn_reload = QPushButton("刷新")
            self.btn_reload.setObjectName("primaryButton")
            header.addWidget(self.title_label)
            header.addWidget(self.header_status, 1)
            header.addWidget(self.btn_relogin)
            header.addWidget(self.btn_reload)
            root.addLayout(header)
        else:
            self.title_label = None
            self.header_status = None
            bar = QHBoxLayout()
            self.hint_label = QLabel("")
            self.hint_label.setObjectName("muted")
            self.btn_relogin = QPushButton("扫码登录")
            self.btn_relogin.setToolTip("该账号当前没有有效会话，扫码后即可拉取个人中心信息")
            self.btn_reload = QPushButton("刷新")
            self.btn_reload.setObjectName("primaryButton")
            bar.addWidget(self.hint_label, 1)
            bar.addWidget(self.btn_relogin)
            bar.addWidget(self.btn_reload)
            root.addLayout(bar)

        # 未登录横幅：**界面照常显示**，只是在这里说清楚为什么拿不到数据、
        # 以及下一步该干什么。
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.banner.setStyleSheet(
            "background: rgba(254, 243, 199, 0.92);"
            "border: 1px solid rgba(252, 211, 77, 0.75); border-radius: 8px;"
            "padding: 9px 11px; color: #92400e;"
        )
        root.addWidget(self.banner)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_profile_tab(), "账号信息")
        self.tabs.addTab(self._build_passengers_tab(), "乘车人")
        self.tabs.addTab(self._build_orders_tab(), "我的订单")
        self.tabs.addTab(self._build_pending_tab(), "待支付")
        root.addWidget(self.tabs, 1)

        tip = QLabel("信息实时来自 12306 官方接口；证件号与手机号按官方口径打码展示。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        root.addWidget(tip)

    def _build_profile_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.profile_values: dict[str, QLabel] = {}
        for label, key in PROFILE_FIELDS:
            value = QLabel("-")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.profile_values[key] = value
            form.addRow(f"{label}：", value)
        self.profile_values["verified"] = QLabel("-")
        form.addRow("人证核验：", self.profile_values["verified"])
        self.profile_values["unfinished"] = QLabel("-")
        form.addRow("待支付订单：", self.profile_values["unfinished"])
        # 这两项是**本工具侧**的状态，不是 12306 返回的
        self.profile_values["session_saved"] = QLabel("-")
        form.addRow("免扫码登录：", self.profile_values["session_saved"])
        self.profile_values["login_type"] = QLabel("-")
        form.addRow("登录方式：", self.profile_values["login_type"])
        layout.addLayout(form)
        layout.addStretch(1)
        self.profile_status = self._status_label(layout)
        return page

    def _build_passengers_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("乘车人变动少，默认使用 5 分钟缓存；改过乘车人后点「强制刷新」"))
        bar.addStretch(1)
        self.btn_pass_refresh = QPushButton("强制刷新")
        bar.addWidget(self.btn_pass_refresh)
        layout.addLayout(bar)

        self.pass_table = make_table(["姓名", "证件类型", "证件号", "票种", "手机", "核验状态", "本人"])
        layout.addWidget(self.pass_table, 1)
        self.pass_status = self._status_label(layout)
        return page

    def _build_orders_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("范围"))
        self.order_where = QComboBox()
        self.order_where.addItem("未出行订单", "G")
        self.order_where.addItem("历史订单", "H")
        bar.addWidget(self.order_where)
        self.order_kw = QLineEdit()
        self.order_kw.setPlaceholderText("订单号 / 车次 / 乘客姓名（可留空）")
        bar.addWidget(self.order_kw, 1)
        self.btn_order_query = QPushButton("查询")
        self.btn_order_query.setObjectName("primaryButton")
        self.btn_prev = QPushButton("上一页")
        self.btn_next = QPushButton("下一页")
        bar.addWidget(self.btn_order_query)
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.btn_next)
        layout.addLayout(bar)

        self.orders_table = make_table(
            ["订单号", "订票时间", "车次", "乘车日期", "行程", "席别", "乘客", "金额", "状态", "座位"]
        )
        layout.addWidget(self.orders_table, 1)
        self.orders_status = self._status_label(layout)
        return page

    def _build_pending_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "本工具<b>不代扣款</b>：出票后请点「去官方支付」到 12306 完成支付，"
            "超时未支付的订单会被官方自动取消。"
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        self.pending_table = make_table(["订单号", "车次", "乘车日期", "乘客", "金额", "状态", "操作"])
        layout.addWidget(self.pending_table, 1)
        self.pending_status = self._status_label(layout)
        return page

    @staticmethod
    def _status_label(layout: QVBoxLayout) -> QLabel:
        label = QLabel("")
        label.setObjectName("muted")
        label.setWordWrap(True)
        layout.addWidget(label)
        return label

    # ------------------------------------------------------------------ #
    def _wire(self) -> None:
        self.btn_reload.clicked.connect(self.reload)
        self.btn_relogin.clicked.connect(self.relogin_requested.emit)
        self.btn_pass_refresh.clicked.connect(lambda: self.reload(force_passengers=True))
        self.btn_order_query.clicked.connect(lambda: self.load_orders(page=1))
        self.order_where.currentIndexChanged.connect(lambda: self.load_orders(page=1))
        self.order_kw.returnPressed.connect(lambda: self.load_orders(page=1))
        self.btn_prev.clicked.connect(lambda: self.load_orders(page=self._order_page - 1))
        self.btn_next.clicked.connect(lambda: self.load_orders(page=self._order_page + 1))

        self.profile_ready.connect(self._render_profile_payload)
        self.orders_ready.connect(self._render_orders)
        self.load_failed.connect(self._on_failed)

    # ------------------------------------------------------------------ #
    # 账号切换
    # ------------------------------------------------------------------ #
    def set_user(self, user: dict[str, Any], *, autoload: bool = True) -> None:
        self.user = dict(user or {})
        self.user_id = int(self.user.get("id") or 0)
        self.username = str(self.user.get("username") or "")
        self._order_page = 1
        self._order_pages = 1
        if self.title_label is not None:
            self.title_label.setText(f"<b>{self.username}</b>")
        # 本工具侧的两项状态（免扫码 / 登录方式）来自账号列表而不是 12306 接口，
        # 所以切换账号时立刻刷新，不用等一次网络往返 —— 否则会短暂显示上一个账号的值。
        self._render_local_state()
        self.user_changed.emit(self.user)
        if autoload:
            self.reload()

    # ------------------------------------------------------------------ #
    # 扫码登录
    # ------------------------------------------------------------------ #
    def on_relogin_ok(self, username: str) -> None:
        """外层扫码成功后调用：切回「已在线」并重新取数。"""
        self.user["online"] = True
        self.user["status"] = "online"
        self.logged_in_username = username
        self.relogin_succeeded.emit(username)
        self.reload(force_passengers=True)

    # ------------------------------------------------------------------ #
    # 取数
    # ------------------------------------------------------------------ #
    def reload(self, *, force_passengers: bool = False) -> None:
        if not self.user_id:
            self._set_all_status("请先在上方选择一个账号")
            return
        if not self.user.get("online"):
            self._show_offline_banner()
        else:
            # 连文案一起清掉：只 setVisible(False) 会把旧文案留在控件里，
            # 一旦之后再显示就会闪出过期的提示。
            self.banner.clear()
            self.banner.setVisible(False)
            if self.hint_label is not None:
                self.hint_label.setText(f"账号：{self.username}（已登录）")

        self.btn_reload.setEnabled(False)
        if self.header_status is not None:
            self.header_status.setText("正在拉取…")
        for label in (self.profile_status, self.pass_status, self.pending_status):
            label.setText("正在拉取…")

        def _worker() -> None:
            try:
                payload = self.api.user_profile(
                    self.user_id,
                    with_passengers=True,
                    with_pending=True,
                    with_orders=False,
                    refresh=force_passengers,
                )
            except ApiError as exc:
                self.load_failed.emit(exc.message)
                return
            except Exception as exc:  # noqa: BLE001 - 工作线程绝不能把异常抛进 Qt
                self.load_failed.emit(f"拉取失败：{exc}")
                return
            self.profile_ready.emit(payload)

        threading.Thread(target=_worker, name=f"profile-{self.user_id}", daemon=True).start()
        self.load_orders(page=1)

    def load_orders(self, *, page: int) -> None:
        if self._orders_busy or not self.user_id:
            return
        page = max(1, int(page))
        if page > self._order_pages:
            return
        self._orders_busy = True
        self.btn_order_query.setEnabled(False)
        self.orders_status.setText("正在查询订单…")
        where = str(self.order_where.currentData() or "G")
        keyword = self.order_kw.text().strip()

        def _worker() -> None:
            try:
                section = self.api.user_orders(
                    self.user_id, where=where, page=page, size=10, keyword=keyword
                )
            except ApiError as exc:
                section = {"ok": False, "error": exc.message, "data": None}
            except Exception as exc:  # noqa: BLE001
                section = {"ok": False, "error": f"查询失败：{exc}", "data": None}
            self.orders_ready.emit(section)

        threading.Thread(target=_worker, name=f"orders-{self.user_id}-{page}", daemon=True).start()

    # ------------------------------------------------------------------ #
    # 渲染（只在主线程）
    # ------------------------------------------------------------------ #
    def _render_profile_payload(self, payload: Any) -> None:
        payload = payload if isinstance(payload, dict) else {}
        self.btn_reload.setEnabled(True)
        self._render_profile(payload.get("profile") or {})
        self._render_passengers(payload.get("passengers") or {})
        self._render_pending(payload.get("pending") or {})
        if self.header_status is not None:
            self.header_status.setText(STATUS_TEXT.get(str(self.user.get("status")), ""))

    def _render_local_state(self) -> None:
        """刷新「免扫码登录 / 登录方式」两行（数据来自账号列表，不来自 12306）。"""
        self.profile_values["session_saved"].setText(
            "已保存（重启后无需重新扫码）" if self.user.get("session_saved") else "未保存（重启后需重新扫码登录）"
        )
        self.profile_values["login_type"].setText(
            "扫码登录" if not self.user.get("has_password") else "账号密码"
        )

    def _render_profile(self, section: Any) -> None:
        self._render_local_state()
        if not _section_ok(section):
            self.profile_status.setText(f"账号信息未取到：{_section_error(section)}")
            for key, label in self.profile_values.items():
                if key in ("session_saved", "login_type", "unfinished"):
                    continue
                label.setText("-")
            return
        data = section.get("data") or {}
        for key, label in self.profile_values.items():
            if key in ("verified", "unfinished", "session_saved", "login_type"):
                continue
            label.setText(str(data.get(key) or "-"))
        self.profile_values["verified"].setText(verified_text(data))
        self.profile_status.setText("")

    def _render_passengers(self, section: Any) -> None:
        if not _section_ok(section):
            self.pass_table.setRowCount(0)
            self.pass_status.setText(f"乘车人未取到：{_section_error(section)}")
            return
        rows = section.get("data") or []
        self.pass_table.setRowCount(len(rows))
        for row, passenger in enumerate(rows):
            values = [
                passenger.get("passenger_name", ""),
                passenger.get("passenger_id_type_name") or passenger.get("passenger_id_type_code", ""),
                passenger.get("passenger_id_no", ""),
                passenger.get("passenger_type_name") or passenger.get("passenger_type", ""),
                passenger.get("mobile_no", ""),
                passenger.get("verify_status_name", ""),
                "是" if passenger.get("is_self") else "",
            ]
            for col, text in enumerate(values):
                self.pass_table.setItem(row, col, _cell(text))
        cached = "（缓存）" if section.get("cached") else ""
        self.pass_status.setText(f"共 {len(rows)} 位乘车人{cached}")

    def _render_orders(self, section: Any) -> None:
        self._orders_busy = False
        self.btn_order_query.setEnabled(True)
        if not _section_ok(section):
            self.orders_table.setRowCount(0)
            self.orders_status.setText(f"订单未取到：{_section_error(section)}")
            self._order_pages = 1
            self._update_page_buttons()
            return

        data = section.get("data") or {}
        orders = data.get("orders") or []
        self._order_page = int(data.get("page_index") or 1)
        self._order_pages = max(1, int(data.get("page_count") or 1))

        self.orders_table.setRowCount(len(orders))
        for row, order in enumerate(orders):
            seat = ""
            passengers = order.get("passengers") or []
            if passengers:
                first = passengers[0]
                seat = f"{first.get('coach_no', '')}车{first.get('seat_no', '')}"
            values = [
                order.get("order_no", ""),
                order.get("order_date", ""),
                order.get("train_code", ""),
                display_train_date(order.get("train_date")),
                f"{order.get('from_station', '')}→{order.get('to_station', '')}",
                order.get("seat_type", ""),
                order.get("passenger_names", ""),
                order.get("price", ""),
                order.get("status", ""),
                seat,
            ]
            for col, text in enumerate(values):
                self.orders_table.setItem(row, col, _cell(text))

        scope = "未出行" if data.get("where") == "G" else "历史"
        # 把「实际查了什么」写出来：订单为空时，使用者最需要知道的是
        # 「你查的是近 N 天 / 按哪个日期口径」——否则他只能怀疑是程序坏了。
        window = data.get("window_days")
        detail = ""
        if window:
            detail = f"（近 {window} 天·按{'乘车' if data.get('query_type') == 2 else '订票'}日期）"
        text = f"{scope}订单 {len(orders)} 条{detail} · 第 {self._order_page}/{self._order_pages} 页"
        if not orders:
            # 一条都没有时，把官方的说法直接摆出来 —— 「真没订单」和
            # 「官方有单但我们没取到」是两件事，不写清楚就会一直当成 bug 查。
            total = data.get("total_number")
            if total == 0:
                text += "　官方返回：名下没有订单"
            elif isinstance(total, int) and total > 0:
                text += f"　注意：官方称名下共 {total} 单，但本窗口没取到 —— 换个范围或口径再试"
        self.orders_status.setText(text)
        self._update_page_buttons()

    def _update_page_buttons(self) -> None:
        self.btn_prev.setEnabled(self._order_page > 1)
        self.btn_next.setEnabled(self._order_page < self._order_pages)

    def _render_pending(self, section: Any) -> None:
        if not _section_ok(section):
            self.pending_table.setRowCount(0)
            self.pending_status.setText(f"待支付订单未取到：{_section_error(section)}")
            self.profile_values["unfinished"].setText("-")
            return
        orders = section.get("data") or []
        self.pending_table.setRowCount(len(orders))
        for row, order in enumerate(orders):
            order_no = str(order.get("sequence_no") or order.get("order_no") or "")
            passengers = order.get("passengerDTOs") or order.get("passengers") or []
            names = ",".join(
                str(p.get("passenger_name") or p.get("name") or "") for p in passengers if isinstance(p, dict)
            )
            values = [
                order_no,
                order.get("station_train_code") or order.get("train_code", ""),
                display_train_date(order.get("train_date")),
                names,
                order.get("ticket_total_price") or order.get("total_price", ""),
                order.get("order_status_name") or order.get("status", ""),
            ]
            for col, text in enumerate(values):
                self.pending_table.setItem(row, col, _cell(text))
            pay_url = str(order.get("pay_url") or "")
            if pay_url:
                button = QPushButton("去官方支付")
                button.setObjectName("tinyButton")
                button.clicked.connect(lambda _=False, url=pay_url: self._open_pay(url))
                self.pending_table.setCellWidget(row, 6, button)
        self.pending_status.setText(f"待支付订单 {len(orders)} 个")
        self.profile_values["unfinished"].setText(str(len(orders)))

    @staticmethod
    def _open_pay(url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    def _show_offline_banner(self) -> None:
        self.banner.setText(
            f"⚠ 账号「{self.username}」当前<b>未登录</b>，以下信息需要有效登录态才能拉取。"
            "<br>点<b>「扫码登录」</b>即可，登录成功后这里会自动刷新；"
            "本次扫码成功后会<b>把会话加密记住</b>，之后重启程序不必再扫。"
        )
        self.banner.setVisible(True)
        if self.hint_label is not None:
            self.hint_label.setText(f"账号：{self.username}（未登录）")

    def _set_all_status(self, message: str) -> None:
        for label in (self.profile_status, self.pass_status, self.pending_status, self.orders_status):
            label.setText(message)
        self.profile_values["unfinished"].setText("-")
        self.profile_values["session_saved"].setText("-")
        self.profile_values["login_type"].setText("-")

    def _on_failed(self, message: str) -> None:
        self.btn_reload.setEnabled(True)
        self._orders_busy = False
        self.btn_order_query.setEnabled(True)
        if self.header_status is not None:
            self.header_status.setText(f"拉取失败：{message}")
        for label in (self.profile_status, self.pass_status, self.pending_status):
            if label.text() == "正在拉取…":
                label.setText(f"未取到：{message}")
        for key, label in self.profile_values.items():
            if key in ("session_saved", "login_type"):
                continue
            label.setText("-")
