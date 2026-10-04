#!/usr/bin/env python
"""启动冒烟测试：真实跑起 mock-12306 + Web 后台，用 HTTP 走完整流程。

与 pytest 的区别：pytest 用 Flask 测试客户端（进程内），
本脚本则是**真正拉起两个 HTTP 服务**再用 requests 打过去，
能额外验证「端口监听、线程服务器、WebSocket 路由、静态资源」这些
只有在真实运行时才会暴露的问题（对接测试方案「启动冒烟」一项）。

用法::

    python scripts/smoke_test.py
    python scripts/smoke_test.py --keep-alive     # 跑完不关闭，便于人工打开浏览器看

退出码 0 表示全部通过。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def _make_console_safe() -> None:
    """中文 Windows 控制台是 GBK 编码，让输出容错，避免特殊符号把脚本打断。"""
    import sys as _sys

    for stream in (_sys.stdout, _sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


_make_console_safe()

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(name)
        print(f"  [√] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [×] {name}" + (f" — {detail}" if detail else ""))
    return condition


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(method: str, url: str, *, payload: dict | None = None, token: str = "", timeout: float = 15.0):
    """极简 HTTP 客户端，避免依赖 requests（用于验证「零依赖也能连通」）。"""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            try:
                return response.status, json.loads(body)
            except ValueError:
                return response.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, body


def tail_file(path, lines: int = 20) -> str:
    """读取文件末尾若干行（用于失败时把子进程日志带出来）。"""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"（读取失败：{exc}）"
    return "\n".join("      " + line for line in content[-lines:]) or "（空）"


def reset_home(home: Path) -> None:
    """把冒烟用的数据目录恢复到「全新安装」状态。

    **必须清空，否则这个脚本第二次跑必然失败** —— 踩过：
    上一轮留下的账号会被启动流程「恢复并重新登录」，而冒烟用的
    ``codec.mode = manual`` 会让登录卡在「等人工输入验证码」上，
    于是 Web 端口在等待窗口内根本没起来，报出来的却是
    「Web 服务已监听：[×]」这种和真实原因（旧账号把启动堵住了）毫无关系的结论。

    这里删的是**脚本自己创建的项目内目录**（``<项目根>/.smoke-home``），
    不是用户数据目录；删不掉时退化为「改名让路」，不会硬删。
    """
    import shutil

    if home.exists():
        try:
            shutil.rmtree(home)
        except OSError:
            # 有文件被占用（例如上一轮的后台没退干净）时不能硬来，
            # 改名让路同样能达到「干净启动」的效果。
            stale = home.with_name(f"{home.name}.stale{int(time.time())}")
            try:
                home.rename(stale)
                print(f"  （旧数据目录被占用，已改名让路：{stale.name}）")
            except OSError as exc:
                print(f"  （警告：无法清理旧数据目录 {home}：{exc}）")
    home.mkdir(parents=True, exist_ok=True)


def wait_for(url: str, *, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = http("GET", url, timeout=2.0)
            if status < 500:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动冒烟测试")
    parser.add_argument("--keep-alive", action="store_true", help="跑完保持服务运行")
    args = parser.parse_args(argv)

    python = sys.executable
    home = ROOT / ".smoke-home"
    # 目录要在**第一节之前**建好（子进程日志写在这里面），而且必须是干净的
    # —— 残留的账号会把启动流程堵在「重新登录」上，见 reset_home 的说明。
    reset_home(home)
    mock_port = free_port()
    web_port = free_port()
    processes: list[subprocess.Popen] = []

    print("=" * 66)
    print(f"启动冒烟测试（mock:{mock_port} / web:{web_port} / home:{home}）")
    print("=" * 66)

    try:
        # ---------------- 1. 拉起 mock-12306 ---------------- #
        print("\n[1/8] 启动 mock-12306 服务…")
        mock_log = (home / "smoke-mock.log").open("w", encoding="utf-8", errors="replace")
        mock_proc = subprocess.Popen(
            [python, str(ROOT / "main.py"), "mock", "--port", str(mock_port), "--scenario", "has_ticket"],
            stdout=mock_log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "TICKET_HELPER_HOME": str(home)},
        )
        processes.append(mock_proc)
        check("mock 服务可访问", wait_for(f"http://127.0.0.1:{mock_port}/otn/login/conf"))

        # ---------------- 2. 准备配置并拉起 Web 后台 ---------------- #
        print("\n[2/8] 准备配置并启动 Web 后台…")
        (home / "env.py").write_text(
            "ENV = {\n"
            f"    'api_base': 'http://127.0.0.1:{mock_port}',\n"
            "    'web': {\n"
            "        'enable': True, 'host': '127.0.0.1',\n"
            f"        'port': {web_port}, 'auth_user': 'admin', 'auth_pass': 'smoke-pass',\n"
            "    },\n"
            "    'ratelimit': {'adaptive': True, 'min_interval': 0.3, 'max_interval': 4.0},\n"
            "    'codec': {'mode': 'manual'},\n"
            "}\n",
            encoding="utf-8",
        )
        web_log_path = home / "smoke-web.log"
        web_log = web_log_path.open("w", encoding="utf-8", errors="replace")
        web_proc = subprocess.Popen(
            [python, str(ROOT / "main.py"), "web", "--port", str(web_port)],
            stdout=web_log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "TICKET_HELPER_HOME": str(home)},
        )
        processes.append(web_proc)
        base = f"http://127.0.0.1:{web_port}"
        up = wait_for(f"{base}/health")
        check("Web 服务已监听", up)
        if not up:
            # 起不来时必须把子进程的日志尾巴打出来。
            # 之前这里把输出丢进 DEVNULL，失败时只能看到「Web 服务已监听：[×]」，
            # 真实原因（端口占用 / 导入错误 / 配置非法）一个字都看不到。
            print(f"  → 后台日志（{web_log_path}）末尾：")
            print(tail_file(web_log_path, 20))
            print("  → 后台没起来，后面的检查全部跳过：先按上面的日志把启动问题修好，")
            print("    再跑冒烟才有意义（否则会刷出十几条同源的失败，掩盖真正的原因）。")
            return 1

        # ---------------- 3. 健康检查与鉴权 ---------------- #
        print("\n[3/8] 健康检查与鉴权…")
        status, health = http("GET", f"{base}/health")
        check("GET /health 返回 ok", status == 200 and isinstance(health, dict) and health.get("status") == "ok")

        status, ready = http("GET", f"{base}/ready")
        check("GET /ready 可访问", status in (200, 503))

        status, _ = http("GET", f"{base}/api/tasks")
        check("未登录访问受保护接口被拒(401)", status == 401)

        status, payload = http("POST", f"{base}/api/auth/login", payload={"user": "admin", "pass": "wrong"})
        check("错误密码登录被拒(401)", status == 401)

        status, payload = http("POST", f"{base}/api/auth/login", payload={"user": "admin", "pass": "smoke-pass"})
        token = (payload or {}).get("token", "") if isinstance(payload, dict) else ""
        check("正确密码登录成功并返回令牌", status == 200 and bool(token))

        status, _ = http("GET", f"{base}/api/tasks", token=token)
        check("带令牌访问受保护接口成功", status == 200)

        # ---------------- 4. 静态资源 ---------------- #
        print("\n[4/8] 静态资源…")
        status, index = http("GET", f"{base}/")
        check("首页可访问且包含标题", status == 200 and isinstance(index, str) and "12306" in index)
        # 「个人中心」必须是一级导航入口，而不是藏在表格里的小按钮
        check(
            "个人中心是一级导航入口",
            isinstance(index, str) and 'data-page="profile"' in index and 'id="page-profile"' in index,
        )
        # 任务配置页的日期 / 站点必须是「选」的控件，而不是一个让人手敲的文本框
        check(
            "任务表单含日期控件与站点选择器",
            isinstance(index, str)
            and 'id="date-pick"' in index
            and 'id="from-search"' in index
            and 'id="to-search"' in index,
        )
        # 信息架构：顶栏常驻全局状态 + 左侧导航分组（9 个平铺菜单看不出重点）
        check(
            "顶栏含全局状态条",
            isinstance(index, str)
            and 'id="topbar-title"' in index
            and 'id="ts-accounts"' in index
            and 'id="ts-running"' in index,
        )
        check(
            "左侧导航已分组",
            isinstance(index, str)
            and 'class="nav-group-title"' in index
            and index.count("nav-group-title") >= 4,
        )
        # 人证核验：说明块 + 刷新入口都要在页面上看得到
        # （候补被拒最常见的原因就是它，藏起来等于没做）
        check(
            "账号页含人证核验说明与重测入口",
            isinstance(index, str) and 'id="verify-guide"' in index,
        )
        # 席别必须能表达顺序（多选下拉表达不了「优先级」，这是真问题不是审美）
        check(
            "席别优先级是可排序的控件",
            isinstance(index, str)
            and 'id="seat-picked"' in index
            and 'id="seat-available"' in index
            and 'id="t-seats"' in index
            and '<select id="t-seats" multiple' not in index,
        )
        # 车次类型改成勾选；白/黑名单都能从 12306 拉取
        check(
            "车次类型是勾选而不是多选下拉",
            isinstance(index, str)
            and 'id="train-types-box"' in index
            and '<select id="t-types" multiple' not in index,
        )
        check(
            "白/黑名单都有拉取入口且用标签展示",
            isinstance(index, str)
            and 'id="btn-pick-trains"' in index
            and 'id="btn-pick-trains-black"' in index
            and 'id="white-chips"' in index
            and 'id="black-chips"' in index,
        )
        status, js = http("GET", f"{base}/static/app.js")
        check("前端脚本可访问", status == 200 and isinstance(js, str) and "api/auth/login" in js)
        check(
            "站点搜索前端逻辑已发布",
            isinstance(js, str) and "/api/stations?kw=" in js and "createTokenField" in js,
        )
        check(
            "席别排序与表单校验逻辑已发布",
            isinstance(js, str) and "renderSeats" in js and "clearFieldErrors" in js and "withBusy" in js,
        )
        # 通知渠道不再把接口里的英文 id 直接甩给用户（serverchan / wecom ...）
        check(
            "通知页显示中文渠道名",
            isinstance(js, str) and "'Server酱'" in js and "'企业微信群机器人'" in js,
        )
        # 核验状态的刷新逻辑：扫码账号没有密码、测试登录是禁用的，
        # 必须有独立的「重测核验」入口，否则这类账号永远刷新不了状态
        check(
            "前端含重测核验逻辑",
            isinstance(js, str) and "verify-user" in js and "/verify/refresh" in js,
        )
        status, css = http("GET", f"{base}/static/style.css")
        check("样式表可访问", status == 200 and isinstance(css, str) and "--primary" in css)
        check("分组表单样式已发布", isinstance(css, str) and ".form-group" in css and ".topbar" in css)
        check(
            "通知卡片样式已发布",
            isinstance(css, str) and ".channel-card" in css and ".channel-error" in css and ".channel-grid" in css,
        )
        check(
            "人证核验说明样式已发布",
            isinstance(css, str) and ".verify-guide" in css and ".verify-steps" in css and ".alert-warn" in css,
        )

        # ---------------- 5. 通过 HTTP 建账号 + 建任务 + 启动 → 抢到票 ---------------- #
        print("\n[5/8] 端到端：建账号 → 建任务 → 启动 → 出票…")
        # 直接写库（模拟「用户已用 seed_account 导入账号」）
        from ticket_helper.config.settings import UserConfig  # noqa: PLC0415

        os.environ["TICKET_HELPER_HOME"] = str(home)
        check("账号写入接口可用", http("POST", f"{base}/api/internal/seed-user",
                                       payload={"username": "smoke_user", "password": "smoke_pw"})[0] == 201)

        status, created = http(
            "POST",
            f"{base}/api/tasks",
            payload={
                "name": "冒烟任务",
                "left_dates": "2026-10-01",
                "from_stations": "广州",
                "to_stations": "长沙",
                "trains_whitelist": "G6102",
                "seat_types": "二等座,一等座",
                "passengers": "张三",
                "strategy": "direct",
                "query_interval": 0.3,
            },
            token=token,
        )
        task_id = (created or {}).get("id") if isinstance(created, dict) else None
        check("创建任务成功", status == 201 and bool(task_id), f"task_id={task_id}")

        status, started = http("POST", f"{base}/api/tasks/{task_id}/start", token=token)
        check("启动任务成功", status == 200)

        # 这里刻意把 codec.mode 配成 manual（人工兜底），因此引擎会把验证码推到前端等待录入。
        # 冒烟测试里我们扮演「人工」：轮询 /api/captcha/pending 并用固定答案回填。
        # 这样等于顺带验证了「自动识别不可用 → 人工兜底 → 流程继续」这条降级链路（VC-04）。
        deadline = time.time() + 90
        succeeded = False
        answered = 0
        while time.time() < deadline:
            status, pending = http("GET", f"{base}/api/captcha/pending", token=token)
            # http() 在非 JSON 响应（例如被重定向到登录页）时会回一个字符串，
            # 直接 .get("tasks") 会抛 AttributeError 把整个冒烟打断 ——
            # 一次降级场景不该让脚本崩在中途。
            tasks = pending.get("tasks") if isinstance(pending, dict) else []
            for item in tasks or []:
                http(
                    "POST",
                    f"{base}/api/captcha/submit",
                    payload={"id": item.get("id"), "answer": "112,45"},
                    token=token,
                )
                answered += 1

            status, statuses = http("GET", f"{base}/api/tasks/status/all", token=token)
            tasks = (statuses or {}).get("tasks") if isinstance(statuses, dict) else []
            task = next((t for t in (tasks or []) if t.get("task_id") == task_id), None)
            if task and task.get("state") == "success":
                succeeded = True
                break
            time.sleep(0.8)

        check("人工验证码回填通道可用", answered > 0, f"处理了 {answered} 次人工验证码")
        check("任务在 mock 上成功出票", succeeded)

        status, orders = http("GET", f"{base}/api/orders", token=token)
        order_rows = (orders or {}).get("orders") if isinstance(orders, dict) else []
        check("订单流水已落库", bool(order_rows), f"{len(order_rows or [])} 条")

        # ---------------- 6. 扫码登录全流程（AC-08） ---------------- #
        print("\n[6/8] 扫码登录：申请二维码 → 轮询 → 自动建号并登录…")
        status, qr_start = http("POST", f"{base}/api/users/qr/start", payload={}, token=token)
        qr_uuid = (qr_start or {}).get("uuid", "") if isinstance(qr_start, dict) else ""
        check("申请登录二维码成功", status == 200 and bool(qr_uuid), f"uuid={qr_uuid[:20]}")
        check("二维码图片已返回", bool((qr_start or {}).get("image")))

        # 扮演「用户扫码 + 手机确认」：把 mock 的扫码状态推进到已确认
        http(
            "POST",
            f"http://127.0.0.1:{mock_port}/__mock__/qr",
            payload={"state": "confirmed", "username": "smoke_qr_user"},
        )

        qr_state = ""
        qr_username = ""
        deadline = time.time() + 30
        while time.time() < deadline:
            status, qr_status = http("GET", f"{base}/api/users/qr/status?uuid={qr_uuid}", token=token)
            qr_state = (qr_status or {}).get("state", "") if isinstance(qr_status, dict) else ""
            qr_username = (qr_status or {}).get("username", "") if isinstance(qr_status, dict) else ""
            if qr_state in ("confirmed", "expired", "failed"):
                break
            time.sleep(0.5)

        check("扫码状态流转到「已确认」", qr_state == "confirmed", f"state={qr_state}")
        check("服务端返回了账号名", qr_username == "smoke_qr_user", f"username={qr_username}")

        status, users_payload = http("GET", f"{base}/api/users", token=token)
        qr_user = next(
            (u for u in (users_payload or {}).get("users", []) if u.get("username") == "smoke_qr_user"),
            None,
        )
        check("扫码账号已自动创建", qr_user is not None)
        check("扫码账号被标记为无密码登录", bool(qr_user) and qr_user.get("login_type") == "qr")
        check("扫码账号处于在线状态", bool(qr_user) and qr_user.get("status") == "online")
        # 「重启后免扫码」的端到端证据：会话必须被加密留存下来
        check(
            "扫码后已留存会话（重启免扫码）",
            bool(qr_user) and qr_user.get("session_saved") is True,
            f"session_saved={bool(qr_user) and qr_user.get('session_saved')}",
        )
        # 核验状态与「下一步做什么」由后端统一下发（两端 + 候补引擎同源），
        # 前端不再自己拼一套判断 —— 否则会出现「界面说无法判定、候补按未核验拦掉」。
        check(
            "账号列表下发核验状态与出路",
            bool(qr_user) and bool(qr_user.get("verify_label")) and bool(qr_user.get("verify_hint")),
            f"state={bool(qr_user) and qr_user.get('verify_state')}",
        )
        status, verify_refresh = http(
            "POST", f"{base}/api/users/{(qr_user or {}).get('id')}/verify/refresh", token=token
        )
        verify_refresh = verify_refresh if isinstance(verify_refresh, dict) else {}
        # 官方接口不返回核验字段时，必须回「无法判定」而不是「未核验」，
        # 而且**不能**因此拦掉候补（can_standby 必须为真）。
        check(
            "重测核验接口可用且不误判",
            status == 200
            and verify_refresh.get("verify_label") in ("已核验", "未核验", "无法判定", "未检测")
            and verify_refresh.get("can_standby") is True,
            f"label={verify_refresh.get('verify_label')} can_standby={verify_refresh.get('can_standby')}",
        )

        # 会话结束后必须能干净收尾（重复取消不该报错）
        status, cancel_payload = http("POST", f"{base}/api/users/qr/cancel", payload={"uuid": qr_uuid}, token=token)
        check("取消扫码会话不报错", status == 200)

        status, missing = http("GET", f"{base}/api/users/qr/status?uuid=not-exists", token=token)
        check("未知会话返回 404（前端据此提示刷新）", status == 404)

        # ---------------- 7. 个人中心（账号信息 / 乘车人 / 我的订单） ---------------- #
        print("\n[7/8] 个人中心：账号信息 / 乘车人 / 我的订单…")
        qr_user_id = (qr_user or {}).get("id")
        status, profile = http("GET", f"{base}/api/users/{qr_user_id}/profile?with_orders=1", token=token)
        profile = profile if isinstance(profile, dict) else {}
        check("账号档案接口可用", status == 200 and "profile" in profile)
        check(
            "账号信息已取到（含账号名）",
            bool((profile.get("profile") or {}).get("ok")) and bool((profile.get("profile") or {}).get("data", {}).get("username")),
            f"username={(profile.get('profile') or {}).get('data', {}).get('username', '')}",
        )
        passengers = (profile.get("passengers") or {}).get("data") or []
        check("乘车人已取到", bool((profile.get("passengers") or {}).get("ok")) and len(passengers) >= 1,
              f"{len(passengers)} 人")
        check(
            "乘车人证件号已打码",
            all("*" in str(p.get("passenger_id_no", "")) for p in passengers),
        )
        orders = (profile.get("orders") or {}).get("data") or {}
        check("我的订单已取到", bool((profile.get("orders") or {}).get("ok")),
              f"{len(orders.get('orders') or [])} 条")

        status, history = http("GET", f"{base}/api/users/{qr_user_id}/orders?where=H&size=5", token=token)
        check("历史订单查询可用", status == 200 and isinstance(history, dict) and history.get("ok") is True)

        # ---------------- 7.5 任务配置：从 12306 拉取车次（白/黑名单不手敲） ---------------- #
        print("\n[7.5/8] 任务配置：从 12306 拉取车次…")
        # 站名是中文，**必须**先百分号编码：urllib 只接受 ASCII URL，
        # 直接把「广州」拼进去会抛 UnicodeEncodeError（不是 HTTP 错误，很容易看岔）。
        query = urllib.parse.urlencode({"dates": "2026-10-01,2026-10-02", "from": "广州", "to": "长沙", "refresh": 1})
        # 超时给得比其它请求长：拉取要按「日期 × 起止站」逐个查，慢几秒是正常的；
        # 单次失败也不该把整条冒烟打断（后面还有安全头等项目要跑）。
        try:
            status, options = http("GET", f"{base}/api/trains/options?{query}", token=token, timeout=60.0)
        except Exception as exc:  # noqa: BLE001
            status, options = 0, {"_error": str(exc)}
        options = options if isinstance(options, dict) else {}
        trains = options.get("trains") or []
        detail = (
            f"status={status} · {len(trains)} 班车 · 组合 {options.get('queried')}/{options.get('combos')}"
            f" · 账号 {options.get('account') or '-'}"
            f" · errors={options.get('errors') or options.get('_error') or []}"
        )
        check("车次拉取接口可用", status == 200 and bool(trains), detail)
        check(
            "车次带时刻与余票摘要",
            all(t.get("train_code") and t.get("start_time") and t.get("remain_text") for t in trains),
        )
        check(
            "同车次跨日期已合并",
            all(len(t.get("dates") or []) >= 1 for t in trains) and len({t["train_code"] for t in trains}) == len(trains),
        )
        check(
            "车次可用于白名单（车次号非空）",
            all("," not in str(t.get("train_code")) for t in trains),
            ",".join(t.get("train_code", "") for t in trains),
        )
        status, missing = http("GET", f"{base}/api/trains/options?dates=2026-10-01", token=token)
        check("车次拉取缺参数被拒(400)", status == 400)

        # ---------------- 7.6 任务配置：站点检索（出发站 / 到达站选择器） ---------------- #
        print("\n[7.6/8] 任务配置：站点检索…")
        status, hot = http("GET", f"{base}/api/stations", token=token)
        hot_items = (hot or {}).get("items") if isinstance(hot, dict) else []
        check(
            "空关键字返回常用站（点开即见，不用先猜）",
            status == 200 and len(hot_items or []) > 10,
            f"{len(hot_items or [])} 个",
        )
        status, found = http(
            "GET",
            f"{base}/api/stations?kw={urllib.parse.quote('长沙')}",
            token=token,
        )
        found = found if isinstance(found, dict) else {}
        names = [item.get("name") for item in (found.get("items") or [])]
        check(
            "站点检索可用且完全匹配排第一",
            status == 200 and names[:1] == ["长沙"] and "长沙南" in names,
            f"{names[:4]}",
        )
        check(
            "站点带电报码（确认选到的是官方那个站）",
            all(item.get("code") for item in (found.get("items") or [])),
        )
        check(
            "旧字段 stations 保持兼容",
            found.get("stations") == names,
        )

        status, bad_where = http("GET", f"{base}/api/users/{qr_user_id}/orders?where=X", token=token)
        check("非法查询范围被拒(400)", status == 400)

        # ---------------- 8. 其他接口与安全头 ---------------- #
        print("\n[8/8] 其他接口与安全响应头…")
        status, stats = http("GET", f"{base}/api/stats", token=token)
        check("统计接口可用", status == 200 and "storage" in (stats or {}))

        status, cfg = http("GET", f"{base}/api/config", token=token)
        masked = ((cfg or {}).get("config") or {}).get("web", {}).get("auth_pass")
        check("配置接口对密码脱敏", masked in ("********", "", None), f"auth_pass={masked!r}")

        status, logs = http("GET", f"{base}/api/logs?limit=20", token=token)
        check("日志接口可用", status == 200 and isinstance((logs or {}).get("logs"), list))

        status, cluster = http("GET", f"{base}/api/cluster", token=token)
        check("集群状态接口可用", status == 200 and "scheduler" in (cluster or {}))

        status, denied = http("GET", f"{base}/api/notify/test", token=token)
        check("不存在的测试接口路径返回 404/405", status in (404, 405))

        # 直接读响应头验证安全策略
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
                headers = response.headers
            check("包含 X-Content-Type-Options", headers.get("X-Content-Type-Options") == "nosniff")
            check("包含 CSP", bool(headers.get("Content-Security-Policy")))
            check("包含 X-Request-Id", bool(headers.get("X-Request-Id")))
        except Exception as exc:  # noqa: BLE001
            check("读取安全响应头", False, str(exc))

        # ---------------- 数据目录检查（UI-13） ---------------- #
        print("\n[额外] 数据落盘位置检查…")
        check("数据库写入用户目录", (home / "data" / "app.db").exists())
        check("密钥写入用户目录", (home / "runtime" / "secret.key").exists())
        check("日志写入用户目录", (home / "logs" / "12306.log").exists())
        check("项目目录未被写入运行数据", not (ROOT / "data").exists())

    finally:
        print("\n" + "=" * 66)
        print(f"结果：通过 {len(PASSED)} 项 / 失败 {len(FAILED)} 项")
        if FAILED:
            print("失败明细：")
            for item in FAILED:
                print("  - " + item)
        print("=" * 66)

        if args.keep_alive:
            print(f"\n--keep-alive 已开启，服务继续运行：\n  Web:  http://127.0.0.1:{web_port}\n  账号: admin / smoke-pass\n  按 Ctrl+C 结束")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass

        for process in processes:
            try:
                process.terminate()
                process.wait(timeout=8)
            except Exception:  # noqa: BLE001
                try:
                    process.kill()
                except Exception:  # noqa: BLE001
                    pass

    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
