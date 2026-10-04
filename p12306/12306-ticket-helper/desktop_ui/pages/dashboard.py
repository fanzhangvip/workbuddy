"""仪表盘页（UI-01）：当前该做什么、任务卡片、命中统计、快捷启停。

排布按「使用者此刻关心什么」定：
- 顶部**引导条**直接给一句当前处境 + 一个能点的按钮
  （没账号 → 去扫码；有账号没任务 → 去配任务；有任务没抢到 → 去看余票）；
- 然后是**三张主卡片**：已抢到 / 运行中任务 / 账号在线；
- 「运行指标」（查询量、延迟、风控）收在下面 —— 它们是留痕用的，日常不用盯。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient, ApiError

log = logging.getLogger(__name__)

STATE_COLORS = {
    "success": "#15803d",
    "grabbing": "#1d4ed8",
    "ready": "#b45309",
    "error": "#b91c1c",
    "stopped": "#64748b",
    "idle": "#64748b",
}


class _StatCard(QFrame):
    def __init__(self, title: str, *, big: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setObjectName("statCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        self.title = QLabel(title)
        self.title.setObjectName("statTitle")
        self.value = QLabel("-")
        self.value.setObjectName("statValueBig" if big else "statValue")
        layout.addWidget(self.title)
        layout.addWidget(self.value)

    def set_value(self, text: str, color: str | None = None) -> None:
        self.value.setText(text)
        self.value.setStyleSheet(f"color: {color};" if color else "")


class DashboardPage(QWidget):
    """仪表盘。"""

    #: 引导条里的「去配置任务」需要主窗口帮忙切页
    goto_requested = Signal(str)

    def __init__(self, api: ApiClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self.cards: dict[str, _StatCard] = {}
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)

        # ---------- 引导条 ---------- #
        self.hero = QFrame()
        self.hero.setObjectName("heroCard")
        hero_layout = QVBoxLayout(self.hero)
        hero_layout.setContentsMargins(14, 12, 14, 12)
        self.hero_title = QLabel("")
        self.hero_title.setObjectName("heroTitle")
        self.hero_text = QLabel("")
        self.hero_text.setObjectName("muted")
        self.hero_text.setWordWrap(True)
        self.hero_actions = QHBoxLayout()
        hero_layout.addWidget(self.hero_title)
        hero_layout.addWidget(self.hero_text)
        hero_layout.addLayout(self.hero_actions)
        root.addWidget(self.hero)

        # ---------- 工具栏 ---------- #
        toolbar = QHBoxLayout()
        self.btn_start_all = QPushButton("一键启动全部任务")
        self.btn_start_all.setObjectName("primaryButton")
        self.btn_stop_all = QPushButton("停止全部任务")
        self.btn_refresh = QPushButton("刷新")
        toolbar.addWidget(self.btn_start_all)
        toolbar.addWidget(self.btn_stop_all)
        toolbar.addWidget(self.btn_refresh)
        toolbar.addStretch(1)
        self.status_label = QLabel("正在加载…")
        self.status_label.setObjectName("muted")
        toolbar.addWidget(self.status_label)
        root.addLayout(toolbar)

        # ---------- 主卡片 ---------- #
        primary = QGridLayout()
        primary.setSpacing(10)
        for column, (key, title) in enumerate(
            [("success", "已抢到"), ("running", "运行中任务"), ("accounts", "账号在线")]
        ):
            card = _StatCard(title, big=True)
            self.cards[key] = card
            primary.addWidget(card, 0, column)
        root.addLayout(primary)

        # ---------- 任务列表 ---------- #
        header = QLabel("进行中的任务")
        header.setObjectName("sectionTitle")
        root.addWidget(header)

        self.tasks_container = QWidget()
        self.tasks_layout = QVBoxLayout(self.tasks_container)
        self.tasks_layout.setContentsMargins(0, 0, 0, 0)
        self.tasks_layout.setSpacing(8)
        self.tasks_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.tasks_container)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(scroll, 1)

        # ---------- 运行指标（次要，放最后） ---------- #
        metrics_title = QLabel("运行指标（查询量 / 延迟 / 风控，日常不用盯）")
        metrics_title.setObjectName("sectionTitle")
        root.addWidget(metrics_title)
        metrics = QGridLayout()
        metrics.setSpacing(10)
        for index, (key, title) in enumerate(
            [("queries", "累计查询"), ("today", "今日查询"), ("latency", "平均延迟"),
             ("interval", "查询间隔"), ("risk", "风控次数")]
        ):
            card = _StatCard(title)
            self.cards[key] = card
            metrics.addWidget(card, index // 5, index % 5)
        root.addLayout(metrics)

        self.btn_start_all.clicked.connect(self._start_all)
        self.btn_stop_all.clicked.connect(self._stop_all)
        self.btn_refresh.clicked.connect(self.refresh)

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        try:
            stats = self.api.stats()
            status = self.api.task_status_all()
        except ApiError as exc:
            self.status_label.setText(str(exc.message))
            return

        runtime = stats.get("runtime") or {}
        accounts = runtime.get("accounts") or {}
        scheduler = runtime.get("scheduler") or {}
        storage = stats.get("storage") or {}
        rate = runtime.get("rate_limit") or {}

        self.cards["accounts"].set_value(
            f"{accounts.get('online', 0)} / {accounts.get('total', 0)}",
            "#15803d" if accounts.get("online") else ("#b45309" if accounts.get("total") else "#b91c1c"),
        )
        self.cards["running"].set_value(
            f"{scheduler.get('running', 0)} / {scheduler.get('total', 0)}",
            "#15803d" if scheduler.get("running") else None,
        )
        self.cards["success"].set_value(str(scheduler.get("success", 0)), "#15803d" if scheduler.get("success") else None)
        self.cards["queries"].set_value(str(storage.get("queries_total", 0)))
        self.cards["today"].set_value(str(storage.get("queries_today", 0)))
        self.cards["latency"].set_value(f"{storage.get('latency_avg_ms', 0)} ms")
        self.cards["interval"].set_value(f"{rate.get('interval', 0)} s")
        self.cards["risk"].set_value(
            str(rate.get("risk_count", 0)), "#b91c1c" if rate.get("risk_count") else "#15803d"
        )
        self.status_label.setText(f"数据目录：{runtime.get('home', '-')}")

        self._render_hero(
            online=accounts.get("online", 0),
            total=accounts.get("total", 0),
            running=scheduler.get("running", 0),
            success=scheduler.get("success", 0),
        )
        self._render_tasks(status.get("tasks") or [])

    # ------------------------------------------------------------------ #
    def _render_hero(self, *, online: int, total: int, running: int, success: int) -> None:
        """一句当前处境 + 一个能点的按钮。

        空荡荡的仪表盘只会让人发呆：数字都在，但不知道该做什么。
        """
        while self.hero_actions.count():
            item = self.hero_actions.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        title, text, actions = "", "", []
        if total == 0:
            title = "第 1 步：登录一个 12306 账号"
            text = ("抢票要用你自己的 12306 账号。密码登录目前会被官方风控挡住，"
                    "请用手机 12306 App 扫码登录（登录成功后会记住会话，通常不用重复扫）。")
            actions = [("去扫码登录", "accounts")]
        elif online == 0:
            title = "账号都不在线，先恢复登录"
            text = (f"共 {total} 个账号，当前 0 个在线。账号掉线时任务跑起来也查不到票"
                    "（查询本身不需要登录，但下单必须登录）。")
            actions = [("去账号管理", "accounts")]
        elif running == 0:
            title = "账号已就绪，下一步是把任务跑起来"
            text = "还没有任务在运行。先去「任务配置」说清楚要坐哪天的哪趟车，然后启动它。"
            actions = [("去配置任务", "tasks"), ("一键启动已启用的任务", "__start_all__")]
        elif success > 0:
            title = f"已经抢到 {success} 张票"
            text = "本工具不代扣款：请尽快到 12306 完成支付，超时未支付的订单会被官方自动取消。"
            actions = [("去订单记录", "orders")]
        else:
            title = f"{running} 个任务正在盯票"
            text = "可以到「余票看板」看实时余票变化，或在「实时日志」里看它每一轮查到了什么。抢到票会有提示音和系统通知。"
            actions = [("看余票看板", "board"), ("看实时日志", "logs")]

        self.hero_title.setText(title)
        self.hero_text.setText(text)
        for label, target in actions:
            button = QPushButton(label)
            button.setObjectName("tinyButton" if len(actions) > 1 else "primaryButton")
            if target == "__start_all__":
                button.clicked.connect(self._start_all)
            else:
                button.clicked.connect(lambda _=False, k=target: self.goto_requested.emit(k))
            self.hero_actions.addWidget(button)
        self.hero_actions.addStretch(1)

    # ------------------------------------------------------------------ #
    def _render_tasks(self, tasks: list[dict[str, Any]]) -> None:
        while self.tasks_layout.count() > 1:
            item = self.tasks_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if not tasks:
            placeholder = QLabel(
                "还没有任务。\n去「任务配置」填好「1 行程 → 2 车次 → 3 席别」，保存后回到这里点启动。"
            )
            placeholder.setObjectName("muted")
            placeholder.setWordWrap(True)
            self.tasks_layout.insertWidget(0, placeholder)
            return

        for task in tasks:
            self.tasks_layout.insertWidget(self.tasks_layout.count() - 1, self._task_card(task))

    def _task_card(self, task: dict[str, Any]) -> QWidget:
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setObjectName("taskCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)

        state = task.get("state", "idle")
        title = QLabel(f"{task.get('task_name', '')}　[{task.get('state_text', state)}]")
        title.setStyleSheet(f"font-weight: 600; color: {STATE_COLORS.get(state, '#1f2937')};")
        layout.addWidget(title)

        stats = task.get("stats") or {}
        detail = (
            f"账号：{task.get('account') or '-'}　策略：{task.get('strategy') or '-'}　"
            f"间隔：{task.get('interval')}s\n"
            f"轮次：{stats.get('rounds', 0)}　命中候选：{stats.get('candidates_seen', 0)}　"
            f"下单尝试：{stats.get('order_attempts', 0)}　失败：{stats.get('order_failures', 0)}"
        )
        if task.get("error"):
            detail += f"\n错误：{task['error']}"
        label = QLabel(detail)
        label.setObjectName("muted")
        label.setWordWrap(True)
        layout.addWidget(label)

        buttons = QHBoxLayout()
        btn_stop = QPushButton("停止")
        btn_stop.clicked.connect(lambda _=False, tid=task.get("task_id"): self._stop_task(tid))
        buttons.addWidget(btn_stop)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return card

    # ------------------------------------------------------------------ #
    def _start_all(self) -> None:
        try:
            tasks = [t for t in self.api.list_tasks() if t.get("enabled")]
            for task in tasks:
                self.api.start_task(int(task["id"]))
            self.status_label.setText(f"已启动 {len(tasks)} 个任务")
            self.refresh()
        except ApiError as exc:
            self.status_label.setText(exc.message)

    def _stop_all(self) -> None:
        try:
            self.api.stop_system()
            self.status_label.setText("已发出停止指令")
            QTimer.singleShot(800, self.refresh)
        except ApiError as exc:
            self.status_label.setText(exc.message)

    def _stop_task(self, task_id: int | None) -> None:
        if task_id is None:
            return
        try:
            self.api.stop_task(task_id)
            self.refresh()
        except ApiError as exc:
            self.status_label.setText(exc.message)

    def on_task_status(self, tasks: list[dict[str, Any]]) -> None:
        """由 WebSocket 推送驱动，实时性比轮询更好。"""
        self._render_tasks(tasks)
