"""腾讯视频专用解析（`src/core/resolver.py` 的 `_extract_tencent`）回归。

背景：yt-dlp 内置的 vqq 提取器靠页面里的 `window.__pinia` 取播放信息，
而腾讯改版后页面不再内嵌它，提取器直接报 "unable to extract pinia data"，
之后一路重试到硬超时——用户看到的就是「解析超时」。这里改走腾讯官方
`getinfo` 接口拿直链，所以这个文件盯：

1. **地址识别**——只有腾讯的页面地址才走这条路，vid 要从三种链接形态里抠对；
2. **拼直链**——`{CDN 节点}{文件名}?vkey={密钥}`，Referer 必须带上；
3. **拿不到就老实返回 None**——绝不能因为"我们没取到"就把 yt-dlp 那条路掐掉；
4. **解析超时可配置**——默认 60 秒，别被谁改回去。

全部离线：网络请求用假响应替换，不发真实请求。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core import resolver as R  # noqa: E402
from src.utils import settings as S  # noqa: E402


class _FakeResponse:
    """够用的假响应：只实现被调用到的几个成员。"""

    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def _qz(payload: dict) -> _FakeResponse:
    """腾讯接口的真实外形：QZOutputJson={...};"""
    return _FakeResponse("QZOutputJson=" + json.dumps(payload) + ";")


def _payload(**overrides) -> dict:
    entry = {
        "fn": "gzc_1000102_abc.f10217.mp4",
        "fvkey": "FVKEY123",
        "ti": "交锋_02",
        "vw": 1280,
        "vh": 546,
        "fs": 204386626,
        "ul": {"ui": [{"url": "http://node.video.tc.qq.com/path/"}]},
    }
    entry.update(overrides)
    return {"em": 0, "exem": 2, "vl": {"vi": [entry]}}


class TencentHostTest(unittest.TestCase):
    def test_recognises_tencent_hosts(self) -> None:
        for url in (
            "https://v.qq.com/x/cover/mzc00200jtsx0ds/v4102mfst3m.html",
            "https://m.v.qq.com/x/page/v4102mfst3m.html",
            "https://film.qq.com/x/cover/abc/xyz.html",
        ):
            self.assertTrue(R._is_tencent(url), url)

    def test_ignores_other_hosts(self) -> None:
        for url in ("https://www.bilibili.com/video/BV1xx", "https://youtu.be/x"):
            self.assertFalse(R._is_tencent(url), url)


class TencentVidTest(unittest.TestCase):
    """三种链接形态都要能抠出 vid（vid 是路径最后一段）。"""

    def test_cover_url(self) -> None:
        self.assertEqual(
            R._tencent_vid(
                "https://v.qq.com/x/cover/mzc00200jtsx0ds/v4102mfst3m.html"
                "?callTime=1791534024601"
            ),
            "v4102mfst3m",
        )

    def test_page_url(self) -> None:
        self.assertEqual(
            R._tencent_vid("https://v.qq.com/x/page/v4102mfst3m.html"),
            "v4102mfst3m",
        )

    def test_query_vid(self) -> None:
        # 路径段太短（"abc"）不可能是 vid，正则要求 ≥8 位，于是落到 ?vid=
        self.assertEqual(
            R._tencent_vid("https://v.qq.com/x/cover/abc.html?vid=v4102mfst3m"),
            "v4102mfst3m",
        )
        self.assertEqual(
            R._tencent_vid("https://v.qq.com/player?vid=v4102mfst3m"),
            "v4102mfst3m",
        )


class ParseQzJsonTest(unittest.TestCase):
    def test_strips_wrapper(self) -> None:
        self.assertEqual(
            R._parse_qz_json('QZOutputJson={"em":0,"s":"o"};'),
            {"em": 0, "s": "o"},
        )

    def test_rejects_garbage(self) -> None:
        for text in ("", "not json", "QZOutputJson=;", "[]"):
            self.assertIsNone(R._parse_qz_json(text), text)


class ExtractTencentTest(unittest.TestCase):
    """取流与拼地址。"""

    URL = "https://v.qq.com/x/cover/mzc00200jtsx0ds/v4102mfst3m.html"

    def test_builds_play_url(self) -> None:
        with patch("requests.get", return_value=_qz(_payload())) as get:
            info = R._extract_tencent(self.URL, lambda _m: None)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(
            info["url"], "http://node.video.tc.qq.com/path/gzc_1000102_abc.f10217.mp4?vkey=FVKEY123"
        )
        self.assertEqual(info["title"], "交锋_02")
        self.assertEqual(info["id"], "v4102mfst3m")
        self.assertEqual((info["width"], info["height"]), (1280, 546))
        self.assertEqual(info["filesize"], 204386626)
        # 直链必须带 Referer，否则腾讯 CDN 会拒
        self.assertEqual(info["http_headers"]["Referer"], "https://v.qq.com/")
        # extractor_key 用 "qq"，复用好 _EXTRACTOR_NAMES 里的中文名
        self.assertEqual(R._extractor_label("qq"), "腾讯视频")
        get.assert_called_once()

    def test_falls_back_to_lower_quality(self) -> None:
        """最高档没地址时要换下一档，而不是直接放弃。"""
        empty = {"em": 91, "exem": -1, "vl": {"vi": [{}]}}
        with patch("requests.get", side_effect=[_qz(empty), _qz(_payload())]) as get:
            info = R._extract_tencent(self.URL, lambda _m: None)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["format_id"], R._TENCENT_DEFNS[1])
        self.assertEqual(get.call_count, 2)

    def test_returns_none_when_no_stream(self) -> None:
        """三档都没地址 → None，让 yt-dlp 那条路继续走。"""
        empty = {"em": 91, "exem": -1, "vl": {"vi": [{}]}}
        with patch("requests.get", return_value=_qz(empty)):
            self.assertIsNone(R._extract_tencent(self.URL, lambda _m: None))

    def test_returns_none_on_request_error(self) -> None:
        with patch("requests.get", side_effect=OSError("boom")):
            self.assertIsNone(R._extract_tencent(self.URL, lambda _m: None))

    def test_returns_none_without_vid(self) -> None:
        with patch("requests.get") as get:
            self.assertIsNone(R._extract_tencent("https://v.qq.com/", lambda _m: None))
        get.assert_not_called()


class ResolveTimeoutTest(unittest.TestCase):
    """解析超时可配置：默认 60 秒，别被改回很短的值。"""

    def test_settings_default_is_60(self) -> None:
        self.assertEqual(S.DEFAULTS[S.K_RESOLVE_TIMEOUT_S], 60)

    def test_resolver_fallback_is_60(self) -> None:
        self.assertEqual(R.DEFAULT_TIMEOUT_S, 60)

    def test_resolve_signature_uses_default(self) -> None:
        import inspect

        signature = inspect.signature(R.resolve)
        self.assertEqual(signature.parameters["timeout_s"].default, 60)


if __name__ == "__main__":
    unittest.main()
