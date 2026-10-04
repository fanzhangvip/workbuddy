"""命令行入口测试（含中文 Windows 控制台编码的回归保护）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# 控制台编码健壮性
# --------------------------------------------------------------------------- #
def test_console_safe_helper_is_exported():
    from ticket_helper.logger import make_console_safe

    assert callable(make_console_safe)
    make_console_safe()  # 幂等且不得抛异常


def test_cli_sources_avoid_non_gbk_symbols():
    """回归：中文 Windows 控制台是 GBK，✔/✘/emoji 会导致 UnicodeEncodeError。

    曾经的实际故障：打包后的 exe 执行 ``doctor`` 自检时，
    打印 ✔ 直接抛 UnicodeEncodeError，整个自检中断。
    """
    for name in ("main.py", "scripts/smoke_test.py", "scripts/seed_account.py", "scripts/build_exe.py"):
        source = (ROOT / name).read_text(encoding="utf-8")
        for bad in ("✔", "✘"):
            assert bad not in source, f"{name} 含 GBK 无法编码的符号 {bad!r}"
        # 替换后的符号必须能在 GBK 下编码
        for safe in ("√", "×"):
            safe.encode("gbk")


def test_doctor_style_symbols_are_gbk_encodable():
    for text in ("√", "×", "[OK]", "【运行中】"):
        text.encode("gbk")


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def test_parser_builds_all_subcommands():
    import main as cli_main

    parser = cli_main.build_parser()
    for command in ("web", "desktop", "grab", "init", "doctor", "mock"):
        args = parser.parse_args([command])
        assert callable(args.func)

    args = parser.parse_args(["web", "--port", "9000"])
    assert args.port == 9000

    args = parser.parse_args(["mock", "--port", "18006", "--scenario", "sold_out"])
    assert args.port == 18006
    assert args.scenario == "sold_out"


def test_version_command_runs(capsys):
    import main as cli_main

    assert cli_main.main(["-v"]) == 0
    out = capsys.readouterr().out
    assert "12306 抢票助手" in out
    assert "web" in out and "desktop" in out


# --------------------------------------------------------------------------- #
# 回归：无参数启动必须进入图形界面，而不是打印帮助后退出
# --------------------------------------------------------------------------- #
def test_no_args_launches_gui_not_help(monkeypatch, capsys):
    """回归测试：双击 exe 曾经会打印帮助文本然后退出。

    控制台/窗口随进程退出立即消失，用户看到的就是「打开就崩溃」。
    因此无参数启动必须走图形界面分支。
    """
    import main as cli_main

    called: list[str] = []
    monkeypatch.setattr(cli_main, "cmd_desktop", lambda args: (called.append("desktop"), 0)[1])

    assert cli_main.main([]) == 0
    assert called == ["desktop"], "无参数启动应进入桌面客户端分支"
    out = capsys.readouterr().out
    assert "正在启动桌面客户端" in out


def test_no_args_falls_back_to_web_without_pyside(monkeypatch, capsys):
    """没装 PySide6 时也应给出可用结果（退回 Web 后台），而不是报错退出。"""
    import builtins

    import main as cli_main

    called: list[str] = []
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "PySide6":
            raise ImportError("未安装 PySide6")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(cli_main, "cmd_web", lambda args: (called.append("web"), 0)[1])

    assert cli_main.main([]) == 0
    assert called == ["web"]
    assert "未安装 PySide6" in capsys.readouterr().out


def test_version_flag_does_not_launch_gui(monkeypatch, capsys):
    import main as cli_main

    monkeypatch.setattr(
        cli_main, "cmd_default", lambda args: pytest.fail("-v 不应触发图形界面启动")
    )
    assert cli_main.main(["-v"]) == 0
    assert "可用子命令" in capsys.readouterr().out


def test_top_level_port_flag_is_accepted():
    import main as cli_main

    parser = cli_main.build_parser()
    args = parser.parse_args(["-p", "8123"])
    assert args.port == 8123


# --------------------------------------------------------------------------- #
# 崩溃兜底
# --------------------------------------------------------------------------- #
def test_crash_log_is_written(temp_home):
    from ticket_helper.runtime.crash import write_crash_log

    try:
        raise ValueError("模拟崩溃")
    except ValueError as exc:
        path = write_crash_log(exc, context="单元测试")

    assert path is not None and path.exists()
    content = path.read_text(encoding="utf-8")
    assert "模拟崩溃" in content
    assert "ValueError" in content
    assert "单元测试" in content
    assert "Traceback" in content


def test_fatal_error_returns_nonzero_without_traceback(temp_home, monkeypatch, capsys):
    """入口异常应被兜住：返回非 0，给出可读原因，并留下崩溃日志。"""
    import main as cli_main

    def boom(args):
        raise RuntimeError("故意失败")

    monkeypatch.setattr(cli_main, "cmd_doctor", boom)
    monkeypatch.setattr(cli_main, "_keeps_window_open", lambda: False)

    assert cli_main.main(["doctor"]) == 1
    err = capsys.readouterr().err
    assert "启动失败" in err
    assert "RuntimeError: 故意失败" in err
    assert "Traceback" not in err  # 不向用户抛裸堆栈

    crash_dir = temp_home / "logs" / "crash"
    assert crash_dir.exists() and list(crash_dir.glob("crash-*.log"))


def test_keeps_window_open_logic(monkeypatch):
    import main as cli_main

    # 源码运行不暂停（避免开发者每次都被卡住）
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert cli_main._keeps_window_open() is False

    # 打包运行 + 交互式控制台 → 暂停，让用户看到报错
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": lambda self: True})())
    assert cli_main._keeps_window_open() is True

    # 显式关闭暂停开关
    monkeypatch.setenv("TH_NO_PAUSE", "1")
    assert cli_main._keeps_window_open() is False


def test_help_flag_exits_cleanly(capsys):
    import main as cli_main

    assert cli_main.main(["--help"]) == 0
    assert "12306" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 窗口模式下 stdout/stderr 为 None 时必须不崩
# --------------------------------------------------------------------------- #
def test_make_console_safe_handles_none_streams(monkeypatch):
    """回归：PyInstaller 窗口模式（console=False）下 sys.stdout 是 None，
    未加防护的 print 会抛 AttributeError，表现为「双击一闪就没」。"""
    from ticket_helper.logger import make_console_safe

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    make_console_safe()
    assert sys.stdout is not None and sys.stderr is not None
    print("这行在窗口模式下也必须能正常输出")  # 不应抛异常


def test_unknown_command_reports_error_without_traceback(capsys):
    """未知子命令由 argparse 报错，入口把退出码透传出来（不抛裸堆栈）。"""
    import main as cli_main

    with pytest.raises(SystemExit) as exc:
        cli_main.build_parser().parse_args(["definitely-not-a-command"])
    assert exc.value.code == 2

    # 走 main() 时不应把 SystemExit 冒给用户，而是返回非 0 退出码
    assert cli_main.main(["definitely-not-a-command"]) == 2
    assert "Traceback" not in capsys.readouterr().err


def test_doctor_from_source_does_not_crash_on_any_encoding(temp_home, capsys):
    """直接调用 doctor 命令，确保自检全流程不会因输出编码中断。"""
    import main as cli_main

    code = cli_main.main(["doctor"])
    out = capsys.readouterr().out
    assert "自检完成" in out
    assert code in (0, 1)


@pytest.mark.parametrize("encoding", ["gbk", "cp936", "utf-8"])
def test_cli_runs_under_restricted_console_encoding(temp_home, encoding: str):
    """用受限编码跑一次真实子进程，模拟中文 Windows 控制台。"""
    env = {
        "TICKET_HELPER_HOME": str(temp_home),
        "PYTHONIOENCODING": encoding,
        "PYTHONPATH": str(ROOT),
        "PATH": "",
    }
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "-v"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**env},
        timeout=120,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "12306" in result.stdout


# --------------------------------------------------------------------------- #
# 扫码登录子命令
# --------------------------------------------------------------------------- #
def test_qr_command_is_registered():
    """打包后的 CLI 也要能直接扫码（与网页/桌面端同一条链路）。"""
    import main as cli_main

    parser = cli_main.build_parser()
    args = parser.parse_args(["qr", "--timeout", "30", "--no-open"])
    assert args.func is cli_main.cmd_qr
    assert args.timeout == 30.0
    assert args.no_open is True


def test_qr_command_delegates_to_same_implementation(monkeypatch, capsys):
    """子命令必须复用同一套实现，避免两处逻辑各自演化。

    真机协议坑很多（appid、同源头、换票顺序），只维护一份才不会再踩。
    """
    import main as cli_main
    import scripts.real_qr_login as qr_script

    captured: dict = {}

    def fake_main(argv=None):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(qr_script, "main", fake_main)
    args = cli_main.build_parser().parse_args(["qr", "--timeout", "45"])
    assert cli_main.cmd_qr(args) == 0
    assert captured["argv"] == ["--timeout", "45.0"]


# --------------------------------------------------------------------------- #
# 扫码登录子命令
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 自检：实时通道能力（QtWebSockets 属于 PySide6-Addons，Essentials 不含）
# --------------------------------------------------------------------------- #
def test_doctor_reports_realtime_channel_when_qtwebsockets_present(temp_home, capsys):
    """装了 QtWebSockets 时，自检要明确说「实时通道可用 WebSocket 推送」。

    桌面端状态栏那句「未安装 QtWebSockets」很容易被误读成「后台没起」——
    所以自检里单列一项，把「环境缺件」和「服务没起」分开说清楚。
    """
    import argparse

    import main as cli_main

    pytest.importorskip("PySide6.QtWebSockets")
    # 返回码 0/1 都可能（临时目录里没有任务配置，属正常空态），
    # 这里只关心输出里有没有把「实时通道」讲清楚。
    cli_main.cmd_doctor(argparse.Namespace())
    out = capsys.readouterr().out
    assert "实时通道" in out, out
    assert "WebSocket" in out, out
    assert "未安装" not in out, out


def test_doctor_realtime_lines_are_gbk_encodable():
    """新增的自检文案必须能在 GBK 控制台打印（曾经因此整条自检中断）。"""
    for text in (
        "√ 实时通道        WebSocket 推送可用（QtWebSockets 已就绪）",
        "× 实时通道        未安装 QtWebSockets，将降级为轮询（功能不受影响，延迟约 2 秒；"
        "安装：pip install PySide6-Addons）",
    ):
        text.encode("gbk")  # 不抛异常即通过
