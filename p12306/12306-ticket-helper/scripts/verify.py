"""验证总入口 —— 一次跑完 V0–V7，输出一张**验证报告**。

设计依据：``docs/测试验证框架设计.md`` §5。

用法::

    python scripts/verify.py                    # V0–V6（V7 需 --device）
    python scripts/verify.py --profile fast     # V0+V2+V3（提交前用）
    python scripts/verify.py --level V0         # 只跑一层
    python scripts/verify.py --update-baseline  # 重建弱断言棘轮基线
    python scripts/verify.py --device           # 附带提示 V7 该采哪些信号

为什么需要这个入口
------------------
1. **本机跑不完全量**：全量 pytest 约 40s 会被回收 → 必须**分批跑**（每批 2–3 个文件）
   + 每组独立进程 + `--junitxml` 汇总。
2. **光跑测试不够**：这个项目的两次事故（405、6 段）都发生在「测试全绿」的情况下。
   所以入口要同时跑 **变异检验 / 差分表 / 协议基线 / 溯源校验**，
   并把「**未验证的声明**」列在最显眼的位置。
3. **报告要能防止误读**：mock 端到端那一层必须带免责句（它的效度有界）。

退出码 0 = 所有被选中的层都通过。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

PY = sys.executable
REPORT_DIR = BASE / ".verify"

#: 分层定义。`tests` 走 pytest（分批），`scripts` 直接跑（判据是退出码）。
LEVELS: dict[str, dict[str, object]] = {
    "V0": {
        "title": "静态纪律门",
        "tests": ["tests/test_discipline.py"],
        "scripts": [
            (["scripts/build_vendor_pjialin.py", "--check"], "vendor/pjialin 溯源"),
            (["scripts/build_vendor_testerSunshine.py", "--check"], "vendor/testerSunshine 溯源"),
            (["scripts/check_packaging.py", "--quiet"], "打包完整性（上游可达依赖 × spec 对账）"),
        ],
        "note": "分层依赖方向 / 单一真源 / 无 skip / 无弱断言增长 / 行尾 / 适配层无协议细节",
    },
    "V1": {
        "title": "冻结回归（**只证明自洽，不得作为正确性依据**）",
        "tests": "ALL_OTHERS",
        "scripts": [],
        "note": "全量单元 + 集成，分批跑",
    },
    "V2": {
        "title": "契约测试 + 能力可用性证明（F4）",
        "tests": [
            "tests/test_contracts.py",
            "tests/test_kernel_facade.py",
            "tests/test_vendor_pjialin.py",
            "tests/test_kernel_station.py",
            "tests/test_kernel_captcha.py",
            "tests/test_kernel_query.py",
            "tests/test_kernel_user.py",
            "tests/test_kernel_qr.py",
            "tests/test_kernel_order.py",
            "tests/test_kernel_slide.py",
            "tests/test_kernel_pacing.py",
            "tests/test_captcha_endpoint.py",
            "tests/test_capability_witness.py",
            "tests/test_verify_tool.py",
            "tests/test_packaging.py",
        ],
        "scripts": [],
        "note": "命名一次全局引用 / 终态不可转出 / 注释即测试；F4：请求真的发出去了吗",
    },
    "V3": {
        "title": "变异检验（撤掉实现必须变红）",
        "tests": [],
        "scripts": [(["scripts/contract_mutation_check.py"], "全部变异")],
        "note": "让 V1/V2 可信的唯一手段",
    },
    "V4": {
        "title": "差分验证（双实现逐字段比对）",
        "tests": [],
        "scripts": [(["scripts/kernel_diff.py"], "候补请求体差异表")],
        "note": "**只说明差在哪，不说明谁对**",
    },
    "V5": {
        "title": "协议基线（外部权威）",
        "tests": [
            "tests/test_protocol_baseline.py",
            "tests/test_query_baseline.py",
            "tests/test_user_baseline.py",
            "tests/test_qr_baseline.py",
            "tests/test_order_baseline.py",
            "tests/test_pacing_baseline.py",
        ],
        "scripts": [],
        "note": (
            "路径级 + **参数级**：每条路径/字段下标必须有上游出处或写明证据。"
            "参数级锚点来自 vendor/pjialin/ticket_facts.py（从上游原件逐字切出）"
        ),
    },
    "V6": {
        "title": "mock 端到端（**效度有界**）",
        "tests": ["tests/test_e2e.py", "tests/test_standby.py"],
        "scripts": [],
        "note": "mock 由本项目编写 → 只证明流程自洽",
    },
}

V6_DISCLAIMER = (
    "⚠️ V6 免责：mock 由本项目编写，它的字段名与格式来自我们的假设，"
    "**无法证明 12306 真的接受这些请求**。本项目两次事故（405 路径整族写错、"
    "6 段格式自证）都发生在这一层全绿的情况下。"
)

FAST_PROFILE = ("V0", "V2", "V3")

#: 每批几个测试文件（本机全量会被回收，必须分批）
BATCH_SIZE = 3


def all_test_files() -> list[str]:
    return sorted(
        p.relative_to(BASE).as_posix()
        for p in (BASE / "tests").rglob("test_*.py")
        if "__pycache__" not in p.parts
    )


#: pytest 收尾摘要里的计数（`83 passed, 1 failed in 12.3s`）
_SUMMARY_ITEM = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")


def parse_pytest_summary(stdout: str) -> dict[str, int] | None:
    """从 pytest 的收尾摘要里取计数；取不到返回 ``None``。

    只在「junitxml 写不出来」的兜底路径上用。**不猜**：一行里一个计数都没有就返回 None，
    让调用方把它记成错误，而不是记成 0 通过（那会把「崩溃」伪装成「全绿」）。
    """
    for line in reversed((stdout or "").strip().splitlines()[-8:]):
        found = _SUMMARY_ITEM.findall(line)
        if not found:
            continue
        out = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        for value, kind in found:
            key = "errors" if kind in ("error", "errors") else kind
            out[key] = int(value)
        return out
    return None


def classify_batch_outcome(
    *, returncode: int, has_xml: bool, failures: int, errors: int
) -> str:
    """把「一批跑完」归成四类之一。★ 它们的处置**完全不同**，所以必须分开。

    - ``ok``            测试真的全过，退出码 0；
    - ``tests_failed``  XML 里读得到失败/错误数；
    - ``exit_anomaly``  **测试全过（XML 0 失败 0 错误）但退出码非 0**。
      本机实测的成因：pytest 在**会话收尾**时清理老的
      ``%TEMP%/pytest-of-*/pytest-N/`` 目录（一次几百个文件），
      被沙箱的**批量删除保护**拦下 → 收尾报错 → 退出码 1。
      当成「失败」会**误伤**（同批别的文件的结果一起丢）；
      当成「通过」又会**掩盖**真实的收尾问题 —— 所以单列一类，
      并且**必须把原始输出打出来**，让人自己看它为什么非 0。
    - ``crashed``       连 XML 都没写出来（进程中途死了）。
    """
    if failures or errors:
        return "tests_failed"
    if returncode == 0:
        return "ok"
    return "exit_anomaly" if has_xml else "crashed"


def run_pytest(files: list[str], tag: str) -> dict:
    """分批跑 pytest，用 junitxml 汇总（本机 stdout 摘要有时被吞）。

    ⚠️ **兜底：junitxml 写不出来时重跑一遍（不带 junitxml）从摘要取计数。**
    进程中途死掉 → xml 没有 → 同批**另两个文件的结果一起丢**。
    重跑一次不改变覆盖范围（还是那三个文件），只是让报告仍然完整。
    判据是「有没有 xml」，不是「退出码非 0」—— 收集期报错也会写出 xml。

    ⚠️ 2026-09-30 更正：原先这里写「带 `--junitxml` 会原生崩溃（access violation）」——
    **那个结论是错的**。连跑多轮 + 解析 junitxml 之后真相是：**85 项全过、
    XML 里 0 失败 0 错误，但退出码 1**，原因是 pytest 收尾清理临时目录被沙箱拦下
    （见 `classify_batch_outcome`）。**退出码受环境影响，不能用它判断测试结果。**
    """
    passed = failed = errors = skipped = 0
    failed_batches: list[str] = []
    retried_batches: list[str] = []
    #: 「测试全过但退出码非 0」的批次（环境造成的收尾报错）—— 单列，不算失败
    exit_code_anomalies: list[str] = []
    for i in range(0, len(files), BATCH_SIZE):
        batch = files[i : i + BATCH_SIZE]
        xml = REPORT_DIR / f"{tag}-{i // BATCH_SIZE}.xml"
        cmd = [PY, "-m", "pytest", *batch, "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"]
        proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True)
        batch_fail = batch_err = 0
        if xml.exists():
            suite = ET.parse(xml).getroot()
            suite = suite.find("testsuite") if suite.tag != "testsuite" else suite
            batch_fail = int(suite.get("failures", 0))
            batch_err = int(suite.get("errors", 0))
            passed += int(suite.get("tests", 0)) - batch_fail - batch_err - int(suite.get("skipped", 0))
            failed += batch_fail
            errors += batch_err
            skipped += int(suite.get("skipped", 0))
        else:
            # 兜底路径：xml 没写出来 → 进程很可能是**原生崩溃**（不是测试失败）。
            # 重跑一遍不带 junitxml，从摘要取计数，保住同批另几个文件的结果。
            batch_label = ", ".join(batch)
            retry = subprocess.run(
                [PY, "-m", "pytest", *batch, "-q", "-p", "no:cacheprovider"],
                cwd=str(BASE),
                capture_output=True,
                text=True,
            )
            counts = parse_pytest_summary(retry.stdout)
            retried_batches.append(batch_label)
            if counts is None:
                # 两次都拿不到摘要（确实崩了）→ 记**错误**，不要记成 0 通过
                errors += 1
                failed_batches.append(batch_label)
                print(f"  ⚠️ 批次无 junitxml 且摘要不可读（疑似原生崩溃）：{batch_label}")
            else:
                passed += counts["passed"]
                failed += counts["failed"]
                errors += counts["errors"]
                skipped += counts["skipped"]
                print(
                    f"  ⚠️ 批次无 junitxml，已改用 stdout 摘要：{batch_label}"
                    f" → passed={counts['passed']} failed={counts['failed']} errors={counts['errors']}"
                )
                if counts["failed"] or counts["errors"]:
                    failed_batches.append(batch_label)
        outcome = classify_batch_outcome(
            returncode=proc.returncode,
            has_xml=xml.exists(),
            failures=batch_fail,
            errors=batch_err,
        )
        if outcome == "tests_failed":
            failed_batches.append(", ".join(batch))
        elif outcome in ("exit_anomaly", "crashed"):
            tail = (proc.stdout or proc.stderr or "").strip().splitlines()
            why = tail[-1][:160] if tail else "(无输出)"
            label = "测试全过但退出码非 0" if outcome == "exit_anomaly" else "进程中途死掉（无 junitxml）"
            exit_code_anomalies.append(f"{', '.join(batch)}  ← {label}：{why}")
    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "failed_batches": failed_batches,
        "retried_batches": retried_batches,
        "exit_code_anomalies": exit_code_anomalies,
        "ok": not failed_batches,
    }


def run_script(cmd: list[str]) -> tuple[bool, str]:
    proc = subprocess.run([PY, *cmd], cwd=str(BASE), capture_output=True, text=True)
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()
    return proc.returncode == 0, (tail[-1] if tail else "")


def load_manifest() -> list[dict]:
    path = BASE / "verification_manifest.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("claims", [])


def unverified_claims(claims: list[dict]) -> list[dict]:
    """只有 V1/V6（或没有）证据的声明 —— 它们**不得**被写成「已正确」。"""
    weak_levels = {"V1", "V6", "none", ""}
    out = []
    for c in claims:
        levels = {e.split(":")[0].strip() for e in c.get("evidence", [])}
        strong = levels - weak_levels
        if not strong:
            out.append(c)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="验证总入口")
    parser.add_argument("--level", default="", help="只跑某一层，如 V0")
    parser.add_argument("--profile", default="", choices=["", "fast"], help="fast = V0+V2+V3")
    parser.add_argument("--update-baseline", action="store_true", help="重建弱断言棘轮基线")
    parser.add_argument("--device", action="store_true", help="提示 V7 需要采集哪些真机信号")
    args = parser.parse_args()

    # **必须清空报告目录**：否则上一批的 junitxml 会被这一批读到，
    # 出现「批次失败但统计显示 0 失败」这种自相矛盾的报告
    # （首轮实测就撞上了：失败批次列了一串，计数却是 0/0）。
    REPORT_DIR.mkdir(exist_ok=True)
    for stale in REPORT_DIR.glob("*.xml"):
        stale.unlink()

    if args.update_baseline:
        from tests.test_discipline import BASELINE_FILE, build_baseline  # noqa: PLC0415

        data = build_baseline()
        BASELINE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✔ 已更新弱断言基线：{data['total']} 条 → {BASELINE_FILE.relative_to(BASE)}")
        return 0

    selected = [args.level] if args.level else list(FAST_PROFILE if args.profile == "fast" else LEVELS)

    results: dict[str, dict] = {}
    every = all_test_files()

    for level in selected:
        spec = LEVELS.get(level)
        if spec is None:
            print(f"未知的层：{level}")
            return 2
        files = spec["tests"]
        if files == "ALL_OTHERS":
            owned = {f for lv in LEVELS.values() for f in (lv["tests"] if lv["tests"] != "ALL_OTHERS" else [])}
            files = [f for f in every if f not in owned]
        entry: dict = {"title": spec["title"], "note": spec["note"]}
        ok = True
        if files:
            res = run_pytest(list(files), level)
            entry["pytest"] = res
            ok = ok and res["ok"]
            # V1 是「只证明自洽」的那层，标注清楚
            if level == "V1":
                entry["green_only_means"] = "自洽"
        for cmd, label in spec["scripts"]:
            passed, tail = run_script(cmd)
            entry.setdefault("scripts", []).append({"label": label, "ok": passed, "tail": tail})
            ok = ok and passed
        entry["ok"] = ok
        results[level] = entry

    # ---------------- 报告 ----------------
    print("=" * 78)
    print("验证报告")
    print("=" * 78)
    for level, entry in results.items():
        mark = "✅" if entry["ok"] else "❌"
        line = f"{level} {entry['title']:<44} {mark}"
        if entry.get("pytest"):
            p = entry["pytest"]
            line += f"  {p['passed']} 通过 / {p['failed']} 失败 / {p['errors']} 错误"
            if p["skipped"]:
                line += f" / {p['skipped']} 跳过"
        print(line)
        if entry["note"]:
            print(f"      {entry['note']}")
        if entry.get("green_only_means") == "自洽":
            print("      ⚠️ 本层绿色**只证明自洽**，不构成功能正确的证据")
        for s in entry.get("scripts", []):
            print(f"      {'✔' if s['ok'] else '✘'} {s['label']}：{s['tail'][:70]}")
        for b in entry.get("pytest", {}).get("retried_batches", []):
            print(f"      ⚠️ 该批次无 junitxml，已改用 stdout 摘要取计数：{b}")
        for item in entry.get("pytest", {}).get("exit_code_anomalies", []):
            print(f"      ⚠️ 退出码异常（XML 里 0 失败，原文如下）→ {item}")
        if entry.get("pytest", {}).get("failed_batches"):
            for b in entry["pytest"]["failed_batches"]:
                print(f"      ✘ 失败批次：{b}")

    if "V6" in results:
        print("\n" + V6_DISCLAIMER)

    # ---------------- 未验证声明 + 未决问题 ----------------
    claims = load_manifest()
    weak = unverified_claims(claims)
    print("\n" + "-" * 78)
    print(f"未验证声明（证据里没有 V4/V5/V7 的）：{len(weak)} 条")
    for c in weak:
        print(f"  · {c['subject']}：{c['claim']}")
        for q in c.get("open_questions", []):
            print(f"      ↳ 未决：{q}")

    if args.device:
        print("\nV7 真机探针：需人工触发，采集后写入 data/evidence/")
        print("  1. 候补链路 B 级信号：见 TicketApi._log_hb_evidence 的 INFO 日志")
        print("     （官方车次号 vs 本地 / jzParam / passengerInitApi 返回键）")
        print("  2. 脚步：python scripts/diagnose_standby.py --until-confirm（写操作前停）")
        print("  3. 下单链路：python scripts/diagnose_order.py --restore --task N（默认只到 initDc：锁票不建单）")

    print("-" * 78)
    bad = [lv for lv, e in results.items() if not e["ok"]]
    if bad:
        print(f"✘ 未通过：{', '.join(bad)}")
        return 1
    print("✔ 选中的层全部通过")
    print("\n⚠️ 提醒：「全部通过」**不等于**「功能正确」。")
    print("   正确与否取决于上面「未验证声明」是否还有剩。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
