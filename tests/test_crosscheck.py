"""v3.1 结构对拍的离线契约测试（``src/crosscheck.py`` + 平台侧读数）。

要防的失效面：``detect_questions`` 从控件形状反推题型，而它**发现自己判错了**的
唯一途径本来是"提交后平台说该题未答"。2026-09-22 拿一份真问卷对拍，15 道题里
6 道与平台自报的题型码不符（矩阵量表被判成 scale、排序题整道没探测到）——
症状全是"提交失败/未知"，日志里看不出从哪一步开始错。

用例按 crosscheck 模块文档的三条契约组织：
  1. 只提示、不拦停、不改作答（未知码不参与题型比对）；
  2. 没有平台信号就彻底静默（一行都不印）；
  3. 同一句提示每进程只印一次。

平台读数怎么来的（``detect_platform_questions``）单独一节：它是只读旁路，
解析必须对任何畸形输入免疫 —— 它崩了不该把一份能提交的问卷判失败。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.crosscheck import (  # noqa: E402
    crosscheck_questions,
    report_structure_drift,
    reset_reported_drift,
)
from src.detection import detect_platform_questions  # noqa: E402
from src.platforms import WJX, SurveyPlatform  # noqa: E402

_BARE = SurveyPlatform(
    name="bare",
    hosts=("example.com",),
    question_control_selector="input",
    submit_selectors=("#submit",),
    next_page_selectors=("#next",),
    page_wrapper_selector=".page",
)


class _FakeDriver:
    """只实现 ``execute_script``，并记下脚本与参数（对拍要按候选选择器传参）。"""

    def __init__(self, return_value: Any = "[]") -> None:
        self._return_value = return_value
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute_script(self, script: str, *args: Any) -> Any:
        self.calls.append((script, args))
        return self._return_value


def _platform(*pairs: tuple[int, str]) -> list[dict]:
    return [{"q": q, "code": c} for q, c in pairs]


# ============================================================================
#  1. 平台读数：detect_platform_questions
# ============================================================================
def test_parses_and_sorts_by_question_number() -> None:
    drv = _FakeDriver(return_value=json.dumps([{"q": 9, "code": "6"}, {"q": 3, "code": "7"}]))
    assert detect_platform_questions(drv, WJX) == [
        {"q": 3, "code": "7", "required": False},
        {"q": 9, "code": "6", "required": False},
    ]


def test_passes_marker_candidates_and_attr_names() -> None:
    """选择器按候选传、属性名也从平台层传 —— JS 里不写死问卷星的拼写。"""
    drv = _FakeDriver(return_value="[]")
    detect_platform_questions(drv, WJX)
    script, args = drv.calls[0]
    assert list(args) == [list(WJX.question_marker_selectors), "topic", "type", True]
    detect_platform_questions(drv, WJX, visible_only=False)
    assert drv.calls[1][1][-1] is False
    assert "pageHidden" in script           # 与 detect_questions 同源的分页过滤


@pytest.mark.parametrize("payload", [None, True, 12, "not json", "{}", '{"q":1}'])
def test_any_unusable_payload_degrades_to_no_signal(payload: Any) -> None:
    """脚本被页面改写 / 返回意外形态 → 空列表，绝不抛（契约 2 的地基）。"""
    drv = _FakeDriver(return_value=payload)
    assert detect_platform_questions(drv, WJX) == []


@pytest.mark.parametrize("raw", [
    json.dumps([{"q": "abc", "code": "3"}]),          # 题号不是数
    json.dumps([{"q": 1, "code": ""}]),               # 题型码空串
    json.dumps([{"q": 1, "code": None}]),             # 题型码缺失
    json.dumps([{"q": None, "code": "3"}]),           # 题号缺失
    json.dumps(["div1"]),                             # 元素不是 dict
    json.dumps([{"q": 1}]),                           # 整个 code 键没有
])
def test_malformed_entries_are_dropped(raw: str) -> None:
    assert detect_platform_questions(_FakeDriver(return_value=raw), WJX) == []


def test_duplicate_topic_keeps_first_readout() -> None:
    """同一题号标了两次（嵌套容器）时留第一个：拿两个矛盾读数去比，等于自己造警。"""
    raw = json.dumps([{"q": 4, "code": "5"}, {"q": 4, "code": "3"}])
    assert detect_platform_questions(_FakeDriver(return_value=raw), WJX) == [
        {"q": 4, "code": "5", "required": False}
    ]


def test_stringy_numbers_are_coerced() -> None:
    raw = json.dumps([{"q": "7", "code": " 6 "}, {"q": 8.0, "code": "1"}])
    assert detect_platform_questions(_FakeDriver(return_value=raw), WJX) == [
        {"q": 7, "code": "6", "required": False},
        {"q": 8, "code": "1", "required": False},
    ]


def test_required_flag_is_carried_through() -> None:
    """``required`` 的判据只在 JS 侧一份（``req="0"`` / 没这个属性都算不必答），Python 照搬。"""
    raw = json.dumps([
        {"q": 1, "code": "3", "required": True},
        {"q": 2, "code": "3", "required": False},
        {"q": 3, "code": "3"},
    ])
    assert detect_platform_questions(_FakeDriver(return_value=raw), WJX) == [
        {"q": 1, "code": "3", "required": True},
        {"q": 2, "code": "3", "required": False},
        {"q": 3, "code": "3", "required": False},
    ]


def test_platform_without_markers_never_touches_the_driver() -> None:
    drv = _FakeDriver(return_value=json.dumps([{"q": 1, "code": "3"}]))
    assert detect_platform_questions(drv, _BARE) == []
    assert drv.calls == []


# ============================================================================
#  2. 对拍本体：crosscheck_questions
# ============================================================================
def test_no_platform_signal_is_completely_silent() -> None:
    """没有平台读数 → 一行都不出。模板不标 topic 不是我们的探测错了。"""
    ours = [{"q": 1, "type": "single"}]
    assert crosscheck_questions(ours, [], WJX) == []


def test_matching_structure_produces_no_line() -> None:
    ours = [
        {"q": 1, "type": "single"},
        {"q": 2, "type": "multi"},
        {"q": 3, "type": "scale", "scale": 5, "scale_min": 1},
        {"q": 4, "type": "text"},
        {"q": 5, "type": "dropdown"},
        {"q": 6, "type": "matrix_single"},
        {"q": 7, "type": "matrix_multi"},
        {"q": 8, "type": "sort"},
    ]
    plat = _platform((1, "3"), (2, "4"), (3, "5"), (4, "1"),
                     (5, "7"), (6, "6"), (7, "6"), (8, "11"))
    assert crosscheck_questions(ours, plat, WJX) == []


def test_missing_question_is_reported_with_platform_label() -> None:
    """真卷上实测到的那一类：平台有排序题，我们整道没探测到。"""
    lines = crosscheck_questions([{"q": 1, "type": "single"}],
                                 _platform((1, "3"), (2, "11")), WJX)
    assert len(lines) == 1
    assert "Q2" in lines[0] and "排序" in lines[0] and "没探测到" in lines[0]


def test_type_mismatch_reports_both_readouts() -> None:
    ours = [{"q": 7, "type": "scale", "scale": 4, "scale_min": 1}]
    lines = crosscheck_questions(ours, _platform((7, "6")), WJX)
    assert len(lines) == 1
    assert "scale（1~4）" in lines[0]      # 级数一起报，直接指向"数错格子还是认错容器"
    assert "矩阵" in lines[0]


def test_phantom_question_we_invented_is_reported() -> None:
    lines = crosscheck_questions([{"q": 3, "type": "multi"}], _platform((1, "3")), WJX)
    assert any("Q3" in ln and "没有标" in ln for ln in lines)


def test_unknown_code_skips_type_comparison() -> None:
    """码表没收录的码（8 / 10…）不做题型比对 —— 无据可依的报警是噪声。"""
    ours = [{"q": 1, "type": "single"}]
    assert crosscheck_questions(ours, _platform((1, "8")), WJX) == []


def test_unknown_code_still_reports_a_missing_question() -> None:
    """但"平台有题、我们没探测到"与码表无关：题没答上就是没答上。"""
    lines = crosscheck_questions([], _platform((9, "10")), WJX)
    assert len(lines) == 1
    assert "Q9" in lines[0] and "题型码 10" in lines[0]


def test_matrix_code_accepts_both_our_matrix_types() -> None:
    plat = _platform((10, "6"))
    assert crosscheck_questions([{"q": 10, "type": "matrix_single"}], plat, WJX) == []
    assert crosscheck_questions([{"q": 10, "type": "matrix_multi"}], plat, WJX) == []


def test_blank_and_multiline_text_share_the_text_type() -> None:
    plat = _platform((1, "1"), (2, "2"))
    ours = [{"q": 1, "type": "text"}, {"q": 2, "type": "text"}]
    assert crosscheck_questions(ours, plat, WJX) == []


def test_unusable_rows_on_both_sides_are_ignored() -> None:
    """畸形输入不崩：题号取不出 int 的行两边都跳过。"""
    ours = [{"type": "single"}, {"q": None, "type": "single"}, {"q": 1, "type": "single"}]
    plat = [{"q": "x", "code": "3"}, {"code": "3"}, {"q": 1, "code": "3"}]
    assert crosscheck_questions(ours, plat, WJX) == []


def test_missing_type_field_is_treated_as_mismatch() -> None:
    """探测项连 type 都没有 = 结构不完整，该报（不能因为取不到就放过）。"""
    lines = crosscheck_questions([{"q": 1}], _platform((1, "3")), WJX)
    assert len(lines) == 1 and "Q1" in lines[0]


def test_platform_without_type_codes_never_claims_mismatch() -> None:
    """没有码表的平台只比"有没有这道题"，不比题型（无据可依）。"""
    ours = [{"q": 1, "type": "single"}]
    lines = crosscheck_questions(ours, _platform((1, "3")), _BARE)
    assert lines == []


# ============================================================================
#  3. 去重（契约 3）
# ============================================================================
def test_same_line_is_emitted_once_per_process() -> None:
    reset_reported_drift()
    try:
        lines = crosscheck_questions([], _platform((2, "11")), WJX)
        assert lines and report_structure_drift(lines) == lines
        assert report_structure_drift(lines) == []        # 第二份问卷不再刷一遍
        assert report_structure_drift(lines) == []
    finally:
        reset_reported_drift()


def test_reset_lets_the_line_through_again() -> None:
    reset_reported_drift()
    try:
        lines = crosscheck_questions([], _platform((2, "11")), WJX)
        assert report_structure_drift(lines) == lines
        reset_reported_drift()
        assert report_structure_drift(lines) == lines
    finally:
        reset_reported_drift()
