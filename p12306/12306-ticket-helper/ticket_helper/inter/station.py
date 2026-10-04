"""站名 ↔ 电报码映射（QY-T1）。

三级数据源（优先级从高到低）：
1. 用户目录 ``<home>/data/station_name.txt``（可手工补全 / 更新）
2. 包内 ``resources/station_name.txt``（离线兜底）
3. 联网从 12306 官方 ``station_name.js`` 拉取并缓存到用户目录（权威数据）

解析函数对未知站名**明确报错**而不是静默失败——静默失败会导致查询参数错误，
最终表现为「查不到票」，极难排查。
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from ticket_helper.errors import ValidationError
from ticket_helper.runtime.paths import AppPaths, get_app_paths

log = logging.getLogger(__name__)

#: 官方 station_name.js 形如 @bjb|北京北|VAP|beijingbei|bjb|0
_JS_ENTRY = re.compile(r"@([a-z]+)\|([^|]+)\|([A-Z]+)\|")

#: 空关键字时给出的「常用站」。
#: 为什么要这一份：任务配置页的站点选择器如果点开是一片空白，
#: 使用者只能靠自己把站名一个字符一个字符敲进去 —— 那就退化回「手填」了。
#: 这里按城市给一组主要车站，点开即可选；站名不一致的会被自动跳过
#: （tests/test_station_search.py 会盯着「清单里的名字在站点表里存不存在」，
#: 免得写错一个站名就永久少一个入口）。
HOT_STATIONS: tuple[str, ...] = (
    "北京南", "北京西", "北京", "北京北",
    "上海虹桥", "上海", "上海南",
    "广州南", "广州东", "广州", "深圳北", "深圳",
    "杭州东", "杭州", "南京南", "南京",
    "天津", "天津西", "石家庄", "太原南", "太原",
    "郑州东", "郑州", "武汉", "汉口", "武昌",
    "长沙南", "长沙", "南昌西", "南昌", "合肥南", "合肥",
    "西安北", "西安", "兰州西", "兰州", "西宁", "银川", "乌鲁木齐",
    "成都东", "成都", "重庆北", "重庆西", "重庆", "贵阳北", "贵阳",
    "昆明南", "昆明", "南宁东", "南宁", "海口东", "海口", "三亚",
    "济南西", "济南", "青岛", "青岛北",
    "沈阳北", "沈阳", "大连北", "大连", "长春", "哈尔滨西", "哈尔滨",
    "福州南", "福州", "厦门北", "厦门", "呼和浩特", "拉萨", "香港西九龙",
)


class StationMap:
    """站名 → 电报码，双向可查。"""

    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or get_app_paths()
        self._name_to_code: dict[str, str] = {}
        self._code_to_name: dict[str, str] = {}
        self._lock = threading.RLock()
        self._loaded_sources: list[str] = []

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def load(self, *, allow_remote: bool = True, client=None) -> "StationMap":
        self._load_file(self.paths.station_file, "bundled" if self.paths.station_file.exists() else "none")

        cache = self.paths.data_dir / "station_name_official.txt"
        if cache.exists():
            self._load_file(cache, "cache")
        elif allow_remote:
            try:
                self._fetch_official(client)
            except Exception as exc:  # noqa: BLE001 - 拉取失败不影响离线使用
                log.warning("拉取官方站名表失败，继续使用内置表: %s", exc)

        if not self._name_to_code:
            raise ValidationError("站点表为空，无法进行查询；请检查 resources/station_name.txt")
        log.info("站点表加载完成: %d 个站点 (来源: %s)", len(self._name_to_code), ",".join(self._loaded_sources))
        return self

    def _load_file(self, path: Path, source: str) -> None:
        if not path.exists():
            return
        count = 0
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            name, _, code = line.partition("|")
            name, code = name.strip(), code.strip().upper()
            if not name or not code:
                continue
            with self._lock:
                if code in self._code_to_name and self._code_to_name[code] != name:
                    # 电报码冲突：保留先出现的，避免错误码覆盖正确码
                    log.debug("站点表电报码冲突，忽略: %s -> %s", name, code)
                    continue
                self._name_to_code[name] = code
                self._code_to_name[code] = name
            count += 1
        if count:
            self._loaded_sources.append(f"{source}:{count}")

    def _fetch_official(self, client) -> None:
        from ticket_helper.inter import urls

        owns_client = client is None
        if owns_client:
            from ticket_helper.inter.base import HttpClient

            client = HttpClient(base_url=urls.BASE_OFFICIAL, timeout=15.0)
        try:
            response = client.get(urls.STATION_NAME_JS, allow_retry=True)
            if not response.ok or "|" not in response.text:
                return
            entries = _JS_ENTRY.findall(response.text)
            if len(entries) < 1000:
                return
            cache = self.paths.data_dir / "station_name_official.txt"
            cache.parent.mkdir(parents=True, exist_ok=True)
            lines = [f"{name}|{code}" for _py, name, code in entries]
            cache.write_text(
                "# 由 12306 官方 station_name.js 自动生成，可删除以重新拉取\n" + "\n".join(lines),
                encoding="utf-8",
            )
            self._load_file(cache, "official")
        finally:
            if owns_client:
                client.close()

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def code_of(self, station_name: str) -> str:
        name = (station_name or "").strip()
        if not name:
            raise ValidationError("站点名称不能为空")
        with self._lock:
            code = self._name_to_code.get(name)
        if code:
            return code
        # 容错：用户可能输入「北京南站」
        if name.endswith("站"):
            with self._lock:
                code = self._name_to_code.get(name[:-1])
            if code:
                return code
        # 容错：可能是直接输入的电报码
        if name.isalpha() and name.upper() in self._code_to_name:
            return name.upper()
        raise ValidationError(f"未知站点: {name}（请检查站名或补全 station_name.txt）")

    def name_of(self, code: str) -> str:
        with self._lock:
            return self._code_to_name.get((code or "").upper(), code or "")

    def codes_of(self, names: list[str]) -> list[str]:
        return [self.code_of(n) for n in names]

    def names_of(self, codes: list[str]) -> list[str]:
        return [self.name_of(c) for c in codes]

    def has(self, station_name: str) -> bool:
        try:
            self.code_of(station_name)
            return True
        except ValidationError:
            return False

    # ------------------------------------------------------------------ #
    # 检索（任务配置页的站点选择器用它，避免使用者手敲站名敲错一个字）
    # ------------------------------------------------------------------ #
    def search(self, keyword: str = "", *, limit: int = 60) -> list[tuple[str, str]]:
        """按关键字检索站点，返回 ``[(站名, 电报码)]``。

        排序按「越可能是用户想要的越靠前」：完全相等 → 前缀 → 包含 → 电报码前缀。
        不这么排的话，输入「广州」时「广州南」可能与「广州北」「广州东」随机混在一起，
        而使用者最常想要的恰恰是那个完全匹配或者前缀最短的。

        ``keyword`` 为空时返回 :data:`HOT_STATIONS`（点开即见常用站，
        而不是一片空白逼着人自己敲）。
        """
        kw = (keyword or "").strip()
        limit = max(1, int(limit))
        if not kw:
            return self._hot_stations(limit)

        with self._lock:
            items = list(self._name_to_code.items())
        upper = kw.upper()
        # 四档分开收集：同档内按站名字典序，档与档之间严格分层
        buckets: list[list[tuple[str, str]]] = [[], [], [], []]
        for name, code in items:
            if name == kw:
                buckets[0].append((name, code))
            elif name.startswith(kw):
                buckets[1].append((name, code))
            elif kw in name:
                buckets[2].append((name, code))
            elif code.startswith(upper):
                buckets[3].append((name, code))

        result: list[tuple[str, str]] = []
        for bucket in buckets:
            result.extend(sorted(bucket))
            if len(result) >= limit:
                break
        return result[:limit]

    def _hot_stations(self, limit: int) -> list[tuple[str, str]]:
        with self._lock:
            mapping = dict(self._name_to_code)
        result = [(name, mapping[name]) for name in HOT_STATIONS if name in mapping]
        if result:
            return result[:limit]
        # 站点表里一个常用站都没有（例如换成了自定义的小表）→ 退化成字典序前 N 个
        return sorted(mapping.items())[:limit]

    def __len__(self) -> int:
        return len(self._name_to_code)


_station_map: StationMap | None = None
_station_lock = threading.Lock()


def get_station_map(paths: AppPaths | None = None, *, refresh: bool = False) -> StationMap:
    """获取全局站点表（单例）。"""
    global _station_map
    with _station_lock:
        if _station_map is None or refresh:
            _station_map = StationMap(paths).load()
        return _station_map
