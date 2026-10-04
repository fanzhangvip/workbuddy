"""下单链路真机探针（**真实 12306**）：一步一步走，每步都有判据。

为什么需要它
------------
下单六步的差异**离线看不见**：路径两边完全相同、返回值也相同，
只有「真实发出去的表单」和「官方对这一步的原始反应」能分辨。

而这条链路最容易出的错是**静默错**：官方对不合法参数基本不用 4xx，
而是回一句 ``{"status":false,"messages":["系统忙，请稍后重试"]}`` ——
「参数不对」和「真的系统忙」在日志里长得一模一样。

所以本脚本把每一步的 **请求（脱敏）+ 响应的关键字段 + 一句判据** 都打出来。
判据针对的是「请求长什么样」，不是「函数返回了什么」。

⚠️ 三个阶段，**默认只做最安全的那一段**
--------------------------------------
============================  ==============================  ===========================
``--until``                    做到哪一步                       官方侧发生什么
============================  ==============================  ===========================
``initdc``（**默认**）          submit → initDc                  锁票，**不产生待支付订单**
``check``                       再跑 checkOrderInfo              仍**不产生待支付订单**
``confirm`` / ``queue``         再跑 confirmSingleForQueue 等     **会产生一个待支付订单**
============================  ==============================  ===========================

走到 ``confirm``/``queue`` 必须显式加 ``--yes-create-order``：那一步真的占座建单，
**必须你自己去 App 支付或取消**（本工具从不代扣款）。

用法::

    # 最安全的默认：只到 initDc，看「这一单要不要过滑块」和 token 拿没拿到
    python scripts/diagnose_order.py --restore --task 2

    # 再往前一步：验证 checkOrderInfo 的参数（仍不建单）
    python scripts/diagnose_order.py --restore --until check

    # 真下单验证（会建待支付单！跑完请去 App 处理掉）
    python scripts/diagnose_order.py --restore --until queue --yes-create-order

把输出整段贴回来即可 —— 里面已脱敏，不含密钥与证件号原文。

注意：**不要在任务正在抢票时跑**，否则与任务的请求叠成突发，把短暂限流变成持续限流，
那时所有结论都不可信（与 ``diagnose_left_ticket.py`` 同一条纪律）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ticket_helper.inter import urls  # wire probe needs module-level urls

#: 输出里要脱敏的表单字段（**一律只打长度**）
SECRET_KEYS = (
    "secretStr", "REPEAT_SUBMIT_TOKEN", "sig", "sessionId", "passengerTicketStr",
    "oldPassengerStr", "leftTicketStr", "leftTicket", "key_check_isChange", "uuid",
)

STAGES = ("initdc", "check", "confirm", "queue")


def _make_console_safe() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


_make_console_safe()


def _mask(data: dict[str, Any]) -> dict[str, Any]:
    """脱敏：敏感字段只留长度，其余原样（超过 60 字符的截断）。"""
    out: dict[str, Any] = {}
    for key, value in data.items():
        text = str(value)
        if key in SECRET_KEYS:
            out[key] = f"<{len(text)} 字符>"
        elif len(text) > 60:
            out[key] = text[:57] + "…"
        else:
            out[key] = text
    return out


#: 真机取证时要逐个报告的关键字。
#: 每一个都对应一个「取不到就整条链走不通」的值 —— 报「有没有」比报「值是啥」重要。
PAGE_MARKERS = (
    "ticketInfoForPassengerForm",
    "leftTicketStr",
    "key_check_isChange",
    "train_location",
    "purpose_codes",
    "globalRepeatSubmitToken",
    "if_check_slide_passcode",
)


def _install_wire_probe(api: Any, pages: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """把每次请求的 **路径 + 脱敏表单** 记下来（HTML 响应另存进 ``pages``）。

    判据之所以要落在这一层：两套实现的路径完全相同、返回值也相同，
    只有「真实发出去的表单」能区分「切了没切」「参数对不对」。
    """
    records: list[dict[str, Any]] = []
    for name in ("get", "post"):
        original = getattr(api.client, name)

        def wrapper(path: str, *a: Any, _original: Any = original, _name: str = name, **kw: Any):
            payload = kw.get("data") or kw.get("params") or {}
            records.append({
                "method": _name.upper(),
                "path": path,
                "form": _mask(dict(payload)) if isinstance(payload, dict) else {},
                "referer": kw.get("referer") or "",
            })
            response = _original(path, *a, **kw)
            if pages is not None and path == urls.INIT_DC:
                pages[path] = str(getattr(response, "text", "") or "")
            return response

        setattr(api.client, name, wrapper)
    return records


def _report_page_forensics(pages: dict[str, str], export_dir: Path) -> None:
    """把 initDc 页面**存盘**，并逐个报告关键字的出现情况。

    ★ 为什么必须做这件事（回流 #49 的教训）
    --------------------------------------
    台账里写着「真机 initDc 页面（25964 字符）里根本没有 ticketInfoForPassengerForm
    这个词」。可那条结论的证据只是「我们的解析没匹配上」——
    「页面里真没有」与「我们没匹配上」在日志里**完全同形**，
    而它决定第 ⑧ 步会不会带空的 ``key_check_isChange`` 去建单。

    ⇒ 不猜：存页面、报关键字。一次真机跑就能定性。
    """
    html = pages.get(urls.INIT_DC, "")
    if not html:
        print("  ⚠️ 没有拿到 initDc 的响应正文，跳过页面取证")
        return
    export_dir.mkdir(parents=True, exist_ok=True)
    saved = export_dir / f"initdc-{time.strftime('%Y%m%d-%H%M%S')}.html"
    saved.write_text(html, encoding="utf-8", errors="replace")
    print(f"  页面已存盘（{len(html)} 字符）：{saved}")
    missing = []
    for marker in PAGE_MARKERS:
        hit = marker in html
        print(f"    {'✔' if hit else '✗'} {marker}")
        if not hit:
            missing.append(marker)
    if "ticketInfoForPassengerForm" in missing and "key_check_isChange" in missing:
        print("  ❗页面里既没有 ticketInfoForPassengerForm 也没有 key_check_isChange ——")
        print("    第 ⑧ 步会带**空**的 key_check_isChange 去建单；请把上面那个文件贴回来。")


def _dump_last(records: list[dict[str, Any]], *, count: int = 1) -> None:
    for item in records[-count:]:
        print(f"    → {item['method']} {item['path']}")
        print(f"      表单: {item['form']}")
        print(f"      Referer: {item['referer'] or '（不带）'}")


def _verdict(ok: bool, good: str, bad: str) -> None:
    print(f"    判据[{'通过' if ok else '不通过'}]: {good if ok else bad}")


def _seat_available(ticket: Any, seat: str) -> bool:
    """目标席别**当前可下单**吗（不是「无」，也不是只剩候补）。"""
    info = ticket.seats.get(seat)
    return bool(info is not None and info.available)


def _pick_ticket(user: Any, task: Any, seat: str) -> tuple[Any, str]:
    """查一次余票，挑一条**当前可网订、有 secretStr、且目标席别有票**的车次。

    判据同候补：``can_web_buy`` 且 ``secretStr`` 非空。停售/未起售的车次
    ``secretStr`` 是空的，拿它下单只会得到「系统忙」，把结论引向错误方向。

    ⚠️ **必须同时看目标席别**。只看「整列可否网订」是不够的 ——
    真机第一次跑 ``--until check`` 就栽在这里：选中的车次整列可网订，
    但二等座已售完，于是 ``submitOrderRequest`` 通过（锁票）、
    到 ``checkOrderInfo`` 才回「余票不足！」。
    那不是参数错，是**选车选错了**；而探针的判据会把「余票不足」
    报成「参数被拒」，把人引去查字段 —— 判据自己制造了误导。
    """
    from ticket_helper.inter.parser import SEAT_CODE

    if task is None:
        raise SystemExit("× 没找到任务，请用 --task N 指定，或先建一个任务")
    seat_code = SEAT_CODE.get(seat, "O")
    for from_station, to_station, train_date in task.combos()[:1]:
        from_code = user.api.stations.code_of(from_station)
        to_code = user.api.stations.code_of(to_station)
        print(f"  查询余票：{from_station}({from_code}) → {to_station}({to_code})  {train_date}")
        tickets = user.api.query_left_ticket(train_date, from_code, to_code)
        buyable = [t for t in tickets if t.buyable and t.secret_str]
        print(f"  取回 {len(tickets)} 条；可网订且有 secretStr 的 {len(buyable)} 条")
        orderable = [t for t in buyable if _seat_available(t, seat)]
        skipped = len(buyable) - len(orderable)
        if skipped:
            print(f"  其中 {skipped} 条「{seat}」无票或只剩候补，已跳过")
        print(f"  「{seat}」有票且可网订的 {len(orderable)} 条")
        if not orderable:
            continue
        # ★ 挑**余票最多**的那一趟，而不是第一趟。
        #
        # 这是为了「排除法」：如果连余票最充足的车次都回「余票不足！」，
        # 那基本可以断定不是「恰好售罄」，而是请求形态本身有问题。
        # 反过来，挑一趟只剩 1 张的车得到「余票不足」是**无法解读**的 ——
        # 你分不清是参数错还是真被抢光了。
        def _remain_rank(item: Any) -> tuple[int, str]:
            value = item.seats[seat].remain if seat in item.seats else ""
            text = str(value).strip()
            # 「有」= 官方不再给具体数（即 >20）：排在具体数字之后，但仍算有票
            return (int(text) if text.isdigit() else 20, item.train_code)

        orderable.sort(key=_remain_rank, reverse=True)
        print(
            "  余票最多的三趟："
            + "、".join(
                f"{t.train_code}({t.seats[seat].remain if seat in t.seats else '?'})"
                for t in orderable[:3]
            )
        )
        ticket = orderable[0]
        remain = ticket.seats[seat].remain if seat in ticket.seats else "?"
        print(f"  选定：{ticket.train_code}  {ticket.train_date}  "
              f"secretStr={'有' if ticket.secret_str else '空'}  {seat}={remain}")
        return ticket, seat_code
    raise SystemExit("× 没有查到可用于下单的车次（换 --date 或换任务）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下单链路真机探针（真实 12306）")
    parser.add_argument("--user", default="", help="指定账号名")
    parser.add_argument("--task", type=int, default=0, help="用哪个任务（默认取第一个启用任务）")
    parser.add_argument("--seat", default="", help="席别（默认取任务里的第一个）")
    parser.add_argument("--date", default="", help="覆盖乘车日期（任务日期停售时用它换一个在售日期）")
    parser.add_argument("--restore", action="store_true", help="先用磁盘会话恢复登录")
    parser.add_argument("--until", choices=STAGES, default="initdc",
                        help="做到哪一步为止（默认 initdc：锁票但不建待支付订单）")
    parser.add_argument("--passengers", type=int, default=1, help="用前几个乘车人（默认 1）")
    parser.add_argument("--yes-create-order", action="store_true",
                        help="确认「走到 confirm/queue 会真的建一个待支付订单，需自己去 App 处理」")
    args = parser.parse_args(argv)

    if args.until in ("confirm", "queue") and not args.yes_create_order:
        print("× --until confirm/queue 会在官方创建**待支付订单**（占座、需要你自己去支付或取消）。")
        print("  确认要这么做时请加：--yes-create-order")
        print("  只想验证参数：用默认的 --until initdc（锁票但不建单），或 --until check。")
        return 2

    from ticket_helper.inter import urls
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
            # ⚠️ 原来只有一句「没有可用的已登录账号。先扫码登录，或加 --restore」——
            # 在**已经加了 `--restore`** 的情况下，这句话把真因藏起来了：
            # 人会以为「恢复逻辑坏了」，而实际常常是**盘上压根没有会话文件**。
            # 三种情况对应三种完全不同的下一步，所以分开说。
            candidates = list(ctx.accounts.all())
            session_dir = ctx.paths.home / "data" / "sessions"
            if args.restore and candidates:
                with_file = [
                    c.username
                    for c in candidates
                    if getattr(c, "session_store", None) is not None
                    and c.session_store.exists(c.username)
                ]
                if not with_file:
                    print("× --restore 没有对象：盘上**没有**任何会话文件。")
                    print(f"  会话目录：{session_dir}（空）")
                    print("  下一步（只需一次，之后 3 天内 --restore 都能用）：")
                    print("    python scripts/real_qr_login.py --no-open")
                    return 1
                print(f"× 会话文件在（{', '.join(with_file)}），但已失效：过期 / 换了服务端 / 密钥变了。")
                print("  （失效的会话会被自动清除，不会每次启动都拿同一个死会话去试。）")
                print("  下一步： python scripts/real_qr_login.py --no-open")
                return 1
            print("× 没有可用的已登录账号。先扫码登录，或加 --restore。")
            return 1

        all_tasks = ctx.list_tasks()
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
        print(f"账号：{user.username}    任务：{getattr(task, 'name', '-')}    席别：{seat}")
        print(f"做到：--until {args.until}")

        api = user.api
        pages: dict[str, str] = {}
        records = _install_wire_probe(api, pages)

        print("\n① 会话预热（真机上不预热必被 WAF 拒 → 回 HTML 错误页）")
        print(f"  ensure_session_warm → {api.ensure_session_warm()}")
        time.sleep(0.5)

        print("\n② 取一条真车次")
        ticket, seat_code = _pick_ticket(user, task, seat)

        print("\n③ submitOrderRequest（锁票；**不建待支付单**）")
        try:
            ok = api.submit_order_request(
                ticket, from_name=ticket.from_station, to_name=ticket.to_station
            )
            _dump_last(records)
            _verdict(
                ok,
                "官方接受了这一单（锁票成功）",
                "官方拒绝 → 看 messages：'系统忙' 多为参数/会话问题。"
                "对照 scripts/order_diff.py 的字段与取值来源",
            )
        except Exception as exc:  # noqa: BLE001
            _dump_last(records)
            print(f"  ✗ {type(exc).__name__}: {exc}")
            print("  判据[不通过]: 第一步没过，后面不用看了")
            return 1

        print("\n④ initDc（拿 REPEAT_SUBMIT_TOKEN / ticketInfoForPassengerForm / 滑块标志）")
        dc = api.init_dc()
        _dump_last(records)
        token = dc.get("repeat_submit_token", "")
        info = dc.get("ticket_info") or {}
        print(f"  repeat_submit_token: {'有（%d 字符）' % len(token) if token else '★ 空'}")
        print(f"  ticketInfoForPassengerForm 的键: {sorted(info)}")
        print(f"  ★ 这一单要不要过滑块: {dc.get('needs_slide')}")
        if dc.get("needs_slide"):
            solved = bool(api._slide_values)  # noqa: SLF001 - 探针就是要看内部状态
            print(f"  滑块结果: {'已拿到 sessionId/sig' if solved else '★ 没拿到（看上面的 WARNING 说明）'}")
        _verdict(
            bool(token),
            "token 拿到了（后面每一步都要它）",
            "token 是空的 → initDc 没被接受（会话 / Referer / 滑块）",
        )
        _report_page_forensics(pages, ctx.paths.export_dir)

        if args.until == "initdc":
            print("\n──────── 到此为止（--until initdc）────────")
            print("已在官方锁票，但**没有创建待支付订单**；锁票会自行过期，也可以去 App 取消。")
            print("继续验证 checkOrderInfo（仍不建单）：--until check")
            return 0

        print("\n⑤ 乘车人（必须在 initDc 之后取：要带 token）")
        passengers = api.get_passengers()[: max(1, args.passengers)]
        names = [str(p.get("passenger_name", "")) for p in passengers]
        print(f"  取回 {len(passengers)} 人：{names}")
        _verdict(bool(passengers), "拿到乘车人（token 也被官方认可）",
                 "一个都没拿到 → 看 initDc 那步的 token")

        print("\n⑥ checkOrderInfo（校验订单参数；**不建单**）")
        try:
            api.check_order_info(passengers, seat_code, token=token)
            _dump_last(records)
            _verdict(True, "官方接受了订单参数", "-")
        except Exception as exc:  # noqa: BLE001
            _dump_last(records)
            print(f"  ✗ {type(exc).__name__}: {exc}")
            print("  判据[不通过]: 参数被拒。要滑块却没过，或字段/取值来源与官方不符")
            return 1

        print("\n⑦ getQueueCount（排队人数）")
        queue_info = api.get_queue_count(ticket, seat_code, token=token)
        _dump_last(records)
        print(f"  data: {queue_info}")
        _verdict(isinstance(queue_info, dict) and "count" in queue_info,
                 "拿到排队数据", "没有 count → 参数或会话问题")

        if args.until == "check":
            print("\n──────── 到此为止（--until check）────────")
            print("仍未创建待支付订单。要继续（**会建单**）：--until queue --yes-create-order")
            return 0

        print("\n⑧ confirmSingleForQueue（★ 这一步才真的建单）")
        key_check = (api.ticket_info or {}).get("key_check_isChange") or ""
        left_ticket = (api.ticket_info or {}).get("leftTicketStr") or ticket.yp_info
        try:
            api.confirm_single_for_queue(
                passengers, seat_code, ticket,
                key_check_is_change=key_check, left_ticket=left_ticket, token=token,
            )
            _dump_last(records)
            _verdict(True, "已进入排队（订单已创建，待支付）", "-")
        except Exception as exc:  # noqa: BLE001
            _dump_last(records)
            print(f"  ✗ {type(exc).__name__}: {exc}")
            print("  判据[不通过]: 「余票不足」属正常竞争；若仍是「系统忙」，")
            print("            重点回看 ⑥ checkOrderInfo 是不是被官方悄悄降级了")
            return 1

        print("\n⑨ queryOrderWaitTime（等到 orderId 就是出票）")
        result = api.query_order_wait_time(token=token, max_polls=10)
        _dump_last(records)
        print(f"  status={result.status}  order_id={result.order_id or '（还没给）'}  "
              f"wait_count={result.wait_count}  message={result.message}")
        if result.order_id:
            print("  支付地址（**本工具不代扣款，请自己去官方支付或取消**）："
                  f"{api.pay_url_of(result.order_id)}")
            _verdict(True, "出票成功，订单号已拿到", "-")
        else:
            print("  判据[待定]: 还没拿到 orderId。订单可能仍在服务端排队 ——")
            print("            去「未完成订单」里确认，**不要**当作失败重试")
        print("\n⚠️ 若真的建了单：请立刻在 12306 App 里**支付或取消**，本工具不会替你处理。")
        return 0
    finally:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
