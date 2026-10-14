"""通知网关（4.7 / NT-01 ~ NT-05）。

- **事件驱动**：业务侧只调用 ``notify(event)``，不关心具体渠道；
- **不阻塞主流程**：发送在独立线程池执行，抢票主循环绝不等通知；
- **故障隔离**：单个渠道失败只记录，不影响其余渠道（NT-T5）；
- **可追溯**：每次发送都落 notifications 表，供 Web 端「通知记录」页查看。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from ticket_helper import APP_NAME
from ticket_helper.notify.channels import Channel, WebChannel, build_channels

log = logging.getLogger(__name__)


class EventType(str, Enum):
    """通知事件类型（NT-05 事件 → 模板 → 渠道 映射的基础）。"""

    SUCCESS = "success"     # 出票成功
    STANDBY = "standby"     # 候补相关（提交/进度/成功）
    ERROR = "error"         # 任务失败 / 异常
    RISK = "risk"           # 风控 / 掉线
    INFO = "info"           # 常规信息

    def __str__(self) -> str:  # pragma: no cover
        return self.value


#: 默认模板：事件类型 → (标题模板, 正文模板)
DEFAULT_TEMPLATES: dict[str, tuple[str, str]] = {
    EventType.SUCCESS.value: ("🎫 抢票成功！{task}", "车次：{train}\n日期：{date}\n{route}\n席别：{seat}\n乘客：{passengers}\n订单号：{order_no}\n\n请尽快登录 12306 完成支付（请在 30 分钟内支付）。"),
    EventType.STANDBY.value: ("📝 候补动态：{task}", "{detail}\n\n车次：{train}\n日期：{date}\n{route}\n席别：{seat}"),
    EventType.ERROR.value: ("⚠️ 任务异常：{task}", "{detail}"),
    EventType.RISK.value: ("🚨 风控告警：{task}", "{detail}"),
    EventType.INFO.value: ("ℹ️ " + APP_NAME + "：{task}", "{detail}"),
}

#: 哪些事件属于「重大事件」（会触发语音拨号等 critical_only 渠道）
CRITICAL_EVENTS = {EventType.SUCCESS.value}

#: 模板里引用的「行程信息」占位符。
#: 用途：候补通知的模板固定渲染「车次 / 日期 / 路线 / 席别」四行，
#: 但**前置失败**（例如账号未完成人证核验、配额已满）发生在挑车次之前，
#: 此时根本没有行程信息，四行就都渲染成 `-`，占掉半屏却什么都没说。
#: 全都缺时只显示正文。
_TRIP_KEYS = ("train_no", "depart_date", "from_station", "to_station", "seat_type")


@dataclass
class NotifyEvent:
    """通知事件数据对象。"""

    type: str
    title: str = ""
    body: str = ""
    task: str = ""
    detail: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def critical(self) -> bool:
        return self.type in CRITICAL_EVENTS

    def render(self) -> tuple[str, str]:
        """按模板渲染标题与正文（NT-05）。"""
        if self.title and self.body:
            return self.title, self.body
        title_tpl, body_tpl = DEFAULT_TEMPLATES.get(
            self.type, (APP_NAME, "{detail}")
        )
        if self.type == EventType.STANDBY.value and not any(
            self.context.get(key) for key in _TRIP_KEYS
        ):
            # 没有行程信息（前置失败）→ 只显示正文，不要四行「-」
            body_tpl = "{detail}"
        values = {
            "task": self.task or "未命名任务",
            "detail": self.detail,
            "train": self.context.get("train_no", "-"),
            "date": self.context.get("depart_date", "-"),
            "route": f"{self.context.get('from_station','-')} → {self.context.get('to_station','-')}",
            "seat": self.context.get("seat_type", "-"),
            "passengers": self.context.get("passenger", "-"),
            "order_no": self.context.get("order_no", "-"),
        }
        return _safe_format(title_tpl, values), _safe_format(body_tpl, values)

    def to_dict(self) -> dict[str, Any]:
        title, body = self.render()
        return {
            "type": self.type,
            "title": title,
            "body": body,
            "critical": self.critical,
            "created_at": self.created_at,
            "context": self.context,
        }


def _safe_format(template: str, values: dict[str, Any]) -> str:
    """模板占位符缺失时保持可读，不抛 KeyError。"""
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        result = template
        for key, value in values.items():
            result = result.replace("{" + key + "}", str(value))
        return result


class NotifyGateway:
    """通知网关。"""

    def __init__(self, notify_config: Any = None, *, db: Any = None, max_workers: int = 4) -> None:
        self.config = notify_config
        self.db = db
        self.channels: list[Channel] = build_channels(notify_config) if notify_config else [WebChannel()]
        self.web_channel: WebChannel | None = next(
            (c for c in self.channels if isinstance(c, WebChannel)), None
        )
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="notify")
        self._history: deque[dict[str, Any]] = deque(maxlen=300)
        self._lock = threading.RLock()
        self.enabled = True
        self.stats: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------ #
    def apply_config(self, notify_config: Any) -> None:
        """配置热更新：重建渠道（CF-04）。"""
        with self._lock:
            self.config = notify_config
            old_web = self.web_channel
            self.channels = build_channels(notify_config)
            self.web_channel = next((c for c in self.channels if isinstance(c, WebChannel)), None)
            if old_web and self.web_channel and old_web is not self.web_channel:
                for callback in getattr(old_web, "_subscribers", []):  # noqa: SLF001
                    self.web_channel.subscribe(callback)
        log.info("通知渠道已热更新: %s", [c.name for c in self.active_channels()])

    def active_channels(self) -> list[Channel]:
        return [c for c in self.channels if c.enabled]

    def channel(self, name: str) -> Channel | None:
        return next((c for c in self.channels if c.name == name), None)

    def subscribe_web(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """订阅 Web/桌面端实时提醒（出票弹窗、声音）。"""
        if self.web_channel is None:
            self.web_channel = WebChannel(enabled=True)
            self.channels.append(self.web_channel)
        return self.web_channel.subscribe(callback)

    # ------------------------------------------------------------------ #
    def notify(self, event: NotifyEvent | str, **kwargs: Any) -> None:
        """异步发送通知（不阻塞调用方）。"""
        if isinstance(event, str):
            event = NotifyEvent(type=event, **kwargs)
        if not self.enabled:
            return
        self._executor.submit(self._dispatch, event)

    def notify_sync(self, event: NotifyEvent | str, **kwargs: Any) -> dict[str, bool]:
        """同步发送（测试与「测试渠道」按钮使用），返回各渠道结果。"""
        if isinstance(event, str):
            event = NotifyEvent(type=event, **kwargs)
        return self._dispatch(event)

    def _dispatch(self, event: NotifyEvent) -> dict[str, bool]:
        title, body = event.render()
        results: dict[str, bool] = {}
        targets = self.active_channels()

        with self._lock:
            self._history.appendleft(
                {"type": event.type, "title": title, "body": body, "ts": event.created_at, "task": event.task}
            )

        for channel in targets:
            ok = channel.send(title, body, critical=event.critical)
            results[channel.name] = ok
            with self._lock:
                bucket = self.stats.setdefault(channel.name, {"ok": 0, "fail": 0})
                bucket["ok" if ok else "fail"] += 1
            self._record(event, channel, ok)
            if not ok and channel.last_error:
                log.warning("渠道 %s 发送失败: %s", channel.name, channel.last_error)

        if not targets:
            log.warning("没有可用的通知渠道，事件未送达: %s", title)
        else:
            log.info("通知已分发: [%s] %s -> %s", event.type, title, results)
        return results

    def _record(self, event: NotifyEvent, channel: Channel, ok: bool) -> None:
        if self.db is None:
            return
        try:
            title, body = event.render()
            self.db.save_notification(event.type, title, body, channel.name, ok, channel.last_error)
        except Exception as exc:  # noqa: BLE001
            log.debug("通知记录落库失败: %s", exc)

    # ------------------------------------------------------------------ #
    # 语义化快捷方法
    # ------------------------------------------------------------------ #
    def notify_success(self, task_name: str, context: dict[str, Any]) -> None:
        self.notify(NotifyEvent(type=EventType.SUCCESS.value, task=task_name, context=context))

    def notify_standby(self, task_name: str, detail: str, context: dict[str, Any] | None = None) -> None:
        self.notify(NotifyEvent(type=EventType.STANDBY.value, task=task_name, detail=detail, context=context or {}))

    def notify_error(self, task_name: str, detail: str) -> None:
        self.notify(NotifyEvent(type=EventType.ERROR.value, task=task_name, detail=detail))

    def notify_risk(self, task_name: str, detail: str) -> None:
        self.notify(NotifyEvent(type=EventType.RISK.value, task=task_name, detail=detail))

    def notify_info(self, task_name: str, detail: str, context: dict[str, Any] | None = None) -> None:
        self.notify(NotifyEvent(type=EventType.INFO.value, task=task_name, detail=detail, context=context or {}))

    # ------------------------------------------------------------------ #
    def test_all(self) -> dict[str, bool]:
        """测试所有已启用渠道（NT-T1/T2/T3）。"""
        return {c.name: c.test() for c in self.active_channels()}

    def test_channel(self, name: str, override: dict[str, Any] | None = None) -> bool:
        """测试单个渠道；``override`` 可用前端临时填写的配置覆盖（便于「先测后存」）。"""
        channel = self.channel(name)
        if override and self.config is not None:
            from ticket_helper.config.settings import AppConfig

            payload = self.config.to_dict(mask_secrets=False)
            payload.setdefault(name, {}).update(override)
            temp = AppConfig.from_dict({"notify": payload}).notify
            temp_channels = {c.name: c for c in build_channels(temp)}
            channel = temp_channels.get(name, channel)
        if channel is None:
            return False
        return channel.test()

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history)[:limit]

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "channels": [c.to_dict() for c in self.channels],
            "stats": dict(self.stats),
        }

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
