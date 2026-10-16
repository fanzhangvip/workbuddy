# -*- coding: utf-8 -*-
"""正文通道端到端自检 —— 修「保活一路报 ✅，通道其实断了」。

    .venv\\Scripts\\python.exe tests\\test_channel_check.py

## 这个文件在守什么

正文链路有**两把独立凭据**：微伴登录态 cookie（滑动过期，刷一次续 7 天）与企微
``open_sid``（页面写进 localStorage 的会话，**会先死**）。改造前保活只校验微伴
``jsapi_sign``，于是 2026-10-13 起正文通道实际断了 3 天，日志却一路「✅ 凭据已刷新」
（`content_state='ok'` 的 `fetched_at` 停在 10-13 13:26）。

本文件锁住两件事：

  1. `ww_chatdata.check_channel()` 的判定口径（签名 → 开会话 → 取样解密）；
  2. `wb_keepalive.refresh()` **写盘成功但通道不通时不得报成功**，要落
     `CODE_CHANNEL_DOWN` 并给出宿主机指引。

全程打桩，**不碰真实凭据文件**（`BOOTSTRAP_FILE` 等被指到临时目录）。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from _harness import ROOT, check, section, summary          # noqa: E402

sys.path.insert(0, str(ROOT))

import wb_keepalive                                          # noqa: E402
import ww_chatdata                                           # noqa: E402


@contextmanager
def env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeWW:
    """替身：`new_session` 成功，`decrypt` 返回固定条数（或按需抛错）。"""

    def __init__(self, *a, **k):
        self.raise_on_decrypt = k.pop("raise_on_decrypt", False)

    def new_session(self):
        if getattr(self, "raise_on_session", False):
            raise ww_chatdata.WecomChatDataError("createOpenChatSession 未返回 sid："
                                                "{'errCode': 42006, 'humanMessage': 'missing session'}")
        return "sid_open_session_ABCDEFGH"

    def decrypt(self, msgs):
        if getattr(self, "raise_on_decrypt", False):
            raise ww_chatdata.WecomChatDataError("getchatdata 失败：40058 reqid missing")
        return {m["msgid"]: {"msgtype": 1, "text": {"content": "x"}} for m in msgs}


@contextmanager
def stub_ww(session_raises=False, decrypt_raises=False, staff=None,
            sessions=None, messages=None):
    """把 `WecomChatData` 与 `weiban` 三个取数函数一起打桩，退出时还原。"""
    import weiban

    class C(FakeWW):
        def new_session(self):
            if session_raises:
                raise ww_chatdata.WecomChatDataError(
                    "createOpenChatSession 未返回 sid：42006 missing session")
            return "sid_open_session_ABCDEFGH"

        def decrypt(self, msgs):
            if decrypt_raises:
                raise ww_chatdata.WecomChatDataError("getchatdata 失败：40058 reqid missing")
            return {m["msgid"]: {"msgtype": 1} for m in msgs}

    saved = (ww_chatdata.WecomChatData, weiban.iter_all_staff,
             weiban.list_sessions, weiban.list_messages)
    ww_chatdata.WecomChatData = C
    weiban.iter_all_staff = lambda: iter(staff or [])
    weiban.list_sessions = lambda *a, **k: {"session_list": sessions or []}
    weiban.list_messages = lambda *a, **k: {"message_list": messages or []}
    try:
        yield
    finally:
        (ww_chatdata.WecomChatData, weiban.iter_all_staff,
         weiban.list_sessions, weiban.list_messages) = saved


def test_check_channel():
    section("check_channel：开会话失败 = 通道不可用")
    with stub_ww(session_raises=True):
        res = ww_chatdata.check_channel()
        check("开会话失败 → ok=False", res["ok"] is False, res)
        check("错误里保留原始 errCode（可归因）", "42006" in res["error"], res["error"])
        check("没有 sid_tail", res["sid_tail"] is None, res)

    section("check_channel：取不到样本不算失败")
    with stub_ww(staff=[]):
        res = ww_chatdata.check_channel()
        check("没有员工 → ok=True（会话已建立）", res["ok"] is True, res)
        check("消息里说明跳过了冒烟", "跳过" in res["message"], res["message"])

    section("check_channel：正常路径解出样本")
    with stub_ww(staff=[{"id": "S1", "name": "甲"}],
                 sessions=[{"id": "C1"}],
                 messages=[{"msgid": f"m{i}"} for i in range(3)]):
        res = ww_chatdata.check_channel()
        check("ok=True", res["ok"] is True, res)
        check("sample_msgs=3", res["sample_msgs"] == 3, res)
        check("decrypted=3（真的过了解密）", res["decrypted"] == 3, res)
        check("sid_tail 有值", bool(res["sid_tail"]), res)

    section("check_channel：解密阶段报错也不吞")
    with stub_ww(staff=[{"id": "S1"}], sessions=[{"id": "C1"}],
                 messages=[{"msgid": "m1"}], decrypt_raises=True):
        res = ww_chatdata.check_channel()
        check("解密失败 → ok=False", res["ok"] is False, res)
        check("错误信息保留", "40058" in res["error"], res["error"])


def test_refresh_must_not_claim_success():
    section("refresh：写盘成功但通道不通 → 不得报成功 + 清缓存自愈")
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        saved_paths = (wb_keepalive.BOOTSTRAP_FILE, wb_keepalive.BACKUP_FILE,
                       wb_keepalive.LOG_FILE)
        saved_fn = (wb_keepalive.verify, wb_keepalive.harvest)
        # 指向临时目录：本测试**绝不能碰到真实的 .cache/ww_bootstrap.json**
        wb_keepalive.BOOTSTRAP_FILE = tmpd / "ww_bootstrap.json"
        wb_keepalive.BACKUP_FILE = tmpd / "bak.json"
        wb_keepalive.LOG_FILE = tmpd / "keepalive.log"
        wb_keepalive.verify = lambda cookies: (True, "ok (corpid=X, agentid=1)")
        calls = []                       # 记录每次抓取是否要求"清缓存重抓"

        def fake_harvest(url, wait_sec, reset_session=False):
            calls.append(reset_session)
            return {
                "open_sid": ("wj_reset_" if reset_session else "wj_first_") + "A" * 20,
                "open_sid_meta": {"ts": 1},
                "open_sid_source": "localStorage" if reset_session else "request",
                "url": "https://weibanzhushou.com/dashboard/messages",
                "agent_config_params": {},
                "wb_cookie": [{"name": "session_id", "value": "v",
                               "domain": ".weibanzhushou.com"}],
                "ww_cookie": [{"name": "wwopen.open.sid", "value": "s",
                               "domain": ".work.weixin.qq.com"}],
                "captured_at": int(time.time()),
            }

        wb_keepalive.harvest = fake_harvest
        saved_check = ww_chatdata.check_channel
        # ⚠️ 必须替身**落库**那一步：这个用例会真调 `refresh()` 三次，而 `_record()`
        #    现在会把结果写进 `app_setting.keepalive_last` —— 不挡掉的话，每跑一次回归
        #    就伪造一条"最近一次保活"（同步页上直接可见，且 source=test 会被当成真事）。
        #    `_last_run` 仍照常更新，用例对 status() 的断言不受影响。
        saved_persist = wb_keepalive._persist_state
        wb_keepalive._persist_state = lambda rec: None
        try:
            # ① 通道一直不通：必须落 CODE_CHANNEL_DOWN，且**已经试过**清缓存重抓
            ww_chatdata.check_channel = lambda *a, **k: {
                "ok": False, "error": "WecomChatDataError: 42006 missing session"}
            with env(BROWSER_ENABLED="1", BROWSER_IN_CONTAINER="0"):
                res = wb_keepalive.refresh(wait_sec=1, source="test")
            check("通道不通 → ok=False", res.get("ok") is False, res)
            check("失败码是 CODE_CHANNEL_DOWN",
                  res.get("code") == wb_keepalive.CODE_CHANNEL_DOWN, res.get("code"))
            check("消息里点明「通道」而不是「保活失败」",
                  "通道" in (res.get("message") or ""), res.get("message"))
            check("给出宿主机操作指引",
                  "host_bootstrap_login.bat" in (res.get("hint") or ""), res.get("hint"))
            check("先照常抓一次、再清缓存重抓一次（两段式）",
                  calls == [False, True], calls)
            check("last_run 记到了这次通道故障",
                  wb_keepalive.status()["last_run"]["code"]
                  == wb_keepalive.CODE_CHANNEL_DOWN)

            # ② 第一次不通、清缓存后通了 → 必须报成功，且落的是**第二段**抓到的凭据
            calls.clear()
            seq = {"n": 0}

            def flaky(*a, **k):
                seq["n"] += 1
                return ({"ok": False, "error": "42006"} if seq["n"] == 1
                        else {"ok": True, "message": "正文通道正常"})

            ww_chatdata.check_channel = flaky
            with env(BROWSER_ENABLED="1", BROWSER_IN_CONTAINER="0"):
                res2 = wb_keepalive.refresh(wait_sec=1, source="test")
            check("清缓存后通道通了 → ok=True", res2.get("ok") is True, res2)
            check("自愈路径确实走了「清缓存重抓」", calls == [False, True], calls)
            saved = json.loads(wb_keepalive.BOOTSTRAP_FILE.read_text(encoding="utf-8"))
            check("落盘的是重抓那次的凭据（不是第一段的旧值）",
                  (saved.get("open_sid") or "").startswith("wj_reset_"),
                  (saved.get("open_sid") or "")[:16])

            # ③ 通道本来就正常：不该多做一次重抓（省一轮浏览器往返）
            calls.clear()
            ww_chatdata.check_channel = lambda *a, **k: {
                "ok": True, "message": "凭据有效，正文通道正常（解密 3/3 条样例消息）"}
            with env(BROWSER_ENABLED="1", BROWSER_IN_CONTAINER="0"):
                res3 = wb_keepalive.refresh(wait_sec=1, source="test")
            check("通道正常 → ok=True", res3.get("ok") is True, res3)
            check("成功消息里带上通道结论",
                  "正文通道正常" in (res3.get("message") or ""), res3.get("message"))
            check("通道正常时只抓一次（不做无谓重抓）", calls == [False], calls)
        finally:
            wb_keepalive._persist_state = saved_persist
            ww_chatdata.check_channel = saved_check
            (wb_keepalive.BOOTSTRAP_FILE, wb_keepalive.BACKUP_FILE,
             wb_keepalive.LOG_FILE) = saved_paths
            (wb_keepalive.verify, wb_keepalive.harvest) = saved_fn


def test_single_source_of_truth():
    section("单一来源：保活不再自带一份抓取实现")
    import wb_bootstrap

    calls = {}

    def fake(url, wait_sec, reset_session=False, reuse_tab=False, launch_if_absent=False):
        calls.update(url=url, wait_sec=wait_sec, reset_session=reset_session,
                     reuse_tab=reuse_tab, launch_if_absent=launch_if_absent)
        return {"open_sid": "x" * 10}

    saved_harvest = wb_bootstrap.harvest
    saved_alive = wb_keepalive.cdp_alive
    wb_bootstrap.harvest = fake
    wb_keepalive.cdp_alive = lambda *a, **k: True     # 声称 CDP 在线 → 不该再拉浏览器
    try:
        out = wb_keepalive.harvest("https://example/x", 5, reset_session=True)
        check("保活 harvest 走的是 wb_bootstrap.harvest", out.get("open_sid") == "x" * 10, out)
        check("透传 reset_session", calls.get("reset_session") is True, calls)
        check("保留「复用已打开标签页」的行为", calls.get("reuse_tab") is True, calls)
        check("保留「没有 CDP 才拉起」的行为", calls.get("launch_if_absent") is True, calls)
    finally:
        wb_bootstrap.harvest = saved_harvest
        wb_keepalive.cdp_alive = saved_alive


def main():
    test_check_channel()
    test_refresh_must_not_claim_success()
    test_single_source_of_truth()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
