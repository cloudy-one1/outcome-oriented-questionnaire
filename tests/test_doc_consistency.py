"""README 的覆盖率口径必须是生成物，不是手抄。

这条测试存在的原因：v2.x~v3.0 每次改代码都要人手把同一组数字抄到
README、CHANGELOG、`ci.yml` 三处，2026-09-22 一天就发了两次「口径同步」提交。
现在数字只有一个来源（`coverage.json` → `scripts/readme_coverage.py`），
这里守住的是"手抄通道已经关掉"这件事。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("readme_coverage", ROOT / "scripts" / "readme_coverage.py")
assert _spec and _spec.loader
rc = importlib.util.module_from_spec(_spec)
# 必须先注册再 exec：脚本里有 @dataclass，而 dataclasses 会回查 sys.modules[cls.__module__]
sys.modules[_spec.name] = rc
_spec.loader.exec_module(rc)

GAPS = json.loads((ROOT / "scripts" / "coverage_gaps.json").read_text(encoding="utf-8"))
README_TEXT = (ROOT / "README.md").read_text(encoding="utf-8")


def outside_block(text: str) -> str:
    head, _, rest = text.partition(rc.BEGIN)
    _, _, tail = rest.partition(rc.END)
    return head + tail


def gap_rows() -> list[dict]:
    rows = list(GAPS["closed_gaps"]["rows"]) + list(GAPS["open_gaps"]["rows"])
    return rows


def test_readme_still_has_the_generated_block() -> None:
    assert rc.BEGIN in README_TEXT and rc.END in README_TEXT
    assert README_TEXT.index(rc.END) > README_TEXT.index(rc.BEGIN)
    assert splice_ok(README_TEXT)


def splice_ok(text: str) -> bool:
    return len(text.split(rc.BEGIN)) == 2 and len(text.split(rc.END)) == 2


def test_measured_numbers_only_live_inside_the_generated_block() -> None:
    """一位小数的百分比 = 实测断言，手抄通道已关，只允许出现在生成块里。"""
    prose = outside_block(README_TEXT)
    offenders = [
        f"{i}: {line.strip()[:90]}"
        for i, line in enumerate(prose.splitlines(), start=1)
        if re.search(r"\d+\.\d\s*%", line) or re.search(r"剩 \d+ 行", line)
    ]
    assert not offenders, "README 生成块之外出现了手写实测数字：\n" + "\n".join(offenders)


@pytest.mark.parametrize("row", gap_rows(), ids=lambda r: r.get("module") or r.get("group"))
def test_gap_row_points_at_things_that_exist(row: dict) -> None:
    target = ROOT / row["module"] if row.get("module") else ROOT / row["group"]
    assert target.exists(), f"{row} 指向的模块不存在（改名或删文件后清单没跟上）"
    for test_file in row.get("covered_by", []):
        assert (ROOT / test_file).exists(), f"{test_file} 不存在，却还在充当契约测试的证据"


def test_undeclared_gaps_must_carry_a_reason() -> None:
    empty = [r["module"] for r in GAPS["open_gaps"]["rows"] if not r.get("reason", "").strip()]
    assert not empty, f"这些缺口没写理由：{empty}"


def test_floor_is_still_parsed_out_of_ci_yml() -> None:
    floor = rc.load_floor()
    assert float(floor) >= 70, f"地板掉到 {floor}，低于历史只许上调的约定"
    assert f"--cov-fail-under={floor}" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_generated_block_matches_coverage_when_present() -> None:
    coverage = ROOT / "coverage.json"
    if not coverage.exists():
        pytest.skip("没有 coverage.json，跑完 `--cov-report=json:coverage.json` 再来看这一条")
    stats = rc.load_coverage(coverage)
    block = rc.render(GAPS, stats, rc.load_floor())
    problems = rc.compare(README_TEXT, block, GAPS)
    assert not problems, "README 口径已漂移，修法是 `python scripts/readme_coverage.py --write`：\n" + "\n".join(problems)
