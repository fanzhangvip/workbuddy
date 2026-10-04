"""下载到本地：直链走 requests 流式，网页地址走 yt-dlp（带进度回调）。

## 两条完全不同的下载路径

- **直链 / HLS / RTSP**：自己用 requests 流式拉取。可控性最好——能续传、
  能精确报进度、能随时取消。
- **网页地址**：交给 yt-dlp。它要处理签名、分片、Referer 这些站点细节，
  自己实现不现实，所以只挂 `progress_hooks` 把进度取回来。

## 网页地址下载必须面对的现实：合流需要 ffmpeg

B 站只提供 DASH 分离流（画面一条、声音一条），要得到带声音的单文件就必须
**合流**，而合流需要外部 ffmpeg。本项目没有内置 ffmpeg.exe，所以要如实分情况：

- 有 ffmpeg → 用 `bestvideo*+bestaudio/best`，下完自动合流，得到完整文件；
- 没有 ffmpeg → 退化为"下载一条自带声音的 progressive 流"；
  实在没有这种流（B 站就是）就明确告诉用户"这条只有画面、没有声音，
  装了 ffmpeg 才能合流"，而不是悄悄给一个没声音的文件。

## 避免半成品污染目录

所有下载先写 `<名字>.part`，完成后才改成正式名字。中断时留下 `.part`，
下次下同一个地址可以基于它续传（Range 请求），不会出现"文件名对了但内容是半个文件"。
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal

from src.utils.logger import get_logger
from src.utils.paths import app_data_dir, default_download_dir

log = get_logger(__name__)

#: 进度信号的最小间隔（秒）。下载线程可能每秒上千次回调，不节流会把 GUI 线程打爆。
_PROGRESS_INTERVAL_S = 0.2

#: 分块读取大小。
#:
#: **这个值同时决定了两件事的响应速度，实测过，别随手调大。**
#: requests 的 `iter_content(n)` 会一直阻塞到凑满 n 字节才返回（不是"有多少给多少"），
#: 所以：
#:   · 进度更新的最细粒度 = 这个值（用 256KB 时，前 256KB 完全看不到变化）；
#:   · **取消是否灵敏** = 这个值（256KB 时，取消要等整整一个 256KB 读完才干，
#:     在慢速连接上表现为"点了取消没反应"）。
#: 实测：服务端 4KB/50ms 时，64KB 分块每 0.8s 才返回一次，8KB 分块每 0.1s 一次。
#: 32KB 是"进度够细、取消够快、又不会把 CPU 耗在循环上"的折中。
_CHUNK_SIZE = 32 * 1024

#: 请求超时（连接, 读取）
_TIMEOUT = (15, 60)

#: Windows 文件名非法字符
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class DownloadState:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    #: 还在排队或正在跑的状态
    ACTIVE = (QUEUED, RUNNING)
    #: 终态
    FINISHED = (DONE, FAILED, CANCELLED)


@dataclass
class DownloadTask:
    """一次下载任务的全部状态。"""

    task_id: int
    url: str
    dest: Path                       # 目标文件路径（最终名字）
    title: str = ""
    page_url: str = ""               # 网页地址下载时，用户输入的那个页面地址
    headers: dict = field(default_factory=dict)
    is_web: bool = False

    state: str = DownloadState.QUEUED
    received: int = 0
    total: int = 0                   # 0 表示服务端没给 Content-Length
    error: str = ""
    note: str = ""                   # 附加说明（如"已合流""仅画面"）
    started_at: float = 0.0
    finished_at: float = 0.0
    _speed_samples: list = field(default_factory=list, repr=False)

    # ------------------------------------------------------------ 展示辅助
    @property
    def part_path(self) -> Path:
        return self.dest.with_name(self.dest.name + ".part")

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at) if self.started_at else 0.0

    @property
    def speed(self) -> float:
        """字节/秒。用最近几次采样算，比"总量/总时长"更能反映当前速度。"""
        samples = self._speed_samples
        if len(samples) < 2:
            return self.received / self.elapsed if self.elapsed > 0.1 else 0.0
        first_t, first_b = samples[0]
        last_t, last_b = samples[-1]
        span = last_t - first_t
        if span <= 0:
            return 0.0
        return max(0.0, (last_b - first_b) / span)

    @property
    def percent(self) -> float:
        if self.total <= 0:
            return 0.0
        return max(0.0, min(100.0, self.received * 100.0 / self.total))

    @property
    def eta(self) -> float:
        """预计剩余秒数；无法估算时返回 0。"""
        speed = self.speed
        if speed <= 0 or self.total <= 0:
            return 0.0
        return max(0.0, (self.total - self.received) / speed)

    def progress_text(self) -> str:
        """一行给用户看的进度描述。"""
        if self.state == DownloadState.QUEUED:
            return "等待中"
        if self.state == DownloadState.CANCELLED:
            return f"已取消（{format_size(self.received)}）"
        if self.state == DownloadState.FAILED:
            return f"失败：{self.error}"
        if self.state == DownloadState.DONE:
            return f"完成 · {format_size(self.received or _file_size(self.dest))}"

        parts = [format_size(self.received)]
        if self.total > 0:
            parts.append(f"/ {format_size(self.total)}（{self.percent:.1f}%）")
        if self.speed > 0:
            parts.append(f"· {format_size(int(self.speed))}/s")
        eta = self.eta
        if eta > 0:
            parts.append(f"· 剩余 {format_seconds(eta)}")
        return " ".join(parts)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def format_size(size: float) -> str:
    """把字节数格式化成人看得懂的大小。"""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def format_seconds(seconds: float) -> str:
    total = int(max(0.0, seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def safe_filename(name: str, *, fallback: str = "download", max_length: int = 120) -> str:
    """把标题清理成合法的文件名。

    去非法字符、去掉结尾的点和空格（Windows 不允许）、避开保留设备名。
    """
    text = _ILLEGAL_CHARS.sub("_", str(name or "")).strip()
    text = text.rstrip(". ")
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_length:
        text = text[:max_length].rstrip(". ")
    if not text:
        text = fallback
    stem = text.split(".")[0].upper()
    if stem in _RESERVED_NAMES:
        text = "_" + text
    return text


def guess_extension(url: str, fallback: str = ".mp4") -> str:
    """从地址猜扩展名（忽略掉 query 里的参数）。"""
    from urllib.parse import urlparse

    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    if ext and len(ext) <= 6 and ext[1:].isalnum():
        return ext
    return fallback


def unique_path(directory: Path, filename: str, *, consider_part: bool = True) -> Path:
    """在目录里找一个不冲突的文件名（`a.mp4` → `a (2).mp4`）。

    `consider_part=True` 时把 `<名字>.part` 也算作"被占用"——两个任务同时下同一个
    名字会互相写坏对方的分片文件。要**续传**旧任务时必须传 `consider_part=False`
    （或直接用 DownloadManager.start 的 dest 参数），否则永远命不中已有的 .part，
    续传就成了摆设。
    """
    directory = Path(directory)
    target = directory / filename

    def taken(candidate: Path) -> bool:
        if candidate.exists():
            return True
        return consider_part and candidate.with_name(candidate.name + ".part").exists()

    if not taken(target):
        return target

    stem, ext = os.path.splitext(filename)
    for index in range(2, 1000):
        candidate = directory / f"{stem} ({index}){ext}"
        if not taken(candidate):
            return candidate
    return directory / f"{stem} ({int(time.time())}){ext}"


# ------------------------------------------------------------------ ffmpeg
_ffmpeg_cache: tuple[bool, str] | None = None


def _canonical_ffmpeg(source: str) -> str:
    """给文件名不标准的 ffmpeg 补一个标准名入口，返回可用路径。

    为什么必须多这一步：imageio-ffmpeg 自带的那份叫
    `ffmpeg-win-x86_64-v7.1.exe`。我们自己调它没问题，但 **yt-dlp 认不出来**——
    它拿到 `ffmpeg_location` 后只会在同目录里找 `ffmpeg(.exe)`，找不到就判定
    "ffmpeg 未安装"，于是 DASH 分离流直接拒绝合流，报
    `You have requested downloading the video partially, but ffmpeg is not
    installed. Aborting`。表现就是"明明装了 ffmpeg，下载下来还是没声音"。

    优先建硬链接（同一份数据，不占额外空间），不支持时退回复制。
    实在建不了就返回原路径——至少我们自己的调用还能用。
    """
    path = Path(source)
    if path.name.lower() in ("ffmpeg", "ffmpeg.exe"):
        return source

    target_dir = app_data_dir() / "bin"
    target = target_dir / "ffmpeg.exe"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        same = target.is_file() and target.stat().st_size == path.stat().st_size
        if not same:
            target.unlink(missing_ok=True)
            try:
                os.link(path, target)
            except OSError:
                shutil.copy2(path, target)
    except OSError as exc:  # noqa: BLE001
        log.warning("无法为 ffmpeg 建立标准文件名入口，yt-dlp 可能用不了它: %s", exc)
        return source

    log.info("已为标准名之外的 ffmpeg 建立入口: %s -> %s", source, target)
    return str(target)


def _prepare_ffmpeg(source: str) -> str:
    """把找到的 ffmpeg 收拾成"外部工具也能用"的状态，返回可用路径。

    两件事，缺一不可（这两处踩的是不同组件）：

    1. **名字要标准**：yt-dlp 的后处理器按 `ffmpeg(.exe)` 这个名字在同目录里
       找，imageio-ffmpeg 那份叫 `ffmpeg-win-x86_64-v7.1.exe`，直接给它就认不出
       —— 见 `_canonical_ffmpeg()`。
    2. **路径要进 `%PATH%`**：yt-dlp 的 `FFmpegFD`（外部下载器，负责
       "只下载某一段"这类功能）**完全不看 `ffmpeg_location` 参数，只认 PATH**，
       找不到就报 `ffmpeg is not installed`。既然我们已经把 ffmpeg 找出来了，
       就顺手让 PATH 也知道它。
    """
    path = _canonical_ffmpeg(source)
    folder = str(Path(path).parent)
    entries = os.environ.get("PATH", "").split(os.pathsep)
    if folder not in entries:
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
    return path


def find_ffmpeg() -> tuple[bool, str]:
    """找一个可用的 ffmpeg 可执行文件。

    顺序：设置里指定的路径 → 系统 PATH → imageio-ffmpeg 自带的那个。
    返回 (是否找到, 路径或说明)。

    注意：非标准文件名的那份会经 `_canonical_ffmpeg()` 换成标准名入口，
    否则 yt-dlp 用不了它（见那个函数的说明）。
    """
    global _ffmpeg_cache
    if _ffmpeg_cache is not None:
        return _ffmpeg_cache

    try:
        from src.utils import settings as S

        configured = S.get_str(S.K_FFMPEG_PATH, "").strip()
        if configured:
            path = Path(os.path.expandvars(os.path.expanduser(configured)))
            if path.is_file():
                _ffmpeg_cache = (True, _prepare_ffmpeg(str(path)))
                return _ffmpeg_cache
            log.warning("设置的 ffmpeg 路径不可用，将自动查找: %s", configured)
    except Exception as exc:  # noqa: BLE001
        log.debug("读取 ffmpeg 路径配置失败: %s", exc)

    found = shutil.which("ffmpeg")
    if found:
        _ffmpeg_cache = (True, _prepare_ffmpeg(found))
        return _ffmpeg_cache

    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            _ffmpeg_cache = (True, _prepare_ffmpeg(exe))
            return _ffmpeg_cache
    except Exception as exc:  # noqa: BLE001 - 没装就是预期情形
        log.debug("imageio-ffmpeg 不可用: %s", exc)

    _ffmpeg_cache = (False, "")
    return _ffmpeg_cache


def reset_ffmpeg_cache() -> None:
    """清掉 ffmpeg 查找缓存（设置里改了路径后调用）。"""
    global _ffmpeg_cache
    _ffmpeg_cache = None


FFMPEG_HINT = (
    "网页地址（B站/YouTube 等）常把画面和声音分成两条流，"
    "合并成带声音的文件需要 ffmpeg。当前系统上没有找到 ffmpeg，"
    "所以只能下载到没有声音的画面流。\n\n"
    "解决办法（任选其一）：\n"
    "1. 安装 ffmpeg 并加入 PATH（https://ffmpeg.org/download.html）；\n"
    "2. 安装 imageio-ffmpeg（它自带一个 ffmpeg 可执行文件）：\n"
    "   ./.venv/Scripts/python.exe -m pip install imageio-ffmpeg "
    "-i https://mirrors.aliyun.com/pypi/simple\n"
    "3. 在设置里指定「ffmpeg 路径」。"
)


class _Cancelled(Exception):
    """内部信号：把下载线程从阻塞的读取里拽出来。"""


class DownloadManager(QObject):
    """下载任务管理器：排队、限流、进度上报、取消。

    所有任务跑在 Python 工作线程里，进度通过 Qt 信号回到主线程。
    和地址诊断一样用 `QueuedConnection` 显式声明跨线程投递，
    不要指望默认连接类型在所有情况下都判对。
    """

    #: 工作线程 → 主线程（强制队列投递）
    progressReady = Signal(object)
    #: 面向 UI：任务新增 / 进度更新 / 状态变化
    taskAdded = Signal(object)
    taskUpdated = Signal(object)
    taskFinished = Signal(object)
    #: 状态栏消息
    statusMessage = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tasks: dict[int, DownloadTask] = {}
        self._order: list[int] = []
        self._locks: dict[int, threading.Event] = {}
        self._threads: dict[int, threading.Thread] = {}
        self._counter = 0
        self._concurrency = 2
        self._download_dir: Path | None = None
        self._stop_all = False

        self.progressReady.connect(
            self._on_progress, Qt.ConnectionType.QueuedConnection
        )

    # ------------------------------------------------------------ 配置
    @property
    def download_dir(self) -> Path:
        """当前下载目录。每次读取都回落到设置（用户可能在设置里改过）。"""
        if self._download_dir is not None:
            return self._download_dir
        return self._configured_dir()

    @staticmethod
    def _configured_dir() -> Path:
        from src.utils import settings as S

        configured = S.get_str(S.K_DOWNLOAD_DIR, "").strip()
        if configured:
            path = Path(os.path.expandvars(os.path.expanduser(configured)))
            try:
                path.mkdir(parents=True, exist_ok=True)
                return path
            except OSError as exc:
                log.warning("配置的下载目录不可用（%s），改用默认目录: %s", exc, path)
        target = default_download_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return target

    def set_download_dir(self, path: str | Path | None) -> None:
        """覆盖下载目录（None 表示回到设置里的值）。"""
        self._download_dir = Path(path) if path else None

    def set_concurrency(self, count: int) -> None:
        self._concurrency = max(1, min(8, int(count)))

    # ------------------------------------------------------------ 查询
    def tasks(self) -> list[DownloadTask]:
        return [self._tasks[tid] for tid in self._order if tid in self._tasks]

    def task(self, task_id: int) -> DownloadTask | None:
        return self._tasks.get(task_id)

    def active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.state in DownloadState.ACTIVE)

    def has_active(self) -> bool:
        return self.active_count() > 0

    # ------------------------------------------------------------ 启动
    def start(
        self,
        url: str,
        *,
        title: str = "",
        headers: dict | None = None,
        is_web: bool = False,
        dest_dir: Path | None = None,
        dest: Path | None = None,
    ) -> DownloadTask | None:
        """新建一个下载任务并尽快开始。返回任务对象；地址为空时返回 None。

        Args:
            url: 要下载的地址。
            title: 用于生成文件名。
            headers: 需要注入的请求头（防盗链）。
            is_web: 是否网页地址（决定走 yt-dlp 还是 requests）。
            dest_dir: 覆盖下载目录。
            dest: 直接指定目标文件路径。**重试/续传时应该传它**——
                否则会另起一个 "(2)" 名字，已有的 `.part` 就用不上了。
        """
        target = (url or "").strip()
        if not target:
            self.statusMessage.emit("下载失败：地址为空")
            return None

        directory = Path(dest_dir) if dest_dir else self.download_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.statusMessage.emit(f"下载目录不可用：{exc}")
            return None

        if dest is not None:
            resolved_dest = Path(dest)
            resolved_dest.parent.mkdir(parents=True, exist_ok=True)
        else:
            name = safe_filename(title or Path(target).name or "download")
            extension = "" if is_web else guess_extension(target)
            # 没给标题时，名字就是从地址最后一段取的，扩展名通常已经在里面了，
            # 再拼一次会得到 "video.mp4.mp4" 这种文件名（每个直链都会中招）。
            if extension and name.lower().endswith(extension.lower()):
                extension = ""
            # 网页地址的最终扩展名由 yt-dlp 决定，这里先给个占位名做 `.part` 用途
            resolved_dest = unique_path(
                directory, f"{name}{extension}" if extension else name
            )

        self._counter += 1
        task = DownloadTask(
            task_id=self._counter,
            url=target,
            dest=resolved_dest,
            title=title or resolved_dest.name,
            headers=dict(headers or {}),
            is_web=is_web,
            page_url=target if is_web else "",
        )
        self._tasks[task.task_id] = task
        self._order.append(task.task_id)
        self._locks[task.task_id] = threading.Event()
        self._stop_all = False

        self.taskAdded.emit(task)
        log.info("新建下载任务 #%d: %s -> %s", task.task_id, target, resolved_dest)
        self._pump()
        return task

    # ------------------------------------------------------------ 调度
    def _pump(self) -> None:
        """在并发上限内启动排队的任务。"""
        for task in self.tasks():
            if len(self._threads) >= self._concurrency:
                break
            if task.state != DownloadState.QUEUED:
                continue
            self._spawn(task)

    def _spawn(self, task: DownloadTask) -> None:
        task.state = DownloadState.RUNNING
        task.started_at = time.time()
        worker = threading.Thread(
            target=self._run,
            args=(task,),
            name=f"download-{task.task_id}",
            daemon=True,
        )
        self._threads[task.task_id] = worker
        worker.start()
        self.taskUpdated.emit(task)

    def cancel(self, task_id: int) -> None:
        """取消一个任务（排队中的会直接标记为已取消）。"""
        task = self._tasks.get(task_id)
        if task is None or task.state in DownloadState.FINISHED:
            return
        event = self._locks.get(task_id)
        if event is not None:
            event.set()
        if task.state == DownloadState.QUEUED:
            task.state = DownloadState.CANCELLED
            task.finished_at = time.time()
            self.taskFinished.emit(task)
            return
        log.info("请求取消下载 #%d", task_id)

    def cancel_all(self) -> None:
        self._stop_all = True
        for task_id in list(self._tasks):
            self.cancel(task_id)

    def clear_finished(self) -> int:
        """从列表里移除所有已结束的任务。返回移除数量。"""
        done = [tid for tid, t in self._tasks.items() if t.state in DownloadState.FINISHED]
        for task_id in done:
            self._tasks.pop(task_id, None)
            self._locks.pop(task_id, None)
            self._threads.pop(task_id, None)
            if task_id in self._order:
                self._order.remove(task_id)
        return len(done)

    # ------------------------------------------------------------ 工作线程
    def _run(self, task: DownloadTask) -> None:
        try:
            if task.is_web:
                self._run_web(task)
            else:
                self._run_direct(task)
        except _Cancelled:
            task.state = DownloadState.CANCELLED
            task.finished_at = time.time()
            log.info("下载 #%d 已取消", task.task_id)
        except Exception as exc:  # noqa: BLE001 - 任何异常都变成"下载失败"
            task.state = DownloadState.FAILED
            task.error = _friendly_download_error(exc)
            task.finished_at = time.time()
            log.warning("下载 #%d 失败: %s", task.task_id, exc)
        finally:
            self._threads.pop(task.task_id, None)
            self.progressReady.emit(task)
            self.taskFinished.emit(task)
            # 空出来的并发位给排队的任务
            self._pump()

    # ------------------------------------------------------------ 直链下载
    def _run_direct(self, task: DownloadTask) -> None:
        import requests

        part = task.part_path
        part.parent.mkdir(parents=True, exist_ok=True)

        headers = dict(task.headers)
        # 已有半成品就续传：Range 请求 + 保留已有字节
        existing = _file_size(part)
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"
            task.received = existing
            task.note = "已续传"

        proxies = _proxies_for(task.url)
        cancel = self._locks[task.task_id]

        with requests.get(
            task.url, headers=headers, stream=True, timeout=_TIMEOUT, proxies=proxies
        ) as response:
            if response.status_code == 416:
                # Range 起点越界：服务端在说"你要的那段不存在"。
                # 对续传场景这通常意味着 .part 已经就是完整文件了，直接收工，
                # 比报一个用户看不懂的 416 错误有用得多。
                log.info("服务端返回 416，视已有分片为完整文件: %s", part.name)
                _replace_file(part, task.dest)
                task.state = DownloadState.DONE
                task.received = _file_size(task.dest)
                task.total = task.received
                task.note = "已有分片即是完整文件"
                task.finished_at = time.time()
                return
            if response.status_code not in (200, 206):
                raise OSError(f"服务器返回 {response.status_code} {response.reason}")
            if existing > 0 and response.status_code == 200:
                # 服务器不认 Range，只能从头下
                existing = 0
                task.received = 0
                task.note = ""

            length = int(response.headers.get("Content-Length") or 0)
            task.total = length + existing if length else 0
            # 服务端给的名字更可信时用它（但保留我们自己的扩展名判断）
            task._speed_samples = [(time.time(), task.received)]

            mode = "ab" if existing else "wb"
            with open(part, mode) as handle:
                last_emit = 0.0
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if cancel.is_set() or self._stop_all:
                        raise _Cancelled
                    if not chunk:
                        continue
                    handle.write(chunk)
                    task.received += len(chunk)
                    now = time.time()
                    if now - last_emit >= _PROGRESS_INTERVAL_S:
                        last_emit = now
                        _push_sample(task, now)
                        self.progressReady.emit(task)

        if task.total == 0:
            task.total = task.received

        # 完成了才改成正式名字：中途失败留下 .part，下次能续
        _replace_file(part, task.dest)
        task.state = DownloadState.DONE
        task.received = _file_size(task.dest) or task.received
        task.finished_at = time.time()
        if not task.note:
            task.note = ""
        log.info("下载完成 #%d: %s", task.task_id, task.dest)

    # ------------------------------------------------------------ 网页地址下载
    def _run_web(self, task: DownloadTask) -> None:
        from yt_dlp import YoutubeDL

        cancel = self._locks[task.task_id]
        has_ffmpeg, ffmpeg_path = find_ffmpeg()

        if not has_ffmpeg:
            task.note = "未找到 ffmpeg，只能下到没有声音的画面流"

        # 有 ffmpeg 就能合流，尽量取最高画质；没有就只能要一条自带声音的
        fmt = "bv*+ba/b" if has_ffmpeg else "b[acodec!=none][vcodec!=none]/b"

        options: dict = {
            "outtmpl": str(task.dest.parent / f"{task.dest.name}.%(ext)s"),
            "format": fmt,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "cachedir": False,
            "no_color": True,
            "retries": 2,
            "restrictfilenames": False,
            "windowsfilenames": True,
            "progress_hooks": [lambda data: self._on_ytdlp_progress(task, data, cancel)],
            "postprocessor_hooks": [lambda data: self._on_ytdlp_postprocess(task, data)],
            "logger": _QuietLogger(),
        }
        options.update(_network_options())
        proxies = _proxies_for(task.url)
        if proxies:
            value = proxies.get("https") or proxies.get("http")
            # 本机地址时 value 是 None，给空串才是"明确不用代理"（yt-dlp 的
            # --proxy "" 语义）；不设这一项它会去读环境变量里的代理并失败。
            options["proxy"] = value or ""
        if task.headers:
            options["http_headers"] = dict(task.headers)
        if has_ffmpeg:
            options["ffmpeg_location"] = ffmpeg_path

        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(task.url, download=True)

        # yt-dlp 会用真实扩展名，把文件名找回来
        final = self._locate_output(task, info)
        if final is None:
            raise OSError("下载结束但找不到输出文件")
        task.dest = final
        if _file_size(final) <= 0:
            raise OSError("下载完成但文件是空的")
        task.state = DownloadState.DONE
        task.received = _file_size(final)
        task.total = task.received
        task.finished_at = time.time()

        # 只有画面时如实标注，别让用户以为是完整文件
        if not has_ffmpeg and _video_only(info):
            task.note = "仅画面（无声音）：需要 ffmpeg 才能合流"
        elif not task.note:
            task.note = "已下载"
        log.info("网页下载完成 #%d: %s", task.task_id, final)

    def _locate_output(self, task: DownloadTask, info: object) -> Path | None:
        """定位 yt-dlp 实际写出的文件。

        yt-dlp 的扩展名由格式决定（可能是 .mkv/.webm），和我们的占位名不一样，
        所以先看它回传的 filepath，再按前缀在目录里找。
        """
        candidates: list[Path] = []
        if isinstance(info, dict):
            requested = info.get("requested_downloads")
            if isinstance(requested, list):
                for entry in requested:
                    if isinstance(entry, dict) and entry.get("filepath"):
                        candidates.append(Path(str(entry["filepath"])))
            for key in ("filepath", "_filename"):
                if info.get(key):
                    candidates.append(Path(str(info[key])))
            if info.get("_type") == "playlist":
                for entry in info.get("entries") or []:
                    if isinstance(entry, dict) and entry.get("filepath"):
                        candidates.append(Path(str(entry["filepath"])))

        for path in candidates:
            if path.is_file() and path.stat().st_size > 0:
                return path

        # 回退：按我们给的 outtmpl 前缀扫目录
        prefix = f"{task.dest.name}."
        try:
            for path in sorted(
                task.dest.parent.glob(f"{prefix}*"),
                key=lambda p: _file_size(p),
                reverse=True,
            ):
                if path.is_file() and path.suffix != ".part" and path.stat().st_size > 0:
                    return path
        except OSError:
            pass
        return None

    def _on_ytdlp_progress(self, task: DownloadTask, data: dict, cancel: threading.Event) -> None:
        """yt-dlp 的进度回调。

        这里**不能抛 _Cancelled 以外的异常**：yt-dlp 会把回调里的异常当成
        下载错误处理，报出来很像网络故障，会误导用户。
        """
        if cancel.is_set() or self._stop_all:
            raise _Cancelled

        status = str(data.get("status") or "")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            task.total = int(total or 0)
            task.received = int(data.get("downloaded_bytes") or 0)
            now = time.time()
            _push_sample(task, now)
            try:
                if data.get("speed"):
                    task.note = ""
            except Exception:  # noqa: BLE001
                pass
            self.progressReady.emit(task)
        elif status == "finished":
            task.received = int(data.get("total_bytes") or task.received)
            self.progressReady.emit(task)

    def _on_ytdlp_postprocess(self, task: DownloadTask, data: dict) -> None:
        if str(data.get("status") or "") == "started":
            name = str(data.get("postprocessor") or "")
            if "Merge" in name or "FFmpegMerger" in name:
                task.note = "正在合流画面与声音…"
                self.progressReady.emit(task)

    # ------------------------------------------------------------ 主线程槽
    def _on_progress(self, task: object) -> None:
        if isinstance(task, DownloadTask):
            self.taskUpdated.emit(task)


def _push_sample(task: DownloadTask, now: float) -> None:
    """记录速度采样点，只保留最近若干个。"""
    samples = task._speed_samples
    samples.append((now, task.received))
    if len(samples) > 6:
        del samples[:-6]


def _video_only(info: object) -> bool:
    """判断 yt-dlp 下到的是不是"没有声音"的画面流。"""
    if not isinstance(info, dict):
        return False
    codec = info.get("acodec")
    return codec is not None and str(codec).strip().lower() == "none"


def _replace_file(source: Path, target: Path) -> None:
    """把 `.part` 改成正式名字。目标已存在时先删掉（Windows 上 rename 不覆盖）。"""
    try:
        if target.exists():
            target.unlink()
        os.replace(source, target)
    except OSError as exc:
        raise OSError(f"无法把临时文件改名为 {target.name}：{exc}") from exc


def _proxy_config() -> dict | None:
    """把设置里的代理转成 requests 需要的格式。"""
    from src.utils import settings as S

    proxy = S.get_str(S.K_PROXY, "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _proxies_for(url: str) -> dict | None:
    """该地址该用哪个代理。

    本机地址必须显式绕开代理：环境变量里若设了 `HTTP_PROXY`（公司网络、CI、
    以及本项目的开发沙箱都有），requests 会连 `127.0.0.1` 都往代理送，
    报出 `ProxyError: Unable to connect to proxy`——而那个地址根本不需要代理，
    更是我们自己起的本地反向代理，绕一圈必然失败。

    非本机地址：配置里有代理就用配置的；没有则返回 None，让 requests 按
    环境变量（系统代理）走，这是符合用户预期的默认行为。
    """
    from src.utils.urls import is_loopback_url

    if is_loopback_url(url):
        # 显式给 None 才能压住环境变量里的代理（requests 用的是 setdefault）
        return {"http": None, "https": None}
    return _proxy_config()


def _network_options() -> dict:
    """yt-dlp 的网络相关选项（代理 / cookies / UA）。"""
    from pathlib import Path as _Path

    from src.utils import settings as S

    options: dict = {}
    proxy = S.get_str(S.K_PROXY, "").strip()
    if proxy:
        options["proxy"] = proxy
    cookie_file = S.get_str(S.K_COOKIE_FILE, "").strip()
    if cookie_file and _Path(cookie_file).is_file():
        options["cookiefile"] = cookie_file
    else:
        browser = S.get_str(S.K_COOKIE_FROM_BROWSER, "").strip().lower()
        if browser:
            options["cookiesfrombrowser"] = (browser, None, None, None)
    user_agent = S.get_str(S.K_USER_AGENT, "").strip()
    if user_agent:
        options["http_headers"] = {"User-Agent": user_agent}
    return options


class _QuietLogger:
    """把 yt-dlp 的输出接到应用日志（默认不往界面刷）。"""

    def debug(self, message: str) -> None:
        text = str(message).strip()
        if text and not text.startswith("[debug]"):
            log.debug("下载: %s", text)

    def info(self, message: str) -> None:
        self.debug(message)

    def warning(self, message: str) -> None:
        log.warning("下载: %s", str(message).strip())

    def error(self, message: str) -> None:
        log.error("下载: %s", str(message).strip())


_ERROR_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("no space left", "disk full"), "磁盘空间不足。"),
    (("permission denied", "access is denied"), "没有写入权限，换个下载目录试试。"),
    (("timed out", "timeout", "connection", "ssl", "unreachable"),
     "网络连接失败或超时：请检查网络与代理设置。"),
    (("403", "forbidden"), "服务器拒绝访问（403）：多半需要 Referer 或登录态，"
                           "可在设置里开启本地代理或指定 cookies 文件。"),
    (("404", "not found"), "地址不存在（404）：可能已失效或地址有误。"),
    (("requested format is not available",),
     "该站点没有可用格式（多半需要登录后才能取到）。"),
)


def _friendly_download_error(exc: BaseException) -> str:
    """把各种异常翻译成一句人话。"""
    text = str(exc).lower()
    for needles, message in _ERROR_RULES:
        if any(needle in text for needle in needles):
            return f"{message}（原始信息：{exc}）"
    name = exc.__class__.__name__
    return f"{exc}" if str(exc) else name
