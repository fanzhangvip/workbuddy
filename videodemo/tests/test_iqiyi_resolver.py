"""爱奇艺专用解析（`src/core/resolver.py` 的 `_extract_iqiyi`）回归。

背景：yt-dlp 内置的 iqiyi 提取器要在页面里找 `data-player-tvid` 属性来拿
tvid，而爱奇艺改版后播放页成了纯前端渲染，属性没了 → 提取器直接报
"Can't find any video"，重试到超时也没用。这里改走爱奇艺自家的移动端取流
接口（tmts），所以这个文件盯：

1. **地址识别**——只有爱奇艺的页面地址才走这条路，`/v_<id>.html` 要抠对；
2. **签名**——`sc = md5(毫秒时间戳 + 密钥 + tvid)`，参数与 UA 都不能错；
3. **取流与选档**——拿到 m3u8，且优先高清晰度档、失败要退回默认档；
4. **拿不到就老实返回 None**——绝不能因为"我们没取到"就把 yt-dlp 那条路掐掉。

全部离线：网络请求用假 Session 替换，不发真实请求。
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core import resolver as R  # noqa: E402

TVID = "5459239950713400"
PRIMARY_VID = "1c0fa2bfc64285dcbbb463d78404f183"
HIGH_VID = "1d9b07141b05107b6dde363fa0274048"
LOW_M3U = "http://mus.video.iqiyi.com/mus/a/low.m3u8?qd_vip=0"
HIGH_M3U = "http://mus.video.iqiyi.com/mus/a/high.m3u8?qd_vip=0"
PAGE_URL = "https://m.iqiyi.com/v_1gu30dui6vo.html"
BASE_TMTS = f"http://cache.m.iqiyi.com/jp/tmts/{TVID}/{PRIMARY_VID}/"
HIGH_TMTS = f"http://cache.m.iqiyi.com/jp/tmts/{TVID}/{HIGH_VID}/"


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _FakeSession:
    """够用的假 Session：按 URL 子串路由，并记下每次请求的参数。"""

    def __init__(self, routes: dict[str, object]) -> None:
        self.headers: dict[str, str] = {}
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict | None = None, timeout=None):  # noqa: ANN001
        self.calls.append((url, dict(params or {})))
        for needle, response in self.routes.items():
            if needle in url:
                if isinstance(response, list):
                    return response.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"未预期的请求: {url}")


def _page_html() -> str:
    """真实页面的外形：SSR 数据里带中文简介，简介里含括号。"""
    state = {
        "play": {
            "videoInfo": {
                "tvid": int(TVID),
                "videoName": "余红旧事第1集",
                "duration": 1070,
                "desc": "他说“{不是兄弟不让你活}”，随后（括号）也来了。",
            },
            "loadInfo": {"qipuId": int(TVID), "vid": PRIMARY_VID, "duration": 1070},
        }
    }
    return (
        "<html><head><title>余红旧事第1集</title></head><body>"
        "<script>window.__INITIAL_STATE__ = "
        + json.dumps(state, ensure_ascii=False)
        + ";</script></body></html>"
    )


def _tmts(code: int = 2, m3u: str = "", vd: str = "96", screen: str = "384x216",
          vidl: list | None = None, duration: int = 1070) -> _FakeResponse:
    data = {
        "code": code,
        "m3u": m3u,
        "vd": vd,
        "screenSize": screen,
        "duration": duration,
        "vidl": vidl if vidl is not None else [],
    }
    return _FakeResponse('var tvInfoJs=' + json.dumps({"code": "A00000", "data": data}) + ";")


def _vidl() -> list[dict]:
    return [{"vid": HIGH_VID, "vd": "4"}, {"vid": "aaaaaaaa", "vd": "17"}]


class IqiyiHostTest(unittest.TestCase):
    def test_recognises_iqiyi_hosts(self) -> None:
        for url in (
            "https://www.iqiyi.com/v_1gu30dui6vo.html",
            "https://m.iqiyi.com/v_1gu30dui6vo.html",
            "https://iqiyi.com/v_1gu30dui6vo.html",
            "https://www.iqiyi.com/x/cover/abc/v_x.html",
        ):
            self.assertTrue(R._is_iqiyi(url), url)

    def test_ignores_other_hosts(self) -> None:
        for url in ("https://v.youku.com/v_show/id_X.html", "https://youtu.be/x"):
            self.assertFalse(R._is_iqiyi(url), url)


class IqiyiVidTest(unittest.TestCase):
    def test_extracts_from_share_url(self) -> None:
        self.assertEqual(
            R._iqiyi_vid(
                "https://www.iqiyi.com/v_1gu30dui6vo.html?qyTrace=GNGhlS8WOZGAoBKU"
            ),
            "1gu30dui6vo",
        )

    def test_lowercases(self) -> None:
        self.assertEqual(R._iqiyi_vid("https://m.iqiyi.com/v_ABC123.html"), "abc123")

    def test_empty_when_not_a_video_page(self) -> None:
        for url in ("https://www.iqiyi.com/", "https://www.iqiyi.com/a_abc.html", ""):
            self.assertEqual(R._iqiyi_vid(url), "", url)


class JsonAfterTest(unittest.TestCase):
    def test_handles_braces_inside_strings(self) -> None:
        text = 'x window.__INITIAL_STATE__ = {"desc":"a{内层}括号","n":1};'
        self.assertEqual(
            R._json_after(text, "__INITIAL_STATE__"),
            {"desc": "a{内层}括号", "n": 1},
        )

    def test_skips_a_failed_occurrence(self) -> None:
        """脚本里先引用、后面才赋值时，不能被前半段骗到。"""
        text = 'var a = __INITIAL_STATE__ ; window.__INITIAL_STATE__ = {"ok":1};'
        self.assertEqual(R._json_after(text, "__INITIAL_STATE__"), {"ok": 1})

    def test_missing_marker(self) -> None:
        self.assertIsNone(R._json_after("<html></html>", "__INITIAL_STATE__"))


class TmtsSignatureTest(unittest.TestCase):
    """签名与参数：这是接口肯不肯给流的唯一门槛。"""

    def test_builds_signed_request(self) -> None:
        session = _FakeSession({
            PAGE_URL: _FakeResponse(_page_html()),
            BASE_TMTS: _tmts(m3u=LOW_M3U),
        })
        with patch("requests.Session", return_value=session):
            R._extract_iqiyi("https://www.iqiyi.com/v_1gu30dui6vo.html", lambda _m: None)

        tmts_calls = [(u, p) for u, p in session.calls if "tmts" in u]
        self.assertTrue(tmts_calls)
        url, params = tmts_calls[0]
        self.assertEqual(url, BASE_TMTS)
        self.assertEqual(params["tvid"], TVID)
        self.assertEqual(params["vid"], PRIMARY_VID)
        self.assertEqual(params["src"], R._IQIYI_TMTS_SRC)
        stamp = int(params["t"])
        self.assertGreater(stamp, 1_600_000_000_000)
        self.assertEqual(
            params["sc"],
            hashlib.md5(f"{stamp}{R._IQIYI_TMTS_KEY}{TVID}".encode()).hexdigest(),
        )
        # tmts 按客户端类型给流，UA 必须是移动端；Referer 也不能少
        self.assertIn("iPhone", session.headers["User-Agent"])
        self.assertEqual(session.headers["Referer"], "https://m.iqiyi.com/")


class QualityCandidateTest(unittest.TestCase):
    def test_orders_by_preference(self) -> None:
        data = {"vidl": [
            {"vid": "low", "vd": "96"},
            {"vid": "hd", "vd": "4"},
            {"vid": "unknown", "vd": "777"},
        ]}
        order = [vd for vd, _ in R._iqiyi_quality_candidates(data)]
        self.assertEqual(order[0], "4")
        self.assertLess(order.index("96"), order.index("777"))

    def test_split_resolution(self) -> None:
        self.assertEqual(R._split_resolution("1280x720"), (1280, 720))
        self.assertEqual(R._split_resolution(""), (0, 0))
        self.assertEqual(R._split_resolution("auto"), (0, 0))


class ExtractIqiyiTest(unittest.TestCase):
    URL = "https://www.iqiyi.com/v_1gu30dui6vo.html?qyTrace=GNGhlS8WOZGAoBKU"

    def test_happy_path_prefers_high_quality(self) -> None:
        session = _FakeSession({
            PAGE_URL: _FakeResponse(_page_html()),
            BASE_TMTS: _tmts(m3u=LOW_M3U, vidl=_vidl()),
            HIGH_TMTS: _tmts(code=2, m3u=HIGH_M3U, vd="4", screen="1280x720"),
        })
        with patch("requests.Session", return_value=session):
            info = R._extract_iqiyi(self.URL, lambda _m: None)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["url"], HIGH_M3U)
        self.assertEqual(info["title"], "余红旧事第1集")
        self.assertEqual(info["id"], TVID)
        self.assertEqual((info["width"], info["height"]), (1280, 720))
        self.assertEqual(info["duration"], 1070.0)
        self.assertEqual(info["extractor_key"], "iqiyi")
        self.assertEqual(info["protocol"], "m3u8_native")
        self.assertEqual(info["http_headers"]["Referer"], "https://m.iqiyi.com/")

        built = R._build_result(self.URL, info)
        self.assertTrue(built.ok)
        self.assertEqual(built.kind.value, "hls")
        self.assertIn("爱奇艺", built.note)

    def test_falls_back_to_default_stream(self) -> None:
        """所有高清晰度档都取不到（code≠2）时，用接口默认那条。"""
        session = _FakeSession({
            PAGE_URL: _FakeResponse(_page_html()),
            BASE_TMTS: _tmts(m3u=LOW_M3U, vidl=_vidl()),
            HIGH_TMTS: _tmts(code=1, m3u="", vd="4"),
        })
        with patch("requests.Session", return_value=session):
            info = R._extract_iqiyi(self.URL, lambda _m: None)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["url"], LOW_M3U)
        self.assertEqual(R._build_result(self.URL, info).ok, True)

    def test_limits_quality_probes(self) -> None:
        """清晰度档试太多次会把"快"耗掉 —— 有上限。"""
        many = [{"vid": f"v{index}", "vd": str(vd)}
                for index, vd in enumerate(R._IQIYI_VD_ORDER)]
        session = _FakeSession({
            PAGE_URL: _FakeResponse(_page_html()),
            BASE_TMTS: _tmts(m3u=LOW_M3U, vidl=many),
        })
        with patch("requests.Session", return_value=session):
            info = R._extract_iqiyi(self.URL, lambda _m: None)
        self.assertIsNotNone(info)
        probes = [u for u, _ in session.calls if "tmts" in u and u != BASE_TMTS]
        self.assertLessEqual(len(probes), R._IQIYI_MAX_QUALITY_TRIES)

    def test_returns_none_on_request_error(self) -> None:
        session = _FakeSession({PAGE_URL: OSError("boom")})
        with patch("requests.Session", return_value=session):
            self.assertIsNone(R._extract_iqiyi(self.URL, lambda _m: None))

    def test_returns_none_without_vid(self) -> None:
        with patch("requests.Session") as factory:
            self.assertIsNone(R._extract_iqiyi("https://www.iqiyi.com/", lambda _m: None))
        factory.assert_not_called()

    def test_returns_none_without_tvid(self) -> None:
        session = _FakeSession({PAGE_URL: _FakeResponse("<html>空页面</html>")})
        with patch("requests.Session", return_value=session):
            self.assertIsNone(R._extract_iqiyi(self.URL, lambda _m: None))


class MemberContentMessageTest(unittest.TestCase):
    """国内站点在会员内容上回报的是中文，不能落成一句没用的"解析失败"。"""

    def test_youku_member_error_becomes_a_clear_hint(self) -> None:
        raw = (
            "[youku] XNjU2NjE5MzI2OA: Youku server reported error -3007: "
            "观看此节目，请先登录！; please report this issue on https://github.com/..."
        )
        message = R.friendly_error(raw)
        self.assertIn("会员", message)
        self.assertIn("Cookie", message)

    def test_english_login_rule_still_works(self) -> None:
        message = R.friendly_error("ERROR: Sign in to confirm you're not a bot")
        self.assertIn("机器人", message)


if __name__ == "__main__":
    unittest.main()
