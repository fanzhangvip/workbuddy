"""配置加载 / 校验 / 热更新（CF-01~CF-05）。

加载优先级（后者覆盖前者）：
1. 代码内置默认值（:class:`AppConfig`）
2. ``<home>/env.py``（用户配置，从 env.example.py 复制而来）
3. 环境变量（``TH_`` 前缀，便于 Docker / CI 覆盖，CF-05）
4. SQLite 中持久化的运行时配置（Web 端修改后写入，CF-04）

热更新通过订阅回调实现：Web 端 PUT 配置 → 落库 → ``reload()`` →
通知所有订阅者（调度器、限速器、限额器等）在不重启的情况下生效。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ticket_helper.config.crypto import SecretCipher
from ticket_helper.config.settings import AppConfig
from ticket_helper.errors import ConfigError

log = logging.getLogger(__name__)

ENV_PREFIX = "TH_"

#: 需要加密的字段路径（section, field）
SECRET_FIELDS: tuple[tuple[str, str], ...] = (
    ("web", "auth_pass"),
    ("web", "secret"),
)
NOTIFY_SECRET_FIELDS = ("password", "key", "webhook", "appcode", "bot_token", "chat_id", "phone")


class ConfigLoader:
    """负责配置的读取、校验、加密与热更新分发。"""

    def __init__(self, env_file: Path, cipher: SecretCipher, *, db_provider: Callable[[], dict] | None = None) -> None:
        self.env_file = Path(env_file)
        self.cipher = cipher
        self._db_provider = db_provider
        self._config: AppConfig = AppConfig()
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[AppConfig], None]] = []
        self._version = 0

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def load(self, *, persist: bool = False) -> AppConfig:
        """完整加载一次配置。"""
        with self._lock:
            config = AppConfig()
            self._apply_env_py(config)
            self._apply_env_vars(config)
            errors = config.validate()
            if errors:
                raise ConfigError("配置校验未通过: " + "; ".join(errors))

            self._config = config
            self._version += 1
            if persist:
                self._persist(config)
            log.info(
                "配置加载完成: users=%d tasks=%d version=%d",
                len(config.users),
                len(config.tasks),
                self._version,
            )
            return config

    def reload(self, *, persist: bool = False) -> AppConfig:
        """热更新：重新加载并通知订阅者。"""
        previous = self.current
        config = self.load(persist=persist)
        # 只有真正变化才通知，避免无意义的下游抖动
        if config.to_dict() != previous.to_dict():
            self._notify(config)
        return config

    # ------------------------------------------------------------------ #
    def _apply_env_py(self, config: AppConfig) -> None:
        if not self.env_file.exists():
            log.warning("未找到配置文件 %s，使用内置默认配置", self.env_file)
            return
        module = self._import_env(self.env_file)
        raw = getattr(module, "ENV", None) or {}
        if not isinstance(raw, dict):
            raise ConfigError("env.py 必须定义 ENV 字典")
        merged = config.from_dict(raw)
        for name in AppConfig.__dataclass_fields__:
            setattr(config, name, getattr(merged, name))

    @staticmethod
    def _import_env(path: Path) -> Any:
        spec = importlib.util.spec_from_file_location("th_env", path)
        if spec is None or spec.loader is None:
            raise ConfigError(f"无法加载配置文件: {path}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"env.py 执行失败: {exc}") from exc
        return module

    def _apply_env_vars(self, config: AppConfig) -> None:
        """环境变量覆盖：TH_WEB_PORT=8080 / TH_LOG_LEVEL=DEBUG / TH_API_BASE=..."""
        mapping: dict[str, tuple[str, str]] = {
            "TH_LOG_LEVEL": ("", "log_level"),
            "TH_API_BASE": ("", "api_base"),
            "TH_WEB_HOST": ("web", "host"),
            "TH_WEB_PORT": ("web", "port"),
            "TH_WEB_AUTH_USER": ("web", "auth_user"),
            "TH_WEB_AUTH_PASS": ("web", "auth_pass"),
            "TH_WEB_SECRET": ("web", "secret"),
        }
        for var, (section, field) in mapping.items():
            value = os.environ.get(var)
            if value is None:
                continue
            target = config if not section else getattr(config, section)
            current = getattr(target, field)
            try:
                if isinstance(current, bool):
                    value = value.strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(current, int):
                    value = int(value)
                elif isinstance(current, float):
                    value = float(value)
            except ValueError:
                log.warning("环境变量 %s 值非法，已忽略: %s", var, value)
                continue
            setattr(target, field, value)

    # ------------------------------------------------------------------ #
    def _persist(self, config: AppConfig) -> None:
        """将配置落库（加密敏感字段）。db_provider 为空时跳过。"""
        if self._db_provider is None:
            return
        try:
            payload = self.serialize_for_storage(config)
            self._db_provider(payload)
        except Exception as exc:  # noqa: BLE001 - 持久化失败不应阻断启动
            log.error("配置持久化失败: %s", exc)

    def serialize_for_storage(self, config: AppConfig) -> dict[str, Any]:
        """序列化并加密敏感字段，用于写库。"""
        data = config.to_dict(mask_secrets=False)
        for user in data.get("users", []):
            user["password"] = self.cipher.encrypt(user.get("password", ""))
        notify = data.get("notify", {})
        for section in ("email", "serverchan", "dingtalk", "wecom", "telegram", "voice"):
            block = notify.get(section)
            if not isinstance(block, dict):
                continue
            for field in NOTIFY_SECRET_FIELDS:
                if block.get(field):
                    block[field] = self.cipher.encrypt(block[field])
        for section, field in SECRET_FIELDS:
            if data.get(section, {}).get(field):
                data[section][field] = self.cipher.encrypt(data[section][field])
        return data

    @staticmethod
    def deserialize_from_storage(data: dict[str, Any], cipher: SecretCipher) -> AppConfig:
        """从库中读回并解密。"""
        payload = dict(data or {})
        for user in payload.get("users", []):
            if isinstance(user, dict) and user.get("password"):
                try:
                    user["password"] = cipher.decrypt(user["password"])
                except Exception:  # noqa: BLE001
                    pass
        notify = payload.get("notify", {})
        for section in ("email", "serverchan", "dingtalk", "wecom", "telegram", "voice"):
            block = notify.get(section)
            if not isinstance(block, dict):
                continue
            for field in NOTIFY_SECRET_FIELDS:
                if block.get(field):
                    try:
                        block[field] = cipher.decrypt(block[field])
                    except Exception:  # noqa: BLE001
                        pass
        for section, field in SECRET_FIELDS:
            if payload.get(section, {}).get(field):
                try:
                    payload[section][field] = cipher.decrypt(payload[section][field])
                except Exception:  # noqa: BLE001
                    pass
        return AppConfig.from_dict(payload)

    # ------------------------------------------------------------------ #
    # 热更新订阅
    # ------------------------------------------------------------------ #
    def subscribe(self, callback: Callable[[AppConfig], None]) -> Callable[[], None]:
        """注册配置变更回调，返回取消订阅函数。"""
        with self._lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return _unsubscribe

    def _notify(self, config: AppConfig) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(config)
            except Exception as exc:  # noqa: BLE001 - 单个订阅者失败不影响其他
                log.error("配置热更新回调执行失败: %s", exc)

    # ------------------------------------------------------------------ #
    def update_section(self, section: str, values: dict[str, Any], *, persist: bool = True) -> AppConfig:
        """局部更新某一节配置（Web 配置中心用）。"""
        if not hasattr(self._config, section):
            raise ConfigError(f"未知配置节: {section}")
        current = self._config.to_dict(mask_secrets=False)
        if section in ("notify",):
            current[section].update(values or {})
        elif isinstance(current.get(section), list):
            current[section] = values if isinstance(values, list) else current[section]
        else:
            current[section] = {**(current.get(section) or {}), **(values or {})}

        candidate = AppConfig.from_dict(current)
        errors = candidate.validate()
        if errors:
            raise ConfigError("配置校验未通过: " + "; ".join(errors))

        self._config = candidate
        self._version += 1
        if persist:
            self._persist(candidate)
        self._notify(candidate)
        return candidate

    @property
    def current(self) -> AppConfig:
        return self._config

    @property
    def version(self) -> int:
        return self._version


# --------------------------------------------------------------------------- #
_loader: ConfigLoader | None = None


def get_loader(
    env_file: Path | None = None,
    cipher: SecretCipher | None = None,
    *,
    db_provider: Callable[[], dict] | None = None,
    refresh: bool = False,
) -> ConfigLoader:
    """获取全局配置加载器（单例）。"""
    global _loader
    if _loader is None or refresh or env_file is not None:
        from ticket_helper.runtime.paths import get_app_paths

        paths = get_app_paths()
        _loader = ConfigLoader(
            env_file=env_file or paths.env_file,
            cipher=cipher or SecretCipher(paths.secret_key_path),
            db_provider=db_provider,
        )
    return _loader
