"""`vendor/pjialin/` 的溯源与合规契约测试。

防的是这几种**真实会发生且静默**的故障
----------------------------------------
1. **手改了上游原件却没人知道** —— 于是「用的是上游代码」变成一句假话，
   而且以后对不上游版本时无法判断是谁改的。
2. **多塞了一个未登记的文件** —— 看起来像上游的，实际是我们写的；溯源失真。
3. **合规文件缺失** —— Apache-2.0 §4 要求随附 LICENSE 与改动声明，
   缺了就是许可违规，而程序照常跑，不会有任何报错。
4. **校验脚本本身是橡皮图章** —— 所以本文件里有篡改用例：
   改动一个字节，`--check` 必须变红。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts import build_vendor_pjialin as mod  # noqa: E402

VENDOR = BASE / "vendor" / "pjialin"
MANIFEST = VENDOR / "PROVENANCE.json"


def test_manifest_matches_the_pinned_upstream_commit():
    """防的是：溯源清单漂了 —— 清单一旦失真，整套「用了上游代码」的声明就没意义。"""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    up = manifest["upstream"]
    assert up["repo"] == "pjialin/py12306"
    assert up["commit"] == "52ccae8b206b828624f185bf8178e1057c6b5fb9", up["commit"]
    assert up["license"] == "Apache-2.0"
    assert up["fetch"].endswith(up["commit"]), "fetch 地址必须锁定到同一个 commit"
    # 90 个文件是实测值；数量变了说明有人增删了 vendor 内容（那必须是有意的）
    assert manifest["count"]["files"] == len(manifest["files"])
    assert manifest["count"]["files"] >= 44, "至少要把 44 个 .py 全搬进来"


def test_py_files_under_vendor_all_registered():
    """防的是：多塞了一个未登记的文件 —— 溯源失真，且没人会发现。

    vendor 下的 .py 分两类，**两类都必须有出处**：

    ==================  ====================================================================
    ``files``           上游原件（逐文件 sha256）
    ``generated``       **由本脚本从上游原件生成**的投影（内容逐字切出，记录来源文件 + sha256）
    ==================  ====================================================================

    第二类是这次新增的（`ticket_facts.py`）：上游那个模块一 import 就拉起
    `order`/`user`/`qrcode`，而我们只要 10 个整数常量与一条正则。
    生成（而不是手抄）才可验证 —— `--check` 会**重新生成一遍再比对**。
    """
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    recorded = set(manifest["files"]) | set(manifest.get("generated", {}))
    actual = {
        p.relative_to(VENDOR).as_posix()
        for p in VENDOR.rglob("*.py")
        if "__pycache__" not in p.parts
    }
    missing = sorted(actual - recorded)
    assert not missing, f"这些 .py 没登记在 PROVENANCE.json 里：{missing}"

    # 生成物必须写清「从哪个上游文件来的」——否则它就成了一个无出处的文件
    for rel, info in manifest.get("generated", {}).items():
        assert info.get("from"), f"{rel} 没记录上游出处"
        for item in info["from"]:
            assert item["sha256"], item
            assert item["path"].startswith("py12306/"), item
    # 数据文件也要在（站点表 112KB、CDN 表 42KB —— 换内核要靠它们）
    for rel in ("data/stations.txt", "data/cdn.txt"):
        assert rel in recorded, f"{rel} 必须随上游一起 vendor"


def test_compliance_files_exist():
    """防的是：许可违规 —— 少了 LICENSE/NOTICE/改动声明，程序照跑但已经违约。"""
    for name in ("LICENSE", "NOTICE", "VENDOR.md"):
        p = VENDOR / name
        assert p.exists(), f"Apache-2.0 合规文件缺失：{name}"
        assert p.stat().st_size > 0, f"{name} 是空文件"
    notice = (VENDOR / "NOTICE").read_text(encoding="utf-8")
    assert "py12306" in notice
    assert "Apache" in notice
    assert "Copyright" in notice, "NOTICE 必须带版权声明"


def test_check_script_passes():
    """`--check` 必须通过 —— 这是 vendor 与上游一致的**唯一**机器判据。"""
    proc = subprocess.run(
        [sys.executable, str(BASE / "scripts" / "build_vendor_pjialin.py"), "--check"],
        capture_output=True, text=True, cwd=str(BASE),
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "校验通过" in proc.stdout


def _copy_vendor(tmp_path: Path) -> Path:
    dst = tmp_path / "pjialin"
    shutil.copytree(mod.VENDOR, dst)
    return dst


def test_tampering_with_an_upstream_file_is_detected(tmp_path, monkeypatch):
    """**这条是给校验器本身做的变异检验。**

    如果改掉上游文件一个字节而 `--check` 仍然通过，
    那整个溯源机制就是橡皮图章 —— 它只能证明「我抄的时候是这样」，
    不能证明「现在还是这样」。
    """
    copy = _copy_vendor(tmp_path)
    monkeypatch.setattr(mod, "VENDOR", copy)
    monkeypatch.setattr(mod, "PROVENANCE", copy / "PROVENANCE.json")
    assert mod.check() == 0, "拷贝出来的干净副本应当通过校验"

    target = copy / "py12306" / "helpers" / "api.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# 偷偷手改一行\n", encoding="utf-8"
    )
    assert mod.check() == 1, "手改了上游文件，--check 必须失败"


def test_extra_unregistered_file_is_detected(tmp_path, monkeypatch):
    """防的是：往 vendor 里塞一个「看起来像上游」的文件，实际是我们写的。"""
    copy = _copy_vendor(tmp_path)
    monkeypatch.setattr(mod, "VENDOR", copy)
    monkeypatch.setattr(mod, "PROVENANCE", copy / "PROVENANCE.json")
    assert mod.check() == 0

    (copy / "py12306" / "helpers" / "our_own_thing.py").write_text(
        "# 我们自己的实现，混进 vendor 冒充上游\n", encoding="utf-8"
    )
    assert mod.check() == 1, "未登记的文件必须让 --check 失败"


def test_missing_compliance_file_is_detected(tmp_path, monkeypatch):
    """防的是：有人「顺手删掉」合规文件来减小体积 —— 那是许可违规。"""
    copy = _copy_vendor(tmp_path)
    monkeypatch.setattr(mod, "VENDOR", copy)
    monkeypatch.setattr(mod, "PROVENANCE", copy / "PROVENANCE.json")
    (copy / "NOTICE").unlink()
    assert mod.check() == 1


def test_vendored_url_table_is_importable_and_pins_the_protocol_facts():
    """防的是：URL 表 import 不进来 —— 那是「用上游源码」最先要用到的东西。

    上游用**绝对 import**（`from py12306.x import y`），所以必须把
    `vendor/pjialin` 注入 `sys.path` 才能 import。这个机制本身也一并钉住，
    否则 P1 的适配层会在运行期才炸。

    ⚠️ 生产侧的注入点是 `ticket_helper/kernel/pjialin/shim.py`，它一旦被
    import 过就会**永久**挂在 `sys.path` 上。所以这里**不能**断言
    「当前不在 path 里」——那会让本用例依赖执行顺序（先跑内核打码用例就红）。
    改成：不在就加、加过才删。
    """
    import importlib

    added = str(VENDOR) not in sys.path
    if added:
        sys.path.insert(0, str(VENDOR))
    try:
        api = importlib.import_module("py12306.helpers.api")
        for name in (
            "API_AUTH_QRCODE_BASE64_DOWNLOAD",
            "API_AUTH_QRCODE_CHECK",
            "API_BASE_LOGIN",
            "API_USER_LOGIN",
            "API_AUTH_UAMTK",
            "API_AUTH_UAMAUTHCLIENT",
            "API_USER_LOGIN_CHECK",
            "API_USER_INFO",
            "API_USER_PASSENGERS",
            "API_SUBMIT_ORDER_REQUEST",
            "API_INITDC_URL",
            "API_GET_QUEUE_COUNT",
            "API_CONFIRM_SINGLE_FOR_QUEUE",
            "API_QUERY_ORDER_WAIT_TIME",
            "API_QUERY_INIT_PAGE",
            "API_CHECK_CDN_AVAILABLE",
        ):
            assert hasattr(api, name), f"上游 URL 表缺少 {name}"

        # 设备指纹已改回官方地址（P1 决策：禁止 pjialin 托管域）
        assert api.API_GET_BROWSER_DEVICE_ID == (
            api.BASE_URL_OF_12306 + "/otn/HttpZF/logdevice"
        ), api.API_GET_BROWSER_DEVICE_ID
        # 被禁的免费打码常量必须**不存在**（而不是指向别处）
        assert not hasattr(api, "API_FREE_CODE_QCR_API"), (
            "API_FREE_CODE_QCR_API 应已删除；它指向被禁的 *.pjialin.com"
        )

        # 这两条是上游确认过的协议事实，顺带钉住：
        # ① 余票页必须先加载（API_QUERY_INIT_PAGE），与本项目 ensure_session_warm 相互印证；
        # ② 登录态检测走 /otn/login/conf 的 data.is_login（不是 checkUser）—— P1 要用它。
        assert api.API_QUERY_INIT_PAGE.endswith("/otn/leftTicket/init")
        assert api.API_USER_LOGIN_CHECK.endswith("/otn/login/conf")
    finally:
        if added:
            sys.path.remove(str(VENDOR))


def test_no_pjialin_host_survives_anywhere_in_vendor():
    """防的是：**回退** —— 有人（或某次上游同步）把托管域又带回来。

    这是「禁止请求 pjialin 托管域」这条需求的**唯一机器判据**：
    扫 `vendor/pjialin/**/*.py` 的**字符串常量**（不是注释 ——
    改动说明里需要引用原值，注释里出现这个词是正常的）。

    只查字符串字面量，所以 `# 原值指向 https://12306-ocr.pjialin.com/` 这类注释不会误报，
    而 `API_X = 'https://...pjialin.com/'` 会立刻被抓住。
    """
    import ast

    offenders: list[str] = []
    for py in VENDOR.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "pjialin.com" in node.value:
                    rel = py.relative_to(VENDOR).as_posix()
                    offenders.append(f"{rel}:{node.lineno} -> {node.value[:60]}")
    assert not offenders, "vendor 里仍有指向被禁托管域的字符串常量：\n  " + "\n  ".join(offenders)


def test_vendored_tree_is_complete_enough_to_import():
    """防的是：vendor 漏搬文件 —— 缺一个 `__init__.py` 就会在 P1 才炸。

    这些模块**全部**要能 import：上游的 import 是绝对路径 + 无条件互相引用，
    所以任何一个 import 失败都意味着 requirements 里少了东西
    （实测过：少了 `lxml_html_clean` 会在 requests_html 处直接 ImportError）。
    """
    import importlib

    added = str(VENDOR) not in sys.path
    if added:
        sys.path.insert(0, str(VENDOR))
    try:
        for name in (
            "py12306.config",
            "py12306.helpers.api",
            "py12306.helpers.station",
            # 下面几个依赖 requests-html / redis / pyppeteer（见 requirements.txt 的说明）
            "py12306.helpers.OCR",
            "py12306.helpers.request",
            "py12306.helpers.cdn",
        ):
            assert importlib.import_module(name) is not None, name
    finally:
        if added:
            sys.path.remove(str(VENDOR))

def test_shim_does_not_shadow_our_own_modules():
    """★ 防的是：装垫片之后 ``import main`` 变成了**上游的入口**。

    ``vendor/pjialin/`` 根目录里有一个 ``main.py``（上游的启动入口），
    而本项目根目录也有 ``main.py``。垫片若把 vendor 插到 ``sys.path`` **最前面**，
    我们的 ``import main`` 就会被上游那个抢走 —— 而报错是
    ``ModuleNotFoundError: No module named 'flask_jwt_extended'``（上游 web 的依赖），
    跟「谁抢了 main」八竿子打不着。这条把它钉住。

    （真机踩过：同一批里先跑装了垫片的测试、再跑 ``test_contracts`` 里
    ``import main`` 的那条，就会以这个面目失败。）
    """
    from ticket_helper.kernel.pjialin.shim import VENDOR_ROOT, install

    install()
    import main as main_mod  # noqa: PLC0415

    resolved = Path(main_mod.__file__).resolve()
    assert resolved == (BASE / "main.py").resolve(), resolved
    assert VENDOR_ROOT not in resolved.parents, resolved


def test_vendor_path_is_appended_not_prepended() -> None:
    """直接钉住垫片的注入位置：**末尾**（不是最前面）。

    上一条看的是「结果」；这一条看的是「机制」—— 若有人为了「让上游优先」改回
    ``insert(0, ...)``，上一条会因为 ``main`` 被抢走而变红，这一条会直接指出原因。
    """
    from ticket_helper.kernel.pjialin.shim import VENDOR_ROOT, install

    install()
    root = str(VENDOR_ROOT)
    assert root in sys.path, "垫片没把 vendor 挂上 sys.path"
    assert sys.path.index(root) == len(sys.path) - 1, (
        "vendor 必须在 sys.path 末尾，否则会抢走与本项目重名的模块（例如 main）"
    )

def test_shim_does_not_shadow_our_own_modules():
    """★ 防的是：装垫片之后 ``import main`` 变成了**上游的入口**。

    ``vendor/pjialin/`` 根目录里有一个 ``main.py``（上游的启动入口），
    而本项目根目录也有 ``main.py``。垫片若把 vendor 插到 ``sys.path`` **最前面**，
    我们的 ``import main`` 就会被上游那个抢走 —— 而报错是
    ``ModuleNotFoundError: No module named 'flask_jwt_extended'``（上游 web 的依赖），
    跟「谁抢了 main」八竿子打不着。这条把它钉住。

    （真机踩过：同一批里先跑装了垫片的测试、再跑 ``test_contracts`` 里
    ``import main`` 的那条，就会以这个面目失败。）
    """
    from ticket_helper.kernel.pjialin.shim import VENDOR_ROOT, install

    install()
    import main as main_mod  # noqa: PLC0415

    resolved = Path(main_mod.__file__).resolve()
    assert resolved == (BASE / "main.py").resolve(), resolved
    assert VENDOR_ROOT not in resolved.parents, resolved


def test_vendor_path_is_appended_not_prepended() -> None:
    """直接钉住垫片的注入位置：**末尾**（不是最前面）。

    上一条看的是「结果」；这一条看的是「机制」—— 若有人为了「让上游优先」改回
    ``insert(0, ...)``，上一条会因为 ``main`` 被抢走而变红，这一条会直接指出原因。
    """
    from ticket_helper.kernel.pjialin.shim import VENDOR_ROOT, install

    install()
    root = str(VENDOR_ROOT)
    assert root in sys.path, "垫片没把 vendor 挂上 sys.path"
    assert sys.path.index(root) == len(sys.path) - 1, (
        "vendor 必须在 sys.path 末尾，否则会抢走与本项目重名的模块（例如 main）"
    )


def test_our_project_root_precedes_vendor_on_sys_path() -> None:
    """★ 防的是：**上游目录排到了本项目根前面**，于是 `import main` 拿到上游入口。

    机制（真机上踩到过，症状极具误导性）：`vendor/pjialin/` 根目录里也有一个
    `main.py`。一旦有人 `sys.path.insert(0, VENDOR)`，`import main` 就解析到
    **上游的启动入口**，而它 `from py12306.web.web import Web` →
    `ModuleNotFoundError: No module named 'flask_jwt_extended'`
    —— 报错指向一个没人问过的包，跟「谁抢了 main」八竿子打不着。

    而且它是**顺序依赖**的：只在特定测试文件组合下才出现
    （`tests/test_query_baseline.py` 曾在模块级插入且从不还原）。

    判据是**不变量**，不依赖「谁先跑」：本项目根必须排在上游目录之前。
    """
    base = str(BASE)
    vendor = str(VENDOR)
    assert vendor in sys.path, "上游目录应当已被挂上（垫片或测试夹具）"
    assert base in sys.path, "本项目根必须在 sys.path 上"
    assert sys.path.index(base) < sys.path.index(vendor), (
        "本项目根必须排在上游目录**前面**：vendor 里也有 main.py，"
        "顺序反了 `import main` 会拿到上游入口（症状是 flask_jwt_extended 缺失）"
    )
