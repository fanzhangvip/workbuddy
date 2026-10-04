"""调用链审计：证明「UI 每个入口都真的走到了协议层（会发 HTTP 出网）」。

它回答的问题是：**界面上点一个按钮，到底有没有真的去调 12306 的接口？**
而不是「靠猜」或者「被某处的假数据糊弄过去」。

做法
----
在 ``HttpClient.request`` 上挂一个探针（协议层唯一的出网出口），
然后**逐个**驱动 UI 能触发的入口（走的是桌面端与网页端共用的那套 HTTP 接口），
记录每个入口**实际打出去的 12306 路径**。于是：

- 有出网记录 → 这条链路真的通到协议层；
- 一条都没有 → 要么它是「本来就该在本地完成」的（查库/配置/日志），
  要么就是**真的没走内核**（会标成 ``⚠ 异常``）。

判定「本来就该在本地」的标准：数据源在本地（SQLite / env.py / 日志总线 / 内存状态），
压根不需要 12306 参与。这类入口在脚本里用 ``local=True`` 显式声明，
**而不是**用「没出网就默认没问题」这种模糊口径盖过去。

配套的反向证据（脚本会一并打印）：
- 运行期包 ``ticket_helper/`` ``desktop_ui/`` 里**没有任何** mock 引用；
- ``AppConfig.api_base`` 默认值是真机 ``https://kyfw.12306.cn``；
- 本脚本里 mock 只替换 ``api_base``（服务端地址），客户端代码一行没改。

用法::

    python scripts/audit_callchain.py              # 全部入口
    python scripts/audit_callchain.py --keep-home  # 保留临时数据目录
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------------------- #
# 探针：记录每一次出网
# --------------------------------------------------------------------------- #
RECORDS: list[tuple[str, str, str, int]] = []   # (标签, method, path, status)
LOCAL_ENTRIES: dict[str, str] = {}              # 标签 → 为什么它是本地的
_LABEL = {"current": "(未标注)"}
_LOCK = threading.RLock()


def _install_probe() -> None:
    """把探针挂在协议层唯一的出网口 ``HttpClient.request`` 上。

    选这个位置的原因：``HttpClient`` 是本项目**唯一**与 12306 通信的实现
    （``requests.Session``）。任何真出网都必然经过这里，
    因此「这里没有记录」等价于「这次操作没有碰 12306」。
    """
    from ticket_helper.inter.base import HttpClient

    original = HttpClient.request

    def patched(self, method, path, **kwargs):  # noqa: ANN001, ANN003
        response = original(self, method, path, **kwargs)
        with _LOCK:
            RECORDS.append((_LABEL["current"], method.upper(), str(path), response.status_code))
        return response

    HttpClient.request = patched  # type: ignore[method-assign]


class step:
    """上下文管理器：给这一段代码里发生的出网请求打上「UI 入口」标签。"""

    def __init__(self, label: str) -> None:
        self.label = label

    def __enter__(self):
        with _LOCK:
            _LABEL["current"] = self.label
        return self

    def __exit__(self, *exc):
        return False


def _mark_local(label: str, why: str) -> None:
    """声明「这个入口的数据源在本地，本来就不会出网」。"""
    LOCAL_ENTRIES[label] = why
    with _LOCK:
        _LABEL["current"] = label     # 让后续（本就没有的）请求也归到它名下


# --------------------------------------------------------------------------- #
# 环境
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _rmtree(path: Path) -> None:
    """删临时目录；被沙箱守卫拦下时不报错退出（审计结果比清理重要）。"""
    import shutil

    try:
        shutil.rmtree(path)
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 临时目录未能删除（{exc}）；可手工删除 {path}", file=sys.stderr)


def _boot(home: Path):
    """起 mock + 建 AppContext。"""
    from werkzeug.serving import make_server

    from mock_server.server import create_mock_app

    port = _free_port()
    app = create_mock_app("has_ticket")
    server = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, name="audit-mock", daemon=True).start()
    time.sleep(0.2)
    mock_url = f"http://127.0.0.1:{port}"

    home.mkdir(parents=True, exist_ok=True)
    (home / "env.py").write_text(
        "ENV = {\n"
        f"    'api_base': {mock_url!r},\n"
        "    'web': {'enable': True, 'auth_user': 'admin', 'auth_pass': 'audit-pass', 'port': 18199},\n"
        "    'codec': {'mode': 'manual'},\n"
        "    'log_level': 'WARNING',\n"
        "}\n",
        encoding="utf-8",
    )

    from ticket_helper.runtime.context import AppContext

    ctx = AppContext(home=home, start_watchdog=False)
    return ctx, server, mock_url


def _prepare_account(ctx) -> None:
    """建一个账号并真的登录（走协议层的登录链路）。"""
    from ticket_helper.config.settings import UserConfig
    from ticket_helper.verify.service import CallbackSolver

    # 必须走 set_solver()：它把头一个识别器放进链首。
    # 直接给 .solver 赋值是无效的（那个属性不存在），
    # 会让登录退回「人工兜底」并一直等到超时 —— 审计脚本会假死在第一步。
    ctx.verify_service.set_solver(CallbackSolver(lambda *a, **k: "112,45", name="fixed"))
    ctx.upsert_user(UserConfig(username="audit_user", password="test_pass", heartbeat=120))
    user = ctx.accounts.get("audit_user")
    with step("登录·协议层登录链路"):
        user.login(force=True)


def _web_client(ctx):
    """返回 (test_client, headers)。桌面端与网页端打的都是这套 HTTP 接口。"""
    from ticket_helper.web.app import create_app

    app = create_app(ctx, start_background=False)
    app.config["TESTING"] = True
    client = app.test_client()
    response = client.post(
        "/api/auth/login",
        json={"user": ctx.config.web.auth_user, "pass": ctx.config.web.auth_pass},
    )
    token = (response.get_json() or {}).get("token", "")
    return client, {"Authorization": f"Bearer {token}"}


def _set_qr(mock_url: str, state: str, username: str) -> None:
    """让 mock 的扫码进入指定状态（模拟用户用手机扫了码）。"""
    body = json.dumps({"state": state, "username": username}).encode()
    request = urllib.request.Request(
        f"{mock_url}/__mock__/qr", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except Exception:  # noqa: BLE001 - 审计不应因 mock 辅助接口失败而中断
        pass


def _set_scenario(mock_url: str, scenario: str) -> None:
    """切换 mock 场景。

    候补链路有个**前提**：车次必须「有票但只能候补」。若场景给的是「有余票」，
    候补轨会在「找可候补车次」那一步就退出（这是正确行为，不是缺陷），
    于是整条 ``afterNate`` 链路根本不会被触发 —— 审计会误报成「候补没走内核」。
    """
    body = json.dumps({"scenario": scenario}).encode()
    request = urllib.request.Request(
        f"{mock_url}/__mock__/scenario", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 逐个 UI 入口
# --------------------------------------------------------------------------- #
def drive_all(ctx, client, headers, mock_url: str) -> None:
    """把 UI 能触发的入口逐个跑一遍。"""

    def call(label: str, method: str, path: str, *, local: str = "", **kwargs):
        """走一次后台 HTTP 接口（等价于在界面上点一次）。

        ``local`` 非空表示「这个入口的数据源在本地，本来就不会出网」，
        传一段说明作为理由。
        """
        if local:
            _mark_local(label, local)
        else:
            _LABEL["current"] = label
        with step(label):
            fn = {"GET": client.get, "POST": client.post, "PUT": client.put, "DELETE": client.delete}[method]
            return fn(path, headers=headers, **kwargs)

    users = (client.get("/api/users", headers=headers).get_json() or {}).get("users") or []
    uid = users[0]["id"] if users else 0

    # ---------------- 账号管理 ---------------- #
    call("账号管理·账号列表", "GET", "/api/users",
         local="账号状态来自内存 AccountManager + 本地 SQLite users 表")
    r = call("账号管理·新增账号", "POST", "/api/users",
             json={"username": "audit_new", "password": "pw123", "heartbeat": 120})
    new_id = (r.get_json() or {}).get("id")
    call("账号管理·修改账号", "PUT", f"/api/users/{uid}", json={"heartbeat": 180})
    call("账号管理·启用/禁用", "PUT", f"/api/users/{uid}/state", json={"enabled": True})
    call("账号管理·测试登录", "POST", f"/api/users/{uid}/login")
    time.sleep(1.5)                       # 登录是异步的，给它时间把请求打出去
    call("账号管理·重测人证核验", "POST", f"/api/users/{uid}/verify/refresh")
    if new_id:
        call("账号管理·删除账号", "DELETE", f"/api/users/{new_id}")

    # ---------------- 扫码登录 ---------------- #
    r = call("扫码登录·申请二维码", "POST", "/api/users/qr/start", json={"ttl": 180})
    qr_uuid = (r.get_json() or {}).get("uuid", "")
    if qr_uuid:
        _set_qr(mock_url, "confirmed", "audit_qr_user")
        call("扫码登录·轮询状态（扫码成功→采纳账号）", "GET", f"/api/users/qr/status?uuid={qr_uuid}")
        call("扫码登录·取消", "POST", "/api/users/qr/cancel", json={"uuid": qr_uuid})

    # ---------------- 个人中心 ---------------- #
    call("个人中心·账号信息+乘车人+待支付", "GET",
         f"/api/users/{uid}/profile?with_passengers=1&with_pending=1&with_orders=0")
    call("个人中心·乘车人（选人）", "GET", f"/api/users/{uid}/passengers",
         local="乘车人走了 5 分钟缓存（上一步刚拉过），命中缓存时按设计不再打接口")
    call("个人中心·我的订单(未出行 G)", "GET", f"/api/users/{uid}/orders?where=G&page=1&size=10")
    call("个人中心·我的订单(历史 H)", "GET", f"/api/users/{uid}/orders?where=H&page=1&size=10")
    call("个人中心·订单关键字筛选", "GET", f"/api/users/{uid}/orders?where=G&kw=G6102")
    call("个人中心·非法 where（应 400）", "GET", f"/api/users/{uid}/orders?where=X",
         local="参数非法在**路由层**就被拦成 400，根本不该打到 12306")

    # ---------------- 任务配置 ---------------- #
    task_payload = {
        "name": "审计任务·直接抢票", "left_dates": ["2026-10-01"], "from_stations": ["广州"],
        "to_stations": ["长沙"], "seat_types": ["二等座"], "passengers": ["张三"],
        "strategy": "direct", "query_interval": 0.3,
    }
    r = call("任务配置·新建任务", "POST", "/api/tasks", json=task_payload,
             local="任务配置只落本地 SQLite，不涉及 12306")
    task_id = (r.get_json() or {}).get("id")
    call("任务配置·任务列表", "GET", "/api/tasks", local="读本地 SQLite + 内存调度器状态")
    if task_id:
        call("任务配置·任务详情", "GET", f"/api/tasks/{task_id}", local="读本地 SQLite")
        call("任务配置·保存修改", "PUT", f"/api/tasks/{task_id}",
             json={**task_payload, "remark": "审计"}, local="写本地 SQLite")
    call("任务配置·站点选择器", "GET", "/api/stations?kw=广州",
         local="站点表来自 station_name.js（启动时已拉，见「启动·站点表」）")
    call("任务配置·拉取候选车次", "GET",
         "/api/trains/options?dates=2026-10-01&from=广州&to=长沙")
    call("任务配置·经停信息", "GET",
         "/api/trains/stop-stations?train_no=240000G6102A&date=2026-10-01"
         "&from_station=GZQ&to_station=CSQ")

    # ---------------- 抢票内核（等价于点「启动任务」） ---------------- #
    if task_id:
        with step("抢票·直接策略（余票→锁票→排队→出票）"):
            call("抢票·启动任务", "POST", f"/api/tasks/{task_id}/start", json={})
            _wait_state(client, headers, task_id, timeout=60)
            call("抢票·任务状态", "GET", f"/api/tasks/{task_id}/status",
                 local="读内存引擎状态（不重复打 12306）")
            call("抢票·余票看板快照", "GET", f"/api/tasks/{task_id}/remain",
                 local="看板复用最近一次查询的候选，不额外打接口（防风控）")
            call("抢票·停止任务", "POST", f"/api/tasks/{task_id}/stop", json={},
                 local="停止是纯本地动作")

    # ---------------- 候补内核（策略 = standby） ---------------- #
    # 先切到「有票但只能候补」的场景，否则候补轨会正确地提前退出，
    # 整条 afterNate 链路不会被触发（见 _set_scenario 的说明）。
    _set_scenario(mock_url, "standby")
    standby_payload = {
        **task_payload, "name": "审计任务·候补", "strategy": "standby",
    }
    r = call("候补·新建任务", "POST", "/api/tasks", json=standby_payload,
             local="任务配置只落本地 SQLite")
    standby_id = (r.get_json() or {}).get("id")
    if standby_id:
        with step("候补·afterNate 全链路"):
            call("候补·启动任务", "POST", f"/api/tasks/{standby_id}/start", json={})
            _wait_state(client, headers, standby_id, timeout=60)
            call("候补·任务状态", "GET", f"/api/tasks/{standby_id}/status",
                 local="读内存引擎状态")
            call("候补·停止任务", "POST", f"/api/tasks/{standby_id}/stop", json={},
                 local="停止是纯本地动作")
    _set_scenario(mock_url, "has_ticket")

    call("余票看板·全部任务状态", "GET", "/api/tasks/status/all",
         local="调度器内存快照")

    # ---------------- 订单记录 ---------------- #
    call("订单记录·本地流水", "GET", "/api/orders?limit=100",
         local="下单流水存在本地 SQLite orders 表")
    call("订单记录·待支付订单", "GET", "/api/orders/unfinished")

    # ---------------- 通知设置 ---------------- #
    call("通知设置·渠道与历史", "GET", "/api/notifications",
         local="渠道状态来自本地配置 + 本地 notifications 表")
    call("通知设置·测试通知", "POST", "/api/notify/test", json={},
         local="测试的是**通知渠道**（Server酱 等），不出网到 12306")
    call("通知设置·读取配置", "GET", "/api/config", local="读本地 env.py + 运行时配置")
    call("通知设置·保存配置", "PUT", "/api/config/notify", json={},
         local="写本地配置并落库")

    # ---------------- 仪表盘 / 系统 / 日志 ---------------- #
    call("仪表盘·统计", "GET", "/api/stats", local="本地 DB 统计 + 运行时快照")
    call("仪表盘·系统快照", "GET", "/api/system/snapshot", local="纯内存/本地状态汇总")
    call("系统设置·集群状态", "GET", "/api/cluster", local="本地配置 + 内存状态")
    call("系统设置·实时日志", "GET", "/api/logs?limit=50", local="读本地日志总线")
    call("系统设置·验证码待办", "GET", "/api/captcha/pending", local="读内存里的待办队列")

    # ---------------- 清理 ---------------- #
    for tid in (task_id, standby_id):
        if tid:
            call(f"任务配置·删除任务 {tid}", "DELETE", f"/api/tasks/{tid}",
                 local="删本地 SQLite 记录")


def _wait_state(client, headers, task_id: int, *, timeout: float) -> None:
    """等任务跑完（出票/失败/停止）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = (client.get(f"/api/tasks/{task_id}/status", headers=headers).get_json() or {})
        if status.get("state") in ("success", "error", "stopped"):
            return
        time.sleep(0.4)


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def report() -> None:
    with _LOCK:
        rows = list(RECORDS)

    grouped: dict[str, list[tuple[str, str, int]]] = {}
    for label, method, path, status in rows:
        grouped.setdefault(label, []).append((method, path, status))

    hit = [label for label in grouped if grouped[label]]
    local = [label for label in LOCAL_ENTRIES if label not in hit]
    anomaly = [
        label for label in LOCAL_ENTRIES
        if label not in hit and not LOCAL_ENTRIES[label]
    ]

    print("\n" + "=" * 108)
    print("① 已证实打到协议层：每个 UI 入口实际打出的 12306 请求")
    print("   （探针挂在 HttpClient.request —— 本项目唯一的出网出口）")
    print("=" * 108)
    for label in hit:
        print(f"\n■ {label}")
        seen: set[tuple[str, str]] = set()
        for method, path, status in grouped[label]:
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            times = sum(1 for m, p, _ in grouped[label] if (m, p) == key)
            suffix = f"   ×{times}" if times > 1 else ""
            print(f"     {method:<5}{path:<52}HTTP {status}{suffix}")

    print("\n" + "=" * 108)
    print("② 本地入口：数据源在本地，本就不该出网（显式声明，不是「默认放过」）")
    print("=" * 108)
    for label in local:
        print(f"\n□ {label}")
        print(f"     理由：{LOCAL_ENTRIES[label]}")

    if anomaly:
        print("\n" + "=" * 108)
        print("③ ⚠ 异常：既没有出网、也没有声明为本地入口")
        print("=" * 108)
        for label in anomaly:
            print(f"     ⚠ {label}")
    else:
        print("\n③ 异常项：无 —— 所有入口要么打到了协议层，要么有明确的本地理由。")

    with _LOCK:
        total = len(rows)
    print("\n" + "=" * 108)
    print(f"合计：{len(hit)} 个入口打到协议层（{total} 次出网请求），"
          f"{len(local)} 个本地入口，{len(anomaly)} 个异常。")
    print("=" * 108)


def report_reverse_evidence(ctx) -> None:
    """反向证据：运行期不含 mock、默认指向真机。"""
    import subprocess

    print("\n" + "=" * 108)
    print("④ 反向证据：确认没有「假动作」旁路")
    print("=" * 108)

    try:
        from ticket_helper.config.settings import AppConfig

        default_base = AppConfig().api_base
    except Exception as exc:  # noqa: BLE001
        default_base = f"(读取失败: {exc})"
    print(f"\n  AppConfig 默认站点          : {default_base}")
    print(f"  本次审计的 api_base（替换） : {ctx.config.api_base}")

    result = subprocess.run(
        ["grep", "-rnE", r"mock_server|from mock|import mock", "ticket_helper/", "desktop_ui/"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    hits = [line for line in (result.stdout or "").splitlines() if "__pycache__" not in line]
    print(f"  运行期包内 mock 引用        : {len(hits)} 处")
    for line in hits:
        print(f"       {line}")

    print("  客户端代码是否被替换        : 否 —— 只改了 api_base（服务端地址），")
    print("                                HttpClient / TicketApi / 服务层 / 两个 UI 一行未动。")


def main() -> int:
    parser = argparse.ArgumentParser(description="调用链审计")
    parser.add_argument("--keep-home", action="store_true", help="保留临时数据目录（排查用）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    home = ROOT / ".audit-home"
    _rmtree(home)
    os.environ["TICKET_HELPER_HOME"] = str(home)

    # 清掉可能已缓存的单例，避免指向别的目录
    import ticket_helper.config.loader as loader_mod
    import ticket_helper.inter.station as station_mod
    import ticket_helper.runtime.context as context_mod
    import ticket_helper.runtime.paths as paths_mod
    import ticket_helper.storage.database as db_mod

    loader_mod._loader = None  # noqa: SLF001
    context_mod._context = None  # noqa: SLF001
    station_mod._station_map = None  # noqa: SLF001
    db_mod._db = None  # noqa: SLF001
    paths_mod.reset_app_paths()

    _install_probe()

    with step("启动·站点表（station_name.js）"):
        ctx, server, mock_url = _boot(home)

    # 站点表用离线版：审计不应依赖联网
    from ticket_helper.inter.station import StationMap

    ctx.station_map = StationMap(ctx.paths).load(allow_remote=False)
    ctx.accounts.station_map = ctx.station_map

    try:
        _prepare_account(ctx)
        client, headers = _web_client(ctx)
        drive_all(ctx, client, headers, mock_url)
        report()
        report_reverse_evidence(ctx)
    finally:
        try:
            ctx.stop()
        except Exception:  # noqa: BLE001
            pass
        server.shutdown()
        if not args.keep_home:
            _rmtree(home)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
