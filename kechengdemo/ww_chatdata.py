# -*- coding: utf-8 -*-
"""企业微信「会话存档展示组件」正文接口客户端（纯 HTTP，无浏览器）。

背景（2026-10-13 实测确认）
--------------------------------
微伴 OpenAPI 返回的消息正文恒为 ``null``，但每条都带：
  - ``original_id``  = 企业微信侧 ``msgid``（实测 27/27 完全一致）
  - ``secret_key``   = 该消息的加密密钥（实测与企微组件上送的完全一致）

微伴网页后台之所以能显示明文，是因为它内嵌了企业微信官方「会话存档展示组件」：
页面把 ``[{msgid, encrypt_info: {secret_key}}]`` 提交给
``open.work.weixin.qq.com/chatdata/getchatdata``，由**企微云侧完成解密**并回传明文。

换句话说：微伴给的是「锁 + 钥匙」，真正开锁的是企微。因此我们能拿到明文，
且**完全不需要企业私钥、不需要 SDK、不需要浏览器**。

三步流程
--------------------------------
① 签名   微伴 ``GET /api/staff_client/jsapi_sign?url=...``  → 拼出 agent_config_params
② 开会话 企微 ``POST /wwopen/monoApi/openData/frame/createOpenChatSession`` → sid
③ 解正文 企微 ``POST /chatdata/getchatdata?reqid=...``
         body = ``{"items": [...], "sid": sid, "reqid": reqid}``
         header ``Open-Msg-User-Sid: sid``

实测要点（踩过的坑）
--------------------------------
- ``reqid`` = ``base36(毫秒时间戳) + "." + 随机串``，纯前端随机数，**无签名**，可自行生成
- ``reqid`` 必须**同时**出现在 query 与 body 里，缺任一处报
  ``40058 reqid missing``
- body 里还必须带 ``sid``（只放 header 会报 40058）
- 一个 sid ``可复用``，且 ``可跨会话`` —— 解密任意会话都用同一个 sid
- 单次可提交上百条 items

凭据文件 ``.cache/ww_bootstrap.json``（由 ``wb_bootstrap.py`` 抓取）
"""
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent / ".cache"
BOOTSTRAP_FILE = CACHE_DIR / "ww_bootstrap.json"

WB_SIGN_URL = "https://weibanzhushou.com/api/staff_client/jsapi_sign"
WW_CREATE_SESSION = ("https://open.work.weixin.qq.com/wwopen/monoApi/openData/"
                     "frame/createOpenChatSession")
WW_GET_CHATDATA = "https://open.work.weixin.qq.com/chatdata/getchatdata"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")

# 与企微组件实际提交的 jsApiList 保持一致（顺序无关，但为稳妥原样保留）
JSAPI_LIST = [
    "uploadImage", "checkJsApi", "closeWindow", "createChatWithMsg", "geoLocation",
    "getContext", "getCurExternalChat", "getCurExternalContact", "hideMenuItems",
    "hideOptionMenu", "chooseImage", "enterpriseVerify", "getLocalImgData",
    "imagePreview", "launchMiniprogram", "openDefaultBrowser", "menu:share:appmessage",
    "menu:share:timeline", "navigateToAddCustomer", "navigateToKfChat", "onHistoryBack",
    "openAppManage", "openEnterpriseChat", "openExistedChatWithMsg", "openLocation",
    "openUserProfile", "previewFile", "scanQRCode", "sendChatMessage", "setClipboardData",
    "shareAppMessage", "shareToExternalChat", "shareToExternalContact",
    "shareToExternalMoments", "showMenuItems", "showOptionMenu", "updateEnterpriseChat",
    "wwapp.getOpenData", "wwapp.initWwOpenData", "wwapp.invokeJsApiByCallInfo",
]

B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
CHUNK = 100            # 单次 getchatdata 提交的最大条数
SIG_TTL = 300          # 签名缓存时长（秒）


class WecomChatDataError(RuntimeError):
    """正文解密链路异常（多为凭据过期：先跑 wb_keepalive.py 保活，不行再 wb_bootstrap.py 扫码）。"""


# ---------------------------------------------------------------- 工具函数

def _b36(n: int) -> str:
    s = ""
    while n:
        s = B36[n % 36] + s
        n //= 36
    return s or "0"


def new_reqid() -> str:
    """复刻企微组件的 reqid 生成逻辑：base36(now_ms).random_base36"""
    rand = "".join(random.choice(B36) for _ in range(12))
    return f"{_b36(int(time.time() * 1000))}.{rand}"


def _cookie_header(cookies: list) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def load_bootstrap() -> dict:
    if not BOOTSTRAP_FILE.exists():
        raise WecomChatDataError(
            f"缺少凭据文件 {BOOTSTRAP_FILE}，请先运行："
            f"\n    python wb_bootstrap.py")
    data = json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8"))
    if not data.get("open_sid"):
        raise WecomChatDataError("凭据文件缺少 open_sid，请重新运行 wb_bootstrap.py")
    return data


# ---------------------------------------------------------------- 客户端

class WecomChatData:
    """企微会话存档正文解密客户端（一次创建，长期复用）。

    典型用法::

        ww = WecomChatData()
        ww.new_session()                       # 可选，首次解密会自动创建
        texts = ww.decrypt_items(items)        # items: [{msgid, secret_key}, ...]
    """

    def __init__(self, bootstrap: dict = None, verbose: bool = True):
        self.boot = bootstrap or load_bootstrap()
        self.verbose = verbose
        self._sid = None
        self._sig = None
        self._sig_at = 0.0

    # ---- 日志 ----
    def _log(self, msg: str):
        if self.verbose:
            print(f"[ww] {msg}", flush=True)

    def _page_url(self) -> str:
        """给企微/微伴用的页面 URL：**必须去掉 `#片段`**。

        实证（2026-10-16）：页面自己发 `createOpenChatSession` 时 body.url 是
        `https://weibanzhushou.com/dashboard/messages`（无片段）；我们曾把带
        `#type=staff&...` 的完整地址传进去，企微回
        ``40093 不合法的jsapi_ticket参数`` —— 签名是按这个字符串算的，
        多一段片段就对不上。这里统一收敛，调用方不必记得这件事。
        """
        raw = (self.boot.get("url") or "").split("#")[0].strip()
        return raw or "https://weibanzhushou.com/dashboard/messages"

    # ---- 原始 POST ----
    def _post(self, url: str, body: dict, cookie: str,
              extra_headers: dict = None, timeout: int = 30):
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "user-agent": UA,
            "cookie": cookie,
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            raise WecomChatDataError(f"HTTP {e.code} <- {url}\n{raw[:300]}") from None

    # ---- ① 签名 ----
    def _sign_params(self, force: bool = False) -> dict:
        if not force and self._sig and time.time() - self._sig_at < SIG_TTL:
            return self._sig

        url = self._page_url()          # 去片段：见 `_page_url` 的 40093 说明
        q = urllib.parse.urlencode({"url": url})
        req = urllib.request.Request(
            f"{WB_SIGN_URL}?{q}",
            headers={"cookie": _cookie_header(self.boot["wb_cookie"]),
                     "user-agent": UA,
                     "accept": "application/json, text/plain, */*",
                     "referer": url})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                res = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise WecomChatDataError(
                f"微伴 jsapi_sign 调用失败 HTTP {e.code}，微伴登录态可能已过期，"
                f"请先运行 wb_keepalive.py 保活；若仍失败则 wb_bootstrap.py 重新扫码") from None

        if not res.get("success"):
            raise WecomChatDataError(f"微伴 jsapi_sign 返回异常：{res}")

        sign = res["agent_sign_data"]
        self._sig = {
            "corpid": res["corpid"],
            "agentid": str(res["agentid"]),
            "timestamp": str(sign["time_stamp"]),
            "nonceStr": sign["nonce_str"],
            "signature": sign["sign"],
            "jsApiList": JSAPI_LIST,
        }
        self._sig_at = time.time()
        self._log(f"已刷新签名 (ts={self._sig['timestamp']})")
        return self._sig

    # ---- ② 开会话拿 sid ----
    def new_session(self) -> str:
        body = {
            "url": self._page_url(),    # 去片段：与页面自己发的请求体逐字一致
            "open_sid": self.boot["open_sid"],
            "open_sid_meta": self.boot.get("open_sid_meta"),
            "agent_config_params": self._sign_params(),
        }
        url = f"{WW_CREATE_SESSION}?lang=zh_CN&ajax=1&f=json&random={random.randint(0, 999999):06d}"
        res = self._post(url, body, _cookie_header(self.boot["ww_cookie"]),
                         {"referer": "https://open.work.weixin.qq.com/",
                          "origin": "https://open.work.weixin.qq.com"})
        sid = (res.get("data") or {}).get("sid_open_session")
        if not sid:
            raise WecomChatDataError(
                f"createOpenChatSession 未返回 sid：{res}\n"
                f"凭据可能已过期，请先运行 wb_keepalive.py 保活；若仍失败则 wb_bootstrap.py 重新扫码")
        self._sid = sid
        self._log(f"已建立解密会话 sid=…{sid[-8:]}")
        return sid

    # ---- ③ 解密正文 ----
    def _getchatdata(self, items: list, sid: str) -> dict:
        rq = new_reqid()
        body = {"items": items, "sid": sid, "reqid": rq}
        return self._post(f"{WW_GET_CHATDATA}?reqid={rq}", body,
                          _cookie_header(self.boot["ww_cookie"]),
                          {"open-msg-user-sid": sid,
                           "referer": "https://open.work.weixin.qq.com/",
                           "origin": "https://open.work.weixin.qq.com"})

    def decrypt_items(self, items: list) -> dict:
        """把 ``[{msgid, secret_key}]`` 批量解成 ``{msgid: 明文条目}``。

        自动分片；sid 失效时自动重建后重试一次。
        """
        if not items:
            return {}
        if self._sid is None:
            self.new_session()

        out, total = {}, len(items)
        for i in range(0, total, CHUNK):
            chunk = items[i:i + CHUNK]
            payload = [{"msgid": it["msgid"],
                        "encrypt_info": {"secret_key": it["secret_key"]}}
                       for it in chunk]
            res = self._getchatdata(payload, self._sid)

            if res.get("errcode") != 0:
                self._log(f"errcode={res.get('errcode')} {res.get('errmsg')} → 重建会话重试")
                self.new_session()
                res = self._getchatdata(payload, self._sid)

            if res.get("errcode") != 0:
                raise WecomChatDataError(
                    f"getchatdata 失败：{res.get('errcode')} {res.get('errmsg')}\n"
                    f"若为凭据过期，请重新运行 wb_bootstrap.py")

            for it in res.get("items") or []:
                out[it["msgid"]] = it
        return out

    # ---- 便捷方法 ----
    def decrypt(self, messages: list) -> dict:
        """直接从微伴 OpenAPI 的消息对象解出正文。

        ``messages`` 为 ``/open-api/chat/message/list/v2`` 的 ``message_list``。
        """
        items = [{"msgid": m["original_id"], "secret_key": m["secret_key"]}
                 for m in messages
                 if m.get("original_id") and m.get("secret_key")]
        return self.decrypt_items(items)


# ---------------------------------------------------------------- 端到端自检

def _sample_session_from_db() -> dict:
    """从库里挑一个**最近有消息**的 ext_user 会话，供自检取样例消息用。

    延迟 import `db`：本模块默认是"纯 HTTP 客户端"，只有需要取样例时才连库；
    连不上就返回空 dict，让调用方退回"扫前几个员工"的兜底路径。
    """
    try:
        import sys as _sys
        _backend = str(Path(__file__).resolve().parent / "backend")
        if _backend not in _sys.path:      # 本模块在仓库根，`db` 在 backend/ 下
            _sys.path.insert(0, _backend)
        import db
        row = db.query_one(
            """SELECT staff_id, session_id FROM chat_session
                WHERE session_type = 'ext_user' AND target_id IS NOT NULL
                ORDER BY last_msg_time DESC NULLS LAST, session_id
                LIMIT 1""")
        return {"staff_id": row["staff_id"], "session_id": row["session_id"]} if row else {}
    except Exception:                                       # noqa: BLE001
        return {}


def check_channel(sample: int = 3, session_hint: dict = None) -> dict:
    """端到端自检：**签名 → 开会话 → 取样例消息 → 解密**。纯 HTTP，不需要浏览器。

    ## 为什么不能只校验微伴 jsapi_sign

    这条链路有**两把独立的凭据**，失效节奏完全不同：
      · 微伴登录态 cookie —— 滑动过期，`wb_keepalive` 每 6 小时刷一次就永不失效；
      · 企微 ``open_sid`` —— 页面写进 ``localStorage`` 的**缓存值**
        （组件源码：`if (!(n = localStorage.getItem("ww.open.sid"))) throw new Error("Missing open sid")`），
        它背后那侧的服务端会话会**先死**。

    实测（2026-10-15）：微伴 cookie 明明有效（`jsapi_sign` 返回 success），但
    ``createOpenChatSession`` 回 ``errCode 42006 missing session`` —— 正文通道实际
    已经断了 3 天（`content_state='ok'` 的 `fetched_at` 停在 10-13 13:26 UTC），
    而保活日志一路在报「✅ 凭据已刷新」。**只验一半就报成功，比不验更危险**。

    所以 `auth_service.verify()`（页面上的"校验凭据是否有效"）与
    `wb_keepalive.refresh()`（保活后的收尾）**都调这一个函数**，口径只有一份。

    返回 ``{ok, sid_tail, sample_msgs, decrypted, message, error}``；不抛异常。
    """
    out = {"ok": False, "sid_tail": None, "sample_msgs": 0, "decrypted": 0,
           "message": "", "error": ""}
    try:
        import weiban
    except Exception as e:                                  # noqa: BLE001
        out["error"] = out["message"] = f"模块导入失败：{e}"
        return out

    # ① + ②：能否建立解密会话 —— 这一步失败基本就是 open session 没了
    try:
        ww = WecomChatData(verbose=False)
        sid = ww.new_session()
        out["sid_tail"] = sid[-8:]
    except Exception as e:                                  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        out["message"] = "企微正文会话建立失败（open session 可能已失效）"
        return out

    # ③：取样例消息真解一遍。
    #   ⚠️ 两处都踩过：只看"第一个员工"很容易拿到名下没有会话的人，于是每次都
    #   "跳过解密冒烟" —— 而**跳过的冒烟等于没验**，通道断了也会报"正常"。
    #   所以优先用调用方给的/库里挑的**确实有消息**的会话，再退回"扫前几个员工"。
    #   :param session_hint: ``{"staff_id":..., "session_id":...}``（可选）
    try:
        inspected, msgs = 0, []
        hint = session_hint if session_hint and session_hint.get("staff_id") else \
            _sample_session_from_db()
        if hint.get("staff_id") and hint.get("session_id"):
            inspected = 1
            msgs = (weiban.list_messages(hint["staff_id"], hint["session_id"], limit=sample)
                    .get("message_list") or [])
        for staff in ([] if msgs else weiban.iter_all_staff()):
            inspected += 1
            if inspected > 5:
                break
            res = weiban.list_sessions(staff["id"], "ext_user", limit=1, offset=0,
                                       order_by="last_updated_desc")
            sess = (res.get("session_list") or [None])[0]
            if not sess:
                continue
            msgs = (weiban.list_messages(staff["id"], sess["id"], limit=sample)
                    .get("message_list") or [])
            if msgs:
                break
        out["sample_msgs"] = len(msgs)
        if msgs:
            out["decrypted"] = len(ww.decrypt(msgs))
        else:
            out["message"] = "正文会话已建立（查了 %d 个员工都没取到消息，跳过解密冒烟）" % inspected
    except Exception as e:                                  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        out["message"] = "正文会话已建立，但取样例消息失败"
        return out

    out["ok"] = True
    # 真解过样例才说"通道正常"；一条样例都没拿到时保留"跳过冒烟"的原话 ——
    # 含糊地把 0/0 说成"正常"，就是本文档开头批评的那种"只验一半"。
    if not out["message"]:
        out["message"] = (f"凭据有效，正文通道正常"
                          f"（解密 {out['decrypted']}/{out['sample_msgs']} 条样例消息）")
    return out


# ---------------------------------------------------------------- 明文格式化

TYPE_NAME = {1: "文本", 2: "图片", 3: "表情", 4: "链接", 5: "小程序", 6: "语音",
             7: "视频", 8: "文件", 9: "名片", 10: "聊天记录", 20: "混合",
             21: "语音通话", 31: "撤回消息"}


def plain_text(item: dict) -> str:
    """从企微明文条目里取出人类可读的正文。

    msgtype 数值映射（2026-10-13 按真实返回逐类核对）：
      1 文本 / 2 图片 / 3 表情 / 4 链接 / 5 小程序 / 6 语音 / 7 视频 / 8 文件
      9 名片 / 10 合并聊天记录 / 20 混合 / 21 语音通话 / 31 撤回
    """
    if not item:
        return ""
    mt = item.get("msgtype")
    if mt == 1:
        return ((item.get("text") or {}).get("content") or "").strip()
    if mt == 2:
        return ((item.get("image") or {}).get("media") or {}).get("url", "")
    if mt == 3:
        return (item.get("emotion") or {}).get("url", "")
    if mt == 4:
        link = item.get("link") or {}
        return f"[链接] {link.get('title') or ''} {link.get('url') or ''}".strip()
    if mt == 5:
        mp = item.get("miniprogram") or {}
        return f"[小程序] {mp.get('title') or mp.get('name') or ''}".strip()
    if mt == 6:
        v = item.get("voice") or {}
        sec = (v.get("attr") or {}).get("playtime")
        return f"[语音 {sec}s] {(v.get('media') or {}).get('url') or ''}".strip()
    if mt == 7:
        return ((item.get("video") or {}).get("media") or {}).get("url", "")
    if mt == 8:
        f = item.get("file") or {}
        name = (f.get("attr") or {}).get("filename") or ""
        url = (f.get("media") or {}).get("url") or ""
        return f"{name} {url}".strip()
    if mt == 9:
        c = item.get("personal_card") or {}
        return f"[名片] {c.get('name') or ''} {c.get('corp_name') or ''}".strip()
    if mt == 10:
        items = ((item.get("forward") or {}).get("items")) or []
        return f"[合并转发的聊天记录，共 {len(items)} 条]"
    if mt == 20:
        parts = []
        for it in ((item.get("mixed") or {}).get("msgitem")) or []:
            if it.get("text"):
                parts.append(it["text"].get("content") or "")
            elif it.get("emotion"):
                parts.append("[表情]")
            elif it.get("image"):
                parts.append("[图片]")
        return "".join(parts) or "[混合消息]"
    if mt == 21:
        mv = item.get("meeting_voice") or {}
        name = ((mv.get("voice") or {}).get("attr") or {}).get("filename") or ""
        return f"[语音通话] {name}".strip()
    if mt == 31:
        inner = ((item.get("revoke_msg") or {}).get("item") or {}).get("text") or {}
        content = inner.get("content")
        return f"[撤回了一条消息]{('：' + content) if content else ''}"
    return json.dumps(item, ensure_ascii=False)
