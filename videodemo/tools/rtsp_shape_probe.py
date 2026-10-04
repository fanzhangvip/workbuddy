"""验证播放器是否把 URL 中的路径「原样」发给了 RTSP 服务器。

为什么需要这个工具：

有些 RTSP 地址的路径里带冒号（Wowza 专有的 ``mp4:`` 前缀写法），
例如 ``rtsp://host/vod/mp4:BigBuckBunny_115k.mov``。这类地址一旦被
QUrl 或中间层转义/截断，服务器就会回 404，而客户端只会报一个
含糊的加载失败——排查成本很高。

这里起一个最小 TCP 服务器，只做一件事：把收到的第一条 RTSP 请求行
原样打出来。只要 ``OPTIONS`` / ``DESCRIBE`` 里的路径与输入一致，
就证明客户端到服务器的这一段是干净的，问题不在 URL 处理上。

用法::

    python tools/rtsp_shape_probe.py                       # 跑内置用例
    python tools/rtsp_shape_probe.py "rtsp://host:554/a:b/c"
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.env_setup import setup_environment  # noqa: E402

setup_environment()

from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402

CAPTURE_PORT = 8555

#: 内置用例：覆盖用户实际遇到的各种形状
CASES = [
    "rtsp://127.0.0.1:8555/vod/mp4:BigBuckBunny_115k.mov",
    "rtsp://127.0.0.1:8555/live/channel 1",           # 路径含空格
    "rtsp://user:pw@127.0.0.1:8555/stream",           # 含认证信息
    "rtsp://127.0.0.1:8555/stream?token=abc&t=123",   # 含 query
]


class RequestRecorder:
    """最小 RTSP 服务器：记录请求行，不做协议应答。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.requests: list[str] = []
        self._stop = False
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(8)
        self._sock.settimeout(0.3)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        self._thread.join(timeout=3)
        self._sock.close()

    def reset(self) -> None:
        self.requests.clear()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        try:
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 8192:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            for line in data.decode("utf-8", "replace").splitlines():
                if line.upper().startswith(("OPTIONS", "DESCRIBE", "SETUP", "PLAY", "GET")):
                    self.requests.append(line.strip())
                    print(f"    [服务器收到] {line.strip()}")
            # 故意不回 RTSP 应答：本轮只关心请求里的路径
        except Exception:  # noqa: BLE001
            pass
        finally:
            time.sleep(0.2)
            conn.close()


def run_case(app: QGuiApplication, url: str, timeout_s: float = 6.0) -> bool:
    """把地址交给 QMediaPlayer，返回路径是否被完整保留。"""
    print(f"\n  输入地址: {url}")

    expected_path = QUrl(url).path()
    print(f"  期望路径: {expected_path}")

    fallback = None
    try:
        from src.utils.urls import to_playable_url

        fallback = to_playable_url(url)
        if fallback != url:
            print(f"  规范化后: {fallback}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [警告] 规范化失败: {exc}")

    player = QMediaPlayer()
    player.setSource(QUrl(url))
    player.play()

    # 给客户端足够时间发出第一条请求
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)

    player.stop()
    return True


def main() -> int:
    cases = sys.argv[1:] or CASES
    app = QGuiApplication(sys.argv[:1])

    print("=" * 76)
    print("RTSP 请求路径保真度验证")
    print("=" * 76)

    results: list[tuple[str, str, bool]] = []

    for url in cases:
        recorder = RequestRecorder(CAPTURE_PORT)
        recorder.start()
        time.sleep(0.2)

        try:
            run_case(app, url)
        finally:
            time.sleep(0.4)
            recorder.stop()

        expected = QUrl(url).path()
        # 取第一条带路径的请求行做比对
        matched = ""
        for line in recorder.requests:
            if expected and expected in line:
                matched = line
                break

        ok = bool(matched)
        results.append((url, matched or (recorder.requests[0] if recorder.requests else "(未收到请求)"), ok))
        print(f"  => {'路径完整' if ok else '*** 路径不匹配 ***'}")

    print("\n" + "=" * 76)
    print("汇总")
    print("=" * 76)
    bad = 0
    for url, seen, ok in results:
        if not ok:
            bad += 1
        print(f"  [{'正常' if ok else '异常'}] {url}")
        print(f"          服务器收到: {seen}")
    print("-" * 76)
    print(f"共 {len(results)} 项，路径异常 {bad} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
