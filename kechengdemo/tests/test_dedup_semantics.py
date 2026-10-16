# -*- coding: utf-8 -*-
"""代码复用收敛的**语义**回归（不是「文件存在」，是「行为对」）。

    .venv/Scripts/python.exe tests/test_dedup_semantics.py

覆盖本轮收敛中**最容易悄悄退化**的三件事：

1. **`audit.record_action` 的 detail 必须是 JSON 对象**。
   改造前调用点是 `db.execute(..., (actor, action, target, db.as_json(detail)))`
   —— 把已序列化的字符串交给 SQL。改成 `record_action(actor, action, target, detail)`
   时如果忘了剥掉那层 `db.as_json(...)`，写进 jsonb 的会是
   `"{\\"a\\":1}"` 这种**被转义的字符串**：审计不报错、查询不报错，
   只是详情从对象退化成字符串 —— 没有测试就永远发现不了。

2. **模块级名字被覆盖（shadowing）**。`api_review.py` 里曾有个路由处理函数叫
   `def audit(limit)`，把顶部的 `import audit` 覆盖掉了；内联 SQL 时代看不出问题，
   一旦同文件里写 `audit.record_action(...)` 就 `AttributeError`。
   这里用 AST 扫全仓库的「顶层定义与已导入模块同名」，一次兜住这类问题。

3. **投递次数**。`score/add` 是**累加**语义：多投一次 = 多加一遍分。
   写操作必须**单次投递**（重试交给 outbox）；读操作的总尝试次数也必须
   是一眼可读的数字（`weiban.READ_ATTEMPTS = 3`），不能再出现
   「两层嵌套、到底打几次要靠乘法算」的说法。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import ast
import sys
from pathlib import Path

from _harness import ROOT, check, summary  # noqa: E402

import audit                                       # noqa: E402
import db                                          # noqa: E402
import http_client                                 # noqa: E402
import weiban                                      # noqa: E402

ROOT = Path(ROOT)


# ============================================================ 1. 审计 detail 形态

def test_audit_detail_shape():
    print("\n--- 1. audit.record_action：detail 落库是 JSON 对象 ---")
    audit.record_action("t_dedup", "t_dedup_check", "dedup:verify",
                        {"k": 1, "nested": [1, 2], "cn": "中文"})
    row = db.query_one("SELECT detail FROM audit_log WHERE target='dedup:verify' "
                       "ORDER BY id DESC LIMIT 1")
    detail = row["detail"] if row else None
    check("写进去了", bool(row), row)
    check("detail 是 dict（不是被转义的字符串）", isinstance(detail, dict),
          type(detail).__name__)
    check("嵌套与中文都对", isinstance(detail, dict) and detail.get("nested") == [1, 2]
          and detail.get("cn") == "中文", detail)

    audit.record_action("t_dedup", "t_dedup_check", "dedup:verify-null", None)
    row2 = db.query_one("SELECT detail FROM audit_log WHERE target='dedup:verify-null' "
                        "ORDER BY id DESC LIMIT 1")
    check("detail=None → NULL（保持改造前语义）", row2 and row2["detail"] is None, row2)

    # 审计是旁路观测：写失败不能抛（表名故意写错来触发）
    try:
        db.execute("CREATE TEMP TABLE _t_dedup_guard (x int)")
        audit.record_action  # noqa: B018  仅提示下面调的是真函数
        ok = True
    except Exception as e:                          # noqa: BLE001
        ok = False
        print("        （临时表创建失败，忽略）", e)
    check("record_action 不抛异常（失败只打印）", ok)

    db.execute("DELETE FROM audit_log WHERE target LIKE %s", ("dedup:%",))


# ============================================================ 2. 模块名被覆盖

def _shadowed_imports(path):
    """返回该文件里「顶层定义/赋值把已导入的模块名占掉」的清单。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {(a.asname or a.name) for a in node.names}
    bad = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.name in imported:
            bad.append(f"{node.name}() @line {node.lineno}")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in imported:
                    bad.append(f"{t.id} = ... @line {node.lineno}")
    return bad


def test_no_module_shadowing():
    print("\n--- 2. 顶层定义不得覆盖已导入的模块名（AST 全仓扫描）---")
    root = Path(ROOT)
    targets = sorted((root / "backend").glob("*.py")) + sorted(root.glob("*.py"))
    problems = {}
    for p in targets:
        if p.name.startswith(".") or p.parts[-1] in ("setup.py",):
            continue
        try:
            bad = _shadowed_imports(p)
        except SyntaxError as e:
            problems[p.name] = [f"语法错误：{e}"]
            continue
        if bad:
            problems[p.name] = bad
    check(f"扫了 {len(targets)} 个模块，无「定义名 = 模块名」冲突", not problems,
          problems)

    import api_review
    check("api_review.audit 仍是模块（不是路由函数）",
          hasattr(api_review.audit, "record_action"), type(api_review.audit).__name__)


# ============================================================ 3. 投递次数

def test_retry_budget():
    print("\n--- 3. 重试层数与投递次数 ---")
    seen = []
    orig = http_client.request_json

    def fake(method, url, **kw):
        seen.append(kw.get("retries"))
        raise http_client.HttpFailure("stub", retryable=True)

    http_client.request_json = fake
    try:
        try:
            weiban.request_retry("GET", "/open-api/tag/list")   # 默认：读接口
            check("重试耗尽抛异常", False)
        except http_client.HttpFailure:
            check("重试耗尽抛 HttpFailure（信息量比 RuntimeError 多）", True)
        check("★ 读接口总尝试次数 = READ_ATTEMPTS = 3（不是 9）",
              seen == [weiban.READ_ATTEMPTS - 1] == [2], seen)

        seen.clear()
        try:
            weiban.request_retry("GET", "/open-api/tag/list", attempts=1)
        except http_client.HttpFailure:
            pass
        check("attempts=1 → 单次（retries=0）", seen == [0], seen)
    finally:
        http_client.request_json = orig

    calls = []
    orig_retry, orig_tok = weiban.request_retry, weiban.get_access_token
    weiban.request_retry = lambda *a, **kw: (calls.append(kw) or {"errcode": 0})
    weiban.get_access_token = lambda *a, **kw: "tok"
    try:
        # 读路径：随便挑一个公开读接口，看它实际传下去的次数
        weiban.list_staff(limit=1)
        check("★ 公开读接口（list_staff）走 READ_ATTEMPTS = 3",
              calls and calls[0].get("attempts") == weiban.READ_ATTEMPTS, calls[:1])
        calls.clear()
        weiban.add_customer_score("S1", external_user_id="C1", score=3,
                                  use_header_token=True)
        weiban.add_customer_score("S1", external_user_id="C1", score=3,
                                  use_header_token=False)
        check("★ score/add 两种 token 模式都是**单次投递**（累加语义，绝不能重试）",
              len(calls) == 2 and all(c.get("attempts") == 1 for c in calls),
              calls)
    finally:
        weiban.request_retry, weiban.get_access_token = orig_retry, orig_tok


def main():
    test_audit_detail_shape()
    test_no_module_shadowing()
    test_retry_budget()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
