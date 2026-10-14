"""Windows 桌面客户端入口（UI-01 ~ UI-13）。

启动流程：
1. 探测本地后台（``/api/internal/ping``）；不在运行则自动拉起（UI-10 双击即用）；
2. 用运行目录里的 ``env.py`` 凭据自动登录后台，失败则弹登录框；
3. 打开主窗口，并建立 WebSocket 实时通道；
4. 关闭窗口只最小化到托盘，后台继续挂机。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 让 desktop_ui 在 PyInstaller 与源码两种形态下都能正确导入 ticket_helper
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from desktop_ui.api_client import ApiClient, ApiError, BackendLauncher  # noqa: E402
from desktop_ui.local_env import load_web_credentials  # noqa: E402
from ticket_helper import APP_DISPLAY_NAME  # noqa: E402

log = logging.getLogger("desktop")

# 全局样式已收敛到 desktop_ui/theme.py（天蓝玻璃主题，唯一来源）。
from desktop_ui.theme import C, STYLESHEET  # noqa: F401  （C 供本模块少量着色用）


class LoginDialog:
    """极简登录框：仅在无法自动读取本地凭据时出现。"""

    @staticmethod
    def ask(base_url: str) -> tuple[str, str, str] | None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLineEdit

        dialog = QDialog()
        dialog.setWindowTitle(f"登录 {APP_DISPLAY_NAME} 后台")
        dialog.resize(360, 170)
        layout = QFormLayout(dialog)

        user_input = QLineEdit("admin")
        pass_input = QLineEdit()
        pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        url_input = QLineEdit(base_url)
        layout.addRow("后台地址", url_input)
        layout.addRow("用户名", user_input)
        layout.addRow("密码", pass_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return url_input.text().strip(), user_input.text().strip(), pass_input.text()


def _setup_logging() -> None:
    """桌面端日志：**必须同时落盘**。

    早期只用 StreamHandler 写 stderr，但窗口模式（console=False）下
    ``sys.stderr`` 是 None / devnull，等于桌面端的所有日志**全部丢弃**。
    用户反馈「打开就崩了」时，就完全没有任何线索可查。

    这里直接复用后端那套 ``setup_logging``，让桌面端与后台的日志
    汇到同一个 ``logs/12306.log``（结构化 JSON、带轮转），
    排查时只看一个文件即可。
    """
    try:
        from ticket_helper.logger import make_console_safe, setup_logging

        make_console_safe()
        setup_logging()
        return
    except Exception as exc:  # noqa: BLE001 - 日志系统不可用也要能启动
        pass

    handler = logging.StreamHandler(sys.stderr) if sys.stderr is not None else logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(handler)
    logging.getLogger("desktop").warning("完整日志系统不可用，仅输出到控制台: %s", exc)


def _port_from(base_url: str) -> int | None:
    """从后台地址里取出端口号。"""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        return parsed.port or (443 if parsed.scheme == "https" else 80)
    except Exception:  # noqa: BLE001
        return None


def _crash_dir() -> str:
    try:
        from ticket_helper.runtime.paths import get_app_paths

        return str(get_app_paths().log_dir / "crash")
    except Exception:  # noqa: BLE001
        return "(未知)"


def _env_file() -> str:
    try:
        from ticket_helper.runtime.paths import get_app_paths

        return str(get_app_paths().env_file)
    except Exception:  # noqa: BLE001
        return "(未知)"


def _fatal_dialog(title: str, message: str) -> None:
    """显示致命错误对话框。

    窗口模式下没有控制台，对话框是用户**唯一**能看到原因的途径；
    如果连对话框都弹不出来（例如 Qt 平台插件加载失败），
    就地打印一遍，至少保证信息不会完全丢失。
    """
    log.error("%s: %s", title, message.replace("\n", " "))
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is not None:
            QMessageBox.critical(None, title, message)
            return
    except Exception as exc:  # noqa: BLE001
        log.warning("弹窗失败: %s", exc)
    try:
        print(f"\n【{title}】\n{message}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _install_qt_message_handler() -> None:
    """把 Qt 自身的警告/错误导入 logging，避免它们在窗口模式下无声无息。"""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return

    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    qt_log = logging.getLogger("qt")

    def _handler(mode, context, message: str) -> None:  # noqa: ANN001
        qt_log.log(levels.get(mode, logging.INFO), "%s", message)

    qInstallMessageHandler(_handler)


def _run(argv: list[str] | None = None, *, port: int | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ticket-helper-desktop", description=f"{APP_DISPLAY_NAME}桌面客户端")
    parser.add_argument("--port", type=int, default=port, help="后台服务端口（默认 8008）")
    parser.add_argument("--base-url", default="", help="直接指定后台地址")
    parser.add_argument("--no-autostart", action="store_true", help="不自动拉起后台服务")
    args, _ = parser.parse_known_args(argv)

    _setup_logging()

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("未安装 PySide6，无法启动桌面客户端。\n请执行：pip install PySide6", file=sys.stderr)
        return 1

    from desktop_ui.main_window import MainWindow
    from desktop_ui.realtime import RealtimeClient

    app = QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setStyleSheet(STYLESHEET)
    # 关掉最后一个窗口不退出（托盘挂机）
    app.setQuitOnLastWindowClosed(False)
    _install_qt_message_handler()

    username, password, base_url = load_web_credentials()
    if args.base_url:
        base_url = args.base_url
    if args.port:
        base_url = f"http://127.0.0.1:{args.port}"
    base_url = base_url or "http://127.0.0.1:8008"

    api = ApiClient(base_url)
    launcher = BackendLauncher(api)

    # 1) 确保后台在线。端口被占用时自动换一个，而不是直接报错退出
    #    （用户双击两次、或上次的后台没退干净，都会占住默认端口）
    if not args.no_autostart and not launcher.is_ready():
        target_port = args.port or _port_from(base_url)
        if target_port and not BackendLauncher.is_port_available(target_port):
            free_port = BackendLauncher.find_free_port(target_port)
            if free_port is None:
                _fatal_dialog(
                    "后台端口被占用",
                    f"端口 {target_port} 已被其它程序占用，且未能找到可用端口。\n\n"
                    "请关闭占用该端口的程序后重试，或用 --port 指定其它端口。",
                )
                return 1
            log.warning("端口 %s 已被占用，自动改用 %s", target_port, free_port)
            base_url = f"http://127.0.0.1:{free_port}"
            api.base_url = base_url
            target_port = free_port
        else:
            target_port = None  # 端口空闲，交给后台按配置自己决定

        log.info("后台未运行，正在自动启动…（%s）", base_url)
        if not launcher.start(port=target_port):
            _fatal_dialog(
                "无法启动后台服务",
                "未能启动本地后台服务，程序无法继续。\n\n"
                "可以尝试：\n"
                "1. 检查是否有安全软件拦截了后台进程；\n"
                "2. 在命令行运行 `TicketHelper-CLI.exe doctor`（或 python main.py doctor）查看自检结果；\n"
                "3. 直接运行 `TicketHelper-CLI.exe web` 用浏览器打开后台。\n\n"
                f"崩溃日志目录：{_crash_dir()}",
            )
            return 1

    # 2) 登录（优先用本地凭据自动登录）
    #    首次运行时 env.py 还不存在，password 为空；而后台启动过程会**自动生成**
    #    一份带随机密码的 env.py。所以必须在后台就绪之后重新读一次配置，
    #    否则用户会被要求输入一个他自己都还不知道的密码。
    if not password:
        username, password, retry_base_url = load_web_credentials()
        if password:
            log.info("已从 %s 读取到后台凭据，自动登录", _env_file())
        elif retry_base_url:
            log.warning("未能在配置里找到后台密码，将弹出登录框")

    if not password:
        credentials = LoginDialog.ask(base_url)
        if credentials is None:
            launcher.stop()
            return 0
        base_url, username, password = credentials
        api.base_url = base_url.rstrip("/")

    try:
        if not api.login(username or "admin", password):
            raise ApiError("登录失败")
    except ApiError as exc:
        _fatal_dialog(
            "登录失败",
            f"{exc.message}\n\n"
            "请检查数据目录下 env.py 里的 web.auth_user / web.auth_pass 是否正确：\n"
            f"{_env_file()}\n\n"
            "提示：删除 env.py 后重新启动，程序会自动生成一份带随机密码的新配置。",
        )
        launcher.stop()
        return 1
    log.info("已登录后台：%s", base_url)

    # 3) 主界面 + 实时通道
    realtime = RealtimeClient(api)
    window = MainWindow(api, realtime)

    def _on_quit() -> None:
        try:
            realtime.stop()
            api.logout()
            launcher.stop()
        except Exception as exc:  # noqa: BLE001 - 退出流程不应再抛
            log.warning("退出清理时出现异常: %s", exc)
        app.quit()

    window.quitting.connect(_on_quit)
    window.show()
    realtime.connect_all()

    exit_code = app.exec()

    try:
        realtime.stop()
    except Exception:  # noqa: BLE001
        pass
    return exit_code


def main(argv: list[str] | None = None, *, port: int | None = None) -> int:
    """桌面客户端总入口：把任何未处理异常都变成「可读原因 + 崩溃日志」。

    窗口模式下没有控制台，异常如果直接冒出去，进程会静默消失，
    用户只能得出「打开就崩溃」的结论。所以这里必须兜住所有异常。
    """
    try:
        from ticket_helper.runtime.crash import install_global_hook, write_crash_log

        install_global_hook("桌面客户端未捕获异常")
    except Exception as exc:  # noqa: BLE001 - 崩溃记录不可用也要能启动
        logging.getLogger("desktop").warning("崩溃记录模块不可用: %s", exc)
        write_crash_log = None  # type: ignore[assignment]

    try:
        return _run(argv, port=port)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 包含 Qt 抛出的非 Exception 错误
        path = None
        if write_crash_log is not None:
            try:
                path = write_crash_log(exc, context="桌面客户端启动/运行失败")
            except Exception:  # noqa: BLE001
                path = None
        _fatal_dialog(
            f"{APP_DISPLAY_NAME}发生错误",
            f"程序遇到未处理的错误，已停止运行。\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"崩溃日志：{path or '(写入失败)'}\n"
            f"日志目录：{_crash_dir()}\n\n"
            "请把崩溃日志提供给排查方；也可以在命令行执行 "
            "`TicketHelper-CLI.exe doctor` 查看自检结果。",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
