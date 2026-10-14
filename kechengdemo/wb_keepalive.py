# -*- coding: utf-8 -*-
"""微伴登录态保活 —— 无人值守刷新凭据（2026-10-13 实测设计）。

为什么不用账号密码？
------------------------
微伴后台 **没有账号密码登录入口**。登录页只有「企业微信扫码」一种方式
（实测：页面内 `密码` / `手机号` / `验证码` 关键词出现次数均为 0）。
微伴是企业微信服务商，登录必须走企微授权。

那怎么做自动化？
------------------------
微伴登录 session 是 **滑动过期** 的。实测：
    访问后台前  session_id 过期时间 = 10-20 21:00:59
    访问后台后  session_id 过期时间 = 10-20 21:17:58   （延后 923 秒）
即每访问一次，cookie 过期时间被重置为「当前时间 + 7 天」。
所以 **只要定期访问一次后台，登录态就永远不过期**。

两种用法
------------------------
**命令行**::

    python wb_keepalive.py            # 保活 + 刷新凭据
    python wb_keepalive.py --check    # 只校验当前凭据，不刷新
    python wb_keepalive.py --restart  # 强制重启浏览器（profile 被占死时用）

**被后端调用**（后端 scheduler / 同步流程直接 import）::

    import wb_keepalive
    wb_keepalive.ensure_credentials()   # 同步前的体检：有效就跳过，否则刷新
    wb_keepalive.refresh()              # 无条件刷新一次
    wb_keepalive.status()               # 取凭据与最近一次保活的状态

安全约定
------------------------
刷新时**先校验再写盘**：新 cookie 调不通 ``jsapi_sign`` 就拒绝写入，
保留原文件（写入前自动备份到 ``ww_bootstrap.bak.json``）。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE = r"C:\Users\EDY\.workbuddy\browser-profiles\wb-session"
CDP_PORT = 9222

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".cache"
BOOTSTRAP_FILE = CACHE_DIR / "ww_bootstrap.json"
BACKUP_FILE = CACHE_DIR / "ww_bootstrap.bak.json"
LOG_FILE = CACHE_DIR / "keepalive.log"

WB_SIGN_URL = "https://weibanzhushou.com/api/staff_client/jsapi_sign"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

DEFAULT_MAX_AGE_HOURS = 48      # 超过这个时长没刷新就主动保活一次（7 天窗口的安全余量）

# 最近一次保活/校验的结果（进程内状态，供 API 展示）
_last_run = {"at": None, "ok": None, "code": None, "message": None, "source": None}


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _record(ok: bool, code: int, message: str, source: str) -> dict:
    global _last_run
    _last_run = {"at": int(time.time()), "ok": ok, "code": code,
                 "message": message, "source": source}
    return _last_run


# ---------------------------------------------------------------- 凭据读写 / 校验

def load_bootstrap() -> dict | None:
    if not BOOTSTRAP_FILE.exists():
        return None
    try:
        return json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def verify(cookie_list: list) -> tuple[bool, str]:
    """调一次 jsapi_sign，判断微伴登录态是否有效。"""
    cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookie_list)
    url = "https://weibanzhushou.com/dashboard/messages"
    q = urllib.parse.urlencode({"url": url})
    req = urllib.request.Request(
        f"{WB_SIGN_URL}?{q}",
        headers={"cookie": cookie, "user-agent": UA,
                 "accept": "application/json, text/plain, */*", "referer": url})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8"))
        if res.get("success"):
            return True, f"ok (corpid={res.get('corpid')}, agentid={res.get('agentid')})"
        return False, f"返回 success=false：{json.dumps(res, ensure_ascii=False)[:160]}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}（登录态可能已失效）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_credentials() -> dict:
    """只校验当前凭据，不做任何刷新。"""
    boot = load_bootstrap()
    if not boot:
        return {"ok": False, "message": "没有凭据文件，请先运行 wb_bootstrap.py",
                "age_hours": None}
    ok, detail = verify(boot.get("wb_cookie") or [])
    age = (time.time() - (boot.get("captured_at") or 0)) / 3600
    return {"ok": ok, "message": detail, "age_hours": round(age, 2)}


def status() -> dict:
    """凭据文件 + 最近一次保活的状态（给 API 用）。"""
    boot = load_bootstrap() or {}
    cap = boot.get("captured_at")
    return {
        "credential": {
            "exists": bool(boot),
            "captured_at": cap,
            "age_hours": round((time.time() - cap) / 3600, 2) if cap else None,
            "open_sid_tail": ("…" + boot["open_sid"][-12:]) if boot.get("open_sid") else None,
            "cookie_count": len(boot.get("wb_cookie") or []),
        },
        "last_run": dict(_last_run),
        "browser": {"cdp_port": CDP_PORT, "alive": cdp_alive()},
    }


# ---------------------------------------------------------------- 浏览器

def cdp_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def launch_chrome(url: str):
    """拉起常驻 Chrome（带已登录 profile）。"""
    log("未发现可用 CDP，拉起 Chrome…")
    subprocess.Popen([
        CHROME, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={PROFILE}",
        "--no-first-run", "--no-default-browser-check", "--no-sandbox",
        "--disable-session-crashed-bubble", "--window-size=1400,950", url,
    ])


def harvest(url: str, wait_sec: int = 45) -> dict:
    """连 CDP，刷新会话页，抓最新 cookie + open_sid。"""
    from playwright.sync_api import sync_playwright

    captured = {}
    with sync_playwright() as p:
        browser = None
        if not cdp_alive():
            launch_chrome(url)
        for _ in range(30):
            try:
                browser = p.chromium.connect_over_cdp(CDP_URL)
                break
            except Exception:
                time.sleep(2)
        if browser is None:
            raise RuntimeError("无法连接 Chrome（CDP 连接失败）")

        ctx = browser.contexts[0]

        # 优先复用已打开的微伴后台标签页（保活不打扰用户）
        page = None
        for pg in ctx.pages:
            if "weibanzhushou.com/dashboard" in pg.url:
                page = pg
                break
        new_page = page is None
        if new_page:
            page = ctx.new_page()

        def on_request(req):
            if "createOpenChatSession" in req.url and "body" not in captured:
                try:
                    captured["body"] = json.loads(req.post_data or "{}")
                except Exception:
                    pass

        page.on("request", on_request)

        log(f"{'打开' if new_page else '刷新'}会话页：{url[:90]}…")
        try:
            if new_page:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            else:
                page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"导航告警（继续）：{str(e)[:100]}")

        deadline = time.time() + wait_sec
        while time.time() < deadline and "body" not in captured:
            page.wait_for_timeout(1200)

        cookies = ctx.cookies()
        if new_page:
            try:
                page.close()
            except Exception:
                pass

    body = captured.get("body") or {}
    return {
        "open_sid": body.get("open_sid"),
        "open_sid_meta": body.get("open_sid_meta"),
        "url": body.get("url") or "https://weibanzhushou.com/dashboard/messages",
        "agent_config_params": body.get("agent_config_params") or {},
        "wb_cookie": [c for c in cookies if "weibanzhushou.com" in c.get("domain", "")],
        "ww_cookie": [c for c in cookies if "work.weixin.qq.com" in c.get("domain", "")],
        "captured_at": int(time.time()),
    }


def pick_session_url() -> str:
    """挑一个会触发组件的会话页 URL：优先上次记录的，其次库里的最新会话。"""
    boot = load_bootstrap() or {}
    prev = boot.get("url") or ""
    if "sessionExtId=" in prev:
        return prev

    # 从数据库取一个最近有消息的会话
    try:
        sys.path.insert(0, str(ROOT / "backend"))
        sys.path.insert(0, str(ROOT))
        import db  # noqa
        row = db.query_one("""
            select s.staff_id, s.session_id
            from chat_session s
            where exists (select 1 from chat_message m where m.session_id = s.session_id)
            order by s.last_msg_time desc nulls last
            limit 1
        """)
        if row:
            return ("https://weibanzhushou.com/dashboard/messages"
                    f"#type=staff&sessionType=ext_user"
                    f"&staffExtId={row['staff_id']}&sessionExtId={row['session_id']}")
    except Exception as e:
        log(f"从数据库取会话失败（忽略）：{str(e)[:80]}")

    return "https://weibanzhushou.com/dashboard/messages"


# ---------------------------------------------------------------- 核心动作

def refresh(wait_sec: int = 45, restart: bool = False, source: str = "manual") -> dict:
    """执行一次保活刷新。返回 {ok, code, message, ...}，不会抛异常。"""
    if restart:
        log("强制关闭旧 Chrome…")
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)

    CACHE_DIR.mkdir(exist_ok=True)
    url = pick_session_url()
    try:
        data = harvest(url, wait_sec)
    except Exception as e:
        msg = f"保活失败：{e}"
        log("❌ " + msg)
        _record(False, 1, msg, source)
        return {"ok": False, "code": 1, "message": msg}

    new_cookies = data.get("wb_cookie") or []
    if not new_cookies:
        msg = "未取到微伴 cookie，放弃写入（保留原凭据）"
        log("❌ " + msg)
        _record(False, 1, msg, source)
        return {"ok": False, "code": 1, "message": msg}

    ok, detail = verify(new_cookies)
    if not ok:
        msg = f"新 cookie 校验失败（{detail}），可能需要人工扫码重新登录"
        log("❌ " + msg)
        _record(False, 2, msg, source)
        return {"ok": False, "code": 2, "message": msg}

    old = load_bootstrap()
    if old:
        shutil.copy2(BOOTSTRAP_FILE, BACKUP_FILE)

    if not data.get("open_sid") and old:
        data["open_sid"] = old.get("open_sid")
        data["open_sid_meta"] = old.get("open_sid_meta")
        log("本次未捕获 open_sid，沿用旧值")

    if not data.get("open_sid"):
        msg = "既没抓到 open_sid 也没有旧值，放弃写入"
        log("❌ " + msg)
        _record(False, 1, msg, source)
        return {"ok": False, "code": 1, "message": msg}

    BOOTSTRAP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    msg = f"凭据已刷新：{detail}"
    log("✅ " + msg)
    log(f"   open_sid   : …{data['open_sid'][-12:]}")
    log(f"   微伴 cookie: {len(data['wb_cookie'])} 个 / 企微 cookie: {len(data['ww_cookie'])} 个")
    _record(True, 0, msg, source)
    return {"ok": True, "code": 0, "message": msg,
            "captured_at": data.get("captured_at"),
            "open_sid_tail": "…" + data["open_sid"][-12:]}


def ensure_credentials(max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
                       source: str = "auto") -> dict:
    """同步前的凭据体检：有效且够新鲜就跳过，否则触发保活。

    返回 ``{ok, code, message, skipped?}``；``skipped=True`` 表示无需刷新。
    """
    boot = load_bootstrap()
    if not boot:
        msg = "缺少凭据文件，需人工扫码一次：python wb_bootstrap.py"
        log("❌ " + msg)
        _record(False, 3, msg, source)
        return {"ok": False, "code": 3, "message": msg}

    ok, detail = verify(boot.get("wb_cookie") or [])
    age = (time.time() - (boot.get("captured_at") or 0)) / 3600

    if ok and age < max_age_hours:
        msg = f"凭据有效（{age:.1f} 小时前刷新），无需保活"
        _record(True, 0, msg, source)
        return {"ok": True, "code": 0, "skipped": True,
                "message": msg, "age_hours": round(age, 2)}

    log(f"凭据需要刷新（有效={ok}，已 {age:.1f} 小时未刷新），开始保活…")
    return refresh(source=source)


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="微伴登录态保活 / 凭据刷新")
    ap.add_argument("--check", action="store_true", help="只校验当前凭据，不刷新")
    ap.add_argument("--wait", type=int, default=45, help="等待组件触发的秒数")
    ap.add_argument("--restart", action="store_true", help="先关掉旧 Chrome 再重来")
    ap.add_argument("--status", action="store_true", help="打印状态 JSON")
    args = ap.parse_args()

    CACHE_DIR.mkdir(exist_ok=True)

    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0

    if args.check:
        res = check_credentials()
        age = res.get("age_hours")
        log(f"{'✅' if res['ok'] else '❌'} 凭据校验：{res['message']}"
            + (f"（抓取于 {age:.1f} 小时前）" if age is not None else ""))
        return 0 if res["ok"] else 1

    res = refresh(wait_sec=args.wait, restart=args.restart, source="cli")
    return 0 if res["ok"] else res.get("code", 1)


if __name__ == "__main__":
    sys.exit(main())
