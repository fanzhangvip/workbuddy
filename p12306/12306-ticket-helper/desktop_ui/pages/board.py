"""余票看板页（UI-04）：分任务实时刷新余票、命中高亮。"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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

from ticket_helper.edition import display_train_date

log = logging.getLogger(__name__)


class RemainBoardPage(QWidget):
    """实时余票看板。"""

    def __init__(self, api: ApiClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self._tasks: list[dict] = []
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self._tick)
        if self.check_auto.isChecked():
            self.timer.start()
        self.refresh_tasks()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("任务："))
        self.combo_task = QComboBox()
        self.combo_task.setMinimumWidth(240)
        toolbar.addWidget(self.combo_task)
        btn_refresh = QPushButton("刷新")
        toolbar.addWidget(btn_refresh)
        self.check_auto = QCheckBox("自动刷新（3s）")
        self.check_auto.setChecked(True)
        toolbar.addWidget(self.check_auto)
        toolbar.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        toolbar.addWidget(self.status_label)
        root.addLayout(toolbar)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["车次", "日期", "区间", "出发", "到达", "席别", "余票"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(self.table, 1)

        btn_refresh.clicked.connect(self.refresh)
        self.combo_task.currentIndexChanged.connect(self.refresh)
        self.check_auto.toggled.connect(self._on_auto_toggled)

    # ------------------------------------------------------------------ #
    def _on_auto_toggled(self, enabled: bool) -> None:
        if enabled:
            self.timer.start()
        else:
            self.timer.stop()

    def _tick(self) -> None:
        if self.isVisible():
            self.refresh()

    # ------------------------------------------------------------------ #
    def refresh_tasks(self) -> None:
        try:
            tasks = self.api.list_tasks()
        except ApiError as exc:
            self.status_label.setText(exc.message)
            return
        self._tasks = tasks
        current = self.combo_task.currentData()
        self.combo_task.blockSignals(True)
        self.combo_task.clear()
        for task in tasks:
            self.combo_task.addItem(f"{task.get('name')}（#{task.get('id')}）", int(task["id"]))
        if current is not None:
            index = self.combo_task.findData(current)
            if index >= 0:
                self.combo_task.setCurrentIndex(index)
        self.combo_task.blockSignals(False)
        self.refresh()

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        task_id = self.combo_task.currentData()
        if task_id is None:
            self.status_label.setText("请先创建一个任务")
            self.table.setRowCount(0)
            return
        try:
            payload = self.api.task_remain(int(task_id))
        except ApiError as exc:
            self.status_label.setText(exc.message)
            return

        # 任务列表可能新增了任务，顺手同步一次下拉框（低频，避免抖动）
        candidates = payload.get("candidates") or []
        self._render(candidates)
        state = payload.get("state", "")
        running = payload.get("running")
        self.status_label.setText(
            f"共 {len(candidates)} 个可购匹配项　"
            + (f"任务状态：{state}（运行中）" if running else f"任务状态：{state}（未运行）")
        )

    def _render(self, candidates: list[dict]) -> None:
        self.table.setRowCount(len(candidates))
        for row, item in enumerate(candidates):
            values = [
                item.get("train_code", ""),
                display_train_date(item.get("train_date")),
                f"{item.get('from_station', '')} → {item.get('to_station', '')}",
                item.get("start_time", ""),
                item.get("arrive_time", ""),
                item.get("seat_type", ""),
                str(item.get("remain", "")),
            ]
            for col, text in enumerate(values):
                cell = QTableWidgetItem(str(text))
                cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                # 命中行整行高亮（UI-04 要求）
                cell.setBackground(QBrush(QColor("#e8f5fe")))
                if col == 6:
                    cell.setForeground(QBrush(QColor(C["ok"])))
                    font = cell.font()
                    font.setBold(True)
                    cell.setFont(font)
                self.table.setItem(row, col, cell)
        if not candidates:
            self.table.setRowCount(1)
            placeholder = QTableWidgetItem("当前没有匹配的可购席位（任务运行中会自动刷新）")
            placeholder.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setForeground(QBrush(QColor(C["muted"])))
            self.table.setItem(0, 0, placeholder)
            self.table.setSpan(0, 0, 1, 7)
        else:
            self.table.clearSpans()
