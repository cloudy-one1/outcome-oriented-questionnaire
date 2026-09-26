"""覆盖率口径必须是生成物，不是手抄。

这条测试存在的原因：v2.x~v3.0 每次改代码都要人手把同一组数字抄到
README、CHANGELOG、`ci.yml` 三处，2026-09-22 一天就发了两次「口径同步」提交。
现在数字只有一个来源（`coverage.json` → `scripts/coverage_doc.py`），
这里守住的是"手抄通道已经关掉"这件事。

README 瘦身后生成块住在 `docs/coverage.md`，但**两处都在扫描范围内**：块之外
不许出现实测数字这条判据，管的是所有对用户讲话的页面，不是某一个文件。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("coverage_doc", ROOT / "scripts" / "coverage_doc.py")
assert _spec and _spec.loader
rc = importlib.util.module_from_spec(_spec)
# 必须先注册再 exec：脚本里有 @dataclass，而 dataclasses 会回查 sys.modules[cls.__module__]
sys.modules[_spec.name] = rc
_spec.loader.exec_module(rc)

GAPS = json.loads((ROOT / "scripts" / "coverage_gaps.json").read_text(encoding="utf-8"))
README_TEXT = (ROOT / "README.md").read_text(encoding="utf-8")
DOC_TEXT = rc.DOC.read_text(encoding="utf-8")


def outside_block(text: str) -> str:
    head, _, rest = text.partition(rc.BEGIN)
    _, _, tail = rest.partition(rc.END)
    return head + tail


def gap_rows() -> list[dict]:
    rows = list(GAPS["closed_gaps"]["rows"]) + list(GAPS["open_gaps"]["rows"])
    return rows


def test_coverage_doc_still_has_the_generated_block() -> None:
    assert rc.BEGIN in DOC_TEXT and rc.END in DOC_TEXT
    assert DOC_TEXT.index(rc.END) > DOC_TEXT.index(rc.BEGIN)
    assert splice_ok(DOC_TEXT)


def test_readme_no_longer_holds_the_block() -> None:
    """README 里不许留哨兵 —— 块搬走之后，半截哨兵是"生成物落在没人看的文件里"的起点。"""
    assert rc.BEGIN not in README_TEXT and rc.END not in README_TEXT


def splice_ok(text: str) -> bool:
    return len(text.split(rc.BEGIN)) == 2 and len(text.split(rc.END)) == 2


def test_measured_numbers_only_live_inside_the_generated_block() -> None:
    """一位小数的百分比 = 实测断言，手抄通道已关，只允许出现在生成块里。"""
    prose = outside_block(DOC_TEXT) + "\n" + README_TEXT
    offenders = [
        f"{i}: {line.strip()[:90]}"
        for i, line in enumerate(prose.splitlines(), start=1)
        if re.search(r"\d+\.\d\s*%", line) or re.search(r"剩 \d+ 行", line)
    ]
    assert not offenders, "生成块之外出现了手写实测数字：\n" + "\n".join(offenders)


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
    assert f"--cov-fail-under={floor}" in DOC_TEXT


def test_the_coverage_scope_is_one_list_not_two() -> None:
    """ci.yml 的 ``--cov`` 与生成器的 ``PACKAGES`` 必须是同一份名单。

    不一致时**什么都不会红**：新包照样被测试跑着，只是不进 coverage.json，
    于是口径文档的"全部"那一行安静地少算一块 —— 门禁看着是绿的，量的范围却缩了。
    ``webui/`` 就是这么在门禁之外待了一整轮的。
    """
    yml = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    measured = {f"{m}/" for m in re.findall(r"--cov=([\w.]+)", yml)}
    assert measured == set(rc.PACKAGES), (
        f"ci.yml 量的是 {sorted(measured)}，生成器写的是 {sorted(rc.PACKAGES)}")


def test_no_session_scoped_prose_in_user_facing_docs() -> None:
    """README、口径文档与缺口清单里不许出现"本轮整改/待复测/定版时记得"这类只对某一次会话有意义的话。

    借的是对标仓库 `check-docs.js` 的黑名单思路，但只收**没有正当用途**的那几个词：
    「本轮」在我们文档里是"这一份提交"的产品词（`--headless` 下判本轮失败），不在黑名单内。
    """
    banned = ("随本轮", "本轮整改", "待复测", "定版时", "请同步", "TODO", "FIXME", "评审意见")
    spec_text = (ROOT / "scripts" / "coverage_gaps.json").read_text(encoding="utf-8")
    offenders = [
        f"{path}:{n}: {line.strip()[:80]}"
        for path, text in (("README.md", README_TEXT),
                           ("docs/coverage.md（生成块之外）", outside_block(DOC_TEXT)),
                           ("scripts/coverage_gaps.json", spec_text))
        for n, line in enumerate(text.splitlines(), start=1)
        if any(word in line for word in banned)
    ]
    assert not offenders, "文档里出现了只对某次会话有意义的话：\n" + "\n".join(offenders)


def test_coverage_gate_script_distinguishes_usage_from_drift(tmp_path: Path) -> None:
    """退出码 0/1/2 的分工：漂移=1，跑不动=2 —— 让 CI 一眼分得清是口径问题还是环境没准备好。"""
    import subprocess

    script = ROOT / "scripts" / "coverage_doc.py"
    missing = tmp_path / "no-such-coverage.json"
    result = subprocess.run(
        [sys.executable, str(script), "--check", "--coverage", str(missing)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "coverage.json" in result.stderr + result.stdout


def test_generated_block_matches_coverage_when_present() -> None:
    coverage = ROOT / "coverage.json"
    if not coverage.exists():
        pytest.skip("没有 coverage.json，跑完 `--cov-report=json:coverage.json` 再来看这一条")
    stats = rc.load_coverage(coverage)
    block = rc.render(GAPS, stats, rc.load_floor())
    problems = rc.compare(DOC_TEXT, block, GAPS)
    assert not problems, (
        "覆盖率口径已漂移，修法是 `python scripts/coverage_doc.py --write`：\n" + "\n".join(problems))
