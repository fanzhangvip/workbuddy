# -*- coding: utf-8 -*-
"""默认值清单：**默认开启的保护** vs **默认关闭的观测/优化**（代码评审 §5.5）。

## 为什么需要这份清单

评审原话：「『防御措施存在但未接线』—— `reap()` 无调用方、保留期与 trace 默认关。
**这类状态比没写更危险，因为看代码的人会以为已经防住了。**」

这句话点的是**可读性/可信度**问题，不是功能缺失：
`data_retention_days=0`、`trace_enabled=false`、`use_graph_session=false` 都是**刻意的**默认，
但代码里没有任何地方把「谁是保护、谁是观测」说清楚 → 读者只能逐个去猜，
猜错的代价是「以为防住了，其实没有」。

本模块把这件事**做成机器可读的清单**，并暴露成 `GET /api/scoring/defaults-manifest`，
让「默认到底开没开、属于哪一类」不需要读 50 个键的源码。

## 两类怎么分

| 类别 | 含义 | 该不该默认开 |
|---|---|---|
| **保护**（`guard`） | 不开就会**丢数据 / 写错外部系统 / 无法追责** | **必须默认开** |
| **观测**（`observe`） | 只产生额外信息，不开也不影响正确性 | 默认关（省成本/避免噪声） |
| **优化**（`optimize`） | 改变处理路径，行为等价 | 默认关（灰度用） |
| **业务决策**（`policy`） | 只有业务能拍板（保留多久、配额多大） | 默认「不限制」，等业务定 |

⚠️ 本清单是**声明 + 自检**，不是新机制：
`verify()` 会去实际读配置，如果某项声称「默认开」而实测是关的，就报出来 ——
清单本身也会漂移，所以它必须能被验证。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "backend")):   # 支持直接 `python backend/defaults_manifest.py`
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db  # noqa: E402  设置读侧（`db.setting*`，唯一实现）

#: `(键, API 名, 类别, 默认值, 说明)`
#: 默认值为 `None` 表示「由其他模块常量决定」（如 confidence.* 的标定值）
MANIFEST: tuple = (
    # ---------------- 保护：默认必须开 ----------------
    ("eval_gate_enabled", "eval_gate_enabled", "guard", True,
     "改规则前强制跑评估集门禁（闸6）：不开则规则可以随便发布、指标倒退无人拦"),
    ("daily_run_analysis", "daily_run_analysis", "guard", True,
     "日常任务：同步跑完自动接着跑分析（需求 4.2）。关掉则日常任务只同步不评分"),
    ("writeback_delta_mode", "writeback_delta_mode", "guard", True,
     "微伴评分是累计语义 → 只写差值。关掉会让客户分数被反复累加而虚高"),
    ("use_outbox", "use_outbox", "guard", True,
     "回写与评分解耦（修 G6）。关掉则回写失败随增量水位推进而**永久丢失**"),
    ("scoring_priority_order", "priority_order", "guard", True,
     "候选排序：先评高价值客户。关掉不会出错，但会让重要客户排到最后"),
    ("ctx_min_customer_msgs", "ctx_min_customer_msgs", "guard", 1,
     "有效信息量闸（B4）：客户至少说一句话。防「销售单方面刷屏」被当成高意向"),

    # ---------------- 观测：默认关 ----------------
    ("trace_enabled", "trace_enabled", "observe", False,
     "本地链路追踪（trace_span）。只写结构指纹、不落原文；关掉零开销"),
    ("data_retention_days", "data_retention_days", "policy", 0,
     "运行观测数据保留天数。**0 = 不清理** —— 保留多久是业务决策，代码不替业务拍板"),

    # ---------------- 优化/灰度：默认关 ----------------
    ("use_graph_session", "use_graph_session", "optimize", False,
     "LangGraph 图化路径。与串行路径行为等价（有 parity 测试），灰度用"),

    # ---------------- 业务决策：默认「不限」 ----------------
    ("writeback_quota_per_run", "writeback_quota_per_run", "policy", 0,
     "单轮回写次数上限。**0 = 不限**；上线前由业务确认后收紧"),
    ("writeback_quota_per_customer_per_day", "writeback_quota_per_customer_per_day",
     "policy", 0, "单客户单日回写上限。**0 = 不限**；文档建议 3"),
    ("scoring_deadline_minutes", "deadline_minutes", "guard", 120,
     "整轮评分的墙钟上限。防「卡住一整轮 2.7 小时没人发现」"),

    # ---------------- 写外部系统：默认最保守 ----------------
    ("grade_tag_mode", "grade_tag_mode", "guard", "off",
     "等级→微伴标签三档开关。**默认 off**：打标签是写外部系统的副作用，必须人显式打开"),
    ("grade_tag_probe_customer", "grade_tag_probe_customer", "guard", "",
     "建标签载体（专用测试客户）。**默认空 = 禁止自动建标签** —— "
     "防止把等级标签打到无关的真实客户身上"),
)

#: 「已接线」的定时任务（防止「函数写好了但没人调用」——评审 P2-1 的原话形态）
WIRED_JOBS: tuple = (
    ("idempotency-reap", "idempotency.reap()", 60,
     "清理过期幂等记录。★ 评审前该函数**全仓库无调用方**，叠加保留期默认 0 → 永不清理"),
    ("writeback-outbox-drain", "outbox.drain()", 0.5,
     "排空分数回写 outbox（30 秒）"),
    ("grade-tag-drain", "grade_tag.drain()", 0.5,
     "排空等级标签 outbox（30 秒，独立于分数回写）"),
    ("session-failure-retry", "failure_worker.drain()", 15,
     "重投评分失败的会话（修 G5）"),
    ("data-retention", "retention.purge()", 720,
     "按 `data_retention_days` 清理观测数据（**默认 0 = 直接返回，不清理**）"),
    ("daily-main", "sync → 分析（需求 4.2 主任务，默认 20:00）", 1440,
     "每晚主增量同步 + 分析：覆盖 19:30 的销售主动发送及其后的客户互动"),
    ("daily-compensate", "sync(lookback=48h) → 分析（需求 4.2 补偿，默认 03:30）", 1440,
     "48 小时补偿同步 + 分析：修复接口入库延迟 / 网络失败 / 任务中断导致的漏处理"),
    ("daily-watch", "scheduler._daily_watch_job（链条推进 + 窗口收尾）", 0.5,
     "每 30 秒推进「同步 → 分析」链条：同步结束才启动分析，窗口到点请求停止"),
)


def _norm(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v).strip().lower()


def verify() -> dict:
    """核对清单与实际配置是否一致。

    ⚠️ 只核对 `guard`（保护类）：**声称默认开、实测却是关的**才是危险信号
    （「以为防住了，其实没有」）。观测/优化类默认关是**设计意图**，不算不符。
    """
    import scoring_service as ss
    items, mismatch = [], []
    for key, api, kind, default, desc in MANIFEST:
        try:
            if isinstance(default, bool):
                actual = db.setting_bool(key, "", default)
            elif isinstance(default, int):
                actual = db.setting_int(key, "", default)
            else:
                actual = db.setting(key, "", default)
        except Exception as e:                  # noqa: BLE001
            actual = f"<读取失败 {type(e).__name__}>"
        drift = _norm(actual) != _norm(default)
        items.append({"key": key, "api": api, "kind": kind, "default": default,
                      "actual": actual, "desc": desc,
                      "changed_from_default": drift})
        # 保护类「声称开却是关」→ 真危险；观测类改了默认只是用户选择
        if kind == "guard" and _norm(default) in ("true", "on") and _norm(actual) in ("false", "off"):
            mismatch.append({"key": key, "expected": default, "actual": actual,
                             "risk": "保护措施被关闭"})
    return {
        "ok": not mismatch,
        "guard_mismatches": mismatch,
        "items": items,
        "wired_jobs": [{"id": j, "callable": c, "interval_minutes": m, "desc": d}
                       for j, c, m, d in WIRED_JOBS],
        "legend": {
            "guard": "不默认开就会丢数据 / 写错外部系统 / 无法追责 → 必须默认开",
            "observe": "只产生额外信息，不开不影响正确性 → 默认关（零开销）",
            "optimize": "改变处理路径但行为等价 → 默认关（灰度）",
            "policy": "只有业务能拍板（保留期、配额）→ 默认「不限制」等业务定",
        },
    }


if __name__ == "__main__":                      # 命令行自查
    import json
    print(json.dumps(verify(), ensure_ascii=False, indent=1))
