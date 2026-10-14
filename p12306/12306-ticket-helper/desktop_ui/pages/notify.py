"""通知设置页（UI-06）：渠道开关与配置、通知记录。

排布原则：**一个渠道一张卡片**。

以前这块是三个大 ``QGroupBox`` 里塞 19 行 ``QFormLayout``：
「启用 Server酱」「钉钉 Webhook」「企业微信 Webhook」全挤在同一列里，
每个渠道的开关和它的参数混在一起，看起来是一堵墙 ——
既难扫「我到底开了哪几个」，也难看出「开了但还没填参数」。

现在：
- 卡片第一行就是「启用」开关 + 该渠道的「测试」按钮 + 测试结果；
- **未启用时参数区整体置灰**，一眼看出这些现在不用填；
- 参数按两列网格排，短字段（端口、SSL）与长字段（Webhook）各得其所；
- 整页可滚动，而「保存 / 测试全部」固定在底部，不跟着内容滚走；
- 每个渠道可以**用界面当前填的值直接测**，不必先保存一份错的配置。

顺手修掉的坑：原来 ``refresh_history`` 每 8 秒会连带把配置回填一遍，
于是**正在输入的内容会被静默覆盖回已保存的值**。现在定时器只刷记录表。
"""

from __future__ import annotations

import logging
import threading
from functools import partial
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient, ApiError
from desktop_ui.theme import C
from desktop_ui.layout import CARD_GAP, ROW_GAP
from ticket_helper.config.settings import SecretMasked

log = logging.getLogger(__name__)


#: 接口读回来时，这些字段已被脱敏成 `********`（见 `AppConfig.to_dict(mask_secrets=True)`）。
#: 要把它们和普通字段区别对待，否则会出数据丢失事故。
MASKED_HINT = "已保存（留空表示不修改）"


def visible_secret(value: Any) -> str:
    """把接口读回来的值转成「可以放进输入框」的文本。

    脱敏占位符**绝不能**塞进输入框：一来用户会以为那就是当前值，
    二来他只要改了别的字段再保存，这个占位符就会被当成真值写回配置 ——
    通知从此静默失效，而且界面上看不出任何异常。
    占位符显示成空串，语义上就是「不修改」。
    """
    text = str(value or "")
    return "" if text == SecretMasked else text


def secret_to_send(text: str) -> str:
    """把输入框里的内容转成「要发给后端的值」；空串或占位符表示**不修改**。"""
    value = str(text or "").strip()
    if not value or value == SecretMasked:
        return ""
    return value


class ChannelCard(QGroupBox):
    """单个通知渠道：启用开关 + 参数 + 就地测试。

    没有用 ``QGroupBox(checkable=True)`` 那种「标题上带勾选框」的写法：
    它会把整组控件一起禁用，连「测试」按钮也点不了 ——
    而「还没启用，想先测一下通不通」恰恰是常见顺序。
    """

    #: 请求测试该渠道。不带参数 —— 配置由页面按渠道自己取，
    #: 免得信号参数一多就写错槽函数签名（曾经 `Signal(str, dict)` 配
    #: `lambda _c, n=name` 时，dict 被塞进默认参数位，传下去的是个字典）。
    test_requested = Signal()

    def __init__(self, title: str, *, channel: str, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.channel = channel

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(ROW_GAP)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.check_enable = QCheckBox("启用")
        self.check_enable.setToolTip("勾上并点「保存」后，这类通知才会真正发出来")
        head.addWidget(self.check_enable)

        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("muted")
            hint_label.setWordWrap(True)
            head.addWidget(hint_label, 1)
        else:
            head.addStretch(1)

        self.result_label = QLabel("")
        self.result_label.setObjectName("muted")
        self.result_label.setToolTip("测试结果只显示在这里；失败详情见「通知记录」选项卡")
        head.addWidget(self.result_label)

        self.btn_test = QPushButton("测试")
        self.btn_test.setObjectName("tinyButton")
        self.btn_test.setToolTip("用上面填的当前值发一条测试通知（不需要先保存）")
        self.btn_test.clicked.connect(self.test_requested.emit)
        head.addWidget(self.btn_test)
        outer.addLayout(head)

        # 参数区：未启用时整块置灰
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(ROW_GAP)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)
        outer.addWidget(self.body)

        self.check_enable.toggled.connect(self._on_enable_toggled)
        self._on_enable_toggled(False)

    # ------------------------------------------------------------------ #
    def add_field(self, label: str, widget: QWidget, *, row: int, col: int = 0, span: int = 1) -> QWidget:
        """一格里放「小标题 + 控件」的竖排单元。

        标签写在控件**上方**而不是左侧：左侧标签会让所有输入框的起点
        取决于最长的那个标签，长短字段一排就会参差不齐。
        """
        cell = QWidget()
        cell_layout = QVBoxLayout(cell)
        cell_layout.setContentsMargins(0, 0, 0, 0)
        cell_layout.setSpacing(4)
        text = QLabel(label)
        text.setObjectName("muted")
        cell_layout.addWidget(text)
        cell_layout.addWidget(widget)
        self.grid.addWidget(cell, row, col, 1, span)
        return widget

    def _on_enable_toggled(self, on: bool) -> None:
        self.body.setEnabled(bool(on))
        if not on:
            self.result_label.setText("")

    # ------------------------------------------------------------------ #
    def is_enabled(self) -> bool:
        return self.check_enable.isChecked()

    def set_enabled(self, on: bool) -> None:
        self.check_enable.setChecked(bool(on))

    def set_result(self, ok: bool | None, text: str = "") -> None:
        if ok is None:
            self.result_label.setText(text)
            self.result_label.setStyleSheet(f"color: {C['muted']};")
        elif ok:
            self.result_label.setText(text or "测试成功")
            self.result_label.setStyleSheet(f"color: {C['ok']};")
        else:
            self.result_label.setText(text or "测试失败")
            self.result_label.setStyleSheet(f"color: {C['bad']};")


class NotifyPage(QWidget):
    """通知设置与系统信息。"""

    #: 后台测试完一条渠道 → 回到 UI 线程更新结果
    channel_tested = Signal(str, bool)

    def __init__(self, api: ApiClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self.cards: dict[str, ChannelCard] = {}
        self._testing: set[str] = set()
        self._build()
        self.channel_tested.connect(self._on_channel_tested)
        self.timer = QTimer(self)
        self.timer.setInterval(8000)
        # 定时器**只刷记录表**：以前它顺带把配置回填一遍，
        # 结果正在输入的内容每 8 秒被静默覆盖回已保存的值。
        self.timer.timeout.connect(self.refresh_history)
        self.timer.start()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()

        tabs.addTab(self._build_channels_tab(), "通知渠道")
        tabs.addTab(self._build_history_tab(), "通知记录")

        root.addWidget(tabs)

        self._connect_card_tests()
        self._load_channels()
        self.refresh_history()

    # ------------------------------------------------------------------ #
    # 通知渠道
    # ------------------------------------------------------------------ #
    def _build_channels_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        body = QVBoxLayout(host)
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(CARD_GAP)

        lead = QLabel(
            "抢到票时默认会弹窗 + 响提示音（网页端与桌面端共用，无需配置）。"
            "需要离开电脑也能收到通知时，再往下选一个渠道填好凭据、勾上「启用」，"
            "最后点底部的「保存并热生效」——不用重启。"
        )
        lead.setObjectName("muted")
        lead.setWordWrap(True)
        body.addWidget(lead)

        self.check_web = QCheckBox("出票时弹窗 + 提示音（本机提醒，推荐保持开启）")
        self.check_web.setChecked(True)
        body.addWidget(self.check_web)

        self.cards["email"] = self._build_email_card()
        self.cards["serverchan"] = self._build_serverchan_card()
        self.cards["dingtalk"] = self._build_webhook_card(
            "dingtalk", "钉钉机器人", "钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义，复制 Webhook",
            "https://oapi.dingtalk.com/robot/send?access_token=...",
        )
        self.cards["wecom"] = self._build_webhook_card(
            "wecom", "企业微信群机器人", "企业微信群 → 群设置 → 群机器人 → 添加，复制 Webhook 地址",
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...",
        )
        self.cards["telegram"] = self._build_telegram_card()
        self.cards["voice"] = self._build_voice_card()

        for name in ("email", "serverchan", "dingtalk", "wecom", "telegram", "voice"):
            body.addWidget(self.cards[name])

        body.addStretch(1)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)

        # 操作条固定在底部：内容再长也不用手动滚到底去找「保存」
        actions = QWidget()
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(12, 8, 12, 12)
        actions_layout.setSpacing(10)
        self.btn_save = QPushButton("保存并热生效")
        self.btn_save.setObjectName("primaryButton")
        self.btn_test_all = QPushButton("测试全部渠道")
        self.btn_test_all.setToolTip("逐个测已启用的渠道，结果看「通知记录」")
        actions_layout.addWidget(self.btn_save)
        actions_layout.addWidget(self.btn_test_all)
        actions_layout.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        actions_layout.addWidget(self.status_label)
        layout.addWidget(actions)

        self.btn_save.clicked.connect(self._save_channels)
        self.btn_test_all.clicked.connect(self._test_all)
        return tab

    # ---- 各渠道卡片 ---- #
    def _build_email_card(self) -> ChannelCard:
        card = ChannelCard("邮件通知（SMTP）", channel="email", hint="离开电脑也能收到；注意多数邮箱要用「授权码」而不是登录密码")
        self.input_smtp_host = QLineEdit()
        self.input_smtp_host.setPlaceholderText("smtp.qq.com")
        self.spin_smtp_port = QSpinBox()
        self.spin_smtp_port.setRange(1, 65535)
        self.spin_smtp_port.setValue(465)
        self.check_smtp_ssl = QCheckBox("使用 SSL（465 端口一般是开）")
        self.check_smtp_ssl.setChecked(True)
        self.input_smtp_user = QLineEdit()
        self.input_smtp_user.setPlaceholderText("发件邮箱，如 you@qq.com")
        self.input_smtp_pass = QLineEdit()
        self.input_smtp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.input_smtp_pass.setPlaceholderText("留空表示不修改")
        self.input_smtp_receivers = QLineEdit()
        self.input_smtp_receivers.setPlaceholderText("收件邮箱，多个用逗号分隔：a@x.com,b@y.com")

        card.add_field("SMTP 主机", self.input_smtp_host, row=0, span=2)
        card.add_field("端口", self.spin_smtp_port, row=1, col=0)
        card.add_field("加密", self.check_smtp_ssl, row=1, col=1)
        card.add_field("发件账号", self.input_smtp_user, row=2, col=0)
        card.add_field("授权码 / 密码", self.input_smtp_pass, row=2, col=1)
        card.add_field("收件人", self.input_smtp_receivers, row=3, span=2)
        return card

    def _build_serverchan_card(self) -> ChannelCard:
        card = ChannelCard("Server酱 / PushBear", channel="serverchan", hint="推送到微信，配置最简单")
        self.input_serverchan_key = QLineEdit()
        self.input_serverchan_key.setPlaceholderText("SCT 开头的 SendKey")
        card.add_field("SendKey", self.input_serverchan_key, row=0, span=2)
        return card

    def _build_webhook_card(self, channel: str, title: str, hint: str, placeholder: str) -> ChannelCard:
        card = ChannelCard(title, channel=channel, hint=hint)
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        if channel == "dingtalk":
            self.input_dingtalk = field
        else:
            self.input_wecom = field
        card.add_field("Webhook 地址", field, row=0, span=2)
        return card

    def _build_telegram_card(self) -> ChannelCard:
        card = ChannelCard("Telegram", channel="telegram", hint="需要能访问 Telegram 的网络环境")
        self.input_tg_token = QLineEdit()
        self.input_tg_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.input_tg_token.setPlaceholderText("Bot Token（找 @BotFather 要）")
        self.input_tg_chat = QLineEdit()
        self.input_tg_chat.setPlaceholderText("Chat ID（找 @userinfobot 查）")
        card.add_field("Bot Token", self.input_tg_token, row=0, col=0)
        card.add_field("Chat ID", self.input_tg_chat, row=0, col=1)
        return card

    def _build_voice_card(self) -> ChannelCard:
        card = ChannelCard("电话语音", channel="voice", hint="只在出票成功这类重大事件才拨号，避免打扰")
        self.input_voice_appcode = QLineEdit()
        self.input_voice_appcode.setPlaceholderText("阿里云市场 AppCode")
        self.input_voice_phone = QLineEdit()
        self.input_voice_phone.setPlaceholderText("接收电话的手机号")
        card.add_field("AppCode", self.input_voice_appcode, row=0, col=0)
        card.add_field("手机号", self.input_voice_phone, row=0, col=1)
        return card

    # ------------------------------------------------------------------ #
    # 通知记录
    # ------------------------------------------------------------------ #
    def _build_history_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(ROW_GAP)
        hint = QLabel("每次通知的发送结果都会记在这里 —— 渠道「测试失败」时，失败原因在这一列里。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.history_table = QTableWidget(0, 5)
        self.history_table.setHorizontalHeaderLabels(["时间", "类型", "标题", "渠道", "结果"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.history_table, 1)
        return tab

    # ------------------------------------------------------------------ #
    # 渠道：读 / 写 / 测
    # ------------------------------------------------------------------ #
    @staticmethod
    def _csv(text: str) -> list[str]:
        return [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]

    def _channel_payload(self, name: str) -> dict[str, Any]:
        """按渠道取出**当前界面上的值**（用于保存，也用于「先用当前值测一测」）。

        **敏感字段留空时不放进 payload** —— 后端 `update_section` 是「有则覆盖」，
        传空串会把已存的凭据清掉，而传脱敏占位符会把它换成 `********`。
        两种都比「什么都不做」糟。
        """
        enabled = self.cards[name].is_enabled() if name in self.cards else self.check_web.isChecked()

        if name == "email":
            payload: dict[str, Any] = {
                "enabled": enabled,
                # 主机/端口/账号/收件人不是敏感字段，读回来就是明文，照常传
                "smtp_host": self.input_smtp_host.text().strip(),
                "smtp_port": self.spin_smtp_port.value(),
                "use_ssl": self.check_smtp_ssl.isChecked(),
                "username": self.input_smtp_user.text().strip(),
                "sender": self.input_smtp_user.text().strip(),
                "receivers": self._csv(self.input_smtp_receivers.text()),
            }
            password = secret_to_send(self.input_smtp_pass.text())
            if password:
                payload["password"] = password
            return payload

        if name == "serverchan":
            payload = {"enabled": enabled}
            key = secret_to_send(self.input_serverchan_key.text())
            if key:
                payload["key"] = key
            return payload

        if name == "dingtalk":
            payload = {"enabled": enabled}
            webhook = secret_to_send(self.input_dingtalk.text())
            if webhook:
                payload["webhook"] = webhook
            return payload

        if name == "wecom":
            payload = {"enabled": enabled}
            webhook = secret_to_send(self.input_wecom.text())
            if webhook:
                payload["webhook"] = webhook
            return payload

        if name == "telegram":
            payload = {"enabled": enabled}
            token = secret_to_send(self.input_tg_token.text())
            if token:
                payload["bot_token"] = token
            chat_id = secret_to_send(self.input_tg_chat.text())
            if chat_id:
                payload["chat_id"] = chat_id
            return payload

        if name == "voice":
            payload = {"enabled": enabled}
            appcode = secret_to_send(self.input_voice_appcode.text())
            if appcode:
                payload["appcode"] = appcode
            phone = secret_to_send(self.input_voice_phone.text())
            if phone:
                payload["phone"] = phone
            return payload

        if name == "web":
            return {"enabled": enabled}
        return {}

    @staticmethod
    def _fill_secret(widget: QLineEdit, stored: Any, empty_hint: str) -> None:
        """把已保存的敏感字段填进输入框（**不显示脱敏占位符**）。"""
        text = visible_secret(stored)
        widget.clear()
        if text:
            widget.setText(text)
            widget.setPlaceholderText(empty_hint)
        elif str(stored or "") == SecretMasked:
            widget.setPlaceholderText(MASKED_HINT)
        else:
            widget.setPlaceholderText(empty_hint)

    def _save_channels(self) -> None:
        payload: dict[str, Any] = {"web": self.check_web.isChecked()}
        for name in ("email", "serverchan", "dingtalk", "wecom", "telegram", "voice"):
            payload[name] = self._channel_payload(name)
        try:
            self.api.update_config("notify", payload)
            self.input_smtp_pass.clear()
            enabled = [
                label for name, label in (
                    ("email", "邮件"), ("serverchan", "Server酱"), ("dingtalk", "钉钉"),
                    ("wecom", "企业微信"), ("telegram", "Telegram"), ("voice", "电话语音"),
                ) if self.cards[name].is_enabled()
            ]
            self.status_label.setText(
                "已保存并热生效。当前启用：" + ("、".join(enabled) if enabled else "（只保留本机弹窗提醒）")
            )
            self.refresh_history()
        except ApiError as exc:
            self.status_label.setText(exc.message)

    def _test_channel(self, name: str, override: dict[str, Any] | None = None) -> None:
        """在后台线程里测一个渠道（网络请求不能占着 UI 线程）。"""
        if name in self._testing:
            return
        card = self.cards.get(name)
        if card is None:
            return
        self._testing.add(name)
        card.btn_test.setEnabled(False)
        card.set_result(None, "测试中…")

        config = override or self._channel_payload(name)
        # enabled=False 的渠道在服务端会被跳过，而「先测再启用」正是这里的用法，
        # 所以测试时把 enabled 强制打开，只测「凭据能不能用」。
        config["enabled"] = True

        def _worker() -> None:
            try:
                result = self.api.test_notify(name, config)
                ok = bool(result.get("ok"))
            except ApiError as exc:
                log.warning("测试通知渠道 %s 失败: %s", name, exc)
                ok = False
            self.channel_tested.emit(name, ok)

        threading.Thread(target=_worker, name=f"notify-test-{name}", daemon=True).start()

    def _on_channel_tested(self, name: str, ok: bool) -> None:
        self._testing.discard(name)
        card = self.cards.get(name)
        if card is None:
            return
        card.btn_test.setEnabled(True)
        card.set_result(ok, "测试成功" if ok else "测试失败（原因见「通知记录」）")
        self.refresh_history()

    def _test_all(self) -> None:
        enabled = [name for name in self.cards if self.cards[name].is_enabled()]
        if not enabled:
            self.status_label.setText("还没有启用任何渠道 —— 先勾上「启用」再测")
            return
        self.status_label.setText("正在逐个测试：" + "、".join(enabled))
        for name in enabled:
            self._test_channel(name)

    # 每个渠道卡片上的「测试」按钮都接到这里
    def _connect_card_tests(self) -> None:
        for name, card in self.cards.items():
            card.test_requested.connect(partial(self._test_channel, name))

    def refresh_history(self) -> None:
        """只刷新记录表 —— **不回填配置**。

        以前这个函数末尾会调一次 ``_load_channels()``，而它挂在 8 秒的定时器上：
        使用者正在输入 Webhook 时，输入框每 8 秒被静默还原成已保存的值。
        """
        try:
            payload = self.api.notifications(limit=100)
        except ApiError as exc:
            self.status_label.setText(exc.message)
            return
        rows = payload.get("notifications") or []
        self.history_table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            values = [
                item.get("created_at", ""),
                item.get("event_type", ""),
                item.get("title", ""),
                item.get("channel", ""),
                "成功" if item.get("ok") else f"失败（{item.get('error', '') or '无详情'}）",
            ]
            for col, text in enumerate(values):
                self.history_table.setItem(row, col, QTableWidgetItem(str(text)))

    def _load_channels(self) -> None:
        """从后端回填配置。

        敏感字段（webhook / chat_id / phone / key / appcode ...）后端只给 `********`，
        一律通过 `_fill_secret` 处理：**绝不把占位符放进输入框**。
        """
        try:
            config = self.api.get_config().get("config") or {}
        except ApiError:
            return
        notify = config.get("notify") or {}

        email = notify.get("email") or {}
        self.cards["email"].set_enabled(bool(email.get("enabled")))
        self.input_smtp_host.setText(email.get("smtp_host", "") or "")
        self.spin_smtp_port.setValue(int(email.get("smtp_port") or 465))
        self.check_smtp_ssl.setChecked(bool(email.get("use_ssl", True)))
        self.input_smtp_user.setText(email.get("username", "") or "")
        self.input_smtp_receivers.setText(",".join(email.get("receivers") or []))
        self._fill_secret(self.input_smtp_pass, email.get("password"), "留空表示不修改")

        serverchan = notify.get("serverchan") or {}
        self.cards["serverchan"].set_enabled(bool(serverchan.get("enabled")))
        self._fill_secret(self.input_serverchan_key, serverchan.get("key"), "SCT 开头的 SendKey")

        dingtalk = notify.get("dingtalk") or {}
        self.cards["dingtalk"].set_enabled(bool(dingtalk.get("enabled")))
        self._fill_secret(
            self.input_dingtalk, dingtalk.get("webhook"),
            "https://oapi.dingtalk.com/robot/send?access_token=...",
        )

        wecom = notify.get("wecom") or {}
        self.cards["wecom"].set_enabled(bool(wecom.get("enabled")))
        self._fill_secret(
            self.input_wecom, wecom.get("webhook"),
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...",
        )

        telegram = notify.get("telegram") or {}
        self.cards["telegram"].set_enabled(bool(telegram.get("enabled")))
        self._fill_secret(self.input_tg_token, telegram.get("bot_token"), "Bot Token（找 @BotFather 要）")
        self._fill_secret(self.input_tg_chat, telegram.get("chat_id"), "Chat ID（找 @userinfobot 查）")

        voice = notify.get("voice") or {}
        self.cards["voice"].set_enabled(bool(voice.get("enabled")))
        self._fill_secret(self.input_voice_appcode, voice.get("appcode"), "阿里云市场 AppCode")
        self._fill_secret(self.input_voice_phone, voice.get("phone"), "接收电话的手机号")

        self.check_web.setChecked(bool(notify.get("web", True)))
