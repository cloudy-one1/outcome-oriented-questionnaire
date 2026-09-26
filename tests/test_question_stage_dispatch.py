"""``_answer_one_question`` 的题型分发契约（v3.0 新增矩阵多选时补）。

当年 README「已知缺口」里 ``src/pipeline_stages/question_stage.py`` 只有 29%，
备注是"真实点击仍靠 E2E"。但**分发**本身（哪道题该调哪个 js_* 函数、
落库的 options_selected 是什么形状）是纯 Python 逻辑，不需要浏览器就能钉住 ——
新增一个题型时，最容易忘的恰好就是这条接线：JS 与生成器各自都对，
中间那个 ``elif ans_type == ...`` 漏了，题目就一路静默走"兜底当单选"分支。
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config  # noqa: E402
from src.pipeline_stages.question_stage import _answer_one_question  # noqa: E402


class RecordingDriver:
    """只记录被注入的脚本，永远返回"成功"。"""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def execute_script(self, script: str, *_args: Any, **_kw: Any) -> Any:
        self.scripts.append(script)
        return True


@pytest.fixture(autouse=True)
def _clean_weight_config():
    saved = dict(config.WEIGHT_CONFIG)
    config.WEIGHT_CONFIG.clear()
    yield
    config.WEIGHT_CONFIG.clear()
    config.WEIGHT_CONFIG.update(saved)


def test_matrix_multi_dispatches_to_the_multi_filler(monkeypatch) -> None:
    """矩阵多选必须调 js_fill_matrix_multi，且每行勾中的列值摊平进 options_selected。"""
    calls: list[tuple[int, dict]] = []

    def fake_multi(driver: Any, q: int, row_selections: dict) -> bool:
        calls.append((q, row_selections))
        return True

    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_fill_matrix_multi", fake_multi
    )
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_fill_matrix_single",
        lambda *a, **k: pytest.fail("矩阵多选不该走单选填充器"),
    )

    q = {"q": 12, "type": "matrix_multi", "rows": [1, 2], "cols": [1, 2, 3],
         "row_weights": {1: [1, 0, 0], 2: [0, 0, 1]}, "pick_options": [2],
         "pick_weights": [1]}
    assert _answer_one_question(RecordingDriver(), q) is True

    assert len(calls) == 1
    answered_q, row_map = calls[0]
    assert answered_q == 12
    # 行 1 权重集中在列 1、行 2 集中在列 3，各勾 2 个 → 次选按等权补齐
    assert sorted(row_map[1]) == sorted(set(row_map[1]))
    assert 1 in row_map[1] and 3 in row_map[2]


def test_matrix_multi_history_row_is_flattened(monkeypatch) -> None:
    """落库形状：options_selected 是一维列值列表，与 multi 的明细口径一致。"""
    recorded: dict[str, Any] = {}

    class FakeHistory:
        def record_answer(self, **kw: Any) -> None:
            recorded.update(kw)

    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_fill_matrix_multi",
        lambda driver, q, row_map: True,
    )
    # question_stage 是 `from ..answering_v2 import generate_answer as generate_answer_v2`
    # —— 名字已绑进本模块命名空间，必须打在这里才生效
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.generate_answer_v2",
        lambda q: {"type": "matrix_multi", "rows": {1: [2, 4], 2: [1]}},
    )

    ok = _answer_one_question(
        RecordingDriver(),
        {"q": 12, "type": "matrix_multi", "rows": [1, 2], "cols": [1, 2, 3, 4]},
        history_db=FakeHistory(), run_id=1, submission_index=1,
    )
    assert ok is True
    assert recorded["question_type"] == "matrix_multi"
    assert recorded["options_selected"] == [2, 4, 1]


def test_matrix_single_still_dispatches_to_the_single_filler(monkeypatch) -> None:
    """对照组：单选的既有分发不能被新分支带偏。"""
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_fill_matrix_multi",
        lambda *a, **k: pytest.fail("单选不该走多选填充器"),
    )
    seen: list[Any] = []
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_fill_matrix_single",
        lambda driver, q, row_map: seen.append(row_map) or True,
    )
    assert _answer_one_question(
        RecordingDriver(),
        {"q": 10, "type": "matrix_single", "rows": [1], "cols": [1, 2, 3]},
    ) is True
    assert len(seen) == 1


# ===========================================================================
#  v3.0：选项自带填空框（"其他____"）
# ===========================================================================
def _force_pick_value_3() -> None:
    """让 Q2 的多选永远只勾中值为 3 的那一项。"""
    config.WEIGHT_CONFIG[2] = {
        "type": "multi",
        "weights": [0, 0, 1],
        "count_options": [1],
        "count_weights": [1],
    }


def test_multi_fills_blank_only_for_selected_option(monkeypatch) -> None:
    """带框的选项被勾中才补文本；没勾中的那一项不许动。

    往没选中的框里写字，页面上一格都没勾、服务端却收到一段文本 ——
    比漏填更难查，因为回收数据里会多出一列没人解释得了的内容。
    """
    _force_pick_value_3()
    fills: list[tuple[int, Any, str]] = []
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_click_question_options",
        lambda driver, q, qtype, values: True,
    )
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_fill_option_blank",
        lambda driver, q, choice, text: fills.append((q, choice, text)) or True,
    )

    q = {"q": 2, "type": "multi", "choices": [1, 2, 3], "blank_options": [2, 3]}
    assert _answer_one_question(RecordingDriver(), q) is True
    assert [f[1] for f in fills] == [3], "只该为被勾中的那一项补文本"
    assert fills[0][0] == 2
    assert fills[0][2].strip(), "补的文本不能是空串"


def test_unselected_blank_option_is_not_touched(monkeypatch) -> None:
    """一个都没勾中带框的项时，一次都不该调用填充器。"""
    config.WEIGHT_CONFIG[2] = {
        "type": "multi",
        "weights": [1, 0, 0],
        "count_options": [1],
        "count_weights": [1],
    }
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_click_question_options",
        lambda driver, q, qtype, values: True,
    )
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_fill_option_blank",
        lambda *a, **k: pytest.fail("没勾中带框的项，不该去填它"),
    )
    assert _answer_one_question(
        RecordingDriver(),
        {"q": 2, "type": "multi", "choices": [1, 2, 3], "blank_options": [3]},
    ) is True


def test_blank_fill_failure_flips_question_to_failed(monkeypatch) -> None:
    """补文本失败必须把本题记为失败。

    报"成功"而页面空着一格，最后只剩一个看不出原因的 unknown 提交 ——
    这是本工具最难查的一类故障，宁可在逐题这一层就认下来。
    """
    _force_pick_value_3()
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_click_question_options",
        lambda driver, q, qtype, values: True,
    )
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_fill_option_blank",
        lambda driver, q, choice, text: False,
    )
    assert _answer_one_question(
        RecordingDriver(),
        {"q": 2, "type": "multi", "choices": [1, 2, 3], "blank_options": [3]},
    ) is False


def test_blank_fill_exception_is_swallowed_as_failure(monkeypatch) -> None:
    """填充器抛 WebDriver 级异常时降级为"本题失败"，不能掀掉整批。"""
    from selenium.common.exceptions import StaleElementReferenceException

    _force_pick_value_3()
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_click_question_options",
        lambda driver, q, qtype, values: True,
    )

    def _boom(*a: Any, **k: Any) -> bool:
        raise StaleElementReferenceException("框所在的 li 被重绘了")

    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_fill_option_blank", _boom
    )
    assert _answer_one_question(
        RecordingDriver(),
        {"q": 2, "type": "multi", "choices": [1, 2, 3], "blank_options": [3]},
    ) is False


def test_replayed_single_overrides_the_weight_config(monkeypatch) -> None:
    """答卷表说选第 2 项，权重配置就得靠边站 —— 覆盖只发生在"生成答案"那一步。

    点击、DOM 回读、落盘走的仍是原路径：回放的答案同样要在真 DOM 里落上才算成功，
    否则"表里有但页面没写进去"会被记成一份成功提交。
    """
    clicked: list[list] = []
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.reverse_fill.answer_for_question",
        lambda q, idx: {"type": "single", "selected": [2]},
    )
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_click_question_options",
        lambda _d, _q, _t, vals: clicked.append(list(vals)) or True,
    )
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.build_answer_strategy",
        lambda _q: pytest.fail("回放覆盖生效时不该再问生成策略"),
    )
    q = {"q": 1, "type": "single", "choices": [1, 2, 3]}
    assert _answer_one_question(RecordingDriver(), q, submission_index=1) is True
    assert clicked == [[2]], clicked


def test_replayed_text_lands_in_the_dom_and_history(monkeypatch) -> None:
    """填空题被回放覆盖时，落库的 text_answer 必须是表里那个值。"""
    from src import config as _c  # noqa: F401  （只要模块可导入即可）

    filled: list[tuple[int, str]] = []
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.reverse_fill.answer_for_question",
        lambda q, idx: {"type": "text", "text": "李娜", "field": "name"},
    )
    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_fill_text",
        lambda _d, qnum, text: filled.append((qnum, text)) or True,
    )
    recorded: list = []

    class _Db:
        def record_answer(self, **kw):
            recorded.append(kw)
            return 1

    assert _answer_one_question(
        RecordingDriver(), {"q": 3, "type": "text", "field": "name"},
        history_db=_Db(), run_id=1, submission_index=1,
    ) is True
    assert filled == [(3, "李娜")], filled
    assert recorded and recorded[0]["text_answer"] == "李娜", recorded
