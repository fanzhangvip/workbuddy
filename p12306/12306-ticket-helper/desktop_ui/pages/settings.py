"""系统设置页（UI-07）：运行信息、反检测限速、验证码识别、配置变更历史。

为什么单独一页：这些控件原先塞在「通知设置」页的第三个 tab 里 —— **页名与内容
不符**（想改限速的人不会去点「通知设置」），而网页端早就是独立的一页。
拆出来之后两端导航**逐项一致**，换客户端不用重新找菜单。

判据（都写在测试里，防止它们又被挪回去）：
- `tests/test_contracts.py::test_desktop_and_web_navigation_are_identical`
  两端页面清单一致；
- `tests/test_desktop.py::test_settings_page_owns_the_ratelimit_and_codec_controls`
  「限速 / 打码」控件属于这一页，且**不在**通知页里。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient, ApiError
from desktop_ui.layout import CARD_GAP, ROW_GAP

log = logging.getLogger(__name__)


class SettingsPage(QWidget):
    """系统设置：运行信息 / 反检测限速 / 验证码识别 / 配置变更历史。"""

    def __init__(self, api: ApiClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ #
    # 组装
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        body = QVBoxLayout(host)
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(CARD_GAP)

        body.addWidget(self._build_info_card())
        body.addWidget(self._build_ratelimit_card())
        body.addWidget(self._build_codec_card())
        body.addWidget(self._build_history_card())
        body.addStretch(1)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)

        actions = QWidget()
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(12, 8, 12, 12)
        actions_layout.setSpacing(10)
        self.btn_save_system = QPushButton("保存系统设置")
        self.btn_save_system.setObjectName("primaryButton")
        self.btn_refresh_system = QPushButton("刷新信息")
        self.system_status = QLabel("")
        self.system_status.setObjectName("muted")
        self.system_status.setWordWrap(True)
        actions_layout.addWidget(self.btn_save_system)
        actions_layout.addWidget(self.btn_refresh_system)
        actions_layout.addStretch(1)
        actions_layout.addWidget(self.system_status)
        layout.addWidget(actions)

        self.btn_save_system.clicked.connect(self._save_system)
        self.btn_refresh_system.clicked.connect(self.refresh)

    def _build_info_card(self) -> QGroupBox:
        box = QGroupBox("运行信息")
        form = QFormLayout(box)
        form.setContentsMargins(14, 12, 14, 12)
        form.setVerticalSpacing(ROW_GAP)
        form.setHorizontalSpacing(16)
        self.info_labels: dict[str, QLabel] = {}
        for key, title in [
            ("running", "后台状态"),
            ("version", "程序版本"),
            ("uptime", "运行时长"),
            ("home", "数据目录"),
            ("api_base", "接口基址"),
            ("config_version", "配置版本"),
            ("codec", "验证码识别链"),
            ("proxy", "代理池"),
            ("watchdog", "看门狗"),
        ]:
            label = QLabel("-")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.info_labels[key] = label
            form.addRow(title, label)
        return box

    def _build_ratelimit_card(self) -> QGroupBox:
        box = QGroupBox("反检测限速（避免 IP 被风控）")
        grid = QGridLayout(box)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(ROW_GAP)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        self.check_adaptive = QCheckBox("自适应降频（被风控时自动放慢）")
        self.check_adaptive.setChecked(True)
        self.spin_min_interval = QDoubleSpinBox()
        self.spin_min_interval.setRange(0.3, 60.0)
        self.spin_min_interval.setSingleStep(0.1)
        self.spin_min_interval.setValue(1.0)
        self.spin_min_interval.setSuffix(" 秒")
        self.spin_max_interval = QDoubleSpinBox()
        self.spin_max_interval.setRange(1.0, 120.0)
        self.spin_max_interval.setSingleStep(0.5)
        self.spin_max_interval.setValue(8.0)
        self.spin_max_interval.setSuffix(" 秒")
        self.input_proxy = QLineEdit()
        self.input_proxy.setPlaceholderText("http://ip:port,http://ip2:port（留空表示直连）")

        grid.addWidget(self.check_adaptive, 0, 0, 1, 2)
        grid.addWidget(self._labeled("最小间隔", self.spin_min_interval), 1, 0)
        grid.addWidget(self._labeled("最大间隔", self.spin_max_interval), 1, 1)
        grid.addWidget(self._labeled("代理池（可选）", self.input_proxy), 2, 0, 1, 2)
        return box

    def _build_codec_card(self) -> QGroupBox:
        box = QGroupBox("验证码识别")
        grid = QGridLayout(box)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(ROW_GAP)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        self.combo_codec_mode = QComboBox()
        self.combo_codec_mode.addItem("free · 外部打码服务", "free")
        self.combo_codec_mode.addItem("local · 本地模型", "local")
        self.combo_codec_mode.addItem("manual · 人工兜底", "manual")
        self.input_codec_model = QLineEdit()
        self.input_codec_model.setPlaceholderText("本地 onnx 模型路径")
        self.input_codec_endpoint = QLineEdit()
        # 地址留空 = 本机服务默认地址。写清默认值，否则使用者不知道该填什么。
        self.input_codec_endpoint.setPlaceholderText(
            "留空 = 本机 127.0.0.1:8899/check（python -m ticket_helper.verify.captcha_server）"
        )
        self.spin_codec_retry = QSpinBox()
        self.spin_codec_retry.setRange(1, 10)
        self.spin_codec_retry.setValue(3)
        self.check_codec_fallback = QCheckBox("识别失败自动切换数据源")
        self.check_codec_fallback.setChecked(True)

        grid.addWidget(self._labeled("识别模式", self.combo_codec_mode), 0, 0)
        grid.addWidget(self._labeled("重试上限", self.spin_codec_retry), 0, 1)
        grid.addWidget(self._labeled("打码服务地址", self.input_codec_endpoint), 1, 0, 1, 2)
        grid.addWidget(self._labeled("本地模型路径", self.input_codec_model), 2, 0, 1, 2)
        grid.addWidget(self.check_codec_fallback, 3, 0, 1, 2)
        return box

    def _build_history_card(self) -> QGroupBox:
        """配置变更历史（与网页端「系统设置」页读的是同一条接口）。"""
        box = QGroupBox("配置变更历史")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(ROW_GAP)
        hint = QLabel("每次「保存并热生效」都会记一行 —— 配置被改成了什么，这里能倒着查。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.config_history_table = QTableWidget(0, 3)
        self.config_history_table.setHorizontalHeaderLabels(["时间", "配置节", "新值"])
        self.config_history_table.verticalHeader().setVisible(False)
        self.config_history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.config_history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.config_history_table.setMinimumHeight(160)
        layout.addWidget(self.config_history_table)
        return box

    @staticmethod
    def _labeled(label: str, widget: QWidget) -> QWidget:
        cell = QWidget()
        layout = QVBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        text = QLabel(label)
        text.setObjectName("muted")
        layout.addWidget(text)
        layout.addWidget(widget)
        return cell

    # ------------------------------------------------------------------ #
    # 读 / 写
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """刷新全部（进页面时调一次；保存后也调）。"""
        self.refresh_system()
        self.refresh_history()

    def refresh_system(self) -> None:
        try:
            snapshot = self.api.system_snapshot()
            config = self.api.get_config().get("config") or {}
        except ApiError as exc:
            self.info_labels["running"].setText(exc.message)
            return

        rate = snapshot.get("rate_limit") or {}
        proxy = rate.get("proxy") or {}
        codec = snapshot.get("codec") or {}
        chain = "、".join(
            f"{item['name']}{'' if item.get('available') else '(不可用)'}" for item in (codec.get("chain") or [])
        )
        minutes = int((snapshot.get("uptime_seconds") or 0) // 60)
        self.info_labels["version"].setText("v1.0.0")
        self.info_labels["home"].setText(str(snapshot.get("home", "-")))
        self.info_labels["api_base"].setText(str(snapshot.get("api_base", "-")))
        self.info_labels["uptime"].setText(f"{minutes} 分钟")
        self.info_labels["running"].setText("运行中" if snapshot.get("running") else "未启动")
        self.info_labels["config_version"].setText(str(snapshot.get("config_version", "-")))
        self.info_labels["codec"].setText(chain or "-")
        self.info_labels["proxy"].setText(
            f"已启用 {proxy.get('size', 0)} 个" if proxy.get("enabled") else "未启用（直连）"
        )
        cluster = snapshot.get("cluster") or {}
        self.info_labels["watchdog"].setText(
            f"节点 {cluster.get('node', 'local')}　角色 {cluster.get('role', 'master')}　"
            f"分布式 {'已启用' if cluster.get('enabled') else '未启用'}"
        )

        ratelimit = config.get("ratelimit") or {}
        self.check_adaptive.setChecked(bool(ratelimit.get("adaptive", True)))
        self.spin_min_interval.setValue(float(ratelimit.get("min_interval") or 1.0))
        self.spin_max_interval.setValue(float(ratelimit.get("max_interval") or 8.0))
        self.input_proxy.setText(ratelimit.get("proxy_pool", "") or "")

        codec_config = config.get("codec") or {}
        index = self.combo_codec_mode.findData(codec_config.get("mode", "free"))
        self.combo_codec_mode.setCurrentIndex(max(0, index))
        self.input_codec_model.setText(codec_config.get("local_model_path", "") or "")
        self.input_codec_endpoint.setText(codec_config.get("user_endpoint", "") or "")
        self.spin_codec_retry.setValue(int(codec_config.get("retry_limit") or 3))
        self.check_codec_fallback.setChecked(bool(codec_config.get("fallback_enabled", True)))

    def refresh_history(self) -> None:
        try:
            rows = self.api.config_history()
        except ApiError as exc:
            self.system_status.setText(f"读配置变更历史失败：{exc.message}")
            return
        self.config_history_table.setRowCount(len(rows))
        for row_index, item in enumerate(rows):
            cells = [
                str(item.get("changed_at", "") or ""),
                str(item.get("key", "") or ""),
                str(item.get("new_value", "") or ""),
            ]
            for col, text in enumerate(cells):
                self.config_history_table.setItem(row_index, col, QTableWidgetItem(text))

    def _save_system(self) -> None:
        try:
            self.api.update_config(
                "ratelimit",
                {
                    "adaptive": self.check_adaptive.isChecked(),
                    "min_interval": self.spin_min_interval.value(),
                    "max_interval": self.spin_max_interval.value(),
                    "proxy_pool": self.input_proxy.text().strip(),
                },
            )
            codec_payload: dict[str, Any] = {
                "mode": self.combo_codec_mode.currentData(),
                "local_model_path": self.input_codec_model.text().strip(),
                "user_endpoint": self.input_codec_endpoint.text().strip(),
                "retry_limit": self.spin_codec_retry.value(),
                "fallback_enabled": self.check_codec_fallback.isChecked(),
            }
            self.api.update_config("codec", codec_payload)
            self.system_status.setText("系统设置已保存并热生效")
            self.refresh()
        except ApiError as exc:
            self.system_status.setText(exc.message)
