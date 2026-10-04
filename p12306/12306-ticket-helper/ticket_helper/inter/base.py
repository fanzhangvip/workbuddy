"""HTTP 基础设施：会话、重试、代理、风控识别。

设计要点：
- 所有出网请求都经此层，便于统一挂代理池、限速、指标采集与风控识别；
- ``base_url`` 可注入（配置 ``api_base``），指向本地 mock-12306 时即可完成
  完整的离线端到端测试（测试方案 1.2 的核心前提）；
- 重试只针对**可重试错误**（网络异常 / 5xx / 429），4xx 业务错误不重试；
- 绝不把账号密码、Cookie、Token 明文写进日志。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ticket_helper.errors import NetworkError, ResponseParseError, RiskControlError
from ticket_helper.inter import urls
from ticket_helper.logger import metrics

log = logging.getLogger(__name__)


#: 回环 / 本机地址 —— 这些地址上的请求**永远不走代理**。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback_host(url: str) -> bool:
    """URL 的主机是不是本机回环地址。

    为什么必须单独判：机器上装了全局代理（``HTTP_PROXY``）时，``requests``
    默认会把**本机**请求也送去代理，报 ``ProxyError: Cannot connect to proxy``
    —— 这句话完全没法让人联想到「我的系统代理把 127.0.0.1 代理掉了」。
    实测表现是「本机 mock 服务 / 桌面端本地后端突然全都不通」。
    """

    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    host = host.strip().lower()
    return host in LOOPBACK_HOSTS or host.startswith("127.")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: 风控 / 限流的响应特征（RC-04）
RISK_MARKERS = (
    "您的操作过于频繁",
    "请稍后重试",
    "网络繁忙",
    "系统繁忙",
    "访问过于频繁",
    "非法的请求",
    "请开启JavaScript",
    "疑似非本人操作",
)
RISK_KEYWORDS_SHORT = ("验证码", "滑块")

#: 需要捕获为「业务失败」而不是「崩溃」的响应标记
RESULT_CODE_FIELD = "result_code"


class RiskSignal:
    """一次风控信号，供限速器消费（RC-01 / RC-04）。"""

    __slots__ = ("kind", "detail", "status_code")

    def __init__(self, kind: str, detail: str = "", status_code: int = 0) -> None:
        self.kind = kind
        self.detail = detail
        self.status_code = status_code

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RiskSignal {self.kind} {self.status_code} {self.detail[:40]!r}>"


class HttpResponse:
    """统一响应包装：状态码 + 文本 + 耗时 + 解析结果。"""

    __slots__ = ("ok", "status_code", "text", "json", "url", "latency_ms", "raw")

    def __init__(
        self,
        *,
        ok: bool,
        status_code: int,
        text: str = "",
        json_data: Any = None,
        url: str = "",
        latency_ms: int = 0,
        raw: requests.Response | None = None,
    ) -> None:
        self.ok = ok
        self.status_code = status_code
        self.text = text
        self.json = json_data
        self.url = url
        self.latency_ms = latency_ms
        self.raw = raw

    def __repr__(self) -> str:  # pragma: no cover
        return f"<HttpResponse {self.status_code} ok={self.ok} {self.latency_ms}ms>"


class HttpClient:
    """带会话、重试、代理与风控识别的 HTTP 客户端。"""

    def __init__(
        self,
        *,
        base_url: str = urls.BASE_OFFICIAL,
        timeout: float = 12.0,
        retries: int = 2,
        proxy_provider: Callable[[], str | None] | None = None,
        rate_limiter: Any | None = None,
        verify_ssl: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, retries)
        self.proxy_provider = proxy_provider
        self.rate_limiter = rate_limiter
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": DEFAULT_UA,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Connection": "keep-alive",
            }
        )
        if headers:
            self.session.headers.update(headers)

        adapter = HTTPAdapter(
            pool_connections=16,
            pool_maxsize=32,
            max_retries=Retry(total=0, connect=0, read=0, status=0),
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._risk_listeners: list[Callable[[RiskSignal], None]] = []
        self._lock = threading.RLock()
        self.request_count = 0

    # ------------------------------------------------------------------ #
    # 风控信号
    # ------------------------------------------------------------------ #
    def on_risk(self, callback: Callable[[RiskSignal], None]) -> Callable[[], None]:
        with self._lock:
            self._risk_listeners.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._risk_listeners:
                    self._risk_listeners.remove(callback)

        return _unsubscribe

    def _emit_risk(self, signal: RiskSignal) -> None:
        with self._lock:
            listeners = list(self._risk_listeners)
        log.warning("检测到风控信号: %s (status=%s)", signal.kind, signal.status_code)
        for callback in listeners:
            try:
                callback(signal)
            except Exception as exc:  # noqa: BLE001
                log.error("风控回调执行失败: %s", exc)

    # ------------------------------------------------------------------ #
    # 请求
    # ------------------------------------------------------------------ #
    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(self.base_url + "/", path.lstrip("/"))

    # ------------------------------------------------------------------ #
    # 会话（Cookie）导入 / 导出 —— 供「重启后免扫码」使用
    # ------------------------------------------------------------------ #
    def dump_cookies(self) -> list[dict[str, Any]]:
        """把整个 Cookie Jar 导成 **browser-ready** 的列表。

        存在的理由：过滑块要把会话 Cookie 交给一个真浏览器（pyppeteer 的
        ``page.setCookie(*dicts)``）。没有这个口子，调用方就得去摸
        ``client.session.cookies`` —— 那是穿透封装。

        ⚠️ **必须导全部、不做筛选**。本项目在「会话持久化」上踩过这个坑：
        当时只留了 5 个关键 Cookie，恢复后 ``checkUser`` 回 ``flag:false``，
        现象是「明明存了会话却总是要求重新登录」。设备指纹那类 Cookie
        少一个就可能被 WAF 拒，而症状是「滑块一直过不去」。

        形状说明：pyppeteer 的 ``setCookie`` 要求每项要么给 ``url``、
        要么给 ``domain``（两者都没有会直接抛错）—— 所以这里按
        「有 domain 就给 domain，没有就回落到基址 url」补齐。
        """
        out: list[dict[str, Any]] = []
        try:
            cookies = list(self.session.cookies)
        except Exception:  # noqa: BLE001 - 导 Cookie 不该成为失败点
            return out
        for cookie in cookies:
            item: dict[str, Any] = {
                "name": str(cookie.name),
                "value": str(cookie.value or ""),
                "path": str(cookie.path or "/"),
                "secure": bool(cookie.secure),
            }
            if cookie.domain:
                item["domain"] = str(cookie.domain)
            else:
                item["url"] = str(self.base_url or "")
            out.append(item)
        return out

    def cookie_of(self, name: str) -> str:
        """读单个 cookie 的值；没有则返回空串。

        存在的理由：有几个接口**要求把某些 cookie 也放进表单**（例如扫码轮询要
        `RAIL_DEVICEID`）。没有这个口子，调用方就得去摸 ``client.session.cookies`` ——
        那是穿透封装，会把「会话怎么存」这件事泄漏到业务代码里。
        """
        try:
            value = self.session.cookies.get(str(name))
        except Exception:  # noqa: BLE001 - 取 cookie 不该成为失败点
            return ""
        return str(value or "")

    def export_cookies(self) -> list[dict[str, Any]]:
        """把当前会话的 Cookie 导出为可序列化的列表。

        **这是登录凭据**：调用方必须加密后再落盘（见 ``account/session.py``），
        且绝不能写进日志。
        """
        cookies: list[dict[str, Any]] = []
        try:
            for cookie in self.session.cookies:
                cookies.append(
                    {
                        "name": cookie.name,
                        "value": cookie.value,
                        "domain": cookie.domain or "",
                        "path": cookie.path or "/",
                    }
                )
        except Exception as exc:  # noqa: BLE001 - 导出失败不该影响任何调用方
            log.debug("导出 Cookie 失败: %s", exc)
        return cookies

    def import_cookies(self, cookies: list[dict[str, Any]]) -> int:
        """把之前导出的 Cookie 灌回会话，返回成功条数。"""
        count = 0
        for cookie in cookies or []:
            name = str(cookie.get("name") or "")
            if not name:
                continue
            try:
                self.session.cookies.set(
                    name,
                    str(cookie.get("value") or ""),
                    domain=str(cookie.get("domain") or ""),
                    path=str(cookie.get("path") or "/"),
                )
                count += 1
            except Exception as exc:  # noqa: BLE001 - 单个 Cookie 坏掉就跳过
                log.debug("导入 Cookie %s 失败: %s", name, exc)
        return count

    def has_session_cookie(self) -> bool:
        """当前会话里是否还有登录态 Cookie（用于判断「值不值得去验证一次」）。"""
        try:
            return any(c.name in ("JSESSIONID", "tk") for c in self.session.cookies)
        except Exception:  # noqa: BLE001
            return False

    def clear_cookies(self) -> None:
        """丢弃当前会话的全部 Cookie。

        用于「验证发现本地会话已失效」：不清掉的话，后面正常登录会带着
        这些无用 Cookie 一起发出去，官方可能仍按旧会话处理，出现
        「刚登录成功却依然报未登录」这种很难解释的现象。
        """
        try:
            self.session.cookies.clear()
        except Exception as exc:  # noqa: BLE001
            log.debug("清空 Cookie 失败: %s", exc)

    def _proxies(self, url: str) -> dict[str, str | None] | None:
        """本次请求该走哪个代理。

        优先级：**显式配置的代理**（``proxy_provider``，那是使用者的明确意图）
        → 回环地址**显式直连** → 其余交给环境变量（``HTTP_PROXY`` 等）。

        中间那条是必须的：``requests`` 会把请求级 ``proxies`` 与环境代理合并，
        ``{"http": None}`` 正好用来**压制**环境代理，而不是「没配代理」。
        """

        if self.proxy_provider:
            try:
                proxy = self.proxy_provider()
            except Exception as exc:  # noqa: BLE001
                log.warning("代理获取失败，改用直连: %s", exc)
                return None
            if proxy:
                return {"http": proxy, "https": proxy}
        if _is_loopback_host(url):
            return {"http": None, "https": None}
        return None

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_retry: bool = True,
        check_risk: bool = True,
        referer: str | None = None,
    ) -> HttpResponse:
        url = self.url(path)
        attempts = (self.retries if allow_retry else 0) + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()
            request_headers = dict(headers or {})
            if referer:
                request_headers.setdefault("Referer", referer)
            started = time.perf_counter()
            try:
                response = self.session.request(
                    method.upper(),
                    url,
                    params=params,
                    data=data,
                    json=json_body,
                    headers=request_headers or None,
                    timeout=timeout or self.timeout,
                    proxies=self._proxies(url),
                    verify=self.verify_ssl,
                )
            except requests.RequestException as exc:
                last_error = exc
                latency = int((time.perf_counter() - started) * 1000)
                metrics.observe("http.latency", latency)
                log.warning("请求异常(%s/%s) 第%s次: %s", method, path, attempt + 1, exc)
                if attempt + 1 < attempts:
                    self._sleep_backoff(attempt)
                    continue
                raise NetworkError(f"网络请求失败: {exc}") from exc

            latency = int((time.perf_counter() - started) * 1000)
            metrics.observe("http.latency", latency)
            self.request_count += 1

            if check_risk:
                signal = self._detect_risk(response)
                if signal is not None:
                    self._emit_risk(signal)

            # 429 / 403 是风控语义，不是普通网络故障：
            # 必须区分为 RiskControlError，上层才会降频而不是无脑重试（RC-01/RC-04）
            if response.status_code in (429, 403):
                if attempt + 1 < attempts:
                    self._sleep_backoff(attempt)
                    continue
                raise RiskControlError(
                    f"请求被风控拦截: HTTP {response.status_code}",
                    detail=response.text[:160],
                )

            if response.status_code >= 500:
                if attempt + 1 < attempts:
                    self._sleep_backoff(attempt)
                    continue
                raise NetworkError(f"服务端异常: HTTP {response.status_code}")

            return self._wrap(response, latency)

        raise NetworkError(f"请求失败且重试耗尽: {last_error}")

    def get(self, path: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> HttpResponse:
        kwargs.setdefault("check_risk", True)
        return self.request("POST", path, **kwargs)

    # ------------------------------------------------------------------ #
    def _wrap(self, response: requests.Response, latency: int) -> HttpResponse:
        text = response.text or ""
        json_data: Any = None
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
            try:
                json_data = response.json()
            except ValueError:
                json_data = None
        return HttpResponse(
            ok=response.status_code == 200,
            status_code=response.status_code,
            text=text,
            json_data=json_data,
            url=response.url,
            latency_ms=latency,
            raw=response,
        )

    def _detect_risk(self, response: requests.Response) -> RiskSignal | None:
        if response.status_code == 429:
            return RiskSignal("rate_limited", "HTTP 429", 429)
        if response.status_code == 403:
            return RiskSignal("forbidden", "HTTP 403", 403)
        text = response.text or ""
        if len(text) > 4000:
            # 大响应体基本是正常数据，跳过全文扫描，避免无谓开销
            return None
        for marker in RISK_MARKERS:
            if marker in text:
                return RiskSignal("risk_control", marker, response.status_code)
        return None

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        # 指数退避 + 抖动，避免多任务同时重试形成尖峰
        delay = min(8.0, 0.5 * (2**attempt)) * (0.7 + random.random() * 0.6)
        time.sleep(delay)

    # ------------------------------------------------------------------ #
    def set_base_url(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass


def expect_json(response: HttpResponse, *, allow_empty: bool = False) -> Any:
    """从响应中取 JSON，失败抛统一错误。"""
    if response.json is None:
        if allow_empty and response.ok:
            return None
        raise ResponseParseError(
            f"响应不是合法 JSON (HTTP {response.status_code})",
            detail=response.text[:200],
        )
    return response.json


def is_login_required(payload: Any) -> bool:
    """识别「未登录」响应（12306 常用 ``{"result_code":0,"messages":"..."}`` 或跳转登录）。"""
    if not isinstance(payload, dict):
        return False
    if payload.get("login") is False:
        return True
    if str(payload.get(RESULT_CODE_FIELD, "1")) == "0":
        message = str(payload.get("messages") or payload.get("msg") or "")
        if "登录" in message or "login" in message.lower():
            return True
    return False
