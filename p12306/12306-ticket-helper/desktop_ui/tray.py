"""系统托盘（UI-07）：最小化到托盘挂机、托盘菜单、出票气泡提醒。

行为约定（这是「挂机工具」的核心体验）：
- 点窗口关闭按钮 → **最小化到托盘**，后台继续抢票，而不是退出；
- 只有从托盘菜单选「退出」才真正结束（并询问是否连带关闭后台服务）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ticket_helper import APP_COPYRIGHT, APP_DISPLAY_NAME

log = logging.getLogger(__name__)


def build_icon() -> QIcon:
    """生成一个简易图标，避免依赖外部资源文件（打包更省事）。"""
    pixmap = QPixmap(64, 64)
    pixmap.fill()
    from PySide6.QtGui import QColor, QFont, QPainter

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#c0392b"))
    painter.setPen(QColor("#c0392b"))
    painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
    painter.setPen(QColor("#ffffff"))
    font = QFont("Microsoft YaHei", 22, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), 0x84, "票")  # AlignCenter
    painter.end()
    return QIcon(pixmap)


class TrayIcon(QObject):
    """托盘图标与菜单。"""

    show_window = Signal()
    start_all = Signal()
    stop_all = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray: QSystemTrayIcon | None = None
        if self.available:
            self._build()

    def _build(self) -> None:
        self.tray = QSystemTrayIcon(build_icon())
        self.tray.setToolTip(f"{APP_DISPLAY_NAME} · 挂机中\n{APP_COPYRIGHT}")

        menu = QMenu()
        action_show = QAction("显示主界面", menu)
        action_show.triggered.connect(self.show_window.emit)
        menu.addAction(action_show)
        menu.addSeparator()

        action_start = QAction("启动全部任务", menu)
        action_start.triggered.connect(self.start_all.emit)
        menu.addAction(action_start)

        action_stop = QAction("停止全部任务", menu)
        action_stop.triggered.connect(self.stop_all.emit)
        menu.addAction(action_stop)
        menu.addSeparator()

        action_quit = QAction("退出", menu)
        action_quit.triggered.connect(self.quit_requested.emit)
        menu.addAction(action_quit)

        self.tray.setContextMenu(menu)

        def _on_activated(reason) -> None:
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                self.show_window.emit()

        self.tray.activated.connect(_on_activated)
        self.tray.show()

    # ------------------------------------------------------------------ #
    def show_notification(self, title: str, body: str, *, critical: bool = False) -> None:
        """出票/候补提醒：托盘气泡 + 系统通知（UI-07 / OD-02）。"""
        if self.tray is None:
            return
        icon = QSystemTrayIcon.MessageIcon.Critical if critical else QSystemTrayIcon.MessageIcon.Information
        # 气泡正文过长会被截断，保留最关键的前几行
        text = "\n".join(body.splitlines()[:5])
        try:
            self.tray.showMessage(title, text, icon, 12000 if critical else 6000)
        except Exception as exc:  # noqa: BLE001
            log.debug("托盘气泡显示失败: %s", exc)

    def set_tooltip(self, text: str) -> None:
        if self.tray is not None:
            self.tray.setToolTip(text)

    def hide(self) -> None:
        if self.tray is not None:
            self.tray.hide()
