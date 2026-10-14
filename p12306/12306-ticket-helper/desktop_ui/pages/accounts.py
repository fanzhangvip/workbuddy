"""账号管理页（UI-02）：增删改查、状态显示、登录测试。"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient, ApiError
from desktop_ui.theme import C
from ticket_helper.account import verify

log = logging.getLogger(__name__)

STATUS_TEXT = {
    "online": "在线",
    "offline": "离线",
    "risk": "风控",
    "logining": "登录中",
    "error": "异常",
    "disabled": "已禁用",
}
STATUS_COLOR = {
    "online": C["ok"],
    "risk": C["bad"],
    "error": C["warn"],
    "disabled": C["muted"],
    "offline": C["muted"],
}


def _verified_text(user: dict[str, Any]) -> str:
    """人证核验状态，四态如实展示。

    - **已核验**：拿到明确字段且通过；
    - **未核验**：拿到明确字段且未通过 —— 这才是真的会被候补拦住的状态；
    - **无法判定**：接口没返回核验字段。以前这种情况用「姓名 ≠ 账号名」猜，
      结果扫码登录（账号名本身就是真实姓名）的已实名账号被误判成「未核验」，
      会让人以为账号被 12306 限制了；
    - **未检测**：还没登录成功，压根没检测过。

    判定逻辑统一放在 ``ticket_helper.account.verify`` ——
    两端（含网页端）与候补引擎必须用同一份，否则会出现
    「界面说无法判定、候补却按未核验拦掉」这种自相矛盾。
    """
    return verify.label_of(user)


def _verify_dialog_text() -> str:
    """「怎么核验」的说明正文（步骤由 verify 模块统一给出）。"""
    lines = verify.guide_lines()
    return (
        "候补购票要求账号先完成**人证一致性核验**（官方规定）。\n\n"
        + "\n".join(lines)
        + "\n\n说明：核验必须本人在手机上做（要人脸识别），"
        "程序不能代做；已通过核验的账号与已激活的「铁路畅行」会员自动满足该条件。"
    )


class AccountsPage(QWidget):
    """账号管理。"""

    def __init__(self, api: ApiClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self._rows: list[dict[str, Any]] = []
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)

        tip = QLabel(
            "密码使用本机密钥加密后存入本地数据库，界面不回显、日志不记录；"
            "后台连接信息可在「系统设置」中查看。"
        )
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        root.addWidget(tip)

        form = QHBoxLayout()
        self.input_name = QLineEdit()
        self.input_name.setPlaceholderText("12306 用户名 / 手机号")
        self.input_pass = QLineEdit()
        self.input_pass.setPlaceholderText("密码（加密存储）")
        self.input_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.input_heartbeat = QSpinBox()
        self.input_heartbeat.setRange(30, 3600)
        self.input_heartbeat.setValue(120)
        self.input_heartbeat.setSuffix(" 秒心跳")
        btn_save = QPushButton("添加 / 更新账号")
        btn_save.setObjectName("primaryButton")
        self.btn_qr = QPushButton("扫码登录")
        self.btn_qr.setToolTip("用 12306 App 扫码登录，无需输入密码")
        # 候补被拦的直接原因往往就是这一项，所以把入口放在这一页显眼的位置，
        # 而不是藏在表格某一格的 tooltip 里（功能存在但不被发现，等于不存在）。
        self.btn_verify_help = QPushButton("人证核验怎么做？")
        self.btn_verify_help.setToolTip("候补购票要求账号先完成人证核验，这里说清楚在 12306 App 里怎么点")
        form.addWidget(self.input_name, 3)
        form.addWidget(self.input_pass, 3)
        form.addWidget(self.input_heartbeat, 1)
        form.addWidget(btn_save)
        form.addWidget(self.btn_qr)
        form.addWidget(self.btn_verify_help)
        root.addLayout(form)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["ID", "账号", "状态", "登录方式", "人证核验", "心跳", "最后活跃", "操作"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(self.table, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        root.addWidget(self.status_label)

        btn_save.clicked.connect(self._save)
        self.btn_qr.clicked.connect(self._open_qr_login)
        self.btn_verify_help.clicked.connect(self._show_verify_guide)

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        try:
            self._rows = self.api.list_users()
        except ApiError as exc:
            self.status_label.setText(exc.message)
            return
        self._render()

    def _render(self) -> None:
        self.table.setRowCount(len(self._rows))
        for row, user in enumerate(self._rows):
            status = user.get("status", "offline")
            has_password = bool(user.get("has_password", True))
            values = [
                str(user.get("id", "")),
                user.get("username", ""),
                STATUS_TEXT.get(status, status),
                "密码" if has_password else "扫码",
                _verified_text(user),
                f"{user.get('heartbeat', 120)}s",
                user.get("last_active") or "-",
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
                if col == 2:
                    color = STATUS_COLOR.get(status)
                    if color:
                        from PySide6.QtGui import QBrush, QColor

                        item.setForeground(QBrush(QColor(color)))
                if col == 4:
                    # 状态本身只是一个词，「下一步做什么」才是使用者要的
                    item.setToolTip(verify.hint_of(user))
                self.table.setItem(row, col, item)

            self.table.setCellWidget(row, 7, self._action_widget(user))
        self.status_label.setText(self._summary_text())

    def _summary_text(self) -> str:
        """底部概要：有账号未核验时**主动说出来**并给出出路。"""
        total = len(self._rows)
        blocked = [u for u in self._rows if verify.should_block(u)]
        if blocked:
            names = "、".join(str(u.get("username", "")) for u in blocked[:3])
            more = "等" if len(blocked) > 3 else ""
            return (
                f"共 {total} 个账号；{names}{more} 未完成人证核验，"
                f"12306 不允许其提交候补 —— 点「人证核验怎么做？」看步骤"
            )
        return f"共 {total} 个账号"

    def _show_verify_guide(self) -> None:
        """把「怎么核验」讲清楚 —— 步骤里要写到「点哪个位置」。"""
        box = QMessageBox(self)
        box.setWindowTitle("人证核验怎么做")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(_verify_dialog_text())
        copy_btn = box.addButton("复制官方链接", QMessageBox.ButtonRole.ActionRole)
        box.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is copy_btn:
            from PySide6.QtWidgets import QApplication

            QApplication.clipboard().setText(verify.GUIDE_URL)
            self.status_label.setText(f"官方说明链接已复制：{verify.GUIDE_URL}")

    def _action_widget(self, user: dict[str, Any]) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        has_password = bool(user.get("has_password", True))
        btn_login = QPushButton("测试登录")
        btn_login.setObjectName("tinyButton")
        if has_password:
            btn_login.clicked.connect(lambda _=False, uid=int(user["id"]): self._login(uid))
        else:
            # 扫码账号没有保存密码，点「测试登录」必然失败，直接禁用并说明原因，
            # 比让人点了之后收到一句含糊的失败更有用。
            btn_login.setEnabled(False)
            btn_login.setToolTip("扫码登录账号没有保存密码，请重新扫码登录")

        btn_detail = QPushButton("详情")
        btn_detail.setObjectName("tinyButton")
        btn_detail.setToolTip("查看账号信息、乘车人与我的订单（实时拉取 12306）")
        btn_detail.clicked.connect(lambda _=False, u=dict(user): self._open_detail(u))

        # 在 12306 App 里做完人证核验之后，得有个地方能刷新状态 ——
        # 「测试登录」对扫码账号是禁用的，光靠它这类账号永远刷新不了。
        btn_verify = QPushButton("重测核验")
        btn_verify.setObjectName("tinyButton")
        btn_verify.setToolTip(
            "重新检测人证核验状态。刚在 12306 App 里做完核验的话点这里刷新（需账号在线）"
        )
        if not user.get("online"):
            btn_verify.setEnabled(False)
            btn_verify.setToolTip("账号当前不在线，先扫码登录再检测")
        else:
            btn_verify.clicked.connect(
                lambda _=False, uid=int(user["id"]), name=user.get("username", ""): self._refresh_verify(uid, name)
            )

        enabled = bool(user.get("enabled", True))
        btn_toggle = QPushButton("禁用" if enabled else "启用")
        btn_toggle.setObjectName("tinyButton")
        btn_toggle.clicked.connect(lambda _=False, uid=int(user["id"]), e=enabled: self._toggle(uid, not e))

        btn_qr = QPushButton("扫码登录")
        btn_qr.setObjectName("tinyButton")
        btn_qr.setToolTip("为该账号刷新登录态（扫码）")
        btn_qr.clicked.connect(lambda _=False, name=user.get("username", ""): self._open_qr_login(name))

        btn_delete = QPushButton("删除")
        btn_delete.setObjectName("tinyButton")
        btn_delete.clicked.connect(lambda _=False, uid=int(user["id"]): self._delete(uid))

        layout.addWidget(btn_detail)
        layout.addWidget(btn_login)
        layout.addWidget(btn_verify)
        layout.addWidget(btn_qr)
        layout.addWidget(btn_toggle)
        layout.addWidget(btn_delete)
        layout.addStretch(1)
        return widget

    def _refresh_verify(self, user_id: int, username: str) -> None:
        """重测人证核验状态，并把结果如实说出来（四态，不猜）。"""
        try:
            result = self.api.refresh_user_verify(user_id)
        except ApiError as exc:
            self.status_label.setText(f"检测核验状态失败：{exc.message}")
            return
        label = result.get("verify_label") or "未知"
        self.status_label.setText(f"{username} 人证核验：{label}。{result.get('verify_hint', '')}")
        self.refresh()

    # ------------------------------------------------------------------ #
    # 账号详情（个人中心：账号信息 / 乘车人 / 我的订单）
    # ------------------------------------------------------------------ #
    def _open_detail(self, user: dict[str, Any]) -> None:
        """跳到「个人中心」页面查看该账号。

        **无论账号是否在线都要跳过去**。
        曾经这里判了「未登录就弹个提示、什么都不做」，结果是：
        扫码账号重启后本来就是未登录（这是常态），用户点「详情」什么都没看到，
        只会以为功能没做出来。现在由目标页面用横幅说明原因并就地提供扫码入口。

        主窗口不在时（例如单独构造本页做测试）退化为弹窗，保证两种场景都可用。
        """
        window = self.window()
        if callable(getattr(window, "show_profile", None)):
            window.show_profile(user)  # type: ignore[attr-defined]
            return

        from desktop_ui.account_detail import AccountDetailDialog

        dialog = AccountDetailDialog(self.api, user, self)
        dialog.exec()
        # 用户可能刚在弹窗里扫了码，回到列表顺手刷一下状态
        self.refresh()

    # ------------------------------------------------------------------ #
    # 扫码登录（AC-08）
    # ------------------------------------------------------------------ #
    def _open_qr_login(self, username_hint: str = "") -> None:
        from desktop_ui.qr_dialog import QrLoginDialog

        # Qt 的 clicked 信号会把 checked(bool) 当第一个参数传进来；
        # 这里的参数本意是「账号名提示」，所以要把非字符串一律归一成空串，
        # 否则 username_hint 会变成 False，后面 .strip() 直接崩。
        if not isinstance(username_hint, str):
            username_hint = ""

        dialog = QrLoginDialog(self.api, self)
        dialog.succeeded.connect(self._on_qr_success)
        dialog.start()
        dialog.exec()

    def _on_qr_success(self, username: str) -> None:
        self.status_label.setText(f"账号 {username} 扫码登录成功")
        self.refresh()

    # ------------------------------------------------------------------ #
    def _save(self) -> None:
        username = self.input_name.text().strip()
        if not username:
            self.status_label.setText("请填写账号")
            return
        try:
            result = self.api.create_user(username, self.input_pass.text(), self.input_heartbeat.value())
            self.input_pass.clear()
            # 后台会在保存后自动登录，这里把它的说法原样告诉使用者
            self.status_label.setText(f"{result.get('message') or f'账号 {username} 已保存'}")
            self.refresh()
            if result.get("login_queued"):
                # 登录是异步的，等一下再刷，状态才来得及变成「在线」
                QTimer.singleShot(1500, self.refresh)
                QTimer.singleShot(5000, self.refresh)
        except ApiError as exc:
            self.status_label.setText(exc.message)

    def _login(self, user_id: int) -> None:
        try:
            result = self.api.login_user(user_id)
            self.status_label.setText(str(result.get("message") or "已发起登录，稍后自动刷新查看状态"))
            QTimer.singleShot(1500, self.refresh)
            QTimer.singleShot(5000, self.refresh)
        except ApiError as exc:
            self.status_label.setText(exc.message)

    def _toggle(self, user_id: int, enabled: bool) -> None:
        try:
            self.api.toggle_user(user_id, enabled)
            self.refresh()
        except ApiError as exc:
            self.status_label.setText(exc.message)

    def _delete(self, user_id: int) -> None:
        confirm = QMessageBox.question(
            self, "确认删除", "确定要删除该账号吗？删除后需要重新配置。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.api.delete_user(user_id)
            self.refresh()
        except ApiError as exc:
            self.status_label.setText(exc.message)
