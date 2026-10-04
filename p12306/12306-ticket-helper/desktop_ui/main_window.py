"""桌面主窗口（UI-01 ~ UI-06）：左侧导航 + 六个功能页。

主窗口只做「界面与交互」，所有业务都通过 :class:`ApiClient` 打到本地后台，
与 Web 网页共用同一套引擎（UI-09）。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QCloseEvent, QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from desktop_ui.api_client import ApiClient
from desktop_ui.captcha_dialog import CaptchaDialog, CaptchaWatcher
from desktop_ui.pages import (
    AccountsPage,
    DashboardPage,
    LogPage,
    NotifyPage,
    OrdersPage,
    ProfilePage,
    RemainBoardPage,
    SettingsPage,
    TaskConfigPage,
)
from desktop_ui.realtime import RealtimeClient
from desktop_ui.tray import TrayIcon

log = logging.getLogger(__name__)

#: 页面清单：(key, 标题)。顺序 = 使用者实际会走的顺序。
#: 网页端用同一套分组与顺序，两端看到的结构一致，切换客户端不用重新找菜单。
PAGES = [
    ("dashboard", "总览"),
    ("tasks", "任务配置"),
    ("board", "余票看板"),
    ("accounts", "账号管理"),
    ("profile", "个人中心"),
    ("orders", "订单记录"),
    ("logs", "实时日志"),
    ("notify", "通知设置"),
    # 「系统设置」原先被塞在通知页里当第三个 tab —— 页名与内容不符，
    # 想改限速/打码的人不会去点「通知设置」。网页端一直是独立一页，
    # 拆出来之后两端清单逐项一致（`tests/test_contracts.py` 盯着这条）。
    ("settings", "系统设置"),
]

#: 导航分组：(组名, 该组下的页面 key)。空组名表示不显示组标题。
NAV_GROUPS: list[tuple[str, list[str]]] = [
    ("", ["dashboard"]),
    ("抢票", ["tasks", "board"]),
    ("账号", ["accounts", "profile"]),
    ("记录", ["orders", "logs"]),
    ("系统", ["notify", "settings"]),
]

#: key → 页面下标（导航行号 ≠ 页面下标，因为中间插了分组标题）
_PAGE_INDEX: dict[str, int] = {key: index for index, (key, _) in enumerate(PAGES)}


class MainWindow(QMainWindow):
    """主窗口。"""

    quitting = Signal()

    def __init__(self, api: ApiClient, realtime: RealtimeClient, tray: TrayIcon | None = None) -> None:
        super().__init__()
        self.api = api
        self.realtime = realtime
        self.tray = tray or TrayIcon()
        self._force_quit = False
        #: 正在展示的验证码弹窗（按任务 id 去重，避免同一个验证码弹好几次）
        self._captcha_dialogs: dict[str, CaptchaDialog] = {}

        self.setWindowTitle("12306 抢票助手 · 个人自用")
        # 默认开得宽一点：任务配置页是「左侧表单 + 右侧任务列表」两栏，
        # 1180 宽时表单只有 686，正好卡在「车次类型」标签要被压窄的边上。
        self.resize(1320, 880)
        # 窗口最小尺寸：再窄下去表单里的控件会开始文字截断，
        # 而拖窄时宁可让窗口停住，也不要让内容变形（横向滚动条在表单里最难用）。
        self.setMinimumSize(1120, 700)

        self._build()
        self._wire()
        self.switch_page(0)
        self.tray.set_tooltip("12306 抢票助手 · 挂机中")

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 左侧导航：按「使用者实际会走的顺序」分组。
        # 平铺八个菜单的坏处是：抢票这件事的主线（任务配置 → 余票看板）
        # 和一堆偶尔才用的设置项混在一起，看不出重点在哪。
        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        self.nav.setFixedWidth(168)
        self._nav_row_of: dict[str, int] = {}
        self._syncing_nav = False
        for group_name, keys in NAV_GROUPS:
            if group_name:
                header = QListWidgetItem(group_name)
                header.setFlags(Qt.ItemFlag.NoItemFlags)   # 组标题不可选中，方向键会跳过它
                header.setForeground(QBrush(QColor("#7c8b9d")))
                self._apply_group_style(header)
                self.nav.addItem(header)
            for key in keys:
                title = dict(PAGES)[key]
                item = QListWidgetItem(title)
                item.setData(Qt.ItemDataRole.UserRole, key)
                item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
                self.nav.addItem(item)
                self._nav_row_of[key] = self.nav.count() - 1
        layout.addWidget(self.nav)

        # 右侧页面
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {
            "dashboard": DashboardPage(self.api),
            "accounts": AccountsPage(self.api),
            "profile": ProfilePage(self.api),
            "tasks": TaskConfigPage(self.api),
            "board": RemainBoardPage(self.api),
            "orders": OrdersPage(self.api),
            "logs": LogPage(self.api),
            "notify": NotifyPage(self.api),
            "settings": SettingsPage(self.api),
        }
        for key, _ in PAGES:
            self.stack.addWidget(self.pages[key])
        layout.addWidget(self.stack, 1)

        self.setCentralWidget(central)

        bar = QStatusBar()
        # 状态栏常驻「账号在线 / 运行中任务」——这是决定要不要现在动手的两个数，
        # 不该只在总览页看得到。
        self.overview_label = QLabel("")
        self.overview_label.setToolTip("账号在线数 / 运行中任务数（每 10 秒刷新）")
        bar.addWidget(self.overview_label)
        self.connection_label = QLabel("实时通道：检测中…")
        self.connection_label.setToolTip(
            "实时通道用于把日志与任务状态即时推到界面。\n"
            "优先使用 WebSocket；若当前 PySide6 未包含 QtWebSockets，\n"
            "会自动降级为轮询（功能不缺失，只是延迟约 2 秒）。"
        )
        bar.addPermanentWidget(self.connection_label)
        self.setStatusBar(bar)

        self.overview_timer = QTimer(self)
        self.overview_timer.setInterval(10000)
        self.overview_timer.timeout.connect(self.refresh_overview)
        self.overview_timer.start()
        self.refresh_overview()

    def _apply_group_style(self, item: QListWidgetItem) -> None:
        from PySide6.QtGui import QFont

        font = QFont()
        font.setPointSize(max(7, font.pointSize() - 2))
        item.setFont(font)
        item.setData(Qt.ItemDataRole.UserRole, "")

    def refresh_overview(self) -> None:
        """状态栏概览：失败时不清空上次的结果，免得数字一闪一闪。"""
        try:
            runtime = (self.api.stats() or {}).get("runtime") or {}
        except Exception as exc:  # noqa: BLE001 - 概览刷不出来不该影响使用
            log.debug("刷新状态栏概览失败: %s", exc)
            return
        accounts = runtime.get("accounts") or {}
        scheduler = runtime.get("scheduler") or {}
        online, total = accounts.get("online", 0), accounts.get("total", 0)
        self.overview_label.setText(
            f"账号在线 {online}/{total}　·　运行中任务 {scheduler.get('running', 0)}/{scheduler.get('total', 0)}"
            f"　·　已抢到 {scheduler.get('success', 0)}"
        )

    def _wire(self) -> None:
        self.nav.currentRowChanged.connect(self._on_nav_row_changed)
        # 总览页引导条上的按钮要能把人送到对应页面（「去扫码登录」等）
        self.pages["dashboard"].goto_requested.connect(self.show_page_key)  # type: ignore[attr-defined]
        self.realtime.log_received.connect(self.pages["logs"].append)  # type: ignore[attr-defined]
        self.realtime.notification_received.connect(self._on_notification)
        self.realtime.task_status_received.connect(self._on_task_status)
        self.realtime.user_event_received.connect(self._on_user_event)
        self.realtime.connection_changed.connect(self._on_connection_changed)
        self.realtime.transport_changed.connect(lambda _="": self._refresh_transport_label())

        # 登录验证码必须有人取走，否则登录会一直卡在「登录中」直到超时
        self.captcha_watcher = CaptchaWatcher(self.api, self)
        self.captcha_watcher.task_received.connect(self._on_captcha_task)
        self.captcha_watcher.start()

        self.tray.show_window.connect(self.restore_window)
        self.tray.start_all.connect(lambda: self.pages["dashboard"]._start_all())  # noqa: SLF001
        self.tray.stop_all.connect(lambda: self.pages["dashboard"]._stop_all())  # noqa: SLF001
        self.tray.quit_requested.connect(self.request_quit)

    # ------------------------------------------------------------------ #
    def _on_captcha_task(self, task: dict[str, Any]) -> None:
        """收到待录入的登录验证码 → 弹窗（最小化时先通知，避免用户不知道要操作）。"""
        task_id = str(task.get("id") or "")
        if not task_id or task_id in self._captcha_dialogs:
            return
        dialog = CaptchaDialog(self.api, task, self)
        dialog.finished.connect(lambda _=0, tid=task_id: self._captcha_dialogs.pop(tid, None))
        dialog.submitted.connect(self._on_captcha_submitted)
        self._captcha_dialogs[task_id] = dialog

        if self.isMinimized() or not self.isVisible():
            self.tray.show_notification("需要输入验证码", "登录 12306 需要验证码，请打开窗口完成输入。")
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_captcha_submitted(self) -> None:
        self._refresh_accounts_soon()

    def _refresh_accounts_soon(self, delay_ms: int = 800) -> None:
        """登录是异步的，稍等一下再刷新，状态才来得及更新。"""
        QTimer.singleShot(delay_ms, lambda: self.pages["accounts"].refresh())  # type: ignore[attr-defined]

    def _on_user_event(self, event: str, payload: dict[str, Any]) -> None:
        """账号登录成功/失败 → 即时刷新账号页与仪表盘，不用等用户手动刷新。"""
        if event == "login_success":
            self.tray.show_notification("账号登录成功", f"{payload.get('user', '')} 已登录 12306")
        elif event == "login_failed":
            self.tray.show_notification(
                "账号登录失败", f"{payload.get('user', '')}：{payload.get('error') or '请查看日志'}"
            )
        self._refresh_accounts_soon(300)
        self.pages["dashboard"].refresh()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ #
    def _on_nav_row_changed(self, row: int) -> None:
        """导航行变化 → 切页。

        导航列表里混着不可选中的**分组标题**，所以行号不等于页面下标，
        必须先取回 ``UserRole`` 里存的页面 key。
        """
        if self._syncing_nav:
            return
        item = self.nav.item(row)
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if key:
            self.switch_page(_PAGE_INDEX[key])

    def show_page_key(self, key: str) -> None:
        """按页面 key 切页（引导条按钮、账号「详情」等都走这里）。"""
        index = _PAGE_INDEX.get(key)
        if index is not None:
            self.switch_page(index)

    def switch_page(self, index: int) -> None:
        if index < 0 or index >= len(PAGES):
            return
        key = PAGES[index][0]
        row = self._nav_row_of.get(key)
        if row is not None and self.nav.currentRow() != row:
            self._syncing_nav = True
            self.nav.setCurrentRow(row)
            self._syncing_nav = False
        self.stack.setCurrentIndex(index)
        # 进入日志页时补一次历史日志（之后靠 WS 增量）
        if key == "logs":
            self.pages["logs"].load_backlog()  # type: ignore[attr-defined]
        if key == "board":
            self.pages["board"].refresh_tasks()  # type: ignore[attr-defined]
        if key == "notify":
            self.pages["notify"].refresh_history()  # type: ignore[attr-defined]
        if key == "settings":
            self.pages["settings"].refresh()  # type: ignore[attr-defined]
        if key == "profile":
            # 进来就带上最新的账号（登录状态可能在别处刚变过）
            self.pages["profile"].refresh_accounts()  # type: ignore[attr-defined]

    def show_profile(self, user: dict[str, Any]) -> None:
        """切到「个人中心」并查看指定账号。

        账号列表的「详情」按钮用它 —— 与其再弹一个窗口，不如把人带到
        那个真正放功能的地方，下次他自己就找得到了。
        """
        page = self.pages.get("profile")
        if page is None:
            return
        for index, (key, _) in enumerate(PAGES):
            if key == "profile":
                self.switch_page(index)
                break
        page.show_account(user)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ #
    def _on_notification(self, event: dict[str, Any]) -> None:
        """出票 / 候补 / 异常提醒：托盘气泡 + 声音（NT-04 / OD-02）。"""
        title = event.get("title") or "通知"
        body = event.get("body") or ""
        critical = bool(event.get("critical"))
        self.tray.show_notification(title, body, critical=critical)
        if critical:
            self._alert()
            self.pages["dashboard"].refresh()  # type: ignore[attr-defined]

    def _alert(self) -> None:
        """出票提示音。用系统响铃，避免打包额外音频资源。"""
        try:
            from PySide6.QtWidgets import QApplication

            QApplication.beep()
            QApplication.beep()
            QApplication.beep()
        except Exception:  # noqa: BLE001
            pass

    def _on_task_status(self, tasks: list[dict[str, Any]]) -> None:
        self.pages["dashboard"].on_task_status(tasks)  # type: ignore[attr-defined]

    def _on_connection_changed(self, connected: bool) -> None:
        self._refresh_transport_label(connected=connected)

    def _refresh_transport_label(self, *, connected: bool | None = None) -> None:
        """如实显示当前传输方式，而不是笼统的「未连接」。

        之前不论什么原因都只显示「未连接」：环境里缺 QtWebSockets、后台没起、
        令牌失效，看上去一模一样，使用者无从判断。
        """
        transport = getattr(self.realtime, "transport", "offline")
        text = getattr(self.realtime, "mode_text", "未连接")
        if connected is False and transport == "offline":
            text = "重连中…"
        self.connection_label.setText(f"实时通道：{text}")
        self.connection_label.setStyleSheet(
            "color: #15803d;" if transport in ("websocket", "polling")
            else "color: #b45309;"
        )

    # ------------------------------------------------------------------ #
    def restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def request_quit(self) -> None:
        """真正的退出流程。"""
        answer = QMessageBox.question(            self,
            "退出抢票助手",
            "确定要退出吗？\n\n选择「Yes」将同时关闭后台服务（停止所有抢票任务）；\n"
            "选择「No」仅关闭界面，后台继续在托盘/后台挂机。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return
        if answer == QMessageBox.StandardButton.Yes:
            try:
                self.api.stop_system()
            except Exception as exc:  # noqa: BLE001
                log.warning("停止后台服务失败: %s", exc)
        # 退出前停掉验证码轮询，避免留下后台线程继续请求
        try:
            self.captcha_watcher.stop()
        except Exception:  # noqa: BLE001
            pass
        self._force_quit = True
        self.quitting.emit()
        self.close()

    # ------------------------------------------------------------------ #
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名约定
        """点关闭按钮 → 最小化到托盘继续挂机（UI-07）。

        特别注意「系统不支持托盘」的情况（部分精简系统、远程桌面会话）：
        此时窗口一关就没有任何入口能把它调回来，而 ``setQuitOnLastWindowClosed(False)``
        又阻止了程序退出，结果是留下一个**看不见的僵尸进程**占着端口，
        用户再双击会毫无反应——表现和「程序坏了」一模一样。
        所以这里必须把关闭当作真正的退出。
        """
        if self._force_quit:
            event.accept()
            return

        if not self.tray.available:
            log.info("系统托盘不可用，关闭窗口即退出程序")
            self._force_quit = True
            event.accept()
            self.quitting.emit()
            return

        event.ignore()
        self.hide()
        self.tray.show_notification(
            "抢票助手仍在后台运行",
            "已最小化到系统托盘，抢票任务继续执行。\n双击托盘图标可恢复窗口，右键菜单可退出。",
        )
