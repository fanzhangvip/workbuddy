"""网页地址全链路检查：解析 → 播放 → （分离流时）合流播放 / 仅画面降级。

用法:
    python tools/page_play_check.py "<页面地址>" [--timeout 45] [--video-only] [--no-degrade]

分离流的三种处理方式:
    默认          看**自动合流**有没有生效（控制器自己把画面与声音合起来播，不弹框）。
                  若自动合流被设置项关掉了，则退化为手动触发「同时播放画面与声音」。
    --video-only  强制只触发「仅播放画面（无声音）」
    --no-degrade  什么都不做，观察原始失败（用来确认站点确实没有单文件流）

与 `env_probe.py` 的分工:
    env_probe     测"这条地址本身能不能播"，可以在播放前强制解析（--resolve）
    page_play_check  测"把地址粘进界面后会发生什么"——离屏驱动**真实的 MainWindow**，
                  走真实入口 `_play_from_address()`，因此覆盖的是用户实际经历的路径：
                  解析中提示 → 播放成功 / 分离流提示 → 触发合流或降级的结果。

判定标准（沿用 playback_probe 的思路，别只看 duration）:
    点播：收到帧 / duration > 0 / 位置推进
    直播：必须真的收到画面帧
    合流：画面出帧 **且** 声音位置在推进，缺一不算成功
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 无桌面会话下真实窗口会被直接杀掉，一律离屏
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.utils.env_setup import setup_environment  # noqa: E402

setup_environment()

from src.utils.logger import get_logger, setup_logging  # noqa: E402

setup_logging()
_log = get_logger("page_play_check")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

# 离屏下模态对话框会永久阻塞，让它立即返回
QMessageBox.exec = lambda self: 0  # type: ignore[method-assign]

from src.ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="网页地址全链路检查")
    parser.add_argument("url", help="要检查的页面地址")
    parser.add_argument("--timeout", type=int, default=45, help="总超时秒数")
    parser.add_argument("--no-degrade", action="store_true",
                        help="分离流时什么都不做，观察原始失败")
    parser.add_argument("--video-only", action="store_true",
                        help="分离流时只触发「仅播放画面」，不尝试合流")
    args = parser.parse_args()

    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = MainWindow()
    window.resize(1200, 760)
    window.show()

    ctrl = window.controller
    events: list[str] = []
    positions: list[int] = []
    durations: list[int] = []
    errors: list[str] = []

    ctrl.statusMessage.connect(lambda s: events.append(f"状态   {s}"))
    ctrl.resolveStarted.connect(lambda u: events.append(f"解析开始 {u}"))
    ctrl.resolveFinished.connect(
        lambda r: events.append(
            f"解析完成 ok={r.ok} 分离流={r.has_separate_streams} "
            f"流={r.url or '（无单文件流）'} 说明={r.note or r.error}"
        )
    )
    ctrl.errorRaised.connect(
        lambda t, d: errors.append(f"{t} | {d.splitlines()[0]}")
    )
    ctrl.positionChanged.connect(lambda ms: positions.append(int(ms)))
    ctrl.durationChanged.connect(lambda ms: durations.append(int(ms)))
    ctrl.backend.extraAudioFailed.connect(
        lambda reason: events.append(f"音轨失败 {reason}")
    )

    # 自己接一路画面帧计数。**不能用 backend.grab_frame() 判"有没有出画面"**：
    # 它内部有个"抓控件画面"的兜底分支，离屏下控件 grab 永远返回非空图，
    # 于是没解码也会判定为"有画面"，是假阳性。只有 videoFrameChanged 是真证据。
    frame_count = [0]

    def on_frame(frame) -> None:
        if frame is not None and frame.isValid():
            frame_count[0] += 1

    ctrl.backend.video_widget().videoSink().videoFrameChanged.connect(on_frame)

    state = {"mode": "", "done": False}
    max_position = lambda: max(positions) if positions else 0  # noqa: E731

    # 附加音轨没有对外信号，只能读后端内部那个播放器实例的位置。
    # 这是"声音真的在走"最直接的证据，比 extra_audio_active() 更硬。
    audio_positions: list[int] = []

    def audio_position() -> int:
        try:
            return int(ctrl.backend._extra_player.position())  # noqa: SLF001
        except (AttributeError, RuntimeError, TypeError):
            return 0

    def max_audio() -> int:
        return max(audio_positions) if audio_positions else 0

    def snapshot() -> str:
        item = ctrl.current
        return (
            f"已加载={ctrl._media_loaded} 解析中={ctrl.is_resolving} "
            f"直播={ctrl.is_live} 时长={max(durations) if durations else 0}ms "
            f"位置={max_position()}ms 画面帧={frame_count[0]} "
            f"声音位置={max_audio()}ms 音轨在播={ctrl.backend.extra_audio_active()} "
            f"标题={item.title if item else None!r}"
        )

    def finish(ok: bool, note: str) -> None:
        if state["done"]:
            return
        state["done"] = True
        print("=" * 70)
        print(f"地址: {args.url}")
        print("-" * 70)
        for line in events:
            print(f"  {line}")
        if errors:
            print("-" * 70)
            for line in errors:
                print(f"  错误   {line}")
        print("-" * 70)
        print(f"最终状态: {snapshot()}")
        print(f"结论: {'成功' if ok else '失败'} —— {note}")
        print("=" * 70)
        window.close()
        app.quit()
        main.exit_code = 0 if ok else 1  # type: ignore[attr-defined]

    def frames_ok() -> bool:
        """真的解出过画面 —— 这是"能播"最硬的证据。

        只看 duration / mediaStatus 会被"加载成功但解不出画面"骗过去
        （缺解码器时就是这样：能解析、能 LoadedMedia，画面却是黑的）。
        至少 3 帧是为了排除偶发的单帧抖动。
        """
        return frame_count[0] >= 3

    def judge() -> tuple[bool, str]:
        """判定成功要拿到硬证据，不能只看"加载完成"。"""
        position = max_position()
        duration = max(durations) if durations else 0

        if state["mode"] == "merged":
            # 合流模式的成功标准是"音画都有"：画面出帧 **且** 声音在走，缺一不算
            if frames_ok() and max_audio() >= 300:
                return True, (
                    f"合流播放成功（画面+声音）：已解出 {frame_count[0]} 帧画面，"
                    f"声音位置={max_audio()}ms，时长={duration}ms，位置={position}ms"
                )
            if frames_ok():
                return False, (
                    f"画面正常（{frame_count[0]} 帧）但声音没播起来"
                    f"（声音位置={max_audio()}ms）"
                )

        if frames_ok():
            tag = "（仅画面，无声音）" if state["mode"] == "video_only" else ""
            return True, (
                f"播放中{tag}：已解出 {frame_count[0]} 帧画面，时长={duration}ms，"
                f"位置={position}ms"
            )
        if position > 1500:
            tag = "（仅画面，无声音）" if state["mode"] == "video_only" else ""
            return True, f"播放中{tag}：位置推进到 {position}ms（纯音频流？）"
        return False, "未确认播放"

    def tick() -> None:
        if state["done"]:
            return

        pos = audio_position()
        if pos > 0:
            audio_positions.append(pos)

        # 分离流：正常情况下控制器会**自动**合流播放（用户不用点任何按钮），
        # 所以先看它有没有自己挂上音轨；没自动才手动触发一次（等价于点按钮）。
        if not state["mode"] and not args.no_degrade and not ctrl.is_resolving:
            item = ctrl.current
            if item is not None and item.has_separate_audio:
                state["mode"] = "merged"
                events.append("动作   控制器已自动合流播放（画面+声音，无需点按钮）")
                return
            if ctrl.can_play_merged() or ctrl.can_play_video_only():
                if not args.video_only and ctrl.can_play_merged():
                    state["mode"] = "merged"
                    events.append("动作   自动合流未生效 → 手动触发「同时播放画面与声音」")
                    triggered = ctrl.play_merged()
                else:
                    state["mode"] = "video_only"
                    events.append("动作   触发「仅播放画面（无声音）」")
                    triggered = ctrl.play_video_only()
                events.append(f"动作   触发结果 {triggered}")
                return

        ok, note = judge()
        if ok:
            finish(True, note)

    def on_timeout() -> None:
        ok, note = judge()
        finish(ok, note if ok else "超时：未能确认播放")

    QTimer.singleShot(200, lambda: (window.address.setText(args.url),
                                    window._play_from_address()))  # noqa: SLF001

    ticker = QTimer()
    ticker.timeout.connect(tick)
    ticker.start(300)

    QTimer.singleShot(args.timeout * 1000, on_timeout)

    app.exec()
    ticker.stop()
    return getattr(main, "exit_code", 1)


if __name__ == "__main__":
    raise SystemExit(main())
