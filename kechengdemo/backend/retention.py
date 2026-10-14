# -*- coding: utf-8 -*-
"""数据保留策略（架构 §2.8 边界4，设置项 `data_retention_days`）。

## 为什么默认关闭

「保留多少天」是**业务决策**，文档里也标的是「待定」。所以：
· 默认 ``0`` = 不清理（**不改变既有行为**）
· 必须显式配置才会生效，且支持 ``dry_run`` 先看会删多少

## 清理范围的取舍（重要）

**只清「运行观测类」表，不清「业务结论类」表**：

| 清理 | 表 | 理由 |
|---|---|---|
| ✅ | `audit_log` / `trace_span` / `session_failure` / `request_guard` / `scoring_run` | 纯观测副产品，随时间只增不减，删掉不影响任何结论 |
| ✅ | `chat_message` | 占空间最大的一张表；**但要显式开**（默认同一 `data_retention_days` 控制） |
| ❌ | `customer_intent_assessment` | 业务结论。「上月这个客户是 S」是历史事实，删了就无法追溯客户变化 |
| ❌ | `negative_review` | 人工复核记录，删除等于丢掉人工判断 |
| ❌ | `score_writeback` | **回写账本**。删了会失去「已回写过多少分」的依据，D2-1 差值累加依赖它 |
| ❌ | `eval_case` / `eval_run` | 评估集是回归基准，删了门禁就失效 |

结论：**删日志不删结论**。若业务要求连结论一起清，那是另一套「归档」流程（导出后删），
不能用「按天数删」草率处理 —— 会在没有任何备份的情况下丢掉人工复核结果。
"""
import time

import db

#: 会被清理的表 → (时间列, 说明)。顺序无关。
PURGEABLE = {
    "audit_log": ("created_at", "审计日志"),
    "trace_span": ("created_at", "本地 trace（默认关）"),
    "session_failure": ("created_at", "单会话失败明细"),
    "request_guard": ("created_at", "请求幂等记录"),
    "scoring_run": ("started_at", "运行记录"),
    "chat_message": ("msg_time", "聊天消息（Unix 秒）"),
}

#: 明确**不**清理的表（写出来是为了让「为什么没清它」有据可查，不靠记忆）
PROTECTED = {
    "customer_intent_assessment": "业务结论（历史事实，删了无法追溯客户变化）",
    "negative_review": "人工复核记录（删除等于丢掉人工判断）",
    "score_writeback": "回写账本（D2-1 差值累加依赖「已回写多少分」）",
    "eval_case": "评估集基线（删了回归门禁失效）",
    "eval_run": "评估运行记录",
    "scoring_rule": "规则版本化快照",
    "app_setting": "运行时配置",
}


def _cutoff_expr(col: str, days: int) -> tuple:
    """返回 ``(WHERE 片段, 参数)``。

    ⚠️ `chat_message.msg_time` 是 **Unix 秒（BIGINT）**，不是时间戳 ——
    直接和 ``now() - interval`` 比会类型错。另一张表的时间列是 TIMESTAMPTZ。
    这个差异必须显式处理，否则清理要么报错要么静默删光（把 `msg_time > 0` 当条件）。
    """
    if col == "msg_time":
        return f"{col} < %s", (int(time.time()) - days * 86400,)
    return f"{col} < now() - (%s || ' days')::interval", (str(int(days)),)


def plan(days: int) -> dict:
    """**只读预演**：算出各表会被删掉多少行。不删任何数据。"""
    days = int(days or 0)
    out = {}
    if days <= 0:
        return {"days": days, "enabled": False, "tables": {}, "protected": PROTECTED}
    for tbl, (col, desc) in PURGEABLE.items():
        where, params = _cutoff_expr(col, days)
        try:
            row = db.query_one(f"SELECT count(*) AS n FROM {tbl} WHERE {where}", params)
            out[tbl] = {"rows": (row or {}).get("n", 0), "column": col, "desc": desc}
        except Exception as e:                  # noqa: BLE001
            # 表不存在（如 request_guard 还没被任何请求用过）不是错误：
            # 报 0 行并标注，比让整个预演失败有用。
            msg = f"{type(e).__name__}: {e}"
            missing = "does not exist" in msg or "relation" in msg
            out[tbl] = {"rows": 0, "column": col, "desc": desc,
                        "table_exists": not missing,
                        **({} if missing else {"error": msg})}
    return {"days": days, "enabled": True, "tables": out, "protected": PROTECTED}


def purge(days: int, dry_run: bool = True, batch: int = 5000) -> dict:
    """按 ``days`` 清理运行观测类数据。``dry_run=True``（默认）只预演。

    分批删（每批 ``batch`` 行）而不是一条大 DELETE：
    大表一次性删几十万行会长时间持锁，把在线查询一起拖住。
    """
    days = int(days or 0)
    if days <= 0:
        return {"days": days, "enabled": False, "deleted": {}, "dry_run": dry_run}
    preview = plan(days)
    if dry_run:
        return {**preview, "dry_run": True, "deleted": {}}

    deleted = {}
    for tbl, (col, _desc) in PURGEABLE.items():
        where, params = _cutoff_expr(col, days)
        total = 0
        while True:
            # 分批 + LIMIT：`DELETE ... WHERE ctid IN (SELECT ...)` 在 PG 上是标准写法
            n = db.execute(
                f"DELETE FROM {tbl} WHERE ctid IN "
                f"(SELECT ctid FROM {tbl} WHERE {where} LIMIT {int(batch)})", params)
            total += (n or 0)
            if not n or n < batch:
                break
        deleted[tbl] = total
    return {**preview, "dry_run": False, "deleted": deleted,
            "purged_at": time.strftime("%Y-%m-%d %H:%M:%S")}
