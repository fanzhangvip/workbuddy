"""站点表域的内核契约测试（V4 差分 + V2 契约）。

**本用例最重要的产出：它否决了「站点表默认切上游」这个看起来顺理成章的决定。**

差分结果（实测，非估计）：

| 项 | self | pjialin（上游 2021 快照） |
|---|---|---|
| 站名数 | 3364 | 3319 |
| 交集 | — | **3148** |
| 只有上游有 | — | 171（切换后**新出现**） |
| 只有我们有 | 216 | —（切换后**消失**） |
| **交集内电报码不同的站** | — | **11 个** ← 最危险 |

那 11 个是**同一站名对应不同电报码**，例：信阳 `XYN`/`XUN`、抚顺 `FET`/`FST`、
乌海 `IAC`/`WVC`、天门 `TKN`/`TMN`。

⇒ 上游是 2021 年的快照，**官方电报码后来改过**。
切到上游会给这 11 个站发出**错误的电报码** → 查不到车 / 下单失败，
而且现象是「查询没结果」，极难联想到是站表过期。

所以：**默认仍是 `self`**（`registry.DEFAULT_BACKENDS`），且本用例把
「这 11 个」钉成**已知分歧清单** —— 官方改一次码，测试就红一次，
逼我们重新评估，而不是静默发错。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from ticket_helper.errors import ValidationError  # noqa: E402
from ticket_helper.kernel import registry  # noqa: E402
from ticket_helper.kernel.pjialin.station import PjialinStationTable  # noqa: E402
from ticket_helper.kernel.self.station import SelfStationTable  # noqa: E402

#: **已知分歧**：站名在两边都有，但电报码不同（上游 2021 快照 vs 官方现值）。
#: 「站名 → (本项目码, 上游码)」
KNOWN_TELECODE_DIVERGENCE: dict[str, tuple[str, str]] = {
    "乌拉特前旗": ("IQC", "WQC"),
    "乌海": ("IAC", "WVC"),
    "乌鲁木齐南": ("WNR", "WMR"),
    "五原": ("IYC", "WYC"),
    "信阳": ("XYN", "XUN"),
    "天门": ("TKN", "TMN"),
    "宜城": ("YYN", "YIN"),
    "抚顺": ("FET", "FST"),
    "烟台西": ("DCK", "YTK"),
    "石嘴山": ("OZJ", "QQJ"),
    "黄陵": ("KLY", "ULY"),
}


@pytest.fixture(scope="module")
def both() -> tuple[SelfStationTable, PjialinStationTable]:
    return SelfStationTable(), PjialinStationTable()


def _code(table, name: str) -> str:
    try:
        return table.code_of(name)
    except ValidationError as exc:
        return f"ERR:{exc}"


# --------------------------------------------------------------------------- #
# V2 契约：两个后端必须可替换
# --------------------------------------------------------------------------- #
def test_boundary_cases_behave_identically(both):
    """防的是：容错规则在两个后端悄悄分叉。

    例：「北京南站」（末位多一个「站」）在一个后端能用、另一个不能用 ——
    换后端后同一个输入时好时坏，是最难查的一类故障。
    """
    mine, up = both
    for case in ("北京南站", "北京北", "VAP", "vap", "不存在的站xyz", "", "   "):
        assert _code(mine, case) == _code(up, case), f"code_of({case!r}) 两个后端不一致"


def test_unknown_station_raises_in_both_backends(both):
    """防的是：把「查不到」做成返回空串 —— 那会拼出一个必然失败的请求。

    既有实现抛 `ValidationError`（带着人话提示），两个后端都必须抛。
    """
    mine, up = both
    for table in (mine, up):
        with pytest.raises(ValidationError) as exc:
            table.code_of("绝对不存在的站名zzz")
        assert "未知站点" in str(exc.value)
        with pytest.raises(ValidationError):
            table.code_of("")


def test_name_of_returns_the_input_when_unknown(both):
    """防的是：把「查不到」做成返回空串 —— 展示层会显示空白，而不是让使用者看见原始码。"""
    mine, up = both
    for table in (mine, up):
        assert table.name_of("ZZZZ") == "ZZZZ"
        assert table.name_of("VAP") == "北京北"


# --------------------------------------------------------------------------- #
# V4 差分：差异必须**正好**是已知那几项
# --------------------------------------------------------------------------- #
def test_telecode_divergence_is_exactly_the_known_set(both):
    """防的是：**静默发错电报码**。

    站名相同、电报码不同 ⇒ 请求里的 `leftTicketDTO.from_station` 就是错的，
    现象是「查不到车」，而没人会想到是站表过期。

    判据：
    ① 已知分歧清单里的每一项，两边码必须**仍然**是记录的那两个（否则说明有人改了一边）；
    ② 交集里不允许出现**清单之外**的新分歧。
    """
    mine, up = both
    common = set(mine._table._name_to_code) & {n for n, _ in up._pairs}  # noqa: SLF001

    # ① 清单本身仍然成立
    for name, (my_code, up_code) in KNOWN_TELECODE_DIVERGENCE.items():
        assert _code(mine, name) == my_code, f"{name} 本项目的码变了：{_code(mine, name)} != {my_code}"
        assert _code(up, name) == up_code, f"{name} 上游的码变了：{_code(up, name)} != {up_code}"

    # ② 不允许出现清单之外的新分歧
    unexpected = {
        name
        for name in common
        if _code(mine, name) != _code(up, name) and name not in KNOWN_TELECODE_DIVERGENCE
    }
    assert not unexpected, (
        "交集里出现了**文档之外**的电报码分歧（上游表可能变了，必须重新评估）：\n  "
        + "\n  ".join(f"{n}: {_code(mine, n)} vs {_code(up, n)}" for n in sorted(unexpected))
    )


def test_intersection_is_large_enough_to_be_meaningful(both):
    """防的是：表被换坏（只剩几个站）却没人发现 —— 差分要建立在足够大的交集上。"""
    mine, up = both
    common = set(mine._table._name_to_code) & {n for n, _ in up._pairs}  # noqa: SLF001
    assert len(common) >= 3000, f"两个表的交集只有 {len(common)} 个站，太小，差分结论不可信"


def test_known_set_difference_directions():
    """把「谁多谁少」也钉住 —— 切换会**新增 171 个、消失 216 个**站名。

    「消失」比「新增」危险：使用者原来能选的站，切了之后就选不到了。
    """
    mine, up = SelfStationTable(), PjialinStationTable()
    mset = set(mine._table._name_to_code)  # noqa: SLF001
    uset = {n for n, _ in up._pairs}
    only_up = uset - mset
    only_mine = mset - uset
    assert len(only_up) > 0 and len(only_mine) > 0, "两边应当各有对方没有的站"
    # 数量级合理即可（官方站表会更新，不钉死具体数字）
    assert 50 < len(only_up) < 1000, len(only_up)
    assert 50 < len(only_mine) < 1000, len(only_mine)


# --------------------------------------------------------------------------- #
# 门面：切换真的生效，且默认是有据可依的
# --------------------------------------------------------------------------- #
def test_switch_actually_changes_the_backend(monkeypatch):
    """防的是：开关写了但没接上 —— 「我切了」变成一句无法查证的猜测。"""
    monkeypatch.delenv(registry.ENV_VAR, raising=False)
    registry.reset_cache()
    assert registry.get("station").name == "self"

    monkeypatch.setenv(registry.ENV_VAR, "station=pjialin")
    registry.reset_cache()
    swapped = registry.get("station")
    assert swapped.name == "pjialin"
    assert isinstance(swapped, PjialinStationTable)
    assert len(swapped) > 0

    registry.reset_cache()


def test_default_stays_self_because_of_the_11_wrong_codes():
    """防的是：有人「顺手」把默认切到上游，把 11 个错电报码带上线。

    这条用例是**那个决策的守卫**：默认值只有在
    `KNOWN_TELECODE_DIVERGENCE` 清空之后才允许改。
    """
    assert registry.DEFAULT_BACKENDS["station"] == "self", (
        "站点表默认后端被改了 —— 但上游表里有 "
        f"{len(KNOWN_TELECODE_DIVERGENCE)} 个站的电报码是错的（2021 快照）。"
        "先把它们清掉（或确认官方已改回），再改默认值。"
    )


def test_invalid_backend_name_fails_fast(monkeypatch):
    """防的是：静默回退 —— 配错了却照跑，跑的是另一个后端。"""
    from ticket_helper.errors import ConfigError

    for bad in ("station=bogus", "station", "nosuchdomain=self"):
        monkeypatch.setenv(registry.ENV_VAR, bad)
        registry.reset_cache()
        with pytest.raises(ConfigError):
            registry.backend_name("station")
    monkeypatch.delenv(registry.ENV_VAR, raising=False)
    registry.reset_cache()


def test_search_returns_the_same_stations_but_may_order_them_differently(both):
    """**已知分歧**：检索的**成员**一致，**顺序**可能不同。

    原因：`self` 后端沿用既有 `StationMap.search` 的排序；`pjialin` 后端用共享的
    `rank_candidates`（上游没有检索功能，排序是本项目的逻辑）。
    两套排序对「同档内怎么排」的处理不同 —— 实测「广州」时
    `广州西` 与 `广州白云` 的相对位置就不一样。

    这不是 bug，是**切换会改变下拉框顺序**这件事必须被写下来。
    本用例只保证**不丢站、不多站**（那才是会让人查不到车的那种错）。
    """
    mine, up = both
    for keyword in ("广州", "北京", "上海", "长", "GZQ", "郑州"):
        my_hits = {name for name, _ in mine.search(keyword, limit=200)}
        up_hits = {name for name, _ in up.search(keyword, limit=200)}
        # 只比较两边都有的站（各表本身有增删，那是上一组用例管的事）
        common_tables = set(mine._table._name_to_code) & {n for n, _ in up._pairs}  # noqa: SLF001
        only_mine = {n for n in my_hits if n not in up_hits and n in common_tables}
        only_up = {n for n in up_hits if n not in my_hits and n in common_tables}
        # 允许因排序导致「谁挤进 limit 边界」的少量差异，但不允许大面积缺站
        assert len(only_mine) <= 2, f"{keyword}: self 独有 {sorted(only_mine)}"
        assert len(only_up) <= 2, f"{keyword}: pjialin 独有 {sorted(only_up)}"
