"""个人中心接口诊断（**真实 12306**）：把原始响应打出来。

为什么要这么个东西
------------------
账号信息 / 乘车人 / 我的订单这三个接口的**字段名在不同版本里并不统一**，
而解析层是按「候选键列表」写的 —— 一旦真机换名，表现是「界面上某一列整片空白」，
从界面上根本看不出是官方改字段了。

本脚本直接把原始 JSON 落盘并打印键名，用来：
1. 首次接入时确认字段假设；
2. 日后某列突然空白时，一分钟判断是官方改字段，还是自己的问题。

    python scripts/diagnose_profile.py                 # 用当前已登录的账号
    python scripts/diagnose_profile.py --user 张三      # 指定账号
    python scripts/diagnose_profile.py --no-orders      # 跳过订单接口

输出写到用户目录（默认 ``%USERPROFILE%\\.ticket-helper``）下的
``diagnose-profile-*.json``。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_console_safe() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


_make_console_safe()


def _keys_of(payload, limit: int = 40) -> str:
    """列出响应里前几层的键名，用来快速判断「字段改名了没有」。"""
    if not isinstance(payload, dict):
        return f"<{type(payload).__name__}>"
    keys = list(payload)
    return ", ".join(keys[:limit]) + (" …" if len(keys) > limit else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="个人中心接口诊断（真实 12306）")
    parser.add_argument("--user", default="", help="指定账号名（默认取第一个已登录账号）")
    parser.add_argument("--no-orders", action="store_true", help="跳过订单接口")
    parser.add_argument("--where", default="G", choices=["G", "H"], help="订单范围：G 未出行 / H 历史")
    parser.add_argument(
        "--restore",
        action="store_true",
        help="先用磁盘上留存的会话恢复登录（不必重新扫码）",
    )
    args = parser.parse_args(argv)

    from ticket_helper.runtime.context import AppContext

    print("正在初始化（读取配置与站点表）…")
    ctx = AppContext()
    try:
        user = ctx.accounts.get(args.user) if args.user else next(iter(ctx.accounts.online_users()), None)
        if user is None and args.restore:
            # 只走「恢复磁盘会话」这一步，**不去碰密码登录** ——
            # 真机上密码登录走不通，硬试只会白等一轮验证码超时。
            for candidate in ctx.accounts.all():
                try:
                    if candidate.restore_session():
                        user = candidate
                        print(f"已用磁盘会话恢复账号「{candidate.username}」的登录态")
                        break
                except Exception as exc:  # noqa: BLE001
                    print(f"  恢复 {candidate.username} 的会话失败：{exc}")
        if user is None:
            print("× 没有可用的已登录账号。")
            print("  请先登录：python main.py qr      （扫码登录，最可靠）")
            print("           或 python scripts/real_qr_login.py")
            print("           或加 --restore 试试用上次留下的会话")
            return 1
        if not user.is_login:
            print(f"× 账号 {user.username} 当前不在线，请先登录。")
            return 1

        print(f"服务端：{ctx.config.api_base}")
        print(f"账号：{user.username}\n")

        report: dict[str, object] = {"account": user.username, "api_base": ctx.config.api_base}

        # ---- 1) 账号信息 ---- #
        print("=" * 72)
        print("① 账号信息  POST /otn/index/initMy12306Api")
        try:
            raw = _raw_get(user)
            report["profile_raw"] = raw
            data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
            print(f"  顶层键：{_keys_of(raw)}")
            print(f"  data 键：{_keys_of(data)}")
            normalized = user.api.get_user_profile()
            print(f"  归一化：{json.dumps(normalized, ensure_ascii=False)}")
            report["profile_normalized"] = normalized
        except Exception as exc:  # noqa: BLE001
            print(f"  × 失败：{exc}")
            report["profile_error"] = str(exc)

        # ---- 2) 乘车人 ---- #
        print("=" * 72)
        print("② 乘车人  POST /otn/passengers/query")
        try:
            raw = _raw_post(user, "/otn/passengers/query",
                            {"pageIndex": 1, "pageSize": 50, "_json_att": ""},
                            referer="/otn/passengers/init")
            report["passengers_raw"] = raw
            body = raw.get("data") if isinstance(raw.get("data"), dict) else {}
            rows = body.get("datas") or body.get("normal_passengers") or []
            print(f"  外层键：{_keys_of(raw)} / data 键：{_keys_of(body)}")
            print(f"  条数：{len(rows)}  首条键：{_keys_of(rows[0]) if rows else '<空>'}")
            normalized = user.api.query_passengers()
            print(f"  归一化（证件号已打码）：{json.dumps(normalized, ensure_ascii=False)}")
            report["passengers_normalized"] = normalized
        except Exception as exc:  # noqa: BLE001
            print(f"  × 失败：{exc}")
            report["passengers_error"] = str(exc)

        # ---- 3) 我的订单 ---- #
        if not args.no_orders:
            print("=" * 72)
            print(f"③ 我的订单  POST /otn/queryOrder/queryMyOrder  (where={args.where})")
            try:
                yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
                today = time.strftime("%Y-%m-%d", time.localtime())
                end = yesterday if args.where == "H" else today
                raw = _raw_post(
                    user,
                    "/otn/queryOrder/queryMyOrder",
                    {
                        "queryType": 1,
                        "queryStartDate": time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400)),
                        "queryEndDate": end,
                        "come_from_flag": "my_order",
                        "pageSize": 5,
                        "pageIndex": 1,
                        "query_where": args.where,
                        "sequeue_train_name": "",
                        "searched_phone": "",
                        "_json_att": "",
                    },
                    referer="/otn/queryOrder/queryMyOrderNoComplete",
                )
                report["orders_raw"] = raw
                body = raw.get("data") if isinstance(raw.get("data"), dict) else {}
                rows = body.get("OrderDTODataList") or []
                print(f"  外层键：{_keys_of(raw)} / data 键：{_keys_of(body)}")
                # 官方说名下一共几单 —— 判断「真没订单」还是「解析落空」的关键数字
                print(f"  order_total_number：{body.get('order_total_number')!r}")
                print(f"  条数：{len(rows)}  首条键：{_keys_of(rows[0]) if rows else '<空>'}")
                if rows and isinstance(rows[0].get("passengerDTOs"), list) and rows[0]["passengerDTOs"]:
                    print(f"  乘客键：{_keys_of(rows[0]['passengerDTOs'][0])}")
                normalized = user.api.query_order_history(where=args.where, page_size=5)
                print(f"  归一化：{json.dumps(normalized, ensure_ascii=False)[:1200]}")
                report["orders_normalized"] = normalized
            except Exception as exc:  # noqa: BLE001
                print(f"  × 失败：{exc}")
                report["orders_error"] = str(exc)

        target = ctx.paths.home / f"diagnose-profile-{int(time.time())}.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("=" * 72)
        print(f"原始响应已写入：{target}")
        return 0
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    finally:
        try:
            ctx.stop()
        except Exception:  # noqa: BLE001
            pass


def _raw_get(user):
    """拉一次账号信息并拿回原始 JSON（绕过解析层的人工映射）。"""
    from ticket_helper.inter.base import expect_json

    response = user.client.get(
        "/otn/index/initMy12306Api",
        params={"_json_att": ""},
        allow_retry=False,
        referer=user.client.url("/otn/index/initMy12306"),
    )
    return expect_json(response, allow_empty=True) or {}


def _raw_post(user, path: str, data: dict, *, referer: str):
    from ticket_helper.inter.base import expect_json

    response = user.client.post(
        path, data=data, allow_retry=False, referer=user.client.url(referer)
    )
    return expect_json(response, allow_empty=True) or {}


if __name__ == "__main__":
    raise SystemExit(main())
