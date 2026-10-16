# -*- coding: utf-8 -*-
"""评估集标注对照工具：**审计里那版** vs **当前行**（修 P13/P14）。

    .venv\\Scripts\\python.exe -m backend.eval_restore              # 预演（默认，不写库）
    .venv\\Scripts\\python.exe -m backend.eval_restore --apply      # 用审计版本覆盖当前行
    .venv\\Scripts\\python.exe -m backend.eval_restore --only-changed
    .venv\\Scripts\\python.exe -m backend.eval_restore --case 7 --apply

## 它解决什么

2026-10-15 实测：30 条标注在 07:30–07:35 保存过（`audit_log.eval_annotate` 里留着
**完整的 payload**），随后至少 21 行被 AI 输出整体替换；而当时的保存路径不清
`prefilled`，所以门禁的 `confirmed` 一直是 0。审计日志里那版内容是**唯一**能还原
"当时到底保存了什么"的依据。

## ⚠️ 为什么默认不写、而且不叫"恢复"

**审计里那版未必是对的**：那批保存的节奏是 30 条 4.5 秒一条（不像逐条人工核对），
而且用它跑出的 `fp_case_rate` 高达 0.5385（说明它比 AI 输出少了很多项）。
**无脑恢复 = 把一个可疑标准提升为金标准** —— 那比没有门禁更糟：它会拦住正确的改动、
放过错误的改动。

所以本工具的定位是**给出可核对的对照表**：谁想用哪一版，得**自己看过 diff** 再 `--apply`。
`--apply` 走 `eval_case_repo.set_annotation`（唯一写入口），因此会：
① 正确清 `prefilled`（该行代码曾漏过一次，见 P13）；② 留 before→after 审计。

## 边界

- 只认**最后一条** `eval_annotate`（那就是人最后一次保存的内容）。
- 已冻结（`frozen`）的样本不写（`set_annotation` 的调用方守卫在 API 层，这里自己判）。
- 逐条 `--apply` 是可重复执行的：第二次跑"改动数 = 0"（幂等）。
"""
import argparse
import json
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db                       # noqa: E402
import eval_case_repo as repo   # noqa: E402


def _ids(items) -> list:
    return sorted((i.get("rule_id"), int(i.get("score") or 0))
                  for i in db.as_dict_list(items))


def _fmt(ids: list) -> str:
    return " ".join(f"{r}{s:+d}" for r, s in ids) or "（空）"


def collect() -> list:
    """取「审计里最后一次保存的版本」与「当前行」的对照。"""
    audit = {}
    for r in db.query("""SELECT target, detail, created_at, actor FROM audit_log
                          WHERE action = 'eval_annotate' ORDER BY id"""):
        d = r["detail"] if isinstance(r["detail"], dict) else json.loads(r["detail"] or "{}")
        audit[str(r["target"])] = {"ids": _ids(d.get("items")),
                                   "raw": d.get("raw_score"), "grade": d.get("grade"),
                                   "at": r["created_at"], "actor": r["actor"]}
    rows = []
    for c in db.query("""SELECT case_id, expected_items, expected_raw_score, expected_grade,
                                prefilled, frozen, annotator, updated_at
                           FROM eval_case ORDER BY case_id"""):
        a = audit.get(str(c["case_id"]))
        cur = _ids(c.get("expected_items"))
        rows.append({"case_id": c["case_id"], "audit": a, "cur": cur,
                     "cur_raw": c.get("expected_raw_score"), "cur_grade": c.get("expected_grade"),
                     "prefilled": c.get("prefilled"), "frozen": c.get("frozen"),
                     "annotator": c.get("annotator"), "updated_at": c.get("updated_at"),
                     "same": bool(a) and a["ids"] == cur})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="评估集标注对照（默认预演）")
    ap.add_argument("--apply", action="store_true", help="把**审计版本**写回（会清 prefilled）")
    ap.add_argument("--only-changed", action="store_true", help="只处理与审计不一致的行")
    ap.add_argument("--case", type=int, help="只处理某一条")
    args = ap.parse_args()

    rows = [r for r in collect() if not args.case or r["case_id"] == args.case]
    if not rows:
        print("没有匹配的样本")
        return 1

    n_same = sum(1 for r in rows if r["same"])
    n_diff = len(rows) - n_same
    print(f"样本 {len(rows)} 条：与审计版**一致** {n_same} 条、**不一致** {n_diff} 条")
    print(f"（当前仍为草稿态 prefilled=true 的：{sum(1 for r in rows if r['prefilled'])} 条）\n")

    todo = []
    for r in rows:
        if args.only_changed and r["same"]:
            continue
        a = r["audit"]
        flag = "一致" if r["same"] else "不一致"
        print(f"case {r['case_id']:<4} [{flag}] 当前 {r['cur_raw']}/{r['cur_grade']}: {_fmt(r['cur'])}")
        if a:
            print(f"{'':>12}  审计 {a['raw']}/{a['grade']}: {_fmt(a['ids'])}"
                  f"   ← {a['at']:%m-%d %H:%M} by {a['actor']}")
        else:
            print(f"{'':>12}  审计：无记录（该行的内容不是通过 /save 存的）")
        if not r["same"] and a:
            todo.append(r)
        print()

    print(f"待写回（用审计版本覆盖当前行）：{len(todo)} 条")
    if not args.apply:
        print("\n这是**预演**（默认不写库）。确认 diff 无误后加 --apply。")
        print("⚠️ 写回会把这些行变成「已人工确认」（清 prefilled）—— 请先自己看过上面的 diff。")
        return 0

    done, skipped = 0, []
    for r in todo:
        if r["frozen"]:
            skipped.append((r["case_id"], "已冻结"))
            continue
        a = r["audit"]
        # 用**审计 payload 的原始 items**（保留 reason / dimension），而不是上面压扁的
        # `(rule_id, score)` 对 —— 写回去要的是能直接当标注用的完整项。
        raw = db.query_one("""SELECT detail FROM audit_log
                               WHERE action='eval_annotate' AND target=%s
                               ORDER BY id DESC LIMIT 1""", (str(r["case_id"]),))
        d = raw["detail"] if isinstance(raw["detail"], dict) else json.loads(raw["detail"] or "{}")
        repo.set_annotation(r["case_id"], items=d.get("items") or [],
                            raw_score=a["raw"], grade=a["grade"],
                            annotator=os.environ.get("USERNAME") or "eval-restore",
                            note="由 eval_restore 写回（审计版本）", disputed=False,
                            detail={"source": "eval_restore",
                                    "audit_at": str(a["at"])})
        done += 1
    print(f"\n已写回 {done} 条" + (f"，跳过 {len(skipped)} 条（{skipped}）" if skipped else ""))
    print("下一步：GET /api/eval/stats 看 confirmed 是否已 >0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
