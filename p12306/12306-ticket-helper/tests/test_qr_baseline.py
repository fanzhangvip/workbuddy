"""V5 参数级基线（扫码域）：端点 / 状态码语义 / 请求体，逐项对上游出处。

锚点仍是 `vendor/pjialin/ticket_facts.py`（从上游原件逐字切出，`--check` 会重新生成比对）。

与另外两份基线的分工：
- `test_query_baseline.py` —— 余票字段下标
- `test_user_baseline.py` —— 用户域端点与响应字段路径
- 这一份 —— 扫码轮询（`create-qr64` / `checkqr`）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ticket_helper.inter import parser as parser_mod
from ticket_helper.inter import urls as urls_mod
from ticket_helper.kernel.pjialin.shim import install

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "pjialin"


@pytest.fixture(scope="module")
def facts():
    install()
    sys.path.insert(0, str(VENDOR))
    try:
        import ticket_facts  # type: ignore[import-not-found]

        yield ticket_facts
    finally:
        sys.path.remove(str(VENDOR))


# --------------------------------------------------------------------------- #
# 一、端点：两条都一致
# --------------------------------------------------------------------------- #
def test_qr_endpoints_match_upstream(facts) -> None:
    """两个扫码端点与上游**逐字一致**。

    与用户域不同，这里**没有差异** —— 所以本域可切的内容只剩「请求体怎么拼」。
    把「一致」写成断言的价值同前：改了一边没人会发现（而且改了还能跑）。
    """
    assert facts.QR_CREATE_PATH == urls_mod.CREATE_QR == "/passport/web/create-qr64"
    assert facts.QR_CHECK_PATH == urls_mod.CHECK_QR == "/passport/web/checkqr"


def test_appid_value_matches_upstream(facts) -> None:
    """``appid`` 取值两边一致（官方固定值 ``otn``）。

    这个值是**真机 trial 出来的关键参数**：少了它官方不报错，只稳定回
    ``{"result_message":"操作失败","result_code":9}`` —— 看起来像服务端故障，
    实际只是少个参数。所以它必须只有一个来源。
    """
    assert facts.QR_APPID == urls_mod.QR_APPID == urls_mod.QR_CHECK_CONSTANTS["appid"]


# --------------------------------------------------------------------------- #
# 二、result_code 语义：★ 本域最重要的一条对照
# --------------------------------------------------------------------------- #
def test_result_code_semantics_match_upstream(facts) -> None:
    """★ 官方用 ``result_code`` 的哪几个值表达扫码状态，两边必须**指向同一组数字**。

    上游是 ``if/elif result_code == N`` 四个分支：

    - ``0`` → 继续等（**待扫**）
    - ``1`` → 提示「请确认登录」（**已扫**）
    - ``2`` → ``break``（**成功**）
    - ``3`` → 删掉二维码重新下载（**过期**）

    本项目有一张自己的 ``QR_STATE_MAP``。两边如果指向不同的数字，
    后果是**状态判反**：把「已扫」当成「待扫」，用户会一直看到「请扫码」；
    把「成功」漏掉，登录就永远完不成 —— 而且不会有任何报错。
    """
    upstream_codes = {str(code) for code in facts.QR_RESULT_CODES}
    assert upstream_codes == {"0", "1", "2", "3"}
    assert set(parser_mod.QR_STATE_MAP) == upstream_codes, (
        "本项目的状态码集合与上游分支对不上 —— 官方改过语义，或我们漏/多了状态"
    )


@pytest.mark.parametrize(
    "code,expected_state",
    [
        ("0", parser_mod.QR_STATE_WAITING),
        ("1", parser_mod.QR_STATE_SCANNED),
        ("2", parser_mod.QR_STATE_CONFIRMED),
        ("3", parser_mod.QR_STATE_EXPIRED),
    ],
)
def test_each_code_maps_to_the_intended_state(code: str, expected_state: str) -> None:
    """逐个钉住「数字 → 语义」的对应关系。

    只断言「集合相同」是不够的 —— 把 1 和 2 的映射对调，集合仍然相同，
    但「已扫」和「成功」就反了。**这类错不会报错，只会让人一直扫不上。**
    """
    assert parser_mod.QR_STATE_MAP[code] == expected_state


def test_unrecognised_code_is_not_a_terminal_failure() -> None:
    """未识别的响应必须是 ``unknown``（**非终态**），不能并进 ``failed``。

    官方偶发返回错误信封（``{"result_message":"操作失败"}`` 而没有 result_code），
    此时二维码往往仍然有效 —— 判失败会让用户「还没扫就被通知失败」。
    """
    assert parser_mod.QR_STATE_UNKNOWN not in parser_mod.QR_STATE_MAP.values()
    state, _ticket, message = parser_mod.parse_qr_check({"result_message": "操作失败"})
    assert state == parser_mod.QR_STATE_UNKNOWN
    assert message != ""


# --------------------------------------------------------------------------- #
# 三、请求体：★ 唯一的差异
# --------------------------------------------------------------------------- #
def test_upstream_sends_device_fingerprint_cookies_we_do_not(facts) -> None:
    """★ 上游往表单里额外放两个**设备指纹 Cookie**；本项目不放。

    上游（``user/job.py::qr_login``）::

        data = {'RAIL_DEVICEID': ..., 'RAIL_EXPIRATION': ..., 'uuid': ..., 'appid': 'otn'}

    本项目只放 ``uuid`` / ``appid`` / ``_json_att``：那两条路径都真机跑通过，
    官方靠 Cookie 也能读到指纹。**差异留档在此**，并做成可切（`qr_check` 域），
    换默认值要有真机证据。

    这条断言防的正是「有人顺手把差异抹平」—— 差异一旦看不见，
    真机上切默认时才发现问题，那时已无从对比。
    """
    assert set(facts.QR_CHECK_FIELDS) == {
        "RAIL_DEVICEID",
        "RAIL_EXPIRATION",
        "uuid",
        "appid",
    }
    ours = set(urls_mod.QR_CHECK_FORM_KEYS)
    assert "RAIL_DEVICEID" not in ours, "本项目刻意不放设备指纹 —— 若改了，请同步本条与文档"
    assert "RAIL_EXPIRATION" not in ours
    assert {"uuid", "appid"} <= ours, "uuid/appid 两边都必须有"
