"""打包产物完整性（P5）——「上游内核到底有没有进包、进对了没有」。

这一批测试防的全是**静默**故障：源码模式下一切正常，只有装上 exe 才暴露，
而那时用户已经在挂了。所以判据不能是「spec 里写了 vendor」，必须是
**「spec 把 vendor 放到了垫片会去找的那个路径上」**。

真机对照（离线验不了的那部分）：`scripts/build_exe.py` 出包之后，
`scripts/check_packaging.py` 能确认配置对；
「产出的 exe 真能 import py12306」由打包日志与产物自检覆盖。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent

#: 垫片写死的寻找公式——两边必须一致（见 test_spec_ships_the_vendor_tree_...）。
SHIM_RELATIVE = Path("ticket_helper") / "kernel" / "pjialin" / "shim.py"


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, BASE / relative)
    module = importlib.util.module_from_spec(spec)
    assert hasattr(spec.loader, "exec_module"), relative
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def packaging():
    """打包自检脚本（独立加载：它同时也是被测对象）。"""
    return _load("packaging_probe", "scripts/check_packaging.py")


def test_spec_ships_the_vendor_tree_where_the_shim_looks_for_it(packaging) -> None:
    """★ 防的是：上游内核进了包，却**落到了垫片找不着的地方**。

    垫片里写死 `Path(__file__).resolve().parents[3] / "vendor" / "pjialin"`，
    冻结后 `parents[3]` 就是 `sys._MEIPASS`。所以 datas 必须把
    `vendor/pjialin` 映射到包内 `vendor/pjialin`。

    映射成 `vendor`、或换个名字、或干脆没这一条 —— 症状都是同一个：
    `shim.is_available()` 为假 → **所有 pjialin 后端静默回退成 self**，
    不报错、只是打码不再用上游、行为悄悄变了。
    """
    datas = packaging.load_spec()["datas"]
    landed = packaging.bundle_landing(datas, packaging.VENDOR_ROOT)
    assert landed == "vendor/pjialin", landed

    # 钉住垫片的**深度假设**：它靠 parents[3] 找包根，多一层少一层都会算错，
    # 而算错的表现同样是「静默回退」。
    depth = len(SHIM_RELATIVE.parts)
    assert depth == 4, f"{SHIM_RELATIVE.as_posix()} 距包根 {depth} 层，parents[3] 会算错"

    # 源码布局下两者必须指到同一个目录（冻结布局由上面两条共同保证）
    from ticket_helper.kernel.pjialin import shim  # noqa: PLC0415

    assert shim.VENDOR_ROOT == packaging.VENDOR_ROOT.resolve()


def test_bundle_landing_reports_nothing_when_vendor_is_not_shipped(packaging) -> None:
    """负向对照：这个判据**真的会红**（否则上一条是空断言）。

    `None` 是「没有映射到它」的契约值 —— 这条检查的正是这个返回值。
    """
    assert packaging.bundle_landing([], packaging.VENDOR_ROOT) is None  # noqa: WEAK-ASSERT - None 是契约值
    assert (  # noqa: WEAK-ASSERT - 同上
        packaging.bundle_landing([("/somewhere/else", "x")], packaging.VENDOR_ROOT) is None
    )
    # 映射到别处也必须被认出来（改成 vendor 少一层就找不到了）
    assert packaging.bundle_landing([(str(packaging.VENDOR_ROOT), "vendor")], packaging.VENDOR_ROOT) == "vendor"
    # 换成 vendor 整棵树时，落点应当仍能算到 vendor/pjialin
    assert (
        packaging.bundle_landing(
            [(str(packaging.VENDOR_ROOT.parent), "vendor")], packaging.VENDOR_ROOT
        )
        == "vendor/pjialin"
    )


def test_no_reachable_upstream_dependency_is_left_without_a_home(packaging) -> None:
    """上游可达路径上用到的第三方包，全都要有归属（进包或登记豁免）。"""
    problems, _ = packaging.check()
    assert problems == [], problems


def test_upstream_files_outside_pjialin_are_statically_imported(packaging) -> None:
    """`vendor/` 里不走 sys.path 注入的部分，必须被**静态** import 到。

    `vendor/testerSunshine/standby.py` 是靠
    `from vendor.testerSunshine import standby` 正常 import 的 ——
    这条路 PyInstaller 看得见，所以不需要写进 datas。
    一旦有人改成按路径加载，静态分析就看不见了、模块不会进包，
    而源码模式下完全正常。
    """
    problems, _ = packaging.statically_imported_helpers()
    assert problems == [], problems


def test_upstream_web_backend_is_kept_out_of_the_bundle(packaging) -> None:
    """上游自带的 web 后台（`py12306.web.**`）不在可达路径上 —— 别把它收进包。

    判据是**算出来的**，不是「我以为」：`flask_jwt_extended` 在上游源码里
    出现过，但不在可达集合里，所以不需要进包、也不该出现在豁免清单里。
    这条同时守住「豁免清单不腐烂」：清单里的名字必须是真的可达但有意跳过。
    """
    required, unreachable_only, reached, _ = packaging.dependency_report()

    assert "py12306.web.handler.app" not in reached
    assert "flask_jwt_extended" not in required
    assert "flask_jwt_extended" in unreachable_only

    for name, reason in packaging.ALLOWED_MISSING.items():
        assert name in required, f"{name!r} 不在可达路径上 —— 不该留在豁免清单里（清单在腐烂）"
        assert len(reason.strip()) >= 10, (name, reason)


def test_onefile_mode_goes_through_the_same_spec() -> None:
    """★ 防的是：单文件那条路另拼一份打包参数，于是**上游内核没进包**。

    改造前 `scripts/build_exe.py` 的 `--onefile` 分支自己拼了
    `--add-data / --hidden-import`，spec 里加的 vendor 它完全不知道。
    症状：目录版一切正常，**单文件版打码/过滑块全废**，而源码模式看不出来。
    `--onefile` 与目录版必须共用同一份定义（`TICKET_HELPER_ONEFILE=1` 只是开关）。

    ⚠️ 查的是**代码里的字符串常量**（AST），不是文本包含 ——
    注释里出现 `--add-data` 是在解释这段历史，不该被判成违规（第一版就踩了）；
    而 `--onefile` 是**我们自己脚本的命令行开关**（argparse 里那个），
    它本来就该在，所以只查 PyInstaller 的参数。
    """
    import ast  # noqa: PLC0415

    builder = BASE / "scripts" / "build_exe.py"
    tree = ast.parse(builder.read_text(encoding="utf-8"))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for flag in ("--add-data", "--hidden-import", "--windowed", "--paths"):
        assert flag not in literals, f"onefile 又在自己拼 PyInstaller 参数（{flag}）—— 应当只走 spec"

    # 两种形态都必须走 spec，并经环境变量区分
    assert "TicketHelper.spec" in " ".join(literals) or str(BASE / "TicketHelper.spec") in literals
    assert "TICKET_HELPER_ONEFILE" in literals

    spec = (BASE / "TicketHelper.spec").read_text(encoding="utf-8")
    assert 'os.environ.get("TICKET_HELPER_ONEFILE")' in spec

    # 两种形态都要能真的被读出来，且都带着 vendor
    onedir = packaging_datas()
    onefile = packaging_datas("1")
    assert onedir == "vendor/pjialin", onedir
    assert onefile == "vendor/pjialin", onefile


def packaging_datas(onefile: str | None = None) -> str | None:
    """按 onedir / onefile 两种形态读 spec，返回 vendor 的包内落点。"""
    import os  # noqa: PLC0415

    module = _load("packaging_probe_modes", "scripts/check_packaging.py")
    previous = os.environ.get("TICKET_HELPER_ONEFILE")
    try:
        if onefile is None:
            os.environ.pop("TICKET_HELPER_ONEFILE", None)
        else:
            os.environ["TICKET_HELPER_ONEFILE"] = onefile
        datas = module.load_spec()["datas"]
        return module.bundle_landing(datas, module.VENDOR_ROOT)
    finally:
        if previous is None:
            os.environ.pop("TICKET_HELPER_ONEFILE", None)
        else:
            os.environ["TICKET_HELPER_ONEFILE"] = previous


def test_doctor_reports_the_upstream_kernel_as_a_first_class_check() -> None:
    """`doctor` 必须把「上游内核能不能真的 import」当成一等公民报出来。

    为什么不能只靠「vendor 目录在不在」：`shim.is_available()` 只看目录，
    而真正会炸的是 `import py12306.config`（缺依赖时在**冻结环境**里才暴露）。
    所以 `_check_upstream_kernel` 分三级：垫片 → import → 可用，
    失败时那句话直接指向「包打错了」还是「依赖没收全」。

    这条钉住成功路径的**结论与出处**（是哪一种运行方式、重定向了几项）。
    """
    from main import _check_upstream_kernel  # noqa: PLC0415

    name, ok, detail = _check_upstream_kernel()
    assert name == "上游内核"
    assert ok is True, detail
    assert "py12306 已加载" in detail, detail
    assert "源码" in detail or "打包产物" in detail, detail
