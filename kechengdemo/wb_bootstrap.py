# -*- coding: utf-8 -*-
"""抓取企微会话存档正文接口所需的凭据（浏览器引导，只需偶尔跑一次）。

为什么还需要浏览器？
    ① 微伴登录态（cookie）            —— 用于实时生成 jsapi_sign 签名
    ② 企微 ``wwopen.open.sid`` cookie —— 即组件的 ``open_sid``，只能由企微下发
    这两样拿到后会缓存到 ``.cache/ww_bootstrap.json``，之后所有抓取都是纯 HTTP。

用法::

    python wb_bootstrap.py                # 自动挑一个会话页打开并抓取
    python wb_bootstrap.py --staff-id XXX --session-id YYY

前提：``BROWSER_PROFILE_DIR``（默认 ``~/.workbuddy/browser-profiles/wb-session``）这个
Chrome 配置里已登录过微伴（首次运行脚本会停在该窗口等你登录）。
Chrome 窗口**不会自动关闭**，扫码后停在窗口里即可。

浏览器路径可用环境变量 ``CHROME_PATH``（若设置但不生效，会回落到注册表与常见安装位置）
/ ``BROWSER_PROFILE_DIR`` / ``CDP_PORT`` 覆盖；``BROWSER_ENABLED=0`` 可显式关掉浏览器能力。

⚠️ **这个脚本只能在宿主机跑**：容器里没有 Chrome、也没有显示器（见 `browser_env()`）。
容器部署时请用 `host_bootstrap_login.bat`，凭据写入 `.cache/` 后容器会自动读到。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

# ---------------------------------------------------------------- 浏览器能力（唯一判定）
#
# 为什么要有这一段：后端既可能跑在**宿主机**（有 Chrome、有桌面），也可能跑在
# **容器**里（没有 Chrome、没有显示器）。改造前三个入口（扫码登录 / 保活 / 导出脚本）
# 各自直接 `Popen(chrome.exe)`，在容器里一律表现为：
#     [Errno 2] No such file or directory: 'C:\Program Files\...\chrome.exe'
# 用户既看不懂、也不知道下一步该做什么。
#
# 现在把「这个环境能不能弹浏览器」收敛成**一个判定**（`browser_env()`）：
#   · 容器内 / 未装 Chrome / 显式关闭 → 不可用，并给出**宿主机该执行的命令**；
#   · 每个真正的启动动作都先过它，谁也不会再抛出那个裸 Errno。
#
# 判定顺序：`BROWSER_ENABLED=0`（显式关）→ 容器标记 → Chrome 是否真的存在。

#: 历史默认路径（现在只是候选之一，不再是唯一来源）
LEGACY_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

#: 容器的兜底提示里给的命令名（与仓库根目录下的 .bat 同名，用户可直接照抄）
HOST_LOGIN_CMD = "host_bootstrap_login.bat"
HOST_HINT = (f"浏览器流程必须在宿主机执行（容器内没有 Chrome 与显示器）：在项目目录"
             f"双击 {HOST_LOGIN_CMD}，或运行 `python wb_bootstrap.py`；"
             f"凭据写入 .cache/ 后容器会自动读到。")

_FALSY = frozenset({"0", "false", "no", "off", "none", ""})


def prepare_console_output() -> None:
    """命令行输出防崩：把控制台编码**编不出**的字符替换掉（不改编码本身）。

    为什么需要：失败分支会打印 ``❌ ...``。Windows 默认控制台是 GBK（cmd.exe 直接跑
    `python wb_keepalive.py`）时，``print`` 会抛
    ``UnicodeEncodeError: 'gbk' codec can't encode character '\\u274c'`` ——
    于是**失败信息本身把进程打挂**，而且崩在「凭据已写完、还没记账」的位置，
    比原始故障更难排查（真踩到过）。

    只放宽 ``errors`` 而不改 ``encoding``：改成 utf-8 会让中文在 GBK 控制台变乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:                                   # noqa: BLE001
            pass


# 本模块既被后端 import，也被命令行直接跑 —— 在入口就把输出修好
prepare_console_output()

PROFILE = (os.environ.get("BROWSER_PROFILE_DIR")
           or str(Path.home() / ".workbuddy" / "browser-profiles" / "wb-session"))
CDP_PORT = int(os.environ.get("CDP_PORT") or 9222)


def _env_flag(name: str):
    """读三态环境变量：未设置 → ``None``；真 / 假 → ``True`` / ``False``。"""
    raw = os.environ.get(name)
    return None if raw is None else (raw.strip().lower() not in _FALSY)


def in_container() -> bool:
    """当前进程是否跑在容器里。

    `BROWSER_IN_CONTAINER` 显式设置时以它为准（便于测试与特殊编排），
    否则看容器标记文件 —— 这比"猜"可靠。
    """
    flag = _env_flag("BROWSER_IN_CONTAINER")
    if flag is not None:
        return flag
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def _registry_chrome():
    """从 Windows 注册表取 Chrome 安装路径（比猜目录可靠，兼容自定义安装盘）。"""
    try:
        import winreg
    except Exception:                                   # 非 Windows / 被裁剪
        return None
    app_paths = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    for hive, key in ((winreg.HKEY_CURRENT_USER, app_paths),
                      (winreg.HKEY_LOCAL_MACHINE, app_paths),
                      (winreg.HKEY_LOCAL_MACHINE,
                       "SOFTWARE\\WOW6432Node\\" + app_paths)):
        try:
            with winreg.OpenKey(hive, key) as h:
                val = winreg.QueryValueEx(h, "")[0]
        except OSError:
            continue
        if val and Path(str(val)).exists():
            return str(val)
    return None


def chrome_candidates() -> list:
    """常见安装位置（用环境变量拼，兼容非 C 盘与非标准用户名）。"""
    out = []
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            out.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    out.append(LEGACY_CHROME)
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def chrome_path() -> tuple:
    """返回 ``(路径, 来源)``；都找不到 → ``(None, None)``。

    顺序：``CHROME_PATH`` 环境变量 → Windows 注册表 → 常见安装位置。
    """
    env_path = (os.environ.get("CHROME_PATH") or "").strip()
    if env_path and Path(env_path).exists():
        return env_path, "env:CHROME_PATH"
    reg = _registry_chrome()
    if reg:
        return reg, "registry"
    for cand in chrome_candidates():
        if Path(cand).exists():
            return cand, "common"
    return None, None


def browser_env() -> dict:
    """**浏览器能力的唯一判定**：能不能弹？为什么不能？该怎么办？

    后端（`auth_service` / `wb_keepalive` / `sync_service`）与前端「凭据」页都读它，
    所以"某处说能、另一处报 Errno"这类不一致不会再出现。
    """
    enabled = _env_flag("BROWSER_ENABLED")
    path, source = chrome_path()
    env = {
        "available": True,
        "enabled": enabled is not False,
        "in_container": in_container(),
        "chrome": path,
        "chrome_source": source,
        "profile": PROFILE,
        "cdp_port": CDP_PORT,
        "reason": "",
        "hint": "",
        "host_command": HOST_LOGIN_CMD,
    }
    # ⚠️ 顺序有意为之：**先判容器，再判开关**。
    #    容器里 compose 本来就设了 BROWSER_ENABLED=0，若先判开关，用户会看到
    #    「去掉 BROWSER_ENABLED=0 后重启即可恢复」—— 而在容器里那是**错误建议**
    #    （容器里根本没有 Chrome，去掉开关也照样弹不出来）。容器场景该给的
    #    指引只有一条：去宿主机跑 host_bootstrap_login.bat。
    if env["in_container"]:
        env.update(available=False,
                   reason="后端运行在容器内：容器里没有 Chrome、也没有显示器，弹不出扫码窗口。",
                   hint=HOST_HINT)
        return env
    if enabled is False:
        env.update(available=False, reason="浏览器能力已显式关闭（BROWSER_ENABLED=0）",
                   hint="把 .env / 环境变量里的 BROWSER_ENABLED=0 去掉后重启，即可恢复本机扫码登录。")
        return env
    if not path:
        custom = (os.environ.get("CHROME_PATH") or "").strip()
        why = (f"（CHROME_PATH={custom} 指向的文件不存在）" if custom else "：")
        env.update(available=False,
                   reason=f"未找到 Chrome{why}候选路径 {'、'.join(chrome_candidates())}",
                   hint="在 .env 里把 CHROME_PATH 指到本机 chrome.exe，或安装 Chrome。")
        return env
    return env


class BrowserUnavailable(RuntimeError):
    """当前环境弹不出浏览器（容器内 / 未装 Chrome / 显式关闭）。

    继承 ``RuntimeError`` 是为了**兼容既有调用方的 ``except RuntimeError``**；
    额外带上 ``env``，让上层能把原因与操作指引原样透给用户。
    """

    def __init__(self, env: dict):
        self.env = env or {}
        super().__init__(f"{self.env.get('reason', '')} {self.env.get('hint', '')}".strip())

    @property
    def reason(self) -> str:
        return self.env.get("reason") or ""

    @property
    def hint(self) -> str:
        return self.env.get("hint") or ""


def require_browser() -> dict:
    """可用则返回 :func:`browser_env`，否则抛 :class:`BrowserUnavailable`。"""
    env = browser_env()
    if not env["available"]:
        raise BrowserUnavailable(env)
    return env


#: 兼容既有引用（`wb_keepalive` / `wb_chat_export` 都 import 过它）。
#: 可用性判断**不要**读这个常量 —— 一律走 :func:`browser_env`（它会现查磁盘与注册表）。
CHROME = chrome_path()[0] or LEGACY_CHROME

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".cache"
BOOTSTRAP_FILE = CACHE_DIR / "ww_bootstrap.json"
PID_FILE = CACHE_DIR / "chrome.pid"

MSG_URL = ("https://weibanzhushou.com/dashboard/messages"
           "#type=staff&sessionType=ext_user&staffExtId={s}&sessionExtId={c}")


def pick_staff_session(staff_id: str = None, session_id: str = None):
    """没指定就用微伴 OpenAPI 自动挑一个「最近有消息」的会话。"""
    if staff_id and session_id:
        return staff_id, session_id
    import weiban
    cands = []
    for st in weiban.iter_all_staff():
        if st.get("status") not in (None, 1):
            continue
        cands.append(st)
        if len(cands) >= 25:
            break
    for st in cands:
        try:
            res = weiban.list_sessions(st["id"], "ext_user", limit=1, offset=0,
                                       order_by="last_updated_desc")
        except Exception:
            continue
        lst = res.get("session_list") or []
        if lst:
            print(f"自动选中员工：{st.get('name')} / 会话：{lst[0].get('name')}")
            return st["id"], lst[0]["id"]
    raise RuntimeError("未能自动找到可用会话，请用 --staff-id/--session-id 手动指定")


# ---------------------------------------------------------------- 浏览器生命周期
#
# ⚠️ 这里**只**管理本项目自己启动的 Chrome（独立 user-data-dir = PROFILE）。
# 曾经的实现用 `taskkill /F /IM chrome.exe`，会**无差别杀掉用户所有 Chrome 窗口**
# （未提交的表单、进行中的下载一并丢失），且被定时任务自动触发 —— 属数据丢失级副作用。
# 现在改为：优先杀「记录在案的 PID（含进程树）」，其次按 profile 精确匹配，
# 找不到自己的实例就什么都不做；全量杀进程必须显式传 force_all=True。


def launch_chrome(url: str = "about:blank") -> "subprocess.Popen":
    """拉起本项目专用 Chrome（独立 profile + CDP），并记录主进程 PID。

    **先过 :func:`require_browser`**：这是「弹浏览器」的唯一落地点，三个入口
    （扫码登录 / 保活 / 导出脚本）都汇到这里，所以在这一处挡住即可 ——
    在容器里得到的是「该去宿主机执行什么」，而不是 `FileNotFoundError`。
    """
    require_browser()
    p = subprocess.Popen([
        CHROME, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={PROFILE}",
        "--no-first-run", "--no-default-browser-check", "--no-sandbox",
        "--disable-session-crashed-bubble", "--window-size=1400,950", url,
    ])
    try:
        PID_FILE.parent.mkdir(exist_ok=True)
        PID_FILE.write_text(str(p.pid), encoding="utf-8")
    except Exception:
        pass
    return p


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _clear_pid() -> None:
    try:
        PID_FILE.unlink()
    except Exception:
        pass


def _kill_pid_tree(pid: int) -> bool:
    """杀一个 PID 及其子进程（/T）。返回是否成功。"""
    try:
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def _pids_by_profile() -> list:
    """按 `--user-data-dir=<PROFILE>` 精确找出本项目 Chrome 的 PID（PID 文件丢失时的兜底）。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{PROFILE}*' }} | "
          "Select-Object -ExpandProperty ProcessId")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=20)
        return [int(x) for x in (out.stdout or "").split() if x.strip().isdigit()]
    except Exception:
        return []


def kill_own_chrome(wait_sec: float = 2.0, force_all: bool = False) -> dict:
    """关闭**本项目启动**的 Chrome。绝不波及其他 Chrome 窗口。

    ``force_all=True`` 才执行历史上的全量 ``taskkill /F /IM chrome.exe``
    （会关掉用户所有 Chrome，仅在人工明确要求时使用）。
    """
    if force_all:
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _clear_pid()
        if wait_sec:
            time.sleep(wait_sec)
        return {"ok": True, "killed": [], "count": 0, "forced_all": True,
                "note": "已按显式要求关闭全部 Chrome"}

    killed = []
    pid = _read_pid()
    if pid and _kill_pid_tree(pid):
        killed.append(pid)
    _clear_pid()
    for p in _pids_by_profile():          # 兜底：PID 丢失 / 实例已被复用
        if p not in killed and _kill_pid_tree(p):
            killed.append(p)

    if wait_sec and killed:
        time.sleep(wait_sec)
    return {"ok": True, "killed": killed, "count": len(killed), "forced_all": False}


#: 「页面正在用的那个 open session」缓存在哪（2026-10-16 实测）
#:   · 值本身：企微组件 iframe（`open.work.weixin.qq.com`）的 localStorage 键
#:     —— 组件源码：`if (!(n = localStorage.getItem("ww.open.sid"))) throw new Error("Missing open sid")`
#:   · 同值也出现在 cookie `wwopen.open.sid`
#: 所以「换发新会话」= 清掉这两处，让页面重新问微伴要一个。
WW_FRAME_HOST = "open.work.weixin.qq.com"
OPEN_SID_LS_KEYS = ("ww.open.sid", "ww.open.sid.meta")
OPEN_SID_COOKIE = "wwopen.open.sid"


def _meta_ts(meta) -> int:
    """`open_sid_meta` 里的时间戳（用于比较两个候选哪个更新）。取不到 → 0。"""
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:                                   # noqa: BLE001
            return 0
    if isinstance(meta, dict):
        try:
            return int(meta.get("ts") or 0)
        except Exception:                                   # noqa: BLE001
            return 0
    return 0


def read_open_session_from_page(page) -> dict:
    """从企微组件 iframe 的 localStorage 读**页面当前正在用**的 open session。

    为什么不能只看抓到的请求体：请求体是"那一刻的值"，而页面**会自己换发**
    （实测：抓到的请求带的是 3 天前的旧 sid，51 秒后 localStorage 换成了新的）。
    旧值保存下来 → 企微回 `42006 missing session`，通道看起来"莫名断了"。
    """
    out = {"open_sid": None, "open_sid_meta": None, "frame": None}
    try:
        frames = page.frames
    except Exception:                                       # noqa: BLE001
        return out
    for fr in frames:
        if WW_FRAME_HOST not in (fr.url or ""):
            continue
        try:
            sid = fr.evaluate("() => localStorage.getItem('ww.open.sid')")
            meta = fr.evaluate("() => localStorage.getItem('ww.open.sid.meta')")
        except Exception:                                   # noqa: BLE001
            continue
        if sid:
            out.update(open_sid=sid, open_sid_meta=meta, frame=fr.url)
            return out
    return out


def reset_open_session(page, ctx) -> dict:
    """清掉缓存的旧企微 open session，逼页面重新换发一个。

    为什么需要：页面把 open session 缓存在 iframe 的 localStorage 里，只要它在那儿，
    页面就**一直复用**它（实测三次保活拿到的都是同一个 3 天前的值）—— 而企微那边
    这个会话早就 `42006 missing session` 了。不清缓存，自动流程永远换不出新会话。
    """
    out = {"cleared_localStorage": [], "cleared_cookies": [], "error": None}
    try:
        for fr in page.frames:
            if WW_FRAME_HOST not in (fr.url or ""):
                continue
            fr.evaluate("(keys) => keys.forEach(k => localStorage.removeItem(k))",
                        list(OPEN_SID_LS_KEYS))
            out["cleared_localStorage"] = list(OPEN_SID_LS_KEYS)
            break
    except Exception as e:                                  # noqa: BLE001
        out["error"] = f"清 localStorage 失败：{type(e).__name__}: {e}"
    try:
        ctx.clear_cookies(name=OPEN_SID_COOKIE)
        out["cleared_cookies"] = [OPEN_SID_COOKIE]
    except Exception as e:                                  # noqa: BLE001
        out["error"] = (out["error"] or "") + f" 清 cookie 失败：{type(e).__name__}: {e}"
    return out


def cdp_alive(url: str = None) -> bool:
    """本项目专用 Chrome 的 CDP 是否在线（浏览器生命周期的一部分，与启动同处一地）。"""
    import urllib.request
    target = url or f"http://127.0.0.1:{CDP_PORT}/json/version"
    try:
        with urllib.request.urlopen(target, timeout=3) as r:
            return r.status == 200
    except Exception:                                       # noqa: BLE001
        return False


def harvest(target_url: str, wait_sec: int = 90, reset_session: bool = False,
            reuse_tab: bool = False, launch_if_absent: bool = False) -> dict:
    """打开会话页 → 捕获组件请求 → 取**最新**的 open session。

    :param reset_session: 先清掉缓存的旧 open session 再让页面重新换发（自愈路径）。
    :param reuse_tab: 优先复用已打开的微伴后台标签页（`reload` 而不是新开）——
        保活走这条，避免每次都多开一个窗口。
    :param launch_if_absent: 只在没有可用 CDP 时才拉起 Chrome（保活走这条）。

    ⚠️ 保活与手动引导**共用这一份实现**：这里原本有两份"差不多"的抓取逻辑，
    于是「取最新的 open session」只修好了一处，另一处照旧复用旧值（实测被咬）。
    """
    from playwright.sync_api import sync_playwright

    captured = {}
    if not (launch_if_absent and cdp_alive()):
        launch_chrome(target_url)

    def on_request(req):
        if "createOpenChatSession" in req.url and "body" not in captured:
            try:
                captured["body"] = json.loads(req.post_data or "{}")
                captured["url"] = req.url
            except Exception:
                pass

    with sync_playwright() as p:
        browser = None
        for _ in range(40):
            try:
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
                break
            except Exception:
                time.sleep(2)
        if browser is None:
            raise RuntimeError("无法连接 Chrome（CDP 连接失败）")

        ctx = browser.contexts[0]
        page = None
        if reuse_tab:
            for pg in ctx.pages:
                if "weibanzhushou.com/dashboard" in (pg.url or ""):
                    page = pg
                    break
        if page is None:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("request", on_request)

        reset_info = None
        if reset_session or reuse_tab:
            if reset_session:
                reset_info = reset_open_session(page, ctx)
                print(f"已清掉缓存的旧企微会话：{reset_info}")
            # 复用已打开的标签页时也要"过一遍"：清缓存后必须重新加载才会换发新会话
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:              # noqa: BLE001
                print(f"导航告警（继续）：{str(e)[:100]}")

        print("等待页面加载并触发企微会话组件…（如需登录请在弹出的窗口里完成）")
        deadline = time.time() + wait_sec
        while time.time() < deadline and "body" not in captured:
            page.wait_for_timeout(1500)

        if "body" not in captured:
            page.screenshot(path=str(CACHE_DIR / "bootstrap_fail.png"))
            raise RuntimeError(
                "未捕获到 createOpenChatSession 请求。\n"
                "常见原因：未登录 / 该会话无存档消息 / 页面没打开到会话详情。\n"
                f"已截图：{CACHE_DIR / 'bootstrap_fail.png'}")

        # ⚠️ 关键一步：请求体里的 open session 可能是**旧的**（页面随后会自己换发）。
        #    再给页面一点时间，然后比对"请求体"与"iframe localStorage"，**取更新的那个**
        #    （两份 `open_sid_meta.ts` 是同一种时间戳，可直接比大小）。
        page.wait_for_timeout(3000)
        live = read_open_session_from_page(page)
        cookies = ctx.cookies()

    body = captured["body"]
    acp = body.get("agent_config_params") or {}
    sid, meta, source = body.get("open_sid"), body.get("open_sid_meta"), "request"
    if live.get("open_sid") and _meta_ts(live.get("open_sid_meta")) >= _meta_ts(meta):
        sid, meta, source = live["open_sid"], live.get("open_sid_meta"), "localStorage"
    return {
        "open_sid": sid,
        "open_sid_meta": meta,
        "open_sid_source": source,           # 取自哪里（排障用：请求体 / localStorage）
        "open_sid_reset": bool(reset_session),
        "reset_info": reset_info,
        "url": body.get("url") or "https://weibanzhushou.com/dashboard/messages",
        "agent_config_params": acp,          # 兜底用（正常走实时签名）
        "wb_cookie": [c for c in cookies if "weibanzhushou.com" in c.get("domain", "")],
        "ww_cookie": [c for c in cookies if "work.weixin.qq.com" in c.get("domain", "")],
        "captured_at": int(time.time()),
    }


def main():
    ap = argparse.ArgumentParser(description="抓取企微正文接口凭据")
    ap.add_argument("--staff-id", help="指定员工 staff_ext_id")
    ap.add_argument("--session-id", help="指定会话 session_ext_id")
    ap.add_argument("--wait", type=int, default=90, help="等待页面触发组件的秒数")
    ap.add_argument("--reset-session", action="store_true",
                    help="先清掉缓存的旧企微会话再重新换发（页面一直复用旧值时用）")
    args = ap.parse_args()

    CACHE_DIR.mkdir(exist_ok=True)

    # 无浏览器时直接给操作指引（而不是先杀进程、再挑会话、最后抛个裸 Errno）
    env = browser_env()
    if not env["available"]:
        print(f"❌ 无法弹浏览器：{env['reason']}")
        print(f"   {env['hint']}")
        return 2

    # 关掉本项目**自己**可能残留的 Chrome，避免 profile 被占用（不影响用户其他窗口）
    kill_own_chrome()

    staff_id, session_id = pick_staff_session(args.staff_id, args.session_id)
    url = MSG_URL.format(s=staff_id, c=session_id)

    try:
        data = harvest(url, args.wait, reset_session=args.reset_session)
    except RuntimeError as e:
        print(f"\n❌ 引导失败：{e}")
        return 1

    if not data.get("open_sid"):
        print("❌ 未取到 open_sid")
        return 1

    BOOTSTRAP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"\n✅ 凭据已保存：{BOOTSTRAP_FILE}")
    print(f"   open_sid      : …{data['open_sid'][-12:]}")
    print(f"   微伴 cookie   : {len(data['wb_cookie'])} 个")
    print(f"   企微 cookie   : {len(data['ww_cookie'])} 个")
    print(f"   url           : {data['url']}")
    print("\n接下来可以纯 HTTP 抓取了，例如：")
    print("   python wb_staff_export.py --staff \"智泊AI大模型-lucky老师\" --sessions 5")
    return 0


if __name__ == "__main__":
    sys.exit(main())
