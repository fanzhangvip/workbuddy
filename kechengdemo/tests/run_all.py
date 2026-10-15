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
#: 失败特征（脚本自研 check() 的汇总行 / 明确失败行）
FAIL_PAT = re.compile(r"(失败：|FAIL\s|FAILED|\bFAIL\b.*[1-9]|\d+\s*failed)", re.I)
#: 通过特征（用于「没输出汇总行」时的兜底判定）
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
    try:
        p = subprocess.run([sys.executable, str(path)], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        out, err, code = p.stdout or "", p.stderr or "", p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = (e.stderr or "") if isinstance(e.stderr, str) else ""
        code = -9
    secs = time.time() - t0
    blob = out + "\n" + err
    # 汇总行优先：取最后一条含「通过 / passed / failed」的行作为摘要
    summary = ""
    for line in blob.splitlines():
        if re.search(r"(通过|passed|\d+\s*/\s*\d+|failed)", line, re.I):
            summary = line.strip()
    # 判定：退出码非零 → 失败；否则再看是否有「明确的失败行」
    ok = (code == 0) and not re.search(r"(^|\s)(FAIL|FAILED)\s", blob, re.M)
    return {"name": path.name, "ok": ok, "seconds": secs, "summary": summary,
            "exit": code, "blob": blob}


def main() -> int:
    ap = argparse.ArgumentParser(description="统一测试入口（串行）")
    ap.add_argument("names", nargs="*", help="只跑指定测试（可不带 .py）")
    ap.add_argument("--list", action="store_true", help="只列出测试清单")
    ap.add_argument("--skip-db", action="store_true", help="跳过需要数据库的测试")
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
        if not has_db and any(m in f.name for m in DB_MARKERS):
            skipped.append(f.name)
            continue
        print(f"▶ {f.name} ...", end="", flush=True)
        r = run_one(f, args.timeout)
        results.append(r)
        flag = "✅" if r["ok"] else "❌"
        print(f" {flag} {r['seconds']:.1f}s  {r['summary'][:70]}")
        if not r["ok"]:
            tail = [ln for ln in r["blob"].splitlines()
                    if re.search(r"(FAIL|Error|Traceback|失败)", ln)][-6:]
            for ln in tail:
                print("      " + ln.strip()[:150])
            if args.exit_first:
                break

    ok_n = sum(1 for r in results if r["ok"])
    print("\n" + "=" * 66)
    print(f"总计 {len(results)} 个测试文件｜通过 {ok_n}｜失败 {len(results) - ok_n}"
          + (f"｜跳过 {len(skipped)}" if skipped else ""))
    if skipped:
        print("跳过：" + ", ".join(skipped))
    failed = [r["name"] for r in results if not r["ok"]]
    if failed:
        print("失败清单：" + ", ".join(failed))
    total_s = sum(r["seconds"] for r in results)
    print(f"总耗时 {total_s:.0f}s")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
