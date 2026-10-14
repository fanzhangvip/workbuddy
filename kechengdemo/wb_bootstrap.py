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

前提：``C:\\Users\\EDY\\.workbuddy\\browser-profiles\\wb-session`` 这个 Chrome
配置里已登录过微伴（首次运行脚本会停在该窗口等你登录）。
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

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE = r"C:\Users\EDY\.workbuddy\browser-profiles\wb-session"
CDP_PORT = 9222

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".cache"
BOOTSTRAP_FILE = CACHE_DIR / "ww_bootstrap.json"

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


def harvest(target_url: str, wait_sec: int = 90) -> dict:
    from playwright.sync_api import sync_playwright

    captured = {}
    subprocess.Popen([
        CHROME, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={PROFILE}",
        "--no-first-run", "--no-default-browser-check", "--no-sandbox",
        "--disable-session-crashed-bubble", "--window-size=1400,950", target_url,
    ])

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

    # 关掉可能残留的 Chrome，避免 profile 被占用
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
