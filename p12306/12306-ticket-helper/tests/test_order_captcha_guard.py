"""下单验证码：响应**必须真的是一张图**才允许交给识别链（回流 #59）。

真实故障（2026-09-30 真机）：``getPassCodeNew`` 被 12306 回了 200 + HTML 错误页，
老实现只做了 ``"json" in response.text[:2]`` 这个**恒为假**的判断
（在前 2 个字符里找 "json"），也没有 HTML 拒绝页检查 —— HTML 字节被当成
验证码图片一路传到人工兜底弹窗，使用者只看到「验证码图片无法显示」，
白等 2 分钟超时，且得不到任何原因。

这里钉住四件事（**撤掉实现必须变红**）：
1. HTML 拒绝页 → ``CaptchaUnavailableError``，且信息里说清是被拒绝；
2. JSON 信封   → ``CaptchaUnavailableError``，并带上官方 ``messages``；
3. Content-Type 不是图片、字节头也不是已知图片格式 → 拦截，不往下传；
4. 正常图片 → 原样返回 ``image_bytes``（不许误伤真图）。
"""

from __future__ import annotations

import pytest

from ticket_helper.errors import CaptchaUnavailableError
from ticket_helper.inter.api import TicketApi
from ticket_helper.inter.base import HttpClient, HttpResponse

HTML_PAGE = "<html><body>网络可能存在问题，请您重试一下！</body></html>"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"rest-of-image-bytes"


class _FakeRaw:
    """最小化的 ``requests.Response`` 替身：只要 ``headers`` 与 ``content``。"""

    def __init__(self, content: bytes, content_type: str) -> None:
        self.headers = {"Content-Type": content_type}
        self.content = content


def _response(content: bytes, content_type: str) -> HttpResponse:
    return HttpResponse(
        ok=True,
        status_code=200,
        text=content.decode("utf-8", errors="replace"),
        url="/otn/passcodeNew/getPassCodeNew",
        raw=_FakeRaw(content=content, content_type=content_type),
    )


def _api(mock_server, monkeypatch, response: HttpResponse) -> TicketApi:
    api = TicketApi(HttpClient(base_url=mock_server.base_url, retries=1), None)  # type: ignore[arg-type]
    monkeypatch.setattr(api.client, "get", lambda *args, **kwargs: response)
    return api


def test_order_captcha_rejects_html_error_page(mock_server, monkeypatch):
    """★ HTML 拒绝页不许被当成验证码图片（本次故障的直接根因）。"""
    api = _api(mock_server, monkeypatch, _response(HTML_PAGE.encode("utf-8"), "text/html"))
    with pytest.raises(CaptchaUnavailableError) as excinfo:
        api.get_order_captcha()
    assert "HTML" in str(excinfo.value.message), excinfo.value.message


def test_order_captcha_rejects_json_envelope_and_carries_official_message(mock_server, monkeypatch):
    """JSON 信封要拦截，并且把官方 ``messages`` 带出来（老判断恒为假，等于没判）。"""
    body = '{"status":false,"messages":["系统忙，请稍后重试"]}'.encode("utf-8")
    api = _api(mock_server, monkeypatch, _response(body, "application/json"))
    with pytest.raises(CaptchaUnavailableError) as excinfo:
        api.get_order_captcha()
    assert "系统忙" in str(excinfo.value.message), excinfo.value.message


def test_order_captcha_rejects_non_image_bytes(mock_server, monkeypatch):
    """Content-Type 不是图片、字节头也不是已知图片格式 → 不把垃圾当图传下去。"""
    api = _api(mock_server, monkeypatch, _response(b"XXXXNOTANIMAGE", "application/octet-stream"))
    with pytest.raises(CaptchaUnavailableError) as excinfo:
        api.get_order_captcha()
    assert "不是图片" in str(excinfo.value.message), excinfo.value.message


def test_order_captcha_returns_real_image_untouched(mock_server, monkeypatch):
    """正常图片必须原样返回 —— 拦截逻辑不许误伤真图。"""
    api = _api(mock_server, monkeypatch, _response(PNG_BYTES, "image/png"))
    captcha = api.get_order_captcha()
    assert captcha.image_bytes == PNG_BYTES
