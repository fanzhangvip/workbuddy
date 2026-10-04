"""打码域（`kernel/domains/captcha.py`）的契约测试。

防的是这几种**真实会发生且静默**的故障
----------------------------------------
1. **又变成假实现**：报告「识别成功」，但请求根本没发出去 / 字段名不对 /
   服务端收不到图。判据是**请求长什么样**，不是函数返回了什么。
2. **坐标换算被偷偷重写**：上游 ``get_image_position_by_offset`` 是本次唯一
   要复用的能力。谁把它改成本地实现（或改了格子常量），与上游的偏移就会分叉，
   而现象是「点击位置偶尔偏一格」—— 没有任何报错。
3. **协议对不上却没人知道**：``self`` 与 ``pjialin`` 两套后端的请求字段名、
   响应约定、坐标来源都不同。混用只会得到一句「识别失败」。
   所以每一处**必须留下可读的原因**（``last_error``）。
4. **切换开关失效**：默认后端被悄悄改掉、或改了地址却还在用旧实例。
"""

from __future__ import annotations

import base64
import random
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from mock_server.echo import EchoServer  # noqa: E402
from ticket_helper.kernel import registry  # noqa: E402
from ticket_helper.kernel.domains.captcha import format_positions  # noqa: E402
from ticket_helper.kernel.pjialin.captcha import (  # noqa: E402
    NAME as PJIALIN_NAME,
    PjialinCaptchaSolver,
)
from ticket_helper.kernel.self.captcha import NAME as SELF_NAME, SelfCaptchaSolver  # noqa: E402
from ticket_helper.verify import captcha_server  # noqa: E402

DOMAIN = "captcha"

def _assert_solve_fails(solver, kind: str, needle: str, *, why: str) -> None:
    """识别失败必须是**两件事**：返回 ``None`` + 留下可读原因。

    合并成一处的原因：分开写会出现 8 条 ``assert ... is None``，
    而它们真正要防的是「失败被吞掉」—— 判据在 ``last_error`` 上。
    """
    answer = solver.solve(b"\x89PNG", kind=kind)
    assert answer is None, f"{why}：应返回 None，实际 {answer!r}"  # noqa: WEAK-ASSERT - None 就是契约规定的失败返回值，真正判据在下一行
    assert needle in solver.last_error, f"{why}：没说清原因 -> {solver.last_error!r}"


# --------------------------------------------------------------------------- #
# 1. 注册表与默认值
# --------------------------------------------------------------------------- #
def test_backend_names_match_the_registry_keys():
    """防的是：模块里的 ``NAME`` 与注册表键不一致 —— 于是「切了没生效」。

    名字是标识符，两处各写一遍迟早分叉；这条用例让分叉当场变红。
    """
    assert registry.available_backends()[DOMAIN] == (SELF_NAME, PJIALIN_NAME)
    assert SELF_NAME == "self"
    assert PJIALIN_NAME == "pjialin"


def test_default_backend_is_upstream_per_decision():
    """防的是：默认后端被悄悄改掉（换默认值 = 换行为）。

    决策 §10-1/2/3：打码必须用上游 ``pjialin/py12306`` 的代码。
    """
    assert registry.DEFAULT_BACKENDS[DOMAIN] == PJIALIN_NAME


def test_switch_actually_swaps_the_implementation(monkeypatch):
    """防的是：开关写了但没接上（本项目最常见的假实现形态）。"""
    registry.reset_cache()
    monkeypatch.setenv(registry.ENV_VAR, f"{DOMAIN}={SELF_NAME}")
    assert type(registry.get(DOMAIN)).__name__ == "SelfCaptchaSolver"

    registry.reset_cache()
    monkeypatch.setenv(registry.ENV_VAR, f"{DOMAIN}={PJIALIN_NAME}")
    assert type(registry.get(DOMAIN)).__name__ == "PjialinCaptchaSolver"


def test_endpoint_is_part_of_the_cache_key(monkeypatch):
    """防的是：改了地址却还拿到旧实例 —— 现象是「配置生效了但没生效」。"""
    registry.reset_cache()
    monkeypatch.setenv(registry.ENV_VAR, f"{DOMAIN}={PJIALIN_NAME}")
    first = registry.get(DOMAIN, endpoint="http://127.0.0.1:1/a")
    again = registry.get(DOMAIN, endpoint="http://127.0.0.1:1/a")
    other = registry.get(DOMAIN, endpoint="http://127.0.0.1:1/b")
    assert first is again, "相同参数应当复用实例"
    assert first is not other, "不同地址必须给出不同实例"
    assert other.endpoint == "http://127.0.0.1:1/b"


def test_illegal_backend_fails_fast(monkeypatch):
    """非法值必须报错，不许静默回退（否则「我明明切了」无法查证）。"""
    registry.reset_cache()
    monkeypatch.setenv(registry.ENV_VAR, f"{DOMAIN}=zzz")
    with pytest.raises(Exception) as exc:  # noqa: B017 - 具体类型由 kernel 定义
        registry.get(DOMAIN)
    assert "zzz" in str(exc.value)
    registry.reset_cache()


# --------------------------------------------------------------------------- #
# 2. ★ 网格换算：必须是上游公式的逆运算
# --------------------------------------------------------------------------- #
def _upstream_ideal_centers() -> list[int]:
    """取上游公式算出的 8 个格子中心（把抖动置零）。

    做法：上游 ``get_image_position_by_offset`` 每次判断都会调 ``random.randint``，
    把它临时替换成「恒返回 0」，得到的就是**无抖动的理想中心**。
    """
    from ticket_helper.kernel.pjialin import shim

    shim.install()
    from py12306.helpers.OCR import OCR  # noqa: PLC0415

    original = random.randint
    random.randint = lambda a, b: 0  # type: ignore[assignment]
    try:
        return list(OCR().get_image_position_by_offset(list(range(1, 9))))
    finally:
        random.randint = original  # type: ignore[assignment]


def test_grid_math_is_the_inverse_of_upstream():
    """★ 本机打码服务的格子换算必须是**上游公式的逆运算**。

    这一条是本域最重要的外部锚点：它把「我们算格子」与「上游算坐标」
    绑在一起。上游哪天改了格边长（75 → 别的值）或列数，本地换算会立刻对不上，
    而这里的断言会当场变红 —— 不必等真机点错位置才发现。
    """
    ideal = _upstream_ideal_centers()
    assert len(ideal) == 16, "上游 8 个格子应产生 16 个数字（x,y 交替）"

    restored = [
        captcha_server.grid_of_point(ideal[i], ideal[i + 1]) for i in range(0, len(ideal), 2)
    ]
    assert restored == [1, 2, 3, 4, 5, 6, 7, 8]


def test_cell_size_equals_the_upstream_spacing():
    """★ 格边长必须等于**上游相邻格中心的间距**。

    为什么不能只验「中心点落回原格」：格边长从 75 改成 80 时，
    上游那 8 个理想中心**仍然**各自落在原来的格里（间距 75 < 80，不会跨过 80 的边界），
    于是那种测试是绿的。变异检验当场抓到了这个漏洞（见 `contract_mutation_check.py -k 打码`）。

    间距是从上游公式算出来的，所以这条是**外部锚点**，不是自证。
    """
    ideal = _upstream_ideal_centers()
    column_gap = ideal[2] - ideal[0]  # 格 1 → 格 2 的水平间距（第二列 - 第一列）
    row_gap = ideal[9] - ideal[1]  # 格 1 → 格 5 的垂直间距（第二行 - 第一行）
    assert column_gap == captcha_server.CELL, f"上游列间距 {column_gap} != 本地 CELL"
    assert row_gap == captcha_server.CELL, f"上游行间距 {row_gap} != 本地 CELL"
    assert captcha_server.COLUMNS == 4, "上游是 4 列"
    assert captcha_server.ROWS == 2, "上游是 2 行（编号 1..8）"


def test_grid_boundaries_follow_the_upstream_cell_size():
    """格的**边界**要跟上游一致 —— 否则贴近格线的框会被判到隔壁格。

    表现是「点击位置偶尔偏一格」，没有任何报错，极难定位。
    边界值同样由上游间距推出来（不是用本地 ``CELL`` 反推，避免自证）。
    """
    ideal = _upstream_ideal_centers()
    gap = ideal[2] - ideal[0]
    row_one_y = ideal[1]

    # 格 2 的跨度是 [gap, 2*gap)；左边界**内侧一点**必须判为格 2
    assert captcha_server.grid_of_point(gap + 1, row_one_y) == 2
    assert captcha_server.grid_of_point(gap - 1, row_one_y) == 1
    # 格 4 的右边界内侧仍是格 4，越界则丢弃
    assert captcha_server.grid_of_point(gap * 4 - 1, row_one_y) == 4
    # 第二行
    assert captcha_server.grid_of_point(ideal[1] + 1, gap + 1) == 5
    assert captcha_server.grid_of_point(gap * 4 - 1, gap * 2 - 1) == 8


def test_grid_math_tolerates_the_upstream_jitter():
    """上游会给坐标加 ±5 抖动；换算必须仍然落在同一格。

    ⚠️ 抖动是加在**上游算出的中心**上的，所以这里必须以
    ``_upstream_ideal_centers()`` 为基准 —— 若拿本地 ``CELL`` 自己算中心，
    这条就退化成自证（变异检验抓过这个漏洞）。
    """
    ideal = _upstream_ideal_centers()
    for grid in range(1, 9):
        cx = ideal[(grid - 1) * 2]
        cy = ideal[(grid - 1) * 2 + 1]
        for dx in (-5, 0, 5):
            for dy in (-5, 0, 5):
                assert captcha_server.grid_of_point(cx + dx, cy + dy) == grid, (grid, dx, dy)


def test_grid_math_rejects_points_outside_the_grid():
    """格网之外必须返回 ``None`` —— 不许硬塞进 1..8。

    硬塞会让一个误检的框变成一个**看起来合法**的格子编号，
    再被上游换算成一个错误但合理的点击坐标。
    """
    cell = captcha_server.CELL
    outside = [
        captcha_server.grid_of_point(-10, 30),
        captcha_server.grid_of_point(30, -10),
        captcha_server.grid_of_point(cell * 4 + 5, 30),
        captcha_server.grid_of_point(30, cell * 2 + 5),
    ]
    assert outside == [None, None, None, None], outside


def test_rects_are_mapped_by_center_point():
    """按中心点判格：检测框紧贴字符时，左上角容易被格线切到隔壁。"""
    rects = [(220, 20, 300, 60), (10, 10, 40, 40)]
    assert captcha_server.grids_from_rects(rects) == [1, 4]


def test_format_positions_is_the_only_place_that_joins():
    """拼接格式只此一处：逗号 + x,y 交替，偶数个数字。"""
    text = format_positions([38, 38, 257, 36])
    assert text == "38,38,257,36"
    parts = text.split(",")
    assert len(parts) % 2 == 0, "坐标必须是 x,y 成对出现"


# --------------------------------------------------------------------------- #
# 3. 上游后端：请求真的发出去 + 坐标真的来自上游
# --------------------------------------------------------------------------- #
def test_upstream_backend_sends_the_upstream_field_name():
    """**这一条直接针对「假实现」**。

    上游 ``OCR.get_image_by_free_site`` 发的字段是 ``img``；
    老实现发的是 ``image``。字段名不同 = 服务端拿不到图 = 必然识别失败，
    而调用方只会看到一句「识别失败」。所以这里断言的是**请求体里的字段名与内容**。
    """
    image = bytes(range(256)) * 4
    with EchoServer() as echo:
        echo.respond({"msg": "success", "result": [1, 4]})
        solver = PjialinCaptchaSolver(echo.url("/check"))
        assert solver.available() is True
        got = solver.solve(image, kind="login")

        assert not echo.empty, "请求**根本没有发出去** —— 这正是当年那个假实现的样子"
        record = echo.records[-1]
        assert record.method == "POST"
        assert record.path == "/check"
        fields = dict(record.form)
        assert sorted(fields) == ["img"], f"字段名必须是上游的 img，实际 {sorted(fields)}"
        assert base64.b64decode(fields["img"]) == image, "请求里的图片与传入的不一致"
        # 返回必须是**坐标串**（网格 1 与 4 → 4 个数字），而不是「随便一个非空」。
        parts = str(got).split(",")
        assert len(parts) == 4, f"{got!r} / {solver.last_error}"
        grids = [captcha_server.grid_of_point(int(parts[i]), int(parts[i + 1])) for i in (0, 2)]
        assert grids == [1, 4], grids


def test_upstream_backend_coordinates_come_from_upstream_code():
    """坐标必须能由**上游的换算函数**逐字节复现。

    若有人把换算改成本地实现，抖动序列就会不同 → 这条会变红。
    """
    from ticket_helper.kernel.pjialin import shim

    shim.install()
    from py12306.helpers.OCR import OCR  # noqa: PLC0415

    with EchoServer() as echo:
        echo.respond({"msg": "success", "result": [1, 4, 8]})
        solver = PjialinCaptchaSolver(echo.url("/check"))

        random.seed(20260930)
        got = solver.solve(b"\x89PNG-fake", kind="login")
        random.seed(20260930)
        expected = format_positions(OCR().get_image_position_by_offset([1, 4, 8]))

        assert got == expected, f"{got!r} != {expected!r}"

        # 再独立验一遍语义：每个点都要落在它编号对应的格子里（不依赖随机序列）
        pairs = [int(v) for v in str(got).split(",")]
        assert len(pairs) == 6
        grids = [captcha_server.grid_of_point(pairs[i], pairs[i + 1]) for i in (0, 2, 4)]
        assert grids == [1, 4, 8]


def test_upstream_backend_refuses_character_captcha_with_a_reason():
    """字符型（下单）上游没有实现，必须**如实降级并说明**，而不是猜一个答案。"""
    solver = PjialinCaptchaSolver("http://127.0.0.1:1/check")
    _assert_solve_fails(solver, "order", "点击型", why="字符型必须如实降级")
    assert "order" in solver.last_error
    assert not solver.last_error.startswith("调用上游打码失败")


def test_upstream_backend_without_endpoint_explains_why():
    """没配地址要说清「为什么不可用」以及**怎么解决**。"""
    solver = PjialinCaptchaSolver("")
    assert solver.available() is False
    assert solver.last_error != "", "不可用时必须说明原因"
    # 连「怎么解决」也要说清：能自己修的问题不该只报一句「不可用」
    assert "captcha_server" in solver.last_error


def test_unreachable_service_still_leaves_a_readable_reason():
    """服务不可达时，失败必须留下**可读且带地址**的原因。

    ⚠️ 这里记录上游的一个真实性质：**它的失败路径不确定**。
    `Request.request()` 有时把连接失败吞成空响应（我们这边表现为
    「未返回可用坐标」），有时又把 `JSONDecodeError` 抛出来传给我们。
    所以这条用例**不断言走哪条路径**，只断言「不管走哪条，报错都同样有用」：
    带上地址 + 给出自检指令。否则同一件事会有两种质量的提示，
    看起来像两个不同的故障 —— 这正是本用例要防的。

    ⚠️ 另一个真实缺口：我们**区分不出**「不可达」与「协议不对」
    （连接失败被上游吞掉了），所以报错里如实说「可能」，而不是猜一个原因。
    """
    solver = PjialinCaptchaSolver("http://127.0.0.1:9/check", timeout=0.5)
    answer = solver.solve(b"\x89PNG", kind="login")
    assert answer is None, f"应返回 None，实际 {answer!r}"  # noqa: WEAK-ASSERT - None 是契约规定的失败返回值

    assert solver.last_error != "", "传输失败不能没有痕迹（老实现吞在 log.debug）"
    assert "http://127.0.0.1:9/check" in solver.last_error, (
        f"报错必须带上地址，否则不知道查哪个 -> {solver.last_error!r}"
    )
    assert "captcha_server" in solver.last_error, (
        f"报错必须给出自检指令 -> {solver.last_error!r}"
    )


# --------------------------------------------------------------------------- #
# 4. self 后端（回退）：协议不同必须说清
# --------------------------------------------------------------------------- #
def test_self_backend_uses_its_own_protocol():
    """``self`` 后端按本项目约定（字段 ``image`` + ``{"answer": "x,y"}``）。"""
    with EchoServer() as echo:
        echo.respond({"answer": "38,38,257,36"})
        solver = SelfCaptchaSolver(echo.url("/check"))
        assert solver.solve(b"\x89PNG", kind="login") == "38,38,257,36"
        assert sorted(dict(echo.records[-1].form)) == ["image", "kind"]


def test_self_backend_reports_protocol_mismatch_instead_of_failing_silently():
    """服务端按上游协议应答时，``self`` 后端必须指出**协议对不上**并给出改法。

    这条正是「老实现配了也用不了」的现场记录：不指出来的话，
    使用者只会看到「验证码识别失败」。
    """
    with EchoServer() as echo:
        echo.respond({"msg": "success", "result": [1, 4]})
        solver = SelfCaptchaSolver(echo.url("/check"))
        _assert_solve_fails(solver, "login", "格子编号", why="服务端用的是上游协议")
        assert f"{DOMAIN}={PJIALIN_NAME}" in solver.last_error, solver.last_error


# --------------------------------------------------------------------------- #
# 5. 本机打码服务：端到端
# --------------------------------------------------------------------------- #
@pytest.fixture()
def local_service(monkeypatch):
    """起一个真实的本机打码服务（端口 0 由系统分配），识别结果用替身固定。

    为什么替身识别而不是真跑 ddddocr：**被测对象是「协议与换算」，不是模型精度**。
    模型精度无法用断言表达（它本来就不准），而协议错了却会被完全掩盖。
    """
    monkeypatch.setattr(captcha_server, "recognize_grids", lambda image: [1, 4, 8])
    server, _thread = captcha_server.serve_in_background(host="127.0.0.1", port=0)
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_local_service_answers_the_upstream_protocol(local_service):
    """本机服务必须按上游协议应答：``{"msg":"success","result":[编号]}``。"""
    body = urllib.parse.urlencode({"img": base64.b64encode(b"\x89PNG").decode()}).encode()
    request = urllib.request.Request(f"{local_service}/check", data=body)
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = __import__("json").loads(response.read().decode("utf-8"))
    assert payload == {"msg": "success", "result": [1, 4, 8]}


def test_local_service_end_to_end_gives_upstream_coordinates(local_service):
    """★ 端到端：本机服务 → 上游后端 → 上游换算 → 坐标串。

    这条把「自建服务」和「上游代码」串起来，是本次打码改造的总验收。
    """
    solver = PjialinCaptchaSolver(f"{local_service}/check")
    answer = solver.solve(b"\x89PNG-fake", kind="login")
    pairs = [int(v) for v in str(answer).split(",")]
    assert len(pairs) == 6, f"{answer!r} / {solver.last_error}"
    grids = [captcha_server.grid_of_point(pairs[i], pairs[i + 1]) for i in (0, 2, 4)]
    assert grids == [1, 4, 8]


def test_local_service_rejects_the_wrong_field_name(local_service):
    """防的是：字段名写错却静默「成功」—— 服务必须明确报错。

    老实现发 ``image``，服务端如果只认 ``img``，就会拿不到图。
    这条断言的是**错误必须被说出来**，而不是「返回了 fail」。
    """
    body = urllib.parse.urlencode({"image": "AAAA"}).encode()
    request = urllib.request.Request(f"{local_service}/check", data=body)
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = __import__("json").loads(response.read().decode("utf-8"))
    assert payload["msg"] == "fail"
    assert "img" in payload.get("error", ""), payload


def test_local_service_reports_fail_when_recognition_is_unavailable(monkeypatch):
    """识别不出来时必须回 ``fail``，**绝不返回猜的编号**。

    猜的编号会被上游换算成一个「看起来合法」的点击坐标 ——
    使用者只会以为「模型不准」，而真正的原因（服务没识别出来）永远查不到。
    """
    monkeypatch.setattr(captcha_server, "recognize_grids", lambda image: None)
    server, _thread = captcha_server.serve_in_background(host="127.0.0.1", port=0)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        body = urllib.parse.urlencode({"img": base64.b64encode(b"\x89PNG").decode()}).encode()
        with urllib.request.urlopen(
            urllib.request.Request(f"{base}/check", data=body), timeout=10
        ) as response:
            payload = __import__("json").loads(response.read().decode("utf-8"))
        assert payload == {"msg": "fail"}
    finally:
        server.shutdown()
        server.server_close()


def test_local_service_rejects_a_non_base64_body(local_service):
    """坏输入要给出可读原因，而不是抛 500（500 会变成「服务坏了」的误判）。"""
    body = urllib.parse.urlencode({"img": "!!!not-base64!!!"}).encode()
    request = urllib.request.Request(f"{local_service}/check", data=body)
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = __import__("json").loads(response.read().decode("utf-8"))
    assert payload["msg"] == "fail"
    assert "base64" in payload.get("error", ""), payload


def test_local_service_rejects_unknown_path(local_service):
    """路径不对要说清「应该是哪条」，否则排障只能靠猜。"""
    body = urllib.parse.urlencode({"img": "AAAA"}).encode()
    request = urllib.request.Request(f"{local_service}/wrong", data=body)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 404
    payload = __import__("json").loads(exc.value.read().decode("utf-8"))
    assert "/check" in payload.get("error", ""), payload


# --------------------------------------------------------------------------- #
# 6. 装配：默认地址 + 热更新
# --------------------------------------------------------------------------- #
def test_default_endpoint_points_at_the_local_service(app_context):
    """``codec.user_endpoint`` 留空时，装配处必须解析成**本机服务**的默认地址。

    常量只在 :mod:`captcha_server` 定义一次；这条用例把「装配处用的是那个常量」
    变成可断言的事实，而不是靠读代码确认。
    """
    expected = (
        f"http://{captcha_server.DEFAULT_HOST}:{captcha_server.DEFAULT_PORT}"
        f"{captcha_server.DEFAULT_PATH}"
    )
    assert expected == "http://127.0.0.1:8899/check"
    assert app_context.verify_service.external.endpoint == expected


def test_composition_root_injects_the_upstream_solver(app_context):
    """防的是：装配处忘了注入 —— 于是线上跑的还是「自己那套」，没人会发现。"""
    service = app_context.verify_service
    assert service.external.name == PJIALIN_NAME
    names = [item["name"] for item in service.snapshot()["chain"]]
    assert PJIALIN_NAME in names, names


def test_hot_reload_replaces_the_solver_instance(app_context):
    """防的是：改了地址仍用旧实例（「配置生效了但没生效」）。

    ⚠️ 必须走**装配中心的热更新路径**（``update_config_section``），
    而不是直接调 ``verify_service.apply_config`` ——
    后者绕过了 `runtime/context.py`，于是「装配处忘了把新实现传进去」
    这个真 bug 完全测不到（变异检验抓过：它曾是绿的假测试）。
    """
    service = app_context.verify_service
    original = service.external
    app_context.update_config_section("codec", {"user_endpoint": "http://127.0.0.1:9/check"})

    assert service.external is not original, "热更新必须换掉实例，而不是只改配置对象"
    assert service.external.endpoint == "http://127.0.0.1:9/check"
