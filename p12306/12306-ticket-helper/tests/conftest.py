"""pytest 全局夹具。

关键设计：**所有测试都不触碰真实 12306**。
- ``mock_server``：在本地线程里跑一个 mock-12306，接口路径与字段与真实协议对齐；
- ``app_context``：把 TICKET_HELPER_HOME 指向临时目录，保证测试之间完全隔离，
  同时又能真实验证「配置落库 / 密钥文件 / SQLite 建表」等持久化行为。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mock_server.server import create_mock_app  # noqa: E402


#: 测试里的「今天」。
#:
#: 为什么必须冻结：乘车日期在真机上是**会过期**的（12306 只卖预售期内的票，
#: 过去的日期查不到、未开售的也查不到），而测试里到处写着 ``2026-10-01``。
#: 如果判定用的是**真实**今天，这套测试会在 2026-10-14 那天集体变红，
#: 失败信息还是「乘车日期已经过去」这种与它原本要验证的东西毫无关系的话。
#: 冻在这一天之后，测试永远稳定，同时新测试仍可显式传 ``now=`` 断言任意日期。
FROZEN_TODAY = date(2026, 9, 29)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> date:
    """把所有「今天是几号」冻住（唯一来源见 ``ticket_helper.train_date.today``）。"""
    import ticket_helper.train_date as train_date

    monkeypatch.setattr(train_date, "today", lambda: FROZEN_TODAY)
    return FROZEN_TODAY


# --------------------------------------------------------------------------- #
# 目录 / 环境隔离
# --------------------------------------------------------------------------- #
@pytest.fixture()
def temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每个测试独立的用户数据目录。"""
    home = tmp_path / "th-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TICKET_HELPER_HOME", str(home))
    # 清掉全局单例，避免上一个测试的对象/路径泄漏进来。
    # 尤其是 get_app_paths 的路径缓存：它一旦缓存了某个目录，
    # 之后即使 TICKET_HELPER_HOME 变了也可能继续沿用旧目录。
    import ticket_helper.config.loader as loader_mod
    import ticket_helper.inter.station as station_mod
    import ticket_helper.runtime.context as context_mod
    import ticket_helper.runtime.paths as paths_mod
    import ticket_helper.storage.database as db_mod

    loader_mod._loader = None  # noqa: SLF001
    context_mod._context = None  # noqa: SLF001
    station_mod._station_map = None  # noqa: SLF001
    db_mod._db = None  # noqa: SLF001
    paths_mod.reset_app_paths()
    return home


@pytest.fixture()
def app_paths(temp_home: Path):
    from ticket_helper.runtime.paths import get_app_paths

    return get_app_paths(temp_home, refresh=True)


@pytest.fixture()
def database(app_paths):
    from ticket_helper.storage.database import get_database

    return get_database(app_paths.db_path, refresh=True)


@pytest.fixture()
def cipher(app_paths):
    from ticket_helper.config.crypto import SecretCipher

    return SecretCipher(app_paths.secret_key_path)


# --------------------------------------------------------------------------- #
# mock 服务
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MockServerHandle:
    """运行中的 mock 服务句柄。"""

    def __init__(self, server, thread: threading.Thread, base_url: str, state) -> None:
        self.server = server
        self.thread = thread
        self.base_url = base_url
        self.state = state

    def set_scenario(self, scenario: str) -> None:
        self.state.set_scenario(scenario)

    # ---- 扫码登录（AC-08）---- #
    def set_qr_state(self, state: str, uuid: str = "") -> bool:
        """把扫码状态固定成指定值（waiting/scanned/confirmed/expired）。"""
        return self.state.set_qr_state(state, uuid)

    def set_qr_username(self, username: str) -> None:
        """指定扫码成功后服务端返回的账号名。"""
        with self.state.lock:
            self.state.qr_username = username

    def qr_poll_count(self, uuid: str = "") -> int:
        with self.state.lock:
            if uuid:
                return self.state.qr_polls.get(uuid, 0)
            return sum(self.state.qr_polls.values())

    def reset(self) -> None:
        with self.state.lock:
            self.state.order_seq = 100000
            self.state.queue_polls.clear()
            self.state.issued_orders.clear()
            self.state.standby_orders.clear()
            self.state.requests.clear()
            self.state.qr_polls.clear()
            self.state.qr_override.clear()
            self.state.qr_default_state = ""
            # 登录态一起复位：个人中心那几个接口都要求登录，
            # 上一个测试残留的 current_login_user 会把「未登录」用例静默变成「已登录」。
            self.state.logged_in_users.clear()
            self.state.current_login_user = ""

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)


@pytest.fixture(scope="function")
def mock_server():
    """启动一个本地 mock-12306 服务（函数级，保证场景互不影响）。"""
    from werkzeug.serving import make_server

    app = create_mock_app("has_ticket")
    port = _free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, name="mock-12306", daemon=True)
    thread.start()
    time.sleep(0.15)

    handle = MockServerHandle(server, thread, f"http://127.0.0.1:{port}", app.config["MOCK_STATE"])
    try:
        yield handle
    finally:
        handle.stop()


# --------------------------------------------------------------------------- #
# 站点表 / 验证码
# --------------------------------------------------------------------------- #
@pytest.fixture()
def station_map(app_paths):
    """使用包内离线站点表，不联网（测试必须可离线复现）。"""
    from ticket_helper.inter.station import StationMap

    return StationMap(app_paths).load(allow_remote=False)


@pytest.fixture()
def fixed_solver():
    """固定答案的验证码识别器：让登录/下单流程在测试中可自动通过。"""
    from ticket_helper.verify.service import CallbackSolver

    return CallbackSolver(lambda image, hint, kind: "112,45", name="fixed")


# --------------------------------------------------------------------------- #
# 应用上下文
# --------------------------------------------------------------------------- #
@pytest.fixture()
def app_context(temp_home: Path, mock_server, monkeypatch: pytest.MonkeyPatch):
    """指向 mock 服务的完整 AppContext（不启动真实登录）。"""
    from ticket_helper.runtime.context import AppContext

    home = temp_home
    (home / "env.py").write_text(
        "ENV = {\n"
        f"    'api_base': '{mock_server.base_url}',\n"
        "    'web': {'enable': True, 'auth_user': 'admin', 'auth_pass': 'test-pass', 'port': 18099},\n"
        "}\n",
        encoding="utf-8",
    )
    ctx = AppContext(home=home, start_watchdog=False)
    # 站点表改用离线版，避免测试联网
    from ticket_helper.inter.station import StationMap

    ctx.station_map = StationMap(ctx.paths).load(allow_remote=False)
    ctx.accounts.station_map = ctx.station_map
    yield ctx
    try:
        ctx.stop()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 便捷构造
# --------------------------------------------------------------------------- #
@pytest.fixture()
def sample_task():
    from ticket_helper.config.settings import TaskConfig

    return TaskConfig(
        name="测试任务 广州→长沙",
        left_dates=["2026-10-01"],
        from_stations=["广州"],
        to_stations=["长沙"],
        trains_whitelist=["G6102", "G6140"],
        seat_types=["二等座", "一等座", "无座"],
        passengers=["张三"],
        strategy="direct",
        query_interval=0.3,
    )


@pytest.fixture()
def sample_user_config():
    from ticket_helper.config.settings import UserConfig

    return UserConfig(username="test_user", password="test_pass", heartbeat=120, enabled=True)
