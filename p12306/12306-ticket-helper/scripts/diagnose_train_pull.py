"""车次拉取诊断（**真实 12306**）：把「从 12306 拉车次」这条链路逐步打出来。

为什么需要它
------------
「拉不到车次」至少有五种完全不同的原因，但症状都是**一个空列表**：

============================  ================================================
现象                          真正的处理动作
============================  ================================================
没有在线账号                  去账号管理登录（请求根本没发出去）
会话失效 / 被风控             官方回的是 **HTML 错误页**（HTTP 200，不是 4xx），
                              要降频重试，而不是换路径再来一遍
站点名与官方不一致            电报码解析失败 → 换正确的站名
那天确实没车 / 日期已过        换日期，程序没问题
候补路径全被下线              换候选路径或更新 urls.py
============================  ================================================

界面上这几种都只是「没拉到」，分不出是哪一种。本脚本直接走一遍真实链路，
把每一步的**请求路径、HTTP 状态、返回是 JSON 还是 HTML、解析出几条**按顺序打出来，
一次就能看出卡在哪一步。

用法::

    python scripts/diagnose_train_pull.py                  # 用任务表里的第一个任务
    python scripts/diagnose_train_pull.py --task 2
    python scripts/diagnose_train_pull.py --dates 2026-10-01 --from 广州 --to 长沙
    python scripts/diagnose_train_pull.py --login           # 没有在线账号时尝试登录（可能要人工过验证码）

注意：这是**真实 12306**，只做低频人工排查用，不要放进自动化流程反复跑。
"""

from __future__ import annotations

import argparse
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


def _step(title: str) -> None:
    print(f"\n=== {title} ===")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="车次拉取链路诊断（真实 12306）")
    parser.add_argument("--task", type=int, default=0, help="用该任务的日期/站点（默认取第一个任务）")
    parser.add_argument("--dates", default="", help="乘车日期，逗号分隔")
    parser.add_argument("--from", dest="from_stations", default="", help="出发站，逗号分隔")
    parser.add_argument("--to", dest="to_stations", default="", help="到达站，逗号分隔")
    parser.add_argument("--login", action="store_true", help="没有在线账号时尝试登录（可能等待人工验证码）")
    parser.add_argument(
        "--no-anonymous",
        action="store_true",
        help="没有在线账号时**不**用匿名会话（默认会用，因为余票查询不要求登录态）",
    )
    parser.add_argument("--home", default="", help="数据目录（默认 %USERPROFILE%\\.ticket-helper）")
    args = parser.parse_args(argv)

    from ticket_helper.inter.api import TicketApi
    from ticket_helper.inter.base import HttpClient
    from ticket_helper.runtime.context import AppContext
    from ticket_helper.storage.repository import TaskRepository

    started = time.time()
    ctx = AppContext(home=Path(args.home) if args.home else None, start_watchdog=False)

    _step("0. 环境")
    print(f"数据目录   : {ctx.paths.home}")
    print(f"接口基址   : {ctx.config.api_base}   （mock 时这里会是 127.0.0.1:xxxx）")
    print(f"站点表     : {len(ctx.station_map)} 个站")

    # ------------------------------------------------------------------ #
    _step("1. 账号")
    users = ctx.accounts.all()
    if not users:
        print("（一个账号都没有）")
    for user in users:
        print(
            f"- {user.username}: enabled={user.enabled} 在线={bool(user.is_login)} "
            f"会话已留存={bool(getattr(user, 'session_saved', False))} "
            f"登录方式={'密码' if getattr(user, 'has_password', True) else '扫码'}"
        )
    online = [u for u in ctx.accounts.online_users() if getattr(u, "api", None) is not None]
    if not online and args.login:
        print("\n没有在线账号，尝试登录（验证码可能需要人工参与，最长约 30 秒）…")
        try:
            ctx.accounts.login_all(parallel=False)
        except Exception as exc:  # noqa: BLE001
            print(f"登录过程异常: {exc}")
        online = [u for u in ctx.accounts.online_users() if getattr(u, "api", None) is not None]

    user = online[0] if online else None
    if user is not None:
        print(f"\n用账号「{user.username}」继续（在线 {len(online)} 个）")
    elif args.no_anonymous:
        print("\n>>> 没有在线账号，且指定了 --no-anonymous，到此为止。")
        return 2
    else:
        print("\n没有在线账号 —— 改用**匿名会话**继续（余票查询不要求登录态，界面也是这么做的）")

    # ------------------------------------------------------------------ #
    _step("2. 取参数（哪条线路 / 哪天）")
    dates, from_stations, to_stations = args.dates, args.from_stations, args.to_stations
    if not (dates and from_stations and to_stations):
        tasks = TaskRepository(ctx.db).list()
        task = next((t for t in tasks if t.id == args.task), None) if args.task else (tasks[0] if tasks else None)
        if task is None:
            print("任务表里没有任务，也没有传 --dates/--from/--to，无法继续")
            return 2
        print(f"取自任务 #{task.id}「{task.name}」")
        dates = ",".join(task.left_dates)
        from_stations = ",".join(task.from_stations)
        to_stations = ",".join(task.to_stations)
    date_list = [d.strip() for d in dates.replace("，", ",").split(",") if d.strip()]
    from_list = [s.strip() for s in from_stations.replace("，", ",").split(",") if s.strip()]
    to_list = [s.strip() for s in to_stations.replace("，", ",").split(",") if s.strip()]
    print(f"日期: {date_list}")
    print(f"出发站: {from_list}")
    print(f"到达站: {to_list}")

    _step("3. 站名 → 电报码")
    codes_ok = True
    for name in from_list + to_list:
        try:
            print(f"- {name} → {ctx.station_map.code_of(name)}")
        except Exception as exc:  # noqa: BLE001
            codes_ok = False
            print(f"- {name} → 解析失败: {exc}")
    if not codes_ok:
        print("\n>>> 结论：站名与官方站点表不一致。用界面上的「搜索站点」重新选一次即可。")
        return 3

    # ------------------------------------------------------------------ #
    from ticket_helper.inter import urls

    #: 有在线账号就用它；--anonymous 时另起一个**干净会话**（无任何 Cookie），
    #: 用来验证「未登录能不能查余票」—— 这决定了任务配置页要不要强制登录。
    if user is not None:
        api = user.api
        print(f"\n会话来源: 账号「{user.username}」")
    else:
        api = TicketApi(HttpClient(base_url=ctx.config.api_base, retries=1), ctx.station_map)
        print("\n会话来源: **匿名**（无 Cookie）")

    train_date = date_list[0]
    from_code = ctx.station_map.code_of(from_list[0])
    to_code = ctx.station_map.code_of(to_list[0])

    _step("4. 会话预热（真机上不预热必被 WAF 拒）")
    t0 = time.time()
    try:
        sent = api.ensure_session_warm(force=True)
        print(f"- 已发送预热请求: {sent}（{int((time.time() - t0) * 1000)}ms）")
    except Exception as exc:  # noqa: BLE001
        print(f"- 预热异常: {exc}")

    _step("5. 原始探针：看官方到底回的是 JSON 还是 HTML")
    params = {
        "leftTicketDTO.train_date": train_date,
        "leftTicketDTO.from_station": from_code,
        "leftTicketDTO.to_station": to_code,
        "purpose_codes": "ADULT",
    }
    probe_path = urls.LEFT_TICKET_QUERY_CANDIDATES[0]
    t0 = time.time()
    try:
        resp = api.client.get(
            probe_path, params=params, allow_retry=False, referer=api.client.url(urls.LEFT_TICKET_INIT)
        )
        body = resp.text or ""
        head = body.lstrip()[:1]
        print(f"- 请求: GET {probe_path} {params}")
        print(f"- 响应: HTTP {resp.status_code}  {int((time.time() - t0) * 1000)}ms  长度={len(body)}")
        print(f"- 返回类型: {'HTML（请求未被接受：会话/风控问题）' if head == '<' else 'JSON/其它'}")
        print(f"- 前 200 字: {body[:200]}")
    except Exception as exc:  # noqa: BLE001
        print(f"- 探针请求异常: {exc}")

    _step("6. 走接口层 query_left_ticket()（含候选路径轮换）")
    t0 = time.time()
    try:
        tickets = api.query_left_ticket(train_date, from_code, to_code)
        print(f"- 成功：{len(tickets)} 个车次（{int((time.time() - t0) * 1000)}ms）")
        for ticket in tickets[:10]:
            print(f"    {ticket.train_code} {ticket.start_time}-{ticket.arrive_time} {ticket.duration}")
    except Exception as exc:  # noqa: BLE001
        print(f"- 失败：{type(exc).__name__}: {exc}")
        print("  说明：RiskControlError=疑似限流（要降频）；LoginError=会话失效（要重登）")

    _step("7. 走产品入口 AppContext.query_train_options()（界面用的就是它）")
    t0 = time.time()
    payload = ctx.query_train_options(
        dates=date_list[:2],
        from_stations=from_list,
        to_stations=to_list,
        refresh=True,
    )
    print(f"- 会话: {'匿名（未登录）' if payload.get('anonymous') else '账号 ' + str(payload.get('account'))}")
    if payload.get("fallback_from"):
        print(f"- 注意: 账号「{payload['fallback_from']}」的会话不可用，已自动改用匿名会话")
    print(f"- 组合数 {payload['queried']}/{payload['combos']}  截断={payload['truncated']}  风控={payload['risk_limited']}")
    print(f"- 错误: {payload['errors'] or '无'}")
    print(f"- 车次: {len(payload['trains'])} 个")
    for train in payload["trains"][:10]:
        print(f"    {train['train_code']} {train['start_time']}-{train['arrive_time']}  {train['remain_text']}")
    print(f"- 全程耗时 {time.time() - started:.1f}s")

    _step("结论")
    if payload["trains"]:
        print("链路是通的：能拉到车次。界面若仍显示空，请确认用的是同一个数据目录、")
        print("且「乘车日期 / 出发站 / 到达站」与本次诊断一致。")
    elif payload["risk_limited"]:
        print("被官方限流拦下（HTML 错误页）。等几分钟再试；不要连点「重新拉取」。")
    elif payload["errors"]:
        print("接口报错且不是限流 —— 看上面的 errors 原文。")
    else:
        print(f"请求都成功了，但 {date_list[0]} {from_list[0]}→{to_list[0]} 这天没有车次。")
        print("多半是日期/线路本身没有车（例如日期已过、或该站当天无车）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
