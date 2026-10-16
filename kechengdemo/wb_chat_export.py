# -*- coding: utf-8 -*-
"""微伴网页后台聊天记录导出工具（复用版）

用法:
    python wb_chat_export.py <staffExtId> <sessionExtId> [输出.md]

原理（2026-10-12 实测验证）:
  1. 微伴 OpenAPI 的正文恒为 null（服务端不下发），但网页后台能看明文 ——
     因为后台内嵌了企业微信官方「会话存档展示组件」(ww-open-data-frame iframe)，
     正文由企微 open.work.weixin.qq.com/chatdata/getchatdata 直接下发明文 JSON。
  2. 本脚本打开指定会话页，同时监听两路数据：
     - 微伴 /api/chat/staff_session/msg   → 元数据（方向/时间/ext_id）
     - 企微   /chatdata/getchatdata        → 明文正文（msgid 与 ext_id 对应）
     按 ext_id == msgid 合并生成可读 Markdown。
  3. 页面内「导出Excel」按钮（企微组件自带）在自动化环境下不产出文件，
     故不依赖它；直接抓 getchatdata 即可。

前提: 浏览器 profile 目录（`BROWSER_PROFILE_DIR`）里已登录过微伴（首次需人工扫码一次）。
⚠️ 只能在宿主机跑 —— 容器里没有 Chrome（见 `wb_bootstrap.browser_env`）。
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

from playwright.sync_api import sync_playwright

from ww_chatdata import TYPE_NAME      # 与权威映射统一（原脚本私有映射 4/5/7/8 错位，已弃用）

ROOT = Path(__file__).resolve().parent
# 浏览器路径 / profile / 启动方式**统一由 wb_bootstrap 管理**（单一来源）：
# 这里原本又抄了一份 `CHROME = ... or r"C:\Program Files\..."` + 一份 `Popen`，
# 于是"Chrome 装在哪 / 容器里能不能弹"要在两处各修一遍（AGENTS.md 的复用硬规则）。
from wb_bootstrap import CDP_PORT, BrowserUnavailable, browser_env, launch_chrome  # noqa: E402

TMP = os.environ.get("WB_TMP_DIR") or str(ROOT / ".workbuddy" / "tmp")


def extract(meta_resp, chat_resp):
    """合并元数据与正文 -> [(时间, 方向, 类型, 文本)]"""
    meta = json.loads(meta_resp["body"])["msg_list"]
    items = json.loads(chat_resp["body"]).get("items") or []
    by_id = {it["msgid"]: it for it in items}
    rows, hit = [], 0
    for m in meta:
        ts = m.get("msgtime") or m.get("created_at") or 0
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"
        who = "员工" if m.get("direction") == "send" else "客户"
        eid = m.get("ext_id")
        it = by_id.get(eid)
        if it:
            hit += 1
            mt = it.get("msgtype")
            tn = TYPE_NAME.get(mt, f"类型{mt}")
            if mt == 1:
                body = (it.get("text") or {}).get("content", "")
            elif mt == 6:
                url = ((it.get("voice") or {}).get("media") or {}).get("url", "")
                body = f"[语音] {url}"
            elif mt in (2, 3, 7):
                key = {2: "image", 3: "emotion", 7: "video"}.get(mt, "")
                url = (it.get(key) or {}).get("url", "") or (it.get(key) or {}).get("media", {}).get("url", "")
                body = f"[{tn}] {url}"
            else:
                body = json.dumps(it, ensure_ascii=False)
            rows.append((t, who, tn, body))
        else:
            rows.append((t, who, str(m.get("msg_type")), "〔正文未取到〕"))
    return rows, len(meta), hit


def main():
    staff_id = sys.argv[1]
    session_id = sys.argv[2]
    out_md = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        r"C:\zero\workbuddy\kechengdemo", f"聊天记录_{session_id[-8:]}.md")
    target = (f"https://weibanzhushou.com/dashboard/messages"
              f"#type=staff&sessionType=ext_user&staffExtId={staff_id}&sessionExtId={session_id}")

    captured = {"meta": None, "chat": None}

    def on_response(resp):
        url = resp.url
        try:
            if "chat/staff_session/msg" in url and captured["meta"] is None:
                captured["meta"] = {"url": url, "body": resp.text()}
            elif "getchatdata" in url and captured["chat"] is None:
                captured["chat"] = {"url": url, "body": resp.text()}
        except Exception:
            pass

    os.makedirs(TMP, exist_ok=True)
    benv = browser_env()
    if not benv["available"]:
        print(f"❌ 无法弹浏览器：{benv['reason']}")
        print(f"   {benv['hint']}")
        return 2
    try:
        # 统一入口：与扫码登录/保活用同一套 CHROME 解析、同一 CDP 端口、同一 PID 记录
        launch_chrome(target)
    except BrowserUnavailable as e:
        print(f"❌ 无法弹浏览器：{e.reason}")
        print(f"   {e.hint}")
        return 2
    with sync_playwright() as p:
        browser = None
        for _ in range(40):
            try:
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
                break
            except Exception:
                time.sleep(2)
        if browser is None:
            print("无法连接 Chrome")
            return 1
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", on_response)

        print("等待会话数据…")
        deadline = time.time() + 90
        while time.time() < deadline:
            if captured["meta"] and captured["chat"]:
                break
            page.wait_for_timeout(2000)

        if not captured["meta"]:
            print("未捕获到微伴消息元数据（可能未登录或会话不存在）")
            page.screenshot(path=os.path.join(TMP, "export_fail.png"))
            return 1

    meta, chat = captured["meta"], captured["chat"]
    json.dump([meta], open(os.path.join(TMP, "export_meta.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    if chat:
        json.dump([chat], open(os.path.join(TMP, "export_chatdata.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)

    rows, n_meta, n_hit = extract(meta, chat) if chat else ([], 0, 0)
    lines = [f"# 聊天记录 {staff_id[:16]}… 会话 {session_id}", "",
             f"> 共 {len(rows) or n_meta} 条，正文命中 {n_hit} 条（来自企微官方会话存档组件）。", ""]
    for t, who, tn, body in rows:
        lines.append(f"**{t} {who}**（{tn}）：{body}")
        lines.append("")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"✅ 已导出 {out_md}  元数据 {n_meta} 条 / 正文命中 {n_hit} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
