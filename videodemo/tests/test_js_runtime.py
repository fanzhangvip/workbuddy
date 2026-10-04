"""内置 JavaScript 运行时的定位。

## 这一组测试守的是什么

守的是"**开发机能跑、用户机器不能跑**"这类只在打包版暴露的回归。

真实事故：整台机器上唯一的 node 来自工具运行时、不在持久 PATH 里。源码运行时
`shutil.which("node")` 找得到，用户双击 exe 时找不到——于是 YouTube 退化成
"无 JS"接口、报"需要确认不是机器人"而解析失败，而开发会话里一切正常。

所以下面既测"内置优先"，也测"PATH 里什么都没有时至少还能靠内置的兜住"。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from src.utils import js_runtime


class BundledRuntimeTest(unittest.TestCase):
    def test_bundled_file_is_the_expected_name(self) -> None:
        self.assertEqual(js_runtime.BUNDLED_EXE, "qjs.exe")
        # 名字必须落在 yt-dlp 认识的集合里，否则整条 js_runtimes 会被忽略
        self.assertIn(js_runtime.BUNDLED_NAME, js_runtime.PATH_RUNTIMES)

    def test_found_in_repo_layout(self) -> None:
        """仓库里就要有这一份——没有它打包版就没有 JS 运行时。"""
        found = js_runtime.find_bundled()
        self.assertIsNotNone(
            found, "jsrt/qjs.exe 不存在：打包版会退化成没有 JS 运行时"
        )
        self.assertTrue(Path(found).is_file())
        self.assertEqual(Path(found).name, js_runtime.BUNDLED_EXE)

    def test_search_dirs_cover_source_and_frozen(self) -> None:
        with mock.patch("src.utils.paths.app_root", return_value=Path("C:/app")), \
             mock.patch("src.utils.paths.is_frozen", return_value=False):
            self.assertEqual(js_runtime._search_dirs(),  # noqa: SLF001
                             [Path("C:/app/jsrt"), Path("C:/app")])

        with mock.patch("src.utils.paths.app_root", return_value=Path("C:/app")), \
             mock.patch("src.utils.paths.is_frozen", return_value=True), \
             mock.patch("sys._MEIPASS", "C:/tmp/_MEI123", create=True):
            dirs = js_runtime._search_dirs()  # noqa: SLF001
        # onefile 会把二进制解压到 _MEIPASS，那里的 jsrt/ 也得查
        self.assertIn(Path("C:/tmp/_MEI123/jsrt"), dirs)
        self.assertIn(Path("C:/tmp/_MEI123"), dirs)


class AutoSelectTest(unittest.TestCase):
    def test_bundled_is_included(self) -> None:
        found = js_runtime.auto_select()
        self.assertIn(js_runtime.BUNDLED_NAME, found)
        config = found[js_runtime.BUNDLED_NAME]
        self.assertTrue(Path(config["path"]).is_file())

    def test_bundled_survives_a_bare_path(self) -> None:
        """用户机器上没有 deno/node/bun —— 只剩内置的那份也必须能交上去。"""
        with mock.patch("shutil.which", return_value=None):
            found = js_runtime.auto_select()
        self.assertIn(js_runtime.BUNDLED_NAME, found)

    def test_path_runtimes_are_added_alongside(self) -> None:
        """装了 node 就用 node（yt-dlp 自己按偏好选），内置的留着兜底。"""
        fake = {name: f"C:/fake/{name}.exe" for name in js_runtime.PATH_RUNTIMES}

        with mock.patch("shutil.which", side_effect=lambda name: fake.get(name)):
            found = js_runtime.auto_select()

        self.assertIn("node", found)
        self.assertIn(js_runtime.BUNDLED_NAME, found)
        self.assertEqual(found["node"]["path"], "C:/fake/node.exe")

    def test_returns_empty_when_nothing_available(self) -> None:
        """一个都没有时返回空字典（沿用 yt-dlp 默认），**不能抛异常**。"""
        with mock.patch.object(js_runtime, "find_bundled", return_value=None), \
             mock.patch("shutil.which", return_value=None):
            self.assertEqual(js_runtime.auto_select(), {})

    def test_missing_hint_is_honest_about_youtube(self) -> None:
        """提示不能写成"不影响多数站点"——实际就是 YouTube 直接失败。"""
        self.assertIn("YouTube", js_runtime.MISSING_HINT)
        self.assertIn("机器人", js_runtime.MISSING_HINT)


class ResolverIntegrationTest(unittest.TestCase):
    """resolver 那边的接线：内置的那份要真的被交到 yt-dlp 手里。"""

    def setUp(self) -> None:
        from src.core import resolver

        self.R = resolver
        resolver.reset_js_runtime_cache()

    def tearDown(self) -> None:
        self.R.reset_js_runtime_cache()

    def test_auto_mode_uses_the_bundled_runtime(self) -> None:
        from unittest import mock

        with mock.patch("src.utils.settings.get_str", return_value=""), \
             mock.patch("shutil.which", return_value=None):
            found = self.R.detect_js_runtime()
        self.assertIn(js_runtime.BUNDLED_NAME, found)
        self.assertTrue(Path(found[js_runtime.BUNDLED_NAME]["path"]).is_file())

    def test_bundled_path_reaches_ytdlp_as_js_runtimes(self) -> None:
        """必须以 `js_runtimes` 这个键交出去，且路径指向内置的那份。"""
        from unittest import mock

        captured: dict = {}

        def capture(_url, options, _budget):
            captured.update(options)
            return self.R._Attempt(status="error", error=RuntimeError("boom"))  # noqa: SLF001

        with mock.patch("src.utils.settings.resolve_preflight_enabled", return_value=False), \
             mock.patch("src.utils.settings.resolve_retry_strategies_enabled",
                        return_value=False), \
             mock.patch("src.utils.settings.get_str", return_value=""), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch.object(self.R, "_run_attempt", side_effect=capture):
            self.R.resolve("https://example.com/page", timeout_s=5)

        payload = captured.get("js_runtimes")
        self.assertIsInstance(payload, dict)
        self.assertIn(js_runtime.BUNDLED_NAME, payload)
        self.assertTrue(Path(payload[js_runtime.BUNDLED_NAME]["path"]).is_file())


class YtdlpAcceptsBundledRuntimeTest(unittest.TestCase):
    """最终裁判是 yt-dlp 自己：它得认得这个二进制、并且报告"可用"。

    只测"文件在不在"是不够的——二进制可能是别的架构、也可能版本太旧而被
    yt-dlp 判为 unsupported，那样等于没内置。
    """

    def test_ytdlp_reports_bundled_runtime_as_supported(self) -> None:
        path = js_runtime.find_bundled()
        if path is None:
            self.skipTest("没有内置运行时")

        try:
            from yt_dlp.utils._jsruntime import QuickJsRuntime
        except ImportError:  # pragma: no cover - yt-dlp 缺了就别测了
            self.skipTest("当前 yt-dlp 不支持 quickjs")

        info = QuickJsRuntime(str(path)).info
        self.assertIsNotNone(info, "yt-dlp 没能识别这个运行时（版本/架构不对？）")
        self.assertTrue(info.supported, f"yt-dlp 认为它不可用: {info}")
        self.assertIn(info.name, ("quickjs", "quickjs-ng"))
