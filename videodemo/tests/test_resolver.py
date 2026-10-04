"""网页地址解析（src/core/resolver.py）测试。

只测纯逻辑——格式筛选、结果组装、报错翻译。**不联网**：
真实的解析链路由 tools/env_probe.py --resolve 与冒烟测试里的本地页面场景覆盖。

运行:
    ./.venv/Scripts/python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import reachability as RC  # noqa: E402
from src.core import resolver as R  # noqa: E402
from src.core.url_router import UrlKind  # noqa: E402


def m4a(**overrides) -> dict:
    """一条纯音频格式（B 站的 30280）。"""
    base = {
        "format_id": "30280", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
        "abr": 204, "url": "https://cdn.example.com/audio.m4s", "protocol": "https",
    }
    base.update(overrides)
    return base


def video_track(height: int, codec: str, tbr: float, **overrides) -> dict:
    """一条纯画面格式（B 站的 300xx）。"""
    base = {
        "format_id": f"{height}-{codec}", "ext": "mp4", "vcodec": codec, "acodec": "none",
        "width": int(height * 16 / 9), "height": height, "tbr": tbr,
        "url": f"https://cdn.example.com/{height}-{codec}.m4s", "protocol": "https",
    }
    base.update(overrides)
    return base


def progressive(height: int, ext: str = "mp4") -> dict:
    """一条音视频合一的格式（YouTube 的 18、普通网页里的 mp4）。"""
    return {
        "format_id": "18", "ext": ext, "vcodec": "avc1.42001E", "acodec": "mp4a.40.2",
        "width": int(height * 16 / 9), "height": height, "tbr": 500,
        "url": f"https://cdn.example.com/progressive-{height}.{ext}", "protocol": "https",
    }


#: B 站真实形态：全是分离流，一条 progressive 都没有
BILI_FORMATS = [
    m4a(),
    video_track(360, "avc1.64001E", 350),
    video_track(1080, "avc1.640032", 2629),
    video_track(1080, "av01.0.08M.08", 604),
]


class TestCodecSemantics(unittest.TestCase):
    """vcodec/acodec 的三种取值必须区分对待，混了就会把普通 mp4 误判成分离流。"""

    def test_absent_is_explicit_none_string(self) -> None:
        self.assertTrue(R.codec_absent("none"))
        self.assertTrue(R.codec_absent("NONE"))
        self.assertFalse(R.codec_absent(None))
        self.assertFalse(R.codec_absent(""))
        self.assertFalse(R.codec_absent("mp4a.40.2"))

    def test_unknown_is_none_or_empty(self) -> None:
        self.assertFalse(R.codec_known(None))
        self.assertFalse(R.codec_known(""))
        self.assertFalse(R.codec_known("none"))
        self.assertTrue(R.codec_known("avc1.640032"))

    def test_generic_page_is_not_treated_as_separate_stream(self) -> None:
        """普通网页里的 <video src="..."> 抓出来 vcodec/acodec 都是 None。"""
        fmt = {"url": "https://a/b.mp4", "protocol": "http", "vcodec": None, "acodec": None}
        self.assertIs(R.select_single_file([fmt]), fmt)
        self.assertIsNone(R.select_video_only([fmt]))
        self.assertIsNone(R.select_audio_only([fmt]))


class TestPlayableFormat(unittest.TestCase):
    def test_rejects_non_playable(self) -> None:
        cases = {
            "没有地址": {"protocol": "https"},
            "空地址": {"url": "", "protocol": "https"},
            "DRM": {"url": "https://a/b", "protocol": "https", "has_drm": True},
            "要分片下载": {"url": "https://a/b", "protocol": "https", "fragments": [1, 2]},
            "DASH 分片协议": {"url": "https://a/b", "protocol": "http_dash_segments"},
            "协议缺失": {"url": "https://a/b"},
        }
        for name, fmt in cases.items():
            with self.subTest(case=name):
                self.assertFalse(R.is_playable_format(fmt))

    def test_accepts_http_and_hls(self) -> None:
        for protocol in ("https", "http", "m3u8_native", "m3u8"):
            with self.subTest(protocol=protocol):
                self.assertTrue(
                    R.is_playable_format({"url": "https://a/b", "protocol": protocol})
                )


class TestSelection(unittest.TestCase):
    def test_bilibili_has_no_single_file_stream(self) -> None:
        self.assertIsNone(R.select_single_file(BILI_FORMATS))

    def test_bilibili_picks_h264_1080p_over_av1_1080p(self) -> None:
        """同分辨率优先 h264：AV1 一旦缺解码器就是"能解析、没画面"。"""
        picked = R.select_video_only(BILI_FORMATS)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["format_id"], "1080-avc1.640032")

    def test_resolution_beats_codec(self) -> None:
        formats = [
            video_track(1080, "av01.0.08M.08", 604),
            video_track(720, "avc1.640028", 1753),
        ]
        self.assertEqual(R.select_video_only(formats)["height"], 1080)

    def test_picks_best_audio(self) -> None:
        formats = [m4a(abr=44), m4a(format_id="30280", abr=204), m4a(abr=103)]
        self.assertEqual(R.select_audio_only(formats)["abr"], 204)

    def test_single_file_prefers_higher_resolution(self) -> None:
        formats = [progressive(360), progressive(720), progressive(1080)]
        self.assertEqual(R.select_single_file(formats)["height"], 1080)

    def test_single_file_ignores_separate_streams(self) -> None:
        formats = [*BILI_FORMATS, progressive(360)]
        self.assertEqual(R.select_single_file(formats)["height"], 360)

    def test_video_only_ignores_progressive(self) -> None:
        formats = [progressive(1080), video_track(360, "avc1.64001E", 350)]
        self.assertEqual(R.select_video_only(formats)["height"], 360)

    def test_drm_and_unplayable_are_skipped(self) -> None:
        formats = [
            video_track(1080, "avc1.640032", 2629, has_drm=True),
            video_track(720, "avc1.640028", 1753, protocol="http_dash_segments"),
            video_track(360, "avc1.64001E", 350),
        ]
        self.assertEqual(R.select_video_only(formats)["height"], 360)

    def test_empty_input(self) -> None:
        self.assertIsNone(R.select_single_file([]))
        self.assertIsNone(R.select_video_only([]))
        self.assertIsNone(R.select_audio_only([]))


class TestStreamKind(unittest.TestCase):
    def test_hls(self) -> None:
        self.assertIs(R.stream_kind("m3u8_native", "mp4"), UrlKind.HLS)
        self.assertIs(R.stream_kind("https", "m3u8"), UrlKind.HLS)

    def test_direct(self) -> None:
        self.assertIs(R.stream_kind("https", "mp4"), UrlKind.DIRECT_MEDIA)
        self.assertIs(R.stream_kind("http", None), UrlKind.DIRECT_MEDIA)

    def test_live(self) -> None:
        self.assertIs(R.stream_kind("rtsp", None), UrlKind.LIVE_STREAM)
        self.assertIs(R.stream_kind("rtmp", "flv"), UrlKind.LIVE_STREAM)


class TestCandidateFormats(unittest.TestCase):
    def test_top_level_url_becomes_candidate(self) -> None:
        """yt-dlp 选定一条时会把结果写在 info 顶层，它也得参与比较。"""
        info = {
            "url": "https://cdn.example.com/top.mp4",
            "format_id": "top", "ext": "mp4", "vcodec": None, "acodec": None,
            "protocol": "http",
            "formats": [progressive(360)],
        }
        candidates = R._candidate_formats(info)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["url"], "https://cdn.example.com/top.mp4")

    def test_no_top_level_url_when_merge_needed(self) -> None:
        """需要合流时 info 顶层没有 url（有 requested_formats），不该硬塞候选。"""
        info = {
            "requested_formats": [{"format_id": "v"}, {"format_id": "a"}],
            "vcodec": "avc1", "acodec": "mp4a",
            "formats": BILI_FORMATS,
        }
        candidates = R._candidate_formats(info)
        self.assertEqual(len(candidates), len(BILI_FORMATS))


class TestBuildResult(unittest.TestCase):
    def test_single_file_success(self) -> None:
        # 真实情况下 yt-dlp 会把选中格式的元信息一并复制到 info 顶层
        info = {
            "title": "某个视频", "duration": 212.393, "extractor_key": "Generic",
            "url": "https://cdn.example.com/a.mp4", "ext": "mp4",
            "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "protocol": "http",
            "width": 640, "height": 360, "tbr": 500,
            "http_headers": {"Referer": "https://example.com"},
            "formats": [progressive(360)],
        }
        result = R._build_result("https://example.com/page", info)
        self.assertTrue(result.ok)
        self.assertEqual(result.url, "https://cdn.example.com/a.mp4")
        self.assertIs(result.kind, UrlKind.DIRECT_MEDIA)
        self.assertEqual(result.title, "某个视频")
        self.assertEqual(result.duration_ms, 212393)
        self.assertEqual(result.height, 360)
        self.assertEqual(result.headers.get("Referer"), "https://example.com")
        self.assertIn("单文件", result.note)
        self.assertIn("成功", result.report())

    def test_richer_candidate_wins_over_bare_top_level_entry(self) -> None:
        """顶层条目缺少元信息时，formats 里更完整、更清晰的那条应当胜出。"""
        info = {
            "title": "某视频",
            "url": "https://cdn.example.com/bare.mp4", "protocol": "http",
            "formats": [progressive(1080)],
        }
        result = R._build_result("https://example.com/page", info)
        self.assertTrue(result.ok)
        self.assertEqual(result.url, "https://cdn.example.com/progressive-1080.mp4")

    def test_separate_streams_reports_both_urls(self) -> None:
        info = {
            "title": "【官方 MV】Never Gonna Give You Up", "duration": 212.393,
            "extractor_key": "BiliBili", "formats": BILI_FORMATS,
        }
        result = R._build_result("https://www.bilibili.com/video/BVxxx", info)
        self.assertFalse(result.ok)
        self.assertTrue(result.has_separate_streams)
        self.assertIn("分离", result.error)
        self.assertEqual(result.video_url, "https://cdn.example.com/1080-avc1.640032.m4s")
        self.assertEqual(result.audio_url, "https://cdn.example.com/audio.m4s")
        # 降级播放要用到的流类型也得填好
        self.assertIs(result.video_kind, UrlKind.DIRECT_MEDIA)
        # 说明文字必须讲清楚"为什么"和"怎么办"
        self.assertIn("两条独立的流", result.detail)
        self.assertIn("仅播放画面", result.detail)
        self.assertIn("哔哩哔哩", result.video_note)

    def test_separate_streams_carry_referer_headers(self) -> None:
        """分离流也必须带出 http_headers（B 站 upos CDN 严格校验 Referer）。

        回归：早期版本只在「单文件流」分支提取 headers，分离流分支漏了，
        导致 item.http_headers 为空 → 本地代理只能用 CDN 域名瞎猜 Referer
        → 防盗链 403。能播/不能播的 B 站视频差异正在于 CDN 是否校验 Referer。
        """
        referer = "https://www.bilibili.com/video/BVxxx"
        headers = {"Referer": referer, "User-Agent": "Mozilla/5.0 Test"}
        fmts = [
            video_track(1080, "avc1.640033", 2629, http_headers=dict(headers)),
            m4a(http_headers=dict(headers)),
        ]
        info = {
            "title": "小米17ultra", "duration": 75.1,
            "extractor_key": "BiliBili", "formats": fmts,
        }
        result = R._build_result("https://www.bilibili.com/video/BVxxx", info)
        self.assertFalse(result.ok)
        self.assertTrue(result.has_separate_streams)
        self.assertEqual(result.headers.get("Referer"), referer)
        self.assertEqual(result.headers.get("User-Agent"), "Mozilla/5.0 Test")

    def test_separate_streams_fallback_to_top_level_headers(self) -> None:
        """format 没带 http_headers 时，回退到 info 顶层的（单文件流同款逻辑）。"""
        info = {
            "title": "t", "extractor_key": "BiliBili", "formats": BILI_FORMATS,
            "http_headers": {"Referer": "https://www.bilibili.com/"},
        }
        result = R._build_result("https://www.bilibili.com/video/BVxxx", info)
        self.assertFalse(result.ok)
        self.assertTrue(result.has_separate_streams)
        self.assertEqual(result.headers.get("Referer"), "https://www.bilibili.com/")

    def test_live_is_flagged(self) -> None:
        info = {
            "title": "直播中", "is_live": True, "extractor_key": "Generic",
            "url": "https://cdn.example.com/live.m3u8", "ext": "mp4",
            "protocol": "m3u8_native", "vcodec": None, "acodec": None,
            "formats": [],
        }
        result = R._build_result("https://example.com/live", info)
        self.assertTrue(result.ok)
        self.assertTrue(result.is_live)
        self.assertIs(result.kind, UrlKind.HLS)

    def test_playlist_takes_first_entry(self) -> None:
        info = {
            "_type": "playlist",
            "entries": [{
                "title": "第 1 个", "url": "https://cdn.example.com/1.mp4",
                "ext": "mp4", "vcodec": None, "acodec": None, "protocol": "http",
                "formats": [],
            }],
        }
        result = R._build_result("https://example.com/list", info)
        self.assertTrue(result.ok)
        self.assertEqual(result.title, "第 1 个")
        self.assertIn("合集", result.note)

    def test_empty_playlist(self) -> None:
        result = R._build_result("https://example.com/list", {"_type": "playlist"})
        self.assertFalse(result.ok)
        self.assertIn("播放列表", result.error)

    def test_no_usable_stream(self) -> None:
        info = {"title": "空页面", "formats": [{"format_id": "x", "protocol": "https"}]}
        result = R._build_result("https://example.com/page", info)
        self.assertFalse(result.ok)
        self.assertFalse(result.has_separate_streams)
        self.assertIn("没有找到", result.error)

    def test_drm_formats_report_drm_error(self) -> None:
        # 所有流都带 DRM 时，不能笼统报「没有可播放的流」，要明确说 DRM
        info = {
            "title": "DRM 正片",
            "formats": [
                {"url": "https://x/v.m3u8", "format_id": "1", "ext": "m3u8",
                 "protocol": "m3u8_native", "vcodec": "h264", "acodec": "aac",
                 "height": 1080, "has_drm": True},
            ],
        }
        result = R._build_result("https://v.qq.com/x/cover/test.html", info)
        self.assertFalse(result.ok)
        self.assertIn("DRM", result.error)
        self.assertNotIn("没有找到", result.error)

    def test_duration_missing_is_zero(self) -> None:
        info = {"title": "直播", "is_live": True, "formats": []}
        result = R._build_result("https://example.com/live", info)
        self.assertEqual(result.duration_ms, 0)


class TestFriendlyError(unittest.TestCase):
    def test_known_messages(self) -> None:
        cases = {
            "ERROR: Unsupported URL: https://foo": "不认识这个网站",
            "ERROR: [BiliBili] xxx: Requested format is not available": "没有可用格式",
            "ERROR: Private video. Sign in if you've been granted access to this video": "需要登录",
            # YouTube 的机器人校验是**独立**一类：视频完全公开，缺的是反爬校验。
            # 措辞必须和"需要登录"区分开，否则用户会去查"是不是会员视频"。
            "ERROR: [youtube] abc: Sign in to confirm you’re not a bot.": "机器人",
            "ERROR: This video is not available in your country": "地区",
            "ERROR: Unable to download webpage: HTTP Error 404": "404",
            "ERROR: Unable to download webpage: [Errno 11001] getaddrinfo failed": "网络",
            "ERROR: unable to extract initial state": "页面结构",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertIn(expected, R.friendly_error(raw))

    def test_bot_check_wins_over_generic_login(self) -> None:
        """机器人校验的原文里带 "sign in" 和 "cookies"，不能被通用「需要登录」吃掉。

        落到通用规则会得出"该视频需要登录身份"的结论，把用户引到
        "这是会员/私密视频"的错方向——而视频其实是完全公开的。
        """
        message = R.friendly_error(
            "ERROR: [youtube] abc: Sign in to confirm you're not a bot. "
            "Use --cookies-from-browser or --cookies for the authentication."
        )
        self.assertIn("机器人", message)
        # 实测结论：判的是出口 IP 的声誉，换客户端参数无效，有效手段是换出口。
        # 所以文案必须把"换代理节点"放在第一位，而不是引去开浏览器 cookie
        # （Windows 上新版 Chrome/Edge 的 cookie 已经读不出来了）。
        self.assertIn("代理节点", message)
        self.assertNotIn("需要登录身份", message)

    def test_anonymous_cookie_hint(self) -> None:
        """抖音/西瓜要的是「匿名新鲜 cookie」，不能误报成"会员/私密"。"""
        for raw in (
            "Fresh cookies (not necessarily logged in) are needed",
            "Cookies (not necessarily logged in) are needed",
        ):
            with self.subTest(raw=raw):
                message = R.friendly_error(raw)
                self.assertIn("Cookie", message)
                self.assertIn("匿名", message)
                self.assertNotIn("会员", message)

    def test_deleted_beats_geo_restricted(self) -> None:
        """B 站 "deleted or geo-restricted" 应报"已删除"，不是"地区不可用"。"""
        raw = "This video may be deleted or geo-restricted"
        self.assertIn("已删除", R.friendly_error(raw))
        self.assertNotIn("地区", R.friendly_error(raw))

    def test_live_offline(self) -> None:
        """直播离线/未开播应给出明确提示，而不是落兜底。"""
        for raw in ("Video is offline", "Streamer is not live"):
            with self.subTest(raw=raw):
                self.assertIn("直播", R.friendly_error(raw))

    def test_no_formats_hint(self) -> None:
        """小红书「没抓到格式」应提示可能需登录。"""
        self.assertIn("视频格式", R.friendly_error("No video formats found"))

    def test_unknown_falls_back(self) -> None:
        self.assertIn("解析失败", R.friendly_error("something entirely unexpected"))

    def test_first_line_cleans_prefix(self) -> None:
        raw = "ERROR: 第一行\n第二行"
        self.assertEqual(R._first_line(raw), "第一行")

    def test_install_hint_is_actionable(self) -> None:
        self.assertIn("pip install yt-dlp", R.INSTALL_HINT)


class TestResolveGuards(unittest.TestCase):
    def test_empty_url(self) -> None:
        result = R.resolve("")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "地址为空")

    def test_whitespace_url(self) -> None:
        self.assertFalse(R.resolve("   ").ok)

    def test_availability_probe_is_truthful(self) -> None:
        # yt-dlp 是本项目的 P2 依赖，测试环境里应当可用
        if R.is_available():
            self.assertTrue(R.version())
            self.assertIn("可用", R.describe_availability())
        else:
            self.assertIn("不可用", R.describe_availability())


class TestRetryDecision(unittest.TestCase):
    """`is_retryable`：只对"换种方式有可能绕过去"的失败重试。"""

    RETRY = {
        "Sign in to confirm you're not a bot",
        "HTTP Error 403: Forbidden",
        "Unable to extract player response",
        "Requested format is not available",
        "no video formats found",
        # 连接被重置很可能是按 TLS 指纹拦的，换指纹能过 —— 必须重试
        "Cannot connect: Connection was reset by peer",
        "('Connection aborted.', ConnectionResetError(10054, '远程主机强迫关闭了一个现有的连接。'))",
        "Broken pipe",
    }
    NO_RETRY = {
        "Unable to download webpage: timed out",
        "Unable to download webpage: [Errno 11001] getaddrinfo failed",
        "unreachable network",
        "HTTP Error 404: Not Found",
        "This video has been removed",
        "Unsupported URL: https://foo",
        "This video is DRM protected",
        "This video is not available in your country",
        "Private video. Sign in if you've been granted access",
        "",
    }

    def test_retryable(self) -> None:
        for raw in sorted(self.RETRY):
            with self.subTest(raw=raw):
                self.assertTrue(R.is_retryable(raw), raw)

    def test_not_retryable(self) -> None:
        for raw in sorted(self.NO_RETRY):
            with self.subTest(raw=raw):
                self.assertFalse(R.is_retryable(raw), raw)

    def test_reset_outranks_network(self) -> None:
        """重置文本里带 "connection"，不能被"网络类"先吃掉判成不重试。"""
        message = "Unable to download webpage: ('Connection aborted.', ConnectionResetError(10054))"
        self.assertIn("connection", message.lower())
        self.assertTrue(R.is_retryable(message))

    def test_reset_has_its_own_message(self) -> None:
        message = R.friendly_error("Connection was reset by peer")
        self.assertIn("断开", message)
        self.assertNotIn("超时：请检查网络", message)


class TestStrategies(unittest.TestCase):
    """解析尝试序列。第一条永远是默认配置。"""

    def setUp(self) -> None:
        self._saved_impersonate = R._impersonate_cache
        self._saved_option = R._impersonate_option_cache
        R._impersonate_cache = True

    def tearDown(self) -> None:
        R._impersonate_cache = self._saved_impersonate
        R._impersonate_option_cache = self._saved_option

    def test_first_is_always_default(self) -> None:
        for url in ("https://www.youtube.com/watch?v=x", "https://vimeo.com/1"):
            with self.subTest(url=url):
                specs = R.strategies_for(url)
                self.assertEqual(specs[0].label, "默认配置")
                self.assertEqual(specs[0].options, {})

    def test_disabled_returns_only_default(self) -> None:
        specs = R.strategies_for("https://www.youtube.com/watch?v=x", enabled=False)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].label, "默认配置")

    def test_youtube_gets_alternate_player_clients(self) -> None:
        specs = R.strategies_for("https://www.youtube.com/watch?v=x")
        labels = [s.label for s in specs]
        self.assertIn("备用播放器接口", labels)
        spec = next(s for s in specs if s.label == "备用播放器接口")
        clients = spec.options["extractor_args"]["youtube"]["player_client"]
        self.assertTrue(clients)
        # 默认集合里已有的 client 再试一遍没有意义，所以备用清单不能包含 'default'
        self.assertNotIn("default", clients)

    def test_youtu_be_counts_as_youtube(self) -> None:
        specs = R.strategies_for("https://youtu.be/abc")
        self.assertIn("备用播放器接口", [s.label for s in specs])

    def test_other_sites_have_no_player_client_strategy(self) -> None:
        specs = R.strategies_for("https://www.bilibili.com/video/BV1xx")
        self.assertNotIn("备用播放器接口", [s.label for s in specs])

    def test_referer_is_derived_from_origin(self) -> None:
        specs = R.strategies_for("https://vimeo.com/12345?x=1")
        spec = next(s for s in specs if s.label == "补发来源页")
        self.assertEqual(spec.options["http_headers"]["Referer"], "https://vimeo.com/")

    def test_no_referer_strategy_without_origin(self) -> None:
        specs = R.strategies_for("not a url")
        self.assertNotIn("补发来源页", [s.label for s in specs])

    def test_impersonation_strategy_follows_availability(self) -> None:
        self.assertIn("浏览器指纹伪装",
                      [s.label for s in R.strategies_for("https://vimeo.com/1")])
        R._impersonate_cache = False
        self.assertNotIn("浏览器指纹伪装",
                         [s.label for s in R.strategies_for("https://vimeo.com/1")])

    def test_impersonation_option_must_be_ytdlp_target_object(self) -> None:
        """`impersonate` 必须是**单个 `ImpersonateTarget` 对象**。

        这是 yt-dlp 的 API 契约：走命令行时它自己会把 `--impersonate chrome`
        转成对象（`yt_dlp/__init__.py`），走 Python API 则不做转换。而
        `YoutubeDL.__init__` 拿这个值直接调 `is_supported_target()`，里头第一行是
        `assert isinstance(target, ImpersonateTarget)`——所以传字符串、或传对象
        **列表**都会在构造阶段抛空 `AssertionError`：该策略一次都没成功过，还会
        把真正的原因（如"需要登录"）从最终报错里挤掉。
        """
        spec = next(s for s in R.strategies_for("https://vimeo.com/1")
                    if s.label == "浏览器指纹伪装")
        target = spec.options["impersonate"]

        from yt_dlp.networking.impersonate import ImpersonateTarget

        self.assertIsInstance(target, ImpersonateTarget)
        self.assertNotIsInstance(target, (str, list, tuple))
        self.assertEqual(target.client, "chrome")

    def test_impersonation_option_survives_youtubedl_construction(self) -> None:
        """真正拿 yt-dlp 构造一次：不崩，且目标被解析成了具体版本。"""
        option = R._impersonate_option()
        if not option:
            self.skipTest("当前环境没有可用的伪装目标（缺 curl_cffi）")

        from yt_dlp import YoutubeDL

        with YoutubeDL({"quiet": True, "skip_download": True, **option}) as ydl:
            self.assertEqual(str(ydl.params["impersonate"]), "chrome")

    def test_impersonation_option_is_cached(self) -> None:
        self.assertIs(R._impersonate_option(), R._impersonate_option())
        R.reset_impersonation_cache()
        self.assertIsNone(R._impersonate_option_cache)


class TestJsRuntime(unittest.TestCase):
    """JS 运行时探测。2026 版 yt-dlp 没有它会静默少拿 YouTube 格式。"""

    def setUp(self) -> None:
        R.reset_js_runtime_cache()

    def tearDown(self) -> None:
        R.reset_js_runtime_cache()

    def test_auto_detect_returns_dict(self) -> None:
        found = R.detect_js_runtime()
        self.assertIsInstance(found, dict)
        for name, config in found.items():
            self.assertIn(name, R._JS_RUNTIMES)
            self.assertIsInstance(config, dict)

    def test_result_is_cached(self) -> None:
        self.assertIs(R.detect_js_runtime(), R.detect_js_runtime())

    def test_explicit_none_disables(self) -> None:
        from unittest import mock

        with mock.patch("src.utils.settings.get_str", return_value="none"):
            self.assertEqual(R.detect_js_runtime(), {})

    def test_bogus_runtime_is_ignored(self) -> None:
        from unittest import mock

        with mock.patch("src.utils.settings.get_str", return_value="definitely-not-a-runtime"):
            self.assertEqual(R.detect_js_runtime(), {})

    def test_missing_path_is_ignored(self) -> None:
        from unittest import mock

        with mock.patch("src.utils.settings.get_str",
                        return_value=r"node:C:\nope\missing\node.exe"):
            self.assertEqual(R.detect_js_runtime(), {})

    def test_reaches_ytdlp_as_js_runtimes_key(self) -> None:
        """探测到还不够——必须以 `js_runtimes` 这个键交到 yt-dlp 手里。

        踩过的坑：这里曾写成 ``network.update(detect_js_runtime())``，运行时名
        被当成顶层选项名，yt-dlp 收不到，于是日志说"启用 node"、它自己却报
        "没有可用的 JavaScript 运行时"。
        """
        from unittest import mock

        captured: dict = {}

        def capture(_url, options, _budget):
            captured.update(options)
            return R._Attempt(status="error", error=RuntimeError("boom"))

        with mock.patch("src.utils.settings.resolve_preflight_enabled", return_value=False), \
             mock.patch("src.utils.settings.resolve_retry_strategies_enabled",
                        return_value=False), \
             mock.patch.object(R, "detect_js_runtime", return_value={"node": {}}), \
             mock.patch.object(R, "_run_attempt", side_effect=capture):
            R.resolve("https://example.com/page", timeout_s=5)

        self.assertEqual(captured.get("js_runtimes"), {"node": {}})
        self.assertNotIn("node", captured)

    def test_absent_runtime_adds_no_key(self) -> None:
        """探测不到就干脆不传，让 yt-dlp 用它的默认值——别塞个空字典进去。"""
        from unittest import mock

        captured: dict = {}

        def capture(_url, options, _budget):
            captured.update(options)
            return R._Attempt(status="error", error=RuntimeError("boom"))

        with mock.patch("src.utils.settings.resolve_preflight_enabled", return_value=False), \
             mock.patch("src.utils.settings.resolve_retry_strategies_enabled",
                        return_value=False), \
             mock.patch.object(R, "detect_js_runtime", return_value={}), \
             mock.patch.object(R, "_run_attempt", side_effect=capture):
            R.resolve("https://example.com/page", timeout_s=5)

        self.assertNotIn("js_runtimes", captured)


class TestErrorReporting(unittest.TestCase):
    """多策略全失败时，展示给用户的那条报错必须是**最有信息量**的一条。

    真实事故：YouTube 五条策略跑完，用户看到的是最后一条策略抛出的光秃秃
    `AssertionError`；而第一条早就说清了"需要确认不是机器人"。用户顺着
    `AssertionError` 去查，方向全反。
    """

    def _run_with(self, errors: list) -> object:
        from unittest import mock

        calls: list[int] = []

        def attempt(_url, _options, _budget):
            index = len(calls)
            calls.append(index)
            exc = errors[min(index, len(errors) - 1)]
            return R._Attempt(status="error", error=exc)

        with mock.patch("src.utils.settings.resolve_preflight_enabled",
                        return_value=False), \
             mock.patch("src.utils.settings.resolve_retry_strategies_enabled",
                        return_value=True), \
             mock.patch.object(R, "detect_js_runtime", return_value={}), \
             mock.patch.object(R, "streamlink_available", return_value=False), \
             mock.patch.object(R, "_run_attempt", side_effect=attempt):
            return R.resolve("https://youtu.be/abc", timeout_s=30)

    def test_internal_crash_does_not_mask_site_reason(self) -> None:
        result = self._run_with([
            RuntimeError("ERROR: [youtube] abc: Sign in to confirm you're not a bot."),
            AssertionError(),
        ])
        self.assertFalse(result.ok)
        self.assertIn("机器人", result.error)
        self.assertNotIn("AssertionError", result.error)
        self.assertIn("you're not a bot", result.detail)

    def test_internal_bug_alone_is_reported_as_a_program_fault(self) -> None:
        """只剩内部崩溃时也要如实说"不是站点问题"，别让用户去查站点。"""
        result = self._run_with([AssertionError()])
        self.assertFalse(result.ok)
        self.assertIn("内部出错", result.error)
        self.assertIn("不是站点问题", result.error)

    def test_missing_js_runtime_is_mentioned_in_detail(self) -> None:
        """没有 JS 运行时时要在失败详情里说清——这在本机是真实现状。"""
        result = self._run_with([RuntimeError("ERROR: [youtube] abc: Video unavailable")])
        self.assertFalse(result.ok)
        self.assertIn("JavaScript 运行时", result.detail)


class TestInternalBugDetection(unittest.TestCase):
    def test_bare_exception_type_counts_as_internal(self) -> None:
        for raw in ("AssertionError", "TypeError", "AttributeError"):
            with self.subTest(raw=raw):
                self.assertTrue(R._is_internal_bug(raw))

    def test_exception_with_message_is_not_internal(self) -> None:
        """带消息的多半是 yt-dlp 主动抛的，得让原文透出去。"""
        for raw in ("AssertionError: something specific",
                    "ERROR: [youtube] x: Video unavailable",
                    ""):
            with self.subTest(raw=raw):
                self.assertFalse(R._is_internal_bug(raw))

    def test_rank_prefers_actionable_reasons(self) -> None:
        self.assertGreater(R._error_rank("ERROR: x: Video unavailable"),
                           R._error_rank("ERROR: x: Sign in to confirm you're not a bot"))
        self.assertGreater(R._error_rank("ERROR: x: Sign in to confirm you're not a bot"),
                           R._error_rank("AssertionError"))
        self.assertGreater(R._error_rank("AssertionError"), R._error_rank(""))

    def test_internal_bug_is_retryable(self) -> None:
        """我们自己的参数炸了，跟站点无关——换下一条策略还有机会。"""
        self.assertTrue(R.is_retryable("AssertionError"))
        self.assertTrue(R.is_retryable("TypeError"))


class TestBudgetAndMerge(unittest.TestCase):
    def test_last_attempt_gets_everything(self) -> None:
        self.assertEqual(R._budget_for(20.0, 1), 20.0)

    def test_earlier_attempts_leave_a_reserve(self) -> None:
        """第一条策略不能把预算耗光，否则后面本来能救回来的重试永远轮不到。"""
        budget = R._budget_for(20.0, 3)
        self.assertLess(budget, 20.0)
        self.assertGreaterEqual(20.0 - budget, 1.0)

    def test_budget_never_goes_to_zero(self) -> None:
        self.assertGreaterEqual(R._budget_for(3.0, 4), 2.0)

    def test_headers_are_merged_not_replaced(self) -> None:
        """策略只想改 UA，不该顺手把用户填的 Referer 丢掉。"""
        base = R._base_options(lambda _m: None, False)
        network = {"http_headers": {"Referer": "https://user.example/"}}
        spec = R.Strategy("x", {"http_headers": {"User-Agent": "UA"}})
        options = R._merge_options(base, network, spec)
        self.assertEqual(options["http_headers"]["Referer"], "https://user.example/")
        self.assertEqual(options["http_headers"]["User-Agent"], "UA")

    def test_strategy_scalar_overrides_network(self) -> None:
        base = R._base_options(lambda _m: None, False)
        options = R._merge_options(base, {"socket_timeout": 10},
                                   R.Strategy("x", {"socket_timeout": 3}))
        self.assertEqual(options["socket_timeout"], 3)

    def test_base_options_never_download(self) -> None:
        base = R._base_options(lambda _m: None, False)
        self.assertTrue(base["skip_download"])
        self.assertTrue(base["no_color"])
        self.assertFalse(base["cachedir"])   # 打包后目录可能只读


class TestPreflightInResolve(unittest.TestCase):
    """解析前的可达性预检：站点连不上时立刻收工，不烧满超时。"""

    @staticmethod
    def _blocked(url: str, **_kwargs) -> RC.Reachability:
        pre = RC.Reachability(url=url, host="www.youtube.com", port=443,
                              dns_ok=True, ips=("31.13.92.37",), tcp_ok=False)
        pre.error = "网络不可达"
        pre.headline = "连不上 www.youtube.com：域名能解析，但网络层不通"
        pre.detail = "系统把 www.youtube.com 解析到 31.13.92.37，TCP 连不上。"
        pre.advice = ("在设置里填写可用的 HTTP 代理，再重试",)
        return pre

    def test_blocked_site_fails_fast_without_extracting(self) -> None:
        from unittest import mock

        def explode(*_a, **_k):
            raise AssertionError("站点不可达时不该再去跑 yt-dlp")

        # 把代理探测钉成"没有代理在生效"：这条用例的前提就是裸 TCP 有意义。
        # （本项目的开发沙箱里 HTTP_PROXY 常年有值，不钉住的话测的就不是本意了。）
        with mock.patch("src.utils.settings.resolve_preflight_enabled", return_value=True), \
             mock.patch("src.utils.settings.doh_fallback_enabled", return_value=True), \
             mock.patch("src.utils.net_proxy.proxy_for", return_value=None), \
             mock.patch("src.core.reachability.probe", side_effect=self._blocked), \
             mock.patch.object(R, "_run_attempt", side_effect=explode):
            result = R.resolve("https://www.youtube.com/watch?v=x")

        self.assertFalse(result.ok)
        self.assertIn("连不上", result.error)
        # 结论与可尝试的做法都要带给用户，不能只说"失败"
        self.assertIn("TCP", result.detail)
        self.assertIn("代理", result.detail)

    def test_preflight_skipped_when_proxy_configured(self) -> None:
        """走代理时"直连源站通不通"与能否播放无关，拿它判失败会误杀。"""
        from unittest import mock

        called = []

        with mock.patch("src.utils.settings.resolve_preflight_enabled", return_value=True), \
             mock.patch("src.core.reachability.probe",
                        side_effect=lambda *a, **k: called.append(a) or None), \
             mock.patch.object(R, "_run_attempt",
                               return_value=R._Attempt(status="timeout")):
            R.resolve("https://example.com/page", timeout_s=1,
                      proxy="http://127.0.0.1:7890")

        self.assertEqual(called, [])

    def test_preflight_skipped_when_system_proxy_in_effect(self) -> None:
        """Clash 这类"系统代理"最容易被漏掉。

        它只改 Windows 系统代理、不改变裸 TCP，于是被墙站点直连必然超时，
        预检会据此把"经代理明明能播"的地址误杀。而 yt-dlp 读的正是同一个
        系统代理——两边结论必须一致。
        """
        from unittest import mock

        called = []

        with mock.patch("src.utils.settings.resolve_preflight_enabled", return_value=True), \
             mock.patch("src.utils.net_proxy.env_proxy", return_value=""), \
             mock.patch("src.utils.net_proxy.system_proxy",
                        return_value="http://127.0.0.1:7897"), \
             mock.patch("src.utils.net_proxy._bypassed", return_value=False), \
             mock.patch("src.core.reachability.probe",
                        side_effect=lambda *a, **k: called.append(a) or None), \
             mock.patch.object(R, "_run_attempt",
                               return_value=R._Attempt(status="timeout")):
            R.resolve("https://www.youtube.com/watch?v=x", timeout_s=1)

        self.assertEqual(called, [])

    def test_preflight_disabled_setting(self) -> None:
        from unittest import mock

        called = []

        with mock.patch("src.utils.settings.resolve_preflight_enabled", return_value=False), \
             mock.patch("src.core.reachability.probe",
                        side_effect=lambda *a, **k: called.append(a) or None), \
             mock.patch.object(R, "_run_attempt",
                               return_value=R._Attempt(status="timeout")):
            R.resolve("https://example.com/page", timeout_s=1)

        self.assertEqual(called, [])

    def test_retry_stops_on_non_retryable_error(self) -> None:
        """不可重试的失败不该把后面几种策略都跑一遍。"""
        from unittest import mock

        attempts = []

        def record(_url, _options, _budget):
            attempts.append(1)
            return R._Attempt(status="error",
                              error=RuntimeError("HTTP Error 404: Not Found"))

        with mock.patch("src.utils.settings.resolve_preflight_enabled", return_value=False), \
             mock.patch("src.utils.settings.resolve_retry_strategies_enabled",
                        return_value=True), \
             mock.patch.object(R, "_run_attempt", side_effect=record):
            result = R.resolve("https://vimeo.com/1", timeout_s=30)

        self.assertFalse(result.ok)
        self.assertEqual(len(attempts), 1)
        self.assertIn("404", result.error)

    def test_retry_continues_on_retryable_error(self) -> None:
        """被站点拒绝时要把剩下的策略走完。"""
        from unittest import mock

        attempts = []

        def record(_url, options, _budget):
            attempts.append(options)
            if len(attempts) == 1:
                return R._Attempt(status="error", error=RuntimeError(
                    "Sign in to confirm you're not a bot"))
            return R._Attempt(status="ok", info={
                "title": "救回来了", "extractor_key": "Generic",
                "url": "https://cdn.example.com/a.mp4", "ext": "mp4",
                "vcodec": None, "acodec": None, "protocol": "http",
                "height": 720, "formats": [],
            })

        with mock.patch("src.utils.settings.resolve_preflight_enabled", return_value=False), \
             mock.patch("src.utils.settings.resolve_retry_strategies_enabled",
                        return_value=True), \
             mock.patch.object(R, "_run_attempt", side_effect=record):
            result = R.resolve("https://vimeo.com/1", timeout_s=30)

        self.assertTrue(result.ok)
        self.assertGreater(len(attempts), 1)
        # 成功时必须说明是"换了种方式才拿到的"，将来站点改版时能直接照着看
        self.assertIn("改用", result.note)


class TestCookieBrowser(unittest.TestCase):
    """从浏览器读 cookie（cookiesfrombrowser）的取值与优先级。"""

    def _options(self, browser="", cookie_file=""):
        from unittest import mock

        with mock.patch.object(R, "_cookie_browser", return_value=browser), \
             mock.patch("src.utils.settings.get_str",
                        side_effect=lambda k, d="": {
                            "network/cookie_file": cookie_file,
                            "network/proxy": "",
                            "network/user_agent": "",
                        }.get(k, d)):
            return R._network_options(None, None, None)

    def test_no_browser_no_cookie(self) -> None:
        opts = self._options(browser="", cookie_file="")
        self.assertNotIn("cookiesfrombrowser", opts)
        self.assertNotIn("cookiefile", opts)

    def test_browser_selected(self) -> None:
        opts = self._options(browser="chrome", cookie_file="")
        self.assertEqual(opts.get("cookiesfrombrowser"), ("chrome", None, None, None))

    def test_cookie_file_wins_over_browser(self) -> None:
        """显式 cookie 文件优先于浏览器读取。"""
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"# Netscape HTTP Cookie File\n")
            path = f.name
        try:
            opts = self._options(browser="chrome", cookie_file=path)
            self.assertIn("cookiefile", opts)
            self.assertNotIn("cookiesfrombrowser", opts)
        finally:
            Path(path).unlink()

    def test_bogus_browser_is_ignored(self) -> None:
        """白名单校验在 _cookie_browser 里：非法浏览器名返回空串。"""
        from unittest import mock

        with mock.patch("src.utils.settings.get_str", return_value="not-a-browser"):
            self.assertEqual(R._cookie_browser(), "")

    def test_cookie_browser_accepts_known_names(self) -> None:
        from unittest import mock

        for name in ("chrome", "edge", "firefox", "brave", "opera", "vivaldi"):
            with mock.patch("src.utils.settings.get_str", return_value=name):
                self.assertEqual(R._cookie_browser(), name)

    def test_cookie_browser_empty_when_unset(self) -> None:
        from unittest import mock

        with mock.patch("src.utils.settings.get_str", return_value=""):
            self.assertEqual(R._cookie_browser(), "")


class TestStreamlinkFallback(unittest.TestCase):
    """streamlink 作为 yt-dlp 失败后的降级解析器。"""

    def test_availability_probe(self) -> None:
        # 只是探测，不能因为装了/没装就断言具体值——但要能调用不抛异常
        value = R.streamlink_available()
        self.assertIsInstance(value, bool)
        self.assertIsInstance(R.describe_streamlink(), str)

    def test_missing_streamlink_returns_none(self) -> None:
        from unittest import mock

        with mock.patch.object(R, "streamlink_available", return_value=False):
            self.assertIsNone(R._streamlink_info("https://example.com/a", lambda m: None))

    def test_plugin_error_returns_none(self) -> None:
        from unittest import mock

        import streamlink

        with mock.patch.object(R, "streamlink_available", return_value=True), \
             mock.patch("streamlink.streams",
                        side_effect=streamlink.NoPluginError("https://example.com")):
            self.assertIsNone(R._streamlink_info("https://example.com/a", lambda m: None))

    def test_http_stream_becomes_playable_info(self) -> None:
        from unittest import mock

        fake_stream = mock.Mock()
        fake_stream.url = "https://cdn.example.com/live.mp4"
        fake_stream.headers = {"Referer": "https://example.com/"}
        fake_stream.to_manifest_url.return_value = "https://cdn.example.com/live.mp4"

        streams = {"best": fake_stream}
        with mock.patch.object(R, "streamlink_available", return_value=True), \
             mock.patch("streamlink.streams", return_value=streams):
            info = R._streamlink_info("https://example.com/video", lambda m: None)

        self.assertIsNotNone(info)
        self.assertEqual(info["url"], "https://cdn.example.com/live.mp4")
        self.assertEqual(info["http_headers"].get("Referer"), "https://example.com/")

    def test_resolve_falls_back_to_streamlink(self) -> None:
        """yt-dlp 失败时，应当尝试 streamlink，成功则返回可播放结果。"""
        from unittest import mock

        def fail_attempt(_url, _options, _budget):
            return R._Attempt(status="error", error=RuntimeError("Unsupported URL"))

        def fake_streamlink_info(_url, _sink):
            return {
                "url": "https://cdn.example.com/live.mp4", "ext": "mp4",
                "protocol": "http", "vcodec": None, "acodec": None,
                "http_headers": {}, "extractor_key": "streamlink", "title": "",
                "is_live": False,
            }

        with mock.patch("src.utils.settings.resolve_preflight_enabled", return_value=False), \
             mock.patch("src.utils.settings.resolve_retry_strategies_enabled",
                        return_value=True), \
             mock.patch.object(R, "_run_attempt", side_effect=fail_attempt), \
             mock.patch.object(R, "streamlink_available", return_value=True), \
             mock.patch.object(R, "_streamlink_info", side_effect=fake_streamlink_info):
            result = R.resolve("https://example.com/video", timeout_s=30)

        self.assertTrue(result.ok)
        self.assertIn("streamlink", result.note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
