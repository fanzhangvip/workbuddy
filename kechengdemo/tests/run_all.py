# -*- coding: utf-8 -*-
"""统一测试入口 —— 按**依赖分层**跑，默认只跑便宜的两层。

    .venv\\Scripts\\python.exe tests/run_all.py                  # 默认 t0+t1（目标 ≤1 分钟）
    .venv\\Scripts\\python.exe tests/run_all.py --t2              # 加上真实外部（真模型/真通道）
    .venv\\Scripts\\python.exe tests/run_all.py --tier t0        # 只跑纯逻辑（≤5 秒）
    .venv\\Scripts\\python.exe tests/run_all.py --changed backend/scoring_context.py
    .venv\\Scripts\\python.exe tests/run_all.py --list           # 列出会跑哪些（含分层）
    .venv\\Scripts\\python.exe tests/run_all.py test_hooks -x    # 只跑指定测试，首个失败即停

## 为什么分层（2026-10-16 逐个实测）

| 层 | 依赖 | 文件数 | 实测 |
|---|---|---|---|
| t0 | 无（不碰 PG / 网络） | 8 | 合计 5.5s |
| t1 | 本地 PostgreSQL 5433 | 27 | 合计 530s |
| t2 | 真实外部（真模型 / 真微伴企微 / Chrome） | 1 | **36.5s**（2026-10-16 实测） |

合计一轮约 10 分钟 —— **改一次跑一轮，"半天"就是这么没的**；而其中约 480 秒
还是连接池退出白等（已在 `db.close_pool()` 修掉，见该函数注释）。分层后：
默认一轮 ≈1 分钟，重活显式开。

> ⚠️ **关于 t2 的那个旧数字**：这里曾记着 `test_stage_a_acceptance` "实测 1861s / 2909s
> （31~48 分钟）"。2026-10-16 复核后**判定该数字不可复现**：该测试的墙钟 = 候选数
> （≤6）× 每次真实调用的延迟，而历次真实跑分里**每次会话恰好 1 次调用、16~30 秒/会话**
> （`llm_calls == session_done`，零失败）→ 6 个候选最坏也就 3 分钟。实测复跑：单次判定
> **11.6s**、整个测试 **36.5s**。那个数字应是当时模型侧极慢所致，而非测试本身重。
> 测试现在会逐候选打印耗时，下次再慢能直接定位。

## 四条设计取舍

1. **不改造成 pytest**：脚本用自研 `check()` + 退出码判定，改造收益小于成本（项目约定）。
2. **层级声明写在测试文件里**（`TIER = "t0|t1|t2"`）：名单写在本文件里就会漂 ——
   改造前 `DB_MARKERS = ("ctx_integration",)` 只匹配 1 个文件，而实际需要 DB 的有 27 个，
   `--skip-db` 形同虚设。**忘声明 TIER 的文件直接拒绝执行**：默认拒绝比默认放过安全
   （漏登记 = 悄悄不跑，那比跑得慢危险得多）。
3. **串行执行**：t1 里会启动批量任务的几个测试之间有互斥闸
   （`job_service.running_kinds()`），并行会互相挡住；且连接池 `max_size=8`，
   两进程并行曾触发 `PoolTimeout` 被误读成"断言失败"。
4. **超时按层给**（t0 15s / t1 120s / t2 900s）：挂住一个只花 15 秒，
   而不是 `--timeout` 默认 900 秒的十五分钟。t2 的 3600s 是给
   `test_stage_a_acceptance` 留的（循环最多 6 个候选会话、每次一次真实模型调用，
   最坏 ≈6 × 2 次调用 × 30 秒；实测一轮 36.5 秒）。

## 判定只信退出码

本项目所有测试脚本都以 `sys.exit(1 if FAILED else 0)` 表达结果。曾两次试图用输出特征
辅助判定，两次都误报（把 `PASS 26 / FAIL 0` 判成失败、把正常日志里的"评分失败："判成失败），
已删除。**「超时」与「断言失败」在展示上必须分开**：前者是预算问题，后者是代码问题。
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# 复用 `_harness` 的 stdout 编码修正（它 import 时会把 stdout/stderr 切成 utf-8）。
# ⚠️ 不 import 的话，本文件打印的 `▶`（U+25B6）在 Windows 默认 GBK 控制台下会
#    `UnicodeEncodeError` —— **一键回归入口自己先崩**，比被测代码的失败更难排查。
sys.path.insert(0, str(HERE))
import _harness  # noqa: E402,F401  仅为其导入副作用：统一输出编码
import _map      # noqa: E402  改动 → 测试 的映射（`--changed` 用）

#: 分层的默认超时（秒）。`--timeout` 可整体覆盖。
TIER_TIMEOUT = {"t0": 15, "t1": 120, "t2": 900}
#: 默认跑哪些层
DEFAULT_TIERS = ("t0", "t1")

_TIER_RE = re.compile(r'^TIER\s*=\s*["\'](t[012])["\']', re.M)


def read_tier(path: Path):
    """从测试文件里读它的分层声明；没有声明返回 None。"""
    m = _TIER_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else None


def discover(names=None) -> tuple:
    """返回 (可跑的 [(path, tier)], 未声明 TIER 的文件名)。"""
    files = sorted(HERE.glob("test_*.py"))
    if names:
        want = {n if n.endswith(".py") else f"{n}.py" for n in names}
        files = [f for f in files if f.name in want]
    ok, undeclared = [], []
    for f in files:
        t = read_tier(f)
        (ok.append((f, t)) if t else undeclared.append(f.name))
    return ok, undeclared


def db_available() -> bool:
    """DB 是否可用（不可用时给出明确提示，而不是让每个测试都超时）。"""
    code = (
        "import sys;sys.path.insert(0,r'%s');sys.path.insert(0,r'%s');"
        "import db;db.query_one('SELECT 1 AS x');print('OK')" % (ROOT, ROOT / "backend")
    )
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=25, encoding="utf-8", errors="replace")
        return "OK" in (r.stdout or "")
    except Exception:                          # noqa: BLE001
        return False


def run_one(path: Path, timeout: int) -> dict:
    """跑一个测试脚本，返回 {name, ok, seconds, summary, exit, timed_out}。"""
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    t0 = time.time()
    timed_out = False
    try:
        p = subprocess.run([sys.executable, str(path)], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        out, err, code = p.stdout or "", p.stderr or "", p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = (e.stderr or "") if isinstance(e.stderr, str) else ""
        code, timed_out = -9, True
    secs = time.time() - t0
    blob = out + "\n" + err
    summary = ""
    for line in blob.splitlines():
        if re.search(r"(通过|passed|\d+\s*/\s*\d+|failed)", line, re.I):
            summary = line.strip()
    return {"name": path.name, "ok": code == 0, "seconds": secs, "summary": summary,
            "exit": code, "blob": blob, "timed_out": timed_out}


def main() -> int:
    ap = argparse.ArgumentParser(description="统一测试入口（按依赖分层）")
    ap.add_argument("names", nargs="*", help="只跑指定测试（可不带 .py）")
    ap.add_argument("--list", action="store_true", help="只列出测试清单（含分层）")
    ap.add_argument("--tier", default="", help=f"跑哪些层，逗号分隔（默认 {','.join(DEFAULT_TIERS)}）")
    ap.add_argument("--t2", "--full", action="store_true", dest="t2",
                    help="加上 t2（真实外部依赖，慢且可能花钱）")
    ap.add_argument("--changed", nargs="+", default=None, metavar="FILE",
                    help="只跑与被改文件相关的测试（映射见 tests/_map.py）")
    ap.add_argument("--skip-db", action="store_true", help="等价于 --tier t0（保留旧写法）")
    ap.add_argument("--skip-llm", action="store_true", help="跳过 t2（现在已是默认行为）")
    ap.add_argument("--timeout", type=int, default=0, help="覆盖该层的默认超时")
    ap.add_argument("-x", "--exit-first", action="store_true", help="首个失败即停")
    args = ap.parse_args()

    if args.tier:
        tiers = tuple(t.strip().lower() for t in args.tier.split(",") if t.strip())
    elif args.skip_db:
        tiers = ("t0",)
    else:
        tiers = DEFAULT_TIERS
    if args.t2:
        tiers = tuple(sorted(set(tiers) | {"t2"}))
    bad = [t for t in tiers if t not in TIER_TIMEOUT]
    if bad:
        print(f"未知分层：{bad}（只能是 t0 / t1 / t2）")
        return 1

    names = args.names
    if args.changed:
        picked = _map.tests_for([Path(p).name for p in args.changed])
        if picked is None:
            unk = _map.unknown_files([Path(p).name for p in args.changed])
            print(f"⚠️  这些文件不在 `tests/_map.py` 里：{', '.join(unk)}")
            print("   → 按「宁可多跑，不可漏跑」处理：退回全量（t0+t1）\n")
        else:
            names = picked
            print(f"改了 {', '.join(Path(p).name for p in args.changed)}"
                  f" → 只跑 {len(picked)} 个相关测试\n")

    all_files, undeclared = discover(names)
    if undeclared:
        print("❌ 以下测试文件没有声明分层（文件里缺一行 `TIER = \"t0|t1|t2\"`）：")
        for n in undeclared:
            print("   " + n)
        print("   这是**拒绝执行**而不是警告：漏登记 = 悄悄不跑，比跑得慢危险得多。")
        return 1
    if not all_files:
        print("没有匹配的测试文件")
        return 1

    if args.list:
        for f, t in all_files:
            flag = "" if t in tiers else "  （本次不跑）"
            print(f"  {t}  {f.name}{flag}")
        n = sum(1 for _, t in all_files if t in tiers)
        print(f"\n共 {len(all_files)} 个；本次会跑 {n} 个（层：{','.join(tiers)}）")
        return 0

    selected = [(f, t) for f, t in all_files if t in tiers]
    skipped_tier = [f.name for f, t in all_files if t not in tiers]
    if not selected:
        print(f"这些文件都不在本次层（{','.join(tiers)}）里。用 --list 看全量。")
        return 1

    has_db = db_available()
    if not has_db and any(t == "t1" for _, t in selected):
        print("⚠️  PostgreSQL(5433) 不可用 —— t1 会失败/超时。")
        print("   请确认 Docker Desktop 已启动（PG 是容器）。\n")

    results, skipped = [], list(skipped_tier)
    for f, tier in selected:
        if not has_db and tier == "t1":
            skipped.append(f.name)
            continue
        timeout = args.timeout or TIER_TIMEOUT[tier]
        print(f"▶ [{tier}] {f.name} ...", end="", flush=True)
        r = run_one(f, timeout)
        r["tier"] = tier
        results.append(r)
        flag = "✅" if r["ok"] else ("⏱" if r["timed_out"] else "❌")
        print(f" {flag} {r['seconds']:.1f}s  {r['summary'][:70]}")
        if not r["ok"]:
            if r["timed_out"]:
                print(f"      ⏱ 超时（{timeout}s，层 {tier}）—— 与「断言失败」不同："
                      f"先看是否只是预算不足（`--timeout` 加大，或确认它该不该在这一层）")
            tail = [ln for ln in r["blob"].splitlines()
                    if re.search(r"(FAIL|Error|Traceback|失败)", ln)][-6:]
            for ln in tail:
                print("      " + ln.strip()[:150])
            if args.exit_first:
                break

    ok_n = sum(1 for r in results if r["ok"])
    timed = [r["name"] for r in results if r["timed_out"]]
    failed = [r["name"] for r in results if not r["ok"] and not r["timed_out"]]
    print("\n" + "=" * 66)
    print(f"总计 {len(results)} 个｜通过 {ok_n}"
          + (f"｜超时 {len(timed)}" if timed else "")
          + f"｜失败 {len(failed)}" + (f"｜跳过 {len(skipped)}" if skipped else ""))
    for tier in tiers:
        rs = [r for r in results if r["tier"] == tier]
        if rs:
            print(f"  {tier}: {len(rs)} 个，{sum(r['seconds'] for r in rs):.1f}s")
    if skipped:
        print("跳过：" + ", ".join(skipped))
    if timed:
        print("⏱ 超时（≠ 断言失败）：" + ", ".join(timed))
    if failed:
        print("❌ 失败清单：" + ", ".join(failed))
    print(f"总耗时 {sum(r['seconds'] for r in results):.0f}s")
    print("=" * 66)
    return 1 if (failed or timed) else 0


if __name__ == "__main__":
    sys.exit(main())
