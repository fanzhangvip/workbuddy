"""播放列表与历史记录：数据模型 + JSON 持久化。

## 为什么用 JSON 文件而不是 QSettings

QSettings（INI）适合"键值对"形态的配置，不适合**有序列表**：它没有数组语义，
存列表只能拼成字符串再解析回来，等于自己实现一遍序列化，还容易在中文/特殊字符上翻车。
播放列表天然是"有序列表"，所以直接写 JSON——也便于用户自己查看和编辑。

## 两个必须处理的现实问题

1. **写入必须是原子的。** 用户可能有几千条历史，写到一半断电/进程被杀，
   下次启动就会因为半个 JSON 而全量丢失。做法是"写临时文件 → os.replace"，
   `os.replace` 在 Windows 上也是原子的覆盖。

2. **读坏了不能静默清空。** 文件损坏时直接把坏文件改名成 `.bak` 并重新开始，
   同时在状态栏/日志里说清楚——静默清空等于毁掉用户数据还瞒着不说。

## 拖拽排序的实现取舍

Qt 的自定义模型做内部拖拽，标准路子是实现 mimeData/dropMimeData，
但 `application/x-qabstractitemmodeldatalist` 的编解码相当绕（要手工解 QDataStream）。
这里改用更可控的方式：视图层把"哪几行拖到了哪一行"算出来后调用
`move_rows()`，模型只管数据搬迁。逻辑全部集中在 `move_rows` 里，也好写单元测试。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal

from src.core.media_item import MediaItem
from src.utils.logger import get_logger

log = get_logger(__name__)

#: 历史记录默认上限（条）。太多会让启动时读取与界面渲染都变慢。
DEFAULT_HISTORY_LIMIT = 300

#: 单条 URL 的长度上限。防止异常数据把文件撑爆。
MAX_URL_LENGTH = 8192


class EntryStore:
    """一个 JSON 文件里的一维条目列表。

    文件格式::

        {"version": 1, "items": [{url, title, kind, duration_ms, added_at}, ...]}

    用对象包一层而不是裸数组，是为了以后能加字段（比如排序方式、分组）
    而不破坏兼容。
    """

    VERSION = 1

    def __init__(self, path: Path, *, dedupe: bool = False, max_items: int = 0) -> None:
        self.path = Path(path)
        self.dedupe = dedupe
        self.max_items = int(max_items)

    # ------------------------------------------------------------ 读写
    def load(self) -> list[MediaItem]:
        """读条目。文件不存在/损坏都返回空列表（损坏的文件会被改名留档）。"""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("读取 %s 失败: %s", self.path.name, exc)
            return []

        text = raw.strip()
        if not text:
            return []

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            self._quarantine(f"内容不是合法 JSON（{exc}）")
            return []

        # 兼容裸数组形态（早期版本或用户手工编辑过）
        rows = data.get("items") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            self._quarantine("顶层结构不是列表")
            return []

        items: list[MediaItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            if not url or len(url) > MAX_URL_LENGTH:
                continue
            try:
                items.append(MediaItem.from_dict(row))
            except Exception as exc:  # noqa: BLE001 - 单条坏数据不该毁掉整个列表
                log.debug("跳过无法解析的条目 %r: %s", row, exc)

        if self.max_items:
            items = items[: self.max_items]
        return items

    def save(self, items: Sequence[MediaItem]) -> bool:
        """原子写入。返回是否成功（失败只记日志，不抛给界面）。"""
        payload = {
            "version": self.VERSION,
            "items": [item.to_dict() for item in items],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=1)

        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            # os.replace 覆盖已有文件是原子操作（Windows 上同样成立）
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            log.warning("写入 %s 失败: %s", self.path.name, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _quarantine(self, reason: str) -> None:
        """把读不了的文件改名留档，避免下一次又踩同一个坑。"""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = self.path.with_name(f"{self.path.name}.bad-{stamp}")
        try:
            os.replace(self.path, backup)
            log.error("播放列表文件%s，已改名保留为 %s", reason, backup.name)
        except OSError as exc:
            log.error("播放列表文件%s，且改名失败: %s", reason, exc)


class PlaylistModel(QAbstractListModel):
    """播放列表模型。

    - 支持拖拽排序（`move_rows`），由视图层算好行列后调用；
    - `dedupe=True` 时按 url 去重，重复加入会移动到指定位置（历史记录用）；
    - `max_items > 0` 时超出上限从末尾丢弃（历史记录用）。
    """

    TitleRole = Qt.ItemDataRole.UserRole + 1
    UrlRole = Qt.ItemDataRole.UserRole + 2
    NoteRole = Qt.ItemDataRole.UserRole + 3
    IsLocalRole = Qt.ItemDataRole.UserRole + 4

    #: 列表内拖拽使用的自定义 MIME 类型，载荷是源行号的 JSON 数组。
    #:
    #: 为什么不用 Qt 默认的 `application/x-qabstractitemmodeldatalist`：
    #: QAbstractItemModel::mimeData 的默认实现只序列化 mimeTypes() 里声明的 role，
    #: 而我们的 role 是自定义的（TitleRole 等）。不显式声明 mimeTypes() 的话
    #: 拖出来的 QMimeData 是空的，视图会认为"没什么可拖"，拖拽排序直接失效。
    MIME_TYPE = "application/x-universalurlplayer-rows"

    itemsChanged = Signal()

    def __init__(
        self,
        store: EntryStore | None = None,
        *,
        dedupe: bool = False,
        max_items: int = 0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._items: list[MediaItem] = []
        self._store = store
        self._dedupe = bool(dedupe)
        self._max_items = int(max_items)
        self._autosave = store is not None

    # ------------------------------------------------------------ 载入与保存
    def load(self) -> int:
        """从磁盘载入。返回载入的条目数。"""
        if self._store is None:
            return 0
        self.beginResetModel()
        self._items = self._store.load()
        self.endResetModel()
        self.itemsChanged.emit()
        return len(self._items)

    def save(self) -> bool:
        if self._store is None:
            return False
        return self._store.save(self._items)

    def set_autosave(self, enabled: bool) -> None:
        """是否每次改动都落盘。批量操作时可以先关掉，最后一次写。"""
        self._autosave = bool(enabled)

    def _changed(self) -> None:
        if self._autosave:
            self.save()
        self.itemsChanged.emit()

    # ------------------------------------------------------------ 只读访问
    def items(self) -> list[MediaItem]:
        """当前条目的浅拷贝（外部修改不会影响模型）。"""
        return list(self._items)

    def item_at(self, row: int) -> MediaItem | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def index_of_url(self, url: str) -> int:
        """按 url 找行号，找不到返回 -1。"""
        target = (url or "").strip()
        for i, item in enumerate(self._items):
            if item.url == target:
                return i
        return -1

    def urls(self) -> list[str]:
        return [item.url for item in self._items]

    # ------------------------------------------------------------ Qt 模型接口
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008 - Qt 命名
        if parent.isValid():
            return 0
        return len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        item = self.item_at(index.row())
        if item is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title or item.url
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(item)
        if role == self.TitleRole:
            return item.title or item.url
        if role == self.UrlRole:
            return item.url
        if role == self.NoteRole:
            return self._note(item)
        if role == self.IsLocalRole:
            return item.is_local
        return None

    @staticmethod
    def _note(item: MediaItem) -> str:
        """列表右侧显示的一小段说明（类型 + 时长）。"""
        bits: list[str] = []
        if item.is_local:
            bits.append("本地")
        else:
            bits.append(item.kind.value.replace("_", " "))
        if item.duration_ms > 0:
            total = item.duration_ms // 1000
            hours, rem = divmod(total, 3600)
            minutes, seconds = divmod(rem, 60)
            text = f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
            bits.append(text)
        return " · ".join(bits)

    @staticmethod
    def _tooltip(item: MediaItem) -> str:
        lines = [item.title or item.url, "", item.url]
        if item.resolver_note:
            lines.append(f"解析：{item.resolver_note}")
        return "\n".join(lines)

    def flags(self, index):  # noqa: N802
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.isValid():
            # 可拖可放 >> 列表内拖拽排序
            return base | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
        # 无效索引代表"空白区域"，允许放进来（拖到列表末尾）
        return base | Qt.ItemFlag.ItemIsDropEnabled

    # ------------------------------------------------------------ 拖拽载荷
    def mimeTypes(self) -> list[str]:  # noqa: N802
        return [self.MIME_TYPE]

    def supportedDropActions(self):  # noqa: N802
        return Qt.DropAction.MoveAction

    def mimeData(self, indexes):  # noqa: N802
        """拖拽载荷：源行号数组 + 一份纯文本 URL（拖到外部程序时有用）。"""
        from PySide6.QtCore import QMimeData

        rows = sorted({index.row() for index in indexes if index.isValid()})
        payload = QMimeData()
        payload.setData(self.MIME_TYPE, json.dumps(rows).encode("utf-8"))
        payload.setText(
            "\n".join(
                self._items[row].url for row in rows if 0 <= row < len(self._items)
            )
        )
        return payload

    @staticmethod
    def rows_from_mime(mime) -> list[int]:
        """从拖拽载荷里解析源行号；不是本模型的载荷则返回空列表。"""
        if mime is None or not mime.hasFormat(PlaylistModel.MIME_TYPE):
            return []
        try:
            raw = bytes(mime.data(PlaylistModel.MIME_TYPE)).decode("utf-8")
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError, TypeError):
            return []
        if not isinstance(data, list):
            return []
        return [int(row) for row in data if isinstance(row, (int, float))]

    # ------------------------------------------------------------ 增删改
    def add(
        self,
        items: MediaItem | Iterable[MediaItem],
        *,
        to_top: bool = False,
    ) -> int:
        """加入若干条目，返回实际新增的条数。

        Args:
            items: 单个条目或条目序列。
            to_top: 插到最前面（历史记录的默认行为）。

        `dedupe=True` 时命中已有 url 的条目不会新增，而是被移到最前
        （历史记录的常见语义：最近看过的排最上面）。
        """
        batch = [items] if isinstance(items, MediaItem) else list(items)
        batch = [item for item in batch if item is not None and item.url]
        if not batch:
            return 0

        now = time.time()
        for offset, item in enumerate(batch):
            if not item.added_at:
                item.added_at = now + offset * 1e-6   # 保持加入顺序稳定

        added = 0
        incoming: list[MediaItem] = []

        for item in batch:
            if self._dedupe:
                existing = self.index_of_url(item.url)
                if existing >= 0:
                    # 已存在：换成它的实例并提到最前，不新增
                    old = self._items.pop(existing)
                    if not old.title and item.title:
                        old.title = item.title
                    old.added_at = item.added_at
                    incoming.append(old)
                    continue
            incoming.append(item)
            added += 1

        if not incoming:
            return 0

        self.beginResetModel()
        # 一次性插入整块，保持 incoming 的原有次序。
        # （逐条 insert(0, x) 会把顺序倒过来，这是个很容易写错的点。）
        if to_top:
            self._items[0:0] = incoming
        else:
            self._items.extend(incoming)
        self._trim()
        self.endResetModel()

        self._changed()
        return added

    def _trim(self) -> None:
        if self._max_items and len(self._items) > self._max_items:
            del self._items[self._max_items:]

    def remove_rows(self, rows: Sequence[int]) -> int:
        """删除指定行（可多选）。返回删除条数。"""
        valid = sorted({r for r in rows if 0 <= r < len(self._items)}, reverse=True)
        if not valid:
            return 0
        for row in valid:
            del self._items[row]
        self._changed()
        return len(valid)

    def clear(self) -> None:
        if not self._items:
            return
        self.beginResetModel()
        self._items.clear()
        self.endResetModel()
        self._changed()

    def move_rows(self, rows: Sequence[int], insert_before: int) -> bool:
        """把若干行移动到 `insert_before` 之前。

        `insert_before` 是**原始列表**中的行号，等于行数表示移到末尾。
        返回是否真的发生了变化。
        """
        valid = sorted({r for r in rows if 0 <= r < len(self._items)})
        if not valid:
            return False

        insert_before = max(0, min(int(insert_before), len(self._items)))
        # 拖到自己身上/紧邻位置：没有实际变化，别做无谓的重置（会闪一下）
        if len(valid) == 1 and insert_before in (valid[0], valid[0] + 1):
            return False
        if valid == list(range(valid[0], valid[0] + len(valid))) and (
            insert_before == valid[0] or insert_before == valid[-1] + 1
        ):
            return False

        if len(valid) == 1:
            # 单行拖拽走 Qt 的原生 moveRows：视图能平滑处理，不丢选中态
            src = valid[0]
            dest = insert_before - 1 if insert_before > src else insert_before
            self.beginMoveRows(QModelIndex(), src, src, QModelIndex(), insert_before)
            item = self._items.pop(src)
            self._items.insert(dest, item)
            self.endMoveRows()
            self._changed()
            return True

        # 多选拖拽：整体重置，结果一定正确（顺序保持原有相对次序）
        moving = [self._items[r] for r in valid]
        removed_before = sum(1 for r in valid if r < insert_before)
        rest = [item for i, item in enumerate(self._items) if i not in set(valid)]
        target = insert_before - removed_before
        target = max(0, min(target, len(rest)))

        self.beginResetModel()
        self._items = rest[:target] + moving + rest[target:]
        self.endResetModel()
        self._changed()
        return True


def default_referer_for(url: str) -> str:
    """由地址推导默认 Referer（站点根地址）。

    做防盗链测试时，`https://a.com/x/y.mp4` 的 Referer 通常是
    `https://a.com/`；给了这个默认值，多数站点就能通过校验。
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/"
