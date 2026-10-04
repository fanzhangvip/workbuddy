"""账号会话（Cookie）持久化：重启后不必重新扫码。

为什么需要它
------------
12306 的登录态只在 Cookie 里，不落在本地 —— 这是**协议事实**，没法绕过。
但「不落在本地」指的是官方那边没有服务端会话可恢复，
**我们这边的 Cookie 只要还没过期，就可以继续用**。

以前没做这件事，后果是：扫码登录成功后一关程序，下次打开账号就是「离线」，
想看一眼个人中心又得重新扫码。使用者感受到的是「这功能好像根本不能用」。
（对应需求 AC-03「会话保持与状态恢复」——会话保持原本只做了内存里的心跳。）

安全口径
--------
存的是**加密后**的 Cookie（与账号密码共用同一把本机密钥 ``secret.key``），
落盘位置在用户目录 ``data/sessions/`` 下，不进代码库、不进日志。
文件名用账号名的哈希，避免中文/特殊字符进文件名。

失效策略
--------
1. **不信任磁盘上的会话**：恢复后必须真调一次 ``checkUser`` 验证，
   验证不过立刻丢弃并回到正常登录流程；
2. 超过 ``MAX_AGE`` 的直接不看（Cookie 早就过期了，白跑一次请求没意义）；
3. 登出 / 恢复失败时清除文件，不给「过期的会话一直留在磁盘上」的机会。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable

from ticket_helper.config.crypto import SecretCipher

log = logging.getLogger(__name__)

#: 会话最长保留时间（秒）。默认 3 天 —— 12306 的会话通常远早于此就失效了，
#: 留太久只会让每次启动都白跑一次验证请求。
MAX_AGE = 3 * 24 * 3600

#: 只保留与登录态相关的 Cookie，其余（埋点、统计之类）没有存的价值。
_KEEP_HINT = re.compile(r"^(JSESSIONID|tk|RAIL_|route|BIGipServer|_uab_collina|uamtk|guides|kyfw)", re.I)


def _session_key(username: str) -> str:
    """账号名 → 文件名安全键。"""
    digest = hashlib.sha256(username.strip().encode("utf-8")).hexdigest()[:16]
    return f"session-{digest}.enc"


class SessionStore:
    """按账号保存 / 读取加密的会话 Cookie。"""

    def __init__(
        self,
        directory: Path | str,
        cipher: SecretCipher,
        *,
        max_age: float = MAX_AGE,
        base_url: str = "",
    ) -> None:
        self.directory = Path(directory)
        self.cipher = cipher
        self.max_age = max_age
        #: 会话与站点绑定：换了测试用的 mock 之后，旧会话不该被复用
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------ #
    def _path(self, username: str) -> Path:
        return self.directory / _session_key(username)

    def save(self, username: str, cookies: Iterable[dict[str, Any]]) -> bool:
        """保存会话。``cookies`` 是 :meth:`HttpClient.export_cookies` 的输出。

        **全部存下来，不做筛选。** 这里一开始按「名字看起来相关才存」过滤了一遍
        （只留 JSESSIONID/tk/RAIL_* 等），真机验证时发现：
        恢复出来的会话 ``checkUser`` 返回 ``flag: false``，即**恢复不成功**。
        原因无法确定是「少了某个 Cookie」还是「会话本就过期」，但少存是**确定的
        损失**、多存是零成本（同样是加密落盘）。
        拿不确定的收益去换确定的故障面，不划算 —— 所以改为全量保存。
        """
        if not username:
            return False
        kept = [c for c in cookies if isinstance(c, dict) and c.get("name")]
        if not kept:
            log.warning("账号 %s 登录成功但会话里没有任何 Cookie，无法留存（重启后需重新扫码）", username)
            return False
        payload = {
            "username": username,
            "base_url": self.base_url,
            "saved_at": time.time(),
            "cookies": kept,
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            blob = self.cipher.encrypt(json.dumps(payload, ensure_ascii=False))
            self._path(username).write_text(blob, encoding="utf-8")
            log.info("已保存账号 %s 的登录会话（重启后无需重新扫码）", username)
            return True
        except Exception as exc:  # noqa: BLE001 - 存会话失败绝不能影响登录本身
            log.warning("保存账号 %s 的会话失败: %s", username, exc)
            return False

    def load(self, username: str) -> list[dict[str, Any]] | None:
        """读取会话；不存在、过期、解不开都返回 ``None``。"""
        if not username:
            return None
        path = self._path(username)
        if not path.exists():
            return None
        try:
            raw = self.cipher.decrypt(path.read_text(encoding="utf-8"))
            payload = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            # 密钥换了 / 文件损坏：删掉重新登录即可，不要让启动一直报同一句错
            log.warning("账号 %s 的会话无法读取（%s），将按未登录处理", username, exc)
            self.clear(username)
            return None

        saved_at = float(payload.get("saved_at") or 0)
        if self.max_age and saved_at and (time.time() - saved_at) > self.max_age:
            log.info("账号 %s 的会话已超过 %d 天，忽略", username, int(self.max_age // 86400))
            self.clear(username)
            return None
        # 站点变了（例如切到 mock）就不能复用旧 Cookie
        saved_base = str(payload.get("base_url") or "").rstrip("/")
        if saved_base and self.base_url and saved_base != self.base_url:
            log.info("账号 %s 的会话属于另一个服务端（%s），忽略", username, saved_base)
            return None
        cookies = payload.get("cookies") or []
        return [c for c in cookies if isinstance(c, dict)] or None

    def exists(self, username: str) -> bool:
        """本地是否留有该账号的会话文件。

        只查文件存在性、**不解密**：这个值要出现在账号列表接口里，
        每次列账号都做一次解密纯属浪费（列账号是高频操作）。
        """
        return bool(username) and self._path(username).exists()

    def clear(self, username: str) -> None:
        if not username:
            return
        try:
            self._path(username).unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - 删不掉不影响功能
            log.debug("删除账号 %s 的会话文件失败: %s", username, exc)

    def clear_all(self) -> int:
        removed = 0
        if not self.directory.exists():
            return 0
        for path in self.directory.glob("session-*.enc"):
            try:
                path.unlink()
                removed += 1
            except OSError:  # pragma: no cover
                continue
        return removed


def filter_cookies(cookies: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """只挑出「看起来与登录态有关」的 Cookie。

    .. deprecated::
        **不要再用它来裁剪要保存的会话**（见 :meth:`SessionStore.save` 的说明：
        真机验证发现裁剪后恢复不成功）。保留它只是给「只想看关键 Cookie」的
        排查场景用，比如打印会话里有没有 ``tk`` / ``JSESSIONID``。
    """
    result: list[dict[str, Any]] = []
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        if name and _KEEP_HINT.match(name):
            result.append(cookie)
    return result
