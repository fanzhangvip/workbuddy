"""登录验证码录入（VC-02）。

为什么必须有这个对话框：12306 登录必然要过验证码。识别链里的
``free`` / ``cloud`` / ``local`` 三个源在多数机器上都是不可用的
（免费接口会下线、本地模型要自己放），
真正兜住的是**人工录入**。Web 后台有弹窗，桌面端如果没人把待办验证码取走，
登录就会一直停在「登录中」，最后以超时失败 —— 使用者完全不知道发生了什么。

这里用**拉取 + 后台线程**的方式取待办验证码：

- 拉取（而不是等服务端推送）：WebSocket 断了也不该漏掉验证码；
- 后台线程取：HTTP 请求万一卡住，也不会把界面冻住。

识别成功的答案由 ``/api/captcha/submit`` 回填，登录线程就会继续跑完。
"""

from __future__ import annotations

import base64
import logging
import threading
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient, ApiError

log = logging.getLogger(__name__)

#: 拉取间隔（毫秒）。登录等验证码是全阻塞的，3 秒足够及时。
POLL_INTERVAL_MS = 3000


class CaptchaWatcher(QObject):
    """定期向后端要「待人工录入的验证码」，有新任务就发信号。"""

    task_received = Signal(dict)

    def __init__(self, api: ApiClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self._seen: set[str] = set()
        self._busy = False
        self._stopped = False
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._stopped = False
        self._timer.start()

    def stop(self) -> None:
        self._stopped = True
        self._timer.stop()

    # ------------------------------------------------------------------ #
    def _tick(self) -> None:
        # 上一次请求还没回来就跳过，避免网络慢时请求越积越多
        if self._busy or self._stopped:
            return
        self._busy = True
        threading.Thread(target=self._fetch, name="captcha-poll", daemon=True).start()

    def _fetch(self) -> None:
        tasks: list[dict[str, Any]] = []
        try:
            tasks = self.api.captcha_pending()
        except ApiError as exc:
            log.debug("获取待录入验证码失败: %s", exc.message)
        except Exception as exc:  # noqa: BLE001 - 轮询失败不应影响界面
            log.debug("获取待录入验证码异常: %s", exc)
        finally:
            self._busy = False

        if self._stopped:
            return

        # 已处理过的任务不再重复弹窗；同时把已经不在待办里的 id 清掉，
        # 否则这个集合会随着挂机时长无限增长。
        current = {str(t.get("id")) for t in tasks if t.get("id")}
        self._seen &= current
        for task in tasks:
            task_id = str(task.get("id") or "")
            if not task_id or task_id in self._seen:
                continue
            self._seen.add(task_id)
            self.task_received.emit(task)


class CaptchaDialog(QDialog):
    """录入一个验证码。"""

    submitted = Signal()

    def __init__(self, api: ApiClient, task: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self.task = task
        self.task_id = str(task.get("id") or "")
        self._done = False
        self.setWindowTitle("需要输入验证码")
        self.setModal(False)
        self.setMinimumWidth(340)
        self._build()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        hint = QLabel(self._hint_text())
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumHeight(80)
        self._render_image()
        root.addWidget(self.image_label)

        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("例如 112,45")
        self.input.returnPressed.connect(self.submit)
        btn_ok = QPushButton("提交")
        btn_ok.setObjectName("primaryButton")
        btn_ok.clicked.connect(self.submit)
        row.addWidget(self.input, 1)
        row.addWidget(btn_ok)
        root.addLayout(row)

        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        skip = QPushButton("本次跳过")
        skip.setObjectName("tinyButton")
        skip.clicked.connect(self._skip)
        root.addWidget(skip, alignment=Qt.AlignmentFlag.AlignRight)

        self.input.setFocus()

    def _hint_text(self) -> str:
        """按验证码类型给提示（回流 #59）。

        老版把提示写死成「登录 12306 需要验证码」，下单验证码（kind=order）弹出来
        也这么说 —— 账号明明在线却提示登录，使用者无从判断是哪个环节要验证码。
        """
        kind = str(self.task.get("kind") or "").lower()
        if kind in ("order", "passenger"):
            return (
                "下单 12306 需要图形验证码（不是登录）。请照下图输入图中内容；"
                "若图片显示不出来或不想录入，点「本次跳过」即可，任务会自动重试。"
            )
        return (
            "登录 12306 需要验证码。请照下图输入验证码，"
            "多个字之间用英文逗号分隔（例如 112,45）。"
        )

    def _render_image(self) -> None:
        raw = str(self.task.get("image") or "")
        if not raw:
            self.image_label.setText("（未取到验证码图片，请点击「本次跳过」后重试登录）")
            return
        try:
            data = base64.b64decode(raw)
        except Exception:  # noqa: BLE001
            self.image_label.setText("（验证码图片无法解析）")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data) or pixmap.isNull():
            self.image_label.setText("（验证码图片无法显示）")
            return
        self.image_label.setPixmap(pixmap)

    # ------------------------------------------------------------------ #
    def submit(self) -> None:
        answer = self.input.text().strip()
        if not answer:
            self.status.setText("请输入验证码")
            return
        try:
            self.api.submit_captcha(self.task_id, answer)
        except ApiError as exc:
            # 后台已经不认这个任务了（例如超时被回收），继续留在这里没有意义
            self.status.setText(f"提交失败：{exc.message}")
            self._finish()
            return
        self._done = True
        self.submitted.emit()
        self.accept()

    def _skip(self) -> None:
        """跳过本次等待：后台会在超时后给出结论，不用一直挂着弹窗。"""
        self._finish()

    def _finish(self) -> None:
        self._done = True
        self.close()
