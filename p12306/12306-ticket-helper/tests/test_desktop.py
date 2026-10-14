"""桌面客户端测试（UI-T1 ~ UI-T4）。

原则：这些测试在**没有安装 PySide6** 的环境下自动跳过，
不影响「纯后端」的 CI 流程；安装后使用 Qt 的 offscreen 平台插件运行，
无需真实显示器（CI 友好）。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过桌面端测试")

# 必须在创建 QApplication 之前设置，否则无显示环境下会直接崩
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from desktop_ui.api_client import ApiClient  # noqa: E402
from ticket_helper.config.settings import UserConfig  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def web_backend(app_context, mock_server):
    """起一个真实的后台（复用 Flask test 以外的真实 HTTP 服务）。"""
    from werkzeug.serving import make_server

    import socket
    import threading
    import time

    from ticket_helper.web.app import create_app

    app = create_app(app_context, start_background=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# api_client
# --------------------------------------------------------------------------- #
def test_api_client_login_and_ping(web_backend):
    client = ApiClient(web_backend)
    ping = client.ping()
    assert ping is not None and ping["ok"] is True

    assert client.login("admin", "test-pass") is True
    assert client.token
    users = client.list_users()
    assert isinstance(users, list)


def test_api_client_bad_password_raises(web_backend):
    from desktop_ui.api_client import ApiError

    client = ApiClient(web_backend)
    with pytest.raises(ApiError):
        client.login("admin", "definitely-wrong")


def test_api_client_ping_returns_none_when_offline():
    client = ApiClient("http://127.0.0.1:1", timeout=0.5)
    assert client.ping(timeout=0.5) is None


def test_api_client_task_roundtrip(web_backend):
    client = ApiClient(web_backend)
    client.login("admin", "test-pass")

    created = client.create_task(
        {
            "name": "桌面端任务",
            "left_dates": "2026-10-01",
            "from_stations": "广州",
            "to_stations": "长沙",
            "seat_types": "二等座",
            "passengers": "张三",
            "strategy": "direct",
        }
    )
    task_id = int(created["id"])
    tasks = client.list_tasks()
    assert any(int(t["id"]) == task_id for t in tasks)

    status = client.task_status_all()
    assert "tasks" in status
    remain = client.task_remain(task_id)
    assert "candidates" in remain

    client.delete_task(task_id)


# --------------------------------------------------------------------------- #
# UI-T1 主窗口与页面切换
# --------------------------------------------------------------------------- #
def test_main_window_pages_and_profile_entry(qt_app, web_backend):
    """主窗口的页面数量与切换，且必须存在「个人中心」这个一级入口。

    为什么专门钉住「个人中心」在导航里：功能一开始只挂在账号表格最后一列的
    「详情」按钮上，使用者扫了一眼左侧导航就认定「功能没做」
    —— 功能存在但不被发现，等于不存在。
    """
    from desktop_ui.main_window import NAV_GROUPS, PAGES, MainWindow
    from desktop_ui.realtime import RealtimeClient

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    realtime = RealtimeClient(client)
    window = MainWindow(client, realtime)

    titles = [title for _, title in PAGES]
    assert "个人中心" in titles, f"左侧导航里必须有「个人中心」入口，当前为 {titles}"
    assert "订单记录" in titles, "抢到票之后要有地方看订单号和去付款"
    assert len(PAGES) == 9
    assert window.stack.count() == 9
    # 导航按用户动线分组，且每个页面都要被某组收进去（漏了就等于没有入口）
    grouped = [key for _, keys in NAV_GROUPS for key in keys]
    assert sorted(grouped) == sorted(key for key, _ in PAGES), f"导航分组与页面清单不一致：{grouped}"
    # 逐个切换，确保不崩
    for index in range(len(PAGES)):
        window.switch_page(index)
        assert window.stack.currentIndex() == index
    # 分组标题是不可选中的行，行号与页面下标不再一一对应 —— 这层映射必须独立成立
    assert window.nav.currentRow() != PAGES.index(("tasks", "任务配置"))
    assert window.nav.currentItem().data(Qt.ItemDataRole.UserRole) == "settings"

    # 账号行的「详情」应切到这个页面（而不是另开窗口）
    window.show_profile({"id": 2, "username": "张三", "online": True})
    assert PAGES[window.stack.currentIndex()][0] == "profile"
    assert window.pages["profile"].panel.username == "张三"

    window._force_quit = True  # noqa: SLF001
    window.close()
    realtime.stop()


def test_window_title_and_status_bar_carry_the_copyright(qt_app, web_backend):
    """★ 版权信息必须出现在「不用找就能看到」的两个位置：窗口标题 + 状态栏。

    为什么单独一条：常量改对了**不等于**界面上改了。窗口标题与状态栏是两处
    各自独立的拼装代码，任何一处漏改，用户看到的还是旧文字，而所有常量层面的
    测试依旧全绿 —— 这正是「改名改一半」的典型形态。
    """
    from desktop_ui.main_window import MainWindow
    from desktop_ui.realtime import RealtimeClient
    from ticket_helper import APP_AUDIENCE, APP_COPYRIGHT, APP_WEBSITE

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    window = MainWindow(client, RealtimeClient(client))
    window.show()

    assert APP_AUDIENCE in window.windowTitle(), f"窗口标题缺使用范围：{window.windowTitle()!r}"

    # 状态栏用富文本承载「版权 + 可点击官网」，所以分别断言文字与链接能力
    label = window.disclaimer_label
    assert APP_COPYRIGHT in label.text(), f"状态栏缺版权声明：{label.text()!r}"
    assert APP_WEBSITE in label.text(), f"状态栏缺官网链接：{label.text()!r}"
    assert label.textFormat() == Qt.TextFormat.RichText, "官网要做成链接就必须是富文本"
    assert label.openExternalLinks(), "链接不可点等于没放（QLabel 默认点不动）"

    window._force_quit = True  # noqa: SLF001
    window.close()


# --------------------------------------------------------------------------- #
# UI-T2 任务配置表单保存/回显
# --------------------------------------------------------------------------- #
def test_task_form_save_and_echo(qt_app, web_backend):
    from desktop_ui.pages.tasks import TaskConfigPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = TaskConfigPage(client)
    page.timer.stop()

    page.input_name.setText("表单测试任务")
    page.input_dates.setText("2026-10-01,2026-10-02")
    page.input_from.setText("广州南")
    page.input_to.setText("长沙南")
    page.input_white.setText("G6102")
    page.input_passengers.setText("张三")
    page.input_time.setText("06:00,23:00")
    page.spin_interval.setValue(1.5)

    payload = page._payload()  # noqa: SLF001
    assert payload["name"] == "表单测试任务"
    assert payload["left_dates"] == "2026-10-01,2026-10-02"   # Web 层会再解析成数组
    assert payload["query_interval"] == 1.5
    assert "二等座" in payload["seat_types"]

    page._save()  # noqa: SLF001
    tasks = client.list_tasks()
    saved = next((t for t in tasks if t["name"] == "表单测试任务"), None)
    assert saved is not None

    # 回显
    page._load_into_form(saved)  # noqa: SLF001
    assert page.input_name.text() == "表单测试任务"
    assert page.editing_id == int(saved["id"])
    assert page.input_white.text() == "G6102"

    page.reset_form()
    assert page.input_name.text() == ""
    assert page.editing_id is None

    client.delete_task(int(saved["id"]))


# --------------------------------------------------------------------------- #
# UI-T3 余票看板
# --------------------------------------------------------------------------- #
def test_remain_board_renders(qt_app, web_backend):
    from desktop_ui.pages.board import RemainBoardPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = RemainBoardPage(client)
    page.timer.stop()
    page.refresh_tasks()

    assert page.combo_task.count() >= 0
    page.refresh()   # 无任务时也不应崩
    assert page.status_label.text() != "" or page.combo_task.count() > 0


# --------------------------------------------------------------------------- #
# UI-T4 日志窗
# --------------------------------------------------------------------------- #
def test_log_page_appends_and_filters(qt_app, web_backend):
    from desktop_ui.pages.logs import LogPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = LogPage(client)
    page.clear()

    page.append({"ts": "2026-01-01 00:00:00", "level": "INFO", "logger": "t", "message": "普通信息"})
    page.append({"ts": "2026-01-01 00:00:01", "level": "ERROR", "logger": "t", "message": "出错了"})
    assert "普通信息" in page.view.toPlainText()
    assert "出错了" in page.view.toPlainText()

    # 过滤到 ERROR
    page.combo_level.setCurrentText("ERROR")
    page.append({"ts": "2026-01-01 00:00:02", "level": "INFO", "logger": "t", "message": "不应出现"})
    assert "不应出现" not in page.view.toPlainText()


def test_log_page_trims_when_too_long(qt_app, web_backend):
    from desktop_ui.pages.logs import MAX_LINES, LogPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = LogPage(client)
    page.clear()
    for index in range(MAX_LINES + 300):
        page.append({"ts": "t", "level": "INFO", "logger": "t", "message": f"line-{index}"})
    assert page._line_count <= MAX_LINES  # noqa: SLF001
    assert "line-0" not in page.view.toPlainText()


# --------------------------------------------------------------------------- #
# 托盘
# --------------------------------------------------------------------------- #
def test_tray_icon_signals_defined(qt_app):
    from desktop_ui.tray import TrayIcon, build_icon

    icon = build_icon()
    assert not icon.isNull()
    tray = TrayIcon()
    # 托盘是否可用取决于系统；这里只验证对象构建与信号可用
    assert hasattr(tray, "show_notification")
    tray.show_notification("测试", "内容")
    tray.set_tooltip("测试")
    tray.hide()


# --------------------------------------------------------------------------- #
# 本机配置读取（自动登录）
# --------------------------------------------------------------------------- #
def test_load_web_credentials(app_paths):
    from desktop_ui.local_env import load_web_credentials

    app_paths.env_file.write_text(
        "ENV = {'web': {'host': '127.0.0.1', 'port': 8008, 'auth_user': 'admin', 'auth_pass': 'pw123'}}\n",
        encoding="utf-8",
    )
    user, password, base_url = load_web_credentials(app_paths.home)
    assert user == "admin"
    assert password == "pw123"
    assert base_url == "http://127.0.0.1:8008"


def test_load_web_credentials_missing_file(tmp_path):
    from desktop_ui.local_env import load_web_credentials

    assert load_web_credentials(tmp_path) == ("", "", "")


def test_load_web_credentials_normalizes_wildcard_host(app_paths):
    from desktop_ui.local_env import load_web_credentials

    app_paths.env_file.write_text(
        "ENV = {'web': {'host': '0.0.0.0', 'port': 9000, 'auth_pass': 'x'}}\n", encoding="utf-8"
    )
    _, _, base_url = load_web_credentials(app_paths.home)
    assert base_url == "http://127.0.0.1:9000"


# --------------------------------------------------------------------------- #
# 后台启动器
# --------------------------------------------------------------------------- #
def test_backend_launcher_build_command():
    from desktop_ui.api_client import BackendLauncher

    launcher = BackendLauncher(ApiClient("http://127.0.0.1:8008"))
    command = launcher.build_command(port=8123)
    assert "web" in command
    assert "8123" in command
    assert launcher.owned is False


def test_backend_launcher_detects_running_backend(web_backend):
    from desktop_ui.api_client import ApiClient as AC
    from desktop_ui.api_client import BackendLauncher

    launcher = BackendLauncher(AC(web_backend))
    assert launcher.is_ready() is True
    # 已在运行 → 不应再拉起新进程
    assert launcher.start() is True
    assert launcher.owned is False


# --------------------------------------------------------------------------- #
# 端口占用自愈（用户双击两次 / 上次后台没退干净时不再直接失败）
# --------------------------------------------------------------------------- #
def test_find_free_port_skips_occupied():
    import socket

    from desktop_ui.api_client import BackendLauncher

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        occupied = int(sock.getsockname()[1])

        assert BackendLauncher.is_port_available(occupied) is False
        free = BackendLauncher.find_free_port(occupied)
        assert free is not None and free > occupied
        assert BackendLauncher.is_port_available(free) is True


def test_find_free_port_returns_preferred_when_idle():
    import socket

    from desktop_ui.api_client import BackendLauncher

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    assert BackendLauncher.find_free_port(port) == port


# --------------------------------------------------------------------------- #
# 桌面端崩溃兜底：不能静默消失
# --------------------------------------------------------------------------- #
def test_desktop_main_reports_crash_instead_of_vanishing(qt_app, temp_home, monkeypatch):
    """回归：窗口模式下异常必须变成「可读原因 + 崩溃日志」，不能静默退出。"""
    from desktop_ui import app as desktop_app

    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(desktop_app, "_fatal_dialog", lambda title, message: dialogs.append((title, message)))

    def boom(argv=None, *, port=None):
        raise RuntimeError("模拟桌面端崩溃")

    monkeypatch.setattr(desktop_app, "_run", boom)
    assert desktop_app.main([]) == 1

    assert dialogs, "必须弹出错误对话框"
    title, message = dialogs[0]
    assert "错误" in title
    assert "模拟桌面端崩溃" in message
    assert "崩溃日志" in message

    crash_dir = temp_home / "logs" / "crash"
    assert crash_dir.exists() and list(crash_dir.glob("crash-*.log")), "必须写出崩溃日志"


def test_desktop_main_passes_through_system_exit(qt_app, monkeypatch):
    from desktop_ui import app as desktop_app

    def exit_now(argv=None, *, port=None):
        raise SystemExit(0)

    monkeypatch.setattr(desktop_app, "_run", exit_now)
    with pytest.raises(SystemExit):
        desktop_app.main([])


def test_desktop_helpers_resolve_paths(app_paths):
    from desktop_ui.app import _crash_dir, _env_file, _port_from

    assert _port_from("http://127.0.0.1:8123") == 8123
    assert _port_from("https://example.com") == 443
    assert _port_from("http://example.com:9999/x") == 9999
    assert _port_from("这不是地址") is None or isinstance(_port_from("这不是地址"), int)
    assert str(app_paths.env_file) == _env_file()
    assert _crash_dir().endswith("crash")


def test_api_client_base_url_is_mutable_for_port_fallback():
    """端口被占用时会把 api.base_url 换成新端口，必须真的生效。"""
    from desktop_ui.api_client import ApiClient

    client = ApiClient("http://127.0.0.1:8008")
    client.base_url = "http://127.0.0.1:8010"
    assert client.ws_url("/api/logs").startswith("ws://127.0.0.1:8010/api/logs")


# --------------------------------------------------------------------------- #
# 扫码登录（AC-08）
# --------------------------------------------------------------------------- #
def test_api_client_qr_endpoints(web_backend):
    from desktop_ui.api_client import ApiClient

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")

    started = client.start_qr_login()
    assert started["uuid"] and started["image"]
    assert started["state"] == "waiting"

    status = client.qr_login_status(started["uuid"])
    assert status["state"] in ("waiting", "scanned", "confirmed", "expired", "failed")

    assert client.cancel_qr_login(started["uuid"])["ok"] is True


def test_qr_dialog_shows_image_and_succeeds(qt_app, web_backend, app_context, mock_server):
    """桌面扫码对话框：能拿到二维码 → 轮询到确认 → 发出成功信号。"""
    from desktop_ui.api_client import ApiClient
    from desktop_ui.qr_dialog import QrLoginDialog

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")

    dialog = QrLoginDialog(client)
    received: list[str] = []
    dialog.succeeded.connect(received.append)

    dialog.start()
    assert dialog.uuid, "应拿到 uuid"
    assert not dialog.qr_label.pixmap().isNull(), "应把二维码画到标签上"

    # 直接驱动 poll（不依赖定时器），让测试确定性更强
    for _ in range(10):
        dialog.poll()
        if received:
            break
    assert received, "轮询到确认后应发出 succeeded 信号"
    assert dialog._poll_timer.isActive() is False  # noqa: SLF001 - 终态必须停掉定时器

    # 账号应已在后台创建
    users = client.list_users()
    assert any(u["username"] == received[0] for u in users)
    dialog.close()


def test_qr_dialog_stops_polling_on_expired(qt_app, web_backend, mock_server):
    from desktop_ui.api_client import ApiClient
    from desktop_ui.qr_dialog import QrLoginDialog

    mock_server.set_scenario("qr_expired")
    client = ApiClient(web_backend)
    client.login("admin", "test-pass")

    dialog = QrLoginDialog(client)
    dialog.start()
    # 默认开启自动换码：官方二维码只有约 20 秒寿命，过期即换新码，
    # 所以这里应当**继续轮询**并换成一张新码，而不是停在这里报「已失效」。
    dialog.poll()

    assert dialog._poll_timer.isActive() is True, "自动换码期间应继续轮询"  # noqa: SLF001
    assert dialog.image_uuid, "换码后要更新界面上显示的二维码"
    dialog.close()


def test_qr_dialog_stops_polling_when_refresh_is_exhausted(qt_app, web_backend, app_context, mock_server):
    """关掉自动刷新后，过期就是终态：停下轮询并允许手动刷新。"""
    from desktop_ui.api_client import ApiClient
    from desktop_ui.qr_dialog import QrLoginDialog

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")

    dialog = QrLoginDialog(client)
    dialog.start()
    session = app_context.accounts.get_qr_session(dialog.uuid)
    assert session is not None, "应能按会话标识取到会话"
    session.auto_refresh = False

    mock_server.set_scenario("qr_expired")   # 服务端此刻起一直报「已过期」
    dialog.poll()

    assert dialog._poll_timer.isActive() is False  # noqa: SLF001
    assert dialog.btn_refresh.isEnabled(), "过期后应允许刷新二维码"
    dialog.close()


def test_qr_dialog_close_cancels_remote_session(qt_app, web_backend, app_context):
    """关掉弹窗要通知后台取消会话，不能留一个后台等待中的会话。"""
    from desktop_ui.api_client import ApiClient
    from desktop_ui.qr_dialog import QrLoginDialog

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")

    dialog = QrLoginDialog(client)
    dialog.start()
    uuid = dialog.uuid
    assert app_context.accounts.get_qr_session(uuid) is not None

    dialog.close()
    assert app_context.accounts.get_qr_session(uuid).status.state == "expired"


def test_qr_dialog_keeps_polling_on_unknown_state(qt_app, web_backend, mock_server):
    """回归：服务端返回异常信封时必须继续轮询，不能停下并报失败。

    线上真实故障：官方 checkqr 返回 ``{"status": false, "messages": ["操作失败"]}``，
    早期实现把它当终态失败，用户「还没扫码就被告知失败」，扫完也再无任何变化。
    """
    from desktop_ui.api_client import ApiClient
    from desktop_ui.qr_dialog import QrLoginDialog

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")

    dialog = QrLoginDialog(client)
    mock_server.set_scenario("qr_bad_response")
    dialog.start()
    dialog.poll()

    assert dialog._poll_timer.isActive() is True, "异常响应后必须继续轮询"  # noqa: SLF001
    text = dialog.status_label.text()
    assert "重试" in text or "无法获取" in text, text

    # 服务端恢复后应能走到成功，验证「不会因一次异常而永久失效」
    mock_server.set_scenario("has_ticket")
    mock_server.set_qr_state("confirmed")
    received: list[str] = []
    dialog.succeeded.connect(received.append)
    for _ in range(5):
        dialog.poll()
        if received:
            break
    assert received, "恢复正常后应能完成登录"
    dialog.close()


def test_qr_dialog_handles_backend_error(qt_app, web_backend):
    """后台不可用时对话框要显示原因，而不是抛异常崩掉。"""
    from desktop_ui.api_client import ApiClient
    from desktop_ui.qr_dialog import QrLoginDialog

    client = ApiClient("http://127.0.0.1:1", timeout=0.5)
    dialog = QrLoginDialog(client)
    dialog.start()
    assert "失败" in dialog.status_label.text()
    dialog.close()


def test_accounts_page_has_qr_button_and_login_type_column(qt_app, web_backend, app_context):
    """账号页要有扫码入口，并能区分密码/扫码账号。"""
    from desktop_ui.api_client import ApiClient
    from desktop_ui.pages.accounts import AccountsPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")

    page = AccountsPage(client)
    page.timer.stop()
    assert hasattr(page, "btn_qr")
    assert page.btn_qr.text() == "扫码登录"
    assert page.table.columnCount() == 8
    assert page.table.horizontalHeaderItem(3).text() == "登录方式"

    # 扫码账号：登录按钮应被禁用并说明原因
    page._rows = [  # noqa: SLF001
        {"id": 1, "username": "qr_only", "status": "online", "has_password": False,
         "is_checked": False, "heartbeat": 120, "last_active": "", "enabled": True}
    ]
    page._render()  # noqa: SLF001
    assert page.table.item(0, 3).text() == "扫码"
    widget = page.table.cellWidget(0, 7)
    login_btn = _button_by_text(widget, "测试登录")
    assert login_btn is not None, "账号行里没有「测试登录」按钮"
    assert login_btn.isEnabled() is False
    assert "扫码" in login_btn.toolTip()

    # 密码账号：登录按钮可用，登录方式显示为「密码」
    page._rows = [  # noqa: SLF001
        {"id": 2, "username": "pw_user", "status": "offline", "has_password": True,
         "is_checked": False, "heartbeat": 120, "last_active": "", "enabled": True}
    ]
    page._render()  # noqa: SLF001
    assert page.table.item(0, 3).text() == "密码"
    assert _button_by_text(page.table.cellWidget(0, 7), "测试登录").isEnabled() is True
    # 详情入口在位（个人中心：账号信息 / 乘车人 / 我的订单）
    assert _button_by_text(page.table.cellWidget(0, 7), "详情") is not None


def _button_by_text(widget, text: str):
    """按文字找按钮。

    **不要按下标取**：这一行的按钮会随功能增减而移位，
    按下标写的断言会在「加了个按钮」时莫名其妙地红掉，
    而且报出来的是「isEnabled 为 False」这种和真实原因（下标变了）无关的信息。
    """
    from PySide6.QtWidgets import QPushButton

    layout = widget.layout()
    for index in range(layout.count()):
        item = layout.itemAt(index).widget()
        if isinstance(item, QPushButton) and item.text() == text:
            return item
    return None


def test_accounts_page_qr_handler_tolerates_qt_bool(qt_app, web_backend, monkeypatch):
    """Qt 会把 checked(bool) 传进槽函数，处理函数必须把它当成「没传账号名」。

    否则 username_hint 会变成 False，进而在会话里对 bool 调 .strip() 直接崩。
    """
    import desktop_ui.qr_dialog as qr_mod
    from desktop_ui.api_client import ApiClient
    from desktop_ui.pages.accounts import AccountsPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = AccountsPage(client)
    page.timer.stop()

    created: list[object] = []

    class _FakeSignal:
        def connect(self, fn) -> None:  # noqa: ANN001
            pass

    class _FakeDialog:
        def __init__(self, api, parent=None) -> None:  # noqa: ANN001
            self.succeeded = _FakeSignal()
            created.append(self)

        def start(self) -> None:
            pass

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(qr_mod, "QrLoginDialog", _FakeDialog)

    page._open_qr_login(False)        # noqa: SLF001 - 模拟 Qt 传 bool
    page._open_qr_login(None)         # noqa: SLF001 - 模拟传 None
    page._open_qr_login("someone")    # noqa: SLF001 - 正常传账号名
    assert len(created) == 3, "三种调用都不应抛异常，且都要打开对话框"




# --------------------------------------------------------------------------- #
# 实时通道降级（QtWebSockets 不在 PySide6-Essentials 里）
# --------------------------------------------------------------------------- #
class _FakeRealtimeApi:
    """最小 API 替身：只实现轮询降级需要的两个读接口。"""

    token = "test-token"

    def __init__(self, logs=None, tasks=None) -> None:
        self.logs = logs or []
        self.tasks = tasks or []
        self.calls: list[str] = []

    def get(self, path: str, **kwargs):
        self.calls.append(path)
        if "logs" in path:
            return {"logs": list(self.logs)}
        return {"tasks": list(self.tasks)}

    def ws_url(self, path: str) -> str:
        return "ws://127.0.0.1:8008" + path


def _flush_poll(client) -> None:
    """等后台轮询线程跑完一轮，并驱动事件循环把跨线程信号投递出去。

    轮询在子线程里跑，Signal 投递到主线程的槽是**队列式**的：
    不跑事件循环就永远收不到 —— 这一点在测试里很容易被忽略。
    """
    import time as _t

    from PySide6.QtWidgets import QApplication

    for _ in range(60):
        QApplication.processEvents()
        if not client._poll_busy:  # noqa: SLF001
            break
        _t.sleep(0.02)
    _t.sleep(0.05)
    QApplication.processEvents()
    _t.sleep(0.05)
    QApplication.processEvents()


def test_realtime_falls_back_to_polling_without_qtwebsockets(qt_app, monkeypatch):
    """没有 QtWebSockets 时必须降级为轮询，而不是把实时功能整条丢掉。

    ``QtWebSockets`` 属于 ``PySide6-Addons``，只装 ``PySide6-Essentials`` 时不可用。
    修复前的行为是 ``connect_all()`` 直接 return —— 界面上只留一句「未连接」，
    日志与任务状态完全不刷新，使用者无法判断是环境缺件还是后台没起。
    """
    import desktop_ui.realtime as rt

    received: list[dict] = []
    tasks: list[list] = []
    api = _FakeRealtimeApi(logs=[{"ts": "10:00:00", "level": "INFO", "logger": "x", "message": "hello"}])
    monkeypatch.setattr(rt, "HAS_WEBSOCKET", False)

    client = rt.RealtimeClient(api)
    client.log_received.connect(received.append)
    client.task_status_received.connect(tasks.append)
    client.connect_all()
    _flush_poll(client)

    assert client.transport == "polling"
    assert client.connected is True
    assert "未安装" in client.mode_text or "轮询" in client.mode_text
    assert received and received[0]["message"] == "hello", "降级后也要能拿到日志"
    assert tasks, "降级后也要能拿到任务状态"
    client.stop()


def test_realtime_polling_does_not_repeat_logs(qt_app, monkeypatch):
    """轮询会把同一批日志反复拉回来，必须去重，否则日志窗会被刷成重复内容。"""
    import desktop_ui.realtime as rt

    received: list[dict] = []
    api = _FakeRealtimeApi(logs=[{"ts": "10:00:00", "level": "INFO", "logger": "x", "message": "once"}])
    monkeypatch.setattr(rt, "HAS_WEBSOCKET", False)

    client = rt.RealtimeClient(api)
    client.log_received.connect(received.append)
    client.connect_all()
    _flush_poll(client)
    client._poll_logs()          # noqa: SLF001 - 再拉一次同样的内容
    _flush_poll(client)

    assert len(received) == 1, f"同一条日志不应重复推送，实际 {len(received)} 次"
    client.stop()


def test_realtime_reports_offline_when_backend_unreachable(qt_app, monkeypatch):
    """后台不可达时保持 offline，不要谎报已连接。"""
    import desktop_ui.realtime as rt

    class _Broken(_FakeRealtimeApi):
        def get(self, path, **kwargs):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(rt, "HAS_WEBSOCKET", False)
    client = rt.RealtimeClient(_Broken())
    client.connect_all()
    _flush_poll(client)
    assert client.transport == "offline"
    client.stop()


# --------------------------------------------------------------------------- #
# 人证核验：区分「未检测」与「未核验」
# --------------------------------------------------------------------------- #
def test_verified_text_distinguishes_unchecked_from_unknown(qt_app):
    """人证核验四态必须能区分开。

    重点是「未核验」和「无法判定」不能混：前者是**真的会被候补拦住**的状态，
    后者只是「接口没告诉我们」。以前两者都显示「未核验」，
    会让已实名的账号看起来像被 12306 限制了。
    """
    from desktop_ui.pages.accounts import _verified_text

    # 已核验
    assert _verified_text({"is_checked": True, "verified_checked": True, "verify_known": True}) == "已核验"
    # 明确未通过
    assert _verified_text({"is_checked": False, "verified_checked": True, "verify_known": True}) == "未核验"
    # 检测过但接口没给字段 → 如实说无法判定，不要猜
    assert _verified_text({"is_checked": False, "verified_checked": True, "verify_known": False}) == "无法判定"
    # 还没登录成功，没检测过
    assert _verified_text({"is_checked": False, "verified_checked": False, "verify_known": False}) == "未检测"
    assert _verified_text({}) == "未检测"


def test_detect_verified_marks_that_check_was_performed(app_context, mock_server):
    """检测过之后要留下痕迹，界面才能区分「没检测」和「未通过」。"""
    user = app_context.accounts.build_user(UserConfig(username="verify_probe", password="pw"))
    assert user.verified_checked is False
    user.detect_verified()
    assert user.verified_checked is True
    assert "verified_checked" in user.to_dict()
    # mock 的默认场景带明确字段 → 判定得出来
    assert user.verify_known is True


def test_detect_verified_does_not_guess_when_flag_absent(app_context, mock_server):
    """官方没返回核验字段时**不许猜** —— 这是真机踩出来的。

    扫码登录返回的 ``user_name`` 本身就是真实姓名（实测：「樊章」），
    而旧实现用 ``user_name != username`` 判断已核验，于是
    ``樊章 != 樊章`` 为 False → **一个已实名的账号被判成「未核验」**。
    界面显示「未核验」会让人以为账号被 12306 限制了，属于有害的误导。

    正确做法：拿不到字段就如实说「无法判定」（``verify_known=False``）。
    """
    mock_server.set_scenario("user_info_no_flag")
    # 让账号名与返回的姓名一致，复现扫码登录的场景
    user = app_context.accounts.build_user(UserConfig(username="张三", password="pw"))

    assert user.detect_verified() is False
    assert user.verified_checked is True, "检测动作确实执行了"
    assert user.verify_known is False, "没有明确字段时必须标记为「判不出来」"
    assert user.to_dict()["verify_known"] is False


# --------------------------------------------------------------------------- #
# 实时通道降级（QtWebSockets 不在 PySide6-Essentials 里）
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 实时通道：socket 状态判断（曾经把整个客户端带崩）
# --------------------------------------------------------------------------- #
def test_is_connected_state_handles_enum_and_int():
    """状态判断必须同时兼容 Shiboken 枚举与整型。

    PySide6 6.11 起 ``stateChanged`` 发的是 ``QAbstractSocket.SocketState`` 枚举，
    而该枚举**不支持 int() 转换**：``int(st.ConnectedState)`` 会抛 TypeError。
    旧代码正是这么写的 —— 结果是桌面客户端**整个崩溃**（打包版才暴露，
    因为源码环境当时没装 QtWebSockets，这段代码从没执行过）。
    """
    from PySide6.QtNetwork import QAbstractSocket

    from desktop_ui.realtime import is_connected_state

    state = QAbstractSocket.SocketState
    assert is_connected_state(state.ConnectedState) is True
    for name in ("UnconnectedState", "HostLookupState", "ConnectingState", "ClosingState"):
        assert is_connected_state(getattr(state, name)) is False
    # 旧版/整型兜底
    assert is_connected_state(3) is True
    assert is_connected_state(0) is False


def test_realtime_on_socket_state_switches_transport(qt_app):
    """收到「已连接」状态时应切到 websocket 传输并停止轮询。"""
    from PySide6.QtNetwork import QAbstractSocket

    import desktop_ui.realtime as rt

    client = rt.RealtimeClient(_FakeRealtimeApi())
    client._start_polling()                       # noqa: SLF001
    assert client._log_timer.isActive()           # noqa: SLF001

    client._on_socket_state(QAbstractSocket.SocketState.ConnectedState, None)  # noqa: SLF001

    assert client.transport == "websocket"
    assert client.connected is True
    assert "WebSocket" in client.mode_text
    assert not client._log_timer.isActive(), "连上 WebSocket 后应停掉轮询"  # noqa: SLF001
    client.stop()


def test_realtime_on_socket_state_never_raises(qt_app):
    """状态槽**任何情况下都不能抛异常**。

    它是 Qt 信号槽，异常会冒到全局 excepthook —— 实测会把桌面客户端直接带崩。
    实时通道只是「有则更好」的能力，绝不能影响主程序。
    """
    import desktop_ui.realtime as rt

    client = rt.RealtimeClient(_FakeRealtimeApi())
    for junk in (None, object(), "ConnectedState", 99, -1):
        client._on_socket_state(junk, None)  # noqa: SLF001
    client.stop()


class _FakeSocket:
    """只实现 ``state()`` 的最简 socket 替身。"""

    def __init__(self, state) -> None:
        self._state = state

    def state(self):
        return self._state


def test_realtime_transient_connecting_state_does_not_reconnect(qt_app):
    """另一个 socket 还在「连接中」时，不能判定为掉线，也不该启动重连。

    实际故障：``connect_all()`` 依次打开两个 socket，第一个刚进入 ConnectingState 时
    第二个还没创建，若此刻判定掉线并启动重连定时器，等两个都连上后**定时器仍在跑**，
    到点就把好好的连接关掉重开 —— 状态栏表现为 websocket/offline **反复横跳**。
    """
    from PySide6.QtNetwork import QAbstractSocket

    import desktop_ui.realtime as rt

    state = QAbstractSocket.SocketState
    client = rt.RealtimeClient(_FakeRealtimeApi())
    client._log_socket = _FakeSocket(state.ConnectedState)      # noqa: SLF001
    client._event_socket = _FakeSocket(state.ConnectingState)   # noqa: SLF001
    client._set_transport("websocket", connected=True)          # noqa: SLF001

    client._on_socket_state(state.ConnectingState, None)        # noqa: SLF001

    assert client.transport == "websocket", "过渡状态不该被当成掉线"
    assert not client._reconnect_timer.isActive(), "过渡状态不该启动重连"  # noqa: SLF001
    client.stop()


def test_realtime_connect_cancels_pending_reconnect(qt_app):
    """连上之后必须取消待执行的重连 —— 否则会把好连接关掉重开。"""
    from PySide6.QtNetwork import QAbstractSocket

    import desktop_ui.realtime as rt

    client = rt.RealtimeClient(_FakeRealtimeApi())
    client._reconnect_timer.start(30000)                        # noqa: SLF001
    assert client._reconnect_timer.isActive()                   # noqa: SLF001

    client._on_socket_state(QAbstractSocket.SocketState.ConnectedState, None)  # noqa: SLF001

    assert not client._reconnect_timer.isActive(), "连上后应取消待执行的重连"  # noqa: SLF001
    assert client.transport == "websocket"
    client.stop()


# --------------------------------------------------------------------------- #
# 账号详情（个人中心：账号信息 / 乘车人 / 我的订单 / 待支付）
# --------------------------------------------------------------------------- #
class _StubProfileApi:
    """替身：按固定数据回话，不碰网络。"""

    def __init__(self, payload):
        self.payload = payload
        self.order_calls = []

    def user_profile(self, user_id, **kwargs):  # noqa: ANN003
        return self.payload

    def user_orders(self, user_id, **kwargs):  # noqa: ANN003
        self.order_calls.append(kwargs)
        return self.payload.get("orders") or {"ok": False, "error": "未提供", "data": None}


def _detail_payload():
    return {
        "account": {"id": 1, "username": "u1", "online": True},
        "profile": {
            "ok": True,
            "error": "",
            "data": {
                "username": "张三", "real_name": "张三", "user_type": "成人", "sex": "男",
                "mobile": "138****8888", "email": "z*******@qq.com", "id_type": "中国居民身份证",
                "id_no": "110101********1234", "points": "1280", "register_time": "2015-03-08",
                "verified": None,
            },
        },
        "passengers": {
            "ok": True, "error": "",
            "data": [
                {"passenger_name": "张三", "passenger_id_no": "110101********1234",
                 "mobile_no": "138****0000", "is_self": True, "verify_status_name": "已通过"},
                {"passenger_name": "李四", "passenger_id_no": "110101********2345",
                 "mobile_no": "138****0001", "is_self": False, "verify_status_name": "待核验"},
            ],
        },
        "pending": {"ok": True, "error": "", "data": []},
        "orders": {
            "ok": True, "error": "",
            "data": {"orders": [{"order_no": "E1", "train_code": "G6102", "train_date": "2026-10-01",
                                 "from_station": "广州南", "to_station": "长沙南", "seat_type": "二等座",
                                 "passenger_names": "张三", "price": "314.00", "status": "已支付",
                                 "passengers": [{"coach_no": "05", "seat_no": "12A"}]}],
                     "page_index": 1, "page_count": 1, "where": "G"},
        },
    }


def _wait_dialog(dialog, timeout: float = 3.0) -> None:
    """等一下后台取数线程把结果投递回主线程。"""
    import time

    from PySide6.QtWidgets import QApplication

    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if dialog.panel.profile_status.text() != "正在拉取…":
            QApplication.processEvents()
            return
        time.sleep(0.02)
    QApplication.processEvents()


def test_account_detail_dialog_renders_sections(qt_app):
    from desktop_ui.account_detail import AccountDetailDialog

    api = _StubProfileApi(_detail_payload())
    dialog = AccountDetailDialog(api, {"id": 1, "username": "u1", "status": "online"})
    _wait_dialog(dialog)

    assert dialog.panel.profile_values["username"].text() == "张三"
    assert dialog.panel.profile_values["mobile"].text() == "138****8888"
    # 接口没给明确核验字段 → 必须显示「无法判定」，不能显示「未核验」
    assert "无法判定" in dialog.panel.profile_values["verified"].text()

    assert dialog.panel.pass_table.rowCount() == 2
    assert dialog.panel.pass_table.item(0, 0).text() == "张三"
    assert dialog.panel.pass_table.item(0, 6).text() == "是"        # 本人

    assert dialog.panel.orders_table.rowCount() == 1
    assert dialog.panel.orders_table.item(0, 2).text() == "G6102"
    assert "第 1/1 页" in dialog.panel.orders_status.text()
    assert dialog.panel.pending_status.text() == "待支付订单 0 个"
    dialog.close()


def test_account_detail_dialog_shows_banner_when_offline(qt_app):
    """离线时窗口照开，横幅说明原因；在线时不显示横幅。"""
    from desktop_ui.account_detail import AccountDetailDialog

    payload = _detail_payload()
    payload["profile"] = {"ok": False, "error": "账号未登录，请先登录后再拉取账号信息", "data": None}
    api = _StubProfileApi(payload)

    dialog = AccountDetailDialog(api, {"id": 1, "username": "u1", "status": "offline", "online": False})
    _wait_dialog(dialog)
    assert dialog.panel.banner.text(), "未登录时必须给出说明"
    assert "未登录" in dialog.panel.banner.text()
    assert "扫码登录" in dialog.panel.btn_relogin.text()
    # 单块失败信息要如实落到对应页签上，而不是静默留空
    assert "未登录" in dialog.panel.profile_status.text()
    dialog.close()

    dialog2 = AccountDetailDialog(api, {"id": 1, "username": "u1", "status": "online", "online": True})
    _wait_dialog(dialog2)
    assert not dialog2.panel.banner.isVisible(), "在线时不该显示未登录横幅"
    dialog2.close()


def test_account_detail_dialog_marks_online_after_relogin(qt_app):
    """扫码成功后窗口要切回「已在线」并重新取数，否则界面仍显示未登录。"""
    from desktop_ui.account_detail import AccountDetailDialog

    api = _StubProfileApi(_detail_payload())
    dialog = AccountDetailDialog(api, {"id": 1, "username": "u1", "status": "offline", "online": False})
    _wait_dialog(dialog)
    assert dialog.panel.banner.text()

    dialog.panel.on_relogin_ok("u1")        # noqa: SLF001
    assert dialog.panel.user["online"] is True
    assert dialog.panel.banner.text() == ""
    dialog.close()


def test_account_detail_dialog_keeps_other_tabs_when_one_section_fails(qt_app):
    """一块失败只能影响它自己。"""
    from desktop_ui.account_detail import AccountDetailDialog

    payload = _detail_payload()
    payload["orders"] = {"ok": False, "error": "订单接口被风控", "data": None}
    api = _StubProfileApi(payload)
    dialog = AccountDetailDialog(api, {"id": 1, "username": "u1", "status": "online"})
    _wait_dialog(dialog)
    import time

    from PySide6.QtWidgets import QApplication

    for _ in range(50):
        QApplication.processEvents()
        if "风控" in dialog.panel.orders_status.text():
            break
        time.sleep(0.02)

    assert dialog.panel.profile_values["username"].text() == "张三"      # 账号信息照常显示
    assert dialog.panel.pass_table.rowCount() == 2                       # 乘车人照常显示
    assert "风控" in dialog.panel.orders_status.text()                   # 只有订单这块报错
    dialog.close()


def test_accounts_page_detail_navigates_to_profile_page(qt_app, web_backend, monkeypatch):
    """有主窗口时，「详情」应**切到「个人中心」页面**并选中该账号。

    这比再弹一个窗口好：把人带到功能真正所在的地方，下次他自己就找得到了。
    """
    from desktop_ui.api_client import ApiClient
    from desktop_ui.pages.accounts import AccountsPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = AccountsPage(client)
    page.timer.stop()
    monkeypatch.setattr(page, "refresh", lambda: None)

    calls: list[dict] = []

    class _FakeWindow:
        def show_profile(self, user):
            calls.append(user)

    # QWidget.window() 返回顶层窗口，这里替换成假窗口即可
    monkeypatch.setattr(page, "window", lambda: _FakeWindow())

    page._open_detail({"id": 7, "username": "u1", "online": False, "status": "offline"})   # noqa: SLF001
    assert calls and calls[0]["id"] == 7, "未登录时也要跳到个人中心（由页面内的横幅说明原因）"

    page._open_detail({"id": 8, "username": "u2", "online": True, "status": "online"})     # noqa: SLF001
    assert calls[-1]["id"] == 8


def test_accounts_page_detail_falls_back_to_dialog_without_main_window(qt_app, web_backend, monkeypatch):
    """没有主窗口（例如单独构造本页做测试）时退化为弹窗，两种场景都要可用。"""
    from desktop_ui.api_client import ApiClient
    from desktop_ui.pages.accounts import AccountsPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = AccountsPage(client)
    page.timer.stop()
    monkeypatch.setattr(page, "refresh", lambda: None)

    opened: list[object] = []
    monkeypatch.setattr(
        "desktop_ui.account_detail.AccountDetailDialog.exec",
        lambda self: opened.append(self),
    )

    page._open_detail({"id": 1, "username": "u1", "online": False, "status": "offline"})   # noqa: SLF001
    assert len(opened) == 1, "未登录时也必须打开详情窗口"
    dialog = opened[0]
    assert dialog.panel.banner.text(), "未登录时应显示提示横幅"
    assert "未登录" in dialog.panel.banner.text()
    assert "扫码登录" in dialog.panel.btn_relogin.text(), "应提供就地扫码入口"
    dialog.close()


def _stub_profile_api(users):
    class _Api:
        def list_users(self):
            return users

        def user_profile(self, user_id, **kwargs):  # noqa: ANN003
            return _detail_payload()

        def user_orders(self, user_id, **kwargs):  # noqa: ANN003
            return _detail_payload()["orders"]

    return _Api()


def test_profile_page_lists_accounts_and_selects_online_one(qt_app):
    """个人中心页面：账号下拉要列全，并默认选在线的那个。"""
    from desktop_ui.pages.profile import ProfilePage

    users = [
        {"id": 1, "username": "离线号", "online": False, "status": "offline", "session_saved": False},
        {"id": 2, "username": "在线号", "online": True, "status": "online", "session_saved": True},
    ]
    page = ProfilePage(_stub_profile_api(users))
    assert page.account_box.count() == 2
    assert page.account_box.currentData()["id"] == 2, "应默认选在线账号"
    assert "需扫码" in page.account_box.itemText(0), "未保存会话的账号要标出来"
    assert page.panel.user_id == 2
    page.panel.btn_reload.setEnabled(False)   # 停掉后台线程可能的后续回调
    page._retry_timer.stop()                  # noqa: SLF001
    page.deleteLater()


def test_profile_page_handles_no_accounts(qt_app):
    """一个账号都没有时给出明确指引，而不是空白页。"""
    from desktop_ui.pages.profile import ProfilePage

    page = ProfilePage(_stub_profile_api([]))
    assert page.account_box.count() == 0
    assert "添加" in page.status_label.text() or "账号" in page.status_label.text()
    assert page.panel.profile_status.text(), "应说明当前没有可查看的账号"
    page._retry_timer.stop()                  # noqa: SLF001
    page.deleteLater()


def test_profile_page_show_account_selects_given_user(qt_app):
    from desktop_ui.pages.profile import ProfilePage

    users = [
        {"id": 1, "username": "甲", "online": True, "status": "online", "session_saved": True},
        {"id": 2, "username": "乙", "online": False, "status": "offline", "session_saved": False},
    ]
    page = ProfilePage(_stub_profile_api(users))
    page.show_account({"id": 2, "username": "乙"})
    assert page.account_box.currentData()["id"] == 2
    assert page.panel.username == "乙"
    page._retry_timer.stop()                  # noqa: SLF001
    page.deleteLater()


def test_profile_panel_shows_session_saved_state(qt_app):
    """「免扫码登录」要如实反映会话有没有存下来（使用者据此判断靠不靠得住）。"""
    from desktop_ui.profile_panel import ProfilePanel

    api = _stub_profile_api([])
    panel = ProfilePanel(api, embedded=True)
    panel.set_user({"id": 1, "username": "u1", "online": True, "session_saved": False}, autoload=False)
    assert "未保存" in panel.profile_values["session_saved"].text()

    panel.set_user({"id": 1, "username": "u1", "online": True, "session_saved": True}, autoload=False)
    assert "已保存" in panel.profile_values["session_saved"].text()
    panel.deleteLater()


# --------------------------------------------------------------------------- #
# UI-T5 任务配置：从 12306 拉取车次（白名单 / 黑名单不手敲）
# --------------------------------------------------------------------------- #
class _StubTrainApi:
    """替身：只实现 train_options，用来把「拉取 → 勾选 → 写回表单」跑通。"""

    def __init__(self, payload: dict | None = None, error: str = "") -> None:
        self.payload = payload or _train_options_payload()
        self.error = error
        self.calls: list[dict] = []

    def train_options(self, **kwargs):
        from desktop_ui.api_client import ApiError

        self.calls.append(kwargs)
        if self.error:
            raise ApiError(self.error)
        return self.payload


def _train_options_payload() -> dict:
    def train(code, start, arrive, duration, remain, seats, dates=None):
        return {
            "train_code": code,
            "train_type": "高铁" if code.startswith("G") else "快速",
            "start_time": start,
            "arrive_time": arrive,
            "duration": duration,
            "remain_text": remain,
            "available_seats": seats,
            "dates": dates or ["2026-10-01", "2026-10-02"],
        }

    return {
        "account": "u1",
        "combos": 2,
        "queried": 2,
        "truncated": False,
        "risk_limited": False,
        "errors": [],
        "cached": False,
        "trains": [
            train("G6102", "08:12", "10:47", "02:35", "二等座 5 · 一等座 2", ["二等座", "一等座"]),
            train("K512", "06:40", "14:20", "07:40", "硬座 20", ["硬座"]),
            train("G6140", "14:05", "16:52", "02:47", "一等座 3", ["一等座"]),
        ],
    }


def _wait_picker(dialog, timeout: float = 5.0) -> None:
    """等后台线程把拉取结果投递回主线程。"""
    import time

    from PySide6.QtWidgets import QApplication

    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if dialog.table.rowCount() > 0 or not dialog._busy:  # noqa: SLF001
            QApplication.processEvents()
            return
        time.sleep(0.02)
    QApplication.processEvents()


def test_train_picker_lists_trains_and_returns_selection(qt_app):
    """勾选后返回的车次按表格顺序（= 发车时间），可以直接当白名单优先级。"""
    from desktop_ui.train_picker import TrainPickerDialog

    dialog = TrainPickerDialog(
        _StubTrainApi(), dates="2026-10-01,2026-10-02", from_stations="广州", to_stations="长沙"
    )
    _wait_picker(dialog)

    assert dialog.table.rowCount() == 3
    assert dialog.table.item(0, 1).text() == "G6102"
    assert "2/2" in dialog.status_label.text(), dialog.status_label.text()

    dialog._toggle_row(2)  # noqa: SLF001 - 勾最后一班
    dialog._toggle_row(0)  # noqa: SLF001
    assert dialog.selected_codes() == ["G6102", "G6140"], "顺序应跟表格一致，而不是点击顺序"
    dialog._toggle_row(2)  # noqa: SLF001 - 再点一次取消
    assert dialog.selected_codes() == ["G6102"]

    dialog.radio_black.setChecked(True)
    assert dialog.target() == "black"
    dialog.close()


def test_train_picker_reports_failure_without_crashing(qt_app):
    from desktop_ui.train_picker import TrainPickerDialog

    dialog = TrainPickerDialog(
        _StubTrainApi(error="没有在线账号，无法从 12306 拉取车次"),
        dates="2026-10-01",
        from_stations="广州",
        to_stations="长沙",
    )
    _wait_picker(dialog)

    assert "没有在线账号" in dialog.status_label.text()
    assert dialog.btn_apply.isEnabled() is False, "拉不到车次时不该允许「应用」"
    dialog.close()


def test_task_page_merges_picked_trains_into_whitelist(qt_app, web_backend):
    """应用勾选时：保留已有顺序（优先级），新勾选的追加在后面。"""
    from desktop_ui.api_client import ApiClient
    from desktop_ui.pages.tasks import TaskConfigPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = TaskConfigPage(client)
    page.timer.stop()

    page.input_white.setText("G6102")
    page._merge_trains(["K512", "G6102", "G6140"], "white")  # noqa: SLF001
    assert page.input_white.text() == "G6102,K512,G6140", "已有顺序不能被勾选顺序打乱"
    assert "已加入白名单" in page.status_label.text()
    page.deleteLater()


def test_task_page_merge_moves_conflicting_train_out_of_other_list(qt_app, web_backend):
    """同一车次不能同时在两个名单里 —— 保存时会被后端判配置非法，必须就地移出并说明。"""
    from desktop_ui.api_client import ApiClient
    from desktop_ui.pages.tasks import TaskConfigPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = TaskConfigPage(client)
    page.timer.stop()

    page.input_white.setText("G6102,K512")
    page._merge_trains(["K512"], "black")  # noqa: SLF001
    assert page.input_black.text() == "K512"
    assert page.input_white.text() == "G6102", "冲突车次必须从白名单移出"
    assert "已从白名单移出" in page.status_label.text()
    page.deleteLater()


def test_api_client_train_options_end_to_end(qt_app, web_backend, app_context, mock_server, monkeypatch):
    """桌面端 → 后台 → 12306（mock）整条链路：直连本地后台也能拉到车次表。"""
    from desktop_ui.api_client import ApiClient
    from ticket_helper.inter.api import TicketApi
    from ticket_helper.inter.base import HttpClient

    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)

    monkeypatch.setattr(
        app_context, "pull_session", lambda username="", force_anonymous=False: (api, "u1")
    )

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    payload = client.train_options(
        dates="2026-10-01,2026-10-02", from_stations="广州", to_stations="长沙", refresh=True
    )
    assert [t["train_code"] for t in payload["trains"]] == ["K512", "G6102", "G6140"]
    assert payload["account"] == "u1"
    assert payload["anonymous"] is False
    assert payload["errors"] == []


# --------------------------------------------------------------------------- #
# UI-T6 任务配置：乘车日期用日期控件、出发/到达站从 12306 站点表选择
# --------------------------------------------------------------------------- #
class _StubStationApi:
    """替身：只实现站点检索，用来把「搜索 → 点选 → 写回表单」跑通。"""

    HOT = [{"name": "北京南", "code": "VNP"}, {"name": "上海虹桥", "code": "AOH"}]
    MATCH = [{"name": "广州", "code": "GZQ"}, {"name": "广州南", "code": "IZQ"}]

    def __init__(self) -> None:
        self.keywords: list[str] = []

    def station_items(self, keyword: str = "", *, limit: int = 60) -> list[dict]:
        self.keywords.append(keyword)
        return list(self.MATCH if keyword else self.HOT)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """等后台线程把结果投递回主线程（顺带跑一次事件循环）。"""
    import time

    from PySide6.QtWidgets import QApplication

    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if predicate():
            QApplication.processEvents()
            return True
        time.sleep(0.02)
    QApplication.processEvents()
    return predicate()


def test_chip_field_keeps_qlineedit_contract(qt_app):
    """对外仍是 text()/setText()/clear()，所以 _payload() 那些代码一行都不用改。"""
    from desktop_ui.token_field import ChipField

    field = ChipField()
    field.setText("广州, 广州南，长沙")          # 中英文逗号都要认
    assert field.text() == "广州,广州南,长沙"
    assert field.list.count() == 3

    assert field.add("广州") is False            # 重复的不能加进去
    assert field.add("武汉") is True
    assert field.text() == "广州,广州南,长沙,武汉"

    field.list.setCurrentRow(0)
    field.remove_selected()
    assert field.text() == "广州南,长沙,武汉"

    field.clear()
    assert field.text() == ""
    assert field.list.count() == 1, "空列表要有占位行，否则使用者不知道这里可以点"
    field.deleteLater()


def test_date_field_takes_today_from_the_single_source(qt_app, frozen_clock):
    """★「今天」在桌面端也只许有**一个**来源（``train_date.today()``）。

    防的是：控件里再写一次 ``QDate.currentDate()``（Qt 的真实今天）。
    那样冻结时钟冻不住控件，控件判「已过去」的口径与保存时的
    ``TaskConfig.validate()`` 分叉，用例还会随**真实日期**推进而以
    「日期已经过去了」这种与用例本意无关的方式变红。
    """

    from datetime import timedelta

    from PySide6.QtCore import QDate

    from desktop_ui.token_field import DateField

    field = DateField()
    expected = QDate(frozen_clock.year, frozen_clock.month, frozen_clock.day)
    actual = field.picker.minimumDate()
    assert actual == expected, (
        "日历下限必须来自 train_date.today()，不能是 Qt 的真实今天：期望 "
        f"{expected.toString('yyyy-MM-dd')}，实际 {actual.toString('yyyy-MM-dd')}"
    )
    tomorrow = (frozen_clock + timedelta(days=1)).isoformat()
    assert field.add(tomorrow) is True, "冻结日的次日必须能加进任务配置"
    field.deleteLater()


def test_date_field_takes_dates_from_calendar(qt_app, frozen_clock):
    """日期由日历控件给出；**点选即加入**，不用再点「添加日期」。

    真机来源（回流 #65，2026-10-03）：用户在日历里选好日期就直接去点
    「从 12306 拉取车次」，以为选过就算数 —— 结果日期根本没进列表，
    被「还缺：乘车日期」挡住，后端一次请求都没收到。
    """
    from datetime import timedelta

    from PySide6.QtCore import QDate

    from desktop_ui.token_field import DateField

    field = DateField()
    assert field.picker.calendarPopup() is True, "必须能点开日历，否则还是「敲日期」"

    def qdate_of(offset: int) -> QDate:
        day = frozen_clock + timedelta(days=offset)
        return QDate(day.year, day.month, day.day)

    day1, day2, day3, day4 = (qdate_of(i) for i in range(1, 5))

    # 日历里点选（dateChanged）→ 立即入列表
    field.picker.setDate(day1)
    assert field.text() == day1.toString("yyyy-MM-dd"), (
        "日历里点选日期必须立即加入，不能要求再点一次「添加日期」"
    )

    # 再「点」同一天：静默 —— 不重复加入，也不发「已经在列表里了」的打扰提示
    reasons: list[str] = []
    field.rejected.connect(reasons.append)
    field.picker.setDate(day1)
    assert field.text() == day1.toString("yyyy-MM-dd")
    assert reasons == [], f"重复点选同一天不该打扰人：{reasons}"

    # 点下一天 → 追加
    field.picker.setDate(day2)
    assert field.text() == f"{day1.toString('yyyy-MM-dd')},{day2.toString('yyyy-MM-dd')}"

    # 「连加 3 天」仍然工作，从当前所选日期起，跳过已有的
    assert field.add_three_days() == 2, "day2 已在列表里，实际只新增 day3 / day4"
    assert field.text() == ",".join(
        d.toString("yyyy-MM-dd") for d in (day1, day2, day3, day4)
    )
    field.deleteLater()


def test_station_field_adds_picked_stations(qt_app):
    from desktop_ui.token_field import StationField

    field = StationField(_StubStationApi())
    field.setText("广州")
    assert field.add_many(["广州", "广州南", "长沙"]) == 2, "已在列表里的不重复计数"
    assert field.text() == "广州,广州南,长沙"
    assert field.btn_search.text() == "搜索站点…"
    field.deleteLater()


def test_station_picker_searches_and_returns_selection(qt_app):
    """打开就是常用站（不用先猜关键字），搜索后双击即加入，重复点不重复加。

    ⚠️ 等搜索结果时**不能只等「数量变成 N」**：替身的常用站恰好也是 2 项，
    这个条件会被**上一屏的旧列表**满足，于是测试在结果还没回来时就往下走，
    拿到的是常用站 → 偶发失败（本机跑全量时真出过）。要等**内容**。
    """
    from desktop_ui.station_picker import StationPickerDialog

    api = _StubStationApi()
    dialog = StationPickerDialog(api, selected=["武汉"])
    assert _wait_until(lambda: dialog.candidates.count() > 0), dialog.status_label.text()
    assert "常用站" in dialog.status_label.text()
    assert dialog.selected_stations() == ["武汉"], "传进来的已选站要预填"

    dialog.input.setText("广州")
    dialog.search()
    assert _wait_until(
        lambda: dialog.candidates.count() == 2 and dialog.candidates.item(0).text().startswith("广州")
    ), dialog.status_label.text()
    assert "广州" in api.keywords

    dialog._add_chosen(dialog.candidates.item(0).text())   # noqa: SLF001
    dialog._add_chosen(dialog.candidates.item(1).text())   # noqa: SLF001
    assert dialog.selected_stations() == ["武汉", "广州", "广州南"]

    dialog._add_chosen(dialog.candidates.item(0).text())   # noqa: SLF001 - 再点一次
    assert dialog.selected_stations().count("广州") == 1

    dialog._drop_chosen("武汉")                            # noqa: SLF001
    assert dialog.selected_stations() == ["广州", "广州南"]
    dialog.close()


def test_station_picker_requires_a_station_before_confirm(qt_app):
    """没选站就点确定不该静默关闭（否则回到表单还是空的，人不知道为什么）。"""
    from desktop_ui.station_picker import StationPickerDialog

    dialog = StationPickerDialog(_StubStationApi())
    assert _wait_until(lambda: dialog.candidates.count() > 0)
    dialog._confirm()  # noqa: SLF001
    assert dialog.result() != StationPickerDialog.DialogCode.Accepted
    assert "还没有选择站点" in dialog.status_label.text()
    dialog.close()


def test_task_page_uses_date_and_station_fields(qt_app, web_backend):
    """三个字段换成「选」的控件后，_payload()/回显/清空的契约必须不变。"""
    from desktop_ui.api_client import ApiClient
    from desktop_ui.pages.tasks import TaskConfigPage
    from desktop_ui.token_field import DateField, StationField

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = TaskConfigPage(client)
    page.timer.stop()

    assert isinstance(page.input_dates, DateField)
    assert isinstance(page.input_from, StationField)
    assert isinstance(page.input_to, StationField)

    page.input_dates.setText("2026-10-01,2026-10-02")
    page.input_from.setText("广州")
    page.input_to.setText("长沙,长沙南")
    payload = page._payload()  # noqa: SLF001
    assert payload["left_dates"] == "2026-10-01,2026-10-02"
    assert payload["from_stations"] == "广州"
    assert payload["to_stations"] == "长沙,长沙南"

    page.reset_form()
    assert page.input_dates.text() == ""
    assert page.input_from.text() == ""
    assert page.input_from.list.count() == 1, "清空后应显示占位行"
    page.deleteLater()


# --------------------------------------------------------------------------- #
# UI-T5 席别优先级：顺序必须表达得出来
# --------------------------------------------------------------------------- #
def test_priority_list_expresses_order(qt_app):
    """序就是优先级，控件必须让它看得见、改得动。

    以前的实现是十个 QCheckBox，标题写着「从下往上勾选，勾选顺序即优先级」——
    这句话是错的：勾选框跟勾选先后毫无关系，取值时永远按布局顺序扫一遍，
    所以「把二等座排在硬座前面」这件事在界面上根本做不到。
    """
    from desktop_ui.priority_field import PriorityList

    field = PriorityList(["商务座", "一等座", "二等座", "硬座"], default=["二等座"])
    assert field.text() == "二等座"

    # 双击可选池 → 加进「会抢的席别」，追加在最后
    field.add("硬座")
    assert field.text() == "二等座,硬座"

    # 选中第二行往上挪一位 → 优先级真的换了
    field.list_picked.setCurrentRow(1)
    field.move_selected(-1)
    assert field.text() == "硬座,二等座", "↑ 必须真的改变抢票优先级顺序"

    # 边界：第一行不能再往上、最后一行不能再往下
    field.list_picked.setCurrentRow(0)
    field.move_selected(-1)
    assert field.text() == "硬座,二等座"
    field.list_picked.setCurrentRow(1)
    field.move_selected(1)
    assert field.text() == "硬座,二等座"

    # 移出
    field.list_picked.setCurrentRow(0)
    field.remove_selected()
    assert field.text() == "二等座"

    # 不在候选里的值要被忽略（防止脏数据把后端校验搞崩）
    field.setText("二等座,不存在的席别")
    assert field.text() == "二等座"
    field.deleteLater()


def test_task_page_keeps_seat_priority_order(qt_app, web_backend):
    """表单里调过顺序的席别，保存时顺序必须原样送到后端。"""
    from desktop_ui.pages.tasks import TaskConfigPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = TaskConfigPage(client)
    page.timer.stop()

    page.seats.setText("硬座,二等座,一等座")
    payload = page._payload()  # noqa: SLF001
    assert payload["seat_types"] == ["硬座", "二等座", "一等座"], payload["seat_types"]

    page.input_name.setText("席别顺序任务")
    page.input_dates.setText("2026-10-01")
    page.input_from.setText("广州南")
    page.input_to.setText("长沙南")
    page._save()  # noqa: SLF001
    saved = next((t for t in client.list_tasks() if t["name"] == "席别顺序任务"), None)
    assert saved is not None and saved["seat_types"][0] == "硬座"
    client.delete_task(int(saved["id"]))
    page.deleteLater()


# --------------------------------------------------------------------------- #
# UI-T6 表单分组：高级项默认收起，缺项要指名道姓
# --------------------------------------------------------------------------- #
def test_task_page_advanced_group_collapsed_until_editing(qt_app, web_backend):
    from desktop_ui.pages.tasks import TaskConfigPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = TaskConfigPage(client)
    page.timer.stop()

    assert page.adv_body.isVisibleTo(page) is False, "运行参数默认应收起，否则一屏看不完"
    page.btn_adv.setChecked(True)
    assert page.adv_body.isVisibleTo(page) is True

    # 编辑已有任务时自动摊开：这一组里很可能有他上次特意改过的值
    page._load_into_form({"id": 999, "name": "x", "seat_types": ["二等座"]})  # noqa: SLF001
    assert page.adv_body.isVisibleTo(page) is True
    page.deleteLater()


def test_task_page_validation_names_the_missing_field(qt_app, web_backend):
    """校验失败要说清楚缺哪一项，而不是笼统一句「请至少填写…」。"""
    from desktop_ui.pages.tasks import TaskConfigPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = TaskConfigPage(client)
    page.timer.stop()
    page.reset_form()

    page.input_name.setText("缺出发站的任务")
    page.input_dates.setText("2026-10-01")
    page.input_to.setText("长沙南")
    page._save()  # noqa: SLF001
    assert "出发站" in page.status_label.text(), page.status_label.text()
    assert page.editing_id is None, "校验没过就不该建任务"

    # 席别被清空时也要拦住（后端会判配置非法）
    page.seats.clear()
    page.input_from.setText("广州南")
    page._save()  # noqa: SLF001
    assert "席别" in page.status_label.text(), page.status_label.text()
    page.deleteLater()


def test_task_page_train_types_are_checkboxes(qt_app, web_backend):
    """车次类型不再是一个 96px 高、只看得见四项的滚动列表。"""
    from desktop_ui.pages.tasks import TRAIN_TYPES, TaskConfigPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = TaskConfigPage(client)
    page.timer.stop()

    assert [b.text() for b in page.train_type_boxes] == TRAIN_TYPES
    page.train_type_boxes[0].setChecked(True)
    page.train_type_boxes[3].setChecked(True)
    assert page._payload()["train_types"] == [TRAIN_TYPES[0], TRAIN_TYPES[3]]  # noqa: SLF001
    page.deleteLater()


# --------------------------------------------------------------------------- #
# UI-T7 订单记录页：抢到票之后要有地方看订单号、去付款
# --------------------------------------------------------------------------- #
def test_orders_page_shows_pay_entry(qt_app, web_backend):
    from desktop_ui.pages.orders import OFFICIAL_ORDERS_URL, OrdersPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    page = OrdersPage(client)
    page.refresh()   # 空列表也不能崩
    assert page.table.columnCount() == 8
    assert "12306" in page.btn_official.text()
    assert OFFICIAL_ORDERS_URL.startswith("https://kyfw.12306.cn/")
    page.deleteLater()


# --------------------------------------------------------------------------- #
# UI-T8 通知设置：渠道卡片不许挤在一起
# --------------------------------------------------------------------------- #
class _StubNotifyApi:
    """通知页专用替身：够渲染即可，不发任何请求。"""

    def __init__(self) -> None:
        self.saved: dict = {}
        self.tested: list[tuple] = []
        self.config = {
            "notify": {
                "email": {"enabled": False, "smtp_host": "", "smtp_port": 465, "use_ssl": True,
                          "username": "", "receivers": []},
                "serverchan": {"enabled": False, "key": ""},
                "dingtalk": {"enabled": False, "webhook": ""},
                "wecom": {"enabled": False, "webhook": ""},
                "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
                "voice": {"enabled": False, "appcode": "", "phone": ""},
                "web": True,
            },
            "ratelimit": {"adaptive": True, "min_interval": 1.0, "max_interval": 8.0, "proxy_pool": ""},
            "codec": {"mode": "free", "local_model_path": "", "retry_limit": 3, "fallback_enabled": True},
        }

    def get_config(self):
        return {"config": self.config}

    def update_config(self, section, payload):
        self.config[section] = payload
        self.saved[section] = payload
        return {"ok": True}

    def notifications(self, limit=100):
        return {"notifications": [], "channels": {}}

    def system_snapshot(self):
        return {"running": True, "home": "/tmp", "api_base": "http://127.0.0.1:8008",
                "uptime_seconds": 120, "config_version": 1, "rate_limit": {}, "codec": {}, "cluster": {}}

    def test_notify(self, channel=None, config=None):
        self.tested.append((channel, config))
        return {"ok": True, "channel": channel}


ALL_CHANNELS = ("email", "serverchan", "dingtalk", "wecom", "telegram", "voice")


def test_notify_page_uses_one_card_per_channel(qt_app):
    """一个渠道一张卡：开关、参数、测试三样各就各位。

    改造前是三个大 GroupBox 里塞 19 行表单，「启用 Server酱」「钉钉 Webhook」
    「企业微信 Webhook」全在同一列里，既难扫开了哪几个，也看不出哪个开了却没填参数。
    """
    from desktop_ui.pages.notify import NotifyPage

    api = _StubNotifyApi()
    page = NotifyPage(api)  # type: ignore[arg-type]
    page.timer.stop()

    assert set(page.cards) == set(ALL_CHANNELS)
    for name, card in page.cards.items():
        assert card.btn_test.text() == "测试", f"{name} 卡片应自带测试按钮"
        assert card.grid.count() > 0, f"{name} 卡片应是「标签 + 控件」分格的，不是一列长表单"

    # 未启用 → 参数区置灰，一眼看出「这些现在不用填」
    assert page.cards["email"].body.isEnabledTo(page.cards["email"]) is False
    page.cards["email"].set_enabled(True)
    assert page.cards["email"].body.isEnabledTo(page.cards["email"]) is True

    # 开关状态要真的写进保存的 payload
    page.input_dingtalk.setText("https://example.com/hook")
    page.cards["dingtalk"].set_enabled(True)
    page._save_channels()  # noqa: SLF001
    assert api.saved["notify"]["dingtalk"] == {"enabled": True, "webhook": "https://example.com/hook"}
    page.deleteLater()


def test_notify_page_timer_does_not_overwrite_typing(qt_app):
    """定时刷新只能刷记录表，**不能把正在输入的内容回填掉**。

    以前 refresh_history() 末尾会调 _load_channels()，而它挂在 8 秒定时器上：
    输入 Webhook 时，输入框每 8 秒被静默还原成已保存的值 —— 人会以为键盘坏了。
    """
    from desktop_ui.pages.notify import NotifyPage

    api = _StubNotifyApi()
    page = NotifyPage(api)  # type: ignore[arg-type]
    page.timer.stop()

    page.cards["wecom"].set_enabled(True)
    page.input_wecom.setText("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=正在输入的一长串")
    page.refresh_history()   # 模拟定时器触发
    assert page.input_wecom.text().endswith("正在输入的一长串"), "定时刷新不该覆盖用户正在输入的内容"
    assert page.cards["wecom"].is_enabled() is True
    page.deleteLater()


def test_notify_channel_test_uses_current_form_values(qt_app):
    """「测试」要拿界面当前值去测，而不是要求先保存。

    而且**未启用的渠道也要能测** —— 「先测通了再启用」才是常见顺序，
    所以测试时把 enabled 强制打开，只验证凭据能不能用。
    """
    import threading

    from desktop_ui.pages.notify import NotifyPage

    api = _StubNotifyApi()
    # 把「测试」卡在半路，才能稳定地观察到「测试中」这个中间态。
    #
    # 超时必须给得足够长（30s）：_wait_until 在谓词成立后**还会再跑一次
    # processEvents()**，而机器一忙这一次调用就可能耗时几秒 ——
    # 若 gate 只等 5 秒，那一投递就会把按钮恢复成可用，
    # 于是「测试中禁用按钮」这条断言在满负载跑全量时会随机失败。
    gate = threading.Event()

    def slow_test(channel=None, config=None):
        api.tested.append((channel, config))
        gate.wait(timeout=30)
        return {"ok": True, "channel": channel}

    api.test_notify = slow_test  # type: ignore[assignment]
    page = NotifyPage(api)  # type: ignore[arg-type]
    page.timer.stop()

    page.input_dingtalk.setText("https://oapi.dingtalk.com/robot/send?access_token=abc")
    page.cards["dingtalk"].set_enabled(False)   # 故意不启用
    # 走「点按钮」这条路，而不是直接调私有方法 ——
    # 直接调会绕开信号接线；曾经 Signal(str, dict) 配 lambda 默认参数时，
    # 点按钮传下去的是一个 dict，而单测因为绕过了接线而全绿。
    page.cards["dingtalk"].btn_test.click()
    assert _wait_until(lambda: bool(api.tested))

    channel, config = api.tested[0]
    assert channel == "dingtalk"
    assert config["webhook"].endswith("access_token=abc"), "应带着界面上当前填的值去测"
    assert config["enabled"] is True, "测试不受「启用」开关限制"

    # 测试中：按钮要禁用（避免连点），结果区要有「测试中」反馈
    assert page.cards["dingtalk"].btn_test.isEnabled() is False
    assert "测试中" in page.cards["dingtalk"].result_label.text()

    gate.set()
    assert _wait_until(lambda: page.cards["dingtalk"].btn_test.isEnabled())
    assert "成功" in page.cards["dingtalk"].result_label.text()
    page.deleteLater()


def test_api_client_test_notify_passes_config_override(qt_app):
    """测试要能带临时配置 —— 否则「填完 → 保存 → 测试」来回一趟，
    而保存一份错的配置本身就是污染。"""
    import inspect

    from desktop_ui.api_client import ApiClient

    signature = inspect.signature(ApiClient.test_notify)
    assert "config" in signature.parameters, "test_notify 必须支持 config override"


def test_notify_page_never_writes_masked_placeholder_back(qt_app):
    """后端读回来的敏感字段是 `********`，**绝不能把它写回配置**。

    `AppConfig.to_dict(mask_secrets=True)` 会把 webhook / chat_id / phone / key / appcode
    统统换成 `********`，而 `loader.update_section` 是「有则覆盖」、不做任何过滤。
    所以只要把占位符原样显示进输入框，用户改一下别的字段再保存，
    真实凭据就被 `********` 顶掉了 —— 通知从此静默失效，界面上看不出任何异常。
    """
    from desktop_ui.pages.notify import NotifyPage
    from ticket_helper.config.settings import SecretMasked

    api = _StubNotifyApi()
    api.config["notify"]["dingtalk"] = {"enabled": True, "webhook": SecretMasked}
    api.config["notify"]["wecom"] = {"enabled": True, "webhook": SecretMasked}
    api.config["notify"]["telegram"] = {"enabled": True, "bot_token": SecretMasked, "chat_id": SecretMasked}
    api.config["notify"]["voice"] = {"enabled": True, "appcode": SecretMasked, "phone": SecretMasked}
    api.config["notify"]["serverchan"] = {"enabled": True, "key": SecretMasked}

    page = NotifyPage(api)  # type: ignore[arg-type]
    page.timer.stop()

    # 1) 输入框里不能出现占位符
    for widget in (page.input_dingtalk, page.input_wecom, page.input_tg_chat,
                   page.input_tg_token, page.input_voice_phone, page.input_voice_appcode,
                   page.input_serverchan_key, page.input_smtp_pass):
        assert widget.text() == "", f"{widget.placeholderText()} 不该把脱敏值填进输入框"

    # 2) 但要让人知道「已经存过了」，否则他会以为配置丢了
    assert "已保存" in page.input_dingtalk.placeholderText()

    # 3) 什么都不填就保存 → 这些字段根本不该进 payload（服务端会保留原值）
    page._save_channels()  # noqa: SLF001
    saved = api.saved["notify"]
    assert "webhook" not in saved["dingtalk"], "空值不能把已存的 webhook 覆盖掉"
    assert "webhook" not in saved["wecom"]
    assert "bot_token" not in saved["telegram"] and "chat_id" not in saved["telegram"]
    assert "appcode" not in saved["voice"] and "phone" not in saved["voice"]
    assert "key" not in saved["serverchan"]
    assert SecretMasked not in str(saved), "脱敏占位符绝不能被写回配置"
    # 但开关状态必须照常保存
    assert saved["dingtalk"]["enabled"] is True
    page.deleteLater()


def test_notify_page_sends_typed_value(qt_app):
    """用户真填了新值时要发出去 —— 别把「留空 = 不修改」做成「永远不修改」。"""
    from desktop_ui.pages.notify import NotifyPage

    api = _StubNotifyApi()
    page = NotifyPage(api)  # type: ignore[arg-type]
    page.timer.stop()

    page.cards["wecom"].set_enabled(True)
    page.input_wecom.setText("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=new-key")
    page._save_channels()  # noqa: SLF001
    assert api.saved["notify"]["wecom"]["webhook"].endswith("key=new-key")

    # 手滑把占位符贴进去，也必须按「不修改」处理
    page.input_wecom.setText("********")
    page._save_channels()  # noqa: SLF001
    assert "webhook" not in api.saved["notify"]["wecom"]
    page.deleteLater()


# --------------------------------------------------------------------------- #
# UI-T9 任务配置页的版式：一屏装得下、保存条够得到、控件不被压
# --------------------------------------------------------------------------- #
def _open_tasks_page(web_backend, size=(1320, 880)):
    """把任务配置页放进真实的主窗口里摆好（布局问题只有在真实容器里才量得准）。"""
    from desktop_ui.main_window import MainWindow
    from desktop_ui.realtime import RealtimeClient

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    window = MainWindow(client, RealtimeClient(client))
    window.resize(*size)
    window.show()
    for widget in window.pages.values():
        timer = getattr(widget, "timer", None)
        if timer is not None:
            timer.stop()
    window.overview_timer.stop()
    window.show_page_key("tasks")
    for _ in range(30):
        QApplication.processEvents()
    return window, window.pages["tasks"]


def _form_scroll(page):
    """找出「装着表单」的那个滚动区（用分组标题下面的分隔线认它）。"""
    from PySide6.QtWidgets import QFrame, QScrollArea

    for area in page.findChildren(QScrollArea):
        if area.widget().findChild(QFrame, "sectionRule") is not None:
            return area
    raise AssertionError("没找到装着表单的滚动区")


def test_task_page_fits_one_screen(qt_app, web_backend):
    """默认窗口尺寸下，表单**一屏装得下**，不用滚动。

    改造前表单内容高 1262px、可视只有 800 上下 —— 要滚近 3 屏。
    这不是「审美」问题：要找「乘车日期」得先滚，填完保存还得再滚两屏。
    """
    window, page = _open_tasks_page(web_backend)
    area = _form_scroll(page)
    viewport = area.viewport()
    host = area.widget()

    assert not area.horizontalScrollBar().isVisible(), "表单不该出现横向滚动条"
    assert host.height() <= viewport.height() + 24, (
        f"表单内容 {host.height()}px 超过了可视高度 {viewport.height()}px —— "
        "默认窗口下应该一屏装得下"
    )
    window._force_quit = True  # noqa: SLF001
    window.close()


def test_task_page_save_bar_is_pinned_outside_the_scroll_area(qt_app, web_backend):
    """「保存任务」必须固定在滚动区**外面**，永远看得见。

    以前它长在滚动区末尾：填完表单还得滚两屏才找得到保存按钮 ——
    这是「不好操作」里最直接的一处。
    """
    from PySide6.QtWidgets import QFrame

    window, page = _open_tasks_page(web_backend)
    area = _form_scroll(page)
    bar = page.findChild(QFrame, "actionBar")
    assert bar is not None, "应该有独立的底部操作条"
    assert not area.isAncestorOf(bar), "操作条不能在滚动区里面"
    assert page.btn_save.isVisibleTo(page) is True
    assert page.btn_reset.isVisibleTo(page) is True

    # 展开运行参数（内容变长）之后，保存按钮依然在
    page.btn_adv.setChecked(True)
    for _ in range(10):
        QApplication.processEvents()
    assert page.btn_save.isVisibleTo(page) is True, "内容变长后保存按钮也不能跟着滚走"
    window._force_quit = True  # noqa: SLF001
    window.close()


def test_task_page_status_line_does_not_steal_half_the_screen(qt_app, web_backend):
    """状态栏只该占一行 —— 它不该和主内容区平分高度。

    这是一条真实的布局 bug：主内容区没给 stretch，而状态栏 QLabel 开了
    ``setWordWrap`` 之后垂直方向可以无限长，于是两者各拿了一半（实测 434px）。
    表现是「表单被挤在上半屏、下半屏一大片空白」，很难联想到是状态栏干的。
    """
    window, page = _open_tasks_page(web_backend)
    status = page.status_label
    assert status.height() <= 60, f"状态栏被撑到 {status.height()}px，又在和主内容区抢高度"
    # 主内容区要拿到页面的绝大部分高度
    assert _form_scroll(page).viewport().height() >= page.height() * 0.7
    window._force_quit = True  # noqa: SLF001
    window.close()


def test_task_page_simple_fields_are_single_line_rows(qt_app, web_backend):
    """简单控件用「标签在左、控件在右」的一行式；复合控件才占满整行。

    全用「小标题在上」时，连「任务名称」这种一个词的输入框都独占一整行，
    表单被撑到 1262px。区分两种行型是这次能压到一屏的主要原因。
    """
    window, page = _open_tasks_page(web_backend)
    # 运行参数默认是收起的，里面的控件要摊开才量得到几何
    page.btn_adv.setChecked(True)
    for _ in range(10):
        QApplication.processEvents()

    for widget, label_text in (
        (page.input_name, "任务名称 *"),
        (page.input_passengers, "乘客姓名"),
        (page.input_time, "出发时间"),
        (page.combo_strategy, "抢票策略"),
    ):
        row = widget.parentWidget()
        labels = [lb.text() for lb in row.findChildren(QLabel)]
        assert label_text in labels, f"{label_text} 应该是这一行里的标签（一行式），实际标签={labels}"
        assert row.height() <= 46, f"{label_text} 这一行高 {row.height()}px，不是一行式"
        # 标签在左、控件在右
        assert widget.x() > 40, f"{label_text} 的控件没有排在标签右边"

    # 复合控件仍然占满整行（它们内部有按钮行，塞进窄列会被压扁）
    for field in (page.input_dates, page.input_white):
        assert field.width() >= 450, f"{type(field).__name__} 只有 {field.width()}px，被挤窄了"
    window._force_quit = True  # noqa: SLF001
    window.close()


def test_task_page_train_lists_switch_by_segment(qt_app, web_backend):
    """白/黑名单用一个分段按钮切换，一次看一个，按钮上带数量。

    两个名单本来就互斥（同一趟车不能同时在两边），并排铺开只是让页面更高、
    还要读者自己对比两串车次号。数量写在按钮上，另一边的状态一眼可知。
    """
    window, page = _open_tasks_page(web_backend)

    assert page.train_stack.currentIndex() == 0
    assert page.btn_tab_white.isChecked() is True
    assert "只抢这些" in page.btn_tab_white.text()

    page.btn_tab_black.setChecked(True)
    page.train_stack.setCurrentIndex(1)
    for _ in range(10):
        QApplication.processEvents()
    assert page.train_stack.currentIndex() == 1

    # 切换后当前这个名单的控件同样要「能打字」，不能因为是第二页就拿不到宽度
    assert page.input_black.entry.width() >= 150, (
        f"切到黑名单后输入框只有 {page.input_black.entry.width()}px"
    )
    assert page.input_black.btn_pick.width() >= 120

    # 数量要出现在按钮上
    page.input_black.setText("G1001,K512")
    page.input_white.setText("G6102")
    for _ in range(5):
        QApplication.processEvents()
    assert "2" in page.btn_tab_black.text(), page.btn_tab_black.text()
    assert "1" in page.btn_tab_white.text(), page.btn_tab_white.text()

    # 两个名单都要能原样取到值（切换只是显示层的分组，不改变数据）
    payload = page._payload()  # noqa: SLF001
    assert payload["trains_blacklist"] == "G1001,K512"
    assert payload["trains_whitelist"] == "G6102"
    window._force_quit = True  # noqa: SLF001
    window.close()


def test_task_page_editing_offers_a_way_back(qt_app, web_backend):
    """进了编辑态要能退出来，而且运行参数要自动摊开。

    运行参数里有他上次特意改过的值，收起来会让人以为「我设的东西丢了」；
    而「取消编辑」是唯一的退路 —— 没有它就只能靠清空表单。
    """
    window, page = _open_tasks_page(web_backend)
    assert page.btn_cancel_edit.isVisibleTo(page) is False

    page._load_into_form({"id": 7, "name": "旧任务", "seat_types": ["硬卧"]})  # noqa: SLF001
    for _ in range(5):
        QApplication.processEvents()
    assert page.btn_cancel_edit.isVisibleTo(page) is True
    assert page.adv_body.isVisibleTo(page) is True, "编辑已有任务时运行参数应自动展开"
    assert "编辑任务 #7" in page.form_title.text()

    page.btn_cancel_edit.click()
    for _ in range(5):
        QApplication.processEvents()
    assert page.editing_id is None
    assert page.btn_cancel_edit.isVisibleTo(page) is False
    assert "新建任务" in page.form_title.text()
    window._force_quit = True  # noqa: SLF001
    window.close()


def test_task_page_has_no_horizontal_scroll_bar_with_real_tasks(qt_app, web_backend):
    """有任务卡片时，整页**任何区域都不该出现横向滚动条**。

    真出现过一次：右栏卡片里显示日期区间 `2026-10-01/2026-10-02` ——
    这种没有断点的长串，Qt 换行标签算出的最小宽度就是**整串的宽度**（实测 273px），
    于右栏（336px）被顶出横向滚动条。而当时的测试只量了表单区，漏掉了右栏。
    修法是把分隔符改成带空格的 `" / "`，让 Qt 有换行点。
    """
    from PySide6.QtWidgets import QScrollArea

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    created = client.create_task(
        {
            "name": "横向滚动条探针",
            "left_dates": ["2026-10-01", "2026-10-02"],
            "from_stations": ["广州南"],
            "to_stations": ["长沙南"],
            "seat_types": ["二等座", "一等座"],
            "trains_whitelist": ["G6102", "G6140"],
            "trains_blacklist": ["K512"],
            "query_interval": 1.5,
            "strategy": "dual",
            "enabled": True,
        }
    )

    window, page = _open_tasks_page(web_backend)
    for _ in range(20):
        QApplication.processEvents()

    offenders = []
    for index, area in enumerate(page.findChildren(QScrollArea)):
        if area.horizontalScrollBar().isVisible():
            host = area.widget()
            offenders.append(
                f"滚动区#{index}: 视口 {area.viewport().width()}px < "
                f"内容最小 {host.minimumSizeHint().width()}px"
            )
    client.delete_task(int(created["id"]))
    window._force_quit = True  # noqa: SLF001
    window.close()

    assert not offenders, (
        "任务配置页出现了横向滚动条（内容比可视区还宽）：\n  " + "\n  ".join(offenders)
    )


def test_task_page_composite_fields_have_room(qt_app, web_backend):
    """复合控件（自带按钮行的标签控件）里的输入框要有足够宽度。

    改造前它们被塞进 ``QFormLayout`` 的右列，实测车次输入框只剩 **61px** ——
    因为那一行里还要装下「加入」和「从 12306 拉取车次」两个按钮，等于不能打字。

    这条测试钉住的是「能用」的底线，不是审美。
    """
    window, page = _open_tasks_page(web_backend)

    # 站点 / 日期：按钮和日历控件都要完整可见
    assert page.input_from.btn_search.width() >= 70, page.input_from.btn_search.width()
    assert page.input_to.btn_search.width() >= 70
    assert page.input_dates.picker.width() >= 120
    assert page.input_dates.width() >= 450, "日期控件应该占满整行"

    # 两个车次名单都要**切过去再量** —— 第二个在 QStackedWidget 的非当前页，
    # 不切过去的话它的几何压根没被布局过，量到的是旧值（那种断言等于没断言）。
    for index, field in ((0, page.input_white), (1, page.input_black)):
        page.train_stack.setCurrentIndex(index)
        for _ in range(12):
            QApplication.processEvents()
        # 复合控件整体要拿到整行宽度（而不是 QFormLayout 右列那种半幅）
        assert field.width() >= 450, f"第 {index} 个名单只有 {field.width()}px，被挤窄了"
        assert field.entry.width() >= 150, f"车次输入框只有 {field.entry.width()}px，没法打字"
        assert field.btn_pick.width() >= 120, "「从 12306 拉取车次」被压窄了"

    window._force_quit = True  # noqa: SLF001
    window.close()


def test_station_picker_newer_search_wins(qt_app):
    """搜索进行中再点一次搜索，必须**以新的为准**，不能静默无响应。

    原实现是 `if self._busy: return` —— 上一次查询还没回来时点「搜索」会被直接吞掉：
    按钮按了没反应、状态栏还停在「正在查询」，使用者只会以为程序卡了。
    更糟的是「新结果先到、旧结果后到」会把新结果覆盖回去（本机跑全量时真出现过）。

    现在用请求序号：**后发请求胜出**，迟到的结果与迟到的失败都被丢弃。
    """
    from desktop_ui.station_picker import StationPickerDialog

    import threading  # 只在本用例里用（模拟慢网络）

    class _SlowStub(_StubStationApi):
        def __init__(self) -> None:
            super().__init__()
            self.gate = threading.Event()
            self.calls = 0

        def station_items(self, keyword: str = "", *, limit: int = 60) -> list[dict]:
            self.calls += 1
            if self.calls == 1:
                # 第一次查询卡住（模拟慢网络）：期间用户改了关键字又点了一次搜索
                self.gate.wait(timeout=30)
            return super().station_items(keyword, limit=limit)

    api = _SlowStub()
    dialog = StationPickerDialog(api)      # 打开即触发第一次查询（被卡住）
    dialog.input.setText("广州")
    dialog.search()                        # 旧实现会在这里被 _busy 静默吞掉
    api.gate.set()                         # 放行第一次查询，让它「迟到」

    assert _wait_until(
        lambda: dialog.candidates.count() == 2 and dialog.candidates.item(0).text().startswith("广州")
    ), f"新搜索的结果必须胜出，当前列表={[dialog.candidates.item(i).text() for i in range(dialog.candidates.count())]}"
    assert "广州" in api.keywords
    # 按钮必须恢复可用（被吞掉的实现会一直禁用）
    assert dialog.btn_search.isEnabled() is True
    dialog.close()


def test_teaching_edition_shows_tomorrow_but_keeps_real_dates(qt_app, frozen_clock, monkeypatch):
    """★ 教学演示版：日期标签与日历控件都显示「明天」，但**数据一行没变**。

    录播课程要能长期复用，画面上不能出现具体日期。这里最容易犯的错是
    「顺手把数据也改了」—— 那样任务真的会去抢「明天」的票，而用户配的是别的
    日子。所以这条同时钉两件相反的事：显示是「明天」，``values()`` 仍是真实日期。
    """
    from datetime import timedelta

    from ticket_helper import edition

    from desktop_ui.token_field import DateField

    monkeypatch.setattr(edition, "DEMO_MODE", True)

    d1 = (frozen_clock + timedelta(days=1)).isoformat()
    d2 = (frozen_clock + timedelta(days=2)).isoformat()
    field = DateField(label="乘车日期")
    field.add(d1)
    field.add(d2)

    # 数据：两个真实日期，一个都不少
    assert field.values() == [d1, d2]
    assert field.text() == f"{d1},{d2}"
    # 显示：合并成一行「明天」（「连加 3 天」不该出现三行一模一样的字）
    assert field.list.count() == 1, "演示版该把整组日期并成一行"
    assert field.list.item(0).text() == edition.DEMO_DATE_TEXT

    # 日历控件也显示「明天」，而且只能选明天
    assert edition.DEMO_DATE_TEXT in field.picker.text(), "日历控件上还写着具体日期"
    tomorrow = d1
    assert field.picker.minimumDate().toString("yyyy-MM-dd") == tomorrow
    assert field.picker.maximumDate().toString("yyyy-MM-dd") == tomorrow

    # 移除那一行 = 移除它代表的**全部**真实日期（显示层合并了，数据也得跟着一刀切）
    field.list.item(0).setSelected(True)
    field.remove_selected()
    assert field.values() == []


def test_official_edition_still_shows_the_real_date(qt_app, frozen_clock):
    """反向对照：正式版必须原样显示 —— 这条挂了说明演示版漏进了正式包。"""
    from datetime import timedelta

    from desktop_ui.token_field import DateField

    d1 = (frozen_clock + timedelta(days=1)).isoformat()
    field = DateField(label="乘车日期")
    field.add(d1)

    assert field.list.item(0).text() == d1, "正式版不许把日期改写成「明天」"
    assert field.values() == [d1]
    assert field.picker.minimumDate().toString("yyyy-MM-dd") == frozen_clock.isoformat()


def test_date_field_refuses_past_and_invalid_dates(qt_app, frozen_clock) -> None:
    """★ 乘车日期控件必须拒绝**过去的**和**不存在的**日期，并说明原因。

    真机来源（回流 #53）：这一版之前 `DateField.add` 完全不做日期校验 ——
    实测 `add("2025-10-03")` 与 `add("2025-13-45")`（根本不存在的日期）
    **都返回 True**，于是去年的日期能一路进到任务配置里。
    而且拒绝时是**静默**的，使用者只看到「点了没反应」。

    真正的闸门在 `TaskConfig.validate()`；这里是「刚点添加」就报，越早越不容易填错。
    """
    from PySide6.QtCore import QDate

    from desktop_ui.token_field import DateField

    field = DateField(label="乘车日期")
    reasons: list[str] = []
    field.rejected.connect(reasons.append)

    # ★「今天」只能用**冻结**的那个 —— 控件用的也是 ``train_date.today()``。
    # 以前这里写 ``QDate.currentDate()``（真实今天）：两边都错，反而互相抵消，
    # 于是这条用例一直绿着；直到控件被改回单一来源，它才暴露出**自己**
    # 也是「第二个来源」（会随真实日期推进而变红）。
    today = QDate(frozen_clock.year, frozen_clock.month, frozen_clock.day)
    yesterday = today.addDays(-1).toString("yyyy-MM-dd")

    assert field.add("2025-10-03") is False
    assert field.add("2025-13-45") is False, "不存在的日期也要拦（月 13、日 45）"
    assert field.add(yesterday) is False, "昨天也算过去"
    assert field.text() == "", f"被拒绝的值不该留在列表里：{field.text()!r}"

    assert len(reasons) == 3, reasons
    assert all(reason for reason in reasons), "拒绝必须带原因，不能静默"
    assert any("过去" in reason for reason in reasons), reasons
    assert any("合法日期" in reason for reason in reasons), reasons

    assert field.add(today.toString("yyyy-MM-dd")) is True, "今天要允许"
    assert field.add(today.addDays(7).toString("yyyy-MM-dd")) is True, "未开售只定性不拦，控件不该拦"
    assert field.picker.minimumDate() == today, "日历里也要选不到过去的日期"


def test_settings_page_owns_the_ratelimit_and_codec_controls(qt_app, web_backend) -> None:
    """★ 防的是：「限速 / 打码」控件被放回「通知设置」页。

    真机来源：这些控件原先就在通知页的第三个 tab 里，而**页名与内容不符** ——
    想改限速的人不会去点「通知设置」，网页端却是独立的「系统设置」页。
    拆开之后这条测试同时钉住两件事：控件**在**设置页、且**不在**通知页。
    """
    from desktop_ui.pages import NotifyPage, SettingsPage

    client = ApiClient(web_backend)
    client.login("admin", "test-pass")
    settings = SettingsPage(client)

    # 限速：值必须来自后端配置，而不是控件自己写死的默认值
    config = client.get_config().get("config") or {}
    ratelimit = config.get("ratelimit") or {}
    assert settings.check_adaptive.isChecked() == bool(ratelimit.get("adaptive", True))
    assert settings.spin_min_interval.value() == float(ratelimit.get("min_interval") or 1.0)
    assert settings.spin_max_interval.value() == float(ratelimit.get("max_interval") or 8.0)

    # 打码：三种模式一项不能少
    modes = [
        settings.combo_codec_mode.itemData(i) for i in range(settings.combo_codec_mode.count())
    ]
    assert modes == ["free", "local", "manual"], modes

    # 运行信息 + 配置变更历史（网页端同一页也都有）
    assert settings.info_labels["running"].text() in {"运行中", "未启动"}
    assert settings.config_history_table.columnCount() == 3

    # 反向对照：这些东西**不该**在通知页里
    notify = NotifyPage(client)
    for attr in ("check_adaptive", "combo_codec_mode", "info_labels", "config_history_table"):
        assert not hasattr(notify, attr), f"「{attr}」还在通知页里 —— 它属于系统设置页"


def test_chip_rows_are_never_clipped_by_a_hardcoded_row_height(qt_app):
    """标签列表的高度必须**现量**行高，不许按写死的 24px 预算（回流 #66）。

    真机来源（2026-10-04）：开发机的离屏替身字体一行只要 19px，写死 24px
    怎么算都「够」；真机微软雅黑 13px + 样式内边距一行约 25px，两行起最后
    一行的字被拦腰削掉 —— 使用者看到的就是「添加后的日期/站点显示不全」。

    这里把样式内边距加大来**等效复现**真机的行高：写死预算的实现会在这条
    用例下把最后一行削掉，现量现用的实现才过得去。
    """
    from desktop_ui.theme import STYLESHEET
    from desktop_ui.token_field import DateField

    field = DateField()
    # 加大内边距 = 等效「一行比开发机量出来的高一档」，逼出写死行高的短板
    field.list.setStyleSheet(STYLESHEET + "\nQListWidget::item { padding: 8px 4px; }")
    # 三行才是**承重**的判据：两行时「写死 24px」的预算恰好还够（歪打正着），
    # 三行起它给 24*3+8=80px，而实际需要 3 行 × 28px = 84px —— 必削。
    field.setText("2026-10-04,2026-10-05,2026-10-06")
    field.show()          # 未显示时几何还没排过版，viewport 不可信
    qt_app.processEvents()

    from desktop_ui.token_field import MAX_CHIP_HEIGHT

    assert field.list.count() == 3, "三行数据：真机行高下上限放不满三行，正合适"
    row = field.list.sizeHintForRow(0)
    view = field.list.viewport().height()
    # 真机行高（29px）下，84px 的上限只放得下 2 行 —— 要判的是「可见区
    # 正好等于上限允许的整行数」，而不是「三行都要露出来」。
    assert view == row * (MAX_CHIP_HEIGHT // row), (
        f"可见区 {view}px 应当正好是上限内的整行数（每行实测 {row}px，"
        f"上限 {MAX_CHIP_HEIGHT}px 应放 {MAX_CHIP_HEIGHT // row} 行）"
    )
    # ★ 真正的判据：可见区必须**正好是整行**。上限按像素硬夹（84px）时，
    # 可见区会落在半行上 —— 最后一行只露小半截，看着就是「显示不全」。
    assert view % row == 0, (
        f"可见区 {view}px 不是整行（每行 {row}px）：底部会露半截行 —— 上限必须落在整行上"
    )
    field.deleteLater()


def test_date_field_caps_at_whole_rows(qt_app):
    """日期多到超过上限时，只截在**整行**上（第 4 行不许露半截）。

    真机来源（2026-10-04）：连加 3 天就有 3~4 个日期，上限 84px 而真机一行
    25px —— 4 行要 100px，硬夹到 84px 的结果是第 4 行露出小半截。
    """
    from desktop_ui.theme import STYLESHEET
    from desktop_ui.token_field import MAX_CHIP_HEIGHT, DateField

    field = DateField()
    field.list.setStyleSheet(STYLESHEET + "\nQListWidget::item { padding: 8px 4px; }")
    field.setText("2026-10-04,2026-10-05,2026-10-06,2026-10-07")
    field.show()          # 同上：先显示，几何才可信
    qt_app.processEvents()

    row = field.list.sizeHintForRow(0)
    view = field.list.viewport().height()
    assert field.list.count() == 4, "四个日期：超上限，必须能滚动"
    assert view % row == 0, f"可见区 {view}px 不是整行（每行 {row}px）—— 会露半截行"
    assert view // row == MAX_CHIP_HEIGHT // row, (
        f"上限 {MAX_CHIP_HEIGHT}px 按每行 {row}px 应该正好放 {MAX_CHIP_HEIGHT // row} 行，"
        f"实际放了 {view // row} 行"
    )
    assert field.list.verticalScrollBar().maximum() > 0, "装不下时必须能滚动，而不是把行切掉"
    field.deleteLater()


def test_priority_rows_are_never_clipped(qt_app):
    """席别优先级的两个列表同理：高度跟着**内容与真实行高**走（回流 #66）。

    修复前这里写死 ``LIST_HEIGHT = 124``（约 5 行）：真机一行 25px 时第 5 行
    会被削掉 9px；而且同步只挂在 ``__init__``（列表还空着），量不到行高，
    装多少行都停在 1 行的高度。
    """
    from desktop_ui.theme import STYLESHEET
    from desktop_ui.priority_field import PriorityList

    seats = ["商务座", "一等座", "二等座", "硬卧", "软卧", "硬座", "无座"]
    widget = PriorityList(seats, default=seats[:5])
    widget.list_picked.setStyleSheet(STYLESHEET + "\nQListWidget::item { padding: 8px 4px; }")
    widget.setText(",".join(seats[:5]))
    widget.show()         # 同上：先显示，几何才可信
    qt_app.processEvents()

    # 5 行 × 实测 28px = 140px > 写死的 124px —— 修复前第 5 行必被削
    assert widget.list_picked.count() == 5, "五个席别必须都在「会抢的席别」里"
    picked = widget.list_picked
    row = picked.sizeHintForRow(0)
    view = picked.viewport().height()
    assert view >= row * 5, f"可见区 {view}px 容不下五行（每行实测 {row}px）"
    assert view % row == 0, f"可见区 {view}px 不是整行（每行 {row}px）—— 会露半截行"
    # 不做同步时会退到默认高度（很高），「装得下」是假的 —— 高度要贴着内容走
    assert view <= row * 5 + row, f"可见区 {view}px 高出五行内容太多（每行 {row}px）"
    # 右边的「可选席别」是另一个列表（行数不同：这里剩 2 个），同样要整行
    avail = widget.list_available
    a_row = avail.sizeHintForRow(0)
    a_view = avail.viewport().height()
    assert a_view >= a_row * avail.count(), (
        f"右边「可选席别」可见区 {a_view}px 装不下 {avail.count()} 行（每行 {a_row}px）"
    )
    assert a_view % a_row == 0, f"右边可见区 {a_view}px 不是整行（每行 {a_row}px）"

    # ★ 控件已经在屏幕上、用户又改了内容：这时**不会**再触发 showEvent，
    # 高度只能靠 _render 的同步跟上 —— 少一处就退回旧高度（行被切）。
    picked.setStyleSheet(STYLESHEET + "\nQListWidget::item { padding: 8px 4px; }")
    widget.setText(",".join(seats[:3]))
    qt_app.processEvents()
    row3 = picked.sizeHintForRow(0)
    view3 = picked.viewport().height()
    assert picked.count() == 3, "改成三个席别后列表应只剩三行"
    assert view3 % row3 == 0, f"改内容后可见区 {view3}px 不是整行（每行 {row3}px）"
    assert view3 == row3 * 3, (
        f"改内容后可见区应正好放 3 行，实际 {view3 // row3} 行 —— "
        "内容变化后的高度同步不能只靠 showEvent"
    )
    widget.deleteLater()
