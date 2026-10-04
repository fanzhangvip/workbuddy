"""用手机 12306 App 扫码登录（真实服务端）。

用途：不打开图形界面也能扫码登录并把账号存进本地配置。

    python scripts/real_qr_login.py              # 生成二维码并等待扫码
    python scripts/real_qr_login.py --no-open    # 不自动打开图片
    python scripts/real_qr_login.py --timeout 300

与界面里的「扫码登录」完全同一条链路（同一个 ``QrLoginSession``），
区别只是把二维码写成 PNG 文件让你用手机扫。

协议要点（这一条是拿真机 trial 出来的，别改）：
``checkqr`` **必须带 ``appid=otn``**。不带它官方不会报错，而是稳定返回
``{"result_message":"操作失败","result_code":9}``，看起来像服务端故障，
实际只是少了一个参数。
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_console_safe() -> None:
    """中文 Windows 控制台是 GBK 编码，让输出容错，避免特殊符号打断脚本。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


_make_console_safe()

#: 轮询间隔（秒）。
#: 官方二维码**只有约 20 秒寿命**（真机实测，与轮询频率无关），
#: 用 1.5 秒轮询才能及时跟上自动换码。
POLL_INTERVAL = 1.5

STATE_TEXT = {
    "waiting": "等待扫码 —— 请打开手机 12306 App → 我的 → 右上角扫一扫",
    "scanned": "已扫描 —— 请在手机上点击「确认登录」",
    "confirmed": "登录成功",
    "expired": "二维码已失效",
    "failed": "登录失败",
    "unknown": "暂时无法获取状态，继续重试…",
}


def open_image(path: Path) -> None:
    """用系统默认看图工具打开二维码。失败不影响流程。"""
    try:
        import os

        os.startfile(str(path))  # type: ignore[attr-defined]
        print(f"已用系统默认程序打开二维码：{path}")
    except Exception:  # noqa: BLE001 - 打不开就手动打开，不算错误
        print(f"请手动打开二维码图片：{path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫码登录 12306（真实服务端）")
    parser.add_argument("--timeout", type=float, default=180.0, help="等待扫码的总时长（秒，默认 180）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开二维码图片")
    args = parser.parse_args(argv)

    from ticket_helper.runtime.context import AppContext
    from ticket_helper.inter.station import StationMap

    print("正在初始化（读取配置与站点表）…")
    ctx = AppContext()
    try:
        ctx.station_map = StationMap(ctx.paths).load(allow_remote=False)
        ctx.accounts.station_map = ctx.station_map

        print(f"服务端：{ctx.config.api_base}")
        print("正在向 12306 申请登录二维码…")
        status = ctx.start_qr_login()
        if not status.uuid or not status.image_base64:
            print(f"× 申请二维码失败：{status.message}")
            return 1

        target = ctx.paths.home / "qrcode-login.png"

        def save_qr(image_b64: str) -> bool:
            try:
                target.write_bytes(base64.b64decode(image_b64))
                return True
            except Exception as exc:  # noqa: BLE001
                print(f"× 二维码图片写入失败：{exc}")
                return False

        if not save_qr(status.image_base64):
            return 1
        print(f"√ 二维码已生成：{target}")
        print(f"  （官方二维码约 20 秒失效，脚本会自动换成新码并覆盖这个文件）")
        if not args.no_open:
            open_image(target)

        print(f"\n请在 {int(args.timeout)} 秒内用手机扫描（Ctrl+C 可中断）\n")
        deadline = time.time() + args.timeout
        last_state = ""
        shown_qr = status.uuid
        while time.time() < deadline:
            current = ctx.qr_login_status(status.session_id)
            if current is None:
                print("\n× 会话已结束（后台可能重启过），请重新运行本命令。")
                return 1

            # 后台自动换码 → 覆盖文件，让界面上/磁盘上的码始终是可扫的
            if current.uuid and current.uuid != shown_qr:
                shown_qr = current.uuid
                if save_qr(current.image_base64):
                    print(f"  ↻ 二维码已自动刷新（第 {current.refresh_count} 次）"
                          f"，请重新打开图片：{target}")

            if current.state != last_state:
                mark = "√" if current.state == "confirmed" else " "
                print(f"  [{mark}] {STATE_TEXT.get(current.state, current.state)}"
                      + (f"（{current.message}）" if current.message and current.state == "unknown" else ""))
                last_state = current.state

            if current.state == "confirmed":
                print(f"\n√ 登录成功，账号：{current.username}")
                print("  该账号已写入本地数据库，可在界面「账号管理」中看到（登录方式：扫码）。")
                print("  登录会话已加密留存，正常情况下重启程序不必再扫码（会话过期时才需要）。")
                return 0
            if current.state in ("expired", "failed"):
                print(f"\n× {current.message or STATE_TEXT[current.state]}")
                print("  重新运行本命令即可换一张二维码。")
                return 1
            time.sleep(POLL_INTERVAL)

        print("\n× 等待超时，二维码已失效。重新运行本命令即可。")
        return 1
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    finally:
        try:
            ctx.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
