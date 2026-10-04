"""站点选择对话框（任务配置：出发站 / 到达站不再手敲）。

数据来源就是 :class:`~ticket_helper.inter.station.StationMap` ——
启动时从 12306 官方 ``station_name.js`` 拉全量表并缓存到用户目录，离线时退回包内常用站表。
本对话框通过 ``GET /api/stations`` 检索它。

为什么值得单独做个对话框
------------------------
站名必须与 12306 一字不差。手敲「广州南」时很容易写成「广州」或「广州南站」，
而症状是「任务跑起来一条都查不到」——使用者几乎不可能联想到是站名写错了。
搜索 + 点选可以从源头消掉这类错误。

线程模型与 :class:`~desktop_ui.train_picker.TrainPickerDialog` 一致：
检索走工作线程，结果通过 Signal 回主线程渲染，避免网络抖动时窗口假死。
共三层防竞态：**请求序号**（后发请求胜出，迟到的结果丢弃）、
**关键字比对**（回来时输入框已改则丢弃）、**老失败不改状态栏**。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient, ApiError

log = logging.getLogger(__name__)


class StationPickerDialog(QDialog):
    """从站点表里搜索、多选站名。"""

    search_done = Signal(dict)
    search_failed = Signal(str)

    def __init__(self, api: ApiClient, *, selected: list[str] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        #: 请求序号：**后发请求胜出**。
        # 原来是 `if self._busy: return` —— 上一次搜索还没回来时再点「搜索」
        # 会被静默吞掉（按钮明明点了却没反应），而且「新结果先到、旧结果后到」
        # 会把新结果覆盖回去。用序号判定，两个问题一起解决。
        self._seq = 0

        self.setWindowTitle("选择站点（数据来自 12306 站点表）")
        self.resize(760, 480)
        self._build()
        for name in selected or []:
            self._add_chosen(str(name))

        self.search_done.connect(self._render_results)
        self.search_failed.connect(self._on_failed)
        self.search()          # 打开就先把常用站列出来，不用先打字

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("站名关键字，如 广州 / 长沙；留空看常用站")
        self.input.returnPressed.connect(self.search)
        self.btn_search = QPushButton("搜索")
        self.btn_search.setObjectName("primaryButton")
        self.btn_search.clicked.connect(self.search)
        top.addWidget(QLabel("站点："))
        top.addWidget(self.input, 1)
        top.addWidget(self.btn_search)
        root.addLayout(top)

        self.status_label = QLabel("正在查询站点表…")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        body = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("搜索结果（可多选）"))
        self.candidates = QListWidget()
        self.candidates.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.candidates.setToolTip("双击一行即可加入右侧")
        left.addWidget(self.candidates)
        body.addLayout(left, 3)

        middle = QVBoxLayout()
        middle.addStretch(1)
        self.btn_add = QPushButton("加入 →")
        self.btn_add.clicked.connect(self._add_selected)
        self.btn_remove = QPushButton("← 移除")
        self.btn_remove.clicked.connect(self._remove_selected)
        middle.addWidget(self.btn_add)
        middle.addWidget(self.btn_remove)
        middle.addStretch(1)
        body.addLayout(middle)

        right = QVBoxLayout()
        right.addWidget(QLabel("已选站点（顺序即查询顺序）"))
        self.chosen = QListWidget()
        self.chosen.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.chosen.setToolTip("双击一行即可移除")
        right.addWidget(self.chosen)
        body.addLayout(right, 3)
        root.addLayout(body, 1)

        hint = QLabel("站名取自 12306 官方站点表，请点选而不是手敲；搜索不到说明该站名与官方不一致。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        root.addWidget(hint)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setObjectName("primaryButton")
        btn_cancel = QPushButton("取消")
        footer.addWidget(self.btn_ok)
        footer.addWidget(btn_cancel)
        root.addLayout(footer)

        self.candidates.itemDoubleClicked.connect(lambda item: self._add_chosen(item.text()))
        self.chosen.itemDoubleClicked.connect(lambda item: self._drop_chosen(item.text()))
        self.btn_ok.clicked.connect(self._confirm)
        btn_cancel.clicked.connect(self.reject)

    # ------------------------------------------------------------------ #
    # 搜索
    # ------------------------------------------------------------------ #
    def search(self) -> None:
        keyword = self.input.text().strip()
        self._seq += 1
        seq = self._seq
        self.btn_search.setEnabled(False)
        self.status_label.setText("正在查询站点表…")

        def _worker() -> None:
            try:
                items = self.api.station_items(keyword)
            except ApiError as exc:
                self.search_failed.emit({"seq": seq, "message": exc.message})
                return
            except Exception as exc:  # noqa: BLE001 - 工作线程绝不能把异常抛进 Qt
                self.search_failed.emit({"seq": seq, "message": f"搜索失败：{exc}"})
                return
            self.search_done.emit({"seq": seq, "keyword": keyword, "items": items})

        threading.Thread(target=_worker, name="station-search", daemon=True).start()

    def _on_failed(self, payload: Any) -> None:
        data = payload if isinstance(payload, dict) else {"message": str(payload)}
        if int(data.get("seq", 0)) != self._seq:
            return  # 迟到的失败：已经有更新的请求了，别拿它去改状态栏
        self.btn_search.setEnabled(True)
        self.status_label.setText(str(data.get("message", "搜索失败")))

    def _render_results(self, payload: Any) -> None:
        data = payload if isinstance(payload, dict) else {}
        if int(data.get("seq", 0)) != self._seq:
            # 迟到的结果：期间又发起了新搜索，丢弃，否则会把新结果覆盖回去
            return
        self.btn_search.setEnabled(True)

        # 再防一层：结果回来时输入框可能已经改了
        if str(data.get("keyword", "")) != self.input.text().strip():
            return

        items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
        self.candidates.clear()
        for item in items:
            name = str(item.get("name", ""))
            code = str(item.get("code", ""))
            self.candidates.addItem(f"{name}　{code}" if code else name)
        keyword = str(data.get("keyword", ""))
        if not items:
            self.status_label.setText(f"没有匹配「{keyword}」的站点，换个关键字试试（如「广州」）")
        elif keyword:
            self.status_label.setText(f"「{keyword}」匹配到 {len(items)} 个站点，双击即可加入")
        else:
            self.status_label.setText(f"常用站 {len(items)} 个（输入关键字可搜索全部站点）")

    # ------------------------------------------------------------------ #
    # 已选
    # ------------------------------------------------------------------ #
    @staticmethod
    def _name_of(text: str) -> str:
        """候选列表的文本是「站名　电报码」，取站名部分。"""
        return text.split("　")[0].strip()

    def _add_chosen(self, text: str) -> None:
        name = self._name_of(text)
        if not name:
            return
        for index in range(self.chosen.count()):
            if self.chosen.item(index).text() == name:
                return
        self.chosen.addItem(name)

    def _drop_chosen(self, name: str) -> None:
        for item in self.chosen.findItems(name, Qt.MatchFlag.MatchExactly):
            self.chosen.takeItem(self.chosen.row(item))

    def _add_selected(self) -> None:
        for item in self.candidates.selectedItems():
            self._add_chosen(item.text())

    def _remove_selected(self) -> None:
        for item in self.chosen.selectedItems():
            self._drop_chosen(item.text())

    def selected_stations(self) -> list[str]:
        return [self.chosen.item(i).text() for i in range(self.chosen.count())]

    def _confirm(self) -> None:
        if not self.selected_stations():
            self.status_label.setText("还没有选择站点：请在左侧双击要用的站名")
            return
        self.accept()
