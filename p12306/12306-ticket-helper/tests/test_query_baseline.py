"""V5 参数级基线：用**上游原件**校验我们的余票字段下标与查询路径规则。

为什么这条基线值得单开一个文件
------------------------------
`docs/测试验证框架设计.md` 里 V5 只有「路径级」基线（`tests/test_protocol_baseline.py`），
**参数级是空缺** —— 而本项目出过的两次事故（405 路径整族写错、6 段乘客串）
都属于「参数/格式层面没人能判」。

字段下标是**协议事实**，不是实现选择：错一个下标不会报错，只会让
「历时」显示成「到达时间」这类**看着像数据的错**。所以它不该做成可切换的域
（那等于允许两套「事实」），而应该**拿上游当外部锚点来校验**。

上游锚点来自 `vendor/pjialin/ticket_facts.py` —— 由
`scripts/build_vendor_pjialin.py` 从 `py12306/query/job.py`、`query/query.py`
**逐字切出**（不是手抄），并可被 `--check` 重新生成比对。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

VENDOR = BASE / "vendor" / "pjialin"
# ⚠️ **append，绝不 insert(0)**：上游目录里也有一个 `main.py`，
# 插到本项目根前面会让 `import main` 拿到**上游入口**，报错却是
# `ModuleNotFoundError: flask_jwt_extended`（上游 web 的依赖），
# 与「谁抢了 main」完全对不上 —— 而且只在**特定文件组合**下才现形
# （本文件是模块级插入，进过 pytest 收集阶段就一直留在 sys.path 上）。
# 同一条规则见 `ticket_helper/kernel/pjialin/shim.py`（那边也是 append）。
if str(VENDOR) not in sys.path:
    sys.path.append(str(VENDOR))

from ticket_facts import (  # noqa: E402  （生成物：上游协议事实）
    INDEX_ARRIVE_STATION,
    INDEX_ARRIVE_TIME,
    INDEX_LEFT_DATE,
    INDEX_LEFT_STATION,
    INDEX_LEFT_TIME,
    INDEX_ORDER_TEXT,
    INDEX_SECRET_STR,
    INDEX_TICKET_NUM,
    INDEX_TRAIN_NO,
    INDEX_TRAIN_NUMBER,
    ROUTE_PATTERN,
)
from ticket_helper.inter import parser as parser_mod  # noqa: E402
from ticket_helper.inter import urls as urls_mod  # noqa: E402

#: 语义（我们的字段名）→ (我们的常量名, 上游的常量名)。
#:
#: **这张表本身就是要被校验的「契约」**：两边变量名不同是正常的
#: （它们各自反映了不同年代的命名习惯），但**下标必须相同**。
FIELD_INDEX_CONTRACT: dict[str, tuple[str, str]] = {
    "secret_str": ("IDX_SECRET_STR", "INDEX_SECRET_STR"),
    "button_text": ("IDX_BUTTON_TEXT", "INDEX_ORDER_TEXT"),
    "train_no": ("IDX_TRAIN_NO", "INDEX_TRAIN_NO"),
    "train_code": ("IDX_TRAIN_CODE", "INDEX_TRAIN_NUMBER"),
    "from_station_code": ("IDX_FROM_STATION_CODE", "INDEX_LEFT_STATION"),
    "to_station_code": ("IDX_TO_STATION_CODE", "INDEX_ARRIVE_STATION"),
    "start_time": ("IDX_START_TIME", "INDEX_LEFT_TIME"),
    "arrive_time": ("IDX_ARRIVE_TIME", "INDEX_ARRIVE_TIME"),
    # ⚠️ 上游那个名字（TICKET_NUM =「余票数」）与它**实际的用法**不一致，见下一条用例。
    #    这里按**语义**对齐：两边都是「可否网订」，取值都是下标 11。
    "can_web_buy": ("IDX_CAN_WEB_BUY", "INDEX_TICKET_NUM"),
    "train_date": ("IDX_START_TRAIN_DATE", "INDEX_LEFT_DATE"),
}


def test_our_field_indexes_match_upstream():
    """★ 本项目用的字段下标必须与**上游**一致。

    这是 V5「参数级基线」的落地：每条下标都有外部出处，
    而不是「我们一直是这么写的」。

    改动 `ticket_helper/inter/parser.py` 里任一 `IDX_*` 都会让这条变红 ——
    这正是我们要的：下标错了不会报错，只会让字段**串位**。
    """
    mismatches: list[str] = []
    for field, (our_name, up_name) in FIELD_INDEX_CONTRACT.items():
        ours = getattr(parser_mod, our_name)
        theirs = {
            "INDEX_SECRET_STR": INDEX_SECRET_STR,
            "INDEX_ORDER_TEXT": INDEX_ORDER_TEXT,
            "INDEX_TRAIN_NO": INDEX_TRAIN_NO,
            "INDEX_TRAIN_NUMBER": INDEX_TRAIN_NUMBER,
            "INDEX_LEFT_STATION": INDEX_LEFT_STATION,
            "INDEX_ARRIVE_STATION": INDEX_ARRIVE_STATION,
            "INDEX_LEFT_TIME": INDEX_LEFT_TIME,
            "INDEX_ARRIVE_TIME": INDEX_ARRIVE_TIME,
            "INDEX_TICKET_NUM": INDEX_TICKET_NUM,
            "INDEX_LEFT_DATE": INDEX_LEFT_DATE,
        }[up_name]
        if ours != theirs:
            mismatches.append(f"{field}: {our_name}={ours} 而 {up_name}={theirs}")
    assert not mismatches, "字段下标与上游不一致：\n  " + "\n  ".join(mismatches)


def test_the_contract_table_covers_every_shared_field():
    """防的是：契约表漏项 —— 少一项就少一条基线，而没人会发现少了。

    做法：把两边**同值**的常量都找出来，要求每一个同值对都能在契约表里对上号。
    """
    our_indexes = {
        name: value
        for name, value in vars(parser_mod).items()
        if name.startswith("IDX_") and isinstance(value, int)
    }
    up_indexes = {
        "INDEX_SECRET_STR": INDEX_SECRET_STR,
        "INDEX_ORDER_TEXT": INDEX_ORDER_TEXT,
        "INDEX_TRAIN_NO": INDEX_TRAIN_NO,
        "INDEX_TRAIN_NUMBER": INDEX_TRAIN_NUMBER,
        "INDEX_LEFT_STATION": INDEX_LEFT_STATION,
        "INDEX_ARRIVE_STATION": INDEX_ARRIVE_STATION,
        "INDEX_LEFT_TIME": INDEX_LEFT_TIME,
        "INDEX_ARRIVE_TIME": INDEX_ARRIVE_TIME,
        "INDEX_TICKET_NUM": INDEX_TICKET_NUM,
        "INDEX_LEFT_DATE": INDEX_LEFT_DATE,
    }
    contracted = {p for pair in FIELD_INDEX_CONTRACT.values() for p in pair}
    uncovered = [
        f"{up}（下标 {value}）在我们的表里没有对应项"
        for up, value in up_indexes.items()
        if up not in contracted
    ]
    assert not uncovered, "\n  ".join(uncovered)
    # 契约表里提到的常量必须真的存在于我们的 parser（防止写错名字后静默跳过）
    missing = [name for name in our_indexes if name in contracted and name not in our_indexes]
    assert not missing, missing


def test_upstream_name_is_misleading_but_its_usage_matches_ours():
    """★ 记录一个**真实的命名陷阱**：上游 `INDEX_TICKET_NUM` 名字是「余票数」，
    但它的用法是「== ``'Y'``」—— 也就是**可否网订**，与本项目的
    `IDX_CAN_WEB_BUY` 同义。

    为什么这条要写成测试而不是写进注释：如果我们哪天「按上游的名字」把
    `IDX_CAN_WEB_BUY` 改名成 `IDX_TICKET_NUM`，后续维护者很可能就真的
    去拿它当数量比较（``int(...)``）—— 那时会抛异常，或者更糟：把
    ``'Y'`` 当成真值算出荒谬的判断。**名字会骗人，取值不会。**

    断言分两跳（上游是 ``is_has_ticket → get_info_of_ticket_num → ticket_info[INDEX]``，
    中间隔了一层 getter，第一版测试没看出来，被自己的断言打回来了）：
    """
    assert INDEX_TICKET_NUM == parser_mod.IDX_CAN_WEB_BUY == 11

    source = (VENDOR / "py12306" / "query" / "job.py").read_text(encoding="utf-8")

    getter = re.search(r"def get_info_of_ticket_num\(self\):\s*\n\s*(.+)", source)
    assert "INDEX_TICKET_NUM" in (getter.group(1) if getter else ""), (
        "上游的 getter 不见了或不再用 INDEX_TICKET_NUM（上游结构变了？）"
    )

    checker = re.search(r"def is_has_ticket\(self, ticket_info\):\s*\n\s*(.+)", source)
    line = checker.group(1) if checker else ""
    assert "get_info_of_ticket_num()" in line, line or "上游 `is_has_ticket` 不见了"
    assert "== 'Y'" in line, (
        f"上游不再把它与 'Y' 比较（说明它其实是「余票数」吗？需要重新核对）：{line.strip()}"
    )


def test_length_check_covers_every_seat_index_we_read():
    """防的是：长度校验放行的行，读席别时越界。

    `parse_left_ticket_result` 先看 ``len(parts) >= MIN_RESULT_FIELDS``，
    然后按 ``SEAT_INDEX`` 取值（取之前还有一层 ``index >= len(parts)`` 兜底）。
    这里断言**长度校验本身就够**：``MIN_RESULT_FIELDS`` 必须严格大于最深的席别下标，
    否则那层兜底就从「保险」变成了**唯一**防线 ——
    而它的表现是「某几个席别静默没有数据」，不是报错。
    """
    deepest_seat = max(parser_mod.SEAT_INDEX.values())
    assert deepest_seat < parser_mod.MIN_RESULT_FIELDS, (
        f"最深席别下标 {deepest_seat} >= MIN_RESULT_FIELDS {parser_mod.MIN_RESULT_FIELDS}；"
        "长度校验已经挡不住越界了"
    )


def test_seat_index_table_is_sane():
    """席别下标表是「哪个下标是哪个席别」的**唯一使用处**，先钉住它的基本性质。

    ⚠️ 顺带记录一个已知重复（未修，见 `docs/缺陷回流台账.md`）：
    `parser.py` 里还有一组 ``IDX_RW_NUM`` / ``IDX_ZE_NUM`` … 常量**当前没有任何调用方**，
    它们按 12306 的**字母席别码**命名（``rw``/``ze``/``swz``…），
    与 ``SEAT_INDEX``（按中文席别名）描述**同一件事**。
    两套说法将来若分叉，表现是「界面有的席别查不到票」。
    这条用例只保证 `SEAT_INDEX` 自身可用，不去替它们编一张映射表
    （编错了只会得到一条**错的**契约）。
    """
    indexes = list(parser_mod.SEAT_INDEX.values())
    assert len(indexes) == len(set(indexes)), f"有重复下标：{indexes}"
    assert min(indexes) >= 0, indexes
    for name, index in parser_mod.SEAT_INDEX.items():
        assert isinstance(name, str) and name.strip() == name and name, repr(name)
        assert isinstance(index, int), (name, index)


def test_a_minimal_row_parses_without_raising():
    """行为层面的边界：字段数**刚好等于** `MIN_RESULT_FIELDS` 的行必须能解析。

    这条防的是「长度校验与下标不同步」：把 `MIN_RESULT_FIELDS` 改大或改小，
    都会在这里立刻现形（改大 → 合法行被拒；改小 → 越界）。
    """
    fields = [""] * parser_mod.MIN_RESULT_FIELDS
    fields[parser_mod.IDX_SECRET_STR] = "SECRET"
    fields[parser_mod.IDX_TRAIN_CODE] = "G1"
    fields[parser_mod.IDX_TRAIN_NO] = "5l000G10"
    fields[parser_mod.IDX_CAN_WEB_BUY] = "Y"
    fields[parser_mod.IDX_START_TRAIN_DATE] = "2026-10-01"
    ticket = parser_mod.parse_left_ticket_result("|".join(fields), train_date="2026-10-01")
    assert ticket.train_code == "G1"
    assert ticket.secret_str == "SECRET"
    assert ticket.train_date == "2026-10-01"


def test_a_short_row_is_rejected_with_a_readable_error():
    """字段数不足必须抛**我们自己的**类型化异常，而不是 ``IndexError``。

    ``IndexError`` 会被上层当成「解析器坏了」，而真相是「官方换了返回格式」——
    两种情况的处置完全不同。
    """
    short = "|".join([""] * (parser_mod.MIN_RESULT_FIELDS - 1))
    with pytest.raises(parser_mod.ResponseParseError) as exc:
        parser_mod.parse_left_ticket_result(short)
    assert "字段数" in str(exc.value), str(exc.value)


@pytest.mark.parametrize(
    "html_fragment,expected",
    [
        ("<script>var CLeftTicketUrl = 'leftTicket/queryZ';</script>", "leftTicket/queryZ"),
        ("var CLeftTicketUrl = 'leftTicket/queryA';", "leftTicket/queryA"),
        ("var CLeftTicketUrl = 'leftTicket/query';", "leftTicket/query"),
    ],
)
def test_upstream_route_regex_reads_the_path_from_the_page(html_fragment, expected):
    """上游读查询路径的办法：从余票页 HTML 里取 ``var CLeftTicketUrl``。

    这印证了「余票页必须先加载」不只是热身 —— **它还携带真正的查询路径**。
    本项目的 `ensure_session_warm()` 正是加载这张页面，却一直没读这个字段。
    """
    # 用 findall 一次拿到分组列表：既是具体值比较，也不会有「非 None」这种弱断言
    found = re.findall(ROUTE_PATTERN, html_fragment)
    assert found == [expected], f"上游正则取到的路径不对：{html_fragment} -> {found}"


def test_route_regex_yields_nothing_on_a_page_without_it():
    """页面里没有这个变量时**必须取不到**，不许返回一个瞎猜的路径。

    返回瞎猜的路径意味着我们会去请求一条不存在的地址，
    而症状是「查询失败」—— 与「被风控」长得一样。
    """
    assert not re.search(ROUTE_PATTERN, "<html><body>nothing here</body></html>")


def test_our_candidate_list_is_the_fallback_the_domain_depends_on():
    """防的是：兜底候选列表被清空 —— 页面取不到路径时就没有退路了。"""
    assert len(urls_mod.LEFT_TICKET_QUERY_CANDIDATES) >= 2, (
        "至少要有两条候选路径：页面解析失败时靠它兜底"
    )
    for path in urls_mod.LEFT_TICKET_QUERY_CANDIDATES:
        assert path.startswith("/otn/leftTicket/"), path
