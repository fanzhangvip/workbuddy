"""账号详情弹窗：给账号列表的「详情」按钮用。

真正的界面在 :class:`~desktop_ui.profile_panel.ProfilePanel` 里，
这里只做一层窗口壳（标题 + 关闭），以保证**页面入口与弹窗入口共用同一份实现**。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtWidgets import QDialog, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from desktop_ui.api_client import ApiClient
from desktop_ui.profile_panel import ProfilePanel

log = logging.getLogger(__name__)

__all__ = ["AccountDetailDialog", "ProfilePanel"]


class AccountDetailDialog(QDialog):
    """展示某个账号的个人中心内容。"""

    def __init__(self, api: ApiClient, user: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self.setWindowTitle(f"账号详情 · {user.get('username', '')}")
        self.resize(980, 660)

        root = QVBoxLayout(self)
        self.panel = ProfilePanel(api, embedded=False, parent=self)
        root.addWidget(self.panel, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.close)
        actions.addWidget(self.btn_close)
        root.addLayout(actions)

        self.panel.set_user(user)
