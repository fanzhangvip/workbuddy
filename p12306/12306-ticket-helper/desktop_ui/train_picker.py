"""任务配置页的「从 12306 拉取车次」对话框（UI-03 补充）。

为什么单独弹一个窗口
--------------------
一条线路当天的车次常有十几到几十班，还要看发车时间、历时、余票才能决定勾哪些。
把这些塞进表单，会把「填基础信息」本身挤没了；所以拉取结果单独一个窗口，
勾完再一次性应用回「车次白名单 / 黑名单」输入框。

线程模型（与 :mod:`desktop_ui.profile_panel` 一致）
---------------------------------------------------
查询要挨个日期/站点打 12306，慢起来十几秒，放在 GUI 线程里窗口会假死。
因此网络请求走工作线程，结果通过 Signal 回到主线程渲染。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QRadioButton,
    QButtonGroup,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient, ApiError

log = logging.getLogger(__name__)

COLUMNS = ["选择", "车次", "类型", "发车", "到达", "历时", "余票", "日期"]
CHECKED = "☑"
UNCHECKED = "☐"


class TrainPickerDialog(QDialog):
    """从 12306 拉取车次并勾选，返回 ``(车次列表, 目标名单)``。"""

    payload_ready = Signal(dict)
    load_failed = Signal(str)

    TARGET_WHITE = "white"
    TARGET_BLACK = "black"

    def __init__(
        self,
        api: ApiClient,
        *,
        dates: str,
        from_stations: str,
        to_stations: str,
        target: str = TARGET_WHITE,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.api = api
        self.dates = dates
        self.from_stations = from_stations
        self.to_stations = to_stations
        self.target_default = target
        self._rows: list[dict[str, Any]] = []
        self._selected: set[str] = set()
        self._busy = False

        self.setWindowTitle("从 12306 拉取车次")
        self.resize(940, 560)
        self._build()

        self.payload_ready.connect(self._render)
        self.load_failed.connect(self._on_failed)
        self.reload()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)

        self.status_label = QLabel("正在从 12306 拉取车次…（第一次查询要几秒）")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("勾选的车次加入："))
        self.radio_white = QRadioButton("车次白名单")
        # 从「黑名单」那一行点进来的人，默认就是想加入黑名单 —— 不用再手动切一次
        self.radio_white.setChecked(self.target_default != self.TARGET_BLACK)
        self.radio_white.setToolTip("白名单 = 只抢这些车次，列表顺序即抢票优先级")
        self.radio_black = QRadioButton("车次黑名单")
        self.radio_black.setChecked(self.target_default == self.TARGET_BLACK)
        self.radio_black.setToolTip("黑名单 = 这些车次一律不抢")
        group = QButtonGroup(self)
        group.addButton(self.radio_white)
        group.addButton(self.radio_black)
        toolbar.addWidget(self.radio_white)
        toolbar.addWidget(self.radio_black)
        toolbar.addStretch(1)

        self.btn_all = QPushButton("全选")
        self.btn_all.setObjectName("tinyButton")
        self.btn_none = QPushButton("清空勾选")
        self.btn_none.setObjectName("tinyButton")
        self.btn_reload = QPushButton("重新拉取")
        self.btn_reload.setObjectName("tinyButton")
        for button in (self.btn_all, self.btn_none, self.btn_reload):
            toolbar.addWidget(button)
        root.addLayout(toolbar)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.btn_apply = QPushButton("应用并关闭")
        self.btn_apply.setObjectName("primaryButton")
        self.btn_apply.setEnabled(False)
        btn_cancel = QPushButton("取消")
        footer.addWidget(self.btn_apply)
        footer.addWidget(btn_cancel)
        root.addLayout(footer)

        self.table.cellClicked.connect(self._toggle_row)
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        self.btn_reload.clicked.connect(lambda: self.reload(refresh=True))
        self.btn_apply.clicked.connect(self._apply)
        btn_cancel.clicked.connect(self.reject)

    # ------------------------------------------------------------------ #
    # 取数
    # ------------------------------------------------------------------ #
    def reload(self, *, refresh: bool = False) -> None:
        if self._busy:
            return
        self._busy = True
        self.btn_apply.setEnabled(False)
        self.btn_reload.setEnabled(False)
        self.status_label.setText("正在从 12306 拉取车次…（第一次查询要几秒）")

        def _worker() -> None:
            try:
                payload = self.api.train_options(
                    dates=self.dates,
                    from_stations=self.from_stations,
                    to_stations=self.to_stations,
                    refresh=refresh,
                )
            except ApiError as exc:
                self.load_failed.emit(exc.message)
                return
            except Exception as exc:  # noqa: BLE001 - 工作线程绝不能把异常抛进 Qt
                self.load_failed.emit(f"拉取失败：{exc}")
                return
            self.payload_ready.emit(payload)

        threading.Thread(target=_worker, name="train-options", daemon=True).start()

    def _on_failed(self, message: str) -> None:
        self._busy = False
        self.btn_reload.setEnabled(True)
        self.status_label.setText(message)

    # ------------------------------------------------------------------ #
    # 渲染（只在主线程）
    # ------------------------------------------------------------------ #
    def _render(self, payload: Any) -> None:
        data = payload if isinstance(payload, dict) else {}
        self._busy = False
        self.btn_reload.setEnabled(True)
        self._rows = [row for row in (data.get("trains") or []) if isinstance(row, dict)]
        self._selected.clear()

        # 拉车次不需要登录：没有在线账号时后端用匿名会话查，这里如实说明，
        # 免得使用者以为「没登录就没法拉」而去做一次没必要的扫码。
        notes = [
            "匿名查询（未登录也能拉，抢票才需要登录）"
            if data.get("anonymous")
            else f"账号 {data.get('account') or '-'}",
            f"已查 {data.get('queried') or 0}/{data.get('combos') or 0} 个「日期 × 起止站」组合",
        ]
        if data.get("fallback_from"):
            notes.append(f"账号 {data['fallback_from']} 的会话不可用，已改用匿名查询")
        if data.get("cached"):
            notes.append("60 秒内缓存（点「重新拉取」可强制刷新）")
        if data.get("truncated"):
            notes.append(f"组合数超过上限，只查了前 {data.get('combos') or 0} 个")
        errors = [str(e) for e in (data.get("errors") or [])]
        if errors:
            notes.append("部分组合没查到：" + "；".join(errors))
        if data.get("risk_limited"):
            notes.append("当前被官方限流，建议稍后再试")
        self.status_label.setText(" · ".join(notes))

        self.table.setRowCount(len(self._rows))
        for row_index, train in enumerate(self._rows):
            available = train.get("available_seats") or []
            values = [
                UNCHECKED,
                str(train.get("train_code", "")),
                str(train.get("train_type", "")),
                str(train.get("start_time", "")),
                str(train.get("arrive_time", "")),
                str(train.get("duration", "")),
                str(train.get("remain_text", "")),
                " / ".join(train.get("dates") or []),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 6 and not available:
                    item.setForeground(QBrush(QColor("#64748b")))
                elif col == 6:
                    item.setForeground(QBrush(QColor("#15803d")))
                self.table.setItem(row_index, col, item)

        if not self._rows:
            self.status_label.setText(
                (self.status_label.text() + "　")
                + "没有拉到车次：请确认日期与起止站、以及是否有在线账号"
            )
        self.btn_apply.setEnabled(bool(self._rows))

    # ------------------------------------------------------------------ #
    # 勾选
    # ------------------------------------------------------------------ #
    def _toggle_row(self, row_index: int, _column: int = 0) -> None:
        if row_index < 0 or row_index >= len(self._rows):
            return
        code = str(self._rows[row_index].get("train_code", ""))
        if not code:
            return
        if code in self._selected:
            self._selected.discard(code)
        else:
            self._selected.add(code)
        item = self.table.item(row_index, 0)
        if item is not None:
            item.setText(CHECKED if code in self._selected else UNCHECKED)

    def _set_all(self, checked: bool) -> None:
        for row_index, train in enumerate(self._rows):
            code = str(train.get("train_code", ""))
            if not code:
                continue
            if checked:
                self._selected.add(code)
            else:
                self._selected.discard(code)
            item = self.table.item(row_index, 0)
            if item is not None:
                item.setText(CHECKED if checked else UNCHECKED)

    # ------------------------------------------------------------------ #
    def target(self) -> str:
        return self.TARGET_BLACK if self.radio_black.isChecked() else self.TARGET_WHITE

    def selected_codes(self) -> list[str]:
        """按表格顺序（= 发车时间）返回勾选的车次，正好可当白名单优先级。"""
        return [str(t.get("train_code", "")) for t in self._rows if str(t.get("train_code", "")) in self._selected]

    def _apply(self) -> None:
        if not self.selected_codes():
            self.status_label.setText("请先勾选要加入的车次")
            return
        self.accept()
