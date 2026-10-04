"""`scripts/verify.py` 自身的测试（**产出证据的那个工具，也要有证据**）。

为什么值得单独一个文件
----------------------
`verify.py` 是「分层验证」的总入口，它的报告是**给人看的判据**。
而它此前有个真实的坑：本机 `test_desktop.py` 带 `--junitxml` 会**原生崩溃**
（Windows access violation），进程直接死掉 → xml 写不出来 →
那一批的计数是 0，而且**同批另两个文件的结果一起丢**。

更糟的是「写不出来」当时被当成「错误 +1」，报告上看起来像「有一批跑挂了」，
而不是「有一批**没跑**」—— 两者该采取的行动完全不同。

⚠️ 本文件**不真跑 pytest**：用假的 subprocess 结果驱动分支。
真跑一次桌面端要 6 分钟，而且它崩不崩是本机玄学 —— 那样的测试不可信。
"""

from __future__ import annotations

import importlib.util
import shutil
import textwrap
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, BASE / relative)
    module = importlib.util.module_from_spec(spec)
    assert hasattr(spec.loader, "exec_module"), relative
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def discipline():
    """V0 纪律门模块（独立加载：它自己也会被本文件当被测对象用）。"""
    return _load("discipline_probe", "tests/test_discipline.py")


@pytest.fixture()
def verify():
    return _load("verify_tool", "scripts/verify.py")


class _FakeProc:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_parse_pytest_summary_reads_the_tail(verify) -> None:
    """摘要解析：正常 / 有失败 / 崩溃输出（必须返回 None，不许猜成 0）。"""
    assert verify.parse_pytest_summary("....\n===== 83 passed in 224.44s (0:03:44) =====") == {
        "passed": 83, "failed": 0, "errors": 0, "skipped": 0,
    }
    assert verify.parse_pytest_summary("===== 80 passed, 2 failed, 1 error, 3 skipped in 12.3s =====") == {
        "passed": 80, "failed": 2, "errors": 1, "skipped": 3,
    }
    assert verify.parse_pytest_summary(  # noqa: WEAK-ASSERT - None 是本函数的契约值
        "Windows fatal exception: access violation\nThread 0x28:"
    ) is None
    assert verify.parse_pytest_summary("") is None  # noqa: WEAK-ASSERT - 同上


def test_crashed_batch_is_retried_without_junitxml(verify, monkeypatch, tmp_path) -> None:
    """★ 防的是：**原生崩溃那一批的结果整批丢失**（同批另两个文件也一起丢）。

    第一次调用（带 `--junitxml`）模拟「进程被 access violation 打死」：没有 xml、没有摘要；
    第二次（不带 junitxml）返回正常摘要 —— 报告应当取到后者的计数，并记下「这一批重试过」。
    """
    calls: list[str] = []

    def _fake_run(cmd, **kwargs):  # noqa: ANN001,ANN003
        joined = " ".join(str(c) for c in cmd)
        calls.append(joined)
        if "--junitxml" in joined:
            return _FakeProc(139, "Windows fatal exception: access violation\n")
        return _FakeProc(0, "....\n===== 5 passed in 1.0s =====\n")

    monkeypatch.setattr(verify.subprocess, "run", _fake_run)
    monkeypatch.setattr(verify, "REPORT_DIR", tmp_path)  # 保证 xml 永远不存在
    result = verify.run_pytest(["tests/test_demo_a.py", "tests/test_demo_b.py"], "probe")

    assert len(calls) == 2, calls
    assert "--junitxml" not in calls[1], "重试必须去掉 junitxml"
    assert result["passed"] == 5, result
    assert result["failed"] == 0 and result["errors"] == 0
    assert result["retried_batches"] == ["tests/test_demo_a.py, tests/test_demo_b.py"]
    assert result["ok"] is True


def test_unreadable_summary_is_an_error_not_zero_passed(verify, monkeypatch, tmp_path) -> None:
    """★ 防的是：把「两次都拿不到摘要」记成**0 通过、0 失败**（= 悄悄全绿）。

    拿不到摘要只可能是进程没跑完 —— 那必须记成错误并进 `failed_batches`，
    否则报告会说「一切正常」而实际上有一批根本没执行。
    """
    def _fake_run(cmd, **kwargs):  # noqa: ANN001,ANN003
        return _FakeProc(139, "Windows fatal exception: access violation\n")

    monkeypatch.setattr(verify.subprocess, "run", _fake_run)
    monkeypatch.setattr(verify, "REPORT_DIR", tmp_path)
    result = verify.run_pytest(["tests/test_demo_c.py"], "probe")

    assert result["passed"] == 0
    assert result["errors"] == 1, result
    assert result["ok"] is False
    assert result["failed_batches"] == ["tests/test_demo_c.py"]


def test_successful_batch_uses_junitxml_and_is_not_retried(verify, monkeypatch, tmp_path) -> None:
    """正常路径不受兜底影响：用 xml、不重试（否则每批都跑两遍，全量时间翻倍）。"""
    xml = tmp_path / "probe-0.xml"
    xml.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="pytest" tests="7" failures="1" errors="0" skipped="0"/></testsuites>',
        encoding="utf-8",
    )
    calls: list[str] = []

    def _fake_run(cmd, **kwargs):  # noqa: ANN001,ANN003
        calls.append(" ".join(str(c) for c in cmd))
        return _FakeProc(1, "===== 6 passed, 1 failed in 1.0s =====\n")

    monkeypatch.setattr(verify.subprocess, "run", _fake_run)
    monkeypatch.setattr(verify, "REPORT_DIR", tmp_path)
    result = verify.run_pytest(["tests/test_demo_d.py"], "probe")

    assert len(calls) == 1, calls
    assert result["passed"] == 6 and result["failed"] == 1
    assert result["retried_batches"] == []
    assert result["ok"] is False, "有失败就应当不是 ok"


# --------------------------------------------------------------------------- #
# 三、V0 门：弱断言豁免的生效范围
# --------------------------------------------------------------------------- #
#: 探针源码。**故意**混了四种写法，用来验证豁免的边界到底在哪。
_WEAK_ASSERT_PROBE = textwrap.dedent(
    """\
    def test_last_line_noqa():
        assert check(
            "x"
        ) is None  # noqa: WEAK-ASSERT - 理由写在末行

    def test_first_line_noqa():
        assert check(  # noqa: WEAK-ASSERT - 理由写在首行
            "y"
        ) is None

    def test_noqa_on_the_def_line_is_not_enough():  # noqa: WEAK-ASSERT - 放错地方了
        assert check("w") is None

    def test_no_exemption():
        assert check("z") is None
    """
)


def test_weak_assert_exemption_covers_the_whole_statement(discipline, tmp_path) -> None:
    """★ 防的是：豁免只在 assert 的**首行**生效。

    「把 `# noqa: WEAK-ASSERT` 写在多行 assert 的末行」是很自然的写法，
    而门原来只看 `lines[node.lineno - 1]`；那个 `ast.get_source_segment(src, node)`
    分支**永远为假**（尾随注释不属于语句节点，`end_col_offset` 停在表达式末尾）——
    于是「看起来豁免了」实际会被判成新增弱断言。

    判据：首行写 noqa → 豁免；末行写 noqa → 豁免；
    **没写（或写在 `def` 行上）→ 仍然要被抓到** —— 放宽不等于放空。

    探针写进 `tmp_path`（仓库外），并把根目录传给门 ——
    **不往 `tests/` 里写文件**：万一某次异常没清掉，这个门自己就会变红（曾经真的发生过）。
    """
    (tmp_path / "probe_multi.py").write_text(_WEAK_ASSERT_PROBE, encoding="utf-8")
    found = discipline.collect_weak_asserts(tmp_path)
    expressions = sorted(key[1] for key in found)
    assert len(found) == 2, expressions
    assert "'w'" in expressions[0] and "'z'" in expressions[1], expressions


@pytest.mark.parametrize(
    ("returncode", "has_xml", "failures", "errors", "expected"),
    [
        (0, True, 0, 0, "ok"),
        # ★ 这一条是真机实证：85 项全过、XML 里 0 失败 0 错误，退出码却是 1
        (1, True, 0, 0, "exit_anomaly"),
        (1, True, 2, 0, "tests_failed"),
        (0, True, 0, 1, "tests_failed"),
        (1, False, 0, 0, "crashed"),
    ],
)
def test_batch_outcome_classification(
    verify, returncode: int, has_xml: bool, failures: int, errors: int, expected: str
) -> None:
    """★ 防的是：把「测试全过但退出码非 0」**当成失败**（误伤同批别的文件）或**当成通过**（掩盖真问题）。

    来源（2026-09-30 实测）：`test_desktop.py` 连跑多轮，junitxml 里始终
    **85 项全过 / 0 失败 / 0 错误**，而进程退出码是 1。日志最后一行是
    `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":409, ...}`
    —— pytest 在**会话收尾**时清理老的 `%TEMP%/pytest-of-*/pytest-N/`，
    被本机沙箱的批量删除保护拦下。

    ⇒ **退出码受环境影响，不能拿它判断测试结果**；但也不能因此不看它。
    三类必须分开：`ok` / `tests_failed` / `exit_anomaly`（外加 `crashed`）。
    """
    assert (
        verify.classify_batch_outcome(
            returncode=returncode, has_xml=has_xml, failures=failures, errors=errors
        )
        == expected
    )
