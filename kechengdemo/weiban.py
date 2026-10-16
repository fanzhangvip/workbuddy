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
from pathlib import Path

import config

BASE = config.WEIBAN_BASE
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
TOKEN_FILE = CACHE_DIR / "weiban_token.json"
TIMEOUT = 30


def _request(method: str, path: str, params: dict = None, body: dict = None,
             headers: dict = None, retries: int = 2,
             backoff: float = 1.2, backoff_cap: float = None) -> dict:
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
                                    backoff=backoff, backoff_cap=backoff_cap,
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


#: 读接口的**总尝试次数**（含首次）。
#: 3 次 = 首次 + 2 次重试（退避 0.8s / 1.6s，抖动 ±30%），最坏 ~2.4 秒放弃。
#: 为什么不是更多：读的调用方（同步任务 / 排空循环）本身有重试节奏，
#: 在这里死磕 9 次只会让「微伴不可用」表现为**同步看起来卡住**，
#: 反倒掩盖故障（失败快一点、由上层下一轮重来，更容易看清问题）。
READ_ATTEMPTS = 3


def _get(path: str, what: str, *, attempts: int = READ_ATTEMPTS, **extra) -> dict:
    """带 access_token 的 GET：**token 注入与 errcode 校验只此一处**。

    改造前每个读接口都自己拼一遍 `params={"access_token": get_access_token(), ...}`
    —— 同一个动作抄了 7 遍。这行「token 怎么带」的知识在写侧已经出过一次分歧
    （header vs URL，见 `add_customer_score` 的注释），读侧再抄 7 遍，
    以后不管怎么改都是 7 处要同步。

    ⚠️ 顺带统一了重试预算（改造前 `list_staff` 是 3 次，其余读接口是 9 次，
    **没人选过**这个差异，是历史长出来的）：现在读接口一律
    :data:`READ_ATTEMPTS` 次，且是**总次数**、不用做乘法。
    """
    return _check(
        request_retry("GET", path, attempts=attempts,
                      params={"access_token": get_access_token(), **extra}),
        what)


def _iter_paged(fetch, key, page_size: int):
    """通用翻页：反复调 `fetch(limit=, offset=)`，取响应里的 `key` 列表，直到**短页**为止。

    为什么必须是一个实现：改造前「offset 循环 + 短页即停」在 4 个 `iter_all_*`
    里各写一遍（staff / sessions / messages / tags）。这段循环有个踩过的坑：
    结束条件必须是「本页条数 < 请求条数」—— 写成「本页为空」在服务端把 `limit`
    静默截断时会**一直翻下去**。同一件事只该在一处写对。

    :param fetch: 形如 ``f(limit=..., offset=...) -> dict``
    :param key: 响应里承载列表的字段名，或**按优先级排列的多个候选名**
        （微伴各接口不统一：`tag_list` / `staff_list`，个别版本回 `staff`）
    """
    keys = (key,) if isinstance(key, str) else tuple(key)
    size = max(1, int(page_size))
    offset = 0
    while True:
        res = fetch(limit=size, offset=offset) or {}
        batch = next((res.get(k) for k in keys if res.get(k)), None) or []
        for row in batch:
            yield row
        if len(batch) < size:
            return
        offset += size


def request_retry(method: str, path: str, params: dict = None, body: dict = None,
                  attempts: int = READ_ATTEMPTS, base_delay: float = 0.8,
                  headers: dict = None) -> dict:
    """带指数退避的请求（网络抖动 / 限流时自动重试）。

    **只有一层重试实现**（`http_client`）。参数就是**总尝试次数**（含首次），
    不再有「轮数 × 每轮次数」那种要按乘法算的说法 —— 改造前是两层嵌套循环
    （外层 3 轮 × 内层 3 次 = **最多 9 次**，两套退避各自算），
    「到底会打几次、最多等多久」没人算得清，也没人选过那个 9。

    :param attempts: 总尝试次数。**读接口默认 3**（见 :data:`READ_ATTEMPTS`）；
        写操作一律传 1（重试归 outbox，见 :func:`add_customer_score`）。
    :param headers: 附加请求头（例如 ``access_token`` 走 header 而非 URL）。

    :raises http_client.HttpFailure: 重试耗尽后抛出（**不再是 RuntimeError**）。
        这是唯一比原实现多出来的信息量：``retryable`` / ``status`` / ``retry_after``
        都在异常上，调用方想据此改判重试策略时有据可依。既有调用点都是
        ``except Exception``，因此不受影响。
    """
    n = max(1, int(attempts))
    return _request(method, path, params=params, body=body, headers=headers,
                    retries=n - 1, backoff=base_delay,
                    backoff_cap=float(base_delay) * 3)


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
    #
    # ⚠️ **单次投递**（`attempts=1`）：这里绝不能再加重试。`score/add`
    # 是**累加**语义 —— 服务端把分加上去之后才超时/断连的话，每一次"重试"都会
    # 再加一遍分。改造前是两层嵌套（外层 2 × 内层 3 = 最多 6 次），而调用方
    # `scoring_service` 的注释写的是「live：单次投递，重试交给 outbox」——
    # 代码与自己的设计意图相反。重试归 outbox（有 max_attempts + 独立退避 + 状态记录）。
    # `_post_tag` 早就是单次投递，这里补齐，两者口径一致。
    if use_header_token:
        return request_retry("POST", "/open-api/external_user/score/add", body=body,
                             attempts=1,
                             headers={"access_token": tok})
    return request_retry("POST", "/open-api/external_user/score/add",
                         params={"access_token": tok}, body=body,
                         attempts=1)


def list_external_users(staff_id: str = "", limit: int = 100, offset: int = 0,
                        order: str = "desc", phone_number: str = "",
                        start_time: int = None, end_time: int = None) -> dict:
    """获取客户列表（可选按员工过滤）。返回 external_user_list（含 id/name/score）。"""
    extra = {"limit": limit, "offset": offset, "order": order}
    if staff_id:
        extra["staff_id"] = staff_id
    if phone_number:
        extra["phone_number"] = phone_number
    if start_time is not None:
        extra["start_time"] = start_time
    if end_time is not None:
        extra["end_time"] = end_time
    return _get("/open-api/external_user/list", "获取客户列表", **extra)


def get_custom_fields(staff_id: str, external_user_id: str) -> list:
    """获取客户自定义字段值列表（含标签类信息，可用于「到课/完课」判定）。

    返回 ``custom_remark_list``（失败时返回空列表，不抛出，避免影响主流程）。
    """
    try:
        res = _get("/open-api/external_user/remark/custom_field/get",
                   "获取客户自定义字段", staff_id=staff_id,
                   external_user_id=external_user_id)
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
    return request_retry("POST", path, body=body, attempts=1,
                         headers={"access_token": get_access_token()})


#: `/open-api/tag/list` 的 limit 上限 —— **实测**：101 以上（200/500/1000）一律
#: 返回 ``errcode=20357 查询tags limit不合法``，即整条标签链路直接不可用。
TAG_PAGE_MAX = 100


def _norm_tag(t: dict) -> dict:
    """把标签的**组名**归一到统一的 ``tag_group`` 键（原键保留）。

    ⚠️ 实测官方 ``/open-api/tag/list`` 返回的是 ``tag_group_name``（**没有** ``tag_group``）。
    直接读 ``tag_group`` 会永远拿到空串 → 任何「按组名筛选」的逻辑都会**全部匹配失败**，
    表现为「标签明明存在却被判为缺失」，进而在 create_missing 时**重复创建标签**（真副作用）。
    """
    if isinstance(t, dict) and not t.get("tag_group") and t.get("tag_group_name"):
        return {**t, "tag_group": t["tag_group_name"]}
    return t


def list_tags(limit: int = TAG_PAGE_MAX, offset: int = 0) -> dict:
    """获取标签列表（`/open-api/tag/list`）。返回 `tag_list`（含 tag_id/tag_group/tag_name）。

    ⚠️ ``limit`` **不得超过 :data:`TAG_PAGE_MAX`（100）**：微伴对超出上限的请求
    直接返回 20357，不会自动截断。这里主动夹取并改成合法值，避免调用方传大值就静默全失败。
    """
    lim = TAG_PAGE_MAX if not limit or int(limit) > TAG_PAGE_MAX else int(limit)
    res = _get("/open-api/tag/list", "获取标签列表", limit=lim, offset=offset)
    if isinstance(res, dict) and res.get("tag_list"):
        res["tag_list"] = [_norm_tag(t) for t in res["tag_list"]]
    return res


def iter_all_tags(page_size: int = TAG_PAGE_MAX):
    """迭代全部标签（自动翻页）。``page_size`` 同样受 :data:`TAG_PAGE_MAX` 约束。"""
    size = TAG_PAGE_MAX if not page_size or int(page_size) > TAG_PAGE_MAX else int(page_size)
    for t in _iter_paged(list_tags, "tag_list", size):
        yield _norm_tag(t)              # 已在 list_tags 归一，这里兜底（防止其它传入路径）


def _customer_tag_op(path: str, what: str, staff_id: str, external_user_id: str,
                     unionid: str, tag_group: str, tag_name: str) -> dict:
    """客户标签的增 / 删（两个接口**参数与校验完全一致**，只有路径和名字不同）。

    改造前 `add_customer_tag` 与 `remove_customer_tag` 是两份逐字相同的实现
    （校验三条、组 body 四行、`_post_tag`），差别只有 URL 和文档字符串。
    """
    if not staff_id:
        raise ValueError(f"{what} 需要 staff_id")
    if not external_user_id and not unionid:
        raise ValueError(f"{what} 需要 external_user_id 或 unionid")
    if not tag_group or not tag_name:
        raise ValueError(f"{what} 需要 tag_group 与 tag_name")
    body = {"staff_id": staff_id, "tag_group": tag_group, "tag_name": tag_name}
    if external_user_id:
        body["external_user_id"] = external_user_id
    if unionid:
        body["external_user_unionid"] = unionid
    return _post_tag(path, body)


def add_customer_tag(staff_id: str, external_user_id: str = "", unionid: str = "",
                     tag_group: str = "", tag_name: str = "") -> dict:
    """给客户添加企业标签（`/open-api/tag/add`）。

    ⚠️ **标签/标签组不存在时会自动创建** —— 这是真副作用。等级标签应**预先建好**
    （见 `grade_tag.sync_tags`），不要在评分链路里隐式创建。
    """
    return _customer_tag_op("/open-api/tag/add", "add_customer_tag", staff_id,
                            external_user_id, unionid, tag_group, tag_name)


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
    return _get("/open-api/staff/list", "获取员工列表", limit=limit, offset=offset)


def iter_all_staff(page_size: int = 100):
    """迭代全部员工（自动翻页）。"""
    # 两个键名兜底：不同接口版本分别返回 `staff_list` / `staff`
    for s in _iter_paged(list_staff, ("staff_list", "staff"), page_size):
        yield s


def list_sessions(staff_id: str, session_type: str = None,
                  limit: int = 100, offset: int = 0, order_by: str = None) -> dict:
    """获取某员工的会话列表。session_type: ext_user(客户) / room(群聊) / other_staff(同事)。"""
    extra = {"staff_id": staff_id, "limit": limit, "offset": offset}
    if session_type:
        extra["type"] = session_type
    if order_by:
        extra["order_by"] = order_by
    return _get("/open-api/chat/session/list", "获取会话列表", **extra)


def list_messages(staff_id: str, session_id: str, limit: int = 100, offset: int = 0,
                  start_time: int = None, end_time: int = None) -> dict:
    """获取指定会话的消息（v2，按消息时间逆序）。"""
    extra = {"staff_id": staff_id, "session_id": session_id,
             "limit": limit, "offset": offset}
    if start_time is not None:
        extra["start_time"] = start_time
    if end_time is not None:
        extra["end_time"] = end_time
    return _get("/open-api/chat/message/list/v2", "获取会话消息", **extra)


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
