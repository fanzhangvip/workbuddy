"""智泊AI抢票助手 · 统一入口。

用法示例::

    双击 TicketHelper.exe                # 直接打开桌面客户端（推荐）
    python main.py                       # 同上：无参数 = 启动图形界面
    python main.py desktop               # 启动 Windows 桌面客户端
    python main.py web --port 8008       # 启动 Web 后台（浏览器里用）
    python main.py init                  # 初始化配置目录与 env.py
    python main.py doctor                # 自检：配置 / 数据库 / 站点表 / 网络
    python main.py grab --task-id 1      # 命令行方式直接启动某个任务
    python main.py mock                  # 启动本地 mock-12306 服务（演练用）
    python main.py -v                    # 查看版本与命令概览
    python main.py -h                    # 查看完整帮助

定位：智泊AI学员学习工具，仅限智泊AI学员使用。仅供学习使用、严禁商用；
请勿用于代抢、黄牛倒票。官方网站：https://www.zhipoai.com/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# 让脚本在任意工作目录下都能 import 到 ticket_helper
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from ticket_helper import (  # noqa: E402
    APP_COPYRIGHT,
    APP_DISPLAY_NAME,
    APP_USAGE_NOTICE,
    APP_WEBSITE,
    __version__,
)
from ticket_helper.edition import display_train_date  # noqa: E402

log = logging.getLogger("main")

BANNER = f"""
============================================================
  {APP_DISPLAY_NAME}  v{__version__}
  {APP_COPYRIGHT}
  {APP_WEBSITE}
  {APP_USAGE_NOTICE} · 请勿用于代抢 / 黄牛倒票
============================================================
"""


def _display_width(text: str) -> int:
    """按终端显示宽度计算字符串长度（中文/全角算 2 列）。

    直接用 ``len()`` 做 ljust 会导致中文项与英文项无法对齐，
    自检输出看起来歪歪扭扭，影响可读性。
    """
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def cmd_init(args: argparse.Namespace) -> int:
    """初始化数据目录与配置文件。"""
    from ticket_helper.runtime.paths import get_app_paths

    paths = get_app_paths()
    paths.ensure_dirs()
    print(f"数据目录: {paths.home}")
    print(f"  ├─ 配置    {paths.env_file}")
    print(f"  ├─ 数据库  {paths.db_path}")
    print(f"  └─ 日志    {paths.log_dir}")

    from ticket_helper.config.crypto import SecretCipher

    cipher = SecretCipher(paths.secret_key_path)
    print(f"加密密钥: {paths.secret_key_path} (降级模式={cipher.degraded})")

    from ticket_helper.storage.database import get_database

    db = get_database(paths.db_path)
    print(f"数据库已初始化，共 {len(db.table_names())} 张表")

    from ticket_helper.runtime.context import AppContext

    ctx = AppContext(start_watchdog=False)
    print(f"配置文件已就绪: {paths.env_file}")
    auth_pass = ctx.config.web.auth_pass
    print(f"Web 后台登录: 用户名 {ctx.config.web.auth_user} / 密码 {auth_pass or '(见 env.py)'}")
    print(f"站点表: 已加载 {len(ctx.station_map)} 个站点")
    print("\n下一步：编辑 env.py 填入账号与任务，然后执行 python main.py web")
    return 0


def _check_upstream_kernel() -> tuple[str, bool, str]:
    """上游内核（`vendor/pjialin`）能不能**真的** import —— 不只是「目录在不在」。

    为什么值得单列一项：vendor 不是被静态 import 的（垫片把它挂到 `sys.path`），
    PyInstaller 看不见它。冻结后如果 `VENDOR_ROOT` 指错、或 vendor 压根没进包，
    **所有 pjialin 后端会静默回退成 self**：程序照常启动、照常抢票，
    只是打码不再走上游、滑块没人过。这种「不报错的降级」放在自检里最合适。

    判据刻意分三级，因为三级的处置完全不同：
    ① 垫片/目录 —— 「包打错了」；② `import py12306.config` —— 「依赖没收全」；
    ③ 都过了才算上游真的可用。
    """
    import sys  # noqa: PLC0415

    origin = "打包产物" if getattr(sys, "frozen", False) else "源码"
    try:
        from ticket_helper.kernel.pjialin import shim  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return ("上游内核", False, f"垫片不可用：{type(exc).__name__}: {exc}")
    if not shim.is_available():
        return (
            "上游内核",
            False,
            f"vendor 未就位（{shim.VENDOR_ROOT}）—— pjialin 后端会静默回退到 self",
        )
    try:
        moved = shim.install()
        import py12306.config  # noqa: F401, PLC0415
        import ticket_facts  # noqa: F401, PLC0415  （协议事实生成物，同样必须随包）
    except Exception as exc:  # noqa: BLE001
        return (
            "上游内核",
            False,
            f"vendor 在但 import 失败：{type(exc).__name__}: {exc}（打包时漏收了上游依赖？）",
        )
    return (
        "上游内核",
        True,
        f"py12306 已加载（{origin}；可写路径已重定向 {len(moved)} 项）",
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    """启动前自检，逐项给出可执行的结论。"""
    from ticket_helper.runtime.context import AppContext

    print(BANNER)
    ctx = AppContext(start_watchdog=False)
    checks: list[tuple[str, bool, str]] = []

    # 1) 配置
    errors = ctx.config.validate()
    checks.append(("配置校验", not errors, "通过" if not errors else "; ".join(errors)))

    # 2) 数据库
    try:
        tables = ctx.db.table_names()
        checks.append(("数据库", len(tables) >= 10, f"{ctx.paths.db_path}（{len(tables)} 张表）"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("数据库", False, str(exc)))

    # 3) 站点表
    count = len(ctx.station_map)
    checks.append(("站点表", count > 100, f"{count} 个站点"))

    # 4) 加密
    cipher = ctx.cipher
    sample = cipher.encrypt("probe")
    ok = cipher.decrypt(sample) == "probe"
    checks.append(("敏感信息加密", ok, "降级模式（建议安装 cryptography）" if cipher.degraded else "Fernet 正常"))

    # 5) 账号
    checks.append(("账号配置", ctx.accounts.count() > 0, f"{ctx.accounts.count()} 个账号"))

    # 6) 任务
    checks.append(("任务配置", len(ctx.list_tasks()) > 0, f"{len(ctx.list_tasks())} 个任务"))

    # 7) 验证码链路
    codec = ctx.verify_service.snapshot()
    usable = [c["name"] for c in codec["chain"] if c["available"]]
    detail = f"可用: {', '.join(usable) or '无'}（模式 {codec['mode']}）"
    if codec.get("manual_pull_enabled"):
        detail += "；人工兜底已启用"
    checks.append(("验证码识别源", bool(usable), detail))

    # 8) 通知渠道
    channels = [c.name for c in ctx.notifier.active_channels()]
    checks.append(("通知渠道", bool(channels), ", ".join(channels) or "无（仅本地提醒）"))

    # 9) Web 依赖
    try:
        import flask  # noqa: F401
        import flask_sock  # noqa: F401

        checks.append(("Web 依赖", True, "flask + flask-sock 就绪"))
    except ImportError as exc:
        checks.append(("Web 依赖", False, f"缺少依赖: {exc}"))

    # 10) 桌面依赖（可选）
    desktop_ok = False
    try:
        import PySide6  # noqa: F401

        desktop_ok = True
        checks.append(("桌面客户端依赖", True, f"PySide6 {PySide6.__version__}"))
    except ImportError:
        checks.append(("桌面客户端依赖", False, "未安装 PySide6（可选：pip install PySide6）"))

    # 11) 实时通道（桌面端状态栏「已连接（WebSocket）」的来源）
    # 只在装了桌面依赖时才检查 —— 没装 PySide6 就根本没有桌面端，
    # 此时报「缺 QtWebSockets」会把人引到错误方向。
    #
    # 注意：QtWebSockets **不在** PySide6-Essentials 里，它在 PySide6-Addons 里。
    # 缺它时桌面端会自动降级为轮询（功能不缺失，延迟约 2 秒），
    # 但状态栏那句「未安装 QtWebSockets」很容易被误读成后台没起，所以这里单列说清。
    if desktop_ok:
        try:
            from PySide6.QtWebSockets import QWebSocket  # noqa: F401

            checks.append(("实时通道", True, "WebSocket 推送可用（QtWebSockets 已就绪）"))
        except ImportError as exc:
            # 把真实原因带出来：打包环境下「文件在但导入失败」的情况确实存在，
            # 只写一句「未安装」会让人以为装漏了，而实际可能是 DLL 加载失败。
            checks.append(
                (
                    "实时通道",
                    False,
                    "QtWebSockets 不可用，将降级为轮询（功能不受影响，延迟约 2 秒；"
                    f"安装：pip install PySide6-Addons）。原因：{exc}",
                )
            )

    # 12) 上游内核（P5）—— 打包产物里最容易静默变坏的一环
    checks.append(_check_upstream_kernel())

    width = max(_display_width(name) for name, _, _ in checks)
    failed = 0
    for name, ok, detail in checks:
        mark = "√" if ok else "×"
        if not ok:
            failed += 1
        padding = " " * max(1, width - _display_width(name))
        print(f"  [{mark}] {name}{padding}  {detail}")

    print(f"\n自检完成：{len(checks) - failed}/{len(checks)} 项通过")
    if failed:
        print("提示：非核心项失败不影响启动，核心项（配置/数据库/站点表）失败请先修复。")
    return 0 if failed == 0 else 1


def cmd_web(args: argparse.Namespace) -> int:
    from ticket_helper.web.app import serve

    print(BANNER)
    serve(port=args.port, host=args.host)
    return 0


def cmd_desktop(args: argparse.Namespace) -> int:
    try:
        from desktop_ui.app import main as desktop_main
    except ImportError as exc:
        print(f"桌面客户端不可用：{exc}")
        print("请先安装依赖：pip install PySide6")
        return 1
    return desktop_main(port=args.port)


def cmd_qr(args: argparse.Namespace) -> int:
    """扫码登录：生成真机二维码并等待扫码，成功后账号入库。

    等价于 ``scripts/real_qr_login.py``，但打包后的 CLI 也能直接用。
    """
    from scripts.real_qr_login import main as qr_main

    argv = ["--timeout", str(args.timeout)]
    if args.no_open:
        argv.append("--no-open")
    return int(qr_main(argv))


def cmd_profile(args: argparse.Namespace) -> int:
    """在命令行查看账号档案：账号信息 / 乘车人 / 我的订单。

    界面上对应的入口是「账号管理 → 详情」，这里给不打开界面的场景用
    （服务器挂机、排查问题时最方便）。
    """
    from ticket_helper.runtime.context import AppContext

    print(BANNER)
    ctx = AppContext()
    try:
        user = ctx.accounts.get(args.user) if args.user else next(iter(ctx.accounts.online_users()), None)
        if user is None or not user.is_login:
            print("× 没有可用的已登录账号。先登录：python main.py qr")
            return 1

        payload = ctx.profile_service.fetch(
            user,
            with_passengers=True,
            with_pending=True,
            with_orders=not args.no_orders,
            where=args.where,
            page_size=args.size,
        )
        print(f"账号：{user.username}\n")

        profile = payload["profile"]
        print("【账号信息】")
        if profile["ok"]:
            data = profile["data"]
            for label, key in (
                ("账号", "username"), ("真实姓名", "real_name"), ("用户类型", "user_type"),
                ("手机", "mobile"), ("邮箱", "email"), ("证件", "id_no"),
            ):
                print(f"  {label}：{data.get(key) or '-'}")
            verified = data.get("verified")
            print("  人证核验：" + ("已核验" if verified else ("未核验" if verified is False else "无法判定")))
        else:
            print(f"  （未取到：{profile['error']}）")

        passengers = payload.get("passengers") or {}
        print(f"\n【乘车人】{'' if passengers.get('ok') else '（未取到：' + str(passengers.get('error')) + '）'}")
        for p in passengers.get("data") or []:
            flag = "（本人）" if p.get("is_self") else ""
            print(f"  {p.get('passenger_name')}{flag}  {p.get('passenger_id_no')}  {p.get('mobile_no')}")

        pending = payload.get("pending") or {}
        print(f"\n【待支付订单】{'' if pending.get('ok') else '（未取到：' + str(pending.get('error')) + '）'}")
        for order in pending.get("data") or []:
            print(f"  {order.get('sequence_no') or order.get('order_no')}  "
                  f"{order.get('station_train_code') or order.get('train_code', '')}  "
                  f"{display_train_date(order.get('train_date'))}  {order.get('pay_url', '')}")

        orders = payload.get("orders")
        if orders is not None:
            print(f"\n【我的订单（{'历史' if args.where == 'H' else '未出行'}）】"
                  f"{'' if orders.get('ok') else '（未取到：' + str(orders.get('error')) + '）'}")
            for order in (orders.get("data") or {}).get("orders", []):
                print(f"  {order.get('order_no')}  {order.get('train_code')}  "
                      f"{display_train_date(order.get('train_date'))}  "
                      f"{order.get('from_station')}→{order.get('to_station')}  "
                      f"{order.get('passenger_names')}  {order.get('price')}  {order.get('status')}")
        return 0
    finally:
        try:
            ctx.stop()
        except Exception:  # noqa: BLE001
            pass


def cmd_grab(args: argparse.Namespace) -> int:
    """命令行直接跑任务（无 UI，适合服务器挂机）。"""
    from ticket_helper.runtime.context import AppContext

    print(BANNER)
    ctx = AppContext()
    ctx.start(login=True, restore_tasks=False)

    if args.task_id:
        ctx.start_task(args.task_id)
    else:
        for task in ctx.list_tasks():
            if task.enabled:
                ctx.start_task(task.id or 0)

    print("按 Ctrl+C 停止…")
    try:
        while True:
            time.sleep(2)
            summary = ctx.scheduler.summary()
            print(
                "\r运行中 {running}/{total} | 已抢到 {success}   ".format(**summary),
                end="",
                flush=True,
            )
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        ctx.stop()
    return 0


def cmd_mock(args: argparse.Namespace) -> int:
    from mock_server.server import run_mock_server

    print(BANNER)
    print(f"mock-12306 服务启动于 http://127.0.0.1:{args.port}")
    print(f"场景注入：POST /__mock__/scenario  {{\"scenario\": \"sold_out|has_ticket|queue_fail|risk|timeout|error500|standby\"}}")
    print("把 env.py 的 api_base 指向该地址即可进行离线端到端演练。")
    run_mock_server(port=args.port, scenario=args.scenario)
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(BANNER)
    print("不带任何参数启动 = 直接打开桌面客户端（双击 exe 的效果）\n")
    print("可用子命令：")
    for name, help_text in [
        ("desktop", "启动 Windows 桌面客户端（PySide6）"),
        ("web", "启动 Web 管理后台（浏览器访问）"),
        ("qr", "扫码登录账号（手机 12306 App 扫一扫）"),
        ("profile", "查看账号信息 / 乘车人 / 我的订单"),
        ("grab", "命令行挂机抢票（无界面）"),
        ("init", "初始化数据目录与配置文件"),
        ("doctor", "启动前自检"),
        ("mock", "启动本地 mock-12306 服务（演练/测试用）"),
    ]:
        print(f"  {name.ljust(10)} {help_text}")
    print("\n数据目录：用户主目录下的 .ticket-helper（可用环境变量 TICKET_HELPER_HOME 覆盖）")
    print(f"当前数据目录：{_current_home()}")
    return 0


def _current_home() -> str:
    try:
        from ticket_helper.runtime.paths import get_app_paths

        return str(get_app_paths().home)
    except Exception:  # noqa: BLE001 - 概览信息拿不到不应影响退出码
        return "(未知)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="12306-ticket-helper",
        description=f"{APP_DISPLAY_NAME}（{APP_COPYRIGHT}）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--version", action="store_true", help="显示版本与命令概览")
    parser.add_argument(
        "-p", "--port", type=int, default=None,
        help="后台服务端口（无参数启动图形界面时生效，默认 8008）",
    )
    sub = parser.add_subparsers(dest="command")

    p_web = sub.add_parser("web", help="启动 Web 管理后台")
    p_web.add_argument("--host", default=None, help="监听地址（默认取 env.py 的 web.host）")
    p_web.add_argument("--port", type=int, default=None, help="监听端口（默认 8008）")
    p_web.set_defaults(func=cmd_web)

    p_desktop = sub.add_parser("desktop", help="启动 Windows 桌面客户端")
    p_desktop.add_argument("--port", type=int, default=None, help="后台服务端口")
    p_desktop.set_defaults(func=cmd_desktop)

    p_init = sub.add_parser("init", help="初始化数据目录与配置文件")
    p_init.set_defaults(func=cmd_init)

    p_doctor = sub.add_parser("doctor", help="启动前自检")
    p_doctor.set_defaults(func=cmd_doctor)

    p_qr = sub.add_parser("qr", help="扫码登录账号（用手机 12306 App 扫）")
    p_qr.add_argument("--timeout", type=float, default=180.0, help="等待扫码的秒数（默认 180）")
    p_qr.add_argument("--no-open", action="store_true", help="不自动打开二维码图片")
    p_qr.set_defaults(func=cmd_qr)

    p_profile = sub.add_parser("profile", help="查看账号信息 / 乘车人 / 我的订单（需账号已登录）")
    p_profile.add_argument("--user", default="", help="指定账号名（默认取第一个已登录账号）")
    p_profile.add_argument("--where", default="G", choices=["G", "H"], help="订单范围：G 未出行 / H 历史")
    p_profile.add_argument("--size", type=int, default=10, help="订单每页条数（默认 10）")
    p_profile.add_argument("--no-orders", action="store_true", help="不查订单（只拉账号信息与乘车人）")
    p_profile.set_defaults(func=cmd_profile)

    p_grab = sub.add_parser("grab", help="命令行挂机抢票")
    p_grab.add_argument("--task-id", type=int, default=None, help="只启动指定任务 id")
    p_grab.set_defaults(func=cmd_grab)

    p_mock = sub.add_parser("mock", help="启动本地 mock-12306 服务")
    p_mock.add_argument("--port", type=int, default=18006)
    p_mock.add_argument("--scenario", default="has_ticket", help="初始场景")
    p_mock.set_defaults(func=cmd_mock)
    return parser


def cmd_default(args: argparse.Namespace) -> int:
    """无参数启动（= 双击 exe / 直接运行）时的默认行为：打开图形界面。

    这是「双击即用」的唯一入口，必须选一个**看起来像在运行**的动作。
    早期版本在这里打印帮助文本然后退出，结果就是双击后黑窗一闪而过，
    用户完全不知道发生了什么，只会觉得「打开就崩了」。

    选择顺序：桌面客户端 → Web 后台（浏览器）。
    桌面客户端自带后端拉起逻辑，所以两者实际上都会把服务跑起来。
    """
    print(BANNER)
    try:
        import PySide6  # noqa: F401

        print("正在启动桌面客户端…（首次启动需拉起后台服务，约 3~10 秒）")
        return cmd_desktop(args)
    except ImportError:
        print("未安装 PySide6，无法启动桌面客户端，改为启动 Web 后台。")
        print("如需桌面客户端：pip install PySide6")
        return cmd_web(_web_fallback_args(args))


def _web_fallback_args(args: argparse.Namespace) -> argparse.Namespace:
    """为「降级到 Web」构造一份**合法**的 web 子命令参数。

    必须重新走一次同一个 parser，而不是直接复用顶层 args：
    无参数启动时产生的顶层 args **根本没有 host 属性**，直接交给 ``cmd_web``
    会抛 ``AttributeError: 'Namespace' object has no attribute 'host'``。
    这条路径真机复现过：双击启动、机器上没装 PySide6 时，
    用户看到的是「启动失败」，而本该是浏览器打开后台。

    也**不**在这里给 Namespace 动态补属性 —— 那只是把这个契约缺口藏起来，
    下次换一个参数还会以同样方式炸。
    """
    argv = ["web"]
    port = getattr(args, "port", None)
    if port:
        argv += ["--port", str(port)]
    return build_parser().parse_args(argv)


def _keeps_window_open() -> bool:
    """判断是不是「双击运行」——是的话出错时必须留住窗口，否则用户什么都看不到。"""
    if not getattr(sys, "frozen", False):
        return False
    if os.environ.get("TH_NO_PAUSE"):
        return False
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        return False


def _write_crash_log(exc: BaseException, command: str) -> Path | None:
    """把完整堆栈写到用户目录的崩溃日志里，并返回路径。"""
    from ticket_helper.runtime.crash import write_crash_log

    return write_crash_log(exc, context=f"CLI 命令: {command or '(无参数)'}", extra={"版本": __version__})


def _report_fatal(exc: BaseException, command: str) -> int:
    """入口级异常处理：给用户可读的原因 + 崩溃日志路径，并尽量留住窗口。"""
    log_path = _write_crash_log(exc, command)
    message = f"{type(exc).__name__}: {exc}"
    try:
        print(f"\n启动失败：{message}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
    if log_path is not None:
        try:
            print(f"崩溃日志：{log_path}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass

    if log_path is not None:
        logging.getLogger("main").error("启动失败: %s（崩溃日志 %s）", message, log_path, exc_info=exc)

    if _keeps_window_open():
        # 双击运行时窗口会随进程退出而关闭，必须等一下让用户看到原因
        try:
            print("\n按回车键关闭窗口…", file=sys.stderr)
            input()
        except Exception:  # noqa: BLE001
            pass
    return 1


def main(argv: list[str] | None = None) -> int:
    # 中文 Windows 控制台是 GBK、窗口模式下 stdout 可能是 None，
    # 都要先处理，否则下面的 print 自己就会崩
    from ticket_helper.logger import make_console_safe

    make_console_safe()

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_exc:
        # --help / 参数错误：argparse 已经打印过信息，直接透传退出码
        return int(exit_exc.code or 0)

    if getattr(args, "command", None) is None:
        if args.version:
            return cmd_version(args)
        # 无参数 = 双击 exe：必须启动图形界面，不能只打印帮助就退出
        return cmd_default(args)

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as exc:  # noqa: BLE001 - 入口处兜底，避免用户看到裸堆栈
        return _report_fatal(exc, str(getattr(args, "command", "")))


if __name__ == "__main__":
    raise SystemExit(main())
