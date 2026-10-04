"""反检测限速测试（RC-T1 ~ RC-T3）。"""

from __future__ import annotations

import time

from ticket_helper.config.settings import RateLimitConfig
from ticket_helper.ratelimit.limiter import ProxyPool, RateLimiter, TokenBucket


# --------------------------------------------------------------------------- #
# RC-T1 风控信号 → 退避到上限
# --------------------------------------------------------------------------- #
def test_risk_signal_backs_off_to_max():
    limiter = RateLimiter(RateLimitConfig(min_interval=1.0, max_interval=8.0, risk_backoff_factor=2.0))
    assert limiter.interval == 1.0

    limiter.report_risk()
    assert limiter.interval == 2.0
    limiter.report_risk()
    assert limiter.interval == 4.0
    limiter.report_risk()
    assert limiter.interval == 8.0
    limiter.report_risk()
    assert limiter.interval == 8.0, "不得超过 max_interval"

    snap = limiter.snapshot()
    assert snap["risk_count"] == 4
    assert snap["max_interval"] == 8.0


# --------------------------------------------------------------------------- #
# RC-T2 平稳后恢复（缓慢回落）
# --------------------------------------------------------------------------- #
def test_recovery_is_gradual():
    cfg = RateLimitConfig(min_interval=1.0, max_interval=8.0, recovery_step=1.0, recovery_after=3)
    limiter = RateLimiter(cfg)
    limiter.report_risk()   # 2.0
    limiter.report_risk()   # 4.0
    assert limiter.interval == 4.0

    # 未达到 recovery_after 次平稳，不得回落
    limiter.report_ok()
    limiter.report_ok()
    assert limiter.interval == 4.0

    limiter.report_ok()     # 第 3 次 → 回落一个步长
    assert limiter.interval == 3.0
    for _ in range(30):
        limiter.report_ok()
    assert limiter.interval == 1.0, "应回落且不低于 min_interval"


def test_recovery_never_goes_below_min():
    limiter = RateLimiter(RateLimitConfig(min_interval=2.0, recovery_step=5.0, recovery_after=1))
    for _ in range(10):
        limiter.report_ok()
    assert limiter.interval == 2.0


def test_adaptive_disabled_keeps_base_interval():
    limiter = RateLimiter(RateLimitConfig(adaptive=False, min_interval=1.0, max_interval=8.0))
    limiter.report_risk()
    assert limiter.interval == 1.0
    limiter.report_error()
    assert limiter.interval == 1.0


def test_report_error_adds_small_backoff():
    limiter = RateLimiter(RateLimitConfig(min_interval=1.0, max_interval=5.0, recovery_step=0.5))
    limiter.report_error()
    assert limiter.interval == 1.5


# --------------------------------------------------------------------------- #
# acquire 实际节流
# --------------------------------------------------------------------------- #
def test_acquire_enforces_interval():
    limiter = RateLimiter(RateLimitConfig(min_interval=0.3, max_interval=1.0))
    limiter.acquire()
    started = time.perf_counter()
    limiter.acquire()
    elapsed = time.perf_counter() - started
    assert elapsed >= 0.25, f"第二次请求应被节流，实际等待 {elapsed:.3f}s"


def test_acquire_is_roughly_concurrent_safe():
    """多线程共享限速器时，总请求频率不应超过配置（这是风控规避的核心保障）。"""
    import threading

    limiter = RateLimiter(RateLimitConfig(min_interval=0.1, adaptive=False))
    times: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(3):
            limiter.acquire()
            with lock:
                times.append(time.perf_counter())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    span = time.perf_counter() - started

    assert len(times) == 12
    # 12 次请求、间隔 0.1s → 至少要 1.1s 才可能完成
    assert span >= 1.0, f"12 次请求耗时 {span:.2f}s，节流未生效"


def test_reset_and_apply_config():
    limiter = RateLimiter(RateLimitConfig(min_interval=1.0, max_interval=8.0))
    limiter.report_risk()
    limiter.reset()
    assert limiter.interval == 1.0
    assert limiter.snapshot()["risk_count"] == 0

    limiter.apply_config(RateLimitConfig(min_interval=0.5, max_interval=3.0, adaptive=True))
    assert limiter.snapshot()["min_interval"] == 0.5
    assert limiter.snapshot()["max_interval"] == 3.0


# --------------------------------------------------------------------------- #
# RC-T3 代理池轮换
# --------------------------------------------------------------------------- #
def test_proxy_pool_disabled_by_default():
    pool = ProxyPool("")
    assert pool.enabled is False
    assert pool.get() is None
    assert pool.rotate() is None


def test_proxy_pool_rotates_static_list():
    pool = ProxyPool("http://p1:1,http://p2:2,http://p3:3", rotate_every=2)
    assert pool.enabled is True
    first = pool.get()
    assert first in ("http://p1:1", "http://p2:2", "http://p3:3")
    pool.get()   # 触发一次轮换
    rotated = pool.get()
    assert rotated != first
    assert pool.snapshot()["rotate_count"] >= 1


def test_proxy_pool_rotate_on_risk():
    pool = ProxyPool("http://p1:1,http://p2:2")
    limiter = RateLimiter(RateLimitConfig(proxy_pool="http://p1:1,http://p2:2"), proxy_pool=pool)
    before = pool.snapshot()["index"]
    limiter.report_risk()
    assert pool.snapshot()["index"] != before, "风控应触发代理轮换"
    assert limiter.snapshot()["proxy"]["enabled"] is True


def test_http_client_uses_proxy_provider(mock_server, app_paths):
    """配置代理后，HttpClient 必须把代理带上（否则等于没配）。"""
    from ticket_helper.inter.base import HttpClient

    seen: list[str] = []

    def provider():
        seen.append("called")
        return None  # 返回 None 时退化为直连，不报错

    client = HttpClient(base_url=mock_server.base_url, retries=0, proxy_provider=provider)
    response = client.get("/otn/login/conf", check_risk=False)
    assert response.ok
    assert seen == ["called"]
    client.close()


def test_ambient_env_proxy_never_covers_loopback(mock_server, monkeypatch):
    """★ 环境变量里的代理**不许**代理回环地址（本机 mock / 桌面端本地后端）。

    防的是：机器上装了全局代理时，本机请求被送到那个代理，报
    ``ProxyError: Cannot connect to proxy`` —— 这句话完全没法让人联想到
    「我的系统代理把 127.0.0.1 代理掉了」。真机上的表现同样是「本地接口全不通」。

    判据是**请求真的到达 mock 服务**（拿到 200），而不是「没抛异常」。
    """

    from ticket_helper.inter.base import HttpClient

    dead = "http://127.0.0.1:1"  # 端口 1 不会有人监听：一旦被代理，必定连不上
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, dead)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    client = HttpClient(base_url=mock_server.base_url, retries=0)
    try:
        response = client.get("/otn/login/conf", check_risk=False)
        assert response.status_code == 200, (
            "回环地址被环境代理劫走了：状态码 " + str(response.status_code)
        )
        assert client.request_count == 1, "请求必须真的发出去（而不是被吞成失败）"
    finally:
        client.close()


def test_token_bucket_limits_rate():
    bucket = TokenBucket(rate=20.0, capacity=2.0)
    assert bucket.take() is True
    assert bucket.take() is True
    started = time.perf_counter()
    assert bucket.take(timeout=2.0) is True
    assert time.perf_counter() - started >= 0.03
