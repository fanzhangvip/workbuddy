"""REST API 路由（设计文档第八节接口契约）。

约定：
- 统一返回 JSON；错误由类型化异常 → 全局处理器映射（不在此层 try/except 兜底）；
- 路由层只做参数解析与边界校验，业务全部委托给 :class:`AppContext` 及其组件；
- 密码等敏感字段**只入不出**：所有返回体都经过 :meth:`AppContext` 的脱敏路径。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from flask import Blueprint, jsonify, request

from ticket_helper.account.qrlogin import DEFAULT_QR_TTL
from ticket_helper.config.settings import (
    SEAT_TYPES,
    STRATEGIES,
    TASK_STATUS_STOPPED,
    TRAIN_TYPES,
    TaskConfig,
    UserConfig,
)
from ticket_helper.errors import NotFoundError, ValidationError
from ticket_helper.order.service import ORDER_STATUS_TEXT
from ticket_helper.runtime.context import AppContext
from ticket_helper import train_date as train_date_util
from ticket_helper.web.auth import TokenManager, current_user, require_auth, require_internal

log = logging.getLogger(__name__)

api = Blueprint("api", __name__, url_prefix="/api")

#: 由 register_api 注入
_CTX: AppContext | None = None
_TOKENS: TokenManager | None = None
_GUARD = None


def ctx() -> AppContext:
    assert _CTX is not None, "AppContext 未注入"
    return _CTX


def guard(view):
    """登录鉴权装饰器。

    注意：这里在**请求时**才去取 ``_GUARD``，而不是在 ``@guard`` 装饰执行的
    那一刻。装饰器在模块导入阶段就会执行，而 TokenManager 是随后由
    ``register_api`` 注入的 —— 如果在这里直接断言，导入模块本身就会崩。
    """
    from functools import wraps

    @wraps(view)
    def wrapper(*args, **kwargs):
        if _GUARD is None:
            raise RuntimeError("鉴权组件尚未初始化（请先调用 register_api）")
        return _GUARD(view)(*args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------- #
# 请求体解析
# --------------------------------------------------------------------------- #
def body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if data is None:
        data = request.form.to_dict() or {}
    if not isinstance(data, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    return data


def require_fields(data: dict[str, Any], *fields: str) -> None:
    missing = [f for f in fields if data.get(f) in (None, "")]
    if missing:
        raise ValidationError("缺少必填字段: " + ", ".join(missing))


def as_list(value: Any) -> list[str]:
    """容忍前端传来的「逗号分隔字符串 / 数组」两种形式。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        text = value.replace("，", ",").replace("\n", ",")
        return [v.strip() for v in text.split(",") if v.strip()]
    return []


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


# =========================================================================== #
# 认证
# =========================================================================== #
@api.post("/auth/login")
def login():
    data = body()
    require_fields(data, "user", "pass")
    assert _TOKENS is not None
    client = request.remote_addr or "unknown"

    if _TOKENS.is_locked(client):
        return jsonify({"code": "too_many_attempts", "message": "登录失败次数过多，请 5 分钟后再试"}), 429

    web_cfg = ctx().config.web
    if str(data["user"]) != web_cfg.auth_user or str(data["pass"]) != web_cfg.auth_pass:
        _TOKENS.record_failure(client)
        log.warning("后台登录失败: user=%s ip=%s", data.get("user"), client)
        return jsonify({"code": "unauthorized", "message": "用户名或密码错误"}), 401

    _TOKENS.clear_failures(client)
    token = _TOKENS.issue(str(data["user"]))
    log.info("后台登录成功: %s", data["user"])
    return jsonify({"token": token, "user": data["user"], "expires_in": _TOKENS.ttl})


@api.post("/auth/logout")
@guard
def logout():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if token and _TOKENS is not None:
        _TOKENS.revoke(token)
    return jsonify({"ok": True})


@api.get("/auth/me")
@guard
def me():
    return jsonify({"user": current_user(), "version": __import__("ticket_helper").__version__})


# =========================================================================== #
# 账号管理（WB-01 / AC-01 ~ AC-05）
# =========================================================================== #
@api.get("/users")
@guard
def list_users():
    context = ctx()
    # 合并「数据库中的账号」与「内存中的运行时状态」，前端一次拿到全部信息
    rows = {r["username"]: r for r in context.db.list_users()}
    users: list[dict[str, Any]] = []
    for user in context.accounts.all():
        runtime = user.to_dict()
        row = rows.get(user.username, {})
        runtime.update(
            {
                "is_checked": runtime.get("is_checked") or bool(row.get("is_checked")),
                "remark": row.get("remark", ""),
            }
        )
        # 这里**不能**硬编码 has_password=True：扫码登录创建的账号本来就没有密码，
        # 硬编码会让前端把「扫码账号」显示成「密码账号」，还会放出必然失败的「测试登录」按钮。
        # 正常由 User.to_dict() 提供；setdefault 只是给 DB-only 的异常形态兜个底。
        runtime.setdefault("has_password", bool(row.get("password_enc")))
        runtime.setdefault("login_type", "password" if runtime["has_password"] else "qr")
        users.append(runtime)
    return jsonify({"users": users, "summary": context.accounts.snapshot()})


@api.post("/users")
@guard
def create_user():
    data = body()
    require_fields(data, "username")
    cfg = UserConfig(
        username=str(data["username"]).strip(),
        password=str(data.get("password", "")),
        heartbeat=int(data.get("heartbeat", 120) or 120),
        enabled=as_bool(data.get("enabled"), True),
    )
    errors = cfg.validate()
    if errors:
        raise ValidationError("; ".join(errors))
    context = ctx()
    user = context.upsert_user(cfg)
    # 保存后立即在后台登录（AC-02）。否则「启动之后才添加的账号」永远是离线状态，
    # 用户会以为程序坏了 —— 实际上只是没人去触发登录。
    queued, message = _auto_login_after_save(context, user)
    return (
        jsonify(
            {
                "ok": True,
                "id": user.id,
                "username": user.username,
                "login_queued": queued,
                "message": message,
            }
        ),
        201,
    )


def _auto_login_after_save(context: Any, user: Any) -> tuple[bool, str]:
    """保存账号后的自动登录，返回 ``(是否已提交登录, 给用户看的提示)``。"""
    if not user.enabled:
        return False, "账号已保存（当前为禁用状态，启用后会自动登录）"
    if not user.password:
        # 扫码登录创建的账号本来就没有密码，提交密码登录只会白跑一趟。
        return False, "账号已保存。该账号没有密码，请使用「扫码登录」"
    if context.login_user_async(user.username, force=True):
        return True, "账号已保存，正在登录…"
    return False, "账号已保存（已在线，无需重复登录）"


@api.put("/users/<int:user_id>")
@guard
def update_user(user_id: int):
    data = body()
    context = ctx()
    row = context.db.get_user(user_id)
    if row is None:
        raise NotFoundError(f"账号不存在: {user_id}")

    password = str(data.get("password") or "")
    # 密码发生变化时必须强制重新登录，否则旧会话会一直沿用错误凭据。
    password_changed = bool(password)

    user = context.accounts.get(row["username"])
    if user is not None:
        if "heartbeat" in data:
            user.heartbeat_interval = max(30, int(data["heartbeat"]))
        if password_changed:
            user.set_password(password)
        if "enabled" in data:
            user.set_enabled(as_bool(data["enabled"], user.enabled))

    if password_changed:
        context.db.update_user_state(user_id, password_enc=context.cipher.encrypt(password))
    for key in ("heartbeat", "enabled", "remark"):
        if key in data:
            context.db.update_user_state(user_id, **{key: data[key]})

    queued, message = _auto_login_after_save(context, user) if user is not None else (False, "已更新")
    return jsonify({"ok": True, "login_queued": queued, "message": message})


@api.put("/users/<int:user_id>/state")
@guard
def toggle_user(user_id: int):
    data = body()
    context = ctx()
    row = context.db.get_user(user_id)
    if row is None:
        raise NotFoundError(f"账号不存在: {user_id}")
    enabled = as_bool(data.get("enabled"), True)
    context.accounts.enable(row["username"], enabled)
    context.db.update_user_state(user_id, enabled=int(enabled))
    return jsonify({"ok": True, "enabled": enabled})


@api.delete("/users/<int:user_id>")
@guard
def delete_user(user_id: int):
    context = ctx()
    row = context.db.get_user(user_id)
    if row is None:
        raise NotFoundError(f"账号不存在: {user_id}")
    context.remove_user(row["username"])
    return jsonify({"ok": True})


@api.post("/users/<int:user_id>/verify/refresh")
@guard
def refresh_user_verify(user_id: int):
    """重新检测一次人证核验状态（AC-07）。

    存在的意义：去 12306 App 里做完人证核验之后，**界面得有个地方能刷新**。
    原来是靠「测试登录」顺带带上这一步的，但扫码登录的账号没有密码、
    「测试登录」是禁用状态 —— 于是这类账号完成核验后，界面会一直停在旧状态，
    使用者会以为核验没生效。

    只读接口，不改变任何配置。
    """
    context = ctx()
    row = context.db.get_user(user_id)
    if row is None:
        raise NotFoundError(f"账号不存在: {user_id}")
    user = context.accounts.get(row["username"])
    if user is None:
        raise NotFoundError("该账号未加载到运行时，请重启后台")
    if not user.is_login:
        raise ValidationError("账号当前不在线，无法检测核验状态；请先登录（扫码）")

    try:
        checked = user.detect_verified()
    except Exception as exc:  # noqa: BLE001 - 检测失败不该把接口打挂
        raise ValidationError(f"检测核验状态失败：{exc}") from exc

    from ticket_helper.account import verify as verify_mod

    state = verify_mod.state_of(user)
    return jsonify(
        {
            "ok": True,
            # verify_known=False 表示「判不出来」，此时**不能**说成「未核验」
            "verify_known": user.verify_known,
            "is_checked": bool(checked),
            "verify_state": state,
            "verify_label": verify_mod.LABEL[state],
            "verify_hint": verify_mod.HINT[state],
            "can_standby": not verify_mod.should_block(user),
        }
    )


@api.post("/users/<int:user_id>/login")
@guard
def login_user(user_id: int):
    """触发一次账号登录（异步，避免请求超时）。"""
    context = ctx()
    row = context.db.get_user(user_id)
    if row is None:
        raise NotFoundError(f"账号不存在: {user_id}")
    user = context.accounts.get(row["username"])
    if user is None:
        raise NotFoundError("该账号未加载到运行时，请重启后台")

    if not user.password:
        secret = context.user_secret(user.username)
        if secret:
            user.set_password(secret)

    if not user.password:
        # 扫码登录创建的账号没有密码，触发密码登录只会得到一句含糊的失败。
        # 这里直接把原因和下一步告诉使用者。
        raise ValidationError("该账号是扫码登录账号（没有保存密码），请使用「扫码登录」重新登录")

    queued = context.login_user_async(user.username, force=True)
    if not queued:
        return jsonify({"ok": True, "login_queued": False, "message": "已有登录正在进行，请稍候…"})
    return jsonify({"ok": True, "login_queued": True, "message": "已发起登录，正在获取验证码…"})


# --------------------------------------------------------------------------- #
# 扫码登录（AC-08）
# --------------------------------------------------------------------------- #
def _qr_payload(status: Any, *, include_image: bool = False) -> dict[str, Any]:
    """扫码状态 → 前端契约。

    **两个 uuid 必须区分开**，否则「二维码自动换码」这条链路必崩：

    - ``uuid``：**会话标识**，从建会话到结束都不变，前端用它做轮询的键；
    - ``qr_uuid``：**当前这张二维码**的 uuid，约 20 秒换一次，
      前端拿它和界面上显示的比对，只有变了才重画图片。

    早期实现把两者混为一个字段，二维码一刷新，前端拿着旧 key 轮询就会
    直接拿到「会话不存在」。
    """
    payload = status.to_dict(include_image=include_image)
    payload["qr_uuid"] = status.uuid
    # uuid 在对外契约里统一表示「轮询用的稳定键」
    payload["uuid"] = status.session_id or status.uuid
    return payload


@api.post("/users/qr/start")
@guard
def start_qr_login():
    """申请一个登录二维码。**非阻塞**：拿到二维码立刻返回，前端自行轮询状态。"""
    body = request.get_json(silent=True) or {}
    username_hint = str(body.get("username_hint") or body.get("username") or "")
    # 显式判 None：用 `body.get("ttl") or 默认值` 会让 ttl=0 被当成「没传」而静默走默认值，
    # 使用者传 0 想表达「立刻过期」时会得到一个 180 秒的二维码，很难理解。
    raw_ttl = body.get("ttl")
    try:
        ttl = float(raw_ttl) if raw_ttl is not None else DEFAULT_QR_TTL
    except (TypeError, ValueError):
        ttl = DEFAULT_QR_TTL
    # 有效范围收敛一下：ttl=0 会让二维码立刻失效（也许正是调用方想要的），
    # 但不能是负数；上限避免会话永不回收。
    ttl = min(600.0, max(5.0, ttl))

    context = ctx()
    status = context.start_qr_login(username_hint=username_hint, ttl=ttl)
    payload = _qr_payload(status, include_image=True)
    payload["expires_in"] = int(ttl)
    log.info("Web 端发起扫码登录: session=%s qr=%s state=%s", status.session_id, status.uuid, status.state)
    return jsonify(payload)


@api.get("/users/qr/status")
@guard
def qr_login_status():
    """轮询扫码状态；确认成功时会在此刻完成登录并把账号写入账号列表。

    ``seen`` 是前端**当前显示着的那张二维码**的 uuid。
    官方二维码只有约 20 秒寿命，会话会自动换新码，
    这里只在「换过码」时把新图带回去 —— 否则每次都重传一张 PNG 纯属浪费。
    """
    uuid = str(request.args.get("uuid") or "").strip()
    if not uuid:
        raise ValidationError("缺少参数 uuid")
    context = ctx()
    status = context.qr_login_status(uuid)
    if status is None:
        # 会话不存在（已过期回收 / 后台重启过）：给出明确结论让前端提示重新生成
        raise NotFoundError("扫码会话不存在或已结束，请重新获取二维码")
    seen = str(request.args.get("seen") or "").strip()
    payload = _qr_payload(status, include_image=(seen != status.uuid))
    if status.succeeded:
        payload["message"] = f"账号 {status.username} 登录成功"
    return jsonify(payload)


@api.post("/users/qr/cancel")
@guard
def cancel_qr_login():
    """用户关闭弹窗：立刻停止轮询，避免继续打接口。"""
    body = request.get_json(silent=True) or {}
    uuid = str(body.get("uuid") or "").strip()
    if not uuid:
        raise ValidationError("缺少参数 uuid")
    context = ctx()
    return jsonify({"ok": context.cancel_qr_login(uuid), "uuid": uuid})


@api.get("/users/<int:user_id>/passengers")
@guard
def user_passengers(user_id: int):
    """读取账号下的乘车人列表（配置任务时选择用）。

    走的是个人中心链路（``/otn/passengers/query``）：不需要先锁票，
    也不会在下单上下文里留下痕迹。代价是官方对证件号打了码 ——
    对「展示 / 选人」这个用途来说，打码反而是对的。
    下单要用的完整证件号走 ``getPassengerDTOs``，见 ``TicketApi.get_passengers``。
    """
    context = ctx()
    user = _require_runtime_user(user_id)
    section = context.profile_service.passengers_of(
        user, refresh=as_bool(request.args.get("refresh"))
    )
    if not section["ok"]:
        raise ValidationError(section["error"])
    return jsonify({"passengers": section["data"]})


# --------------------------------------------------------------------------- #
# 个人中心：账号信息 / 我的订单（AC-06 / OD-01）
# --------------------------------------------------------------------------- #
def _require_runtime_user(user_id: int):
    """取「数据库里有、运行时也加载了」的账号对象。

    这两步以前在四个路由里各抄了一遍，且报错措辞不一致
    （「账号不存在」/「未加载到运行时」混着用）。集中一处，
    顺带保证新加的接口不会漏掉运行时校验。
    """
    context = ctx()
    row = context.db.get_user(user_id)
    if row is None:
        raise NotFoundError(f"账号不存在: {user_id}")
    user = context.accounts.get(row["username"])
    if user is None:
        raise NotFoundError("该账号未加载到运行时，请重启后台")
    return user


def _section_payload(section: dict[str, Any]) -> dict[str, Any]:
    """服务层的分块结果 → 对外契约（统一 ``ok`` / ``error`` / ``data``）。"""
    return {
        "ok": bool(section.get("ok")),
        "error": section.get("error", ""),
        "data": section.get("data"),
        **({"cached": True} if section.get("cached") else {}),
    }


@api.get("/users/<int:user_id>/profile")
@guard
def user_profile(user_id: int):
    """账号档案：账号信息 + 乘车人 + 未完成订单 +（可选）我的订单。

    **会真的去打 12306**，所以默认只带「账号信息 + 乘车人 + 未完成订单」，
    订单需要显式 ``with_orders=1`` —— 订单接口带日期分页，代价更高，
    一打开个人中心就把三四个接口一起打出去，是把自己往风控里送。
    """
    context = ctx()
    user = _require_runtime_user(user_id)
    result = context.profile_service.fetch(
        user,
        with_passengers=as_bool(request.args.get("with_passengers"), True),
        with_pending=as_bool(request.args.get("with_pending"), True),
        with_orders=as_bool(request.args.get("with_orders"), False),
        where=str(request.args.get("where") or "G"),
        page_index=request.args.get("page", default=1, type=int),
        page_size=min(50, request.args.get("size", default=10, type=int)),
        refresh_passengers=as_bool(request.args.get("refresh")),
    )
    payload: dict[str, Any] = {
        "account": {**result["account"], "id": user_id},
        "profile": _section_payload(result["profile"]),
    }
    for key in ("passengers", "pending", "orders"):
        if key in result:
            payload[key] = _section_payload(result[key])
    return jsonify(payload)


@api.get("/users/<int:user_id>/orders")
@guard
def user_orders(user_id: int):
    """「我的订单」分页查询。

    ``where=G`` 未出行 / ``H`` 历史；``kw`` 支持按订单号 / 车次 / 乘客姓名筛选。
    """
    context = ctx()
    user = _require_runtime_user(user_id)
    where = str(request.args.get("where") or "G").upper()
    if where not in ("G", "H"):
        # 参数非法属于「调用方传错了」，必须在这里拦成 400。
        # 交给下面的服务层去捕获，会被它按「上游查询失败」处理成 200 + ok=false，
        # 前端就分不清「我传错了」和「12306 拒绝了」。
        raise ValidationError(f"where 只能是 G（未出行）或 H（历史），收到: {where}")
    section = context.profile_service.orders_of(
        user,
        where=where,
        query_type=request.args.get("type", default=1, type=int),
        start_date=str(request.args.get("start") or ""),
        end_date=str(request.args.get("end") or ""),
        page_index=request.args.get("page", default=1, type=int),
        page_size=min(50, request.args.get("size", default=10, type=int)),
        keyword=str(request.args.get("kw") or ""),
    )
    payload = _section_payload(section)
    payload["account"] = user.username
    return jsonify(payload)


# =========================================================================== #
# 任务管理（WB-02 / WB-04）
# =========================================================================== #
def _task_from_payload(data: dict[str, Any], task_id: int | None = None) -> TaskConfig:
    train_filter = data.get("train_filter") or {}
    return TaskConfig(
        id=task_id,
        name=str(data.get("name", "")).strip(),
        left_dates=as_list(data.get("left_dates")),
        from_stations=as_list(data.get("from_stations") or data.get("from")),
        to_stations=as_list(data.get("to_stations") or data.get("to")),
        trains_whitelist=as_list(
            data.get("trains_whitelist") if data.get("trains_whitelist") is not None else train_filter.get("whitelist")
        ),
        trains_blacklist=as_list(
            data.get("trains_blacklist") if data.get("trains_blacklist") is not None else train_filter.get("blacklist")
        ),
        train_types=as_list(data.get("train_types")),
        seat_types=as_list(data.get("seat_types")),
        depart_time_range=as_list(data.get("depart_time_range")),
        passengers=as_list(data.get("passengers")),
        accounts=as_list(data.get("accounts")),
        strategy=str(data.get("strategy", "direct")),
        query_interval=float(data.get("query_interval", 1.0) or 1.0),
        sale_open_time=str(data.get("sale_open_time", "") or ""),
        sale_prepare_seconds=int(data.get("sale_prepare_seconds", 30) or 30),
        max_retry=int(data.get("max_retry", 20) or 20),
        priority=int(data.get("priority", 0) or 0),
        remark=str(data.get("remark", "") or ""),
        enabled=as_bool(data.get("enabled"), True),
        status=str(data.get("status", TASK_STATUS_STOPPED)),
    )


@api.get("/tasks")
@guard
def list_tasks():
    context = ctx()
    tasks = context.list_tasks()
    running = {t["task_id"]: t for t in context.scheduler.status_all()}
    payload = []
    for task in tasks:
        item = {
            "id": task.id,
            "name": task.name,
            "left_dates": task.left_dates,
            "from_stations": task.from_stations,
            "to_stations": task.to_stations,
            "trains_whitelist": task.trains_whitelist,
            "trains_blacklist": task.trains_blacklist,
            "train_types": task.train_types,
            "seat_types": task.seat_types,
            "depart_time_range": task.depart_time_range,
            "passengers": task.passengers,
            "accounts": task.accounts,
            "strategy": task.strategy,
            "query_interval": task.query_interval,
            "sale_open_time": task.sale_open_time,
            "sale_prepare_seconds": task.sale_prepare_seconds,
            "max_retry": task.max_retry,
            "priority": task.priority,
            "remark": task.remark,
            "enabled": task.enabled,
            "status": task.status,
            "runtime": running.get(task.id or 0),
            # 哪些乘车日期 12306 现在查不到、为什么 —— **规则只在 train_date 一处**，
            # 两端只渲染。以前界面把日期原样打出来就算完事，
            # 于是「任务填成了去年」这件事在列表上完全看不出来。
            "date_hints": [
                {"date": value, "verdict": verdict, "text": train_date_util.explain(value)}
                for value in task.left_dates
                for verdict in (train_date_util.classify(value),)
                if verdict != train_date_util.DATE_OK
            ],
        }
        payload.append(item)
    return jsonify(
        {
            "tasks": payload,
            "summary": context.scheduler.summary(),
            "date_window": context.date_window(),
        }
    )


@api.post("/tasks")
@guard
def create_task():
    task = _task_from_payload(body())
    context = ctx()
    task = context.save_task(task)
    log.info("任务已创建: %s (id=%s)", task.name, task.id)
    return jsonify({"ok": True, "id": task.id}), 201


@api.get("/tasks/<int:task_id>")
@guard
def get_task(task_id: int):
    from ticket_helper.storage.repository import TaskRepository

    task = TaskRepository(ctx().db).get(task_id)
    if task is None:
        raise NotFoundError(f"任务不存在: {task_id}")
    return jsonify({"task": task.__dict__, "runtime": ctx().task_status(task_id)})


@api.put("/tasks/<int:task_id>")
@guard
def update_task(task_id: int):
    context = ctx()
    from ticket_helper.storage.repository import TaskRepository

    if TaskRepository(context.db).get(task_id) is None:
        raise NotFoundError(f"任务不存在: {task_id}")
    task = _task_from_payload(body(), task_id)
    task = context.save_task(task)

    # 已在运行的任务热更新配置，无需重启（CF-04）
    wrapper = context.scheduler.get(task_id)
    if wrapper is not None:
        wrapper.config = task
        wrapper.grab_engine.apply_config(task)
        if wrapper.standby_engine is not None:
            wrapper.standby_engine.apply_config(task)
    return jsonify({"ok": True})


@api.delete("/tasks/<int:task_id>")
@guard
def delete_task(task_id: int):
    if not ctx().delete_task(task_id):
        raise NotFoundError(f"任务不存在: {task_id}")
    return jsonify({"ok": True})


@api.post("/tasks/<int:task_id>/start")
@guard
def start_task(task_id: int):
    ctx().start_task(task_id)
    return jsonify({"ok": True, "status": ctx().task_status(task_id)})


@api.post("/tasks/<int:task_id>/stop")
@guard
def stop_task(task_id: int):
    ok = ctx().stop_task(task_id)
    return jsonify({"ok": ok, "status": ctx().task_status(task_id)})


@api.get("/tasks/<int:task_id>/status")
@guard
def task_status(task_id: int):
    status = ctx().task_status(task_id)
    if status is None:
        raise NotFoundError(f"任务不存在: {task_id}")
    return jsonify(status)


@api.get("/tasks/<int:task_id>/remain")
@guard
def task_remain(task_id: int):
    return jsonify(ctx().task_remain(task_id))


@api.get("/tasks/status/all")
@guard
def tasks_status_all():
    return jsonify({"tasks": ctx().scheduler.status_all(), "summary": ctx().scheduler.summary()})


# =========================================================================== #
# 订单（OD-01 / OD-04）
# =========================================================================== #
@api.get("/orders")
@guard
def list_orders():
    status = request.args.get("status") or None
    task_id = request.args.get("task_id", type=int)
    limit = min(500, request.args.get("limit", default=100, type=int))
    orders = ctx().db.list_orders(status=status, task_id=task_id, limit=limit)
    # 展示文案由服务端给（``ORDER_STATUS_TEXT`` 是唯一定义处）：
    # 两端界面直接渲染，不再各自猜状态字符串。
    for order in orders:
        order["status_text"] = ORDER_STATUS_TEXT.get(str(order.get("status") or ""), "")
    return jsonify({"orders": orders})


@api.get("/orders/unfinished")
@guard
def unfinished_orders():
    context = ctx()
    results: list[dict[str, Any]] = []
    for user in context.accounts.online_users():
        try:
            for order in context.order_service.query_unfinished(user):
                order["account"] = user.username
                results.append(order)
        except Exception as exc:  # noqa: BLE001
            log.warning("查询账号 %s 未完成订单失败: %s", user.username, exc)
    return jsonify({"orders": results})


# =========================================================================== #
# 统计与监控（WB-04 / WB-05 / LG-04）
# =========================================================================== #
@api.get("/stats")
@guard
def stats():
    context = ctx()
    return jsonify({"storage": context.db.stats(), "runtime": context.snapshot()})


@api.get("/cluster")
@guard
def cluster():
    context = ctx()
    config = context.config
    return jsonify(
        {
            "enabled": config.cluster.enabled,
            "node": config.cluster.node_name or "local",
            "role": config.cluster.role,
            "redis": bool(config.cluster.redis_url),
            "scheduler": context.scheduler.summary(),
            "accounts": context.accounts.snapshot(),
            "api_base": config.api_base,
        }
    )


@api.get("/system/snapshot")
@guard
def system_snapshot():
    return jsonify(ctx().snapshot())


@api.post("/system/start")
@guard
def system_start():
    context = ctx()
    if context.running:
        return jsonify({"ok": True, "message": "后台已在运行"})

    def _worker() -> None:
        try:
            context.start()
        except Exception as exc:  # noqa: BLE001
            log.error("后台启动失败: %s", exc)

    threading.Thread(target=_worker, name="context-start", daemon=True).start()
    return jsonify({"ok": True, "message": "正在启动后台（登录账号需要一点时间）"})


@api.post("/system/stop")
@guard
def system_stop():
    ctx().stop()
    return jsonify({"ok": True})


@api.get("/logs")
@guard
def logs():
    limit = min(1000, request.args.get("limit", default=200, type=int))
    level = request.args.get("level") or None
    from ticket_helper.logger import get_log_bus

    return jsonify({"logs": get_log_bus().recent(limit=limit, level=level)})


@api.get("/logs/export")
@guard
def export_logs():
    from flask import send_file

    path = ctx().paths.log_file
    if not path.exists():
        raise NotFoundError("日志文件尚未生成")
    return send_file(path, as_attachment=True, download_name="ticket-helper-12306.log")


@api.get("/notifications")
@guard
def notifications():
    limit = min(500, request.args.get("limit", default=100, type=int))
    return jsonify({"notifications": ctx().db.list_notifications(limit), "channels": ctx().notifier.snapshot()})


@api.post("/notify/test")
@guard
def test_notify():
    data = body()
    channel = data.get("channel")
    context = ctx()
    if channel:
        ok = context.notifier.test_channel(str(channel), data.get("config"))
        return jsonify({"ok": ok, "channel": channel})
    return jsonify({"results": context.notifier.test_all()})


# =========================================================================== #
# 配置中心（WB-06 / CF-04）
# =========================================================================== #
@api.get("/config")
@guard
def get_config():
    context = ctx()
    return jsonify(
        {
            "config": context.config.to_dict(mask_secrets=True),
            "version": context.loader.version,
            "options": {
                "seat_types": list(SEAT_TYPES),
                "train_types": list(TRAIN_TYPES),
                "strategies": list(STRATEGIES),
            },
        }
    )


@api.put("/config/<section>")
@guard
def update_config(section: str):
    data = body()
    config = ctx().update_config_section(section, data)
    return jsonify({"ok": True, "version": ctx().loader.version, "config": config.to_dict(mask_secrets=True)})


@api.get("/config/history")
@guard
def config_history():
    limit = min(500, request.args.get("limit", default=100, type=int))
    return jsonify({"history": ctx().db.list_config_history(limit)})


# =========================================================================== #
# 验证码人工兜底（VC-04）
# =========================================================================== #
@api.get("/captcha/pending")
@guard
def captcha_pending():
    tasks = ctx().verify_service.manual.pending()
    return jsonify({"tasks": [t.to_dict() for t in tasks]})


@api.post("/captcha/submit")
@guard
def captcha_submit():
    data = body()
    require_fields(data, "id", "answer")
    ok = ctx().verify_service.manual.submit(str(data["id"]), str(data["answer"]))
    return jsonify({"ok": ok})


# =========================================================================== #
# 站点检索（任务配置页「出发站 / 到达站」选择器用）
#
# 数据源就是 StationMap —— 启动时会从 12306 官方 station_name.js 拉全量表并缓存，
# 离线时退回包内常用站表。所以「从 12306 选站」这件事不需要新链路，
# 需要的是把检索做得好用：能搜、有常用站兜底、排序合理。
# =========================================================================== #
@api.get("/stations")
@guard
def stations():
    keyword = (request.args.get("kw") or "").strip()
    limit = min(200, max(1, request.args.get("limit", default=60, type=int)))
    items = [
        {"name": name, "code": code}
        for name, code in ctx().station_map.search(keyword, limit=limit)
    ]
    return jsonify(
        {
            "count": len(items),
            "keyword": keyword,
            # stations 保留「纯站名列表」的旧形态，避免老调用方（含桌面端）被改动波及
            "stations": [item["name"] for item in items],
            "items": items,
        }
    )


# =========================================================================== #
# 本机专用：一次性把账号写入加密存储（桌面端首次配置用）
# =========================================================================== #
@api.post("/internal/seed-user")
@require_internal()
def seed_user():
    """仅本机可调用：写入账号（密码立即加密）。

    之所以单独开这个接口并加本机限制：桌面端首次使用时需要把账号交给后台，
    但又不能允许公网来源调用，否则等于给后台开了个「写入任意账号」的口子。
    """
    data = body()
    require_fields(data, "username", "password")
    cfg = UserConfig(
        username=str(data["username"]).strip(),
        password=str(data["password"]),
        heartbeat=int(data.get("heartbeat", 120) or 120),
        enabled=as_bool(data.get("enabled"), True),
    )
    user = ctx().upsert_user(cfg)
    return jsonify({"ok": True, "id": user.id, "username": user.username}), 201


@api.get("/internal/ping")
@require_internal()
def internal_ping():
    """桌面端用于探测后台是否已就绪（无需登录令牌，但**仅本机可调用**）。

    之所以不开放给任意来源：它会暴露账号数量与在线情况，
    在没有鉴权的前提下放到公网等于免费的情报接口。
    """
    context = ctx()
    accounts = context.accounts.snapshot()
    return jsonify(
        {
            "ok": True,
            "version": __import__("ticket_helper").__version__,
            "running": context.running,
            "online": accounts["online"],
            "total": accounts["total"],
            "ts": time.time(),
        }
    )


# =========================================================================== #
# 车次候选拉取（任务配置页：白名单 / 黑名单直接勾选，不用手敲车次号）
# =========================================================================== #
@api.get("/trains/options")
@guard
def train_options():
    """按「乘车日期 × 起止站」从 12306 拉取候选车次。

    参数（都支持逗号分隔，也可重复出现）::

        GET /api/trains/options?dates=2026-10-01,2026-10-02&from=广州南&to=长沙南

    ``refresh=1`` 时绕过短时缓存强制重查（用户手点「重新拉取」）。

    **返回 200 但 ``errors`` 非空是正常结果**：某个日期/线路查不到只影响它自己，
    其余车次照常返回；整体失败（没有在线账号等）才走类型化异常的 4xx。
    """
    dates = as_list(request.args.get("dates") or request.args.get("left_dates"))
    from_stations = as_list(request.args.get("from") or request.args.get("from_stations"))
    to_stations = as_list(request.args.get("to") or request.args.get("to_stations"))
    if not (dates and from_stations and to_stations):
        raise ValidationError("需要 dates（乘车日期）、from（出发站）、to（到达站）三个参数")
    return jsonify(
        ctx().query_train_options(
            dates=dates,
            from_stations=from_stations,
            to_stations=to_stations,
            account=(request.args.get("account") or "").strip(),
            refresh=as_bool(request.args.get("refresh")),
        )
    )


# =========================================================================== #
# 车辆信息辅助（QY-07 经停信息）
# =========================================================================== #
@api.get("/trains/stop-stations")
@guard
def stop_stations():
    train_no = request.args.get("train_no", "")
    date = request.args.get("date", "")
    from_code = request.args.get("from_station", "")
    to_code = request.args.get("to_station", "")
    if not (train_no and date and from_code and to_code):
        raise ValidationError("需要 train_no / date / from_station / to_station 四个参数")
    context = ctx()
    user = next(iter(context.accounts.online_users()), None)
    if user is None or user.api is None:
        raise ValidationError("没有在线账号，无法查询经停信息")
    stops = user.api.query_stop_stations(train_no, from_code, to_code, date)
    return jsonify({"stop_stations": stops})


# --------------------------------------------------------------------------- #
def register_api(app, context: AppContext, tokens: TokenManager) -> None:
    """把路由注册到 Flask 应用，并注入依赖。"""
    global _CTX, _TOKENS, _GUARD
    _CTX = context
    _TOKENS = tokens
    _GUARD = require_auth(tokens)
    app.register_blueprint(api)
