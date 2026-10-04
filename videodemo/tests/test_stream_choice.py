"""格式挑选：同一个分辨率下优先"能完整走代理"的非清单地址。

清单（HLS 的 .m3u8 / DASH 的 .mpd）不是媒体文件，播放器拿到清单后还要照着
里面的分片地址自己直连——本地反向代理改写不到那些分片。所以在"必须走代理"
的网络里，清单型地址会"清单拉得到、分片拉不到"，最后还是播不出来。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core import resolver as R  # noqa: E402


def fmt(url: str, height: int = 1080, vcodec: str = "avc1.640028",
        acodec: str = "none", **extra) -> dict:
    data = {
        "url": url,
        "protocol": "https",
        "ext": "mp4",
        "height": height,
        "width": int(height * 16 / 9),
        "vcodec": vcodec,
        "acodec": acodec,
    }
    data.update(extra)
    return data


class ManifestDetectionTest(unittest.TestCase):
    def test_path_suffix(self) -> None:
        self.assertTrue(R.is_manifest_format(fmt("https://m.example.com/x/index.m3u8")))
        self.assertTrue(R.is_manifest_format(fmt("https://m.example.com/x/index.mpd")))

    def test_ext_wins_even_without_suffix(self) -> None:
        self.assertTrue(R.is_manifest_format(fmt("https://m.example.com/x", ext="m3u8")))

    def test_protocol_wins_even_without_suffix(self) -> None:
        self.assertTrue(R.is_manifest_format(
            fmt("https://m.example.com/x", protocol="m3u8_native")
        ))

    def test_plain_stream_is_not_manifest(self) -> None:
        self.assertFalse(R.is_manifest_format(
            fmt("https://rr1---sn-x.googlevideo.com/videoplayback?itag=137", ext="mp4")
        ))


class PreferenceTest(unittest.TestCase):
    MANIFEST = "https://manifest.googlevideo.com/api/manifest/hls_playlist/itag/137/index.m3u8"
    PLAIN = "https://rr1---sn-x.googlevideo.com/videoplayback?itag=137"

    def test_single_file_wins_at_same_quality(self) -> None:
        chosen = R.select_video_only([fmt(self.MANIFEST), fmt(self.PLAIN)])
        assert chosen is not None
        self.assertIn("videoplayback", chosen["url"])

    def test_resolution_still_wins_over_manifest_avoidance(self) -> None:
        """不能为了避开清单而降清晰度。"""
        chosen = R.select_video_only([fmt(self.MANIFEST, 1080), fmt(self.PLAIN, 720)])
        assert chosen is not None
        self.assertIn("index.m3u8", chosen["url"])

    def test_codec_compatibility_still_wins(self) -> None:
        """编码兼容性排在"避开清单"之前：宁可清单，也不要一个解不开的编码。

        同是 1080P 时若为了避开清单去选 AV1，缺 AV1 解码器的机器会变成
        "能解析、没画面"。
        """
        h264_manifest = fmt(self.MANIFEST, vcodec="avc1.640028")
        av1_plain = fmt(self.PLAIN, vcodec="av01.0.08M.08")
        chosen = R.select_video_only([h264_manifest, av1_plain])
        assert chosen is not None
        self.assertIn("index.m3u8", chosen["url"])

    def test_manifest_used_when_it_is_the_only_option(self) -> None:
        chosen = R.select_video_only([fmt(self.MANIFEST, 720)])
        assert chosen is not None
        self.assertIn("index.m3u8", chosen["url"])


if __name__ == "__main__":
    unittest.main()
