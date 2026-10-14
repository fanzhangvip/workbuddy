"""
程序入口。

注意执行顺序：必须先设置环境变量，再创建 QApplication。
QtMultimedia 会在插件加载阶段读取 QT_FFMPEG_PROTOCOL_WHITELIST 等变量，
晚设置则不生效（表现为 HLS / rtsp 打不开）。
"""

from __future__ import annotations

import os
import sys

# --- 第一步：设置环境变量（务必早于任何 PySide6 导入） ---
from src.utils.env_setup import describe_environment, setup_environment

setup_environment()

# --- 第二步：日志 ---
from src.utils.logger import (  # noqa: E402
    get_logger,
    install_qt_message_handler,
    setup_logging,
)

setup_logging()
# 让 Qt / FFmpeg 自身的报错也进入日志文件，否则窗口模式下看不到失败原因
install_qt_message_handler()
log = get_logger("main")


def _make_console_output_robust() -> None:
    """别让 `print()` 因为编码问题把整个命令行模式打断。

    Windows 控制台默认是 GBK，而视频标题里出现 emoji（YouTube 上很常见，比如 🎧）
    或日文假名之类的字符时根本编不出来，`print()` 会抛 `UnicodeEncodeError`，
    把 `--resolve` / `--net-check` 这两个**专门用来排查问题**的模式整个掀掉。
    只放宽错误处理、不改编码：改成 utf-8 会在真实 cmd 窗口里显示成乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_make_console_output_robust()

# --- 第三步：Qt ---
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.core.backends.factory import describe_backends  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402
from src.ui.theme import apply_theme  # noqa: E402


def _run_net_check(targets: list[str], argv: list[str]) -> int:
    """对若干地址做分层诊断并输出报告。

    窗口程序（打包后 console=False）没有可附加的控制台，因此这里除了打印，
    还会把报告写进日志目录；冻结运行时再用弹窗把结果呈现给用户。
    """
    import datetime as dt  # noqa: PLC0415

    from src.utils.net_diagnose import diagnose  # noqa: PLC0415
    from src.utils.paths import log_dir  # noqa: PLC0415

    reports: list[str] = []
    failed = 0
    for url in targets:
        result = diagnose(url)
        reports.append(result.report())
        if not (result.reachable and result.protocol_ok):
            failed += 1

    text = "\n\n".join(reports)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        path = log_dir() / f"netcheck_{stamp}.txt"
        path.write_text(text + "\n", encoding="utf-8")
        print(f"\n报告已写入: {path}")
    except OSError as exc:
        print(f"\n报告写入失败: {exc}")

    print(text)

    # 窗口程序（打包后）没有控制台，用弹窗把结果呈现给用户。
    # 但无桌面会话时（--offscreen / 服务器上）弹窗会永久阻塞，必须跳过。
    headless = os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen"
    if getattr(sys, "frozen", False) and not headless:
        from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: PLC0415

        app = QApplication.instance() or QApplication(argv[:1])
        box = QMessageBox()
        box.setWindowTitle("地址诊断")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("地址诊断结果")
        box.setDetailedText(text)
        box.setInformativeText(
            "摘要：" + ("全部连通正常" if not failed else f"{failed} 条地址连通异常")
        )
        box.exec()
        del app

    return 1 if failed else 0


def _run_resolve(targets: list[str], verbose: bool = False) -> int:
    """解析网页地址并打印报告（不播放）。

    界面之外的一个入口：排查"这个网页地址为什么播不了"时，
    先看解析这一步拿到了什么，比在界面里反复点要快得多。

    和 --net-check 一样，报告会同时写进日志目录——打包后的 exe 是窗口程序，
    没有控制台，输出只能靠文件带出来。
    """
    import datetime as dt  # noqa: PLC0415

    from src.core import resolver  # noqa: PLC0415
    from src.utils.paths import log_dir  # noqa: PLC0415

    lines: list[str] = ["=" * 68, "网页地址解析（yt-dlp）", "=" * 68,
                        resolver.describe_availability(), ""]

    def say(text: str = "") -> None:
        lines.append(text)
        print(text)

    failed = 0
    for url in targets:
        result = resolver.resolve(url, log=say if verbose else None, verbose=verbose)
        report = result.report()
        say(report)
        say("-" * 68)
        if not result.ok:
            failed += 1

    if failed:
        # 说清楚"单文件流"这个口径：YouTube / B 站这类站点只给分离的音视频流，
        # 播放器里会自动合流播放，这里统计的只是"单文件"，别让用户以为播不了。
        say(f"{failed}/{len(targets)} 个地址没有取到音视频合一的单文件流"
            "（只提供分离流的站点仍会在播放器里自动合流播放，这里只统计「单文件」）。")

    text = "\n".join(lines) + "\n"
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        path = log_dir() / f"resolve_{stamp}.txt"
        path.write_text(text, encoding="utf-8")
        print(f"\n报告已写入: {path}")
    except OSError as exc:
        print(f"\n报告写入失败: {exc}")

    # 窗口程序（打包后）没有控制台，用弹窗把结果呈现给用户。
    # 无桌面会话时（--offscreen）弹窗会永久阻塞，必须跳过。
    headless = os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen"
    if getattr(sys, "frozen", False) and not headless:
        from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: PLC0415

        app = QApplication.instance() or QApplication(argv[:1])
        box = QMessageBox()
        box.setWindowTitle("网页地址解析")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("网页地址解析结果")
        box.setDetailedText(text)
        box.setInformativeText(
            "全部解析成功，已取得可直接播放的单文件流。"
            if not failed
            else f"{failed}/{len(targets)} 个地址没有取到音视频合一的单文件流，详见详细信息。"
        )
        box.exec()
        del app

    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    # --offscreen：无桌面会话时的验证/截图用（必须在 QApplication 之前设置）
    if "--offscreen" in argv:
        argv.remove("--offscreen")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    # --self-check [地址]：环境与产物自检，不起界面
    if "--self-check" in argv:
        index = argv.index("--self-check")
        rest = [a for a in argv[index + 1:] if not a.startswith("-")]
        from src.utils.selfcheck import run_self_check  # noqa: PLC0415

        return run_self_check(rest[0] if rest else None)

    # --resolve <地址...>：只做网页地址解析，不播放
    if "--resolve" in argv:
        index = argv.index("--resolve")
        targets = [a for a in argv[index + 1:] if not a.startswith("-")]
        if not targets:
            print("用法: --resolve <网页地址> [更多地址...] [--verbose]")
            return 2
        return _run_resolve(targets, verbose="--verbose" in argv)

    # --net-check <地址...>：只做地址诊断，不播放
    if "--net-check" in argv:
        index = argv.index("--net-check")
        targets = [a for a in argv[index + 1:] if not a.startswith("-")]
        if not targets:
            print("用法: --net-check <地址> [更多地址...]")
            return 2
        return _run_net_check(targets, argv)

    # 品牌信息在函数内导入：`src.ui` 包一旦被提前加载，可能连带拉起 PySide6，
    # 而本模块顶部要求"环境变量早于任何 PySide6 导入"（见文件头说明）。
    from src.ui import branding

    log.info("启动 %s", branding.WINDOW_TITLE)
    log.info("环境变量:\n%s", describe_environment())
    log.info("播放后端: %s", describe_backends())

    app = QApplication(argv)
    app.setApplicationName("UniversalURLPlayer")
    app.setOrganizationName("videodemo")
    app.setApplicationDisplayName(branding.WINDOW_TITLE)

    # 主题必须在任何窗口创建之前装上：全局样式表会参与控件第一次 polish，
    # 窗口建完再装的话，先建好的那些控件不会重新走一遍解析。
    apply_theme(app)

    window = MainWindow()
    window.show()

    # 命令行传入地址时直接播放
    initial = [a for a in argv[1:] if not a.startswith("-")]
    if initial:
        window.address.setText(initial[0])
        window._play_from_address()  # noqa: SLF001 - 内部入口，便于命令行启动

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
