"""个人中心页面（左侧导航入口）：账号信息 / 乘车人 / 我的订单 / 待支付。

为什么要有这个「页面」而不只是账号行上的「详情」按钮
--------------------------------------------------
使用者反馈了两次「怎么还是没有看到个人信息/乘车人/我的订单」。
功能其实做了，但入口是账号表格最后一列里的一个小按钮 ——
在左侧导航扫一眼根本发现不了它。**功能存在但不被发现，等于不存在。**

所以这里把它提升为一级导航页面，并在页面顶部提供账号下拉选择；
账号行上的「详情」按钮依然保留，点击后直接切到这个页面并选中该账号。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient, ApiError
from desktop_ui.profile_panel import ProfilePanel

log = logging.getLogger(__name__)

STATUS_TEXT = {
    "online": "在线",
    "offline": "离线",
    "risk": "风控",
    "logining": "登录中",
    "error": "异常",
    "disabled": "已禁用",
}


class ProfilePage(QWidget):
    """个人中心。"""

    #: 请求外层刷新账号列表（扫码登录成功后）
    accounts_dirty = Signal()

    def __init__(self, api: ApiClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self._users: list[dict[str, Any]] = []
        self._build()
        self._wire()
        self.refresh_accounts()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("选择账号"))
        self.account_box = QComboBox()
        self.account_box.setMinimumWidth(240)
        self.account_box.setToolTip("这里列出所有已添加的 12306 账号；未登录的账号扫码一次即可查看")
        bar.addWidget(self.account_box)
        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        bar.addWidget(self.status_label, 1)
        self.btn_reload_accounts = QPushButton("刷新账号列表")
        bar.addWidget(self.btn_reload_accounts)
        root.addLayout(bar)

        self.panel = ProfilePanel(self.api, embedded=True, parent=self)
        root.addWidget(self.panel, 1)

        # 页面被打开时如果账号列表还是空的（例如后台刚起），稍后自动再取一次
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self.refresh_accounts)

    def _wire(self) -> None:
        self.btn_reload_accounts.clicked.connect(self.refresh_accounts)
        self.account_box.currentIndexChanged.connect(self._on_account_picked)
        self.panel.relogin_requested.connect(self.relogin_active_account)
        self.panel.relogin_succeeded.connect(lambda _name: self.accounts_dirty.emit())

    # ------------------------------------------------------------------ #
    # 账号列表
    # ------------------------------------------------------------------ #
    def refresh_accounts(self) -> None:
        try:
            self._users = self.api.list_users()
        except ApiError as exc:
            self.status_label.setText(f"读取账号列表失败：{exc.message}")
            return

        current = self.current_username()
        self.account_box.blockSignals(True)
        self.account_box.clear()
        for user in self._users:
            mark = "在线" if user.get("online") else STATUS_TEXT.get(str(user.get("status")), "离线")
            label = f"{user.get('username', '')}（{mark}）"
            if not user.get("session_saved"):
                label += " · 需扫码"
            self.account_box.addItem(label, user)
        self.account_box.blockSignals(False)

        if not self._users:
            self.status_label.setText("还没有账号。请先到「账号管理」添加，或点「扫码登录」。")
            self.panel.set_user({}, autoload=False)
            self.panel._set_all_status("请先添加账号")  # noqa: SLF001 - 同包内的展示辅助
            # 后台可能还在启动（账号列表稍后才可用），自动重试一次
            self._retry_timer.start(2500)
            return

        index = 0
        if current:
            for i, user in enumerate(self._users):
                if str(user.get("username")) == current:
                    index = i
                    break
        else:
            # 首次进入：优先选一个**在线**账号。
            # 默认停在第一个（可能是离线）账号上，等于一进来就给一屏「未登录」，
            # 使用者会以为整个功能不能用。
            for i, user in enumerate(self._users):
                if user.get("online"):
                    index = i
                    break
        self.account_box.setCurrentIndex(index)
        self.status_label.setText(f"共 {len(self._users)} 个账号")
        self._on_account_picked(index)

    def current_username(self) -> str:
        data = self.account_box.currentData()
        return str((data or {}).get("username") or "")

    def _on_account_picked(self, index: int) -> None:
        if index < 0:
            return
        user = self.account_box.itemData(index)
        if isinstance(user, dict):
            self.panel.set_user(user)

    # ------------------------------------------------------------------ #
    def show_account(self, user: dict[str, Any]) -> None:
        """外部（账号列表的「详情」按钮）指定要查看的账号。"""
        target = str(user.get("username") or "")
        self.refresh_accounts()
        for i, item in enumerate(self._users):
            if str(item.get("username")) == target:
                self.account_box.setCurrentIndex(i)
                return
        # 列表里没有（账号刚被删/刚改名）：直接用传进来的快照展示
        self.panel.set_user(user)

    # ------------------------------------------------------------------ #
    def relogin_active_account(self) -> None:
        """就地扫码登录当前选中的账号。

        扫码成功后要**重新拉一次账号列表**：扫码可能会新建账号
        （账号名在扫之前是不知道的），列表里的项必须跟着更新。
        """
        from desktop_ui.qr_dialog import QrLoginDialog

        dialog = QrLoginDialog(self.api, self)
        dialog.succeeded.connect(self._on_relogin_ok)
        dialog.start()
        dialog.exec()

    def _on_relogin_ok(self, username: str) -> None:
        self.panel.on_relogin_ok(username)
        self.refresh_accounts()
        self.accounts_dirty.emit()
