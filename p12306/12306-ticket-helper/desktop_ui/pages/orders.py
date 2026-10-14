"""订单记录页：抢到的票都在这里，也是「去哪儿付款」的落脚点。

为什么单独做一页
----------------
抢到票只是第一步 —— **未支付的订单会被 12306 自动取消**，而以前桌面端
只有在系统托盘弹一句「已抢到」，人就不知道该去哪看订单号、怎么去付款。
所以这一页第一列就是订单号和「去 12306 支付」，不让人再去翻日志。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient, ApiError
from desktop_ui.theme import C

# 状态值来自后端唯一的定义处；本页不再自建映射表。
from ticket_helper.order.service import ORDER_FAILED, ORDER_SUCCESS
from ticket_helper.edition import display_train_date

log = logging.getLogger(__name__)

#: 官方订单页（本工具不代扣款，付款到官方做）
OFFICIAL_ORDERS_URL = "https://kyfw.12306.cn/otn/view/train_order.html"

COLUMNS = ["抢票时间", "车次", "乘车日期", "席别", "乘客", "状态", "订单号", "付款"]
#
# 这里曾经有一份 STATE_TEXT = {"success": ..., "failed": ..., "pending": ...}。
# 「pending」在订单表里**根本不存在**（服务层实际写入的是 submitted / queued），
# 于是「已提交 / 排队中」的订单在界面上显示英文原文，而且不会报错。
# 现在文案由后端随订单下发（``order["status_text"]``），本页只负责渲染与上色。


class OrdersPage(QWidget):
    """本地订单记录。"""

    def __init__(self, api: ApiClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self._orders: list[dict[str, Any]] = []
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_official = QPushButton("打开 12306 订单页去支付")
        self.btn_official.setObjectName("primaryButton")
        self.btn_official.setToolTip(
            "本工具不代扣款。出票后请到 12306 官方渠道完成支付，\n"
            "超时未支付的订单会被官方自动取消。"
        )
        self.btn_official.clicked.connect(self.open_official)
        toolbar.addWidget(self.btn_refresh)
        toolbar.addWidget(self.btn_official)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        self.hint = QLabel(
            "本工具<strong>不代扣款</strong>：出票后请点「付款」到 12306 完成支付，"
            "超时未支付的订单会被官方自动取消。"
        )
        self.hint.setTextFormat(Qt.TextFormat.RichText)
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        root.addWidget(self.table, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        try:
            self._orders = self.api.orders(limit=200)
        except ApiError as exc:
            self.status_label.setText(exc.message)
            return
        self._render()

    def _render(self) -> None:
        self.table.setRowCount(0)
        if not self._orders:
            # 空列表说清楚「这里是干嘛的」，而不是一句「暂无数据」
            self.status_label.setText(
                "还没有抢到票的记录。这里的每一行都是一次成功下单 —— "
                "任务跑起来之后，抢到票会自动出现在这里，并有提示音和系统通知。"
            )
            return

        self.table.setRowCount(len(self._orders))
        unpaid = 0
        for row, order in enumerate(self._orders):
            status = str(order.get("status") or "")
            need_pay = bool(order.get("pay_url")) and status == "success"
            if need_pay:
                unpaid += 1
            cells = [
                str(order.get("created_at") or ""),
                str(order.get("train_no") or ""),
                display_train_date(order.get("depart_date")),
                str(order.get("seat_type") or ""),
                str(order.get("passenger") or ""),
                str(order.get("status_text") or status or "-"),
                str(order.get("order_no") or ""),
                "",
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 5 and status == ORDER_SUCCESS:
                    item.setForeground(QBrush(QColor(C["ok"])))
                if column == 5 and status == ORDER_FAILED:
                    item.setForeground(QBrush(QColor(C["bad"])))
                self.table.setItem(row, column, item)

            pay_button = QPushButton("去 12306 支付")
            pay_button.setObjectName("tinyButton")
            pay_url = str(order.get("pay_url") or "")
            if pay_url:
                pay_button.clicked.connect(lambda _=False, u=pay_url: self._open(u))
            else:
                pay_button.setEnabled(False)
                pay_button.setToolTip("这一条没有支付链接（可能已支付或已取消）")
            self.table.setCellWidget(row, len(COLUMNS) - 1, pay_button)

        self.status_label.setText(
            f"共 {len(self._orders)} 条记录"
            + (f"，其中 {unpaid} 条待支付 —— 请尽快到 12306 完成支付。" if unpaid else "。")
        )

    @staticmethod
    def _open(url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    def open_official(self) -> None:
        self._open(OFFICIAL_ORDERS_URL)
