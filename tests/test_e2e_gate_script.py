"""``scripts/e2e_gate.py`` 的契约 —— 那条"CI 绿但其实没跑"的最后一道闸。

为什么值得单独钉：本脚本自身就是防作弊的，而它自己的失效方式恰好都是安静的
（读不到报告、被 skip 骗过去、要求的文件根本不在报告里）。断言全部打在退出码上，
因为 CI 只看那个数。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.e2e_gate import main, module_of  # noqa: E402


def _report(tmp_path: Path, cases: list[tuple[str, str, bool]],
            filename: str = "e2e-junit.xml") -> str:
    """按 pytest 的 junit 形状拼一份报告（classname 是点号的模块路径）。"""
    rows = []
    skipped = 0
    for classname, name, is_skip in cases:
        if is_skip:
            skipped += 1
            rows.append(f'<testcase classname="{classname}" name="{name}">'
                        '<skipped type="pytest.skip" message="driver"/></testcase>')
        else:
            rows.append(f'<testcase classname="{classname}" name="{name}" '
                        'time="0.1" />')
    total = len(cases)
    xml = (f'<testsuites name="pytest tests"><testsuite name="pytest" '
           f'errors="0" failures="0" skipped="{skipped}" tests="{total}" '
           f'time="1.0">{"".join(rows)}</testsuite></testsuites>')
    path = tmp_path / filename
    path.write_text(xml, encoding="utf-8")
    return str(path)


BOTH = ([("tests.test_e2e_integration", f"test_round_{i}", False) for i in range(3)]
        + [("tests.test_webui_e2e", f"test_console_{i}", False) for i in range(2)])

REQUIRED = ["--require-file", "tests/test_e2e_integration.py",
            "--require-file", "tests/test_webui_e2e.py"]


# ------------------------------------------------------------------ 既有语义

def test_a_real_browser_run_passes(tmp_path, capsys) -> None:
    assert main([_report(tmp_path, BOTH), "--min-run", "1"]) == 0
    assert "OK: E2E ran in a real browser" in capsys.readouterr().out


def test_everything_skipped_is_a_fail_not_a_green(tmp_path) -> None:
    """pytest 全 skip 时退出码仍是 0 —— 这个脚本存在的第一条理由。"""
    assert main([_report(tmp_path, [(c, n, True) for c, n, _ in BOTH])]) == 1


def test_an_empty_report_is_a_fail(tmp_path) -> None:
    assert main([_report(tmp_path, [])]) == 1


def test_a_missing_report_is_exit_two_not_one(tmp_path) -> None:
    """读不动与"没跑过"必须分得开，否则驱动装不上看起来像代码回归。"""
    assert main([str(tmp_path / "nope.xml")]) == 2


def test_an_unparsable_report_is_exit_two(tmp_path) -> None:
    """截断/半截 XML 是"报告读不了"，不是"没跑过"。"""
    bad = tmp_path / "broken.xml"
    good = Path(_report(tmp_path, BOTH, filename="good.xml")).read_text(
        encoding="utf-8")
    bad.write_text(good[: len(good) // 2], encoding="utf-8")
    assert main([str(bad)]) == 2


def test_a_report_without_a_testsuite_element_is_exit_two(tmp_path) -> None:
    bad = tmp_path / "shape.xml"
    bad.write_text("<root><item/></root>", encoding="utf-8")
    assert main([str(bad)]) == 2


# ------------------------------------------------- --require-file（另一半洞）

def test_required_file_fully_skipped_fails_even_though_ran_is_positive(tmp_path,
                                                                       capsys) -> None:
    """25 项跑了、webui 那几项全被 conftest 吞成 skip：全局计数看不出来。"""
    report = _report(tmp_path, [
        ("tests.test_e2e_integration", "test_round_0", False),
        ("tests.test_e2e_integration", "test_round_1", False),
        ("tests.test_webui_e2e", "test_console_0", True),
        ("tests.test_webui_e2e", "test_console_1", True)])
    assert main([report] + REQUIRED) == 1
    out = capsys.readouterr().out
    assert "required file never ran" in out and "tests/test_webui_e2e.py" in out


def test_required_file_absent_from_the_report_fails(tmp_path, capsys) -> None:
    """改名、被 -m 筛掉、忘了收集 —— 都表现为"要求的那份根本不在报告里"。"""
    report = _report(tmp_path, BOTH)
    assert main([report, "--require-file", "tests/test_gone.py"]) == 1
    assert "contributed no testcases" in capsys.readouterr().out


def test_a_partially_skipped_required_file_still_passes(tmp_path) -> None:
    """只要求"至少一条真跑过"，否则一次正常抖动就会红掉整条 leg。"""
    report = _report(tmp_path, [
        ("tests.test_e2e_integration", "test_round_0", False),
        ("tests.test_webui_e2e", "test_console_0", True),
        ("tests.test_webui_e2e", "test_console_1", False)])
    assert main([report] + REQUIRED) == 0


def test_both_path_shapes_are_accepted(tmp_path) -> None:
    """CI 里写路径，本地手敲爱写点号模块名 —— 两种都认，别让人猜。"""
    report = _report(tmp_path, BOTH)
    assert main([report, "--require-file", "tests/test_webui_e2e.py"]) == 0
    assert main([report, "--require-file", "tests.test_webui_e2e"]) == 0
    assert main([report, "--require-file", "./tests\\webui_x.py"]) == 1


def test_a_sibling_module_with_the_same_prefix_is_not_counted(tmp_path, capsys) -> None:
    """``tests/test_webui_e2e_extra.py`` 不算 ``tests/test_webui_e2e.py`` 跑过。"""
    report = _report(tmp_path, [
        ("tests.test_e2e_integration", "test_round_0", False),
        ("tests.test_webui_e2e_extra", "test_console_0", False)])
    assert main([report] + REQUIRED) == 1
    assert "contributed no testcases" in capsys.readouterr().out


@pytest.mark.parametrize("text,want", [
    ("tests/test_webui_e2e.py", "tests.test_webui_e2e"),
    ("./tests/test_webui_e2e.py", "tests.test_webui_e2e"),
    ("tests\\test_webui_e2e.py", "tests.test_webui_e2e"),
    ("tests.test_webui_e2e", "tests.test_webui_e2e"),
    ("tests/test_webui_e2e", "tests.test_webui_e2e"),
])
def test_module_of_normalizes(text, want) -> None:
    assert module_of(text) == want
