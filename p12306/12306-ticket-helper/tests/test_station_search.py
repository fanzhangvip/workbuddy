"""站点检索测试（任务配置页「出发站 / 到达站」选择器背后的能力）。

数据源是 :class:`StationMap`：内置离线常用站表 + 真机从 12306 官方
``station_name.js`` 拉取的全量表。这里验证的是**「检索好不好用」**：
排序是否把用户最想要的那个排在前面、空关键字有没有兜底、电报码能不能搜。
"""

from __future__ import annotations

from ticket_helper.inter.station import HOT_STATIONS


def _names(result: list[tuple[str, str]]) -> list[str]:
    return [name for name, _code in result]


def test_exact_match_comes_first(station_map):
    """输入「北京」时，第一条必须是「北京」本身，而不是「北京南」。"""
    result = station_map.search("北京")
    assert result[0] == ("北京", "BJP")


def test_prefix_matches_come_before_substring_matches(station_map):
    """前缀匹配必须整体排在「仅包含」之前，否则选站时会在一堆无关站里翻。"""
    names = _names(station_map.search("广州"))
    assert names[0] == "广州"
    assert "广州南" in names
    # 离线表里「广州」系都是前缀匹配，这里确认没有混入「仅包含」的站
    assert all(name.startswith("广州") for name in names)


def test_search_returns_station_code(station_map):
    """返回电报码才能让上层确认选到的确实是官方那个站。"""
    mapping = dict(station_map.search("长沙南"))
    assert mapping["长沙南"] == "CWQ"


def test_search_by_telegraph_code(station_map):
    """有时人手里只有电报码，也要能搜出来。"""
    mapping = dict(station_map.search("GZQ"))
    assert mapping.get("广州") == "GZQ"


def test_empty_keyword_returns_hot_stations(station_map):
    """点开选择器就该看到常用站，而不是一片空白逼着人自己敲。"""
    result = station_map.search("")
    assert len(result) > 10
    names = _names(result)
    assert names[:3] == ["北京南", "北京西", "北京"], names[:3]
    # 站点表里没有的常用站要被跳过（否则会冒出一堆点不了的空名）
    assert all(station_map.has(name) for name in names)


def test_hot_stations_are_all_valid_names(station_map):
    """常用站清单里的名字必须都是站点表里的真实站名 —— 写错一个就等于少一个入口。"""
    missing = [name for name in HOT_STATIONS if not station_map.has(name)]
    assert missing == [], f"常用站清单里有站点表不存在的名字: {missing}"


def test_limit_is_respected(station_map):
    assert len(station_map.search("", limit=5)) == 5
    assert len(station_map.search("北", limit=3)) == 3


def test_unknown_keyword_returns_empty(station_map):
    """搜不到就老实返回空，让界面能提示「换个关键字」，而不是硬塞几条。"""
    assert station_map.search("火星站") == []
    assert station_map.search("zzzzz") == []
