"""提交前完整度自检（``src/completeness.py``）的离线契约测试。

要防的失效面：探测漏了某种真页面控件（2026-09-22 真卷上的排序题与日期题就是），
于是那道必答题从头到尾没被答过，而症状是**点完提交之后**平台弹一句"第 N 题未答" ——
日志里只剩一条看不懂的失败。这一步在点提交之前把这种题挑出来。

判据只有一种事实：**平台标了必答 + 我们整题没探测到**。测试的另一半同样重要：
探测到了但没答上（那是"已答扫描"的活，而它读的是同一套可能判错的结构）、
平台没自报、没有 ``req`` 属性 —— 全都不许拦。**宁可少拦，不能拦错**，
拦错一次就是一单本来能交的问卷被判失败。
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.completeness import describe_gap, unanswered_required  # noqa: E402


def _items(*pairs: tuple[int, bool]) -> list[dict]:
    return [{"q": q, "code": "3", "required": req} for q, req in pairs]


def test_no_platform_signal_never_blocks() -> None:
    assert unanswered_required([], {1, 2}) == []


def test_required_and_never_detected_is_the_only_gap() -> None:
    assert unanswered_required(_items((1, True), (2, True)), {1}) == [2]


def test_detected_required_questions_are_not_reported() -> None:
    """整卷题号都在"见过"集合里 → 空。逐页累积漏了就会在这里暴露。"""
    assert unanswered_required(_items((1, True), (2, True), (3, True)), {1, 2, 3}) == []


def test_optional_question_we_missed_is_not_a_gap() -> None:
    """没标 ``req`` 的题我们漏了也不拦：那是题型缺口（[对拍] 负责说），不是拦停理由。"""
    assert unanswered_required(_items((7, False)), set()) == []


def test_gap_is_sorted_and_deduped() -> None:
    items = _items((9, True), (4, True), (9, True))
    assert unanswered_required(items, set()) == [4, 9]


@pytest.mark.parametrize("bad_q", [None, "x", "", [], {}])
def test_unusable_question_numbers_are_skipped(bad_q: Any) -> None:
    items = [{"q": bad_q, "code": "3", "required": True}, {"q": 5, "code": "3", "required": True}]
    assert unanswered_required(items, set()) == [5]


def test_missing_required_key_is_treated_as_optional() -> None:
    """读不到 ``required`` 就当不必答 —— 缺信号时朝"不拦"的方向降级。"""
    assert unanswered_required([{"q": 3, "code": "3"}], set()) == []


def test_description_names_the_questions_and_the_action() -> None:
    line = describe_gap([12, 15])
    assert "Q12" in line and "Q15" in line
    assert "不点提交" in line and "[完整度]" in line


def test_empty_gap_produces_no_line_from_caller() -> None:
    """空集合时调用方根本不该出声（这里只锁住判据本身返回空）。"""
    assert unanswered_required(_items((1, True)), {1}) == []
