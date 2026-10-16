# -*- coding: utf-8 -*-
"""配置加载：读取项目根目录 .env，并向上兼容系统环境变量。

优先级：系统环境变量 > .env 文件。
安全约定：本模块只负责取值，任何日志/输出都不得打印密钥明文。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def _load_env(path: Path) -> dict:
    """极简 .env 解析：KEY=VALUE，忽略空行与 # 注释，去除值两端引号。"""
    data = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


_ENV = _load_env(ENV_PATH)


def get(key: str, default=None):
    """取配置：环境变量优先，其次 .env。"""
    return os.environ.get(key) or _ENV.get(key, default)


# ---- 微伴助手 ----
WEIBAN_BASE = get("WEIBAN_BASE", "https://open.weibanzhushou.com")
CORP_ID = get("corp_id")
SECRET = get("secret")

# ---- 大模型（DeepSeek）----
LLM_BASE_URL = get("LLM_BASE_URL")
LLM_API_KEY = get("LLM_API_KEY")
LLM_MODEL = get("LLM_MODEL")


def check_required() -> list:
    """返回缺失的必填配置项名称列表（用于启动自检）。"""
    missing = []
    if not CORP_ID:
        missing.append("corp_id")
    if not SECRET:
        missing.append("secret")
    return missing
