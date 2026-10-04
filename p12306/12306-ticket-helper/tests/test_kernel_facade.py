"""内核门面契约测试（C1–C8）。

每条都写明**防的是哪一种真实会发生且静默的故障**。
判定标准：撤掉实现必须变红 —— 本文件的用例都经得起这条
（`scripts/contract_mutation_check.py` 里可加对应变异）。

依据：``docs/内核门面架构设计.md`` §7。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from ticket_helper import kernel  # noqa: E402
from ticket_helper.errors import ConfigError, InterError  # noqa: E402

VENDOR_DIR = BASE / "vendor" / "testerSunshine"
REF_DIR = BASE / "reference" / "upstream" / "testerSunshine_standby"
KERNEL_DIR = BASE / "ticket_helper" / "kernel"

#: 业务层目录 —— 这些**不许** import kernel（灰度是部署配置，不是业务分支）
BUSINESS_DIRS = ("grab", "standby", "order", "query", "account", "notify")

SAMPLE_PASSENGERS = [
    {
        "passenger_name": "张三",
        "passenger_id_type_code": "1",
        "passenger_id_no": "110101199001011234",
        "passenger_type": "1",
        "born_date": "1988-07-23",
        "allEncStr": "enc-1",
    },
    {
        "passenger_name": "李四",
        "passenger_id_type_code": "1",
        "passenger_id_no": "110101196002022345",
        "passenger_type": "1",
        "born_date": "1960-02-11",
        "allEncStr": "enc-2",
    },
]


def _imported_tops(py_file: Path) -> set[str]:
    """列出某文件顶层 import 的模块名首段（静态分析，不执行它）。"""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            tops.add(node.module.split(".")[0])
    return tops


# --# C1：门面层不得依赖具体实现
# --------------------------------------------------------------------------- #
def test_c1_facade_does_not_import_implementations():
    """防的是：门面与实现绑死 —— 一旦绑死，切换就失去意义，且会形成循环依赖。

    这种故障是**静默的**：加了 import 之后一切照旧能跑，
    只有到「想换掉 inter 的实现」时才发现换不掉。

    ⚠️ 规则在 P1 精确化了：**不是「整个 kernel 都不许 import inter」**，
    而是按目录分工（见 `ticket_helper/kernel/self/__init__.py` 的说明）：

    - `kernel/domains/` 与 `kernel/*.py`（门面）：**不许** import inter / py12306
    - `kernel/registry.py`（唯一切换点）：**不许** import inter（只能 import 适配层）
    - `kernel/self/`：**可以** import inter —— 它的职责就是适配 inter
    - `kernel/pjialin/`：不许 import inter（可 import py12306）

    最初写成「全部 kernel 都不许」，那会让 `kernel/self/` 无法存在 ——
    适配器**必须**能碰到它适配的那个实现，否则就不是适配器了。
    """
    facade_files = list(KERNEL_DIR.glob("*.py")) + list((KERNEL_DIR / "domains").glob("*.py"))
    offenders: list[str] = []
    for py in facade_files:
        for top in _imported_tops(py):
            if top in {"inter", "py12306", "desktop_ui", "web", "mock_server"}:
                offenders.append(f"{py.relative_to(BASE)} -> {top}")
        text = py.read_text(encoding="utf-8")
        for bad in ("ticket_helper.inter", "py12306."):
            if bad in text:
                offenders.append(f"{py.relative_to(BASE)} 源码里出现 {bad}")
    assert not offenders, f"门面层不许依赖具体实现：{offenders}"


def test_c1b_self_adapter_is_the_only_one_that_may_touch_inter():
    """防的是：把「允许」当成「随便」—— 适配层可以 import inter，
    但门面（domains / registry）不得跟着一起依赖它。"""
    self_dir = KERNEL_DIR / "self"
    assert self_dir.exists(), "kernel/self/ 是 inter 的适配层，不应缺失"
    registry_src = (KERNEL_DIR / "registry.py").read_text(encoding="utf-8")
    assert "ticket_helper.inter" not in registry_src
    domains_src = "\n".join(
        q.read_text(encoding="utf-8") for q in (KERNEL_DIR / "domains").glob("*.py")
    )
    assert "ticket_helper.inter" not in domains_src


# --------------------------------------------------------------------------- #
# C2：业务层不得 import kernel
# --------------------------------------------------------------------------- #
def test_c2_business_layer_does_not_import_kernel():
    """防的是：灰度泄漏进业务语义 —— 换部署配置等于改业务逻辑。

    一旦业务层里出现「当前用的是哪套协议」的判断，
    「切一件事、退一件事」就不可能做到，回退也不再干净。
    """
    offenders: list[str] = []
    for sub in BUSINESS_DIRS:
        for py in (BASE / "ticket_helper" / sub).rglob("*.py"):
            tops = _imported_tops(py)
            if "kernel" in tops or "ticket_helper.kernel" in py.read_text(encoding="utf-8"):
                offenders.append(str(py.relative_to(BASE)))
    assert not offenders, f"业务层不许 import kernel：{offenders}"


# --------------------------------------------------------------------------- #
# C3：默认后端 = 上游实现（决策 B），且回退后端在册
# --------------------------------------------------------------------------- #
def test_c3_default_backend_is_upstream_per_decision_b():
    """防的是：默认后端被悄悄改掉（换默认值 = 换行为）。

    2026-09-29 决策 B：候补必须用上游 `testerSunshine/12306`
    （`pjialin/py12306` 没有候补，0 处）。
    """
    assert kernel.DEFAULT_BACKEND == "testerSunshine", kernel.DEFAULT_BACKEND
    assert kernel.ROLLBACK_BACKEND == "self", kernel.ROLLBACK_BACKEND
    assert set(kernel.available_backends()) == {"self", "testerSunshine"}


def test_c3b_default_rollback_switch_actually_works(monkeypatch):
    """防的是：回退开关写了但没接上 —— 「一键回退」变成一句空话。"""
    monkeypatch.delenv(kernel.ENV_VAR, raising=False)
    assert kernel.get_standby_payload().name == "testerSunshine"

    monkeypatch.setenv(kernel.ENV_VAR, kernel.ROLLBACK_BACKEND)
    assert kernel.get_standby_payload().name == "self"


# --------------------------------------------------------------------------- #
# C4：self 后端输出 == 改造前的字面量（回退保真）
# --------------------------------------------------------------------------- #
def test_c4_self_backend_output_is_the_legacy_literal():
    """防的是：「回退能回到从前」只是口头承诺。

    这里的字面量是 2026-09-29 改造前 `TicketApi.confirm_hb` 直接写在代码里的那份，
    一个字都没按新写法调整过。
    """
    builder = kernel.SelfStandbyPayloadBuilder()
    assert builder.name == "self"

    info = builder.build_passenger_info(SAMPLE_PASSENGERS)
    chunks = [c for c in info.split(";") if c]
    assert len(chunks) == 2
    for chunk in chunks:
        assert len(chunk.split("#")) == 6, chunk  # F3：本项目旧写法是 6 段
    assert chunks[0] == "1#张三#1#110101199001011234#enc-1#0"
    assert chunks[1].endswith("#1"), "1960 年生 → 60 岁以上标记位应为 1"

    params = builder.confirm_hb_params(
        passenger_info=info,
        jz_param="2026-10-01#19#00",  # ← 故意给上真值，本后端必须无视它（F2）
        train_no="G6102",
        seat_code="O",
        seat_name="二等座",
        server_train_no="5l000G61020A",  # ← 故意给上，本后端必须无视它（F1）
    )
    assert params == {
        "passengerInfo": info,
        "jzParam": "",  # F2：回退后端固定空串
        "hbTrain": "G6102,O#",  # F1：回退后端用本地车次号
        "lkParam": "",
        "sessionId": "",
        "sig": "",
        "scene": "nc_login",
        "encryptedData": "",
        "if_receive_wseat": "N",
        "realize_limit_time_diff": 1440,
    }
    assert list(params) == [
        "passengerInfo", "jzParam", "hbTrain", "lkParam", "sessionId", "sig",
        "scene", "encryptedData", "if_receive_wseat", "realize_limit_time_diff",
    ], "键顺序也要一致 —— 表单乱序会让差分比对产生假差异"

    # 缺加密串必须明确报错（两个后端共有的安全底线）
    with pytest.raises(InterError) as exc:
        builder.build_passenger_info([{"passenger_name": "王五", "allEncStr": ""}])
    assert "allEncStr" in str(exc.value)


# --------------------------------------------------------------------------- #
# C5：上游后端输出 == 上游原件（且 vendor 文件与原件逐字一致）
# --------------------------------------------------------------------------- #
def test_c5_upstream_backend_matches_upstream_originals():
    """防的是：「用了上游代码」只是口头声明。

    两件事一起验：
    ① vendor 文件与 `reference/upstream/` 里的原件一致（跑生成器的 --check）；
    ② 上游后端的行为就是上游的行为（5 段 / 4 字段）。
    """
    # ① 源码级
    proc = subprocess.run(
        [sys.executable, str(BASE / "scripts" / "build_vendor_testerSunshine.py"), "--check"],
        capture_output=True, text=True, cwd=str(BASE),
    )
    assert proc.returncode == 0, f"vendor 文件与上游原件不一致：{proc.stdout}{proc.stderr}"

    # ② 行为级
    builder = kernel.TesterSunshineStandbyPayloadBuilder()
    assert builder.name == "testerSunshine"
    info = builder.build_passenger_info(SAMPLE_PASSENGERS)
    chunks = [c for c in info.split(";") if c]
    assert len(chunks) == 2
    for chunk in chunks:
        assert len(chunk.split("#")) == 5, f"上游是 5 段：{chunk}"
    assert info.endswith(";"), info
    assert info == "1#张三#1#110101199001011234#enc-1;1#李四#1#110101196002022345#enc-2;", info

    params = builder.confirm_hb_params(
        passenger_info=info,
        jz_param="2026-10-01#19#00",
        train_no="G6102",
        seat_code="O",
        seat_name="二等座",
        server_train_no="5l000G61020A",
    )
    assert list(params) == ["passengerInfo", "jzParam", "hbTrain", "lkParam"], (
        "上游 ConfirmHB.data_apr 只发 4 个字段（F4）"
    )
    assert params["jzParam"] == "2026-10-01#19#00"  # F2
    assert params["hbTrain"] == "5l000G61020A,O#"  # F1：用官方返回的车次号
    assert params["lkParam"] == ""


def test_c5b_upstream_seat_table_rejects_codes_with_a_clear_error():
    """防的是：把「席别码」当「席别名」传进上游 —— 它会静默拼出 `,None#`。

    上游 `data_apr` 走 `PASSENGER_TICKER_STR.get(SET_TYPE[0])`，
    拿不到就得到 `None`，**不报错**，只是拼出一个官方必然拒绝的串，
    使用者只会看到一句「系统忙，请稍后重试」。这条用例把这个坑钉住。
    """
    with pytest.raises(InterError) as exc:
        kernel.TesterSunshineStandbyPayloadBuilder().confirm_hb_params(
            passenger_info="1#张三#1#1#enc;",
            jz_param="",
            train_no="T1",
            seat_code="O",
            seat_name="O",  # ← 传码而不是名
        )
    assert "席别" in str(exc.value)


def test_c5c_vendor_provenance_manifest_matches_sources():
    """防的是：`PROVENANCE.json` 漂了 —— 溯源一旦失真，整套合规声明就没意义。"""
    import hashlib
    import json

    manifest = json.loads((VENDOR_DIR / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert manifest["upstream"]["repo"] == "testerSunshine/12306"
    assert manifest["upstream"]["license"] == "MIT"
    for name, meta in manifest["sources"].items():
        actual = hashlib.sha256((REF_DIR / name).read_bytes()).hexdigest()
        assert actual == meta["sha256"], f"{name} 原件被换过，溯源清单已过期"
    assert manifest["output"]["sha256"] == hashlib.sha256(
        (VENDOR_DIR / "standby.py").read_bytes()
    ).hexdigest(), "vendor 产物被手改过（应重跑生成器）"
    assert (VENDOR_DIR / "LICENSE").exists(), "MIT 要求随附许可证全文"


# --------------------------------------------------------------------------- #
# C6：name 与注册表键一致
# --------------------------------------------------------------------------- #
def test_c6_builder_names_match_registry_keys():
    """防的是：注册名与自报名两处漂移成两个不同的东西。

    漂移后 `get_standby_payload()` 仍然能按注册键取到对象，
    但日志里打印的 `builder.name` 是另一个值 —— 排障时会被误导。
    """
    for key in kernel.available_backends():
        builder = kernel.get_standby_payload.__globals__["_BUILDERS"][key]
        assert builder.name == key, f"{key} 的 name 是 {builder.name}"
        assert isinstance(builder, kernel.StandbyPayloadBuilder), key


# --------------------------------------------------------------------------- #
# C7：非法开关值必须快速失败
# --------------------------------------------------------------------------- #
def test_c7_invalid_backend_fails_fast(monkeypatch):
    """防的是：静默回退 —— 「我明明切了 upstream」变成一句无法查证的猜测。

    静默回退比报错危险得多：使用者以为换的是 A，实际跑的是 B，
    而两边行为不同（5 段 vs 6 段），却没有任何迹象。
    """
    monkeypatch.setenv(kernel.ENV_VAR, "upstream")  # 曾经的旧名，已废弃
    with pytest.raises(ConfigError) as exc:
        kernel.get_standby_payload()
    assert kernel.ENV_VAR in str(exc.value)

    monkeypatch.setenv(kernel.ENV_VAR, "SELF")  # 大小写不敏感 = 让人以为存在两个东西
    with pytest.raises(ConfigError):
        kernel.get_standby_payload()


# --------------------------------------------------------------------------- #
# C8：门面产出确实到达传输层（端到端）
# --------------------------------------------------------------------------- #
def test_c8_facade_payload_reaches_transport(app_context, mock_server, monkeypatch):
    """防的是：门面写了但没接上 —— 最常见也最难发现的「假实现」。

    `kernel/` 里有漂亮的后端、`scripts/kernel_diff.py` 也能打印差异，
    但 `inter/api.py` 如果忘记改成调门面，一切测试仍然全绿，
    而线上跑的还是老代码。所以这条用例看的是**真发出去的请求体**。
    """
    from ticket_helper.account.manager import UserConfig
    from ticket_helper.verify.service import CallbackSolver as Solver

    app_context.verify_service.set_solver(Solver(lambda i, h, k: "112,45", name="fixed"))
    user = app_context.accounts.add_user(UserConfig(username="kfacade", password="pw"))
    assert user.login() is True

    captured: dict[str, dict] = {}
    real_post = user.api._hb_post  # noqa: SLF001

    def spy(path, data, **kwargs):
        if path.endswith("confirmHB"):
            captured["data"] = dict(data)
        return real_post(path, data, **kwargs)

    monkeypatch.setattr(user.api, "_hb_post", spy)

    class _Ticket:
        # 本地推算值 = train_no or train_code。train_no 留空 →
        # local == "G6102"（明文车次）；而官方向（② getSuccessRate）会给内部号。
        # **两者必须不同**，否则 F1 在离线看不出来。
        train_code = "G6102"
        train_no = ""
        secret_str = "SECRET-STR"

    out = user.api.confirm_hb(
        [{"passenger_name": "张三", "passenger_id_no": "110101199001011234",
          "passenger_type": "1", "allEncStr": "enc-1"}],
        ["二等座"],
        _Ticket(),
        seat_type="二等座",
    )
    assert captured, "门面产出没有到达传输层 —— 说明 api.py 没接上门面"

    params = captured["data"]
    builder = kernel.get_standby_payload()
    assert builder.name == "testerSunshine"
    # F4：上游只有 4 个字段
    assert list(params) == ["passengerInfo", "jzParam", "hbTrain", "lkParam"], list(params)
    # F3：上游 5 段
    assert len(params["passengerInfo"].rstrip(";").split("#")) == 5
    # F2：jzParam 来自 ④ passengerInitApi（mock 按上游字段名下发 jzdhDateE/jzdhHourE）
    assert params["jzParam"], "jzParam 为空 → F2 的数据没接上"
    assert "#" in params["jzParam"], params["jzParam"]
    # F1：hbTrain 用 ② getSuccessRate 返回的官方车次号
    # F1：切到上游后端后，hbTrain 用的是 ② 返回的**官方内部车次号**，
    # 而不是本地推算出来的明文车次 "G6102"。这就是 F1 在链路上的体现。
    assert params["hbTrain"] == "5l000G61020A,O#", params["hbTrain"]
    assert out["confirmed"] is True


def test_c8b_rollback_switch_changes_the_wire_payload(app_context, mock_server, monkeypatch):
    """防的是：回退开关只改了日志里的名字，没改真实请求体。

    与 C8 同一套链路，只把开关拨到 `self`，看**发出去的**是不是老格式。
    """
    from ticket_helper.account.manager import UserConfig
    from ticket_helper.verify.service import CallbackSolver as Solver

    app_context.verify_service.set_solver(Solver(lambda i, h, k: "112,45", name="fixed"))
    user = app_context.accounts.add_user(UserConfig(username="kfacade2", password="pw"))
    assert user.login() is True
    monkeypatch.setenv(kernel.ENV_VAR, kernel.ROLLBACK_BACKEND)

    captured: dict[str, dict] = {}
    real_post = user.api._hb_post  # noqa: SLF001

    def spy(path, data, **kwargs):
        if path.endswith("confirmHB"):
            captured["data"] = dict(data)
        return real_post(path, data, **kwargs)

    monkeypatch.setattr(user.api, "_hb_post", spy)

    class _Ticket:
        train_code = "G6102"
        train_no = ""
        secret_str = "SECRET-STR"

    user.api.confirm_hb(
        [{"passenger_name": "张三", "passenger_id_no": "110101199001011234",
          "passenger_type": "1", "allEncStr": "enc-1"}],
        ["二等座"],
        _Ticket(),
        seat_type="二等座",
    )
    params = captured["data"]
    assert len(params) == 10, f"回退后端应是 10 个字段：{list(params)}"
    assert params["jzParam"] == ""
    # F1：回退后端仍用本地推算值（明文车次）
    assert params["hbTrain"] == "G6102,O#", params["hbTrain"]
    assert len(params["passengerInfo"].rstrip(";").split("#")) == 6
