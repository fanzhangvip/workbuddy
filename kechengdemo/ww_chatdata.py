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

        url = self.boot["url"]
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
            "url": self.boot["url"],
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


def type_name(item: dict) -> str:
    return TYPE_NAME.get((item or {}).get("msgtype"), "其它")
