"""播放条目数据模型。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from src.core.url_router import UrlKind, classify
from src.utils.urls import to_playable_url


@dataclass
class MediaItem:
    """一条可播放的媒体。url 是原始输入，用于展示与持久化。"""

    url: str
    title: str = ""
    kind: UrlKind = UrlKind.UNKNOWN
    duration_ms: int = 0
    is_local: bool = False
    #: 解析后的真实流地址（P2 yt-dlp 使用）。页面地址播不了，它才播得了。
    resolved_url: str = ""
    #: 解析出来的流属于哪一类。加载超时、直播判定都要看**它**而不是 kind——
    #: kind 描述的是用户输入（网页地址），resolved_kind 描述的才是播放对象。
    resolved_kind: UrlKind | None = None
    #: 解析结果的一句话说明，如「哔哩哔哩 · 1080P · mp4 · 单文件」
    resolver_note: str = ""
    #: 站点把画面和声音放在两条流里时（DASH 分离流），这里是"声音"那一路上。
    #: 非空时播放器会把它挂成附加音轨，与画面同时播放，实现音画同步。
    audio_url: str = ""
    http_headers: dict = field(default_factory=dict)
    #: 加入播放列表/历史记录的时间戳（Unix 秒）。用于历史按时间倒序展示。
    added_at: float = 0.0

    @classmethod
    def from_url(cls, url: str, title: str = "") -> "MediaItem":
        url = (url or "").strip()
        kind = classify(url)
        return cls(
            url=url,
            title=title or _guess_title(url),
            kind=kind,
            is_local=(kind is UrlKind.LOCAL),
        )

    @property
    def play_url(self) -> str:
        """实际交给播放器的地址（解析后优先，并把本地路径规范成 file:// URL）。"""
        return to_playable_url(self.resolved_url or self.url)

    @property
    def audio_play_url(self) -> str:
        """附加音轨交给播放器的地址（同样经 to_playable_url 规范化）。"""
        return to_playable_url(self.audio_url) if self.audio_url else ""

    @property
    def has_separate_audio(self) -> bool:
        """是否挂了独立的声音流（即"本地合流播放"模式）。"""
        return bool(self.audio_url)

    @property
    def effective_kind(self) -> UrlKind:
        """实际播放对象的类别。解析成功时以解析结果为准。"""
        return self.resolved_kind or self.kind

    @property
    def external_url(self) -> str:
        """交给外部播放器时用哪个地址。

        页面地址交给 yt-dlp 型播放器（mpv）才有意义；解析出来的直链是带签名
        的临时地址，且部分站点要求 Referer，交给 VLC 反而更差。所以只要解析过，
        外部打开就还回原页面地址。
        """
        if self.resolved_url:
            return self.url
        return self.play_url

    def to_dict(self) -> dict:
        # 故意不持久化 resolved_url / resolved_kind / audio_url：解析出来的直链
        # 带签名、有有效期，存下来下次启动必然失效。
        return {
            "url": self.url,
            "title": self.title,
            "kind": self.kind.value,
            "duration_ms": self.duration_ms,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MediaItem":
        url = data.get("url", "")
        item = cls.from_url(url, data.get("title", ""))
        item.duration_ms = int(data.get("duration_ms", 0) or 0)
        try:
            item.added_at = float(data.get("added_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            item.added_at = 0.0
        return item


def _guess_title(url: str) -> str:
    """从 URL 猜一个可读标题，猜不出来就原样返回。"""
    if not url:
        return ""
    if os.path.exists(url):
        return os.path.basename(url)

    parsed = urlparse(url)
    if parsed.scheme == "file":
        return os.path.basename(unquote(parsed.path))

    name = os.path.basename(parsed.path or "")
    if name:
        return unquote(name)
    return parsed.netloc or url
