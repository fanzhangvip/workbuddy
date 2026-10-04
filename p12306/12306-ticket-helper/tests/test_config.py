"""配置模块测试（CF-T1 ~ CF-T5）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ticket_helper.config.loader import ConfigLoader
from ticket_helper.config.settings import AppConfig, TaskConfig, UserConfig
from ticket_helper.errors import ConfigError, ValidationError


def _write_env(home: Path, content: str) -> Path:
    path = home / "env.py"
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# CF-T1 合法配置加载
# --------------------------------------------------------------------------- #
def test_load_valid_env(temp_home, cipher):
    env_file = _write_env(
        temp_home,
        "ENV = {\n"
        "    'log_level': 'DEBUG',\n"
        "    'users': [{'username': 'u1', 'password': 'p1', 'heartbeat': 90}],\n"
        "    'tasks': [{\n"
        "        'name': 'T1', 'left_dates': ['2026-10-01'],\n"
        "        'from_stations': ['广州'], 'to_stations': ['长沙'],\n"
        "        'seat_types': ['二等座'], 'passengers': ['张三'],\n"
        "    }],\n"
        "}\n",
    )
    loader = ConfigLoader(env_file, cipher)
    config = loader.load()

    assert config.log_level == "DEBUG"
    assert len(config.users) == 1
    assert config.users[0].username == "u1"
    assert config.users[0].heartbeat == 90
    assert len(config.tasks) == 1
    assert config.tasks[0].left_dates == ["2026-10-01"]
    assert config.tasks[0].seat_types == ["二等座"]


# --------------------------------------------------------------------------- #
# CF-T2 缺失必填项
# --------------------------------------------------------------------------- #
def test_missing_required_fields_returns_errors(temp_home, cipher):
    env_file = _write_env(
        temp_home,
        "ENV = {\n"
        "    'users': [],\n"
        "    'tasks': [{'name': 'T1', 'left_dates': [], 'from_stations': [],\n"
        "               'to_stations': [], 'seat_types': []}],\n"
        "    'web': {'enable': True, 'auth_pass': ''},\n"
        "}\n",
    )
    loader = ConfigLoader(env_file, cipher)
    with pytest.raises(ConfigError) as exc:
        loader.load()
    message = str(exc.value)
    assert "乘车日期" in message
    assert "出发站" in message


def test_web_security_problems_detects_risky_settings():
    """后台空密码/监听公网/放开跨域都必须被识别为安全风险。"""
    from ticket_helper.config.settings import WebConfig

    risky = WebConfig(enable=True, auth_pass="", host="0.0.0.0", strict_cors=False)
    problems = " ".join(risky.security_problems())
    assert "auth_pass" in problems
    assert "非本机地址" in problems
    assert "strict_cors" in problems

    safe = WebConfig(enable=True, auth_pass="strong", host="127.0.0.1", strict_cors=True)
    assert safe.security_problems() == []


def test_validate_reports_all_problems():
    config = AppConfig(
        web=AppConfig().web,
        tasks=[
            TaskConfig(name="", left_dates=["2026/10/01"], from_stations=[], to_stations=[], seat_types=["头等座"]),
        ],
    )
    errors = config.validate()
    joined = " ".join(errors)
    assert "任务名称" in joined
    assert "日期格式非法" in joined
    assert "不支持的席别" in joined


# --------------------------------------------------------------------------- #
# CF-T3 敏感信息加密落库
# --------------------------------------------------------------------------- #
def test_secrets_encrypted_on_persist(temp_home, cipher):
    env_file = _write_env(temp_home, "ENV = {'web': {'auth_pass': 'secret-pw'}}\n")
    loader = ConfigLoader(env_file, cipher)
    config = loader.load()
    config.users = [UserConfig(username="u1", password="my-password")]
    config.notify.email.password = "smtp-token"

    stored = loader.serialize_for_storage(config)
    assert stored["users"][0]["password"].startswith(("F1:", "P1:"))
    assert "my-password" not in str(stored)
    assert "secret-pw" not in str(stored)
    assert "smtp-token" not in str(stored)

    restored = ConfigLoader.deserialize_from_storage(stored, cipher)
    assert restored.users[0].password == "my-password"
    assert restored.notify.email.password == "smtp-token"
    assert restored.web.auth_pass == "secret-pw"


def test_cipher_roundtrip_and_mask(cipher):
    text = "p@ss word 中文"
    encrypted = cipher.encrypt(text)
    assert encrypted != text
    assert cipher.is_encrypted(encrypted)
    assert cipher.decrypt(encrypted) == text
    assert cipher.encrypt("") == ""
    assert cipher.decrypt("") == ""
    assert cipher.mask("abcdef") == "a****f"
    assert cipher.mask("") == ""


# --------------------------------------------------------------------------- #
# CF-T4 非法值拦截
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "task_kwargs, expected",
    [
        ({"query_interval": 0.05}, "查询间隔"),
        ({"strategy": "teleport"}, "策略非法"),
        ({"depart_time_range": ["06:00"]}, "出发时间区间"),
        ({"trains_whitelist": ["G1"], "trains_blacklist": ["G1"]}, "同时存在于白名单与黑名单"),
        ({"max_retry": 0}, "max_retry"),
    ],
)
def test_task_validation_rejects_bad_values(task_kwargs, expected):
    base = dict(
        name="T",
        left_dates=["2026-10-01"],
        from_stations=["广州"],
        to_stations=["长沙"],
        seat_types=["二等座"],
    )
    base.update(task_kwargs)
    errors = TaskConfig(**base).validate()
    assert any(expected in e for e in errors), errors


def test_task_validation_accepts_valid():
    task = TaskConfig(
        name="T",
        left_dates=["2026-10-01", "2026-10-02"],
        from_stations=["广州南"],
        to_stations=["长沙南"],
        seat_types=["二等座", "一等座"],
        depart_time_range=["06:00", "23:00"],
        passengers=["张三"],
    )
    assert task.validate() == []
    assert len(task.combos()) == 2  # 2 个日期 × 1 对起止站


# --------------------------------------------------------------------------- #
# CF-T5 配置热更新
# --------------------------------------------------------------------------- #
def test_hot_reload_triggers_subscribers(temp_home, cipher):
    env_file = _write_env(
        temp_home,
        "ENV = {'ratelimit': {'min_interval': 1.0, 'max_interval': 8.0}}\n",
    )
    loader = ConfigLoader(env_file, cipher)
    loader.load()

    seen: list[float] = []
    unsubscribe = loader.subscribe(lambda cfg: seen.append(cfg.ratelimit.min_interval))

    _write_env(
        temp_home,
        "ENV = {'ratelimit': {'min_interval': 2.5, 'max_interval': 8.0}}\n",
    )
    config = loader.reload()
    assert config.ratelimit.min_interval == 2.5
    assert seen == [2.5]
    assert loader.version == 2

    # 取消订阅后不再收到通知
    unsubscribe()
    _write_env(
        temp_home,
        "ENV = {'ratelimit': {'min_interval': 3.5, 'max_interval': 8.0}}\n",
    )
    loader.reload()
    assert seen == [2.5]


def test_update_section_validates_before_applying(temp_home, cipher):
    env_file = _write_env(temp_home, "ENV = {'log_level': 'INFO'}\n")
    loader = ConfigLoader(env_file, cipher)
    loader.load()

    # 非法值不应生效
    with pytest.raises(ConfigError):
        loader.update_section("ratelimit", {"min_interval": -1})

    # 合法值生效并持久化回调
    persisted: list[dict] = []
    loader._db_provider = persisted.append  # noqa: SLF001
    config = loader.update_section("ratelimit", {"min_interval": 2.0})
    assert config.ratelimit.min_interval == 2.0
    assert persisted


def test_missing_env_file_uses_defaults(temp_home, cipher):
    loader = ConfigLoader(temp_home / "not-exists.py", cipher)
    config = loader.load()
    assert config.api_base == "https://kyfw.12306.cn"
    assert config.users == []


def test_broken_env_file_raises_config_error(temp_home, cipher):
    env_file = _write_env(temp_home, "ENV = {this is not python\n")
    loader = ConfigLoader(env_file, cipher)
    with pytest.raises(ConfigError):
        loader.load()


# --------------------------------------------------------------------------- #
# 回归：模板占位符绝不能变成真实后台密码
# --------------------------------------------------------------------------- #
def test_placeholder_password_is_treated_as_missing():
    from ticket_helper.config.settings import WebConfig

    for value in ("", "   ", "***", "********", "*"):
        assert WebConfig(auth_pass=value).is_password_missing() is True, value
    assert WebConfig(auth_pass="a-real-password").is_password_missing() is False
    # 脱敏输出必须是全星号，才能被 is_password_missing 正确识别为「未设置」
    assert set(WebConfig.MASK) == {"*"}
    assert WebConfig(auth_pass="secret").to_dict_masked()["auth_pass"] == WebConfig.MASK


def test_render_env_content_injects_real_password(tmp_path):
    """回归测试：模板里的示例值不得原样成为后台密码。"""
    from ticket_helper.runtime.context import render_env_content

    example = Path(__file__).resolve().parent.parent / "ticket_helper" / "env.example.py"
    rendered = render_env_content(example, "Abc123XyZ789")
    assert rendered is not None
    assert '"auth_pass": "Abc123XyZ789"' in rendered
    assert '"auth_pass": "********"' not in rendered

    # 模板被改成任意占位内容（且用单引号）时，仍能正确替换且保持原有引号风格
    custom = tmp_path / "custom_env_example.py"
    custom.write_text("ENV = {'web': {'auth_pass': 'WHATEVER'}}\n", encoding="utf-8")
    rendered_custom = render_env_content(custom, "Abc123XyZ789") or ""
    assert "'auth_pass': 'Abc123XyZ789'" in rendered_custom
    assert "WHATEVER" not in rendered_custom

    # 模板里没有该字段时返回 None，由调用方回退到最小配置
    bare = tmp_path / "bare_env_example.py"
    bare.write_text("ENV = {'log_level': 'INFO'}\n", encoding="utf-8")
    assert render_env_content(bare, "x") is None

    # 模板文件不存在同样回退
    assert render_env_content(tmp_path / "nope.py", "x") is None

    # 密码含引号时拒绝替换，避免生成语法错误的 env.py
    assert render_env_content(example, 'has"quote') is None
