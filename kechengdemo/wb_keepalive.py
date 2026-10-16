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
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 浏览器路径与生命周期统一由 wb_bootstrap 管理（可配置 + 只关自己的实例）
import wb_bootstrap  # noqa: E402

# 本脚本常被 cmd.exe 直接跑（GBK 控制台）→ 与 wb_bootstrap 共用同一份输出防崩逻辑，
# 否则失败分支里的 `❌` 会把 print 打挂（`UnicodeEncodeError`）。
wb_bootstrap.prepare_console_output()

CHROME = wb_bootstrap.CHROME
PROFILE = wb_bootstrap.PROFILE
CDP_PORT = wb_bootstrap.CDP_PORT
CACHE_DIR = ROOT / ".cache"
BOOTSTRAP_FILE = CACHE_DIR / "ww_bootstrap.json"
BACKUP_FILE = CACHE_DIR / "ww_bootstrap.bak.json"
LOG_FILE = CACHE_DIR / "keepalive.log"

WB_SIGN_URL = "https://weibanzhushou.com/api/staff_client/jsapi_sign"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

DEFAULT_MAX_AGE_HOURS = 48      # 超过这个时长没刷新就主动保活一次（7 天窗口的安全余量）

#: 保活失败码：4 = **当前环境弹不出浏览器**（容器内 / 未装 Chrome）。
#: 与 1（抓取失败）/ 2（cookie 校验失败）/ 3（缺凭据文件）区分开 —— 这三种要修代码或
#: 重新扫码，而 4 是「换个地方执行就行」，处置完全不同，混在一起会误判。
CODE_NO_BROWSER = 4
#: 5 = **微伴 cookie 刷到了，但企微正文通道仍然不可用**（实测 42006 missing session）。
#: 这一档必须单独存在：它**不是**"保活失败"，而是"保活做完了，通道还是断的" ——
#: 处置是"去宿主机打开后台点进一个会话/重新扫码"，与 1/2 的重试逻辑不同。
CODE_CHANNEL_DOWN = 5

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
    _persist_state(_last_run)
    return _last_run


def _persist_state(rec: dict) -> None:
    """把保活结果**落库**（修 P12），供 `status()` 与 `/api/eval/alerts` 读取。

    为什么要有它：改造前状态只写在 `_last_run` 这个模块级全局变量里 —— **重启即失**，
    于是 `GET /api/sync/keepalive` 的 `last_run` 恒为 null，"上次成功保活是什么时候"
    没有任何地方记得住。正文通道 2026-10-13 断掉、同步空转两天才被发现，
    根因之一就是这里没有可查、可告警的事实。

    ⚠️ 两条纪律：
    · 落库失败**绝不能影响保活本身**（本脚本跑在宿主机，可能比库先起来，
      也可能在没有库的环境里当纯 CLI 用）；
    · 单独抽成一个函数是**为了让测试替身它** —— `tests/test_channel_check.py` 会连调
      `refresh()` 三次，若真落库就会把"最近一次保活"写成测试结果（同步页上直接可见）。
    """
    try:
        if str(ROOT / "backend") not in sys.path:
            sys.path.insert(0, str(ROOT / "backend"))
        import db                                  # noqa: PLC0415
        db.set_setting("keepalive_last", json.dumps(rec, ensure_ascii=False))
    except Exception as e:                         # noqa: BLE001
        log(f"（保活状态未落库：{type(e).__name__}: {e}）")


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
    benv = wb_bootstrap.browser_env()
    return {
        "credential": {
            "exists": bool(boot),
            "captured_at": cap,
            "age_hours": round((time.time() - cap) / 3600, 2) if cap else None,
            "open_sid_tail": ("…" + boot["open_sid"][-12:]) if boot.get("open_sid") else None,
            "cookie_count": len(boot.get("wb_cookie") or []),
        },
        "last_run": dict(_last_run),
        # `available=False` 说明"这台机器根本弹不出浏览器"（容器部署的常态），
        # 与 `alive`（CDP 连不连得上）是两件事，必须分开看：
        # 前者要换地方执行，后者只是"还没把 Chrome 拉起来"。
        "browser": {"cdp_port": CDP_PORT, "alive": cdp_alive(),
                    "available": benv["available"], "reason": benv["reason"],
                    "hint": benv["hint"], "in_container": benv["in_container"]},
    }


# ---------------------------------------------------------------- 浏览器

def cdp_alive() -> bool:
    """CDP 是否在线。实现统一在 `wb_bootstrap`（浏览器生命周期只有一份）。"""
    return wb_bootstrap.cdp_alive(CDP_URL)


def launch_chrome(url: str):
    """拉起常驻 Chrome（带已登录 profile）。统一走 wb_bootstrap（会记录 PID）。"""
    log("未发现可用 CDP，拉起 Chrome…")
    wb_bootstrap.launch_chrome(url)


def harvest(url: str, wait_sec: int = 45, reset_session: bool = False) -> dict:
    """连 CDP、刷新会话页、抓最新 cookie + open_sid。

    **复用** `wb_bootstrap.harvest` —— 这里原先是照抄一份的实现，于是「取最新的
    open session」只在另一处修好、这一处照旧复用旧值（2026-10-16 实测被咬：
    保活连抓三次都拿到 3 天前的会话）。差异只保留在参数上：
    `reuse_tab`（复用已打开的后台标签页，不打扰用户）+ `launch_if_absent`
    （没有 CDP 才拉起浏览器）。
    """
    if not cdp_alive():
        launch_chrome(url)
    log(f"刷新会话页：{url[:90]}…")
    return wb_bootstrap.harvest(url, wait_sec, reset_session=reset_session,
                                reuse_tab=True, launch_if_absent=True)


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

def _capture_and_write(url: str, wait_sec: int, *, reset_session: bool,
                       source: str) -> dict:
    """抓一次 → 校验微伴 cookie → 写盘 → 端到端自检。返回 {ok, code, message, ...}。

    抽成函数是为了让 :func:`refresh` 的「抓一次不成就清缓存重抓」复用同一条路径 ——
    复制一份"差不多的抓取流程"正是 `AGENTS.md` 禁止的那种写法。
    """
    try:
        data = harvest(url, wait_sec, reset_session=reset_session)
    except Exception as e:                                # noqa: BLE001
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
    log(f"   写入凭据：open_sid …{data['open_sid'][-12:]}"
        f"（取自 {data.get('open_sid_source')}）"
        f"｜微伴 cookie {len(data['wb_cookie'])} 个 / 企微 cookie {len(data['ww_cookie'])} 个")

    # ⚠️ 写盘成功 ≠ 通道可用。企微那侧的 open session 会**先于**微伴 cookie 失效，
    #    而它与微伴 cookie 不是同一把钥匙 —— 只看 `verify()`（微伴 jsapi_sign）就报
    #    「✅ 凭据已刷新」会掩盖真实故障：实测 2026-10-13 起正文通道断了 3 天，
    #    保活日志一路绿，没人发现（仓库设计文档 P12 记的就是这一条）。
    chk = {"ok": False, "error": "未执行自检"}
    try:
        import ww_chatdata
        chk = ww_chatdata.check_channel()
    except Exception as e:                                # noqa: BLE001
        chk = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if not chk.get("ok"):
        msg = (f"微伴 cookie 已刷新（{detail}），但**企微正文通道不可用**："
               f"{chk.get('error') or chk.get('message')}")
        log("❌ " + msg)
        _record(False, CODE_CHANNEL_DOWN, msg, source)
        return {"ok": False, "code": CODE_CHANNEL_DOWN, "message": msg,
                "captured_at": data.get("captured_at"),
                "hint": ("去宿主机把微伴后台打开并点进任意一个会话（让组件重建 open session），"
                         "或重新扫码：host_bootstrap_login.bat。"
                         "容器内没有浏览器，这一步必须在宿主机做。"),
                "channel": chk}

    msg = f"凭据已刷新，{chk.get('message')}（{detail}）"
    log("✅ " + msg)
    _record(True, 0, msg, source)
    return {"ok": True, "code": 0, "message": msg,
            "captured_at": data.get("captured_at"),
            "open_sid_tail": "…" + data["open_sid"][-12:],
            "channel": chk}


def refresh(wait_sec: int = 45, restart: bool = False, source: str = "manual") -> dict:
    """执行一次保活刷新。返回 {ok, code, message, ...}，不会抛异常。

    **两段式**：先照常抓一次；若抓到的企微会话已经失效（端到端自检不通），
    就清掉缓存的旧 open session **重抓一次**。

    为什么必须有第二段（2026-10-16 实测）：页面把 open session 缓存进 iframe 的
    localStorage 后会**一直复用**它 —— 三次保活抓到的都是同一个 3 天前的值，
    而企微那边早就回 `42006 missing session`。只会"再抓一次"是没用的，
    必须先把缓存清掉，页面才会去换发新的。
    """
    # 环境体检放在最前面：容器里既没有 Chrome 也没有显示器，继续往下走只会得到
    # `[Errno 2] No such file or directory: 'C:\Program Files\...\chrome.exe'` ——
    # 那句话既不能定位问题，也没告诉人该干什么（保活只能靠宿主机执行）。
    benv = wb_bootstrap.browser_env()
    if not benv["available"]:
        msg = f"保活需要浏览器，但当前环境不可用：{benv['reason']}"
        log(f"❌ {msg}")
        log(f"   {benv['hint']}")
        _record(False, CODE_NO_BROWSER, msg, source)
        return {"ok": False, "code": CODE_NO_BROWSER, "message": msg,
                "hint": benv["hint"], "browser": benv}

    if restart:
        # 只关本项目自己的 Chrome 实例（绝不无差别杀掉用户其他 Chrome 窗口）
        log("重启浏览器：关闭本项目 Chrome 实例…")
        res = wb_bootstrap.kill_own_chrome(wait_sec=3)
        log(f"已关闭本项目 Chrome 实例 {res.get('count', 0)} 个")

    CACHE_DIR.mkdir(exist_ok=True)
    url = pick_session_url()

    last = {"ok": False, "code": 1, "message": "未执行"}
    for reset in (False, True):
        if reset:
            log("通道不可用 → 清掉页面缓存的旧企微会话，重抓一次…")
        res = _capture_and_write(url, wait_sec, reset_session=reset, source=source)
        if res.get("ok"):
            return res
        last = res
        # 只有"通道不通"值得清缓存重抓；其它失败（没抓到请求 / cookie 校验失败）
        # 重抓一次也是一样的结果，白等一轮。
        if res.get("code") != CODE_CHANNEL_DOWN:
            return res
    return last


def ensure_credentials(max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
                       source: str = "auto") -> dict:
    """同步前的凭据体检：**微伴 cookie 新鲜 + 正文通道可用**才跳过，否则触发保活。

    返回 ``{ok, code, message, skipped?}``；``skipped=True`` 表示无需刷新。

    ⚠️ 为什么要连**通道**一起验（而不是只看 cookie 年龄）：两把凭据的失效节奏不同 ——
    微伴 cookie 滑动过期、刷一次续 7 天；企微 ``open_sid`` 是页面缓存的会话，**会先死**。
    只看年龄的话，通道最多可能断 48 小时才被发现（实测就是这么断了 3 天）。
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
        chk = {"ok": True, "message": ""}
        try:
            import ww_chatdata
            chk = ww_chatdata.check_channel()
        except Exception as e:                            # noqa: BLE001
            # 自检本身不可用（模块缺失等）→ 退回"只判 cookie 年龄"的旧行为，
            # 不能因为体检工具坏了就把同步拦下或误报故障。
            log(f"通道自检跳过（{type(e).__name__}: {e}）")
        if chk.get("ok"):
            msg = f"凭据有效（{age:.1f} 小时前刷新），无需保活"
            _record(True, 0, msg, source)
            return {"ok": True, "code": 0, "skipped": True,
                    "message": msg, "age_hours": round(age, 2), "channel": chk}

        # cookie 新鲜但通道断了：有浏览器就重建会话，没有就如实上报 + 给操作指引
        benv = wb_bootstrap.browser_env()
        if benv["available"]:
            log(f"凭据 cookie 新鲜但正文通道不可用（{chk.get('error') or chk.get('message')}），"
                f"尝试通过浏览器重建会话…")
            return refresh(source=source)
        msg = (f"微伴 cookie 有效（{age:.1f} 小时前刷新），但**企微正文通道不可用**："
               f"{chk.get('error') or chk.get('message')}")
        log("❌ " + msg)
        _record(False, CODE_CHANNEL_DOWN, msg, source)
        return {"ok": False, "code": CODE_CHANNEL_DOWN, "message": msg,
                "age_hours": round(age, 2), "hint": benv["hint"], "browser": benv,
                "channel": chk}

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
