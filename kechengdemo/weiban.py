# -*- coding: utf-8 -*-
"""微伴助手 OpenAPI 客户端（S1：认证 + 员工 / 会话 / 消息拉取）。

认证：POST /open-api/access_token/get
会话：GET  /open-api/chat/session/list
消息：GET  /open-api/chat/message/list/v2

说明：access_token 有效期 7200 秒，本地缓存于 .cache/ 下，提前 5 分钟自动刷新。
安全：token 仅用于本地运行，不打印、不写入日志与导出文件。
"""
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import config

BASE = config.WEIBAN_BASE
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
TOKEN_FILE = CACHE_DIR / "weiban_token.json"
TIMEOUT = 30


def _request(method: str, path: str, params: dict = None, body: dict = None) -> dict:
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_access_token(force: bool = False) -> str:
    """获取 access_token（带本地缓存，提前 5 分钟刷新）。"""
    if not force and TOKEN_FILE.exists():
        try:
            cached = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            if cached.get("expire_at", 0) > time.time() + 300:
                return cached["access_token"]
        except Exception:
            pass

    res = _request("POST", "/open-api/access_token/get",
                   body={"corp_id": config.CORP_ID, "secret": config.SECRET})
    if res.get("errcode") != 0:
        raise RuntimeError(f"获取 access_token 失败：{res}")
    token = res["access_token"]
    CACHE_DIR.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({
        "access_token": token,
        "expire_at": time.time() + int(res.get("expires_in", 7200)),
    }), encoding="utf-8")
    return token


def _check(res: dict, what: str) -> dict:
    if res.get("errcode") != 0:
        raise RuntimeError(f"{what}失败：{res}")
    return res


def request_retry(method: str, path: str, params: dict = None, body: dict = None,
                  retries: int = 3, base_delay: float = 0.8) -> dict:
    """带指数退避的请求（网络抖动 / 限流时自动重试）。

    与 ``_request`` 的区别：会吞掉网络层异常并重试；业务错误码仍由调用方判断。
    """
    last = None
    for i in range(max(1, retries)):
        try:
            return _request(method, path, params=params, body=body)
        except Exception as e:               # URLError / HTTPError / 超时
            last = e
            if i < retries - 1:
                time.sleep(base_delay * (2 ** i))
    raise RuntimeError(f"请求 {path} 连续 {retries} 次失败：{last}")


# ---------------- 客户评分 / 客户信息（客户管理）----------------

def add_customer_score(staff_id: str, external_user_id: str = "",
                       unionid: str = "", openid: str = "",
                       score: int = 0, use_header_token: bool = False) -> dict:
    """添加客户评分（微伴评分回写）。

    POST /open-api/external_user/score/add
    约束：``score`` 必须为 **大于 0 的整数**（否则错误码 20215）；
    ``external_user_id`` 与 ``external_user_unionid`` 必须二选一。

    返回原始响应 {errcode, errmsg}，由调用方判断成功与否（不回抛业务错误）。
    """
    if not staff_id:
        raise ValueError("add_customer_score 需要 staff_id")
    if not external_user_id and not unionid:
        raise ValueError("add_customer_score 需要 external_user_id 或 unionid")
    if not isinstance(score, int) or score <= 0:
        raise ValueError(f"score 必须为大于 0 的整数，收到 {score!r}")

    body = {"staff_id": staff_id, "score": int(score)}
    if external_user_id:
        body["external_user_id"] = external_user_id
    if unionid:
        body["external_user_unionid"] = unionid
    if openid:
        body["external_user_openid"] = openid

    tok = get_access_token()
    if use_header_token:                     # token 走 header，避免出现在 URL/日志里
        url = BASE + "/open-api/external_user/score/add"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "access_token": tok},
            method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return request_retry("POST", "/open-api/external_user/score/add",
                         params={"access_token": tok}, body=body, retries=2)


def list_external_users(staff_id: str = "", limit: int = 100, offset: int = 0,
                        order: str = "desc", phone_number: str = "",
                        start_time: int = None, end_time: int = None) -> dict:
    """获取客户列表（可选按员工过滤）。返回 external_user_list（含 id/name/score）。"""
    params = {"access_token": get_access_token(), "limit": limit, "offset": offset,
              "order": order}
    if staff_id:
        params["staff_id"] = staff_id
    if phone_number:
        params["phone_number"] = phone_number
    if start_time is not None:
        params["start_time"] = start_time
    if end_time is not None:
        params["end_time"] = end_time
    return _check(request_retry("GET", "/open-api/external_user/list", params=params),
                  "获取客户列表")


def get_custom_fields(staff_id: str, external_user_id: str) -> list:
    """获取客户自定义字段值列表（含标签类信息，可用于「到课/完课」判定）。

    返回 ``custom_remark_list``（失败时返回空列表，不抛出，避免影响主流程）。
    """
    try:
        res = request_retry("GET", "/open-api/external_user/remark/custom_field/get",
                            params={"access_token": get_access_token(),
                                    "staff_id": staff_id,
                                    "external_user_id": external_user_id}, retries=2)
        if res.get("errcode") != 0:
            return []
        return res.get("custom_remark_list") or []
    except Exception:
        return []


def list_staff(limit: int = 100, offset: int = 0) -> dict:
    """获取员工列表（可分页遍历全部员工）。"""
    return _check(_request("GET", "/open-api/staff/list", params={
        "access_token": get_access_token(), "limit": limit, "offset": offset,
    }), "获取员工列表")


def iter_all_staff(page_size: int = 100):
    """迭代全部员工（自动翻页）。"""
    offset = 0
    while True:
        res = list_staff(limit=page_size, offset=offset)
        batch = res.get("staff_list") or res.get("staff") or []
        for s in batch:
            yield s
        if len(batch) < page_size:
            break
        offset += page_size


def list_sessions(staff_id: str, session_type: str = None,
                  limit: int = 100, offset: int = 0, order_by: str = None) -> dict:
    """获取某员工的会话列表。session_type: ext_user(客户) / room(群聊) / other_staff(同事)。"""
    params = {"access_token": get_access_token(), "staff_id": staff_id,
              "limit": limit, "offset": offset}
    if session_type:
        params["type"] = session_type
    if order_by:
        params["order_by"] = order_by
    return _check(_request("GET", "/open-api/chat/session/list", params=params), "获取会话列表")


def iter_all_sessions(staff_id: str, session_type: str = "ext_user", page_size: int = 100):
    """迭代某员工全部会话（自动翻页）。"""
    offset = 0
    while True:
        res = list_sessions(staff_id, session_type, limit=page_size, offset=offset)
        batch = res.get("session_list", [])
        for s in batch:
            yield s
        if len(batch) < page_size:
            break
        offset += page_size


def list_messages(staff_id: str, session_id: str, limit: int = 100, offset: int = 0,
                  start_time: int = None, end_time: int = None) -> dict:
    """获取指定会话的消息（v2，按消息时间逆序）。"""
    params = {"access_token": get_access_token(), "staff_id": staff_id,
              "session_id": session_id, "limit": limit, "offset": offset}
    if start_time is not None:
        params["start_time"] = start_time
    if end_time is not None:
        params["end_time"] = end_time
    return _check(_request("GET", "/open-api/chat/message/list/v2", params=params), "获取会话消息")


def iter_all_messages(staff_id: str, session_id: str, page_size: int = 100,
                      start_time: int = None, end_time: int = None):
    """迭代单个会话的全部消息（自动翻页，按消息时间逆序）。"""
    offset = 0
    while True:
        res = list_messages(staff_id, session_id, limit=page_size, offset=offset,
                            start_time=start_time, end_time=end_time)
        batch = res.get("message_list", [])
        for m in batch:
            yield m
        if len(batch) < page_size:
            break
        offset += page_size


# ---------------- CLI 自测入口 ----------------

def _main():
    import argparse
    ap = argparse.ArgumentParser(description="微伴 OpenAPI 客户端自测")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("token", help="验证凭据并获取 access_token")

    p_staff = sub.add_parser("staff", help="列出员工")
    p_staff.add_argument("--limit", type=int, default=5)
    p_staff.add_argument("--all", action="store_true", help="翻页列出全部")

    p_sess = sub.add_parser("sessions", help="列出某员工的客户会话")
    p_sess.add_argument("--staff-id", required=True)
    p_sess.add_argument("--type", dest="stype", default="ext_user")
    p_sess.add_argument("--limit", type=int, default=5)

    p_msg = sub.add_parser("messages", help="列出某会话的消息")
    p_msg.add_argument("--staff-id", required=True)
    p_msg.add_argument("--session-id", required=True)
    p_msg.add_argument("--limit", type=int, default=5)

    args = ap.parse_args()
    missing = config.check_required()
    if missing:
        print(f"缺少配置项：{', '.join(missing)}（请检查 .env）")
        return

    if args.cmd == "token":
        tok = get_access_token()
        print(f"认证成功，access_token 长度 {len(tok)}，来源缓存/接口均可正常返回。")

    elif args.cmd == "staff":
        if args.all:
            n = 0
            for s in iter_all_staff():
                n += 1
                print(f"{n:>4}  {s.get('id')}  {s.get('name')}")
            print(f"共 {n} 名员工")
        else:
            res = list_staff(limit=args.limit)
            for s in res.get("staff_list") or res.get("staff") or []:
                print(f"  {s.get('id')}  {s.get('alias')}  {s.get('name')}")

    elif args.cmd == "sessions":
        res = list_sessions(args.staff_id, args.stype, limit=args.limit)
        print(f"total={res.get('total')}")
        for s in res.get("session_list", []):
            print(f"  {s.get('id')}  {s.get('name')}  type={s.get('session_type')}  last={s.get('last_msg_time')}")

    elif args.cmd == "messages":
        res = list_messages(args.staff_id, args.session_id, limit=args.limit)
        for m in res.get("message_list", []):
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.get("time", 0)))
            print(f"  {ts}  {m.get('from_name')}: {m.get('text_content')}")


if __name__ == "__main__":
    _main()
