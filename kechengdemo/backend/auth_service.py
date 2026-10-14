# -*- coding: utf-8 -*-
"""扫码登录（凭据引导）服务。

为什么需要这一步
    抓聊天正文需要两把「凭据」：
      ① 微伴登录态 cookie  —— 用来实时生成 jsapi_sign 签名
      ② 企微 wwopen.open.sid cookie —— 即组件的 open_sid
    这两样只能从浏览器里拿，所以提供「弹浏览器 → 扫码登录 → 自动抓取」的入口。
    抓到的凭据会写进 ``.cache/ww_bootstrap.json``，之后所有同步都是纯 HTTP。

设计
    浏览器抓取是阻塞操作，放在独立线程里跑；主进程只维护一个状态机供前端轮询。
"""
import json
import subprocess
import threading
import time
from pathlib import Path

import db
from db import ROOT

import wb_bootstrap            # 复用已验证的抓取逻辑  # noqa: E402

CACHE_DIR = ROOT / ".cache"
BOOTSTRAP_FILE = CACHE_DIR / "ww_bootstrap.json"
FAIL_SHOT = CACHE_DIR / "bootstrap_fail.png"

_state = {
    "running": False,
    "stage": "idle",        # idle/launching/waiting_login/capturing/done/failed
    "message": "尚未登录",
    "started_at": None,
    "finished_at": None,
    "error": None,
}
_lock = threading.Lock()


# ------------------------------------------------------------ 状态

def _set(stage: str, message: str, error: str = None):
    with _lock:
        _state.update(stage=stage, message=message, error=error)


def get_state() -> dict:
    with _lock:
        return dict(_state)


def credential_status() -> dict:
    """凭据可用性：文件 + 数据库 + 进程内状态。"""
    st = get_state()
    info = {
        "has_file": BOOTSTRAP_FILE.exists(),
        "captured_at": None,
        "open_sid_tail": None,
        "age_hours": None,
        "expired": True,
        "login": st,
    }
    if BOOTSTRAP_FILE.exists():
        try:
            data = json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8"))
            captured = data.get("captured_at")
            info["captured_at"] = captured
            info["open_sid_tail"] = (data.get("open_sid") or "")[-8:]
            if captured:
                age = (time.time() - captured) / 3600
                info["age_hours"] = round(age, 2)
                # 微伴登录态约 7 天，超过 6 天提示重新登录
                info["expired"] = age > 6 * 24
        except Exception as e:
            info["error"] = f"凭据文件解析失败：{e}"
    row = db.query_one("SELECT status, captured_at, updated_at FROM credential WHERE id = 1")
    info["db_status"] = (row or {}).get("status", "missing")
    return info


def _save_credential(data: dict):
    BOOTSTRAP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    db.execute(
        """INSERT INTO credential (id, open_sid, payload, captured_at, status, message, updated_at)
           VALUES (1, %s, %s, %s, 'ok', %s, now())
           ON CONFLICT (id) DO UPDATE
             SET open_sid = EXCLUDED.open_sid, payload = EXCLUDED.payload,
                 captured_at = EXCLUDED.captured_at, status = 'ok',
                 message = EXCLUDED.message, updated_at = now()""",
        (data.get("open_sid"), json.dumps(data, ensure_ascii=False),
         data.get("captured_at"), "登录成功"),
    )


# ------------------------------------------------------------ 抓取流程

def _worker(staff_id: str = None, session_id: str = None, wait_sec: int = 300):
    try:
        _set("launching", "正在启动 Chrome…")
        CACHE_DIR.mkdir(exist_ok=True)

        # 清掉残留 Chrome，避免 profile 被占用
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

        sid_s, sess_s = wb_bootstrap.pick_staff_session(staff_id, session_id)
        url = wb_bootstrap.MSG_URL.format(s=sid_s, c=sess_s)
        _set("waiting_login",
             "已弹出浏览器。若未登录请在窗口中扫码/账号登录；已登录会自动继续。")

        data = wb_bootstrap.harvest(url, wait_sec)

        _set("capturing", "已捕获凭据，正在保存…")
        if not data.get("open_sid"):
            raise RuntimeError("未取到 open_sid，请确认页面已加载出会话内容")
        _save_credential(data)
        _set("done", "登录成功，凭据已保存（后续同步无需浏览器）")
    except Exception as e:
        _set("failed", f"抓取失败：{e}", error=str(e))
    finally:
        with _lock:
            _state["running"] = False
            _state["finished_at"] = time.time()


def start_login(staff_id: str = None, session_id: str = None,
                wait_sec: int = 300) -> dict:
    """触发扫码登录（幂等：已在跑则直接返回当前状态）。"""
    with _lock:
        if _state["running"]:
            return {"ok": False, "reason": "登录流程已在运行中", "state": dict(_state)}
        _state.update(running=True, stage="launching", message="正在启动 Chrome…",
                      started_at=time.time(), finished_at=None, error=None)

    threading.Thread(target=_worker, args=(staff_id, session_id, wait_sec),
                     daemon=True, name="wb-login").start()
    return {"ok": True, "state": get_state()}


# ------------------------------------------------------------ 凭据校验

def verify() -> dict:
    """真调一次企微接口，确认凭据 + 登录态是否仍然有效。"""
    try:
        import ww_chatdata
        import weiban
    except Exception as e:
        return {"ok": False, "error": f"模块导入失败：{e}"}

    try:
        ww = ww_chatdata.WecomChatData(verbose=False)
        sid = ww.new_session()                    # ① 微伴签名 + ② 企微开会话
        staff = next(iter(weiban.iter_all_staff()), None)
        if not staff:
            return {"ok": True, "message": "凭据有效（未取到员工）", "sid_tail": sid[-8:]}
        res = weiban.list_sessions(staff["id"], "ext_user", limit=1, offset=0,
                                   order_by="last_updated_desc")
        sess = (res.get("session_list") or [None])[0]
        if not sess:
            return {"ok": True, "message": "凭据有效（未取到会话）", "sid_tail": sid[-8:]}
        msgs = weiban.list_messages(staff["id"], sess["id"], limit=3).get("message_list") or []
        hit = 0
        if msgs:
            plain = ww.decrypt(msgs)              # ③ 解密正文
            hit = len(plain)
        db.execute(
            "UPDATE credential SET status='ok', message='校验通过', updated_at=now() WHERE id=1")
        return {"ok": True, "sid_tail": sid[-8:], "sample_msgs": len(msgs),
                "decrypted": hit, "message": "凭据有效，正文通道正常"}
    except Exception as e:
        db.execute(
            "UPDATE credential SET status='expired', message=%s, updated_at=now() WHERE id=1",
            (str(e)[:500],))
        return {"ok": False, "error": str(e)}
