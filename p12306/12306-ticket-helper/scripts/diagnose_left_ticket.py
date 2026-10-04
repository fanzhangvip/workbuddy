"""余票查询对照探针（**真实 12306**）：一次跑多组「日期 × 起止站」，并排对比官方返回。

为什么需要它
------------
「任务在盯票但一个候选都没有」至少有四种完全不同的原因，症状却完全一样 —— 空：

============================  ================================================
现象                          真正的处理动作
============================  ================================================
会话被风控 / 没热身           官方回的是 **HTML 错误页**（HTTP 200，不是 4xx），
                              要降频等待；换路径重试只会加重限流
那天已经过去                  官方回 **HTML 错误页**（与风控**同样是 HTML**！）
                              → 换日期，程序没问题
起止站选错（深圳 vs 深圳北）   回的是**合法 JSON 但车次列表为空**
                              → 换站名
车次/席别/时间区间筛没了       请求成功、也有车次，但候选为 0
                              → 放宽筛选条件
============================  ================================================

**必须并排、同一时刻做**：分开时段做会因为限流状态不同而得出相反结论。

用法::

    # 默认跑「任务里的日期」与「今天之后的正确日期」的对照
    python scripts/diagnose_left_ticket.py --task 2

    # 手动指定若干组
    python scripts/diagnose_left_ticket.py \\
        --case 2026-10-03:深圳:长沙南 --case 2025-10-03:深圳:长沙南 \\
        --case 2026-10-03:深圳北:长沙南

注意：这是**真实 12306**，只做低频人工排查用。**不要在任务正在运行时跑**，
否则会与任务的请求叠成突发，把短暂限流变成持续限流，那时所有结论都不可信。
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


def _is_html(text: str) -> bool:
    return (text or "").lstrip("\ufeff").lstrip().startswith("<")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="余票查询对照探针（真实 12306）")
    parser.add_argument("--task", type=int, default=0, help="用该任务的日期/站点生成对照（默认取第一个任务）")
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        metavar="日期:出发站:到达站",
        help="手动指定一组，可重复",
    )
    parser.add_argument("--gap", type=float, default=3.0, help="每组之间的间隔秒数（默认 3）")
    parser.add_argument("--home", default="", help="数据目录（默认 %%USERPROFILE%%\\.ticket-helper）")
    args = parser.parse_args(argv)

    from ticket_helper.inter.api import TicketApi
    from ticket_helper.inter.base import HttpClient
    from ticket_helper.inter.station import StationMap
    from ticket_helper.inter.parser import parse_left_ticket_payload
    from ticket_helper.runtime.paths import get_app_paths

    paths = get_app_paths(Path(args.home) if args.home else None)
    station_map = StationMap(paths).load()

    cases: list[tuple[str, str, str]] = []
    for raw in args.case:
        parts = raw.split(":")
        if len(parts) != 3:
            print(f"忽略无法解析的 --case: {raw}（应为 日期:出发站:到达站）")
            continue
        cases.append((parts[0], parts[1], parts[2]))

    if not cases:
        from ticket_helper.storage.database import get_database
        from ticket_helper.storage.repository import TaskRepository
        from ticket_helper import train_date as train_date_util

        db = get_database(paths.db_path)
        tasks = TaskRepository(db).list()
        task = next((t for t in tasks if t.id == args.task), None) if args.task else (tasks[0] if tasks else None)
        if task is None:
            print("任务表里没有任务，也没有传 --case，无法继续")
            return 2
        print(f"取自任务 #{task.id}「{task.name}」")
        print(f"  任务里的日期: {task.left_dates}   起止: {task.from_stations}→{task.to_stations}")
        date = task.left_dates[0] if task.left_dates else ""
        frm = task.from_stations[0] if task.from_stations else ""
        to = task.to_stations[0] if task.to_stations else ""
        if date and frm and to:
            cases.append((date, frm, to))
            # 任务里的日期如果 12306 现在根本查不了，就必须补一组**可查的**日期做对照 ——
            # 否则「查不到票」到底是日期的问题还是线路的问题，光看一组数据分不出来。
            if not train_date_util.queryable(date):
                probe = train_date_util.last_queryable_date().strftime("%Y-%m-%d")
                cases.append((probe, frm, to))
                print(f"  ⚠️ {date} 不在 12306 可售范围（{train_date_util.explain(date)}）")
                print(f"     已自动补一组可查日期做对照：{probe}")
        # 对照组之二：同城另一个站，用来看站名是不是选错了
        if frm == "深圳":
            cases.append((cases[0][0], "深圳北", to))
        elif frm == "深圳北":
            cases.append((cases[0][0], "深圳", to))

    print(f"\n共 {len(cases)} 组，间隔 {args.gap}s\n")

    api = TicketApi(
        HttpClient(base_url="https://kyfw.12306.cn", retries=1),
        station_map,
    )
    from ticket_helper.inter import urls

    print("=== 会话预热（真机上不预热必被 WAF 拒） ===")
    t0 = time.time()
    api.ensure_session_warm(force=True)
    print(f"- 预热完成 {int((time.time() - t0) * 1000)}ms\n")

    results: list[tuple[str, str, str, str, int]] = []
    for index, (date, frm, to) in enumerate(cases):
        if index:
            time.sleep(max(0.5, args.gap))
        try:
            from_code = station_map.code_of(frm)
            to_code = station_map.code_of(to)
        except Exception as exc:  # noqa: BLE001
            print(f"[{index + 1}] {date} {frm}→{to}: 站名解析失败: {exc}")
            results.append((date, frm, to, f"站名解析失败: {exc}", 0))
            continue

        params = {
            "leftTicketDTO.train_date": date,
            "leftTicketDTO.from_station": from_code,
            "leftTicketDTO.to_station": to_code,
            "purpose_codes": "ADULT",
        }
        path = urls.LEFT_TICKET_QUERY_CANDIDATES[0]
        print(f"[{index + 1}] {date}  {frm}({from_code}) → {to}({to_code})")
        t0 = time.time()
        try:
            resp = api.client.get(
                path, params=params, allow_retry=False, referer=api.client.url(urls.LEFT_TICKET_INIT)
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    请求异常: {type(exc).__name__}: {exc}\n")
            results.append((date, frm, to, f"请求异常: {exc}", 0))
            continue

        body = resp.text or ""
        elapsed = int((time.time() - t0) * 1000)
        print(f"    HTTP {resp.status_code}  {elapsed}ms  长度={len(body)}")
        if _is_html(body):
            head = " ".join(body.split())[:160]
            print(f"    >>> 官方回了 HTML（请求未被接受：会话失效 / 被风控 / 日期非法）")
            print(f"    >>> {head}\n")
            results.append((date, frm, to, "HTML 错误页（请求未被接受）", 0))
            continue

        payload = resp.json
        if payload is None:
            print("    >>> 不是合法 JSON\n")
            results.append((date, frm, to, "非 JSON", 0))
            continue

        try:
            tickets = parse_left_ticket_payload(payload, station_map=station_map, train_date=date)
            count = len(tickets)
            sample = ", ".join(f"{t.train_code}({t.start_time})" for t in tickets[:5]) or "（空）"
            print(f"    >>> JSON 正常，解析出 {count} 个车次：{sample}\n")
            results.append((date, frm, to, "JSON 正常", count))
        except Exception as exc:  # noqa: BLE001
            print(f"    >>> JSON 解析失败: {type(exc).__name__}: {exc}\n")
            print(f"    原始前 200 字: {(body or '')[:200]}")
            results.append((date, frm, to, f"解析失败: {exc}", 0))

    print("=" * 68)
    print("对照表")
    print("=" * 68)
    for date, frm, to, verdict, count in results:
        print(f"  {date}  {frm}→{to:<6}  {verdict:<28} 车次 {count}")

    # 结论必须**按成因分开**：被拒（HTML）和「合法 JSON 但没车」是两件完全不同的事，
    # 混成一句「没查到车次」就等于没说（这次真正出故障的正是这种混淆）。
    html_like = [r for r in results if r[3].startswith("HTML") or r[3] == "非 JSON"]
    ok_json = [r for r in results if r[3] == "JSON 正常"]
    if html_like:
        dates = "、".join(sorted({r[0] for r in html_like}))
        print(f"\n有组合被官方拒（HTML 错误页）：{dates}")
        print("  这一类**换站名/换车次都不会有用**。先看日期是不是已经过去或超出预售期；")
        print("  如果日期本身没问题，那就是真的被限流 —— 停掉正在跑的任务、等几分钟再试。")
        if ok_json:
            print("  注意：同一次实验里其它日期是正常的（见上表），所以**不是**会话被整体限流。")
    if ok_json and all(r[4] == 0 for r in ok_json):
        print("\n合法 JSON 的组合里一个车次都没有 → 线路/日期本身没车（例如站名选错）。")
    if any(r[4] > 0 for r in results):
        print("\n至少有一组能查到车次 → 查询链路是通的，")
        print("去比「任务配置」的日期/站名与上表里能查到的那一行差在哪。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
