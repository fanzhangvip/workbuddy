"""通知渠道实现（NT-01 ~ NT-05）。

统一约定：
- 每个渠道实现 ``send(title, body) -> bool``，**绝不抛异常**（失败返回 False
  并把原因返回给网关记录），这样单渠道故障不会影响其他渠道（NT-T5）；
- 所有出网请求都带超时，避免网络慢导致通知线程堆积；
- 邮件 / webhook / 语音的密钥来自配置，日志里只出现掩码。
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import threading
import time
from abc import ABC, abstractmethod
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Callable

log = logging.getLogger(__name__)


class Channel(ABC):
    """通知渠道抽象。"""

    name = "channel"
    #: 是否为「重大事件」渠道（语音拨号等只在此类事件触发）
    critical_only = False

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.last_error = ""
        self.sent = 0
        self.failed = 0

    @abstractmethod
    def _send(self, title: str, body: str, *, critical: bool = False) -> bool:
        ...

    def send(self, title: str, body: str, *, critical: bool = False) -> bool:
        if not self.enabled:
            # ⚠️ 失败必须留下原因。F4「能力可用性证明」实测发现：
            # 这两条 early return 原先只回 False、**不写 last_error**，
            # 于是调用方看到「发失败」却查不到为什么 —— 与当年云打码
            # 「请求根本没发出去却只说识别失败」是同一类问题。
            self.last_error = "渠道未启用（enabled=False）"
            return False
        if self.critical_only and not critical:
            self.last_error = "该渠道只接受重大事件（需 critical=True）"
            return False
        try:
            ok = bool(self._send(title, body, critical=critical))
        except Exception as exc:  # noqa: BLE001 - 渠道异常一律吞掉，只记录
            self.last_error = str(exc)
            self.failed += 1
            log.error("通知渠道 %s 发送异常: %s", self.name, exc)
            return False
        if ok:
            self.sent += 1
        else:
            self.failed += 1
            if not self.last_error:
                # 兜底：子类没写原因也不能让失败变成哑的
                self.last_error = "发送失败（渠道未给出原因）"
        return ok

    def test(self) -> bool:
        return self.send("【测试通知】抢票助手", f"渠道 {self.name} 连通性测试，时间 {time.strftime('%Y-%m-%d %H:%M:%S')}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "critical_only": self.critical_only,
            "sent": self.sent,
            "failed": self.failed,
            "last_error": self.last_error,
        }


# --------------------------------------------------------------------------- #
class EmailChannel(Channel):
    """邮件通知（NT-01）。"""

    name = "email"

    def __init__(self, config: Any) -> None:
        super().__init__(enabled=bool(getattr(config, "enabled", False)))
        self.host = getattr(config, "smtp_host", "")
        self.port = int(getattr(config, "smtp_port", 465) or 465)
        self.use_ssl = bool(getattr(config, "use_ssl", True))
        self.username = getattr(config, "username", "")
        self.password = getattr(config, "password", "")
        self.sender = getattr(config, "sender", "") or self.username
        self.receivers = list(getattr(config, "receivers", []) or [])

    def _send(self, title: str, body: str, *, critical: bool = False) -> bool:
        if not (self.host and self.receivers):
            self.last_error = "邮件配置不完整"
            return False

        message = MIMEText(body, "plain", "utf-8")
        message["Subject"] = Header(title, "utf-8")
        message["From"] = formataddr((str(Header("12306抢票助手", "utf-8")), self.sender))
        message["To"] = ",".join(self.receivers)

        try:
            if self.use_ssl:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(self.host, self.port, timeout=15, context=context) as server:
                    if self.username:
                        server.login(self.username, self.password)
                    server.sendmail(self.sender, self.receivers, message.as_string())
            else:
                with smtplib.SMTP(self.host, self.port, timeout=15) as server:
                    server.ehlo()
                    try:
                        server.starttls(context=ssl.create_default_context())
                        server.ehlo()
                    except smtplib.SMTPException:
                        pass
                    if self.username:
                        server.login(self.username, self.password)
                    server.sendmail(self.sender, self.receivers, message.as_string())
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.error("邮件发送失败: %s", exc)
            return False


class ServerChanChannel(Channel):
    """Server酱 / PushBear → 微信推送（NT-02）。"""

    name = "serverchan"

    def __init__(self, config: Any) -> None:
        super().__init__(enabled=bool(getattr(config, "enabled", False)))
        self.key = getattr(config, "key", "")
        self.webhook = getattr(config, "webhook", "")

    def _endpoint(self) -> str:
        if self.webhook:
            return self.webhook
        if self.key.startswith("sctp"):
            # Server酱³（Turbo）新版接口
            return f"https://{self.key.split('t')[0]}.push.ft07.com/send/{self.key}.send"
        return f"https://sctapi.ftqq.com/{self.key}.send"

    def _send(self, title: str, body: str, *, critical: bool = False) -> bool:
        if not (self.key or self.webhook):
            self.last_error = "未配置 ServerChan key"
            return False
        from ticket_helper.inter.base import HttpClient

        client = HttpClient(base_url="http://127.0.0.1", timeout=12, retries=1)
        try:
            response = client.post(
                self._endpoint(),
                data={"title": title, "desp": body},
                allow_retry=False,
                check_risk=False,
            )
            payload = response.json or {}
            code = payload.get("code", payload.get("errno", 0))
            if str(code) in ("0", "None"):
                return True
            self.last_error = str(payload.get("message") or payload.get("errmsg") or code)
            return False
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False
        finally:
            client.close()


class DingTalkChannel(Channel):
    """钉钉机器人（NT-02）。"""

    name = "dingtalk"

    def __init__(self, config: Any) -> None:
        super().__init__(enabled=bool(getattr(config, "enabled", False)))
        self.webhook = getattr(config, "webhook", "") or getattr(config, "key", "")

    def _send(self, title: str, body: str, *, critical: bool = False) -> bool:
        if not self.webhook:
            self.last_error = "未配置钉钉 webhook"
            return False
        from ticket_helper.inter.base import HttpClient

        client = HttpClient(base_url="http://127.0.0.1", timeout=12, retries=1)
        try:
            payload = {"msgtype": "markdown", "markdown": {"title": title, "text": f"### {title}\n\n{body}"}}
            response = client.request("POST", self.webhook, json_body=payload, allow_retry=False, check_risk=False)
            result = response.json or {}
            if result.get("errcode") == 0:
                return True
            self.last_error = str(result.get("errmsg") or response.status_code)
            return False
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False
        finally:
            client.close()


class WeComChannel(Channel):
    """企业微信机器人（NT-02）。"""

    name = "wecom"

    def __init__(self, config: Any) -> None:
        super().__init__(enabled=bool(getattr(config, "enabled", False)))
        self.webhook = getattr(config, "webhook", "") or getattr(config, "key", "")

    def _send(self, title: str, body: str, *, critical: bool = False) -> bool:
        if not self.webhook:
            self.last_error = "未配置企业微信 webhook"
            return False
        from ticket_helper.inter.base import HttpClient

        client = HttpClient(base_url="http://127.0.0.1", timeout=12, retries=1)
        try:
            payload = {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{body}"}}
            response = client.request("POST", self.webhook, json_body=payload, allow_retry=False, check_risk=False)
            result = response.json or {}
            if result.get("errcode") == 0:
                return True
            self.last_error = str(result.get("errmsg") or response.status_code)
            return False
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False
        finally:
            client.close()


class TelegramChannel(Channel):
    """Telegram Bot（NT-02）。"""

    name = "telegram"

    def __init__(self, config: Any) -> None:
        super().__init__(enabled=bool(getattr(config, "enabled", False)))
        self.bot_token = getattr(config, "bot_token", "")
        self.chat_id = getattr(config, "chat_id", "")
        self.endpoint = getattr(config, "endpoint", "")

    def _send(self, title: str, body: str, *, critical: bool = False) -> bool:
        if not (self.bot_token and self.chat_id):
            self.last_error = "Telegram 配置不完整"
            return False
        from ticket_helper.inter.base import HttpClient

        client = HttpClient(base_url="http://127.0.0.1", timeout=12, retries=1)
        try:
            url = self.endpoint or f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            response = client.request(
                "POST",
                url,
                json_body={"chat_id": self.chat_id, "text": f"{title}\n\n{body}"},
                allow_retry=False,
                check_risk=False,
            )
            result = response.json or {}
            if result.get("ok"):
                return True
            # F4 实测抓到：这里原先直接 return bool(ok)，**不写 last_error** ——
            # 失败原因（Telegram 会给 description）就此丢失。
            self.last_error = str(
                result.get("description") or result.get("error_code") or response.status_code
            )
            return False
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False
        finally:
            client.close()


class VoiceChannel(Channel):
    """电话语音通知（NT-03）。仅重大事件（出票成功）触发。"""

    name = "voice"
    critical_only = True

    def __init__(self, config: Any) -> None:
        super().__init__(enabled=bool(getattr(config, "enabled", False)))
        self.provider = getattr(config, "provider", "aliyun")
        self.appcode = getattr(config, "appcode", "")
        self.phone = getattr(config, "phone", "")
        self.tts_code = getattr(config, "tts_code", "")
        self.endpoint = getattr(config, "endpoint", "")

    def _send(self, title: str, body: str, *, critical: bool = False) -> bool:
        if not (self.appcode and self.phone):
            self.last_error = "语音通知配置不完整"
            return False
        from ticket_helper.inter.base import HttpClient

        client = HttpClient(base_url="http://127.0.0.1", timeout=15, retries=1)
        try:
            params = {
                "mobile": self.phone,
                "content": f"抢票助手提醒，{title}",
                "tts_code": self.tts_code,
            }
            response = client.request(
                "POST",
                self.endpoint or f"https://voice.{self.provider}.com/api/call",
                params=params,
                headers={"Authorization": f"APPCODE {self.appcode}"},
                allow_retry=False,
                check_risk=False,
            )
            payload = response.json or {}
            if payload.get("code") in (0, "0", 200, "200"):
                return True
            self.last_error = str(payload.get("msg") or response.status_code)
            return False
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False
        finally:
            client.close()


class WebChannel(Channel):
    """Web / 桌面端本地提醒（NT-04）：弹窗 + 声音。

    通过发布-订阅把事件推给所有已连接的前端（WebSocket / Qt 信号），
    因此不做网络请求，永远可用。
    """

    name = "web"

    def __init__(self, enabled: bool = True) -> None:
        super().__init__(enabled=enabled)
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.RLock()
        self._history: list[dict[str, Any]] = []

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return _unsubscribe

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._history.append(event)
            if len(self._history) > 200:
                del self._history[:-200]
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception as exc:  # noqa: BLE001
                log.error("Web 通知推送失败: %s", exc)

    def _send(self, title: str, body: str, *, critical: bool = False) -> bool:
        self.publish({"title": title, "body": body, "critical": critical, "ts": time.time()})
        return True

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history[-limit:])


# --------------------------------------------------------------------------- #
def build_channels(notify_config: Any) -> list[Channel]:
    """按配置构建渠道列表。"""
    channels: list[Channel] = [
        EmailChannel(notify_config.email),
        ServerChanChannel(notify_config.serverchan),
        DingTalkChannel(notify_config.dingtalk),
        WeComChannel(notify_config.wecom),
        TelegramChannel(notify_config.telegram),
        VoiceChannel(notify_config.voice),
        WebChannel(enabled=bool(getattr(notify_config, "web", True))),
    ]
    return channels


def parse_notify_config_from_dict(data: dict[str, Any]) -> Any:
    """从 dict 构造 NotifyConfig（Web 端测试渠道时用）。"""
    from ticket_helper.config.settings import AppConfig, NotifyConfig

    if not data:
        return NotifyConfig()
    return AppConfig.from_dict({"notify": data}).notify


def json_safe(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)
