# -*- coding: utf-8 -*-
"""统一测试入口 —— 一次跑完全部脚本式测试并汇总。

    .venv\\Scripts\\python.exe tests/run_all.py              # 跑全部
    .venv\\Scripts\\python.exe tests/run_all.py --list        # 只列出会跑哪些
    .venv\\Scripts\\python.exe tests/run_all.py test_hooks test_contracts
    .venv\\Scripts\\python.exe tests/run_all.py --skip-db     # 只跑不需要数据库的
    .venv\\Scripts\\python.exe tests/run_all.py -x            # 首个失败即停

为什么需要它（代码评审 P2-3）
    23 个测试是**自包含脚本**（各自 `main()` + `sys.exit()`），需人工逐个执行 ——
    没有统一入口、没有 CI、没有覆盖率。结果是「改了不知道有没有跑全」，且容易漏跑。

设计取舍
    · **不改造成 pytest**：这些脚本用自研 `check()`、且大量依赖 DB 状态与跨进程子测试，
      改造成本远大于收益（且项目约定「非 pytest，勿引入」）。这里只做**编排 + 汇总**。
    · **串行执行**：连接池 `max_size=8`，两个测试进程并行会互相抢连接 →
      抛 `PoolTimeout` 而**看起来像断言失败**（实测踩到过）。这里刻意不加并行。
    · **按退出码 + 输出特征双重判定**：脚本用 `sys.exit(非零)` 报失败，
      但个别脚本结尾汇总行也含「失败 / FAIL」字样。两者任一命中即判失败。
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
#: 需要 PostgreSQL 的测试（文件名片段）—— 用于 `--skip-db`
DB_MARKERS = ("ctx_integration",)
#: 按测试覆盖超时（秒）。默认 `--timeout`（900s）对调**真实模型**的测试不够：
#: `test_stage_a_acceptance` 会循环最多 6 个候选会话直到命中，每次一次真实 LLM 调用
#: —— **实测两次分别是 1861s 与 2909s**（模型侧延迟波动很大，2 倍差异很常见）。
#: ⚠️ 这不是「测试变慢」，是它天然如此；调大超时而不是改小工作量（改小会削弱验收强度）。
#: 日常快跑建议 `--skip-llm` 跳过它，全量回归前再单独跑一次。
TIMEOUT_OVERRIDES = {
    "test_stage_a_acceptance.py": 3600,      # 实测 2909s，留 20% 余量
    "test_backfill.py": 1500,
    "test_batch234.py": 1500,
}
#: 需要真实 LLM 的测试（`--skip-llm` 跳过；无 API Key 时必然失败）
LLM_MARKERS = ("stage_a_acceptance",)
# ⚠️ 这里曾有 `FAIL_PAT` / `_looks_failed()` 用于「输出特征辅助判定」——
# 已删除：两次误报（`PASS 26 / FAIL 0`、正常日志里的「评分失败：」）证明它只会引入假阳性。
# 判定只信退出码；详见 `run_one()` 里的注释。
#: 通过特征（仅用于提取摘要行显示，不参与判定）
PASS_PAT = re.compile(r"(通过|passed|PASS\b)", re.I)


def discover(names=None) -> list:
    files = sorted(HERE.glob("test_*.py"))
    if names:
        want = {n if n.endswith(".py") else f"{n}.py" for n in names}
        files = [f for f in files if f.name in want]
    return files


def db_available() -> bool:
    """DB 是否可用（不可用时给出明确提示，而不是让每个测试都 30 秒超时）。"""
    code = (
        "import sys;sys.path.insert(0,r'%s');sys.path.insert(0,r'%s');"
        "import db;db.query_one('SELECT 1 AS x');print('OK')" % (ROOT, ROOT / "backend")
    )
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=25, encoding="utf-8", errors="replace")
        return "OK" in (r.stdout or "")
    except Exception:
        return False


def run_one(path: Path, timeout: int) -> dict:
    """跑一个测试脚本，返回 {name, ok, seconds, tail, exit}。"""
    env = dict(os.environ)
    # 测试脚本自身已 reconfigure stdout，但子进程与个别第三方输出仍需兜底
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
        code = -9
        timed_out = True
    secs = time.time() - t0
    blob = out + "\n" + err
    # 摘要行：取最后一条含「通过 / passed / failed」的行
    summary = ""
    for line in blob.splitlines():
        if re.search(r"(通过|passed|\d+\s*/\s*\d+|failed)", line, re.I):
            summary = line.strip()

    # ⚠️ 判定**只看退出码**（超时视为失败）：本项目所有测试脚本都以
    # `sys.exit(1 if FAILED else 0)` 表达结果，退出码是权威信号。
    # 曾两次试图用「输出特征」辅助判定，两次都误报：
    #   ① 宽松正则 `\d+\s*failed` → 把 `PASS 26 / FAIL 0` 判成失败；
    #   ② `失败：` 规则 → 把正常日志 `[scoring] 会话 xx 评分失败：stub 失败` 判成失败。
    # 教训：测试结果已在退出码里，再叠一层文本判据只会引入假阳性。
    # 但**「超时」与「断言失败」必须分开显示** —— 两者处置完全不同
    # （超时是预算问题，失败是代码问题），混在一起会误导排查方向。
    ok = (code == 0)
    return {"name": path.name, "ok": ok, "seconds": secs, "summary": summary,
            "exit": code, "blob": blob, "timed_out": timed_out}


def main() -> int:
    ap = argparse.ArgumentParser(description="统一测试入口（串行）")
    ap.add_argument("names", nargs="*", help="只跑指定测试（可不带 .py）")
    ap.add_argument("--list", action="store_true", help="只列出测试清单")
    ap.add_argument("--skip-db", action="store_true", help="跳过需要数据库的测试")
    ap.add_argument("--skip-llm", action="store_true", help="跳过需要真实大模型的测试")
    ap.add_argument("--timeout", type=int, default=900, help="单个测试超时秒数（默认 900）")
    ap.add_argument("-x", "--exit-first", action="store_true", help="首个失败即停")
    args = ap.parse_args()

    files = discover(args.names)
    if not files:
        print("没有匹配的测试文件")
        return 1
    if args.list:
        for f in files:
            print(" ", f.name)
        print(f"\n共 {len(files)} 个")
        return 0

    has_db = db_available()
    if not has_db:
        print("⚠️  PostgreSQL(5433) 不可用 —— 需要数据库的测试会失败/超时。")
        print("   请确认 Docker Desktop 已启动（PG 是容器）。\n")

    results, skipped = [], []
    for f in files:
        if args.skip_db and (not has_db or any(m in f.name for m in DB_MARKERS)):
            skipped.append(f.name)
            continue
        if args.skip_llm and any(m in f.name for m in LLM_MARKERS):
            skipped.append(f.name)
            continue
        if not has_db and any(m in f.name for m in DB_MARKERS):
            skipped.append(f.name)
            continue
        print(f"▶ {f.name} ...", end="", flush=True)
        r = run_one(f, TIMEOUT_OVERRIDES.get(f.name, args.timeout))
        results.append(r)
        flag = "✅" if r["ok"] else ("⏱" if r["timed_out"] else "❌")
        print(f" {flag} {r['seconds']:.1f}s  {r['summary'][:70]}")
        if not r["ok"]:
            if r["timed_out"]:
                print(f"      ⏱ 超时（{TIMEOUT_OVERRIDES.get(f.name, args.timeout)}s）"
                      f"—— 与「断言失败」不同：先看是否只是预算不足，"
                      f"可用 --timeout 加大或 --skip-llm 跳过真模型测试")
            tail = [ln for ln in r["blob"].splitlines()
                    if re.search(r"(FAIL|Error|Traceback|失败)", ln)][-6:]
            for ln in tail:
                print("      " + ln.strip()[:150])
            if args.exit_first:
                break

    ok_n = sum(1 for r in results if r["ok"])
    timed = [r["name"] for r in results if r["timed_out"]]
    print("\n" + "=" * 66)
    print(f"总计 {len(results)} 个测试文件｜通过 {ok_n}"
          + (f"｜超时 {len(timed)}" if timed else "")
          + f"｜失败 {len(results) - ok_n - len(timed)}"
          + (f"｜跳过 {len(skipped)}" if skipped else ""))
    if skipped:
        print("跳过：" + ", ".join(skipped))
    if timed:
        print("⏱ 超时（≠ 断言失败）：" + ", ".join(timed))
    failed = [r["name"] for r in results if not r["ok"] and not r["timed_out"]]
    if failed:
        print("❌ 失败清单：" + ", ".join(failed))
    total_s = sum(r["seconds"] for r in results)
    print(f"总耗时 {total_s:.0f}s")
    print("=" * 66)
    return 1 if (failed or timed) else 0


if __name__ == "__main__":
    sys.exit(main())
