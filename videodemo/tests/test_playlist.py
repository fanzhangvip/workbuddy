"""播放列表与历史记录的模型 / 持久化测试。

重点覆盖三类容易写错的地方：
1. 拖拽排序的**行号换算**（单行、多行、往前移、往后移、移到末尾）；
2. 历史记录的**去重置顶**与批量插入时的**顺序保持**；
3. 持久化的**原子性与损坏容错**（写坏的文件不能把列表静默清空）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.media_item import MediaItem  # noqa: E402
from src.core.playlist import (  # noqa: E402
    EntryStore,
    PlaylistModel,
    default_referer_for,
)


def _items(count: int, prefix: str = "v") -> list[MediaItem]:
    return [MediaItem.from_url(f"https://example.com/{prefix}{i}.mp4") for i in range(count)]


def _titles(model: PlaylistModel) -> list[str]:
    return [item.title for item in model.items()]


class ModelBasicsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.model = PlaylistModel()   # 不带 store，纯内存

    def test_add_single_and_batch(self) -> None:
        self.assertEqual(self.model.add(_items(1)), 1)
        self.assertEqual(self.model.add(_items(3, "w")), 3)
        self.assertEqual(self.model.rowCount(), 4)

    def test_add_ignores_empty_url(self) -> None:
        self.assertEqual(self.model.add(MediaItem.from_url("")), 0)
        self.assertEqual(self.model.rowCount(), 0)

    def test_add_to_top_keeps_batch_order(self) -> None:
        """批量插到头部时不能反序——逐条 insert(0, x) 会倒过来，这是个经典 bug。"""
        self.model.add(_items(3, "old"))
        self.model.add(_items(3, "new"), to_top=True)
        self.assertEqual(_titles(self.model), ["new0.mp4", "new1.mp4", "new2.mp4",
                                              "old0.mp4", "old1.mp4", "old2.mp4"])

    def test_remove_rows_multiple(self) -> None:
        self.model.add(_items(5))
        self.assertEqual(self.model.remove_rows([1, 3]), 2)
        self.assertEqual(_titles(self.model), ["v0.mp4", "v2.mp4", "v4.mp4"])

    def test_remove_rows_ignores_out_of_range(self) -> None:
        self.model.add(_items(2))
        self.assertEqual(self.model.remove_rows([0, 99, -1]), 1)
        self.assertEqual(self.model.rowCount(), 1)

    def test_index_of_url(self) -> None:
        self.model.add(_items(3))
        self.assertEqual(self.model.index_of_url("https://example.com/v1.mp4"), 1)
        self.assertEqual(self.model.index_of_url("https://example.com/nope.mp4"), -1)

    def test_clear(self) -> None:
        self.model.add(_items(3))
        self.model.clear()
        self.assertEqual(self.model.rowCount(), 0)

    def test_added_at_assigned(self) -> None:
        self.model.add(_items(2))
        stamps = [item.added_at for item in self.model.items()]
        self.assertTrue(all(stamp > 0 for stamp in stamps))
        self.assertLess(stamps[0], stamps[1])


class MoveRowsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.model = PlaylistModel()
        self.model.add(_items(5))

    def test_move_single_to_front(self) -> None:
        self.assertTrue(self.model.move_rows([4], 0))
        self.assertEqual(_titles(self.model),
                         ["v4.mp4", "v0.mp4", "v1.mp4", "v2.mp4", "v3.mp4"])

    def test_move_single_to_end(self) -> None:
        self.assertTrue(self.model.move_rows([0], 5))
        self.assertEqual(_titles(self.model),
                         ["v1.mp4", "v2.mp4", "v3.mp4", "v4.mp4", "v0.mp4"])

    def test_move_single_middle_forward(self) -> None:
        # 把第 0 行放到第 3 行之前
        self.assertTrue(self.model.move_rows([0], 3))
        self.assertEqual(_titles(self.model),
                         ["v1.mp4", "v2.mp4", "v0.mp4", "v3.mp4", "v4.mp4"])

    def test_move_single_middle_backward(self) -> None:
        self.assertTrue(self.model.move_rows([3], 1))
        self.assertEqual(_titles(self.model),
                         ["v0.mp4", "v3.mp4", "v1.mp4", "v2.mp4", "v4.mp4"])

    def test_noop_moves_report_false(self) -> None:
        """拖到自己身上 / 紧邻位置不该算变化——否则会白白重置视图。"""
        self.assertFalse(self.model.move_rows([2], 2))
        self.assertFalse(self.model.move_rows([2], 3))

    def test_move_multiple_keeps_relative_order(self) -> None:
        self.assertTrue(self.model.move_rows([0, 1], 4))
        self.assertEqual(_titles(self.model),
                         ["v2.mp4", "v3.mp4", "v0.mp4", "v1.mp4", "v4.mp4"])

    def test_move_multiple_to_front(self) -> None:
        self.assertTrue(self.model.move_rows([3, 4], 0))
        self.assertEqual(_titles(self.model),
                         ["v3.mp4", "v4.mp4", "v0.mp4", "v1.mp4", "v2.mp4"])

    def test_move_ignores_invalid_rows(self) -> None:
        self.assertFalse(self.model.move_rows([99], 0))
        self.assertFalse(self.model.move_rows([], 0))

    def test_move_clamps_oversized_target(self) -> None:
        self.assertTrue(self.model.move_rows([1], 999))
        self.assertEqual(_titles(self.model)[-1], "v1.mp4")

    def test_move_rows_from_mime_json(self) -> None:
        """拖拽载荷解析：只认自己的 MIME，坏数据返回空列表。"""
        from PySide6.QtCore import QMimeData

        payload = QMimeData()
        payload.setData(PlaylistModel.MIME_TYPE, json.dumps([2, 0, 2]).encode())
        self.assertEqual(PlaylistModel.rows_from_mime(payload), [2, 0, 2])

        other = QMimeData()
        other.setText("hello")
        self.assertEqual(PlaylistModel.rows_from_mime(other), [])

        broken = QMimeData()
        broken.setData(PlaylistModel.MIME_TYPE, b"not json")
        self.assertEqual(PlaylistModel.rows_from_mime(broken), [])


class HistorySemanticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.model = PlaylistModel(dedupe=True, max_items=3)

    def test_dedupe_moves_existing_to_top(self) -> None:
        self.model.add(_items(3))
        self.model.add(MediaItem.from_url("https://example.com/v1.mp4"), to_top=True)
        self.assertEqual(_titles(self.model), ["v1.mp4", "v0.mp4", "v2.mp4"])
        # 去重命中不算"新增"
        self.assertEqual(self.model.rowCount(), 3)

    def test_max_items_trims_from_tail(self) -> None:
        """超出上限时从**末尾**丢弃（对"最新在最前"的历史列表，末尾就是最旧的）。"""
        self.model.add(_items(5))
        self.assertEqual(_titles(self.model), ["v0.mp4", "v1.mp4", "v2.mp4"])

    def test_batch_to_top_order_preserved(self) -> None:
        self.model.add(_items(2, "old"))
        self.model.add(_items(2, "new"), to_top=True)
        self.assertEqual(_titles(self.model), ["new0.mp4", "new1.mp4", "old0.mp4"])


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_save_and_load_roundtrip(self) -> None:
        path = self.dir / "playlist.json"
        model = PlaylistModel(EntryStore(path))
        model.add(_items(4))
        titles_before = _titles(model)
        self.assertTrue(path.is_file())

        reloaded = PlaylistModel(EntryStore(path))
        self.assertEqual(reloaded.load(), 4)
        self.assertEqual(_titles(reloaded), titles_before)

    def test_load_missing_file_returns_empty(self) -> None:
        model = PlaylistModel(EntryStore(self.dir / "nope.json"))
        self.assertEqual(model.load(), 0)

    def test_corrupt_file_is_quarantined_not_silently_dropped(self) -> None:
        """坏文件必须改名留档——静默清空等于毁掉用户数据还瞒着不说。"""
        path = self.dir / "playlist.json"
        path.write_text("{ this is not json", encoding="utf-8")

        model = PlaylistModel(EntryStore(path))
        self.assertEqual(model.load(), 0)
        backups = list(self.dir.glob("playlist.json.bad-*"))
        self.assertEqual(len(backups), 1, "坏文件应被改名保留")
        self.assertFalse(path.exists())

    def test_bare_array_format_accepted(self) -> None:
        """兼容裸数组形态（用户手工编辑过，或早期版本写出来的）。"""
        path = self.dir / "playlist.json"
        path.write_text(
            json.dumps([{"url": "https://e.com/a.mp4", "title": "A"}]),
            encoding="utf-8",
        )
        model = PlaylistModel(EntryStore(path))
        self.assertEqual(model.load(), 1)

    def test_skips_broken_entries_but_keeps_good_ones(self) -> None:
        path = self.dir / "playlist.json"
        path.write_text(
            json.dumps({
                "version": 1,
                "items": [
                    {"url": "https://e.com/a.mp4"},
                    {"no_url": True},
                    {"url": ""},
                    {"url": "https://e.com/b.mp4"},
                ],
            }),
            encoding="utf-8",
        )
        model = PlaylistModel(EntryStore(path))
        self.assertEqual(model.load(), 2)

    def test_save_is_atomic_no_tmp_left(self) -> None:
        path = self.dir / "playlist.json"
        store = EntryStore(path)
        model = PlaylistModel(store)
        model.add(_items(2))
        model.save()
        self.assertEqual(list(self.dir.glob("*.tmp")), [], "临时文件应在替换后消失")

    def test_resolved_url_not_persisted(self) -> None:
        """带签名的临时直链不该落盘（下次启动必然失效）。"""
        path = self.dir / "playlist.json"
        model = PlaylistModel(EntryStore(path))
        item = MediaItem.from_url("https://www.bilibili.com/video/BV1xx")
        item.resolved_url = "https://cdn.example.com/signed?token=abc"
        model.add(item)
        model.save()
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn("token=abc", raw)
        self.assertIn("BV1xx", raw)


class HelperTest(unittest.TestCase):
    def test_default_referer(self) -> None:
        self.assertEqual(
            default_referer_for("https://a.com/x/y.mp4"), "https://a.com/"
        )
        self.assertEqual(default_referer_for("rtsp://a.com/x"), "")
        self.assertEqual(default_referer_for(""), "")


if __name__ == "__main__":
    unittest.main()
