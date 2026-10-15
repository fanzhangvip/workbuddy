# -*- coding: utf-8 -*-
"""抓取企微会话存档正文接口所需的凭据（浏览器引导，只需偶尔跑一次）。

为什么还需要浏览器？
    ① 微伴登录态（cookie）            —— 用于实时生成 jsapi_sign 签名
    ② 企微 ``wwopen.open.sid`` cookie —— 即组件的 ``open_sid``，只能由企微下发
    这两样拿到后会缓存到 ``.cache/ww_bootstrap.json``，之后所有抓取都是纯 HTTP。

用法::

    python wb_bootstrap.py                # 自动挑一个会话页打开并抓取
    python wb_bootstrap.py --staff-id XXX --session-id YYY
    python wb_bootstrap.py --verbose      # 保留浏览器窗口便于人工登录

前提：``BROWSER_PROFILE_DIR``（默认 ``~/.workbuddy/browser-profiles/wb-session``）这个
Chrome 配置里已登录过微伴（首次运行脚本会停在该窗口等你登录）。
浏览器路径可用环境变量 ``CHROME_PATH`` / ``BROWSER_PROFILE_DIR`` / ``CDP_PORT`` 覆盖。
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

# 浏览器路径改为可配置（换机器/换用户名不再需要改代码）
CHROME = (os.environ.get("CHROME_PATH")
          or r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PROFILE = (os.environ.get("BROWSER_PROFILE_DIR")
           or str(Path.home() / ".workbuddy" / "browser-profiles" / "wb-session"))
CDP_PORT = int(os.environ.get("CDP_PORT") or 9222)

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
    """拉起本项目专用 Chrome（独立 profile + CDP），并记录主进程 PID。"""
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


def harvest(target_url: str, wait_sec: int = 90) -> dict:
    from playwright.sync_api import sync_playwright

    captured = {}
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
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("request", on_request)

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

        cookies = ctx.cookies()

    body = captured["body"]
    acp = body.get("agent_config_params") or {}
    return {
        "open_sid": body.get("open_sid"),
        "open_sid_meta": body.get("open_sid_meta"),
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
    args = ap.parse_args()

    CACHE_DIR.mkdir(exist_ok=True)

    # 关掉本项目**自己**可能残留的 Chrome，避免 profile 被占用（不影响用户其他窗口）
    kill_own_chrome()

    staff_id, session_id = pick_staff_session(args.staff_id, args.session_id)
    url = MSG_URL.format(s=staff_id, c=session_id)

    try:
        data = harvest(url, args.wait)
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
