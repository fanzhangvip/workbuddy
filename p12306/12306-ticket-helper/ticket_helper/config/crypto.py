"""敏感信息加密（CF-02 / AC-01）。

- 优先使用 ``cryptography`` 的 Fernet（AES-128-CBC + HMAC-SHA256，带认证）；
- 若运行环境缺少 ``cryptography``，退化为基于 HMAC-SHA256 计数器流的
  纯标准库实现（仅作混淆级保护），并通过 ``.degraded`` 明确标识，
  避免出现「以为加密了其实没有」的隐性风险；
- 密钥存放于 ``<home>/runtime/secret.key``（权限 0600），与数据库分离，
  确保仅拖走 app.db 无法还原密码。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger(__name__)

FERNET_PREFIX = "F1:"
FALLBACK_PREFIX = "P1:"

try:  # pragma: no cover - 取决于环境
    from cryptography.fernet import Fernet, InvalidToken

    _HAS_FERNET = True
except Exception:  # pragma: no cover
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment,misc]
    _HAS_FERNET = False


class CryptoError(Exception):
    """加解密失败。"""


class SecretCipher:
    """统一的密钥管理 + 加解密入口。"""

    def __init__(self, key_path: Path, *, key: bytes | None = None) -> None:
        self.key_path = Path(key_path)
        self._key = key or self._load_or_create_key()
        self._fernet = Fernet(self._key) if (_HAS_FERNET and key is None) else None
        if _HAS_FERNET and self._fernet is None:
            self._fernet = Fernet(self._key)

    # ------------------------------------------------------------------ #
    # 密钥
    # ------------------------------------------------------------------ #
    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            raw = self.key_path.read_bytes().strip()
            if raw:
                return raw
        key = Fernet.generate_key() if _HAS_FERNET else _fallback_key()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.write_bytes(key)
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:  # pragma: no cover - Windows 下部分文件系统不支持
            pass
        return key

    @property
    def degraded(self) -> bool:
        """True 表示降级为纯标准库实现（保护强度较低）。"""
        return not _HAS_FERNET

    # ------------------------------------------------------------------ #
    # 加解密
    # ------------------------------------------------------------------ #
    def encrypt(self, plaintext: str | None) -> str:
        if plaintext is None or plaintext == "":
            return ""
        raw = plaintext.encode("utf-8")
        if _HAS_FERNET:
            assert self._fernet is not None
            return FERNET_PREFIX + self._fernet.encrypt(raw).decode("ascii")
        return FALLBACK_PREFIX + _fallback_encrypt(raw, self._key)

    def decrypt(self, ciphertext: str | None) -> str:
        if not ciphertext:
            return ""
        if ciphertext.startswith(FERNET_PREFIX):
            if not _HAS_FERNET:
                raise CryptoError("密文由 cryptography 生成，但当前环境缺少该依赖")
            assert self._fernet is not None
            try:
                return self._fernet.decrypt(ciphertext[len(FERNET_PREFIX):].encode("ascii")).decode("utf-8")
            except InvalidToken as exc:
                raise CryptoError("解密失败：密钥不匹配或数据损坏") from exc
        if ciphertext.startswith(FALLBACK_PREFIX):
            return _fallback_decrypt(ciphertext[len(FALLBACK_PREFIX):], self._key).decode("utf-8")
        # 兼容历史明文数据：原样返回，交由上层迁移
        return ciphertext

    # ------------------------------------------------------------------ #
    def encrypt_dict(self, data: dict, fields: tuple[str, ...]) -> dict:
        out = dict(data)
        for field in fields:
            if field in out and isinstance(out[field], str) and out[field] and not self.is_encrypted(out[field]):
                out[field] = self.encrypt(out[field])
        return out

    @staticmethod
    def is_encrypted(value: str | None) -> bool:
        return bool(value) and (value.startswith(FERNET_PREFIX) or value.startswith(FALLBACK_PREFIX))

    def mask(self, value: str | None) -> str:
        """用于日志输出，绝不打印明文。"""
        if not value:
            return ""
        if len(value) <= 2:
            return "*" * len(value)
        return value[0] + "*" * (len(value) - 2) + value[-1]


# --------------------------------------------------------------------------- #
# 纯标准库降级实现（HMAC-SHA256 计数器流 + XOR）
# --------------------------------------------------------------------------- #
def _fallback_key() -> bytes:
    return base64.urlsafe_b64encode(secrets.token_bytes(32))


def _keystream(key: bytes, length: int, nonce: bytes) -> bytes:
    out = bytearray()
    counter = 0
    base = base64.urlsafe_b64decode(key + b"=" * (-len(key) % 4))
    while len(out) < length:
        out.extend(hmac.new(base, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def _fallback_encrypt(raw: bytes, key: bytes) -> str:
    nonce = secrets.token_bytes(16)
    stream = _keystream(key, len(raw), nonce)
    body = bytes(a ^ b for a, b in zip(raw, stream))
    tag = hmac.new(base64.urlsafe_b64decode(key + b"=" * (-len(key) % 4)), nonce + body, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(nonce + tag + body).decode("ascii")


def _fallback_decrypt(payload: str, key: bytes) -> bytes:
    blob = base64.urlsafe_b64decode(payload.encode("ascii"))
    nonce, tag, body = blob[:16], blob[16:32], blob[32:]
    expect = hmac.new(base64.urlsafe_b64decode(key + b"=" * (-len(key) % 4)), nonce + body, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expect):
        raise CryptoError("解密失败：校验和不匹配")
    stream = _keystream(key, len(body), nonce)
    return bytes(a ^ b for a, b in zip(body, stream))
