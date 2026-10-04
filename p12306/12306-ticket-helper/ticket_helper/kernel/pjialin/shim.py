"""I-1 隔离垫片：让上游那个「独立应用」能在我们的进程里被安全地调用。

上游不是库，是**配置驱动的独立应用**：`import py12306.config` 之后，
只要有人构造 ``Config()``，它就会

1. 去读它自己的 ``env.py``（与我们的配置文件是两回事）；
2. **起一个后台线程** ``watch_file_change`` 盯着配置文件；
3. 若集群开关为真，还会去连 Redis。

直接塞进我们进程会打架，所以这里做三件事（决策 §10-5 选 I-1）：

======== ====================================================================
① 路径  把它的可写目录（``RUNTIME_DIR`` / ``USER_DATA_DIR`` / ``CONFIG_FILE`` …）
        **重定向到我们的用户目录**。上游默认写在 ``PROJECT_DIR``（= vendor 目录）里，
        打包成 exe 后那可能只读；而且会**污染 vendor 目录**，
        让 `scripts/build_vendor_pjialin.py --check` 当场失败。
② 副作用 屏蔽它起线程 / 连集群 / 远端同步的入口。
③ 时机  以上都在**第一次构造 ``Config()`` 之前**完成 ——
        因为 `singleton` 装饰器在首次构造后会把 `__init__` 换成 `object.__init__`，
        之后再改类属性就不生效了。
======== ====================================================================

**本文件不含任何 12306 协议细节**（路径 / 参数 / 解析）——
它只做配置桥接。这一条由 `tests/test_discipline.py` 机械检查。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ticket_helper.runtime.paths import get_app_paths

#: vendor 目录（上游整包所在）
VENDOR_ROOT = Path(__file__).resolve().parents[3] / "vendor" / "pjialin"

_installed = False


def _inject_sys_path() -> None:
    """上游用**绝对 import**（`from py12306.x import y`），必须把 vendor 目录挂上 sys.path。

    ⚠️ **必须挂在末尾（append），不能插到最前面**。
    当初这里写的是 `insert(0, ...)`，注释还写着「顶层的包名只有 `py12306`，
    不会与别人撞名」—— **这句话是错的**：vendor 根目录里还有一个
    ``main.py``（上游的启动入口），而本项目根目录也有 ``main.py``。
    插到最前面之后，测试里的 ``import main`` 会拿到**上游那个**，
    报错是 ``ModuleNotFoundError: No module named 'flask_jwt_extended'``
    （上游 web 的依赖）—— 症状与「谁抢了 main」八竿子打不着，极难联想。

    挂在末尾：`py12306` / `ticket_facts` 这些**只属于上游**的名字照样能导入，
    而与本项目重名的 `main` 仍然解析到我们自己的那份。
    由 `tests/test_vendor_pjialin.py::test_shim_does_not_shadow_our_own_modules` 守住。
    """
    root = str(VENDOR_ROOT)
    if root not in sys.path:
        sys.path.append(root)


def _redirect_writable_dirs(cfg_cls: Any) -> dict[str, str]:
    """把上游的可写路径搬到我们的用户目录，返回 ``{属性名: 新值}`` 以便测试核对。"""
    base = get_app_paths().home / "upstream"
    runtime = base / "runtime"
    query = runtime / "query"
    user = runtime / "user"
    for d in (runtime, query, user, base):
        d.mkdir(parents=True, exist_ok=True)

    # 上游 `Config.__init__` 会 `get_file_modify_time(CONFIG_FILE)`，
    # **文件不存在会抛 FileNotFoundError** —— 所以这里必须真的放一个（内容可以是空，
    # `EnvLoader.load_with_file` 对空文件返回空列表，也就是"全用默认值"）。
    config_file = base / "env.py"
    if not config_file.exists():
        config_file.write_text("# 由 ticket_helper 生成：上游配置为空 = 全用上游默认值\n", encoding="utf-8")

    moved = {
        "RUNTIME_DIR": f"{runtime}{'/'}",
        "QUERY_DATA_DIR": f"{query}{'/'}",
        "USER_DATA_DIR": f"{user}{'/'}",
        "USER_PASSENGERS_FILE": str(user / "%s_passengers.json"),
        "CDN_ENABLED_AVAILABLE_ITEM_FILE": str(query / "available.json"),
        "CONFIG_FILE": str(config_file),
    }
    for name, value in moved.items():
        setattr(cfg_cls, name, value)
    return moved


def _suppress_side_effects(cfg_mod: Any) -> None:
    """屏蔽上游起线程 / 连集群 / 远端同步的入口。

    做法是替换 **`py12306.config` 这个模块命名空间里**的名字
    （它用 `from ... import *` 拿进来的），而不是去改 `helpers/func` ——
    后者是全局公共工具，改它会影响上游其它模块的正常行为。
    """
    cfg_cls = cfg_mod.Config
    # 关掉集群：`is_slave()` 里 `CLUSTER_ENABLED and ...` 会短路，
    # 于是 `is_master()` 里的 `from py12306.cluster.cluster import Cluster`（会 import redis）根本不会被走到。
    cfg_cls.CLUSTER_ENABLED = 0
    cfg_cls.NODE_IS_MASTER = False
    cfg_cls.CACHE_RAIL_ID_ENABLED = False
    # 不再起「盯配置文件变化」的后台线程，也不做远端配置同步
    cfg_mod.create_thread_and_run = lambda *a, **k: None  # type: ignore[attr-defined]
    cfg_cls.watch_file_change = lambda self, *a, **k: None
    cfg_cls.save_to_remote = lambda self, *a, **k: None


def install() -> dict[str, str]:
    """幂等地安装垫片。返回被重定向的路径，供测试核对。

    返回值刻意用「数据」而不是日志：**能断言的才算证据**。
    """
    global _installed
    _inject_sys_path()
    if not VENDOR_ROOT.exists():
        # vendor 未安装时不要炸：站点表等域会自动回退到 self 后端（见 kernel/registry.py）
        return {}

    import py12306.config as cfg_mod  # noqa: PLC0415  （必须在注入 sys.path 之后）

    moved = _redirect_writable_dirs(cfg_mod.Config)
    if not _installed:
        # 时序很关键：副作用必须在**第一次构造 Config() 之前**屏蔽
        # （singleton 首次构造后会把 __init__ 换成 object.__init__，之后再改类属性就没用了）
        _suppress_side_effects(cfg_mod)
        cfg_mod.Config()
        _installed = True
    return moved


def upstream_module(dotted: str) -> Any:
    """按上游的点分路径 import 一个模块（自动先装垫片）。"""
    install()
    import importlib  # noqa: PLC0415

    return importlib.import_module(dotted)


def is_available() -> bool:
    """vendor 是否就位（用于让门面决定能否切到上游后端）。"""
    return VENDOR_ROOT.exists()
