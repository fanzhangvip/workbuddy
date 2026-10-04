"""用 libmpv 内核实测播放一条地址。

用法:
    python .tools/mpv_play_check.py "<地址>"

为什么单独写一个：`tools/env_probe.py` 走的是 Qt 内核（playback_probe），
覆盖不到 MpvBackend。这里直接构造 MpvBackend，验证"换了内核还真的能播"。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.env_setup import setup_environment  # noqa: E402

setup_environment()

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])

from src.core.backends.mpv_backend import MpvBackend  # noqa: E402

url = sys.argv[1] if len(sys.argv) > 1 else "https://media.w3.org/2010/05/sintel/trailer.mp4"

backend = MpvBackend()
state: dict = {"positions": [], "duration": 0, "errors": [], "done": False}

backend.positionChanged.connect(lambda ms: state["positions"].append(int(ms)))
backend.durationChanged.connect(lambda ms: state.update(duration=int(ms)))
backend.errorOccurred.connect(lambda code, msg: state["errors"].append(msg))
backend.mediaStatusChanged.connect(
    lambda st: print(f"  [状态] {st}", flush=True)
)

print(f"地址: {url}")
print(f"能力: {sorted(backend.capabilities())}")

backend.set_source(url)
backend.play()


def finish(ok: bool, note: str) -> None:
    if state["done"]:
        return
    state["done"] = True
    pos = max(state["positions"]) if state["positions"] else 0
    print(f"时长={state['duration']}ms 最大位置={pos}ms 错误={state['errors'][:2]}")
    print(f"结论: {'成功 —— ' + note if ok else '失败 —— ' + note}")
    backend.release()
    app.quit()


def check() -> None:
    if state["done"]:
        return
    pos = max(state["positions"]) if state["positions"] else 0
    if pos > 300:
        finish(True, f"libmpv 内核播放中，位置推进到 {pos}ms")
    elif state["errors"]:
        finish(False, f"报错 {state['errors'][0]}")


ticker = QTimer()
ticker.setInterval(1500)
ticker.timeout.connect(check)
ticker.start()

QTimer.singleShot(20000, lambda: finish(False, "20 秒内没有推进"))
app.exec()
