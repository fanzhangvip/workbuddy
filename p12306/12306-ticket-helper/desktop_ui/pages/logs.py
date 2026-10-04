"""实时日志窗（UI-05）：分色分级、自动滚动、可导出。"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient, ApiError

log = logging.getLogger(__name__)

LEVEL_COLORS = {
    "DEBUG": "#38bdf8",
    "INFO": "#4ade80",
    "WARNING": "#fbbf24",
    "ERROR": "#f87171",
    "CRITICAL": "#f87171",
}

MAX_LINES = 2000


class LogPage(QWidget):
    """实时日志。"""

    def __init__(self, api: ApiClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self._line_count = 0
        self._build()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("级别："))
        self.combo_level = QComboBox()
        self.combo_level.addItems(["全部", "DEBUG", "INFO", "WARNING", "ERROR"])
        toolbar.addWidget(self.combo_level)
        self.check_auto = QCheckBox("自动滚动")
        self.check_auto.setChecked(True)
        toolbar.addWidget(self.check_auto)
        btn_clear = QPushButton("清屏")
        toolbar.addWidget(btn_clear)
        btn_export = QPushButton("导出日志")
        toolbar.addWidget(btn_export)
        toolbar.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        toolbar.addWidget(self.status_label)
        root.addLayout(toolbar)

        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setObjectName("logView")
        self.view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        root.addWidget(self.view, 1)

        btn_clear.clicked.connect(self.clear)
        btn_export.clicked.connect(self.export)
        self.combo_level.currentIndexChanged.connect(self.load_backlog)

    # ------------------------------------------------------------------ #
    def load_backlog(self) -> None:
        """首次进入时把最近的日志补上（之后靠 WebSocket 增量推送）。"""
        level = self._current_level()
        try:
            entries = self.api.recent_logs(limit=300, level=level)
        except ApiError as exc:
            self.status_label.setText(exc.message)
            return
        self.clear()
        for entry in entries:
            self.append(entry)

    def _current_level(self) -> str | None:
        text = self.combo_level.currentText()
        return None if text == "全部" else text

    # ------------------------------------------------------------------ #
    def append(self, entry: dict[str, Any]) -> None:
        level = str(entry.get("level") or "INFO")
        wanted = self._current_level()
        if wanted and level != wanted:
            return

        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        ts_format = QTextCharFormat()
        ts_format.setForeground(QColor("#64748b"))
        cursor.insertText(f"{entry.get('ts', '')} ", ts_format)

        level_format = QTextCharFormat()
        level_format.setForeground(QColor(LEVEL_COLORS.get(level, "#cbd5e1")))
        cursor.insertText(f"{level:<7} ", level_format)

        body_format = QTextCharFormat()
        body_format.setForeground(QColor(LEVEL_COLORS.get(level, "#cbd5e1") if level in ("WARNING", "ERROR", "CRITICAL") else "#cbd5e1"))
        source = entry.get("logger") or ""
        cursor.insertText(f"[{source}] {entry.get('message', '')}\n", body_format)

        self._line_count += 1
        if self._line_count > MAX_LINES:
            self._trim()
        if self.check_auto.isChecked():
            self.view.moveCursor(QTextCursor.MoveOperation.End)
            self.view.ensureCursorVisible()
        self.status_label.setText(f"已接收 {self._line_count} 行")

    def _trim(self) -> None:
        """控制内存：超出上限时丢弃最旧的约 1/5。"""
        text = self.view.toPlainText()
        keep = text.splitlines()[-(MAX_LINES * 4 // 5):]
        self.view.setPlainText("\n".join(keep) + "\n")
        self._line_count = len(keep)

    def clear(self) -> None:
        self.view.clear()
        self._line_count = 0
        self.status_label.setText("已清屏")

    def export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", "ticket-helper-12306.log", "日志文件 (*.log *.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.view.toPlainText())
            self.status_label.setText(f"已导出到 {path}")
        except OSError as exc:
            self.status_label.setText(f"导出失败：{exc}")
