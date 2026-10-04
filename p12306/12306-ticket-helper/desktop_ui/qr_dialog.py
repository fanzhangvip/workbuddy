"""扫码登录对话框（AC-08）。

交互设计要点：
- 二维码用 ``QTimer`` 每 2 秒轮询一次状态，不阻塞界面；
- 进入终态（成功/过期/失败）后**立刻停止定时器**，避免关掉弹窗后还在打接口；
- 关闭对话框时主动通知后台取消会话，让服务端也停止等待；
- 过期时提供「刷新二维码」，不用关掉重开。
"""

from __future__ import annotations

import base64
import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient, ApiError

log = logging.getLogger(__name__)

#: 轮询间隔（毫秒）。2 秒足够及时，又不会给服务端带来压力。
POLL_INTERVAL_MS = 2000

STATE_TEXT = {
    "waiting": "请使用 12306 App 扫描二维码",
    "scanned": "已扫描，请在手机上点击确认",
    "confirmed": "登录成功",
    "expired": "二维码已失效，请点击「刷新二维码」",
    "failed": "扫码登录失败",
    # 非终态：服务端偶发返回异常信封时只是「这一轮没拿到状态」，二维码通常仍然有效
    "unknown": "暂时无法获取扫码状态，正在重试…",
}


class QrLoginDialog(QDialog):
    """展示二维码并轮询扫码状态。"""

    succeeded = Signal(str)          # 携带登录成功的账号名

    def __init__(self, api: ApiClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self.uuid = ""
        #: 界面上当前显示的那张二维码的 uuid（后台自动换码时用来判断要不要重画）
        self.image_uuid = ""
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self.poll)
        self._build()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        self.setWindowTitle("扫码登录")
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)

        tip = QLabel("打开手机 <b>12306 App</b> →「我的」→ 右上角扫一扫，扫描下方二维码并确认登录。")
        tip.setWordWrap(True)
        tip.setObjectName("muted")
        layout.addWidget(tip)

        # 二维码显示区（固定方形，二维码是 1:1 的）
        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setFixedSize(240, 240)
        self.qr_label.setFrameShape(QFrame.Shape.StyledPanel)
        self.qr_label.setText("正在获取二维码…")
        holder = QHBoxLayout()
        holder.addStretch(1)
        holder.addWidget(self.qr_label)
        holder.addStretch(1)
        layout.addLayout(holder)

        self.status_label = QLabel("正在获取二维码…")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.remaining_label = QLabel("")
        self.remaining_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.remaining_label.setObjectName("muted")
        layout.addWidget(self.remaining_label)

        buttons = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新二维码")
        self.btn_refresh.setObjectName("primaryButton")
        self.btn_close = QPushButton("关闭")
        buttons.addWidget(self.btn_refresh)
        buttons.addWidget(self.btn_close)
        layout.addLayout(buttons)

        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_close.clicked.connect(self.close)

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """重新申请一张二维码。"""
        self._stop_polling()
        self._cancel_remote()
        self.qr_label.setPixmap(QPixmap())
        self.qr_label.setText("正在获取二维码…")
        self.status_label.setText("正在获取二维码…")
        self.remaining_label.setText("")
        self.btn_refresh.setEnabled(True)
        self.start()

    def start(self) -> None:
        try:
            data = self.api.start_qr_login()
        except ApiError as exc:
            self._set_status(f"获取二维码失败：{exc.message}", ok=False)
            return

        # uuid = 会话标识（轮询的键，不变）；qr_uuid = 当前这张二维码
        self.uuid = str(data.get("uuid") or "")
        self.image_uuid = str(data.get("qr_uuid") or data.get("uuid") or "")
        self._render_image(str(data.get("image") or ""))

        self._set_status(STATE_TEXT.get(str(data.get("state")), "") or "请扫描二维码")
        self._poll_timer.start()

    def _render_image(self, image_b64: str) -> None:
        """把裸 base64 的二维码画到界面上。"""
        if not image_b64:
            self.qr_label.setText("服务端未返回二维码图片")
            return
        pixmap = QPixmap()
        try:
            # 服务端返回的是裸 base64，Qt 需要显式的 PNG 格式提示
            pixmap.loadFromData(base64.b64decode(image_b64), "PNG")
        except Exception as exc:  # noqa: BLE001
            log.warning("二维码解码失败: %s", exc)
        if pixmap.isNull():
            self.qr_label.setText("二维码解析失败")
            return
        self.qr_label.setPixmap(
            pixmap.scaled(
                self.qr_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    # ------------------------------------------------------------------ #
    def poll(self) -> None:
        if not self.uuid:
            return
        try:
            # seen 传当前显示的那张码：后台自动换码后才会把新图带回来
            data = self.api.qr_login_status(self.uuid, seen=self.image_uuid)
        except ApiError as exc:
            # 会话不存在 / 后台重启过：继续轮询只会一直报错，直接停下并提示刷新
            self._stop_polling()
            self._set_status(f"状态查询失败：{exc.message}", ok=False)
            return

        # 官方二维码只有约 20 秒寿命，后台会自动换码，这里跟着换图，
        # 否则界面上会一直挂着一张已经失效的码，用户怎么扫都没反应
        new_image = str(data.get("image") or "")
        new_qr_uuid = str(data.get("qr_uuid") or data.get("uuid") or "")
        if new_image and new_qr_uuid and new_qr_uuid != self.image_uuid:
            self.image_uuid = new_qr_uuid
            self._render_image(new_image)
            if int(data.get("refresh_count") or 0) > 1:
                self._set_status("二维码已自动刷新，请扫描当前这张")

        state = str(data.get("state") or "")
        remaining = int(data.get("remaining") or 0)
        if remaining > 0:
            self.remaining_label.setText(f"有效期剩余 {remaining} 秒")

        if state == "confirmed":
            username = str(data.get("username") or "")
            self._stop_polling()
            self._set_status(f"登录成功：{username}", ok=True)
            self.remaining_label.setText("")
            # 这里不直接 accept()：先把结果发给调用方去刷新列表，再自动关闭
            self.succeeded.emit(username)
            QTimer.singleShot(900, self.accept)
            return

        if state in ("expired", "failed"):
            self._stop_polling()
            self._set_status(str(data.get("message") or STATE_TEXT.get(state, "")), ok=False)
            self.btn_refresh.setEnabled(True)
            return

        # unknown 属于「重试中」，用中性色展示：标红会让人以为已经失败了
        if state == "unknown":
            self._set_status(str(data.get("message") or STATE_TEXT["unknown"]))
        else:
            self._set_status(STATE_TEXT.get(state, str(data.get("message") or "")))
        # 已扫码时给个视觉提示，让用户知道「就差最后一步了」
        self.btn_refresh.setEnabled(state != "scanned")

    # ------------------------------------------------------------------ #
    def _set_status(self, text: str, *, ok: bool | None = None) -> None:
        color = "#15803d" if ok is True else ("#b91c1c" if ok is False else "#1f2937")
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color};")

    def _stop_polling(self) -> None:
        if self._poll_timer.isActive():
            self._poll_timer.stop()

    def _cancel_remote(self) -> None:
        if not self.uuid:
            return
        try:
            self.api.cancel_qr_login(self.uuid)
        except ApiError as exc:
            log.debug("取消扫码会话失败（不影响本地关闭）: %s", exc.message)
        self.uuid = ""

    # ------------------------------------------------------------------ #
    def closeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt 命名约定
        self._stop_polling()
        self._cancel_remote()
        super().closeEvent(event)

    def reject(self) -> None:
        self._stop_polling()
        self._cancel_remote()
        super().reject()
