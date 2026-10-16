# -*- coding: utf-8 -*-
"""历史评估的置信度**离线回填**（架构 §6 的 E4 配套工具）。

## 为什么需要它

`session_confidence` / `review_reasons` 是 §6 才引入的列。在那之前跑出的评估
（本项目实测 196 条：run#1/#21/#22 + 历史人工重评）这三列全是 NULL，
于是复核页/评分页对这批数据显示空白，`hook_*` 之外的观测也断了。

但**重跑模型没必要**：置信度是**纯函数**算出来的（`confidence.compute`），
输入全部已在库里（命中项、漏检、分值、等级、规则表）。所以离线重算即可 ——
**零 LLM 调用、零成本、可重复**。

## 它改什么、不改什么

**改**（补全观测 + 修复不一致）：
· `session_confidence` —— 新增观测列
· `review_reasons`     —— 新增观测列（「为何要复核」可追溯）
· `positive_items` / `negative_items` 内的 `item_confidence` / `confidence_kind`
  —— 与新版生产数据保持一致（否则新老数据字段不同，前端要写两套渲染）
· `needs_human_review` —— **修复历史不一致**：实测有 4 条「有负分却标 False」，
  回填后与 `negative_review` 队列恢复一致

**不改**：
· `rule_version` / `prompt_version` / `raw_score` / `grade` / 各项分值 —— 回填不重判，
  只补算「可信度」这一个维度（改了它们会触发增量重评，那才是真的动业务）
· `confidence` 兼容列 —— 已停写（E4），回填**不复活**它
· `negative_review` 队列 —— 实测需变更的行**本来就在队列里**，无需增删

## 确定性与可回滚

· **`last_grade` 必须用「回填前」的快照**：`_last_assessment` 的语义是「上一条的等级」，
  若边算边写会形成链条（第 2 行看到的是第 1 行的新值）→ 结果依赖处理顺序。
  故 `snapshot_last_grade()` 先把所有行的 last_grade 一次算好，再逐行应用。
  由于回填不改 `grade`，本快照天然稳定。
· **只处理 `session_confidence IS NULL` 的行**（默认）→ 重复执行改动数为 0（幂等）。
· **写前备份**：被改行的全部原值写成 JSON，`--rollback <file>` 可一键还原。

## 用法

    .venv\\Scripts\\python.exe -m backend.backfill                    # 预演（默认，不写库）
    .venv\\Scripts\\python.exe -m backend.backfill --apply            # 执行
    .venv\\Scripts\\python.exe -m backend.backfill --apply --limit 20 # 小批试跑
    .venv\\Scripts\\python.exe -m backend.backfill --rollback backups/xxx.json
    .venv\\Scripts\\python.exe -m backend.backfill --rel "staff:target" --apply  # 只回填某个客户
"""
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import confidence as cf            # noqa: E402
import assessment_repo             # noqa: E402  评估结果表唯一写入口（P3）
import db                          # noqa: E402
import rules as rules_mod          # noqa: E402
import scoring_service as ss       # noqa: E402

#: 每批行数（事务粒度）。回填是纯计算 + 单表 UPDATE，50 条一提交足够轻。
BATCH = 50

#: 备份目录（已加入 .gitignore）
BACKUP_DIR = os.path.join(_ROOT, "backups")


def _backup_path(backup_dir: str = None) -> str:
    """生成**绝不重名**的备份文件路径。

    ⚠️ 只精确到「秒」会出事：执行完立刻再跑一次（幂等自检、或误触）会在同一秒内
    生成同名文件 → **覆盖掉真正的备份**，用户就此失去回滚能力（实测被验收测试抓出）。
    故加「已存在则递增序号」。
    """
    base = os.path.join(backup_dir or BACKUP_DIR,
                        f"backfill_{time.strftime('%Y%m%d_%H%M%S')}")
    path = base + ".json"
    i = 1
    while os.path.exists(path):
        path = f"{base}_{i}.json"
        i += 1
    return path


# ============================================================ 纯函数

#: 旧数据兜底：给「有原文行、但缺回链编号」的 item 临时注入的占位编号。
#: 它**只用于触发 `item_confidence` 的「有回链」分支**（进而走关键词匹配），
#: 算完必须移除 —— 绝不能写回库（否则伪造了证据编号）。
_LEGACY_MARK = "#LEGACY"


def _has_evidence(row: dict) -> bool:
    """该行是否有**任何**可用的回链信息（编号）。

    这是「能不能算出有意义的置信度」的判据。实测：`source_run_id=NULL` 的 26 条
    历史人工重评（阶段A 之前写入）**所有 item 都没有 `evidence_ids`** ——
    对它们 `item_confidence` 恒判 `none`(0.0)，`session_confidence` 必然是 0.0，
    从而**必然**触发 `low_confidence`。那不是「置信度低」，而是「输入缺失」，
    写进库只会污染数据（宁缺勿假）。
    """
    for col in ("positive_items", "negative_items"):
        for it in (row.get(col) or []):
            if isinstance(it, dict) and (it.get("evidence_ids")
                                         or it.get("evidence_message_ids")):
                return True
    return False


def _prepare_items(items: list, allow_legacy: bool) -> list:
    """按需给「有原文但无编号」的 item 注入占位编号（仅为触发匹配分支）。"""
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            out.append(it)
            continue
        if (allow_legacy and not (it.get("evidence_ids") or it.get("evidence_message_ids"))
                and it.get("evidence")):
            it = {**it, "evidence_ids": [_LEGACY_MARK]}
        out.append(it)
    return out


def _strip_legacy(items: list) -> list:
    """移除占位编号（保证不把 `#LEGACY` 写回库）。"""
    out = []
    for it in (items or []):
        if isinstance(it, dict) and it.get("evidence_ids") == [_LEGACY_MARK]:
            it = {k: v for k, v in it.items() if k != "evidence_ids"}
        out.append(it)
    return out


def compute_new(row: dict, rules: dict, thresholds: dict, last_grade: str = None,
                allow_legacy: bool = False) -> dict:
    """按**当前生产口径**算出一行该有的新值。**纯函数**（不读库、不写库）。

    返回 ``None`` 表示**不可计算**（缺回链信息，且未开 `allow_legacy`）。

    ⚠️ 必须传「引擎硬指标已合并」的 positive/negative_items —— 库里的
    `positive_items` / `negative_items` 就是当年合并后的结果，直接可用。
    这与 `_score_one` 的调用点一致（见 `confidence.compute` 的注释：传错会漏判）。
    """
    if not _has_evidence(row) and not allow_legacy:
        return None
    res = {
        "positive_items": _prepare_items(row.get("positive_items"), allow_legacy),
        "negative_items": _prepare_items(row.get("negative_items"), allow_legacy),
        "unchecked_rules": row.get("unchecked_rules") or [],
        "invalid_items": row.get("invalid_items") or [],
    }
    score = {
        "raw_score": row.get("raw_score"),
        "positive_total": row.get("positive_total"),
        "negative_total": row.get("negative_total"),
    }
    out = cf.compute(res["positive_items"], res["negative_items"], res, score,
                     {"grade": row.get("grade")}, rules,
                     last_grade=last_grade, thresholds=thresholds)
    n_pos = len(res["positive_items"])
    items = out.get("items") or []
    return {
        "session_confidence": out["session_confidence"],
        "review_reasons": out["review_reasons"],
        "needs_human_review": bool(out["needs_human_review"]),
        # 占位编号必须剔除
        "positive_items": _strip_legacy(items[:n_pos]),
        "negative_items": _strip_legacy(items[n_pos:]),
    }


def diff_of(row: dict, new: dict) -> dict:
    """列出「实际会变」的字段（用于预演报告与断言）。"""
    d = {}
    if row.get("session_confidence") != new["session_confidence"]:
        d["session_confidence"] = (row.get("session_confidence"), new["session_confidence"])
    if (row.get("review_reasons") or None) != (new["review_reasons"] or None):
        d["review_reasons"] = (row.get("review_reasons"), new["review_reasons"])
    if bool(row.get("needs_human_review")) != new["needs_human_review"]:
        d["needs_human_review"] = (bool(row.get("needs_human_review")),
                                   new["needs_human_review"])
    if (row.get("positive_items") or []) != new["positive_items"]:
        d["positive_items"] = ("<items>", "<items+confidence>")
    if (row.get("negative_items") or []) != new["negative_items"]:
        d["negative_items"] = ("<items>", "<items+confidence>")
    return d


# ============================================================ 取数与快照

_SELECT = """SELECT assessment_id, relationship_key, staff_id, session_id, grade,
                    raw_score, positive_total, negative_total,
                    positive_items, negative_items, unchecked_rules, invalid_items,
                    needs_human_review, session_confidence, review_reasons,
                    source_run_id
               FROM customer_intent_assessment"""


def fetch_rows(only_missing: bool = True, limit: int = 0, rel_like: str = None) -> list:
    """取待回填的行。

    · ``only_missing``（默认）只取 `session_confidence IS NULL` → 保证幂等
    · ``rel_like`` 只取关系键以它开头的行 → **定向回填**（也便于测试隔离，
      不必依赖全库现状）
    """
    conds, params = [], {}
    if only_missing:
        conds.append("session_confidence IS NULL")
    if rel_like:
        conds.append("relationship_key LIKE %(rel)s")
        params["rel"] = f"{rel_like}%"
    sql = _SELECT
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY assessment_id ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.query(sql, params) if params else db.query(sql)


def snapshot_last_grade(rows: list) -> dict:
    """一次性算出每行的 `last_grade`（**回填前**状态）。

    必须在**任何写操作之前**调用，否则「边算边写」会让后面的行看到前面的新值 ——
    结果将依赖处理顺序（最隐蔽的一类不确定性，实测踩过类似坑：
    `test_graph_session_parity` 因为水位在同一测试内推进而失败）。
    """
    cache = {}
    out = {}
    for r in rows:
        aid, rel = r["assessment_id"], r["relationship_key"]
        key = (rel, aid)
        if key not in cache:
            row = db.query_one(
                """SELECT grade FROM customer_intent_assessment
                    WHERE relationship_key=%s AND assessment_id < %s
                    ORDER BY assessment_id DESC LIMIT 1""", (rel, aid))
            cache[key] = (row or {}).get("grade")
        out[aid] = cache[key]
    return out


# ============================================================ 执行

def run(apply: bool = False, limit: int = 0, only_missing: bool = True,
        backup_dir: str = None, allow_legacy: bool = False,
        rel_like: str = None, verbose: bool = True) -> dict:
    """预演或执行回填。返回统计字典。

    分两类处理（区别见 `_has_evidence`）：
    · **可算**：有回链信息 → 按生产口径回填全部字段
    · **不可算**：缺回链信息 → 跳过置信度列；但若存在「有负分却 `needs_human_review=False`」
      的逻辑矛盾，做**最小修复**（`has_negative` 这一条 reason 不依赖置信度，可证明正确）
    """
    t0 = time.time()
    rules = rules_mod.load_rules()
    thresholds = ss._confidence_thresholds()
    rows = fetch_rows(only_missing=only_missing, limit=limit, rel_like=rel_like)
    if not rows:
        if verbose:
            print("[backfill] 没有待回填的行（全部已有 session_confidence）")
        return {"total": 0, "computable": 0, "changed": 0, "applied": 0,
                "skipped_no_evidence": 0, "flag_only_fixed": 0,
                "backup": None, "elapsed": 0.0, "diff_summary": {}}

    # ⚠️ 快照必须在写之前 —— 否则链条效应
    last_grades = snapshot_last_grade(rows)

    plan, flag_only, skipped = [], [], []
    summary = {}
    for r in rows:
        new = compute_new(r, rules, thresholds, last_grades.get(r["assessment_id"]),
                          allow_legacy=allow_legacy)
        if new is None:
            skipped.append(r["assessment_id"])
            # ── 不可算行的**自洽性修复**（只依赖事实，不依赖置信度）──────────
            # 为什么值得做：这批行置信度确实补不出来（缺回链编号），但
            # 「要不要人看一眼」与「为什么」是**独立于置信度**的事实，
            # 可以用老判据（有负分 / 有漏检）无损推出，且可证明正确。
            # 不做的话，前端会显示「需复核」却给不出原因 —— 那是可解释性缺口。
            has_neg = bool(r.get("negative_items"))
            has_unchecked = bool(r.get("unchecked_rules"))
            fix = None
            if has_neg and not r.get("needs_human_review"):
                # 类型一：有负分却没标复核 → 矛盾（实测 13 行，且它们**本来就在复核队列里**）
                fix = {"needs_human_review": True, "review_reasons": ["has_negative"]}
            elif has_neg and not r.get("review_reasons"):
                # 类型二：已标复核、有负分，但没有原因 → 补原因（不改 flag）
                fix = {"needs_human_review": True, "review_reasons": ["has_negative"]}
            elif has_unchecked and not r.get("review_reasons") and not r.get("needs_human_review"):
                # 类型三：有漏检却没标复核（同类型一的另一种成因）
                fix = {"needs_human_review": True, "review_reasons": ["unchecked_rules"]}
            if fix and not (bool(r.get("needs_human_review")) == fix["needs_human_review"]
                            and (r.get("review_reasons") or None) == fix["review_reasons"]):
                flag_only.append((r, fix))
            continue
        d = diff_of(r, new)
        if d:
            plan.append((r, new, d))
            for k in d:
                summary[k] = summary.get(k, 0) + 1

    result = {"total": len(rows), "computable": len(rows) - len(skipped),
              "changed": len(plan), "applied": 0,
              "skipped_no_evidence": len(skipped),
              "flag_only_fixed": len(flag_only),
              "backup": None, "elapsed": 0.0, "diff_summary": summary}

    if not apply:
        if verbose:
            _report(rows, plan, summary, applied=False, skipped=skipped,
                    flag_only=flag_only)
        result["elapsed"] = round(time.time() - t0, 2)
        return result

    # ---- 无改动 → 不写备份（避免空文件堆积，也避免误覆盖）----
    if not plan and not flag_only:
        if verbose:
            print("[backfill] 没有需要改动的行（已是最新）")
        result["elapsed"] = round(time.time() - t0, 2)
        return result

    # ---- 备份（写之前）----
    os.makedirs(backup_dir or BACKUP_DIR, exist_ok=True)
    path = _backup_path(backup_dir)
    backup = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "thresholds": thresholds,
        "rule_version": rules.get("rule_version"),
        "allow_legacy": allow_legacy,
        # 只备份「真正会被改」的行（含自洽性修复）
        "rows": [{"assessment_id": r["assessment_id"],
                  "session_confidence": r.get("session_confidence"),
                  "review_reasons": r.get("review_reasons"),
                  "needs_human_review": r.get("needs_human_review"),
                  "positive_items": r.get("positive_items"),
                  "negative_items": r.get("negative_items")}
                 for r, _n, _d in plan] + [
                 {"assessment_id": r["assessment_id"],
                  "session_confidence": r.get("session_confidence"),
                  "review_reasons": r.get("review_reasons"),
                  "needs_human_review": r.get("needs_human_review"),
                  "positive_items": r.get("positive_items"),
                  "negative_items": r.get("negative_items")}
                 for r, _fix in flag_only],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(backup, fh, ensure_ascii=False, indent=1, default=str)
    result["backup"] = path

    # ---- 分批写（每批一个事务）----
    for i in range(0, len(plan), BATCH):
        chunk = plan[i:i + BATCH]
        with db.tx() as conn:
            for r, new, _d in chunk:
                assessment_repo.apply_backfill(
                    conn, r["assessment_id"],
                    session_confidence=new["session_confidence"],
                    review_reasons=db.as_json(new["review_reasons"]),
                    needs_human_review=new["needs_human_review"],
                    positive_items=db.as_json(new["positive_items"]),
                    negative_items=db.as_json(new["negative_items"]))
        result["applied"] += len(chunk)
        if verbose:
            print(f"  已回填 {result['applied']}/{len(plan)}")

    # ---- 自洽性修复（不可算行）----
    if flag_only:
        with db.tx() as conn:
            for r, fix in flag_only:
                assessment_repo.set_review_semantics(
                    conn, r["assessment_id"],
                    needs_human_review=fix["needs_human_review"],
                    review_reasons=db.as_json(fix["review_reasons"]))
        if verbose:
            print(f"  已修复 {len(flag_only)} 行「需复核 / 原因」的自洽性问题")

    result["elapsed"] = round(time.time() - t0, 2)
    if verbose:
        _report(rows, plan, summary, applied=True, backup=path, skipped=skipped,
                flag_only=flag_only)
    return result


def rollback(path: str, verbose: bool = True) -> dict:
    """按备份文件还原（只还原备份里记录的字段）。"""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("rows") or []
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        with db.tx() as conn:
            for r in chunk:
                assessment_repo.apply_backfill(
                    conn, r["assessment_id"],
                    session_confidence=r.get("session_confidence"),
                    review_reasons=(db.as_json(r.get("review_reasons"))
                                    if r.get("review_reasons") else None),
                    needs_human_review=bool(r.get("needs_human_review")),
                    positive_items=db.as_json(r.get("positive_items")),
                    negative_items=db.as_json(r.get("negative_items")))
    if verbose:
        print(f"[backfill] 已回滚 {len(rows)} 行（来源 {path}）")
    return {"rolled_back": len(rows)}


# ============================================================ 报告

def _report(rows, plan, summary, applied, backup=None, skipped=None, flag_only=None):
    skipped = skipped or []
    flag_only = flag_only or []
    print("=" * 66)
    print("历史评估置信度回填" + ("（已执行）" if applied else "（预演，未写库）"))
    print("=" * 66)
    print(f"  待处理行   : {len(rows)}")
    print(f"  可算       : {len(rows) - len(skipped)}")
    print(f"  实际会变   : {len(plan)}")
    print(f"  字段变更数 : {summary or '无'}")
    if skipped:
        print(f"  ⚠️ 跳过（缺 evidence_ids，无法算置信度）: {len(skipped)} 行")
        print("      → 这批是阶段A 之前写的（无证据回链编号）。按生产口径必然算出"
              " conf=0.0 + low_confidence，属**失真**，故不写。")
        print("      → 可用 --legacy-evidence 以「原文行」代替编号兜底计算（非生产口径）")
    if flag_only:
        print(f"  ✔ 另做自洽性修复: {len(flag_only)} 行（flag 与 reasons 不一致 / 缺原因）")
        print("      · 类型一：有负分却 flag=False → 置 true（老判据 `bool(neg or unchecked)` 就该 True）")
        print("      · 类型二：已 flag=True、有负分但 reasons 缺失 → 补 ['has_negative']")
        print("      → 这些 reason **不依赖置信度**，可由事实无损推出，故可在「不回填置信度」时单独修")
    if applied and backup:
        print(f"  备份文件   : {backup}")
        print(f"  回滚命令   : python -m backend.backfill --rollback {backup}")
    if plan and not applied:
        print("\n  样例（前 5 行）：")
        for r, new, _d in plan[:5]:
            print(f"    #{r['assessment_id']} grade={r['grade']} → "
                  f"conf={new['session_confidence']} "
                  f"reasons={new['review_reasons']} review={new['needs_human_review']}")
    print("=" * 66)


# ============================================================ CLI

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in argv
    limit = 0
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    only_missing = "--all" not in argv
    allow_legacy = "--legacy-evidence" in argv
    rel_like = None
    if "--rel" in argv:
        rel_like = argv[argv.index("--rel") + 1]
    if "--rollback" in argv:
        path = argv[argv.index("--rollback") + 1]
        rollback(path)
        return 0
    res = run(apply=apply, limit=limit, only_missing=only_missing,
              allow_legacy=allow_legacy, rel_like=rel_like)
    print(f"\n结果: {res}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
