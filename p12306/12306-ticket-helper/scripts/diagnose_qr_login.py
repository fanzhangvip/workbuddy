"""扫码登录真机诊断：把每一步的原始报文都抓下来。

用途：真机扫码失败时，一次扫码就能拿到全部证据（不是猜）。
它不走业务封装，而是直接打接口并**原样打印**服务端返回，
包括请求体、Cookie 变化、以及各种可能的调用组合。

    python scripts/diagnose_qr_login.py --timeout 300

输出同时写到 <用户目录>/qr-diagnose.txt，便于回传。
"""

from __future__ import annotations

import argparse
import base64
import json
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

REPORT: list[str] = []


def say(line: str = "") -> None:
    print(line)
    REPORT.append(line)


def dump(label: str, response, *, extra: dict | None = None) -> dict:
    """原样打印一次请求的结果。"""
    payload = response.json if getattr(response, "json", None) is not None else None
    say(f"--- {label} ---")
    say(f"    HTTP {response.status_code}  ({response.latency_ms}ms)")
    if extra:
        say(f"    请求: {extra}")
    body = (response.text or "").strip()
    say(f"    响应: {body[:900]}")
    return payload or {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫码登录真机诊断")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)

    import logging

    logging.disable(logging.INFO)  # 只留本脚本的输出，避免刷屏

    from ticket_helper.inter import urls
    from ticket_helper.inter.station import StationMap
    from ticket_helper.runtime.context import AppContext

    ctx = AppContext()
    report_path = ctx.paths.home / "qr-diagnose.txt"
    try:
        ctx.station_map = StationMap(ctx.paths).load(allow_remote=False)
        ctx.accounts.station_map = ctx.station_map

        user = ctx.accounts.build_user(
            __import__("ticket_helper.config.settings", fromlist=["UserConfig"]).UserConfig(username="qr_diag")
        )
        client = user.api.client
        referer = client.url("/otn/resources/login.html")

        say(f"服务端: {ctx.config.api_base}")
        say(f"User-Agent: {client.session.headers.get('User-Agent', '')[:80]}")
        say("")

        # ---------- 1. 申请二维码 ----------
        created = client.post(urls.CREATE_QR, data={"appid": "otn"}, allow_retry=False, referer=referer)
        create_payload = dump("1) POST /passport/web/create-qr64", created, extra={"appid": "otn"})
        qr_uuid = str(create_payload.get("uuid") or "")
        if not qr_uuid:
            say("× 没拿到 uuid，无法继续")
            return 1

        target = ctx.paths.home / "qrcode-login.png"
        target.write_bytes(base64.b64decode(str(create_payload.get("image") or "")))
        say(f"    二维码已写入: {target}")
        if not args.no_open:
            try:
                import os

                os.startfile(str(target))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                say(f"    （未能自动打开图片: {exc}）")

        say("")
        say(f"请用手机 12306 App 扫描上面的二维码，等待 {int(args.timeout)} 秒…")
        say("")

        # ---------- 2. 轮询，直到确认 ----------
        deadline = time.time() + args.timeout
        confirmed_payload: dict = {}
        shown = ""
        last_code = ""
        while time.time() < deadline:
            resp = client.post(
                urls.CHECK_QR,
                data={"uuid": qr_uuid, "appid": "otn", "_json_att": ""},
                allow_retry=False,
                referer=referer,
            )
            payload = resp.json or {}
            code = str(payload.get("result_code", ""))
            if code != last_code:
                say(f"  [轮询] result_code={code!r} body={json.dumps(payload, ensure_ascii=False)[:200]}")
                last_code = code
            if code == "3":
                # 过期就换一张，诊断脚本也保持一直有一张可扫的码
                created = client.post(
                    urls.CREATE_QR, data={"appid": "otn"}, allow_retry=False, referer=referer
                )
                create_payload = created.json or {}
                qr_uuid = str(create_payload.get("uuid") or qr_uuid)
                target.write_bytes(base64.b64decode(str(create_payload.get("image") or "")))
                shown = qr_uuid
                say("  [轮询] 二维码已过期，已换新码（请重新打开图片）")
                last_code = ""
            if code == "2":
                confirmed_payload = payload
                break
            time.sleep(1.5)
        else:
            say("× 等待超时，未完成扫码确认")
            return 1

        say("")
        say("=" * 68)
        say("扫码已确认，下面是**关键证据**")
        say("=" * 68)
        say("")
        dump("2) checkqr 确认时的原始响应（重点看有没有 uamtk/apptk）", type("R", (), {
            "json": confirmed_payload, "status_code": 200, "latency_ms": 0,
            "text": json.dumps(confirmed_payload, ensure_ascii=False),
        })())
        checkqr_ticket = str(
            confirmed_payload.get("uamtk") or confirmed_payload.get("apptk") or confirmed_payload.get("newapptk") or ""
        )
        say(f"    → checkqr 里带的票据: {checkqr_ticket[:24] + '…' if checkqr_ticket else '（无）'}")
        say("")

        def cookies() -> str:
            names = sorted(client.session.cookies.keys())
            return ",".join(names) if names else "（空）"

        say(f"    当前 Cookie: {cookies()}")
        say("")

        # ---------- 3. 换票（规范路径：不碰 userLogin） ----------
        # 真机实测：扫码流程里**不能**调用 /otn/login/userLogin —— 它会新建
        # JSESSIONID 把扫码上下文丢掉，随后换票接口返回错误页、换身份报「票据内容为空」。
        say("（注意：扫码流程不调用 userLogin，它新建会话会打断扫码上下文）")
        say("")
        resp = client.post(urls.UAMTk, data={"appid": "otn"}, allow_retry=False, referer=referer)
        uamtk_payload = dump("3) POST /passport/web/auth/uamtk", resp, extra={"appid": "otn"})
        say(f"    Cookie 变化: {cookies()}")
        fetched_ticket = str(
            uamtk_payload.get("newapptk") or uamtk_payload.get("apptk") or uamtk_payload.get("uamtk") or ""
        )
        say(f"    → 换到的 newapptk: {fetched_ticket[:24] + '…' if fetched_ticket else '（空）'}")
        say("")

        # ---------- 4. 换身份：按优先级依次尝试 ----------
        attempts: list[tuple[str, dict]] = []
        if fetched_ticket:
            attempts.append(("规范路径：uamtk 换到的 newapptk", {"tk": fetched_ticket}))
        if checkqr_ticket and checkqr_ticket != fetched_ticket:
            attempts.append(("兜底：checkqr 响应里的 uamtk", {"tk": checkqr_ticket}))
        attempts.append(("对照组：空 tk", {"tk": ""}))

        success = False
        for label, form in attempts:
            resp = client.post(urls.UAMAUTH_CLIENT, data=form, allow_retry=False, referer=referer)
            payload = dump(f"4) POST /otn/uamauthclient  [{label}]", resp, extra=form)
            if str(payload.get("result_code", "")) == "0":
                say(f"    √ 成功！username={payload.get('username')}")
                success = True
                break
            say(f"    × 未通过: {payload.get('result_message')}")
            say("")

        say("")
        say("=" * 68)
        say("结论")
        say("=" * 68)
        if success:
            say("√ 已找到可用组合，请据此修正实现。")
        else:
            say("× 全部组合均未通过。重点看第 2 步：checkqr 确认时到底给了什么字段。")
            say(f"  checkqr 完整响应: {json.dumps(confirmed_payload, ensure_ascii=False)}")
            say(f"  uamtk 完整响应:   {json.dumps(uamtk_payload, ensure_ascii=False)}")
        return 0 if success else 1
    finally:
        try:
            report_path.write_text("\n".join(REPORT), encoding="utf-8")
            print(f"\n完整报告已写入: {report_path}")
        except Exception:  # noqa: BLE001
            pass
        try:
            ctx.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
