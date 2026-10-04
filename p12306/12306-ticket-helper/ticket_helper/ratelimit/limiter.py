"""反检测限速与代理池（RC-01 ~ RC-04）。

核心思想：**默认低频、遇风控即时降频、连续平稳后缓慢恢复**。
- 收到风控信号 → 间隔按倍数放大（上限 ``max_interval``），并触发代理轮换；
- 连续 N 次无风控 → 间隔按步长回落至默认值；
- 关键设计：**「恢复」是缓慢的，避免刚放行就再次撞墙**，形成锯齿形稳定曲线。

同时提供令牌桶式的 ``acquire``（供 HttpClient 调用）与「闸门」方法，
登录与查询可共用同一限速器，也可分别注入（Py12306 的「登录与查询分离」思路）。
"""

from __future__ import annotations

import logging
import random
import threading
import time

from ticket_helper.config.settings import RateLimitConfig

log = logging.getLogger(__name__)


class ProxyPool:
    """轮换代理池（RC-02）。

    代理来源：配置 ``proxy_pool`` 支持
    - 逗号分隔的代理列表：``http://u:p@host:port,http://host:port``
    - 一个返回代理的 HTTP 接口（每行一个 / JSON 数组），按 TTL 拉取
    """

    def __init__(self, pool: str = "", *, rotate_every: int = 200, refresh_interval: float = 300.0) -> None:
        self.raw = pool or ""
        self.rotate_every = max(1, rotate_every)
        self.refresh_interval = refresh_interval
        self._static: list[str] = [p.strip() for p in self.raw.split(",") if p.strip() and p.strip().startswith("http")]
        self._remote_url = "" if self._static else self.raw.strip()
        self._dynamic: list[str] = []
        self._index = 0
        self._used = 0
        self._last_refresh = 0.0
        self._lock = threading.RLock()
        self.rotate_count = 0

    @property
    def enabled(self) -> bool:
        return bool(self._static or self._remote_url)

    def _refresh(self) -> None:
        if not self._remote_url:
            return
        now = time.time()
        if now - self._last_refresh < self.refresh_interval and self._dynamic:
            return
        self._last_refresh = now
        try:
            from ticket_helper.inter.base import HttpClient

            client = HttpClient(base_url="http://127.0.0.1", timeout=8.0, retries=0)
            response = client.get(self._remote_url, allow_retry=False, check_risk=False)
            client.close()
            text = response.text or ""
            items: list[str] = []
            if text.lstrip().startswith("["):
                import json

                items = [str(i) for i in json.loads(text)]
            else:
                for line in text.splitlines():
                    line = line.strip()
                    if line and (line.startswith("http") or ":" in line):
                        items.append(line if line.startswith("http") else f"http://{line}")
            if items:
                with self._lock:
                    self._dynamic = items
                log.info("代理池刷新成功: %d 个代理", len(items))
        except Exception as exc:  # noqa: BLE001
            log.warning("代理池刷新失败，沿用现有代理: %s", exc)

    def get(self) -> str | None:
        """取一个代理；未配置代理池时返回 None（直连）。"""
        if not self.enabled:
            return None
        self._refresh()
        with self._lock:
            pool = self._static or self._dynamic
            if not pool:
                return None
            self._used += 1
            if self._used % self.rotate_every == 0:
                self._index = (self._index + 1) % len(pool)
                self.rotate_count += 1
                log.info("代理轮换: 第 %d 个代理（已轮换 %d 次）", self._index + 1, self.rotate_count)
            return pool[self._index % len(pool)]

    def rotate(self) -> str | None:
        """主动轮换（风控触发时调用）。"""
        with self._lock:
            pool = self._static or self._dynamic
            if not pool:
                return None
            self._index = (self._index + 1) % len(pool)
            self.rotate_count += 1
            log.warning("检测到风控，代理已切换至第 %d 个", self._index + 1)
            return pool[self._index]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            size = len(self._static or self._dynamic)
            return {
                "enabled": self.enabled,
                "size": size,
                "index": self._index,
                "rotate_count": self.rotate_count,
            }


class RateLimiter:
    """自适应查询限速器。

    用法::

        limiter = RateLimiter(config)
        limiter.acquire()          # 阻塞到允许发起下一次请求
        limiter.report_risk()      # 收到风控信号 → 降频
        limiter.report_ok()        # 平稳 → 逐步恢复
    """

    def __init__(
        self,
        config: RateLimitConfig | None = None,
        *,
        interval: float | None = None,
        proxy_pool: ProxyPool | None = None,
        pacing: object | None = None,
    ) -> None:
        cfg = config or RateLimitConfig()
        self.config = cfg
        self.enabled = cfg.adaptive
        # 节奏规格由装配中心注入（`kernel` 域的 `pacing`）：**只换参数，不换算法** ——
        # 自适应那套（放大/回落）上游没有，留着。未注入 = 用配置里的两个数（改造前行为）。
        min_source = (
            float(getattr(pacing, "interval_min")) if pacing is not None else float(cfg.min_interval)
        )
        max_source = (
            float(getattr(pacing, "interval_max")) if pacing is not None else float(cfg.max_interval)
        )
        base = interval if interval is not None else min_source
        self._base_interval = max(0.05, float(base))
        self._interval = self._base_interval
        self._min = max(0.05, min_source)
        self._max = max(self._min, max_source)
        self._ok_streak = 0
        self._risk_count = 0
        self._last_request = 0.0
        self._lock = threading.RLock()
        self.proxy_pool = proxy_pool or ProxyPool(cfg.proxy_pool, rotate_every=cfg.proxy_rotate_every)

    # ------------------------------------------------------------------ #
    @property
    def interval(self) -> float:
        with self._lock:
            return self._interval

    def acquire(self, *, timeout: float | None = None) -> float:
        """阻塞至允许发起请求，返回实际等待秒数。

        等待期间使用「分片睡眠 + 抖动」，避免多线程在同一时刻齐刷刷醒来形成尖峰。
        """
        with self._lock:
            interval = self._interval
            elapsed = time.time() - self._last_request
            wait = interval - elapsed
            if wait <= 0:
                self._last_request = time.time()
                return 0.0
            # 预留本次窗口，其他线程会排队等待
            self._last_request = time.time() + wait

        waited = 0.0
        deadline = time.time() + wait + (timeout or wait + 5.0)
        while waited < wait:
            chunk = min(0.05 + random.random() * 0.05, wait - waited)
            time.sleep(max(0.001, chunk))
            waited += chunk
            if time.time() > deadline:  # pragma: no cover - 防御性
                break
        return waited

    def report_risk(self, *, kind: str = "risk_control") -> float:
        """收到风控信号：指数放大间隔并轮换代理（RC-01 / RC-02）。"""
        with self._lock:
            self._risk_count += 1
            self._ok_streak = 0
            if not self.enabled:
                return self._interval
            self._interval = min(self._max, max(self._interval * self.config.risk_backoff_factor, self._min * 2))
            new_interval = self._interval
        log.warning("风控信号(%s)，查询间隔升至 %.1fs", kind, new_interval)
        self.proxy_pool.rotate()
        return new_interval

    def report_ok(self) -> float:
        """一次平稳请求：连续 N 次后缓慢回落。"""
        with self._lock:
            self._ok_streak += 1
            if not self.enabled:
                return self._interval
            if self._ok_streak >= self.config.recovery_after and self._interval > self._base_interval:
                self._interval = max(self._base_interval, self._interval - self.config.recovery_step)
                self._ok_streak = 0
                log.info("环境平稳，查询间隔恢复至 %.1fs", self._interval)
            return self._interval

    def report_error(self) -> float:
        """网络错误：轻度退避（不轮换代理）。"""
        with self._lock:
            self._ok_streak = 0
            if self.enabled:
                self._interval = min(self._max, self._interval + self.config.recovery_step)
            return self._interval

    def reset(self) -> None:
        with self._lock:
            self._interval = self._base_interval
            self._ok_streak = 0
            self._risk_count = 0

    def apply_config(self, config: RateLimitConfig) -> None:
        """配置热更新回调（CF-04）。"""
        with self._lock:
            self.config = config
            self.enabled = config.adaptive
            self._min = max(0.05, float(config.min_interval))
            self._max = max(self._min, float(config.max_interval))
            self._base_interval = self._min
            self._interval = min(max(self._interval, self._min), self._max)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "interval": round(self._interval, 2),
                "min_interval": self._min,
                "max_interval": self._max,
                "adaptive": self.enabled,
                "risk_count": self._risk_count,
                "proxy": self.proxy_pool.snapshot(),
            }


class TokenBucket:
    """可选：对「下单」这类关键动作做独立限流，避免提交过密被风控。"""

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = max(0.01, rate)
        self.capacity = capacity if capacity is not None else rate
        self._tokens = self.capacity
        self._last = time.time()
        self._lock = threading.Lock()

    def take(self, tokens: float = 1.0, *, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while True:
            with self._lock:
                now = time.time()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                need = (tokens - self._tokens) / self.rate
            if time.time() + need > deadline:
                return False
            time.sleep(max(0.005, min(need, 0.2)))
