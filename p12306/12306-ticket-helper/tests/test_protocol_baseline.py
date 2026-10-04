"""协议基线：本项目用到的每一条 12306 接口路径，都要有**外部出处**或**写下来的理由**。

防的是哪一种真实故障
--------------------
2026-09-29：候补提交一直失败，日志只有一句「响应不是合法 JSON (HTTP 405)」。
真因是候补接口**路径整族写错** —— 写成了 `/otn/confirmPassenger/confirmHB`
（真机 405，该路径不存在），而正确家族是 `/otn/afterNate/`。
之所以能一路混到线上：**当时没有任何外部基线可以对**，路径是照直觉写的，
本地 mock 也照着同一个直觉实现，于是离线测试永远是绿的。

本文件做的就是那件当时缺的事：拿两个上游开源项目的接口表做基线
（`reference/baseline/upstream_endpoints.json`），
本项目 `ticket_helper/inter/urls.py` 里的**每一条路径**都必须落在下面三者之一：

1. **基线覆盖**：上游地址表里有这条路径（有出处）；
2. **显式归类**：在 `JUSTIFIED` 里写明「为什么上游没有」+ 证据类型（可机器检查）；
3. 否则 —— **测试失败**。

第 3 条是重点：它把「这条路径是我们猜的」变成一件**必须写下来**的事。
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.build_upstream_baseline import BASELINE_FILE, build  # noqa: E402

URLS_FILE = BASE / "ticket_helper" / "inter" / "urls.py"
#: 找引用时要扫的目录（`urls.py` 自身不算引用）
SCAN_DIRS = ("ticket_helper", "desktop_ui", "mock_server")

#: 证据类型的白名单。每一种都必须**可机器检查**，否则「理由」会退化成一句安慰。
EVIDENCE_KINDS = {
    # 真机实测过 —— 必须带日期
    "device": "真机实测过（必须带 YYYY-MM-DD）",
    # 只是 Referer / 浏览器跳转页，不是被程序调用的接口
    "referer": "只作为 Referer 或浏览器跳转页，不参与接口调用",
    # 官方静态资源
    "static": "官方静态资源（js/css/站点表），不走业务协议",
    # 不参与主流程，且调用点已做降级 —— 必须确实被引用（否则该用 unused）
    "degraded": "不在抢票主流程上，调用点已做「失败即忽略 / 返回空」的降级处理",
    # 定义了但没人用 —— 必须确实是 0 引用
    "unused": "定义在 urls.py 里但全仓库无任何引用（历史遗留常量）",
    # 上游用模板拼接 —— 该模板必须真的在基线里
    "upstream_templated": "上游用 /otn/{...} 模板拼接，本项目取的是它的一个具体取值",
}

#: 上游基线里没有、但本项目在用的路径。每条都必须给出 (证据类型, 理由, 日期)。
JUSTIFIED: dict[str, tuple[str, str, str]] = {
    # ---------- 真机实测过 ----------
    "/otn/afterNate/getQueueNum": (
        "device",
        "候补链路第 5 步（排队前置检查）。两个上游的地址表都没有这一条，"
        "但真机调用返回合法 JSON（status:true、data.flag=true），"
        "复核方式见 scripts/diagnose_standby.py。",
        "2026-09-29",
    ),
    "/otn/index/initMy12306Api": (
        "device",
        "个人中心账号信息。路径里带 init 但真机收 **POST**、返回 JSON；"
        "上游只列了页面 /otn/index/initMy12306（GET），没有这个 Api 后缀。",
        "2026-09-29",
    ),
    "/otn/queryOrder/queryMyOrder": (
        "device",
        "我的订单分页查询。真机已确认这里有个坑：分页从 **0** 开始（乘车人接口却是 1 开始），"
        "上游未列该接口。",
        "2026-09-29",
    ),
    "/otn/passengers/query": (
        "device",
        "常用联系人分页查询（1-based）。上游未列；它与下单链路用的 getPassengerDTOs 是"
        "两个不同接口（这条证件号打码、且不需要 REPEAT_SUBMIT_TOKEN）。",
        "2026-09-29",
    ),
    "/otn/leftTicket/queryZ": (
        "device",
        "余票查询首选路径。上游只用模板 /otn/{type} 拼（见下面几条），"
        "本项目的具体取值 queryZ 有真机实测（深圳→长沙南 返回 83 条车次）。",
        "2026-09-28",
    ),
    # ---------- 不在主流程、调用点已降级 ----------
    "/otn/leftTicket/queryA": (
        "degraded",
        "灰度轮换的兜底候选。上游的模板注释只确认过 leftTicket/queryX | leftTicket/queryZ，"
        "A/无后缀/O 是本项目额外容忍的取值；首选 queryZ 已真机验证，这几个只在前者打不通时才用到。",
        "",
    ),
    "/otn/leftTicket/query": (
        "degraded",
        "同上：灰度轮换候选之一，仅在首选路径不可用时才用到。",
        "",
    ),
    "/otn/leftTicket/queryO": (
        "degraded",
        "同上：灰度轮换候选之一，仅在首选路径不可用时才用到。",
        "",
    ),
    "/otn/leftTicket/queryTicketPrice": (
        "degraded",
        "票价查询（QY-07 结构化的一部分）。上游未列；调用点失败返回空字典，"
        "不影响抢票主流程。",
        "",
    ),
    "/otn/czxx/queryByTrainNo": (
        "degraded",
        "经停站信息（QY-07）。上游未列；调用点失败返回空列表，不影响抢票主流程。",
        "",
    ),
    "/otn/confirmPassenger/resultOrderForDcQueue": (
        "degraded",
        "通知服务端「排队完成」。上游未列（官方前端才会回调）；"
        "调用点把异常吞掉并记 debug 日志，不影响出票。",
        "",
    ),
    "/otn/login/loginOut": (
        "degraded",
        "登出。上游未列；调用点 try/except 忽略失败。",
        "",
    ),
    # ---------- 不是接口 ----------
    "/otn/passengers/init": (
        "referer",
        "只是「常用联系人」页面的 Referer，程序不把它当接口调用。",
        "",
    ),
    "/otn/payOrder/initPay": (
        "referer",
        "12306 支付入口。本工具**不代付**，只在界面上作为跳转链接打开。",
        "",
    ),
    "/otn/resources/js/framework/station_name.js": (
        "static",
        "官方站点表静态资源（js），用于把电报码翻译成站名，不走业务协议。",
        "",
    ),
    # ---------- 死常量 ----------
    "/otn/leftTicket/queryTrainStop": (
        "unused",
        "urls.py 里定义了 CHECK_TRAIN_AVAILABILITY，但全仓库没有任何引用 —— "
        "真正在用的经停站接口是 /otn/czxx/queryByTrainNo。留着它只会让人以为能查余票。",
        "",
    ),
}


# --------------------------------------------------------------------------- #
def _strings(node: ast.AST):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for item in node.elts:
            yield from _strings(item)


def _to_path(raw: str) -> str | None:
    text = re.sub(r"^https?://[^/]+", "", str(raw or "").strip())
    text = text.split("?", 1)[0].split("#", 1)[0]
    if not text.startswith(("/otn/", "/passport/")):
        return None
    return text


def _local_paths() -> dict[str, str]:
    """从 ``urls.py`` 里抽出「本项目用到的所有路径」→ {路径: 常量名}。"""
    tree = ast.parse(URLS_FILE.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not names:
            continue
        for raw in _strings(node.value):
            path = _to_path(raw)
            if path:
                found.setdefault(path, names[0])
    return found


def _reference_count(const: str) -> int:
    """常量被引用了几次（扫 ``urls.<常量>`，不含 urls.py 自身的定义）。"""
    pattern = re.compile(rf"urls\.{re.escape(const)}\b")
    hits = 0
    for folder in SCAN_DIRS:
        for path in (BASE / folder).rglob("*.py"):
            if path == URLS_FILE:
                continue
            hits += len(pattern.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return hits


def _baseline() -> dict:
    if not BASELINE_FILE.exists():
        pytest.fail(
            f"缺少协议基线快照 {BASELINE_FILE.relative_to(BASE)}。"
            "请运行：python scripts/build_upstream_baseline.py"
        )
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 基线自身
# --------------------------------------------------------------------------- #
def test_baseline_snapshot_is_up_to_date() -> None:
    """快照必须能被重新生成出来 —— 防止它被手改。

    基线一旦可以手改，它就退化成「我抄了一遍自己的想法」，
    那份测试也只是自洽，而不是与外部对齐。
    """
    assert _baseline() == build(), (
        "基线快照与上游参照文件不一致：要么上游文件换了（请重建快照），"
        "要么快照被手工改过。运行 `python scripts/build_upstream_baseline.py` 重建。"
    )


def test_baseline_actually_covers_something() -> None:
    """基线必须真的抓到了东西 —— 空快照会让主断言变成永远通过。"""
    data = _baseline()
    assert data["count"]["paths"] >= 30, f"基线只有 {data['count']['paths']} 条路径，像没抓到"
    paths = {e["path"] for e in data["paths"]}
    for must in (
        "/passport/web/login",
        "/otn/leftTicket/init",
        "/otn/confirmPassenger/initDc",
        "/otn/afterNate/confirmHB",
    ):
        assert must in paths, f"基线里缺少 {must} —— 抓取逻辑可能坏了"


# --------------------------------------------------------------------------- #
# 主断言
# --------------------------------------------------------------------------- #
def test_every_local_path_has_an_external_source_or_a_written_reason() -> None:
    """本项目用到的每条路径：要么上游有，要么写明理由。"""
    upstream = {e["path"] for e in _baseline()["paths"]}
    unexplained = [
        (path, const)
        for path, const in sorted(_local_paths().items())
        if path not in upstream and path not in JUSTIFIED
    ]
    if unexplained:
        lines = "\n".join(f"    {p}    （常量 {c}）" for p, c in unexplained)
        pytest.fail(
            "以下路径在**上游基线里找不到**，也没有在 JUSTIFIED 里写明理由。\n"
            "这正是当初 `/otn/confirmPassenger/confirmHB` 造成 HTTP 405 的那类问题：\n"
            "路径照直觉写、没有出处，于是离线测试永远发现不了。\n\n"
            f"{lines}\n\n"
            "处理方式（二选一）：\n"
            "  1. 它是错的 → 改成基线里有的那条路径；\n"
            "  2. 它是对的 → 加进 JUSTIFIED，并写清证据类型与日期。"
        )


def test_justified_entries_are_not_stale() -> None:
    """JUSTIFIED 里不许有「已经不需要」的条目。

    没有这条，归类表会越堆越长，最后变成「什么都能豁免」的橡皮图章。
    """
    upstream = {e["path"] for e in _baseline()["paths"]}
    local = _local_paths()
    stale = sorted(p for p in JUSTIFIED if p in upstream or p not in local)
    assert not stale, (
        f"JUSTIFIED 里这些条目已经不需要了（上游已有该路径，或本项目已不再使用）：{stale}"
    )


def test_justified_evidence_is_machine_checkable() -> None:
    """每一条归类的证据都必须能被机器验证，而不是一句无法核实的说法。"""
    local = _local_paths()
    templates = {e["path"] for e in _baseline()["templates"]}
    for path, entry in JUSTIFIED.items():
        assert len(entry) == 3, f"{path} 的归类项必须是 (证据类型, 理由, 日期)"
        kind, reason, date = entry
        assert kind in EVIDENCE_KINDS, f"{path} 的证据类型 {kind!r} 不在白名单里"
        assert len(reason.strip()) >= 20, f"{path} 的理由太短，说不清楚：{reason!r}"
        const = local.get(path)
        assert const, f"{path} 不在 urls.py 里，归类项多余"

        if kind == "device":
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""), (
                f"{path} 声称真机实测，但日期格式不对：{date!r}（应为 YYYY-MM-DD）"
            )
        elif kind == "unused":
            count = _reference_count(const)
            assert count == 0, (
                f"{path}（{const}）被归类为 unused，但实际被引用了 {count} 次 —— "
                "要么补一份像样的证据，要么把调用点去掉"
            )
        elif kind == "degraded":
            count = _reference_count(const)
            assert count > 0, (
                f"{path}（{const}）被归类为 degraded，但它一次都没被引用 —— "
                "那应该是 unused，别拿「已降级」当挡箭牌"
            )
        elif kind == "upstream_templated":
            assert any(t for t in templates if path.startswith(t.replace("{type}", ""))), (
                f"{path} 声称来自上游模板，但基线里找不到对应模板：{sorted(templates)}"
            )


def test_the_405_path_is_recorded_as_a_regression() -> None:
    """把那次事故钉在基线上。

    上游**有** `/otn/afterNate/confirmHB`，**没有** `/otn/confirmPassenger/confirmHB`。
    这条测试的价值不在于「现在是对的」，而在于以后有人想把候补路径改回去时，
    能立刻看到当年是为什么错的。
    """
    paths = {e["path"] for e in _baseline()["paths"]}
    assert "/otn/afterNate/confirmHB" in paths, "上游基线里必须能找到正确的候补确认接口"
    assert "/otn/confirmPassenger/confirmHB" not in paths, (
        "旧路径 `/otn/confirmPassenger/confirmHB` 不该在上游基线里 —— "
        "这正是真机回 HTTP 405 的那条路径"
    )
    assert "/otn/confirmPassenger/confirmHB" not in _local_paths(), "本项目又用回了那条 405 路径"


def test_candidate_family_is_fully_covered_upstream() -> None:
    """候补链路七步里，**有上游出处的**必须真的是上游出处 —— 不许靠归类蒙过去。

    这是本轮把基线建起来以后才拿到的东西：`testerSunshine/12306` 的
    `config/urlConf.py` 里完整定义了 afterNate 候补族（chechFace / getSuccessRate /
    submitOrderRequest / confirmHB / queryQueue / passengerInitApi）。
    也就是说 —— 当初那条 405，只要有一份上游基线就**当场能发现**。
    """
    paths = {e["path"] for e in _baseline()["paths"]}
    for must in (
        "/otn/afterNate/chechFace",
        "/otn/afterNate/getSuccessRate",
        "/otn/afterNate/submitOrderRequest",
        "/otn/afterNate/passengerInitApi",
        "/otn/afterNate/confirmHB",
        "/otn/afterNate/queryQueue",
    ):
        assert must in paths, f"上游基线里应该有 {must}"
        assert must in _local_paths(), f"候补链路应该用到 {must}"
