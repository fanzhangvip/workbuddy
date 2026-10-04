"""站点表域的接口与共享排序。

**上游有什么、没有什么（必须写清）**
------------------------------------
`pjialin/py12306::helpers/station.py`（46 行）提供的是**表数据 + 两种查询**：

- ``stations``：每个元素 ``{key, name, pinyin, id}``（从 ``data/stations.txt`` 读）
- ``station_kvs``：``{站名: 电报码}``

它**没有**「按关键字模糊检索」——上游是配置驱动的应用，站名由使用者在 `env.py` 里写死，
不需要检索。所以：

======== ==========================  ====================================
能力     上游有吗                    实现来源
======== ==========================  ====================================
表数据   ✅ ``data/stations.txt``     **用上游**
站名→码  ✅ ``station_kvs``           **用上游**
码→站名  ❌（要自己反转）             本项目（一行反转，见适配层）
模糊检索 ❌                          本项目（:func:`rank_candidates`）
======== ==========================  ====================================

这一节必须写在代码里，否则「用上游」会退化成一个说不清范围的词。
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from ticket_helper.errors import ValidationError

#: 「检索结果」的形状：``(站名, 电报码)``。
#: 用元组而不是 dataclass，是为了**零成本兼容**既有的 ``StationMap.search`` 返回形状
#: （任务配置页的站点选择器已经在消费它）。
StationHit = tuple[str, str]


@runtime_checkable
class StationTable(Protocol):
    """站点表。两个后端都必须满足它，业务层只认它。

    ⚠️ **契约以既有实现为准，不是以"我觉得应该怎样"为准。**

    我最初把 ``code_of`` 写成「未知则返回空串、不要抛」—— 那是**错的**：
    实测 `StationMap.code_of` 对未知站点**抛 `ValidationError`**，
    而 ``name_of`` 在查不到时**返回传入的码本身**（不是空串）。
    按我写的文档实现上游适配器，两个后端就**不可替换**了 ——
    这正是「靠自己的想象定契约」的典型错误，记录在此以免重犯。
    """

    #: 后端名（必须与 `kernel/registry.py` 的注册键一致，由契约测试核对）
    name: str

    def search(self, keyword: str = "", *, limit: int = 60) -> list[StationHit]:
        """按关键字检索，返回 ``[(站名, 电报码)]``。

        ``keyword`` 为空时返回「常用站」（而不是空列表）——
        点开即见，不用人先敲字。
        ⚠️ 「常用站」是**本项目的概念**，上游没有；两个后端在这个分支上**必然不同**，
        差分表会把它列出来。
        """
        ...

    def code_of(self, station_name: str) -> str:
        """站名 → 电报码。**未知时抛 `ValidationError`**（不是返回空串）。

        容错规则（来自既有实现）：站名末位多一个「站」也算命中；
        直接传电报码也算命中。
        """
        ...

    def name_of(self, code: str) -> str:
        """电报码 → 站名。**查不到时返回传入的码本身**（不是空串）。"""
        ...

    def __len__(self) -> int:
        """表里有多少个站（用于差分与体检）。"""
        ...


def resolve_code(
    station_name: str,
    *,
    lookup: Callable[[str], str],
    code_exists: Callable[[str], bool],
) -> str:
    """站名 / 电报码 → 电报码（**共享的解析规则**，两个后端用同一套）。

    规则与错误文案**逐条对齐既有实现** `inter/station.py::StationMap.code_of`：

    1. 空 → ``站点名称不能为空``；
    2. 直接命中站名 → 返回；
    3. 末位多一个「站」→ 去掉再查（使用者常输入「北京南站」）；
    4. 本身就是已知电报码 → 原样返回（大写）；
    5. 否则 → ``未知站点: X（请检查站名或补全 station_name.txt）``。

    「两个后端语义必须一致」这件事由
    `tests/test_kernel_station.py` 用**全量交集比对 + 边界用例**守住 ——
    不是靠两份代码长得像。
    """
    name = (station_name or "").strip()
    if not name:
        raise ValidationError("站点名称不能为空")
    code = lookup(name)
    if code:
        return code
    if name.endswith("站"):
        code = lookup(name[:-1])
        if code:
            return code
    upper = name.upper()
    if name.isalpha() and code_exists(upper):
        return upper
    raise ValidationError(f"未知站点: {name}（请检查站名或补全 station_name.txt）")


def rank_candidates(items: list[StationHit], keyword: str, limit: int) -> list[StationHit]:
    """站名检索的排序（**单一实现，两个后端共用**）。

    排序语义（与既有的 ``StationMap.search`` 一致，不能随便改 —— 使用者已经在用）：
    完全相等 → 站名前缀 → 电报码前缀 → 站名包含 → 其它。

    为什么把它放在 Protocol 模块里而不是各自实现：
    上游**没有**检索（见模块 docstring），如果两个后端各写一份排序，
    差分表里出现的差异就全是「我们自己和自己不一样」，噪声淹掉真差异。
    """
    kw = (keyword or "").strip()
    limit = max(1, int(limit))
    if not kw:
        return items[:limit]

    upper = kw.upper()
    buckets: list[list[StationHit]] = [[], [], [], []]
    for name, code in items:
        if name == kw:
            buckets[0].append((name, code))
        elif name.startswith(kw):
            buckets[1].append((name, code))
        elif code.upper().startswith(upper):
            buckets[2].append((name, code))
        elif kw in name:
            buckets[3].append((name, code))

    out: list[StationHit] = []
    for bucket in buckets:
        bucket.sort(key=lambda pair: (len(pair[0]), pair[0]))
        out.extend(bucket)
        if len(out) >= limit:
            break
    return out[:limit]
