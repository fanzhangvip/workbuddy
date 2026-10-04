"""Web API 接口测试（WB-T1 ~ WB-T6 / UI-09 双端同源）。"""

from __future__ import annotations

import json
import time

import pytest

from ticket_helper.web.app import create_app
from ticket_helper.web.auth import TokenManager


@pytest.fixture()
def web(app_context, mock_server):
    # 添加账号会在后台自动登录，而登录要过验证码。
    # 不注入固定识别器的话，验证码会走「人工兜底」等满超时，
    # 测试既慢又不确定（这也是默认配置下真实登录的必经环节）。
    from ticket_helper.verify.service import CallbackSolver

    app_context.verify_service.set_solver(CallbackSolver(lambda i, h, k: "112,45", name="fixed"))
    app = create_app(app_context, start_background=False)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client, app_context


def _login(client, password="test-pass") -> str:
    response = client.post("/api/auth/login", json={"user": "admin", "pass": password})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# WB-T1 / WB-T2 鉴权
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/users"),
        ("get", "/api/tasks"),
        ("get", "/api/stats"),
        ("get", "/api/orders"),
        ("get", "/api/config"),
        ("get", "/api/logs"),
        ("get", "/api/cluster"),
    ],
)
def test_protected_endpoints_require_auth(web, method, path):
    client, _ = web
    response = getattr(client, method)(path)
    assert response.status_code == 401
    assert response.get_json()["code"] == "unauthorized"


def test_login_rejects_wrong_password(web):
    client, _ = web
    response = client.post("/api/auth/login", json={"user": "admin", "pass": "wrong"})
    assert response.status_code == 401


def test_login_requires_both_fields(web):
    client, _ = web
    response = client.post("/api/auth/login", json={"user": "admin"})
    assert response.status_code == 400
    assert response.get_json()["code"] == "validation_error"


def test_login_throttles_brute_force(web):
    client, _ = web
    last = None
    for _ in range(12):
        last = client.post("/api/auth/login", json={"user": "admin", "pass": "guess"})
    assert last.status_code == 429
    assert last.get_json()["code"] == "too_many_attempts"


def test_login_and_me(web):
    client, _ = web
    token = _login(client)
    response = client.get("/api/auth/me", headers=_auth(token))
    assert response.status_code == 200
    assert response.get_json()["user"] == "admin"


def test_invalid_token_rejected(web):
    client, _ = web
    response = client.get("/api/users", headers=_auth("garbage.token"))
    assert response.status_code == 401


def test_logout_revokes_token(web):
    client, _ = web
    token = _login(client)
    assert client.get("/api/users", headers=_auth(token)).status_code == 200
    assert client.post("/api/auth/logout", headers=_auth(token)).status_code == 200
    assert client.get("/api/users", headers=_auth(token)).status_code == 401


def test_token_manager_expiry():
    """令牌过期后必须失效（用冻结时间验证，避免真等 5 分钟）。"""
    from freezegun import freeze_time

    from ticket_helper.errors import AuthError

    with freeze_time("2026-01-01 00:00:00"):
        manager = TokenManager("s3cret", ttl=300)
        token = manager.issue("admin")
        assert manager.verify(token)["u"] == "admin"

    with freeze_time("2026-01-01 00:06:00"):
        with pytest.raises(AuthError):
            manager.verify(token)


def test_token_signature_tamper_detected():
    from ticket_helper.errors import AuthError

    manager = TokenManager("s3cret")
    token = manager.issue("admin")
    payload, _, signature = token.partition(".")
    tampered = f"{payload}.{'a' * len(signature)}"
    with pytest.raises(AuthError):
        manager.verify(tampered)


# --------------------------------------------------------------------------- #
# 健康检查（无鉴权）
# --------------------------------------------------------------------------- #
def test_health_and_ready(web):
    client, _ = web
    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["status"] == "ok"

    ready = client.get("/ready")
    assert ready.status_code in (200, 503)


def test_internal_ping_is_local_only(web):
    """内部接口必须限制来源，避免变成「写入任意账号」的后门。"""
    client, _ = web
    assert client.get("/api/internal/ping").status_code == 200

    response = client.get("/api/internal/ping", environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
    assert response.status_code == 403
    assert response.get_json()["code"] == "forbidden"


def test_internal_seed_user_encrypts_password(web):
    client, ctx = web
    response = client.post(
        "/api/internal/seed-user",
        json={"username": "seed_user", "password": "plain-secret"},
    )
    assert response.status_code == 201

    row = ctx.db.get_user_by_name("seed_user", with_secret=True)
    assert row is not None
    assert "plain-secret" not in row["password_enc"]
    assert ctx.cipher.decrypt(row["password_enc"]) == "plain-secret"

    # 通过 API 读取时不应返回密码
    token = _login(client)
    users = client.get("/api/users", headers=_auth(token)).get_json()
    assert all("password_enc" not in u for u in users["users"])


# --------------------------------------------------------------------------- #
# WB-T3 账号 CRUD
# --------------------------------------------------------------------------- #
def test_users_crud(web):
    client, ctx = web
    token = _login(client)

    created = client.post(
        "/api/users", json={"username": "api_user", "password": "pw", "heartbeat": 90}, headers=_auth(token)
    )
    assert created.status_code == 201
    user_id = created.get_json()["id"]

    listing = client.get("/api/users", headers=_auth(token)).get_json()
    assert any(u["username"] == "api_user" for u in listing["users"])
    assert listing["summary"]["total"] >= 1

    # 更新心跳
    assert client.put(f"/api/users/{user_id}", json={"heartbeat": 150}, headers=_auth(token)).status_code == 200
    assert ctx.accounts.get("api_user").heartbeat_interval == 150

    # 启停
    toggled = client.put(f"/api/users/{user_id}/state", json={"enabled": False}, headers=_auth(token))
    assert toggled.get_json()["enabled"] is False
    assert ctx.accounts.get("api_user").enabled is False

    # 删除
    assert client.delete(f"/api/users/{user_id}", headers=_auth(token)).status_code == 200
    assert ctx.accounts.get("api_user") is None


def test_update_missing_user_returns_404(web):
    client, _ = web
    token = _login(client)
    assert client.put("/api/users/9999", json={"heartbeat": 100}, headers=_auth(token)).status_code == 404
    assert client.delete("/api/users/9999", headers=_auth(token)).status_code == 404


def test_create_user_requires_username(web):
    client, _ = web
    token = _login(client)
    response = client.post("/api/users", json={"password": "x"}, headers=_auth(token))
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# 回归：添加账号后必须真的去登录（AC-02）
# --------------------------------------------------------------------------- #
def _wait_until(predicate, timeout: float = 20.0, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def test_adding_account_triggers_login(web, mock_server):
    """回归：保存账号只会写库、**从不发起登录**，导致界面永远显示「离线」。

    这是「添加了账号但还是没登录」的直接原因：账号在启动之后才添加，
    而启动时的批量登录只覆盖当时已存在的账号。
    """
    client, ctx = web
    token = _login(client)

    response = client.post(
        "/api/users", json={"username": "auto_login", "password": "pw123"}, headers=_auth(token)
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["login_queued"] is True, "保存账号后应当自动发起登录"

    def _online() -> bool:
        user = ctx.accounts.get("auto_login")
        return bool(user and user.is_login)

    assert _wait_until(_online), "账号应在后台自动登录成功"
    # 确认真的打到 12306（而不是只在本地改了状态）
    paths = [r["path"] for r in mock_server.state.requests]
    assert "/passport/web/login" in paths


def test_adding_disabled_account_does_not_login(web, mock_server):
    """禁用状态的账号不应触发登录，否则会让使用者以为「怎么偷偷在登录」。"""
    client, ctx = web
    token = _login(client)
    body = client.post(
        "/api/users",
        json={"username": "disabled_one", "password": "pw", "enabled": False},
        headers=_auth(token),
    ).get_json()
    assert body["login_queued"] is False
    assert "禁用" in body["message"]
    time.sleep(0.5)
    assert ctx.accounts.get("disabled_one").is_login is False


def test_adding_qr_account_without_password_asks_to_scan(web, mock_server):
    """没有密码的账号（扫码登录创建）不该去跑密码登录，要直接告诉用户去扫码。"""
    client, ctx = web
    token = _login(client)
    body = client.post("/api/users", json={"username": "no_pass"}, headers=_auth(token)).get_json()
    assert body["login_queued"] is False
    assert "扫码" in body["message"]


def test_updating_password_relogs_in(web, mock_server):
    """改了密码必须重新登录，否则会一直沿用旧凭据的会话。"""
    client, ctx = web
    token = _login(client)
    user_id = client.post(
        "/api/users", json={"username": "pw_change", "password": "old"}, headers=_auth(token)
    ).get_json()["id"]
    assert _wait_until(lambda: ctx.accounts.get("pw_change").is_login)

    ctx.accounts.get("pw_change").is_login = False  # 模拟会话已失效
    response = client.put(f"/api/users/{user_id}", json={"password": "new"}, headers=_auth(token))
    assert response.get_json()["login_queued"] is True
    assert _wait_until(lambda: ctx.accounts.get("pw_change").is_login)


def test_login_endpoint_reports_progress_instead_of_vague_ok(web):
    """「测试登录」要给出可读的进度说明，而不是一句没有信息量的成功。"""
    client, ctx = web
    token = _login(client)
    user_id = client.post(
        "/api/users", json={"username": "manual_login", "password": "pw"}, headers=_auth(token)
    ).get_json()["id"]

    # 添加账号时已经在自动登录了，此时再点一次「测试登录」不应叠起第二个登录线程
    first = client.post(f"/api/users/{user_id}/login", headers=_auth(token))
    assert first.status_code == 200
    assert first.get_json()["message"]
    if first.get_json()["login_queued"] is False:
        assert "正在进行" in first.get_json()["message"]

    # 等第一次登录结束（在线或失败），再手动触发一次应能正常提交
    _wait_until(lambda: not ctx._login_inflight)  # noqa: SLF001
    ctx.accounts.get("manual_login").is_login = False
    again = client.post(f"/api/users/{user_id}/login", headers=_auth(token))
    assert again.get_json()["login_queued"] is True
    assert again.get_json()["message"]


# --------------------------------------------------------------------------- #
# WB-T3 / WB-T4 任务 CRUD 与启停
# --------------------------------------------------------------------------- #
TASK_PAYLOAD = {
    "name": "API 任务",
    "left_dates": "2026-10-01,2026-10-02",
    "from_stations": "广州",
    "to_stations": "长沙",
    "trains_whitelist": "G6102",
    "seat_types": "二等座,一等座",
    "passengers": "张三",
    "strategy": "direct",
    "query_interval": 1.0,
}


def test_tasks_crud_and_toggle(web):
    client, ctx = web
    token = _login(client)

    created = client.post("/api/tasks", json=TASK_PAYLOAD, headers=_auth(token))
    assert created.status_code == 201
    task_id = created.get_json()["id"]

    listing = client.get("/api/tasks", headers=_auth(token)).get_json()
    task = next(t for t in listing["tasks"] if t["id"] == task_id)
    assert task["left_dates"] == ["2026-10-01", "2026-10-02"]   # 逗号串被正确解析
    assert task["seat_types"] == ["二等座", "一等座"]

    # 编辑
    updated = client.put(f"/api/tasks/{task_id}", json={**TASK_PAYLOAD, "name": "改名了"}, headers=_auth(token))
    assert updated.status_code == 200
    detail = client.get(f"/api/tasks/{task_id}", headers=_auth(token)).get_json()
    assert detail["task"]["name"] == "改名了"

    # 启动（任务线程会自行跑；这里主要验证端点与状态迁移）
    started = client.post(f"/api/tasks/{task_id}/start", headers=_auth(token))
    assert started.status_code == 200
    assert started.get_json()["status"]["running"] is True

    status = client.get(f"/api/tasks/{task_id}/status", headers=_auth(token)).get_json()
    assert status["task_id"] == task_id

    remain = client.get(f"/api/tasks/{task_id}/remain", headers=_auth(token)).get_json()
    assert "candidates" in remain

    stopped = client.post(f"/api/tasks/{task_id}/stop", headers=_auth(token))
    assert stopped.get_json()["status"]["running"] is False

    assert client.delete(f"/api/tasks/{task_id}", headers=_auth(token)).status_code == 200
    assert client.get(f"/api/tasks/{task_id}", headers=_auth(token)).status_code == 404


def test_create_task_with_invalid_payload_returns_400(web):
    client, _ = web
    token = _login(client)
    bad = {**TASK_PAYLOAD, "left_dates": "2026/10/01"}
    response = client.post("/api/tasks", json=bad, headers=_auth(token))
    assert response.status_code == 400
    assert "日期格式非法" in response.get_json()["message"]


def test_tasks_status_all(web):
    client, _ = web
    token = _login(client)
    payload = client.get("/api/tasks/status/all", headers=_auth(token)).get_json()
    assert "tasks" in payload and "summary" in payload


# --------------------------------------------------------------------------- #
# WB-T4 订单 / 统计 / 集群
# --------------------------------------------------------------------------- #
def test_orders_endpoint(web):
    client, ctx = web
    token = _login(client)
    ctx.db.save_order({"task_id": 1, "order_no": "E1", "train_no": "G1", "status": "success", "depart_date": "2026-10-01"})
    payload = client.get("/api/orders?status=success", headers=_auth(token)).get_json()
    assert len(payload["orders"]) == 1
    assert payload["orders"][0]["order_no"] == "E1"


def test_stats_and_snapshot(web):
    client, _ = web
    token = _login(client)
    stats = client.get("/api/stats", headers=_auth(token)).get_json()
    assert "storage" in stats and "runtime" in stats
    assert "queries_total" in stats["storage"]

    snapshot = client.get("/api/system/snapshot", headers=_auth(token)).get_json()
    for key in ("accounts", "scheduler", "rate_limit", "codec", "notify", "cluster"):
        assert key in snapshot


def test_cluster_endpoint(web):
    client, _ = web
    token = _login(client)
    payload = client.get("/api/cluster", headers=_auth(token)).get_json()
    assert payload["enabled"] is False
    assert payload["role"] == "master"


# --------------------------------------------------------------------------- #
# WB-T5 实时日志 / WB-T6 配置热更新
# --------------------------------------------------------------------------- #
def test_logs_endpoints(web):
    client, _ = web
    token = _login(client)
    import logging

    logging.getLogger("test.web").warning("这是一条测试日志")
    payload = client.get("/api/logs?limit=50", headers=_auth(token)).get_json()
    assert any("测试日志" in entry["message"] for entry in payload["logs"])

    filtered = client.get("/api/logs?level=WARNING&limit=50", headers=_auth(token)).get_json()
    assert all(entry["level"] == "WARNING" for entry in filtered["logs"])


def test_config_get_and_hot_update(web):
    client, ctx = web
    token = _login(client)

    config = client.get("/api/config", headers=_auth(token)).get_json()
    assert config["config"]["web"]["auth_pass"] == "********"   # 敏感字段必须脱敏
    assert set(config["options"]["seat_types"])
    before_version = config["version"]

    updated = client.put(
        "/api/config/ratelimit",
        json={"min_interval": 1.5, "max_interval": 6.0, "adaptive": True},
        headers=_auth(token),
    )
    assert updated.status_code == 200
    assert updated.get_json()["version"] > before_version
    assert ctx.config.ratelimit.min_interval == 1.5
    assert ctx.rate_limiter.snapshot()["min_interval"] == 1.5   # 热更新已下发到限速器

    history = client.get("/api/config/history", headers=_auth(token)).get_json()
    assert any(h["key"] == "ratelimit" for h in history["history"])


def test_config_update_rejects_invalid_values(web):
    client, _ = web
    token = _login(client)
    response = client.put("/api/config/ratelimit", json={"min_interval": -5}, headers=_auth(token))
    assert response.status_code == 500
    assert response.get_json()["code"] == "config_error"


def test_notify_config_masks_secrets(web):
    client, ctx = web
    token = _login(client)
    ctx.config.notify.email.password = "super-secret"
    payload = client.get("/api/config", headers=_auth(token)).get_json()
    assert payload["config"]["notify"]["email"]["password"] == "********"


def test_notifications_and_test_channel(web):
    client, _ = web
    token = _login(client)
    payload = client.get("/api/notifications", headers=_auth(token)).get_json()
    assert "notifications" in payload
    assert "channels" in payload

    result = client.post("/api/notify/test", json={"channel": "web"}, headers=_auth(token)).get_json()
    assert result["ok"] is True


# --------------------------------------------------------------------------- #
# 扫码登录（AC-08）
# --------------------------------------------------------------------------- #
def test_qr_login_endpoints_require_auth(web):
    client, _ = web
    assert client.post("/api/users/qr/start").status_code == 401
    assert client.get("/api/users/qr/status?uuid=x").status_code == 401
    assert client.post("/api/users/qr/cancel", json={"uuid": "x"}).status_code == 401


def test_qr_start_returns_image_and_uuid(web):
    client, _ = web
    token = _login(client)

    response = client.post("/api/users/qr/start", headers=_auth(token), json={})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["uuid"]
    assert payload["image"]                      # 前端要拼 data:image/png;base64,
    assert payload["state"] == "waiting"
    # 真机实测官方二维码只有约 20 秒寿命，本地兜底 TTL 取 25 秒；
    # 传一个「明显不可能」的下限，避免把数值写死后又得改测试。
    assert 5 <= payload["expires_in"] <= 60
    assert payload["remaining"] > 0
    assert payload["done"] is False
    assert payload["session_id"], "必须返回稳定的会话标识（换码后仍用它轮询）"
    assert payload["qr_uuid"] == payload["uuid"] or payload["qr_uuid"]


def test_qr_start_clamps_ttl(web):
    """ttl 必须被收敛：传 0 会让二维码立刻失效，传超大值会让会话永不回收。"""
    client, _ = web
    token = _login(client)

    # 传 0 会被收敛到 5 秒（而不是被当成「没传」走默认值，那是另一个坑）
    too_small = client.post("/api/users/qr/start", headers=_auth(token), json={"ttl": 0}).get_json()
    assert too_small["expires_in"] == 5

    too_big = client.post("/api/users/qr/start", headers=_auth(token), json={"ttl": 999999}).get_json()
    assert too_big["expires_in"] == 600

    garbage = client.post("/api/users/qr/start", headers=_auth(token), json={"ttl": "abc"}).get_json()
    assert garbage["expires_in"] > 0


def test_qr_status_flow_and_account_creation(web):
    client, ctx = web
    token = _login(client)

    started = client.post("/api/users/qr/start", headers=_auth(token), json={}).get_json()
    uuid = started["uuid"]

    # 轮询到确认（mock 默认节奏最多 5 次）
    payload = None
    for _ in range(10):
        payload = client.get(f"/api/users/qr/status?uuid={uuid}", headers=_auth(token)).get_json()
        if payload["done"]:
            break
        time.sleep(0.05)

    assert payload["state"] == "confirmed"
    assert payload["succeeded"] is True
    assert payload["username"]
    assert "登录成功" in payload["message"]

    # 账号应已出现在账号列表里，且标记为扫码登录（无密码）
    users = client.get("/api/users", headers=_auth(token)).get_json()["users"]
    created = next((u for u in users if u["username"] == payload["username"]), None)
    assert created is not None
    assert created["has_password"] is False
    assert created["login_type"] == "qr"
    assert created["status"] == "online"


def test_qr_status_requires_uuid(web):
    client, _ = web
    token = _login(client)
    response = client.get("/api/users/qr/status", headers=_auth(token))
    assert response.status_code == 400
    assert "uuid" in response.get_json()["message"]


def test_qr_status_unknown_uuid_returns_404(web):
    """会话不存在要给出明确 404，前端据此提示「重新获取二维码」。"""
    client, _ = web
    token = _login(client)
    response = client.get("/api/users/qr/status?uuid=does-not-exist", headers=_auth(token))
    assert response.status_code == 404
    assert "重新" in response.get_json()["message"]


def test_qr_cancel_stops_session(web):
    client, _ = web
    token = _login(client)

    started = client.post("/api/users/qr/start", headers=_auth(token), json={}).get_json()
    uuid = started["uuid"]

    cancelled = client.post("/api/users/qr/cancel", headers=_auth(token), json={"uuid": uuid})
    assert cancelled.status_code == 200
    assert cancelled.get_json()["ok"] is True

    status = client.get(f"/api/users/qr/status?uuid={uuid}", headers=_auth(token)).get_json()
    assert status["state"] == "expired"


def test_qr_cancel_requires_uuid(web):
    client, _ = web
    token = _login(client)
    response = client.post("/api/users/qr/cancel", headers=_auth(token), json={})
    assert response.status_code == 400


def test_qr_cancel_unknown_uuid_is_not_an_error(web):
    """重复取消（例如两个标签页同时关闭）不该报错。"""
    client, _ = web
    token = _login(client)
    response = client.post("/api/users/qr/cancel", headers=_auth(token), json={"uuid": "nope"})
    assert response.status_code == 200
    assert response.get_json()["ok"] is False


def test_manual_login_rejects_qr_account_with_clear_message(web):
    """扫码账号点「测试登录」必须给出可操作的原因，而不是含糊的失败。"""
    client, ctx = web
    token = _login(client)

    started = client.post("/api/users/qr/start", headers=_auth(token), json={}).get_json()
    session = ctx.accounts.get_qr_session(started["uuid"])
    assert session is not None
    # 直接把会话推进到「已确认」，避免依赖轮询次数
    session.status.state = "confirmed"
    session._complete("mock-uamtk")  # noqa: SLF001

    users = client.get("/api/users", headers=_auth(token)).get_json()["users"]
    qr_user = next(u for u in users if u["login_type"] == "qr")

    response = client.post(f"/api/users/{qr_user['id']}/login", headers=_auth(token))
    assert response.status_code == 400
    assert "扫码登录" in response.get_json()["message"]


def test_users_response_exposes_login_type_without_password(web):
    """账号列表要能区分登录方式，但绝不能泄露密码本身。"""
    client, ctx = web
    token = _login(client)
    client.post("/api/users", json={"username": "pw_user", "password": "secret"}, headers=_auth(token))

    users = client.get("/api/users", headers=_auth(token)).get_json()["users"]
    pw_user = next(u for u in users if u["username"] == "pw_user")
    assert pw_user["has_password"] is True
    assert pw_user["login_type"] == "password"
    for leaked in ("password", "password_enc"):
        assert leaked not in pw_user
    assert "secret" not in json.dumps(users)


# --------------------------------------------------------------------------- #
# 其他端点
# --------------------------------------------------------------------------- #
def test_stations_endpoint(web):
    client, _ = web
    token = _login(client)
    payload = client.get("/api/stations?kw=广州", headers=_auth(token)).get_json()
    assert "广州南" in payload["stations"]
    # items 带上电报码：前端据此确认「选到的是官方那个站」，而不是一个同名的错站
    assert payload["items"][0] == {"name": "广州", "code": "GZQ"}, payload["items"][:3]
    assert all(item["code"] for item in payload["items"])
    assert payload["stations"] == [item["name"] for item in payload["items"]]


def test_stations_endpoint_returns_hot_stations_without_keyword(web):
    """空关键字要回常用站 —— 站点选择器点开即见，不用先猜一个关键字。"""
    client, _ = web
    token = _login(client)
    payload = client.get("/api/stations", headers=_auth(token)).get_json()
    assert payload["count"] > 10
    assert payload["items"][0]["name"] == "北京南", payload["items"][:3]


def test_stations_endpoint_reports_no_match(web):
    client, _ = web
    token = _login(client)
    payload = client.get("/api/stations?kw=火星站", headers=_auth(token)).get_json()
    assert payload["count"] == 0 and payload["items"] == []


def test_captcha_endpoints(web):
    client, _ = web
    token = _login(client)
    assert client.get("/api/captcha/pending", headers=_auth(token)).get_json()["tasks"] == []
    assert client.post("/api/captcha/submit", json={"id": "x", "answer": "1"}, headers=_auth(token)).status_code == 200


def test_unknown_route_returns_json_404(web):
    client, _ = web
    response = client.get("/api/not-exist")
    assert response.status_code == 404
    assert response.get_json()["code"] == "not_found"


def test_security_headers_present(web):
    client, _ = web
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in response.headers
    assert response.headers.get("X-Request-Id")


def test_index_page_served(web):
    client, _ = web
    response = client.get("/")
    assert response.status_code == 200
    assert b"12306" in response.data


def test_users_list_carries_verify_state_and_hint(web):
    """核验状态与「下一步做什么」由后端统一下发，前端不再自己拼一套。

    两端（网页 / 桌面）+ 候补引擎必须口径一致，否则会出现
    「界面写着无法判定、候补却按未核验拦掉」这种自相矛盾。

    ⚠️ 这里**不要断言具体是哪种状态**：新建账号会触发后台登录，
    而登录成功就会顺手检测一次核验状态 —— 断言「未检测」会变成
    「跑得快就过、跑得慢就红」的偶发失败。要钉的是**映射关系**。
    """
    from ticket_helper.account import verify

    client, ctx = web
    token = _login(client)
    client.post("/api/users", json={"username": "verify_user", "password": "pw"}, headers=_auth(token))

    listing = client.get("/api/users", headers=_auth(token)).get_json()
    row = next(u for u in listing["users"] if u["username"] == "verify_user")
    for key in ("verify_state", "verify_label", "verify_hint"):
        assert key in row, f"账号列表要带上 {key}"

    assert row["verify_state"] in verify.LABEL, f"未知状态：{row['verify_state']}"
    # 标签与「下一步做什么」必须与状态匹配（这就是「同一份语义」的含义）
    assert row["verify_label"] == verify.LABEL[row["verify_state"]]
    assert row["verify_hint"] == verify.HINT[row["verify_state"]]
    # 后端算出来的状态必须与三个布尔量自洽
    assert row["verify_state"] == verify.state_of(row)


def test_refresh_verify_reports_unknown_without_claiming_unverified(web):
    """官方不返回核验字段时，接口必须回「无法判定」，而不是「未核验」。

    真机实测官方两个用户信息接口都不返回核验字段，
    把这种情况说成「未核验」会让人以为账号被 12306 限制了。
    """
    client, ctx = web
    token = _login(client)
    # ⚠️ **刻意不带密码**：`POST /api/users` 保存后会自动排队登录（AC-02），
    # 而那是**后台线程**——它可能在下面任何一行之后才跑完，把 `is_login` 又置回 True。
    # 于是「账号当前不在线」这个前置条件就成了竞态：
    # 单独跑 8/8 绿，与另两个测试文件同跑时偶发红，而失败信息指向一个**不存在**的问题
    # （「接口没拦离线账号」）。
    # 无密码账号（= 扫码登录那种）在 `_auto_login_after_save` 里直接返回，不排队登录，
    # 所以离线状态是**确定的**。前置条件必须由构造方式保证，不能靠时序巧合。
    created = client.post("/api/users", json={"username": "v2"}, headers=_auth(token))
    user_id = created.get_json()["id"]
    assert created.get_json()["login_queued"] is False, "无密码账号不该排队登录"

    user = ctx.accounts.get("v2")
    assert getattr(user, "username", "") == "v2", "创建后账号应立即加载到运行时"
    assert user.is_login is False, "无密码账号必须处于「未登录」"

    # 账号不在线时不能瞎测 —— 明确指出先登录
    offline = client.post(f"/api/users/{user_id}/verify/refresh", headers=_auth(token))
    assert offline.status_code == 400
    assert "登录" in offline.get_json()["message"]

    user.is_login = True  # 伪造在线，只测「检测结果如何如实播报」
    user.api.get_user_info = lambda: {"user_name": "张三", "sexCode": "M"}  # type: ignore[method-assign]

    response = client.post(f"/api/users/{user_id}/verify/refresh", headers=_auth(token))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["verify_known"] is False
    assert payload["verify_state"] == "unknown"
    assert payload["verify_label"] == "无法判定"
    assert payload["can_standby"] is True, "判不出来就不能拦，要让官方去判"


# --------------------------------------------------------------------------- #
# 余票看板：没有候选时**必须说清是哪一步空的**
#
# 旧实现只回 ``candidates``（筛选之后的结果），于是「查了 84 趟车但席别没配」
# 和「一次都没查成功」在界面上长成同一句话「暂未命中可购席位」——
# 使用者完全不知道该改哪里。这一组把「原因」也钉成接口契约。
# --------------------------------------------------------------------------- #
def test_task_list_warns_about_dates_12306_cannot_query(web):
    """任务列表要标出「12306 现在查不到这些日期」，并说明为什么。

    防的是：任务日期填成了去年（或超出预售期），列表上却看不出任何异常，
    直到启动后才发现「一直没动静」——而那时候的真实原因还被报成了「风控」。

    这一条针对的是**库里已经有的**行（新写入的会被闸门拦住），
    所以用仓储层直接造数据，见下面的说明。
    """
    client, ctx = web
    token = _login(client)

    # ⚠️ 这里**不能走 API** 了：2026-09-30 起「过去的日期」会被 `TaskConfig.validate`
    # 直接拦下来（见 `test_saving_a_task_with_a_past_date_is_rejected`）。
    # 但**库里可能还留着以前存进去的行**，那正是这条提示要继续生效的场景 ——
    # 所以用仓储层直接落一条「历史遗留」的行来复现那一支。
    from ticket_helper.config.settings import TaskConfig
    from ticket_helper.storage.repository import TaskRepository

    legacy = TaskConfig(
        name="去年的任务",
        left_dates=["2025-10-03", "2026-10-01"],
        from_stations=["广州"],
        to_stations=["长沙"],
        seat_types=["二等座"],
    )
    task_id = TaskRepository(ctx.db).save(legacy).id

    listing = client.get("/api/tasks", headers=_auth(token)).get_json()
    task = next(t for t in listing["tasks"] if t["id"] == task_id)
    hints = {h["date"]: h for h in task["date_hints"]}
    assert "2025-10-03" in hints, f"过去的日期应当被标出来，实际 {task['date_hints']}"
    assert hints["2025-10-03"]["verdict"] == "past"
    assert "已经过去" in hints["2025-10-03"]["text"]
    assert "2026-10-01" not in hints, "可查的日期不该被标"

    # 可查窗口也要如实下发（界面据此显示「12306 可查 A ~ B」）
    assert listing["date_window"]["today"] and listing["date_window"]["last"]


def test_remain_explains_empty_candidates(web):
    """没有候选时，``empty_reason`` 必须指出真正的成因，而不是一句空话。"""
    client, ctx = web
    token = _login(client)

    # 引擎要有在线账号才会真的去查（没账号它只会空等，测不到余票链路）
    from ticket_helper.config.settings import UserConfig

    user = ctx.accounts.add_user(UserConfig(username="test_user", password="test_pass"))
    assert user.login() is True

    # 任务里只配「特等座」—— mock 默认返回的是 二等座/一等座/商务座/硬卧/硬座/无座，
    # 一个都不匹配 → 车次有票，候选却为空（正是要看的那一支）
    created = client.post(
        "/api/tasks",
        json={**TASK_PAYLOAD, "name": "席别配错", "seat_types": "特等座", "trains_whitelist": ""},
        headers=_auth(token),
    )
    task_id = created.get_json()["id"]
    client.post(f"/api/tasks/{task_id}/start", headers=_auth(token))
    time.sleep(2.5)

    remain = client.get(f"/api/tasks/{task_id}/remain", headers=_auth(token)).get_json()
    assert remain["candidates"] == []
    assert remain["tickets"], "查到的车次必须回给界面，否则界面只能显示一片空白"
    assert "席别" in remain["empty_reason"], remain["empty_reason"]
    # 「最近一轮」的统计也要有，界面才能显示"查到 N 个车次 / M 个席别有票"
    assert remain["last_query"]["train_count"] == len(remain["tickets"])
    assert remain["last_query"]["available_seat_count"] >= 1

    client.post(f"/api/tasks/{task_id}/stop", headers=_auth(token))


def test_remain_says_not_running_instead_of_blank(web):
    """任务没在跑时也要给下一步，而不是留一个空数组让界面自己猜。"""
    client, _ = web
    token = _login(client)
    created = client.post("/api/tasks", json={**TASK_PAYLOAD, "name": "没启动"}, headers=_auth(token))
    task_id = created.get_json()["id"]

    remain = client.get(f"/api/tasks/{task_id}/remain", headers=_auth(token)).get_json()
    assert remain["running"] is False
    assert "未在运行" in remain["empty_reason"], remain["empty_reason"]
