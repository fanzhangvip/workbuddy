"""分离流合流播放的单元测试。

站点把画面和声音放在两条流里（DASH）时，播放器用"主源播画面 + 附加音轨播声音"
来合流。这里不碰真实解码、也不联网：用假后端把"控制器把哪两条地址交给了谁"
固定下来。真实解码链路由 tools/page_play_check.py 与 .tools/merged_play_probe.py 覆盖。
"""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.core.backends.qt_backend import (  # noqa: E402
    _SYNC_MAX_INTERVAL_MS,
    _SYNC_MIN_INTERVAL_MS,
    _SYNC_PENDING_EPS_MS,
    _SYNC_STUCK_TIMEOUT_MS,
    _SYNC_TOLERANCE_MS,
)

import src.core.player_controller as PC  # noqa: E402
from src.core.backends.base import PlayerBackend  # noqa: E402
from src.core.media_item import MediaItem  # noqa: E402
from src.core.resolver import ResolveResult  # noqa: E402
from src.core.url_router import UrlKind  # noqa: E402

VIDEO_URL = "https://cdn.example.com/1080-avc1.m4s"
AUDIO_URL = "https://cdn.example.com/audio.m4s"
PAGE_URL = "https://www.bilibili.com/video/BV1xx411c7mD"


class FakeBackend(PlayerBackend):
    """只记录调用，不做任何真实解码。"""

    def __init__(self, *, extra_audio: bool = True) -> None:
        super().__init__()
        self._supports_extra = extra_audio
        self.main_source = ""
        self.audio_source = ""
        self.play_count = 0
        self.clear_count = 0

    def supports_extra_audio(self) -> bool:
        return self._supports_extra

    def set_source(self, url: str, headers: dict | None = None) -> None:
        self.main_source = url

    def set_audio_source(self, url: str, headers: dict | None = None) -> None:
        self.audio_source = url

    def clear_audio_source(self) -> None:
        self.clear_count += 1
        self.audio_source = ""

    def extra_audio_active(self) -> bool:
        return bool(self.audio_source)

    def play(self) -> None:
        self.play_count += 1

    def set_volume(self, volume: float) -> None:
        pass

    def set_muted(self, muted: bool) -> None:
        pass

    def set_rate(self, rate: float) -> None:
        pass


def separate_result() -> ResolveResult:
    """照 B 站真机跑出来的形态造一份"只有分离流"的解析结果。"""
    result = ResolveResult()
    result.page_url = PAGE_URL
    result.title = "测试视频"
    result.video_url = VIDEO_URL
    result.video_kind = UrlKind.DIRECT_MEDIA
    result.audio_url = AUDIO_URL
    result.error = "该站点只提供「分离的音视频流」，内置内核无法同时播放"
    return result


def make_controller(*, extra_audio: bool = True) -> tuple[PC.PlayerController, FakeBackend]:
    """造一个把后端换成假实现的控制器。"""
    ctrl = PC.PlayerController()
    fake = FakeBackend(extra_audio=extra_audio)
    ctrl._backend = fake  # noqa: SLF001
    ctrl._connect_backend()  # noqa: SLF001
    # 诊断会真的联网，测试里不需要（既有超时也拖慢用例）
    ctrl._start_diagnosis = lambda item: None  # type: ignore[method-assign]  # noqa: SLF001
    return ctrl, fake


def _ensure_app() -> QApplication:
    return QApplication.instance() or QApplication([])


class BaseBackendTest(unittest.TestCase):
    """后端抽象层的默认行为：不支持附加音轨时也不能炸。"""

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_app()

    def test_default_backend_has_no_extra_audio(self) -> None:
        backend = PlayerBackend()
        self.assertFalse(backend.supports_extra_audio())
        self.assertFalse(backend.extra_audio_active())

    def test_default_backend_tolerates_extra_audio_calls(self) -> None:
        backend = PlayerBackend()
        backend.set_audio_source(AUDIO_URL)   # 不支持的后端被调用也不该抛
        backend.clear_audio_source()


class MediaItemTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _ensure_app()

    def test_reports_separate_audio(self) -> None:
        item = MediaItem.from_url(PAGE_URL)
        self.assertFalse(item.has_separate_audio)
        item.audio_url = AUDIO_URL
        self.assertTrue(item.has_separate_audio)
        self.assertIn("audio.m4s", item.audio_play_url)

    def test_audio_url_is_not_persisted(self) -> None:
        """直链带签名、会失效，不能存进播放列表。"""
        item = MediaItem.from_url(PAGE_URL)
        item.audio_url = AUDIO_URL
        self.assertNotIn("audio_url", item.to_dict())


class MergedPlaybackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _ensure_app()

    def setUp(self) -> None:
        self.ctrl, self.fake = make_controller()

    # ---------------------------------------------------------- 入口判定
    def test_cannot_merge_without_resolution(self) -> None:
        self.assertFalse(self.ctrl.can_play_merged())

    def test_cannot_merge_with_video_stream_only(self) -> None:
        result = separate_result()
        result.audio_url = ""
        self.ctrl._resolution = result  # noqa: SLF001
        self.ctrl._current = MediaItem.from_url(PAGE_URL)  # noqa: SLF001
        self.assertFalse(self.ctrl.can_play_merged())
        self.assertTrue(self.ctrl.can_play_video_only())

    def test_can_merge_with_both_streams(self) -> None:
        self.ctrl._resolution = separate_result()  # noqa: SLF001
        self.ctrl._current = MediaItem.from_url(PAGE_URL)  # noqa: SLF001
        self.assertTrue(self.ctrl.can_play_merged())

    def test_cannot_merge_when_backend_lacks_support(self) -> None:
        ctrl, _ = make_controller(extra_audio=False)
        ctrl._resolution = separate_result()  # noqa: SLF001
        ctrl._current = MediaItem.from_url(PAGE_URL)  # noqa: SLF001
        self.assertFalse(ctrl.can_play_merged())
        # 仅画面这条路必须还在：后端不支持合流不等于什么都播不了
        self.assertTrue(ctrl.can_play_video_only())

    def test_stale_resolution_gives_no_entry(self) -> None:
        """换了地址之后，上一轮的解析结果不该再提供降级入口。"""
        self.ctrl._resolution = separate_result()  # noqa: SLF001
        self.ctrl._current = MediaItem.from_url("https://example.com/other.mp4")  # noqa: SLF001
        self.assertFalse(self.ctrl.can_play_merged())
        self.assertFalse(self.ctrl.can_play_video_only())

    # ---------------------------------------------------------- 实际播放
    def test_play_merged_hands_both_streams(self) -> None:
        self.ctrl._resolution = separate_result()  # noqa: SLF001
        self.ctrl._current = MediaItem.from_url(PAGE_URL)  # noqa: SLF001
        self.assertTrue(self.ctrl.play_merged())
        self.assertIn("1080-avc1.m4s", self.fake.main_source)
        self.assertIn("audio.m4s", self.fake.audio_source)
        self.assertEqual(self.fake.play_count, 1, "两路必须在同一次 play() 里一起启动")
        self.assertIn("画面+声音", self.ctrl.current.resolver_note)

    def test_play_merged_falls_back_when_backend_cannot(self) -> None:
        ctrl, fake = make_controller(extra_audio=False)
        ctrl._resolution = separate_result()  # noqa: SLF001
        ctrl._current = MediaItem.from_url(PAGE_URL)  # noqa: SLF001
        self.assertTrue(ctrl.play_merged())
        self.assertIn("1080-avc1.m4s", fake.main_source)
        self.assertEqual(fake.audio_source, "", "后端不支持时不该挂音轨")

    def test_video_only_does_not_hang_audio_track(self) -> None:
        self.ctrl._resolution = separate_result()  # noqa: SLF001
        self.ctrl._current = MediaItem.from_url(PAGE_URL)  # noqa: SLF001
        self.ctrl.play_video_only()
        self.assertIn("1080-avc1.m4s", self.fake.main_source)
        self.assertEqual(self.fake.audio_source, "")

    def test_new_playback_clears_old_audio_track(self) -> None:
        """换源要拆掉上一轮的音轨，否则会接着播上一个视频的声音。"""
        self.ctrl._resolution = separate_result()  # noqa: SLF001
        self.ctrl._current = MediaItem.from_url(PAGE_URL)  # noqa: SLF001
        self.ctrl.play_merged()
        self.assertTrue(self.fake.audio_source)

        self.ctrl._start(MediaItem.from_url("https://example.com/a.mp4"))  # noqa: SLF001
        self.assertEqual(self.fake.audio_source, "")

    def test_extra_audio_failure_degrades_to_silent(self) -> None:
        """声音流挂了只降级成无声，不能报成"播放失败"。"""
        self.ctrl._resolution = separate_result()  # noqa: SLF001
        self.ctrl._current = MediaItem.from_url(PAGE_URL)  # noqa: SLF001
        self.ctrl.play_merged()

        messages: list[str] = []
        self.ctrl.statusMessage.connect(messages.append)
        self.ctrl._on_extra_audio_failed("网络错误")  # noqa: SLF001
        self.assertTrue(any("降级为仅画面" in m for m in messages))

    def test_stalled_audio_track_is_not_declared_dead(self) -> None:
        """音轨追不上画面只是"停止自动校正"，**不能**谎报已降级为无声。

        两者的用户可见后果完全不同：降级 = 声音彻底没了；失步 = 声音还在，
        只是对不齐。把它报成降级会让人以为已经没救，直接放弃排查。
        """
        self.ctrl._resolution = separate_result()  # noqa: SLF001
        self.ctrl._current = MediaItem.from_url(PAGE_URL)  # noqa: SLF001
        self.ctrl.play_merged()

        messages: list[str] = []
        self.ctrl.statusMessage.connect(messages.append)
        self.ctrl._on_extra_audio_stalled("声音比画面落后较多")  # noqa: SLF001

        self.assertTrue(any("停止自动校正" in m for m in messages))
        self.assertFalse(any("降级为仅画面" in m for m in messages))
        # 音轨本身没有被丢掉
        self.assertTrue(self.fake.extra_audio_active())


class _StubPlayer:
    """只提供 `_sync_tick` 需要的那几个读数接口，不碰真实解码。"""

    def __init__(self) -> None:
        self.pos = 0
        self.state = QMediaPlayer.PlaybackState.PlayingState
        self.seeks: list[int] = []

    def position(self) -> int:
        return self.pos

    def playbackState(self):
        return self.state

    def setPosition(self, ms: int) -> None:
        self.seeks.append(int(ms))
        # 真实播放器 seek 之后 position 会立刻报成目标值（真正出声要晚些）
        self.pos = int(ms)

    def play(self) -> None:
        pass

    def pause(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def setSource(self, *_args) -> None:
        pass


class SyncAntiDeadlockTest(unittest.TestCase):
    """音画同步的两个闸：不补刀、不白费刀。

    实测背景（YouTube DASH + 本地代理）：两条流各由一个播放器实例播放，声音
    会固定落后画面 270~600ms（起播固有差，同一次运行内恒定）；而"把声音 seek
    到画面"这一步要 0.5~2s 才能重新出声，代价比它修掉的偏差更大。所以：
    阈值抬高（正常不校正）+ 一刀未生效绝不补刀 + 一刀没帮上忙就退避。
    """

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_app()
        from src.core.backends.qt_backend import QtMediaBackend

        cls._backend = QtMediaBackend()

    def setUp(self) -> None:
        backend = self._backend
        self.video = _StubPlayer()
        self.audio = _StubPlayer()
        backend._player = self.video          # noqa: SLF001
        backend._extra_player = self.audio    # noqa: SLF001
        backend._extra_active = True          # noqa: SLF001
        backend._extra_loaded = True          # noqa: SLF001
        backend._sync_hold_until = 0.0        # noqa: SLF001
        backend._last_sync_at = 0.0           # noqa: SLF001
        backend._pending_target = None        # noqa: SLF001
        backend._pending_since = 0.0          # noqa: SLF001
        backend._pending_drift = None         # noqa: SLF001
        backend._sync_interval_ms = _SYNC_MIN_INTERVAL_MS  # noqa: SLF001
        backend._sync_timer.stop()            # noqa: SLF001
        self.stalls: list[str] = []
        backend.extraAudioStalled.connect(self.stalls.append)
        self.backend = backend

    # ---------------------------------------------------------------- 补刀
    def test_no_second_seek_while_first_is_still_in_flight(self) -> None:
        """上一刀还没生效时，偏差再大也**不许**再补一刀。"""
        self.video.pos = 30000
        self.audio.pos = 5000          # 停在 seek 目标上 → 说明还没生效
        self.backend._pending_target = 5000   # noqa: SLF001
        self.backend._pending_since = time.monotonic() - 1.0  # noqa: SLF001

        self.backend._sync_tick()      # noqa: SLF001

        self.assertEqual(self.audio.seeks, [])
        self.assertEqual(self.backend._pending_target, 5000)  # noqa: SLF001

    def test_seek_again_once_previous_one_took_effect(self) -> None:
        """声音动起来了 → 上一刀生效，可以接受下一次判定。"""
        self.video.pos = 30000
        self.audio.pos = 8000          # 已经离开 seek 目标 → 生效了
        self.backend._pending_target = 5000   # noqa: SLF001
        self.backend._pending_since = time.monotonic() - 1.0  # noqa: SLF001

        self.backend._sync_tick()      # noqa: SLF001

        self.assertEqual(self.audio.seeks, [30000])
        self.assertEqual(self.backend._pending_target, 30000)  # noqa: SLF001

    # ---------------------------------------------------------------- 放弃
    def test_gives_up_and_reports_after_timeout(self) -> None:
        """一刀拖过超时仍不生效 → 停止自动校正并如实上报（而不是继续 seek）。"""
        self.video.pos = 60000
        self.audio.pos = 5000
        self.backend._pending_target = 5000   # noqa: SLF001
        self.backend._pending_since = (         # noqa: SLF001
            time.monotonic() - _SYNC_STUCK_TIMEOUT_MS / 1000 - 1.0
        )
        self.backend._sync_timer.start()       # noqa: SLF001

        self.backend._sync_tick()      # noqa: SLF001

        self.assertEqual(self.audio.seeks, [], "放弃之后不该再 seek")
        self.assertIsNone(self.backend._pending_target)   # noqa: SLF001
        self.assertFalse(self.backend._sync_timer.isActive(), "应停掉定时器")  # noqa: SLF001
        self.assertEqual(len(self.stalls), 1, "应上报一次「失步/停校正」")
        self.assertIn("落后", self.stalls[0])

    def test_still_within_timeout_keeps_waiting(self) -> None:
        """没到超时就继续等，不要提前认输（重定位本来就要几秒）。"""
        self.video.pos = 60000
        self.audio.pos = 5000
        self.backend._pending_target = 5000   # noqa: SLF001
        self.backend._pending_since = time.monotonic() - 1.0  # noqa: SLF001

        self.backend._sync_tick()      # noqa: SLF001

        self.assertEqual(self.stalls, [])
        self.assertEqual(self.backend._pending_target, 5000)  # noqa: SLF001

    # ---------------------------------------------------------------- 退避
    def test_effective_correction_keeps_base_interval(self) -> None:
        """一刀就把偏差压下去了 → 间隔保持基准，下次有事能立刻响应。"""
        self.video.pos = 50000
        self.audio.pos = 5000
        backend = self.backend
        backend._pending_target = 5000        # noqa: SLF001
        backend._pending_drift = 45000        # noqa: SLF001
        backend._pending_since = time.monotonic() - 1.0   # noqa: SLF001
        self.audio.pos = 20000                # 离开目标 15s → 生效
        self.audio.seeks.clear()

        backend._sync_tick()                  # noqa: SLF001

        self.assertEqual(backend._sync_interval_ms, _SYNC_MIN_INTERVAL_MS)  # noqa: SLF001

    def test_ineffective_correction_backs_off(self) -> None:
        """一刀下去偏差几乎没变（重定位开销吃掉了收益）→ 拉长下一次的间隔。"""
        self.video.pos = 60000
        self.audio.pos = 5000
        backend = self.backend
        backend._pending_target = 5000        # noqa: SLF001
        backend._pending_drift = 58000        # noqa: SLF001
        backend._pending_since = time.monotonic() - 1.0   # noqa: SLF001
        self.audio.pos = 2000                 # 离开目标 → 判定生效（否则会被当成卡住）
        # 生效后重新量：画面 60000、声音 2000 → 还剩 58s，等于白校
        self.audio.seeks.clear()
        backend._last_sync_at = time.monotonic()   # 挡住本拍再补一刀  # noqa: SLF001

        backend._sync_tick()                  # noqa: SLF001

        self.assertEqual(backend._sync_interval_ms, _SYNC_MIN_INTERVAL_MS * 2)  # noqa: SLF001

    def test_backoff_is_capped(self) -> None:
        backend = self.backend
        backend._sync_interval_ms = _SYNC_MAX_INTERVAL_MS  # noqa: SLF001
        backend._pending_drift = 58000        # noqa: SLF001
        self.video.pos = 60000
        self.audio.pos = 2000
        backend._settle_correction(60000, 2000)   # noqa: SLF001
        self.assertEqual(backend._sync_interval_ms, _SYNC_MAX_INTERVAL_MS)  # noqa: SLF001

    def test_bogus_zero_readings_do_not_trigger_backoff(self) -> None:
        """刚 seek 完两边读数可能都还是 0 —— 那是"没数据"，不是"没改善"。"""
        backend = self.backend
        backend._pending_drift = 58000        # noqa: SLF001
        backend._settle_correction(0, 0)      # noqa: SLF001
        self.assertEqual(backend._sync_interval_ms, _SYNC_MIN_INTERVAL_MS)  # noqa: SLF001

    def test_interval_recovers_when_drift_is_gone(self) -> None:
        """偏差回到容忍范围内 → 间隔收回基准（退避不是永久的）。"""
        backend = self.backend
        backend._sync_interval_ms = _SYNC_MAX_INTERVAL_MS  # noqa: SLF001
        self.video.pos = 10000
        self.audio.pos = 9900
        backend._sync_tick()                  # noqa: SLF001
        self.assertEqual(backend._sync_interval_ms, _SYNC_MIN_INTERVAL_MS)  # noqa: SLF001

    def test_new_audio_track_gets_a_fresh_interval(self) -> None:
        """换一条音轨就把退避清掉：上一个视频退化出来的间隔不该跟着过来。"""
        backend = self.backend
        backend._sync_interval_ms = _SYNC_MAX_INTERVAL_MS  # noqa: SLF001
        backend.set_audio_source(AUDIO_URL)   # noqa: SLF001
        self.assertEqual(backend._sync_interval_ms, _SYNC_MIN_INTERVAL_MS)  # noqa: SLF001

    # ---------------------------------------------------------------- 常规
    def test_natural_start_skew_is_left_alone(self) -> None:
        """实测的声音起播固有差（270~600ms）**不该**触发校正——校它反而更糟。"""
        self.video.pos = 10000
        self.audio.pos = 9592          # 偏差 408ms，正是实测到的固有差
        self.backend._sync_tick()      # noqa: SLF001
        self.assertEqual(self.audio.seeks, [])

    def test_large_drift_seeks_audio_to_video(self) -> None:
        """真正的卡顿（差到几秒）才出手。"""
        self.video.pos = 20000
        self.audio.pos = 3000          # 偏差 17s，远超阈值
        self.backend._sync_tick()      # noqa: SLF001
        self.assertEqual(self.audio.seeks, [20000])
        self.assertEqual(self.backend._pending_target, 20000)  # noqa: SLF001
        self.assertEqual(self.backend._pending_drift, 17000)   # noqa: SLF001

    def test_pending_epsilon_is_much_smaller_than_tolerance(self) -> None:
        """"还没生效"的余量必须远小于"不同步"的阈值，两者语义相反。"""
        self.assertLess(_SYNC_PENDING_EPS_MS, _SYNC_TOLERANCE_MS / 3)

    def test_switching_audio_source_clears_pending_seek(self) -> None:
        """换音轨时要忘掉上一刀的账，否则新音轨会被误判成"卡住"。"""
        self.backend._pending_target = 12345   # noqa: SLF001
        self.backend._pending_drift = 9999     # noqa: SLF001
        self.backend.set_audio_source(AUDIO_URL)   # noqa: SLF001
        self.assertIsNone(self.backend._pending_target)  # noqa: SLF001
        self.assertIsNone(self.backend._pending_drift)   # noqa: SLF001
        self.backend.clear_audio_source()          # noqa: SLF001
        self.assertIsNone(self.backend._pending_target)  # noqa: SLF001

    def test_empty_source_stops_sync_timer(self) -> None:
        self.backend._sync_timer.start()       # noqa: SLF001
        self.backend.set_audio_source("")      # noqa: SLF001
        self.assertFalse(self.backend._sync_timer.isActive())  # noqa: SLF001

    def test_user_seek_does_not_fire_the_stall_warning(self) -> None:
        """用户自己拖进度条不该被当成"校正卡住了"。

        拖动进度条走的是 `set_position()`，它同样会记下一笔 pending。而判定
        "还停在原地"的余量如果跟着容忍值一起放大（两者共用一个常量），一次
        位置刚好落在目标附近的正常播放，8 秒后就会报出"已停止自动校正"——
        一条纯属误报的提示。这里钉死：偏离目标 400ms 就算"已经在放了"。
        """
        self.video.pos = 600000
        self.audio.pos = 600400        # 偏离目标 400ms < 容忍值，但远大于余量
        self.backend._pending_target = 600000      # noqa: SLF001
        self.backend._pending_drift = None         # noqa: SLF001
        self.backend._pending_since = (            # noqa: SLF001
            time.monotonic() - _SYNC_STUCK_TIMEOUT_MS / 1000 - 1.0
        )

        self.backend._sync_tick()                   # noqa: SLF001

        self.assertEqual(self.stalls, [], "它已经在放了，不该报「卡住」")


class AutoMergeTest(unittest.TestCase):
    """解析到分离流时应**直接开播**，而不是弹框让用户点一次。

    用户视角的诉求："粘贴 B 站地址 → 直接出画面和声音"。解析器对 DASH 站点
    给的是 ok=False + 两条流，早期版本把它当失败弹框，于是每次都要手动点
    「同时播放画面与声音」。这里把"什么时候自动、什么时候还弹框"钉死。
    """

    KEYS = ("playback/auto_merge_separate", "proxy/enabled")

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_app()

    def setUp(self) -> None:
        from src.utils import settings as S

        self.S = S
        self._saved = {key: S.settings().value(key) for key in self.KEYS}
        # 断言里要比较"交给后端的地址"，代理不能插手
        S.settings().setValue(S.K_LOCAL_PROXY_ENABLED, False)
        S.settings().setValue(S.K_AUTO_MERGE_SEPARATE, True)
        self.ctrl, self.fake = make_controller()

    def tearDown(self) -> None:
        store = self.S.settings()
        for key, value in self._saved.items():
            if value is None:
                store.remove(key)
            else:
                store.setValue(key, value)

    def _deliver(self, result: ResolveResult) -> list[str]:
        """把一份解析结果送进控制器（走真实的解析完成回调）。"""
        errors: list[str] = []
        self.ctrl.errorRaised.connect(lambda t, d: errors.append(t))
        token = object()
        self.ctrl._resolve_token = token  # noqa: SLF001
        self.ctrl._on_resolve_ready((token, result))  # noqa: SLF001
        return errors

    def test_separate_streams_start_playing_right_away(self) -> None:
        errors = self._deliver(separate_result())
        self.assertEqual(errors, [], "有分离流可合流时不该弹错误框")
        self.assertIn("1080-avc1.m4s", self.fake.main_source)
        self.assertIn("audio.m4s", self.fake.audio_source)
        self.assertEqual(self.fake.play_count, 1)

    def test_status_line_says_what_is_happening(self) -> None:
        messages: list[str] = []
        self.ctrl.statusMessage.connect(messages.append)
        self._deliver(separate_result())
        self.assertTrue(
            any("自动" in m and "画面" in m for m in messages),
            f"状态栏该说清'正在自动合流'，实际: {messages}",
        )

    def test_single_file_stream_keeps_playing_directly(self) -> None:
        """有音视频合一的单文件流时走原路：不挂附加音轨。"""
        result = ResolveResult()
        result.page_url = PAGE_URL
        result.ok = True
        result.url = "https://cdn.example.com/360-progressive.mp4"
        result.kind = UrlKind.DIRECT_MEDIA
        errors = self._deliver(result)
        self.assertEqual(errors, [])
        self.assertIn("360-progressive.mp4", self.fake.main_source)
        self.assertEqual(self.fake.audio_source, "")

    def test_setting_off_keeps_the_dialog(self) -> None:
        """用户关掉自动合流后，仍然弹框让他自己选。"""
        self.S.settings().setValue(self.S.K_AUTO_MERGE_SEPARATE, False)
        errors = self._deliver(separate_result())
        self.assertEqual(errors, ["网页地址解析失败"])
        self.assertEqual(self.fake.play_count, 0)
        # 弹框里得给出可选项，否则等于把路堵死
        self.assertTrue(self.ctrl.can_play_merged())
        self.assertTrue(self.ctrl.can_play_video_only())

    def test_no_auto_merge_when_backend_cannot(self) -> None:
        ctrl, fake = make_controller(extra_audio=False)
        errors: list[str] = []
        ctrl.errorRaised.connect(lambda t, d: errors.append(t))
        token = object()
        ctrl._resolve_token = token  # noqa: SLF001
        ctrl._on_resolve_ready((token, separate_result()))  # noqa: SLF001
        self.assertEqual(errors, ["网页地址解析失败"])
        self.assertEqual(fake.play_count, 0)
        self.assertTrue(ctrl.can_play_video_only(), "至少要能只播画面")

    def test_does_not_auto_merge_twice(self) -> None:
        """试过合流且失败后，不该再自动重播同一个组合（否则会来回死循环）。"""
        self.ctrl._resolution = separate_result()  # noqa: SLF001
        self.ctrl._current = MediaItem.from_url(PAGE_URL)  # noqa: SLF001
        self.ctrl.play_merged()
        self.assertFalse(self.ctrl._can_auto_merge(separate_result()))  # noqa: SLF001
        self.assertFalse(self.ctrl.can_play_merged(), "刚失败过的组合不该再推荐")
        self.assertTrue(self.ctrl.can_play_video_only())

    def test_new_address_resets_the_attempt_flag(self) -> None:
        self.ctrl._resolution = separate_result()  # noqa: SLF001
        self.ctrl._current = MediaItem.from_url(PAGE_URL)  # noqa: SLF001
        self.ctrl.play_merged()
        self.assertTrue(self.ctrl._merged_attempted)  # noqa: SLF001

        self.ctrl.play_text("https://example.com/other.mp4")  # noqa: SLF001
        self.assertFalse(self.ctrl._merged_attempted)  # noqa: SLF001


class AutoMergeSettingTest(unittest.TestCase):
    """自动合流的开关必须真的接通（不是只加了个复选框）。

    "界面加了控件但忘了写进 `_collect()`"是个典型静默 bug：复选框能勾、
    看起来生效了，存盘时却丢掉了，下次启动又变回默认。这里把它挡住。
    """

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_app()

    def setUp(self) -> None:
        from src.utils import settings as S

        self.S = S
        self._saved = S.settings().value(S.K_AUTO_MERGE_SEPARATE)

    def tearDown(self) -> None:
        store = self.S.settings()
        if self._saved is None:
            store.remove(self.S.K_AUTO_MERGE_SEPARATE)
        else:
            store.setValue(self.S.K_AUTO_MERGE_SEPARATE, self._saved)

    def test_registered_with_default_on(self) -> None:
        self.assertIn(self.S.K_AUTO_MERGE_SEPARATE, self.S.DEFAULTS)
        self.assertTrue(
            self.S.DEFAULTS[self.S.K_AUTO_MERGE_SEPARATE],
            "默认应当是自动合流——用户要的是'粘地址就播'",
        )

    def test_dialog_collects_and_loads_it(self) -> None:
        from src.ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog()
        try:
            dialog.check_auto_merge.setChecked(False)
            self.assertFalse(dialog._collect()[self.S.K_AUTO_MERGE_SEPARATE])  # noqa: SLF001

            self.S.settings().setValue(self.S.K_AUTO_MERGE_SEPARATE, False)
            dialog._load()  # noqa: SLF001
            self.assertFalse(dialog.check_auto_merge.isChecked())

            self.S.settings().setValue(self.S.K_AUTO_MERGE_SEPARATE, True)
            dialog._load()  # noqa: SLF001
            self.assertTrue(dialog.check_auto_merge.isChecked())
        finally:
            dialog.deleteLater()


class ProxyRoutingTest(unittest.TestCase):
    """防盗链代理的路由判定（`player_controller._proxy_route`）。

    这里不联网：代理启没启用完全由配置项决定，开关关闭时 `proxy_url`
    直接返回空串，所以"怎么路由"这件事不需要真的起代理就能钉死。
    """

    KEYS = ("proxy/enabled", "proxy/referer", "proxy/port")

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_app()

    def setUp(self) -> None:
        from src.utils import settings as S

        self.S = S
        self.ctrl, self.fake = make_controller()
        self._saved = {key: S.settings().value(key) for key in self.KEYS}

        # 把"有没有代理在生效"钉成没有：本项目的开发沙箱里 HTTP_PROXY 常年
        # 有值，不钉住的话下面几条用例测的就不是它们自称的东西了。
        patcher = mock.patch("src.utils.net_proxy.proxy_for", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        from src.core import local_proxy

        local_proxy.stop()
        store = self.S.settings()
        for key, value in self._saved.items():
            if value is None:
                store.remove(key)
            else:
                store.setValue(key, value)

    def test_untouched_when_proxy_disabled(self) -> None:
        self.S.settings().setValue(self.S.K_LOCAL_PROXY_ENABLED, False)
        item = MediaItem.from_url(VIDEO_URL)
        item.http_headers = {"Referer": "https://cdn.example.com/"}
        self.assertEqual(self.ctrl._proxy_route(item.play_url, item), item.play_url)  # noqa: SLF001

    def test_direct_link_without_headers_uses_proxy_when_enabled(self) -> None:
        """用户开着代理开关、地址自己不带请求头时也必须走代理。

        这是最自然的防盗链用法：地址栏粘贴一条播不了的直链，去设置里勾上
        代理并填 Referer。早期版本要求 `item.http_headers` 非空才走代理，
        于是这条链路整个失效——开关和 Referer 都白填。
        """
        self.S.settings().setValue(self.S.K_LOCAL_PROXY_ENABLED, True)
        item = MediaItem.from_url(VIDEO_URL)
        self.assertEqual(item.http_headers, {}, "这条地址本来就不带请求头")

        routed = self.ctrl._proxy_route(item.play_url, item)  # noqa: SLF001
        self.assertNotEqual(routed, item.play_url, "开了代理却没用上")
        self.assertIn("/proxy?u=", routed)
        self.assertIn("127.0.0.1", routed)

    def test_proxy_route_applies_to_what_backend_receives(self) -> None:
        """路由结果要真的交到后端手上（接线，而不只是函数返回值对）。"""
        self.S.settings().setValue(self.S.K_LOCAL_PROXY_ENABLED, True)
        item = MediaItem.from_url(VIDEO_URL)
        self.ctrl._start(item)  # noqa: SLF001
        self.assertIn("/proxy?u=", self.fake.main_source)

    def test_rtsp_and_hls_are_never_proxied(self) -> None:
        """协议代理不了就别硬塞：rtsp 不走 HTTP，m3u8 清单会被分片地址绕过。"""
        self.S.settings().setValue(self.S.K_LOCAL_PROXY_ENABLED, True)
        for url in (
            "rtsp://192.168.1.9:554/live",
            "https://cdn.example.com/hls/index.m3u8",
        ):
            item = MediaItem.from_url(url)
            self.assertEqual(
                self.ctrl._proxy_route(item.play_url, item), item.play_url, url  # noqa: SLF001
            )

    def test_proxied_host_routes_even_with_the_switch_off(self) -> None:
        """有代理在生效时必须绕一层，且不受防盗链总开关约束。

        播放内核（Qt 的 FFmpeg 后端）不认系统代理，而解析器用的是 requests /
        yt-dlp，两者读同一份代理配置——不绕这一层就是"解析成功、播放报连不上
        服务器"。这不是增强，是唯一能播的路子。
        """
        from src.utils import net_proxy as NP

        self.S.settings().setValue(self.S.K_LOCAL_PROXY_ENABLED, False)
        item = MediaItem.from_url(VIDEO_URL)

        with mock.patch("src.utils.net_proxy.proxy_for",
                        return_value=NP.ProxyHit("http://127.0.0.1:7897", NP.SOURCE_SYSTEM)):
            routed = self.ctrl._proxy_route(item.play_url, item)  # noqa: SLF001

        self.assertIn("/proxy?u=", routed)

    def test_hls_manifest_stays_direct_even_when_proxied(self) -> None:
        """清单型地址绕不过去：分片地址在播放器手里，代理改写不到。"""
        from src.utils import net_proxy as NP

        item = MediaItem.from_url("https://cdn.example.com/hls/index.m3u8")
        with mock.patch("src.utils.net_proxy.proxy_for",
                        return_value=NP.ProxyHit("http://127.0.0.1:7897", NP.SOURCE_SYSTEM)):
            self.assertEqual(
                self.ctrl._proxy_route(item.play_url, item), item.play_url  # noqa: SLF001
            )


if __name__ == "__main__":
    unittest.main()
