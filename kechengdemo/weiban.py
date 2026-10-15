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


def _request(method: str, path: str, params: dict = None, body: dict = None,
             headers: dict = None, retries: int = 2) -> dict:
    """统一走 `backend/http_client`（架构 §2.5 工具层）。

    改造前这里是裸 ``urlopen``：**一次瞬时网络抖动就整轮同步失败**（没有重试），
    而同一项目里 LLM 调用却有退避重试 —— 同一个「网络抖一下」两套行为。
    现在统一：超时 30s、只重试可重试故障（网络/408/429/5xx）、认证与参数错误立即失败。

    ⚠️ 返回值语义**保持不变**（仍是解析后的 dict，`errcode` 由调用方判断）——
    这样所有既有调用点零改动。
    """
    import sys
    from pathlib import Path as _P
    _backend = str(_P(__file__).resolve().parent / "backend")
    if _backend not in sys.path:
        sys.path.insert(0, _backend)
    import http_client
    return http_client.request_json(method, BASE + path, params=params, body=body,
                                    headers=headers, timeout=TIMEOUT, retries=retries,
                                    label="weiban")


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
                  retries: int = 3, base_delay: float = 0.8, headers: dict = None,
                  inner_retries: int = 2) -> dict:
    """带指数退避的请求（网络抖动 / 限流时自动重试）。

    与 ``_request`` 的区别：会吞掉网络层异常并重试；业务错误码仍由调用方判断。

    :param headers: 附加请求头（例如 ``access_token`` 走 header 而非 URL）。
    :param inner_retries: 内层 ``http_client`` 的重试次数；**单次投递**场景传 0
        （外层已有 outbox 退避，不需要这里再退避）。
    """
    last = None
    for i in range(max(1, retries)):
        try:
            return _request(method, path, params=params, body=body,
                            headers=headers, retries=inner_retries)
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
    # token 走 header（避免写进 URL / 日志）；**两种模式都经同一个 HTTP 层**。
    # 曾出现「同一函数两种行为」：header 模式是裸 urlopen，没有超时分类、没有 metrics，
    # 而另一支走 request_retry —— 改动一处时极易漏掉另一支。
    if use_header_token:
        return request_retry("POST", "/open-api/external_user/score/add", body=body,
                             retries=2, headers={"access_token": tok})
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


# ---------------- 客户标签（D2-1：等级改用微伴标签）----------------
#
# 与「客户评分」的根本差别（架构 §10 D2）：
#   评分 = 累计行为分，**只能累加、无覆盖接口** → 必须差值回写
#   标签 = **有增有删** → 用 batch_update 一次 add 新 + remove 旧 = 天然的「覆盖语义」
#
# ⚠️ 官方文档标注：以下标签接口**需购买基础席位**。

def _post_tag(path: str, body: dict) -> dict:
    """标签类 POST：token 走 **header**（不放 URL，避免写进日志/审计），单次投递。

    重试交给 `tag_outbox`（与分数回写同一套「至少一次 + 独立退避」思路），
    这里不叠加网络层重试 —— 否则两层重试会让「失败一次」变得很慢才可见。
    但仍走统一 ``http_client``（拿超时分类与 metrics 计数），只是把重试关到 0 次。
    """
    return request_retry("POST", path, body=body, retries=1, inner_retries=0,
                         headers={"access_token": get_access_token()})


def list_tags(limit: int = 1000, offset: int = 0) -> dict:
    """获取标签列表（`/open-api/tag/list`）。返回 `tag_list`（含 tag_id/tag_group/tag_name）。"""
    return _check(request_retry("GET", "/open-api/tag/list", params={
        "access_token": get_access_token(), "limit": limit, "offset": offset,
    }, retries=2), "获取标签列表")


def iter_all_tags(page_size: int = 200):
    """迭代全部标签（自动翻页）。"""
    offset = 0
    while True:
        res = list_tags(limit=page_size, offset=offset)
        batch = res.get("tag_list") or []
        for t in batch:
            yield t
        if len(batch) < page_size:
            break
        offset += page_size


def add_customer_tag(staff_id: str, external_user_id: str = "", unionid: str = "",
                     tag_group: str = "", tag_name: str = "") -> dict:
    """给客户添加企业标签（`/open-api/tag/add`）。

    ⚠️ **标签/标签组不存在时会自动创建** —— 这是真副作用。等级标签应**预先建好**
    （见 `grade_tag.sync_tags`），不要在评分链路里隐式创建。
    """
    if not staff_id:
        raise ValueError("add_customer_tag 需要 staff_id")
    if not external_user_id and not unionid:
        raise ValueError("add_customer_tag 需要 external_user_id 或 unionid")
    if not tag_group or not tag_name:
        raise ValueError("add_customer_tag 需要 tag_group 与 tag_name")
    body = {"staff_id": staff_id, "tag_group": tag_group, "tag_name": tag_name}
    if external_user_id:
        body["external_user_id"] = external_user_id
    if unionid:
        body["external_user_unionid"] = unionid
    return _post_tag("/open-api/tag/add", body)


def remove_customer_tag(staff_id: str, external_user_id: str = "", unionid: str = "",
                        tag_group: str = "", tag_name: str = "") -> dict:
    """删除客户的企业标签（`/open-api/tag/remove`）。

    ⚠️ 只影响**该客户**，不会从所有客户身上删除这个标签。
    """
    if not staff_id:
        raise ValueError("remove_customer_tag 需要 staff_id")
    if not external_user_id and not unionid:
        raise ValueError("remove_customer_tag 需要 external_user_id 或 unionid")
    if not tag_group or not tag_name:
        raise ValueError("remove_customer_tag 需要 tag_group 与 tag_name")
    body = {"staff_id": staff_id, "tag_group": tag_group, "tag_name": tag_name}
    if external_user_id:
        body["external_user_id"] = external_user_id
    if unionid:
        body["external_user_unionid"] = unionid
    return _post_tag("/open-api/tag/remove", body)


def batch_update_customer_tags(staff_id: str, external_user_id: str,
                               add_tag_id_list: list = None,
                               remove_tag_id_list: list = None) -> dict:
    """批量更新客户企业标签（`/open-api/tag/batch_update`）—— **D2-1 用这个**。

    一次调用同时 `add_tag_id_list`（新等级）+ `remove_tag_id_list`（旧等级），
    等价于「把该客户的等级设为 X」，且**不会出现中间态**。

    约束：`add` 与 `remove` **至少填一个**，各最多 50 个；`external_user_id` 必填。
    """
    if not staff_id:
        raise ValueError("batch_update_customer_tags 需要 staff_id")
    if not external_user_id:
        raise ValueError("batch_update_customer_tags 需要 external_user_id")
    add = [x for x in (add_tag_id_list or []) if x]
    rm = [x for x in (remove_tag_id_list or []) if x]
    if not add and not rm:
        raise ValueError("add_tag_id_list 与 remove_tag_id_list 至少要填一个")
    for name, lst in (("add_tag_id_list", add), ("remove_tag_id_list", rm)):
        if len(lst) > 50:
            raise ValueError(f"{name} 最多 50 个，收到 {len(lst)}")
    body = {"staff_id": staff_id, "external_user_id": external_user_id}
    if add:
        body["add_tag_id_list"] = add
    if rm:
        body["remove_tag_id_list"] = rm
    return _post_tag("/open-api/tag/batch_update", body)


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
