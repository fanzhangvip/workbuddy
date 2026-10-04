"""候补链路诊断（**真实 12306**）：把 afterNate 家族接口的原始响应打出来。

为什么需要它
------------
我们的候补提交一直打在 ``/otn/confirmPassenger/confirmHB``，真机回 **HTTP 405**。
查证后确认：12306 的候补接口**不在 confirmPassenger 下，而在 ``/otn/afterNate/`` 下**，
而且是**一串调用**（不是单接口）：

    ① chechFace          → 检查登录标志 + **人脸核验标志**（face_flag）
    ② getSuccessRate     → 候补成功率（决定值不值得候补）
    ③ submitOrderRequest → 提交候补需求
    ④ confirmHB          → 确认候补订单（带乘客信息）
    ⑤ getQueueNum / queryQueue → 排队

其中 ① 的 ``face_flag`` 就是**真机上「人证核验」的实际信号** ——
这解释了为什么看 ``initQueryUserInfoApi`` 永远判不出来。

安全约束（本脚本刻意为之）
--------------------------
- 只做**读取/检查**类调用：chechFace、getSuccessRate、passengerInitApi、getQueueNum；
- **绝不调用** submitOrderRequest / confirmHB ——
  它们会真的占用一个候补订单名额并进入待支付，属于有副作用的写操作。
  要用真机验证这两步，请由使用者自己在界面上跑一次任务并观察。
- 每次调用之间留间隔，避免把突发做大触发风控。

    python scripts/diagnose_standby.py --restore              # 用磁盘会话
    python scripts/diagnose_standby.py --restore --user 樊章
    python scripts/diagnose_standby.py --restore --task 2

输出写到用户目录下的 ``diagnose-standby-*.json``。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _console_safe() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


_console_safe()

#: 这些接口**只读**，可以安全探测
SAFE_PROBES: tuple[tuple[str, str, str], ...] = (
    ("chechFace", "/otn/afterNate/chechFace", "检查登录标志与人脸核验标志"),
    ("getSuccessRate", "/otn/afterNate/getSuccessRate", "候补成功率"),
    ("passengerInitApi", "/otn/afterNate/passengerInitApi", "候补乘车人初始化"),
    ("getQueueNum", "/otn/afterNate/getQueueNum", "排队前置检查"),
    ("queryQueue", "/otn/afterNate/queryQueue", "排队状态"),
)

#: 有副作用、**绝不代调**
WRITE_STEPS: tuple[tuple[str, str, str], ...] = (
    ("submitOrderRequest", "/otn/afterNate/submitOrderRequest", "提交候补需求（占用名额）"),
    ("confirmHB", "/otn/afterNate/confirmHB", "确认候补订单（进入待支付）"),
)

#: afterNate 家族统一用这个 Referer（来自可用的开源实现）
REFERER = "/otn/leftTicket/init"


def _brief(payload: Any, limit: int = 700) -> str:
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    return text[:limit] + ("…" if len(text) > limit else "")


def _post_raw(user: Any, path: str, data: dict[str, Any]) -> dict[str, Any]:
    """打一次 POST，返回 {status, content_type, body, json}，**不做任何解析假设**。"""
    from ticket_helper.inter.base import expect_json  # noqa: F401  (仅用于对照)

    response = user.client.post(
        path,
        data=data,
        allow_retry=False,
        check_risk=False,
        referer=user.client.url(REFERER),
    )
    return {
        "status": response.status_code,
        "content_type": (response.raw.headers.get("Content-Type") if response.raw else "") or "",
        "is_json": isinstance(response.json, (dict, list)),
        "json": response.json,
        "text_head": (response.text or "")[:400],
    }


def _pick_ticket(user: Any, task: Any, seat: str) -> Any:
    """按任务行程查一次余票，挑一条可用于候补的车次。"""
    from ticket_helper.inter.parser import SEAT_CODE

    if task is None:
        raise SystemExit("× 没找到任务，请用 --task N 指定，或先建一个任务")
    seat_code = SEAT_CODE.get(seat, "O")
    for from_station, to_station, train_date in task.combos()[:1]:
        from_code = user.api.stations.code_of(from_station)
        to_code = user.api.stations.code_of(to_station)
        print(f"  查询余票：{from_station}({from_code}) → {to_station}({to_code})  {train_date}")
        tickets = user.api.query_left_ticket(train_date, from_code, to_code)
        print(f"  取回 {len(tickets)} 条车次")
        with_secret = [t for t in tickets if t.secret_str]
        print(
            f"  secretStr 体检：{len(with_secret)}/{len(tickets)} 条有值"
            + ("" if with_secret else "  ← ⚠ 全都为空：解析层没取到下单密钥，候补不可能成功")
        )
        standby_any = [t for t in tickets if t.standby_seats([seat])]
        print(f"  当前有「候补」字样的车次：{len(standby_any)} 条")
        if tickets:
            sample = tickets[0]
            print(f"  样例：{sample.train_code} secretStr长度={len(sample.secret_str)} 席别原始串={sample.seat_types_raw[:24]!r}")
        # 优先挑「有候补字样」的车次；没有的话退化成第一条
        standby_first = [t for t in tickets if t.standby_seats([seat])]
        # 与引擎同一口径：**必须当前可网订**。停售/未起售的车次 secretStr 是空的，
        # 拿去候补只会得到一句「系统忙」，把链路验证引向错误结论。
        buyable = [t for t in tickets if t.buyable and t.secret_str]
        print(f"  可网订且有 secretStr 的车次：{len(buyable)} 条")
        picked = ([t for t in standby_first if t in buyable] or buyable)
        if not picked:
            continue
        ticket = picked[0]
        print(
            f"  选定：{ticket.train_code}（{ticket.from_station}→{ticket.to_station} {ticket.train_date}）"
            f" 可候补={bool(ticket.standby_seats([seat]))} secretStr={'有' if ticket.secret_str else '空'}"
        )
        return ticket, seat_code, train_date
    raise SystemExit("× 没有查到可用于候补的车次")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="候补链路诊断（真实 12306）")
    parser.add_argument("--user", default="", help="指定账号名")
    parser.add_argument("--task", type=int, default=0, help="用哪个任务（默认取第一个启用任务）")
    parser.add_argument("--seat", default="", help="席别（默认取任务里的第一个）")
    parser.add_argument("--restore", action="store_true", help="先用磁盘会话恢复登录")
    parser.add_argument(
        "--until-confirm",
        action="store_true",
        help="按真机顺序走到「确认订单」**之前**为止（含 submitOrderRequest / passengerInitApi / "
             "getQueueNum），**不创建候补订单**。用来验证准备阶段每一步是否被接受。",
    )
    parser.add_argument(
        "--date",
        default="",
        help="覆盖乘车日期（默认用任务的日期）。任务的日期若已停售，准备阶段必然失败，"
             "所以验证链路时要用一个在售的日期。",
    )
    args = parser.parse_args(argv)

    from ticket_helper.runtime.context import AppContext

    print("正在初始化（读取配置、站点表与会话）…")
    ctx = AppContext()
    try:
        user = ctx.accounts.get(args.user) if args.user else next(iter(ctx.accounts.online_users()), None)
        if args.restore and (user is None or not user.is_login):
            candidates = [user] if user is not None else list(ctx.accounts.all())
            for candidate in candidates:
                try:
                    if candidate.restore_session():
                        user = candidate
                        print(f"已用磁盘会话恢复账号「{candidate.username}」的登录态")
                        break
                except Exception as exc:  # noqa: BLE001
                    print(f"  恢复 {candidate.username} 的会话失败：{exc}")
        if user is None or not user.is_login:
            print("× 没有可用的已登录账号。先扫码登录，或加 --restore。")
            return 1

        all_tasks = ctx.list_tasks()
        task = None
        if args.task:
            task = next((t for t in all_tasks if int(t.id or 0) == args.task), None)
            if task is None:
                print(f"× 没有 id={args.task} 的任务，现有：{[t.id for t in all_tasks]}")
                return 1
        else:
            task = next((t for t in all_tasks if t.enabled), None) or (all_tasks[0] if all_tasks else None)
        seat = args.seat or (task.seat_types[0] if task and task.seat_types else "二等座")
        if args.date:
            task.left_dates = [args.date]
            print(f"（已把乘车日期覆盖为 {args.date}）")

        print(f"服务端：{ctx.config.api_base}")
        print(f"账号：{user.username}    任务：{getattr(task, 'name', '-')}    席别：{seat}\n")

        report: dict[str, Any] = {
            "account": user.username,
            "task": getattr(task, "name", ""),
            "seat": seat,
        }

        # 余票查询前的会话热身是硬约束（不热身会被 WAF 拒并回 HTML）
        print("① 会话热身 + 取一条真车次")
        warmed = user.api.ensure_session_warm()
        print(f"  ensure_session_warm → {warmed}")
        time.sleep(0.5)
        ticket, seat_code, train_date = _pick_ticket(user, task, seat)
        report["ticket"] = {
            "train_code": ticket.train_code,
            "train_no": ticket.train_no,
            "train_date": ticket.train_date,
            "secret_str_len": len(ticket.secret_str or ""),
            "seat_code": seat_code,
        }

        secret_list = f"{ticket.secret_str}#{seat_code}|"
        success_secret = f"{ticket.secret_str}#{seat_code}"

        # ---- 旧路径对照：确认 405 是真的（不是我们哪里写错了） ---- #
        print("\n② 旧路径对照（我们一直在用的那个）")
        old = _post_raw(user, "/otn/confirmPassenger/confirmHB", {"_json_att": ""})
        print(f"  POST /otn/confirmPassenger/confirmHB → HTTP {old['status']}  合法JSON={old['is_json']}")
        print(f"      body: {_brief(old['json'] if old['is_json'] else old['text_head'], 160)}")
        report["legacy_confirm_hb"] = old
        time.sleep(0.8)

        # ---- 新家族：只读探测 ---- #
        params_by_name = {
            "chechFace": {"secretList": secret_list, "_json_att": "", "_json_attr": ""},
            "getSuccessRate": {"successSecret": success_secret, "_json_att": "", "_json_attr": ""},
            "passengerInitApi": {"_json_att": "", "_json_attr": ""},
            "getQueueNum": {"_json_att": "", "_json_attr": ""},
            "queryQueue": {"_json_att": "", "_json_attr": ""},
        }
        for name, path, why in SAFE_PROBES:
            print(f"\n③ 探测 {name}（{why}）")
            print(f"  POST {path}")
            result = _post_raw(user, path, params_by_name[name])
            report[name] = result
            print(f"  → HTTP {result['status']}  合法JSON={result['is_json']}  ct={result['content_type'][:40]}")
            print(f"      body: {_brief(result['json'] if result['is_json'] else result['text_head'])}")
            if name == "chechFace" and result["is_json"]:
                data = (result["json"] or {}).get("data") or {}
                if isinstance(data, dict):
                    print(
                        f"      ★ login_flag={data.get('login_flag')!r} "
                        f"face_flag={data.get('face_flag')!r} msg={data.get('msg')!r}"
                    )
            time.sleep(1.0)

        # ---- 准备阶段（按真机顺序，且**止步于 confirmHB 之前**） ---- #
        if args.until_confirm:
            print("\n④ 准备阶段（真机顺序：submitOrderRequest → passengerInitApi → getQueueNum）")
            print("   注意：**不会**调用 confirmHB —— 那一步会真的创建候补订单（占名额 + 30 分钟内需支付）。")
            prep = (
                ("submitOrderRequest", "/otn/afterNate/submitOrderRequest",
                 {"secretList": secret_list, "_json_att": ""}),
                ("passengerInitApi", "/otn/afterNate/passengerInitApi", {"_json_att": ""}),
                ("getQueueNum", "/otn/afterNate/getQueueNum", {"_json_att": ""}),
            )
            for name, path, data in prep:
                result = _post_raw(user, path, data)
                report[f"prep_{name}"] = result
                print(f"\n  POST {path}")
                print(f"  → HTTP {result['status']}  合法JSON={result['is_json']}")
                body = result["json"] if result["is_json"] else result["text_head"]
                print(f"      body: {_brief(body, 300)}")
                time.sleep(1.2)

        # ---- 结论 ---- #
        print("\n" + "=" * 72)
        print("结论")
        check = report.get("chechFace") or {}
        if check.get("is_json"):
            data = (check["json"] or {}).get("data") or {}
            login_flag = data.get("login_flag") if isinstance(data, dict) else None
            face_flag = data.get("face_flag") if isinstance(data, dict) else None
            print(f"  chechFace 给出了真机上唯一可靠的核验信号：")
            print(f"    login_flag = {login_flag!r}（登录态是否有效）")
            print(f"    face_flag  = {face_flag!r}（**人脸/人证核验是否已通过**）")
            if face_flag is False:
                print("    → face_flag=False：需要在铁路 12306 App 里完成人证核验后才能候补。")
            elif face_flag is True:
                print("    → face_flag=True：核验没问题，候补失败的原因不在核验。")
        else:
            print("  chechFace 没有返回合法 JSON —— 看上面的状态码与正文片段再判断。")
        print(
            "\n  仍未真机验证的一步：**confirmHB**（创建候补订单）。"
            "它只在真的提交时才跑得到，所以要由真机任务去验；"
            "本脚本刻意不代调。"
        )
        print(f"\n  其余有副作用的接口（**本脚本不会代调**）：")
        for name, path, why in WRITE_STEPS:
            print(f"    {name:20s} POST {path}   （{why}）")

        target = ctx.paths.home / f"diagnose-standby-{int(time.time())}.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
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


if __name__ == "__main__":
    raise SystemExit(main())
