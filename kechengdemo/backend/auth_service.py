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
import threading
import time

import db
from db import ROOT

import wb_bootstrap            # 复用已验证的抓取逻辑  # noqa: E402

CACHE_DIR = ROOT / ".cache"
BOOTSTRAP_FILE = CACHE_DIR / "ww_bootstrap.json"

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
        # 浏览器能力（唯一判定在 wb_bootstrap.browser_env）：
        # 容器部署时这里会是 available=False —— 前端据此把「扫码登录」换成
        # 「去宿主机执行 xxx」，而不是让用户点一个必然失败的按钮。
        "browser": wb_bootstrap.browser_env(),
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

        # 二次兜底（start_login 已挡一次）：万一环境在流程中被改坏，
        # 也要落到"可执行的操作指引"上，而不是 FileNotFoundError。
        env = wb_bootstrap.browser_env()
        if not env["available"]:
            raise wb_bootstrap.BrowserUnavailable(env)

        # 清掉本项目**自己**残留的 Chrome，避免 profile 被占用（不影响用户其他 Chrome 窗口）
        wb_bootstrap.kill_own_chrome(wait_sec=1)

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
    except wb_bootstrap.BrowserUnavailable as e:
        # 环境能力问题（容器内 / 未装 Chrome）：报"该怎么办"，不报裸系统错误
        _set("unavailable", e.reason, error=e.hint)
    except Exception as e:
        _set("failed", f"抓取失败：{e}", error=str(e))
    finally:
        with _lock:
            _state["running"] = False
            _state["finished_at"] = time.time()


def start_login(staff_id: str = None, session_id: str = None,
                wait_sec: int = 300) -> dict:
    """触发扫码登录（幂等：已在跑则直接返回当前状态）。

    **前置体检**：没有浏览器（容器部署、未装 Chrome、显式关闭）时**不启动**流程 ——
    否则线程会在 1 秒内失败，把状态打成 `failed` 并在页面上留下一条
    `[Errno 2] No such file or directory: 'C:\\Program Files\\...\\chrome.exe'`。
    现在直接返回 ``ok=False`` + 原因 + **该在宿主机执行的命令**。
    """
    env = wb_bootstrap.browser_env()
    if not env["available"]:
        return {"ok": False, "reason": env["reason"], "hint": env["hint"],
                "browser": env}

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
    """真调一次企微接口，确认凭据 **+ 正文通道** 是否仍然有效。

    自检口径（签名 → 开会话 → 取样解密）**只在 `ww_chatdata.check_channel` 一处实现**：
    保活收尾用它、本端点用它，避免"一个入口说通、另一个入口报错"。
    """
    try:
        import ww_chatdata
    except Exception as e:                                # noqa: BLE001
        return {"ok": False, "error": f"模块导入失败：{e}"}

    res = ww_chatdata.check_channel()
    try:
        db.execute(
            "UPDATE credential SET status=%s, message=%s, updated_at=now() WHERE id=1",
            ("ok" if res.get("ok") else "expired",
             (res.get("message") or res.get("error") or "")[:500]))
    except Exception:                                     # noqa: BLE001
        pass                                              # 台账写失败不影响本次校验结论
    return res
