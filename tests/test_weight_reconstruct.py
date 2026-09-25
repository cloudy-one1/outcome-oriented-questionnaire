"""``src.weight_text.reconstruct_questions`` —— 从持久化权重反构表格行。

这份规则原本只在 ``gui/weight_panel.restore_from_config`` 里，而 webui 宿主根本没做
这一步（续传确认框那句"已自动恢复到表格"因此是空的）。抽共用的同时把断言从
``tests/test_gui_panels.py`` 搬到这里 —— 它钉的是**数据规则**，与 tkinter 无关，
不该随桌面版宿主一起退役。

刻意保留两条已知的粗糙（见函数 docstring）：未知题型给两个占位选项，``sort`` 因此
显示成"2 选项"。搬家时不顺手改行为，是第 7 步能安全删 Tk 的前提。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.weight_text import reconstruct_questions  # noqa: E402

MIXED = {
    1: {"type": "single", "weights": [0.2, 0.3, 0.5]},
    2: {"type": "scale", "weights": [1, 2, 3, 4]},
    3: {"type": "text", "field": "phone", "options": ["13800000000"]},
    4: {"type": "matrix", "row_weights": {"1": [1, 2], "2": [3, 4]}},
    5: {"type": "weird"},
}


def _by_q(questions: list[dict]) -> dict[int, dict]:
    return {q["q"]: q for q in questions}


def test_rows_come_out_in_question_number_order() -> None:
    assert [q["q"] for q in reconstruct_questions(MIXED)] == [1, 2, 3, 4, 5]


def test_choice_rows_get_placeholder_options_of_the_same_length() -> None:
    q = _by_q(reconstruct_questions(MIXED))[1]
    assert q["type"] == "single"
    assert len(q["choices"]) == 3, "占位选项只要够长，让权重串能对得上个数"


def test_scale_without_a_scale_key_is_inferred_from_the_weights_length() -> None:
    q = _by_q(reconstruct_questions(MIXED))[2]
    assert q["scale"] == 4 and q["scale_min"] == 1
    assert q["choices"] == [1, 2, 3, 4]


def test_scale_honours_an_explicit_scale_and_a_zero_floor_is_not_read_as_absent() -> None:
    rows = _by_q(reconstruct_questions({
        7: {"type": "scale", "scale": 10, "weights": [1] * 11, "scale_min": 0},
    }))
    assert rows[7]["scale"] == 10 and len(rows[7]["choices"]) == 10


def test_text_rows_keep_the_field_and_a_blank_choice_list() -> None:
    q = _by_q(reconstruct_questions(MIXED))[3]
    assert q["field"] == "phone" and q["choices"] == []


def test_matrix_rows_come_from_row_weights_when_rows_are_missing() -> None:
    q = _by_q(reconstruct_questions(MIXED))[4]
    assert q["rows"] == [1, 2] and q["cols"] == [1, 2]
    assert q["choices"] == q["cols"], "矩阵行的第三列按 cols 显示"


def test_unknown_types_get_two_placeholder_choices() -> None:
    """包含 ``sort`` —— 排序题反构出来是"2 选项"，与权重串不对应。

    这是桌面版既有行为，这次只是搬家。留在这儿当记号：第 7 步之后要修就一处修。
    """
    rows = _by_q(reconstruct_questions({5: {"type": "weird"},
                                        6: {"type": "sort",
                                            "weights": [1, 2, 3, 4]}}))
    assert rows[5]["choices"] == [1, 2]
    assert rows[6]["choices"] == [1, 2], "排序题没有专门分支（现状）"


def test_non_dict_entries_and_empty_config_are_dropped() -> None:
    assert reconstruct_questions({}) == []
    assert [q["q"] for q in reconstruct_questions(
        {7: "not-a-dict", 8: {"type": "single", "weights": [1.0, 2.0]}})] == [8]
