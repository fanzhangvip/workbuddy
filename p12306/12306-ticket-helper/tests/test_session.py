"""会话持久化测试（AC-03 的「重启后省略重新扫码」部分）。

三件事必须成立：
1. 会话**加密**落盘（不能是明文 Cookie）；
2. 磁盘上的会话**不可信** —— 恢复时必须真验证一次，验证不过就丢弃；
3. 恢复成功等价于「已登录」，恢复失败要能干净地退回正常登录流程。
"""

from __future__ import annotations

import json
import time

import pytest

from ticket_helper.account.session import SessionStore, filter_cookies
from ticket_helper.config.crypto import SecretCipher
from ticket_helper.config.settings import UserConfig
from ticket_helper.inter.api import TicketApi
from ticket_helper.inter.base import HttpClient

COOKIES = [
    {"name": "JSESSIONID", "value": "abc123", "domain": "127.0.0.1", "path": "/"},
    {"name": "tk", "value": "ticket-value", "domain": "127.0.0.1", "path": "/"},
    {"name": "_ga", "value": "tracking", "domain": "127.0.0.1", "path": "/"},
]


@pytest.fixture()
def store(app_paths, cipher):
    app_paths.ensure_dirs()
    return SessionStore(app_paths.session_dir, cipher, base_url="https://kyfw.12306.cn")


# --------------------------------------------------------------------------- #
# 存储层
# --------------------------------------------------------------------------- #
def test_save_load_roundtrip(store, app_paths):
    assert store.save("樊章", COOKIES) is True
    loaded = store.load("樊章")
    # **全部 Cookie 都要存下来**，不做筛选 —— 见下面 test_save_does_not_filter_cookies 的原因
    assert {c["name"] for c in loaded} == {"JSESSIONID", "tk", "_ga"}


def test_session_file_is_not_plaintext(store, app_paths):
    """落盘的绝不能是明文 Cookie —— 它等价于登录凭据。"""
    store.save("u1", COOKIES)
    files = list(app_paths.session_dir.glob("session-*.enc"))
    assert len(files) == 1
    raw = files[0].read_text(encoding="utf-8")
    assert "abc123" not in raw
    assert "JSESSIONID" not in raw
    # 文件内容必须是可解密的密文，而不是碰巧被改名的 JSON
    payload = json.loads(SecretCipher(app_paths.secret_key_path).decrypt(raw))
    assert payload["username"] == "u1"


def test_filename_is_safe_for_non_ascii_username(store, app_paths):
    """中文账号名不能直接进文件名。"""
    store.save("易健", COOKIES)
    names = [p.name for p in app_paths.session_dir.glob("*")]
    assert all(n.startswith("session-") and n.endswith(".enc") for n in names)
    assert all("易" not in n for n in names)


def test_expired_session_is_ignored_and_removed(store, app_paths):
    store.save("u1", COOKIES)
    path = next(app_paths.session_dir.glob("session-*.enc"))
    # 把保存时间改成很久以前
    cipher = SecretCipher(app_paths.secret_key_path)
    payload = json.loads(cipher.decrypt(path.read_text(encoding="utf-8")))
    payload["saved_at"] = time.time() - 30 * 24 * 3600
    path.write_text(cipher.encrypt(json.dumps(payload, ensure_ascii=False)), encoding="utf-8")

    assert store.load("u1") is None
    assert not path.exists(), "过期会话应被清掉，不该每次启动都拿它去试"


def test_corrupted_session_is_cleared(store, app_paths):
    store.save("u1", COOKIES)
    path = next(app_paths.session_dir.glob("session-*.enc"))
    path.write_text("not-a-ciphertext", encoding="utf-8")
    assert store.load("u1") is None
    assert not path.exists()


def test_session_bound_to_base_url(store):
    """换了服务端（例如切到 mock）不能复用旧会话。"""
    store.save("u1", COOKIES)
    other = SessionStore(store.directory, store.cipher, base_url="http://127.0.0.1:18006")
    assert other.load("u1") is None
    assert store.load("u1") is not None, "不匹配时不该删掉原会话"


def test_save_does_not_filter_cookies(store):
    """**回归**：保存会话时不做任何裁剪。

    曾经按「名字看起来相关」过滤成 5 个 Cookie（JSESSIONID/tk/BIGipServer*/uamtk），
    真机验证时恢复出来的会话 ``checkUser`` 返回 ``flag: false``，即**恢复失败**。
    无法确证是「少了某个 Cookie」还是「会话本就过期」，但少存是确定的损失、
    多存是零成本 —— 所以改为全量保存。这条断言用来防止有人再「优化」回去。
    """
    cookies = [
        {"name": "JSESSIONID", "value": "a", "domain": "kyfw.12306.cn", "path": "/"},
        {"name": "RAIL_DEVICEID", "value": "b", "domain": "kyfw.12306.cn", "path": "/"},
        {"name": "_uab_collina", "value": "c", "domain": "kyfw.12306.cn", "path": "/"},
        {"name": "some_future_cookie", "value": "d", "domain": "kyfw.12306.cn", "path": "/"},
    ]
    assert store.save("u1", cookies) is True
    saved = {c["name"] for c in store.load("u1")}
    assert saved == {"JSESSIONID", "RAIL_DEVICEID", "_uab_collina", "some_future_cookie"}


def test_save_rejects_empty_cookie_jar(store, caplog):
    """一条 Cookie 都没有时要明确警告「重启后需重新扫码」，而不是静默成功。"""
    assert store.save("u1", []) is False
    assert not store.exists("u1")


def test_filter_cookies_helper_is_diagnostic_only():
    """``filter_cookies`` 现在只用于排查（看会话里有没有关键 Cookie），不再参与保存。"""
    kept = {c["name"] for c in filter_cookies(COOKIES)}
    assert kept == {"JSESSIONID", "tk"}


# --------------------------------------------------------------------------- #
# checkUser 的假阳性（回归）
# --------------------------------------------------------------------------- #
def test_check_user_rejects_html_instead_of_false_positive(app_context, mock_server):
    """官方用「HTTP 200 + 登录页 HTML」表示会话不可用。

    以前这里是 ``payload is None → return response.ok``，而 ``response.ok`` 对
    HTTP 200 恒为 True，于是失效会话会被判成「还活着」：账号显示在线，
    但之后每个业务请求都被拒 —— 使用者只会看到「查询一直失败」，
    完全想不到是登录态早就没了。
    """
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=0), app_context.station_map)
    assert api.check_user() is True

    # 直接把「未登录」这一分支钉住：mock 在 risk 场景下 checkUser 返回 flag=False
    mock_server.set_scenario("risk")
    assert api.check_user() is False


# --------------------------------------------------------------------------- #
# User 层：恢复
# --------------------------------------------------------------------------- #
def _make_user(app_context, username: str = "qr_user"):
    return app_context.accounts.build_user(UserConfig(username=username, password="", heartbeat=120, enabled=True))


def test_restore_session_success_skips_login(app_context, mock_server, store):
    """磁盘上有有效会话时，扫码账号也能「免扫码」恢复在线。"""
    app_context.accounts.session_store = store
    user = _make_user(app_context)
    user.client.import_cookies(COOKIES)
    assert user.save_session() is True

    fresh = _make_user(app_context)                 # 模拟「重启后的新对象」
    fresh.client.clear_cookies()
    assert fresh.login() is True, "有本地会话时应直接恢复"
    assert fresh.is_login is True
    assert fresh.status == "online"
    # 证明没有走密码/扫码流程：mock 上不该出现 web_login 的调用记录
    assert not any(r["path"] == "/passport/web/login" for r in mock_server.state.requests)


def test_restore_session_failure_clears_and_falls_back(app_context, mock_server, store):
    """验证不过时必须丢弃并回到正常流程，不能留下「假在线」。"""
    app_context.accounts.session_store = store
    user = _make_user(app_context)
    user.client.import_cookies(COOKIES)
    user.save_session()

    mock_server.set_scenario("risk")                # checkUser → flag False
    fresh = _make_user(app_context)
    assert fresh.restore_session() is False
    assert fresh.is_login is False
    assert store.exists("qr_user") is False, "失效会话必须清掉"
    # 无密码账号走到这里应该给出「需要重新扫码」的结论，而不是假装成功
    assert fresh.login() is False
    assert "扫码" in fresh.stats.last_error


def test_restore_session_without_saved_session_is_noop(app_context, mock_server, store):
    app_context.accounts.session_store = store
    user = _make_user(app_context)
    assert user.restore_session() is False
    assert user.is_login is False


def test_force_login_does_not_reuse_saved_session(app_context, mock_server, store):
    """force=True 表示「明确要重新登录」，不该被本地会话拦住。"""
    app_context.accounts.session_store = store
    user = _make_user(app_context)
    user.client.import_cookies(COOKIES)
    user.save_session()

    called: list[bool] = []
    import ticket_helper.account.manager as manager_mod

    original = manager_mod.User.restore_session

    def _spy(self):  # noqa: ANN001
        called.append(True)
        return original(self)

    manager_mod.User.restore_session = _spy
    try:
        user.login(force=True)
    finally:
        manager_mod.User.restore_session = original
    assert not called, "force=True 时不应尝试复用本地会话"


def test_remove_user_clears_saved_session(app_context, mock_server, store):
    """账号都删了，不该再留一份登录凭据在磁盘上。"""
    app_context.accounts.session_store = store
    app_context.upsert_user(UserConfig(username="byebye", password="pw", heartbeat=120, enabled=True))
    user = app_context.accounts.get("byebye")
    user.client.import_cookies(COOKIES)
    user.save_session()
    assert store.exists("byebye") is True

    app_context.remove_user("byebye")
    assert store.exists("byebye") is False


def test_to_dict_reports_session_saved(app_context, mock_server, store):
    app_context.accounts.session_store = store
    app_context.upsert_user(UserConfig(username="marked", password="pw", heartbeat=120, enabled=True))
    user = app_context.accounts.get("marked")
    assert user.to_dict()["session_saved"] is False
    user.client.import_cookies(COOKIES)
    user.save_session()
    assert user.to_dict()["session_saved"] is True


def test_qr_adopt_saves_session(app_context, mock_server, store):
    """扫码成功那一刻就要留存会话，否则一关程序又得重扫。"""
    app_context.accounts.session_store = store
    temp = app_context.accounts.build_user(UserConfig(username="tmp", password="", heartbeat=120, enabled=True))
    temp.client.import_cookies(COOKIES)

    adopted = app_context.accounts.adopt_qr_user(temp, "fresh_qr_user")
    assert store.exists("fresh_qr_user") is True
    assert adopted.is_login is True
