# -*- coding: utf-8 -*-
"""Schema 漂移检测 —— 部分替代「迁移框架」的缺失（代码评审 P1-3）。

## 为什么不用 Alembic

本项目 `schema.sql` 全部是 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`，
`init_db()` 每次启动整文件幂等执行。引入 Alembic 意味着要把现有 22 张表**重写一遍**成
迁移脚本、并维护版本链 —— 对一个「单实例、单机、本地工具」是过重的成本，且风险大于收益。

评审指出的真实痛点其实是**另一件事**：DDL 曾散在三处（`schema.sql` +
`failure_worker.ensure_columns()` + `idempotency._ensure_table()`），
改一处忘另一处会**静默漂移**；而「新库建对了、老库缺列」只有跑到那行代码才暴露。
那两处内联 DDL 已收敛回 `schema.sql`（保留兼容兜底并注明）。本模块补上**验证环节**：

  · `expected_columns()`：解析 `schema.sql` 得到「权威表 → 列集合」
  · `check()`：与 `information_schema` 实际列对比 → 报**缺失列 / 多余列**
  · `GET /api/scoring/schema-check` 暴露结果（排障用）

这样「漂移」从「运行时才炸」变成「启动/查询时可见」。

## 边界

· 只比对**列**（最常漂移、后果最直接）；索引/约束不做解析（`IF NOT EXISTS` 下噪声大）。
· 解析是**正则**级别，不引入 SQL 解析依赖；解析不了的语句会**跳过并计数**，
  而不是假装成功 —— 拿不准就说拿不准。
"""
import re
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent
if str(_BACKEND) not in sys.path:            # 支持 `python backend/schema_check.py` 直接跑
    sys.path.insert(0, str(_BACKEND))

import db                                    # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

#: `CREATE TABLE IF NOT EXISTS xxx (` 起到第一个独占行的 `);` 止
_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)\s*\((.*?)\n\)\s*;",
    re.S | re.I)
#: `ALTER TABLE xxx ADD COLUMN [IF NOT EXISTS] col ...`（可跨行、可多条连写）
_ALTER_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w]*)\s+(.*?);", re.S | re.I)
_ADD_COL_RE = re.compile(
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)", re.I)
#: 列定义行：`col_name TYPE ...`（排除 PRIMARY/UNIQUE/FOREIGN/CHECK/CONSTRAINT 等表级约束）
_SKIP_PREFIX = ("primary", "unique", "foreign", "check", "constraint", "exclude", "like")


def expected_columns() -> tuple:
    """解析 `schema.sql`，返回 `(表 → 列集合, 无法解析的语句数)`。

    ⚠️ 必须同时解析 `CREATE TABLE` 与 `ALTER TABLE ... ADD COLUMN` ——
    本项目后续新增的列**大量走 ALTER**（幂等补列，老库新库都能跑）。
    只解析建表语句会把这些列误判为「库里有、权威没有」的漂移（实测踩到，纯误报）。
    """
    if not SCHEMA_PATH.exists():
        return {}, -1
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    # 去掉行注释，避免注释里的 `);` 或逗号干扰
    sql = re.sub(r"--[^\n]*", "", sql)

    tables: dict = {}
    for m in _TABLE_RE.finditer(sql):
        name, body = m.group(1), m.group(2)
        cols = set()
        for raw in body.split("\n"):
            line = raw.strip().rstrip(",").strip()
            if not line or not line[0].isalpha() and line[0] != "_":
                continue
            first = line.split()[0].lower().strip('"')
            if first in _SKIP_PREFIX:
                continue
            if re.match(r"^[A-Za-z_][\w]*$", first):
                cols.add(first)
        tables.setdefault(name, set()).update(cols)

    # 依次吸收 ALTER TABLE ADD COLUMN（一条 ALTER 可含多个 ADD COLUMN）
    for m in _ALTER_RE.finditer(sql):
        name, body = m.group(1), m.group(2)
        added = _ADD_COL_RE.findall(body)
        if added:
            tables.setdefault(name, set()).update(c.lower() for c in added)

    n_create = len(re.findall(r"CREATE\s+TABLE", sql, re.I))
    unparsed = max(0, n_create - len([t for t in tables]))
    return tables, unparsed


def actual_columns() -> dict:
    """从 information_schema 取实际列（当前库）。"""
    rows = db.query(
        """SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema = 'public'""")
    out: dict = {}
    for r in rows:
        out.setdefault(r["table_name"], set()).add(r["column_name"])
    return out


def check() -> dict:
    """对比 `schema.sql`（权威）与实际库，返回漂移报告。"""
    want, unparsed = expected_columns()
    if unparsed < 0:
        return {"ok": False, "reason": f"读不到 {SCHEMA_PATH.name}"}
    try:
        have = actual_columns()
    except Exception as e:                      # noqa: BLE001
        return {"ok": False, "reason": f"连不上数据库：{type(e).__name__}: {e}"}

    missing_tables, missing_cols, extra_cols = [], [], []
    for tbl, cols in sorted(want.items()):
        if tbl not in have:
            missing_tables.append(tbl)
            continue
        miss = sorted(cols - have[tbl])
        if miss:
            missing_cols.append({"table": tbl, "columns": miss})
    # 库里有、schema.sql 没声明的列（多为内联 DDL / 手工 ALTER 的产物 = 真实漂移源）
    for tbl, cols in sorted(have.items()):
        if tbl in want:
            extra = sorted(cols - want[tbl])
            if extra:
                extra_cols.append({"table": tbl, "columns": extra})

    drift = bool(missing_tables or missing_cols or extra_cols)
    return {
        "ok": not drift,
        "drift": drift,
        "tables_in_schema": len(want),
        "tables_in_db": len(have),
        "unparsed_statements": unparsed,
        "missing_tables": missing_tables,
        "missing_columns": missing_cols,      # 权威有、库里没有 → 会真的报错
        "extra_columns": extra_cols,          # 库里有、权威没有 → 文档欠账
        "hint": ("无漂移：schema.sql 与实际库一致" if not drift
                 else "有漂移。先确认是否为「老库未补列」（跑 db.init_db() 补齐）；"
                      "extra_columns 说明有 DDL 没写进 schema.sql"),
    }


if __name__ == "__main__":                      # 命令行自查
    import json
    print(json.dumps(check(), ensure_ascii=False, indent=1))
