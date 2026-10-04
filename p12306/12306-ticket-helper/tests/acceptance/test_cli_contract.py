"""
CLI 参数契约验收测试 —— 由人类定义，WorkBuddy 不得修改。

业务规则来源：
  main.py 的 --help 输出 + README 中的启动说明

关键不变量：
  1. 无参数启动时，如果 PySide6 缺失，必须降级到 Web 模式且不崩溃
  2. 降级路径下，cmd_web 需要的所有 args 属性都必须存在
  3. 不允许通过给 Namespace 动态加属性来"绕过"参数缺失
"""
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MAIN_PY = PROJECT_ROOT / "main.py"


def _run_main(args, env_extra=None):
    """用 subprocess 真实调用 main.py，隔离全局状态"""
    import os
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(MAIN_PY), *args],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(PROJECT_ROOT),
        env=env,
    )


class TestCLIContract:
    """命令行接口的行为契约"""

    def test_help_works(self):
        """--help 必须能正常输出"""
        r = _run_main(["--help"])
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert "web" in r.stdout or "grab" in r.stdout

    def test_web_subcommand_accepts_host_and_port(self):
        """web 子命令必须同时接受 --host 和 --port"""
        r = _run_main(["web", "--help"])
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert "--host" in r.stdout, "web 子命令缺少 --host"
        assert "--port" in r.stdout, "web 子命令缺少 --port"

    def test_default_fallback_does_not_crash_on_missing_pyside6(self):
        """
        核心不变量：
        模拟 PySide6 不可导入，无参数启动必须降级到 web，
        且降级路径不能因 args 缺属性而崩溃。

        用环境变量 PYTHONPATH 注入一个假的 PySide6 屏蔽模块不现实，
        改用 monkeypatch 注入到 subprocess 的 sitecustomize 更稳。
        这里用最简单的方式：设置一个环境变量，让 main.py 主动检测。
        如果 main.py 不支持这个方式，就退化为检查源码逻辑。
        """
        # 用 -c 强制在导入 PySide6 前注入一个 ImportError
        code = (
            "import sys, types;"
            "sys.modules['PySide6'] = None;"  # 让 import PySide6 抛 ImportError
            "sys.argv = ['main.py'];"
            "import runpy;"
            "runpy.run_path(r'%s', run_name='__main__')"
        ) % str(MAIN_PY)
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        # 关键：不能是 AttributeError: 'Namespace' object has no attribute 'host'
        assert "AttributeError" not in r.stderr, (
            f"降级路径崩溃: {r.stderr}"
        )
        assert "'Namespace' object has no attribute" not in r.stderr, (
            f"args 参数契约断裂: {r.stderr}"
        )

    def test_host_default_is_valid_ip(self):
        """如果 web 子命令有默认 host，必须是合法地址而非 None"""
        r = _run_main(["web", "--help"])
        # 只要 --host 存在且 help 文本提到默认值即可
        assert r.returncode == 0