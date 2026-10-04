"""
配置热重载验收测试 —— 由人类定义，WorkBuddy 不得修改。

业务规则来源：
  docs/ 中的配置说明 + 用户预期：
    修改配置文件后，运行中的系统应在一个轮询周期内感知新值。

关键不变量：
  1. 热重载后，从"新加载的配置"读到的值必须是新值
  2. 订阅者必须收到通知
  3. 不允许只更新单例的一部分字段
"""
import json
import textwrap
from pathlib import Path

import pytest


class TestHotReloadContract:
    """热重载的行为契约"""

    def test_reload_updates_ratelimit_min_interval(self, tmp_path, monkeypatch):
        """
        核心场景：
        1. 加载初始配置（min_interval=1.0）
        2. 修改配置文件（min_interval=2.5）
        3. 触发重载
        4. 断言新配置对象的 min_interval == 2.5
        """
        from ticket_helper.config import loader as config_loader

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "version": 1,
            "ratelimit": {"min_interval": 1.0},
        }), encoding="utf-8")

        cfg1 = config_loader.load(str(config_file))
        assert cfg1.ratelimit.min_interval == 1.0

        # 修改文件
        config_file.write_text(json.dumps({
            "version": 2,
            "ratelimit": {"min_interval": 2.5},
        }), encoding="utf-8")

        # 重新加载（模拟热重载）
        cfg2 = config_loader.load(str(config_file))
        assert cfg2.ratelimit.min_interval == 2.5, (
            f"热重载后 min_interval 应为 2.5，实际 {cfg2.ratelimit.min_interval}"
        )

    def test_subscribers_are_notified_on_reload(self, tmp_path):
        """
        订阅者契约：重载时，所有注册的订阅者必须被调用一次。
        """
        from ticket_helper.config import loader as config_loader

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"version": 1}), encoding="utf-8")

        called = []

        def subscriber(new_cfg):
            called.append(new_cfg.version if hasattr(new_cfg, "version") else None)

        # 具体注册接口名由你根据 loader 的真实 API 调整
        # 如果 loader 没有 subscribe，说明契约缺失，测试失败是合理的
        assert hasattr(config_loader, "subscribe") or hasattr(
            config_loader, "ConfigLoader"
        ), "配置加载器缺少订阅接口"

        # 如果有 ConfigLoader 类，用它的实例
        if hasattr(config_loader, "ConfigLoader"):
            loader = config_loader.ConfigLoader(str(config_file))
            loader.subscribe(subscriber)
            loader.reload()
            assert len(called) >= 1, "重载后订阅者未被通知"

    def test_reload_does_not_mutate_old_config(self, tmp_path):
        """
        不可变性契约：
        重载产生的新配置不应修改旧配置对象的字段。
        这防止"全局单例被就地修改"的 bug。
        """
        from ticket_helper.config import loader as config_loader

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "version": 1,
            "ratelimit": {"min_interval": 1.0},
        }), encoding="utf-8")

        cfg1 = config_loader.load(str(config_file))
        old_value = cfg1.ratelimit.min_interval

        config_file.write_text(json.dumps({
            "version": 2,
            "ratelimit": {"min_interval": 9.9},
        }), encoding="utf-8")
        cfg2 = config_loader.load(str(config_file))

        # cfg1 的值不能被 cfg2 的加载过程改变
        assert cfg1.ratelimit.min_interval == old_value, (
            "旧配置对象被热重载就地修改了（全局单例污染）"
        )