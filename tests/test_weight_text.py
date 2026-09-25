"""``src/weight_text.parse_weight_texts`` 的解析契约（设计稿 §10 步骤 4）。

两个宿主（桌面版 ``gui/weight_panel``、webui ``service``）共用这一份，所以这里钉的是
**唯一一份**的规则；宿主接线是否一致由 ``tests/test_weight_parser_parity.py`` 钉。

来历：这一版语义是从"webui 自己独立实现一遍、与桌面版逐题型对拍"里长出来的 ——
对拍抓到的三条（锚点整份缺失、矩阵空行号被接受、结尾多一个 ``|`` 毁掉整份行权重）
各有一条回归在这里。
"""

from __future__ import annotations

import pytest

from src.weight_text import parse_weight_texts


def one(qtype="single", **kw):
    q = {"q": 1, "type": qtype}
    q.update(kw)
    return [q]


def cfg_of(questions, texts):
    cfg, warnings = parse_weight_texts(questions, texts)
    return cfg, warnings


# ------------------------------------------------------------ 单选 / 多选


def test_blank_weights_are_left_out_so_the_engine_uses_equal_weights():
    cfg, warnings = cfg_of(one(choices=["a", "b"]), {1: ""})
    assert cfg == {}
    assert warnings == []


def test_valid_weights_become_floats():
    cfg, _ = cfg_of(one(choices=["a", "b", "c"]), {1: "0.2, 0.5, 0.3"})
    assert cfg[1]["weights"] == [0.2, 0.5, 0.3]


def test_a_malformed_number_skips_the_question_and_says_so():
    cfg, warnings = cfg_of(one(choices=["a", "b"]), {1: "0.5,abc"})
    assert cfg == {}
    assert len(warnings) == 1 and "格式错误" in warnings[0]


def test_a_length_mismatch_skips_the_question():
    cfg, warnings = cfg_of(one(choices=["a", "b", "c"]), {1: "1,2"})
    assert cfg == {}
    assert "与选项数 3 不符" in warnings[0]


def test_a_question_with_no_discovered_choices_is_not_blocked_by_length():
    cfg, warnings = cfg_of(one("single", choices=[]), {1: "1,1"})
    assert cfg[1]["weights"] == [1, 1]
    assert warnings == []


@pytest.mark.parametrize("raw", ["-1,2", "nan,1", "inf,1"])
def test_negative_and_non_finite_weights_are_rejected(raw):
    cfg, warnings = cfg_of(one(choices=["a", "b"]), {1: raw})
    assert cfg == {}
    assert "负数或非有限值" in warnings[0]


def test_radio_and_checkbox_aliases_take_the_choice_path():
    for alias in ("radio", "checkbox"):
        cfg, _ = cfg_of(one(alias, choices=["a", "b"]), {1: "1,3"})
        assert cfg[1]["weights"] == [1.0, 3.0]


# ------------------------------------------------------------ 量表


def test_a_single_integer_on_a_scale_forces_that_level():
    cfg, warnings = cfg_of(one("scale", scale=5), {1: "4"})
    assert cfg[1]["weights"] == [0, 0, 0, 1.0, 0]
    assert warnings == []


def test_an_out_of_range_scale_value_warns_instead_of_failing_quietly():
    """Tk 原来会算出全 0 权重且一句提示都没有 —— 设计稿 §5 修第 3 条。"""
    cfg, warnings = cfg_of(one("scale", scale=5), {1: "9"})
    assert cfg[1]["weights"] == [0, 0, 0, 0, 0]
    assert warnings and "超出量表范围" in warnings[0]


def test_a_scale_list_of_the_wrong_length_is_padded_not_dropped():
    cfg, warnings = cfg_of(one("scale", scale=5), {1: "1,2"})
    assert cfg[1]["weights"] == [1.0, 2.0, 0.0, 0.0, 0.0]
    assert "自动补齐/截断" in warnings[0]


def test_a_too_long_scale_list_is_truncated():
    cfg, warnings = cfg_of(one("scale", scale=2), {1: "1,2,3"})
    assert cfg[1]["weights"] == [1.0, 2.0]
    assert warnings


def test_a_broken_scale_keeps_the_structure_and_drops_only_weights():
    cfg, warnings = cfg_of(one("scale", scale=5, scale_min=1), {1: "a,b"})
    assert cfg[1]["scale"] == 5
    assert "weights" not in cfg[1]
    assert "只保留量表结构" in warnings[0]


def test_nps_is_a_scale_with_eleven_levels():
    cfg, _ = cfg_of(one("nps", scale=11), {1: "7"})
    assert len(cfg[1]["weights"]) == 11


# ------------------------------------------------------------ 填空


def test_blank_fill_text_still_produces_a_structural_entry():
    cfg, _ = cfg_of(one("text", field="name"), {1: ""})
    assert cfg[1] == {"type": "text", "field": "name"}


def test_fill_options_are_split_trimmed_and_empties_dropped():
    cfg, _ = cfg_of(one("text"), {1: "张三, 李四,,王五"})
    assert cfg[1]["options"] == ["张三", "李四", "王五"]


# ------------------------------------------------------------ 矩阵


def test_matrix_rows_are_parsed_per_row():
    q = one("matrix", rows=["经常", "偶尔"], cols=["学习", "生活"])[0]
    cfg, warnings = cfg_of([q], {1: "1:0,1 | 2:0.5,0.5"})
    assert cfg[1]["row_weights"] == {"1": [0.0, 1.0], "2": [0.5, 0.5]}
    assert cfg[1]["rows"] == ["经常", "偶尔"]
    assert warnings == []


def test_a_matrix_segment_without_a_row_separator_voids_the_whole_thing():
    cfg, warnings = cfg_of(one("matrix", rows=["a"], cols=["x"]),
                           {1: "1:0,1 | 2 0.5,0.5"})
    assert "row_weights" not in cfg[1]
    assert cfg[1]["rows"] == ["a"]              # 结构仍然保留
    assert "正确格式示例" in warnings[0]


def test_a_bad_number_in_any_matrix_row_voids_all_rows():
    cfg, warnings = cfg_of(one("matrix", rows=["a", "b"], cols=["x"]),
                           {1: "1:0 | 2:abc"})
    assert "row_weights" not in cfg[1]
    assert "第 2 行权重非法" in warnings[0]


def test_blank_matrix_keeps_rows_and_cols_only():
    cfg, _ = cfg_of(one("matrix", rows=["a"], cols=["x", "y"]), {1: ""})
    assert cfg[1] == {"type": "matrix", "rows": ["a"], "cols": ["x", "y"]}


# ------------------------------------------------------------ 排序


def test_blank_sort_means_random_order_and_stores_nothing():
    cfg, _ = cfg_of(one("sort", items=["a", "b"]), {1: ""})
    assert cfg[1] == {"type": "sort"}


def test_a_sort_order_of_known_items_is_kept():
    cfg, _ = cfg_of(one("sort", items=["a", "b", "c"]), {1: "c,a"})
    assert cfg[1]["order"] == ["c", "a"]


def test_one_unknown_item_voids_the_entire_order():
    """部分接受会让"前几名固定"变成"前几名随机"，比整份作废更难发现。"""
    cfg, warnings = cfg_of(one("sort", items=["a", "b"]), {1: "a,zzz"})
    assert "order" not in cfg[1]
    assert "整个顺序作废" in warnings[0]


# ------------------------------------------------------------ 其它


def test_an_unknown_type_with_no_text_is_skipped_entirely():
    cfg, warnings = cfg_of(one("mystery"), {1: ""})
    assert cfg == {}
    assert warnings == []


def test_an_unknown_type_with_numbers_is_taken_at_face_value():
    cfg, _ = cfg_of(one("mystery", choices=["a", "b"]), {1: "1,2"})
    assert cfg[1]["weights"] == [1.0, 2.0]


def test_an_unknown_type_with_junk_is_skipped_with_a_warning():
    cfg, warnings = cfg_of(one("mystery"), {1: "abc"})
    assert cfg == {}
    assert "未知题型" in warnings[0]


def test_the_anchor_is_computed_from_the_stem_not_copied_from_the_question():
    """探测输出的题目 dict 里**没有** ``anchor`` 这个键 —— 锚点是算出来的。

    上一版写的是 ``q.get("anchor")``，于是 webui 产出的整份配置一条锚点都没有：
    题号只是"保存时这道题在第几格"的遗迹，问卷中间插一题就整份错位，而错位是
    静默的（选项数恰好还来得及）。形状由 ``tests/test_weight_parser_parity.py``
    与 Tk 逐题对拍钉住。
    """
    cfg, _ = cfg_of(one(title="你最常用的浏览器", choices=["a", "b"]), {1: "1,1"})
    assert cfg[1]["anchor"] == {"title": "你最常用的浏览器",
                                "signature": "single:2"}


def test_a_question_without_a_stem_gets_no_anchor():
    cfg, _ = cfg_of(one(choices=["a", "b"]), {1: "1,1"})
    assert "anchor" not in cfg[1]


def test_a_question_without_a_number_is_ignored():
    cfg, _ = cfg_of([{"type": "single", "choices": ["a"]},
                     {"q": 2, "type": "single", "choices": ["a", "b"]}],
                    {2: "1,2"})
    assert list(cfg) == [2]


def test_text_keys_are_coerced_so_a_json_round_trip_still_works():
    """配置从 JSON 载入时题号是字符串，权重表按 int 存 —— 两边都要吃得下。"""
    cfg, _ = cfg_of(one(choices=["a", "b"]), {"1": "1,3"})
    assert cfg[1]["weights"] == [1.0, 3.0]


def test_a_blank_scale_keeps_the_structure_and_no_weights():
    cfg, _ = cfg_of(one("scale", scale=5, scale_min=1), {1: "   "})
    assert cfg[1] == {"type": "scale", "scale": 5, "scale_min": 1}


def test_a_scale_list_with_a_negative_is_rejected_but_the_structure_survives():
    cfg, warnings = cfg_of(one("scale", scale=3), {1: "1,-2,3"})
    assert cfg[1]["scale"] == 3
    assert "weights" not in cfg[1]
    assert "负数或非有限值" in warnings[0]


def test_a_question_number_that_is_not_an_int_is_skipped():
    cfg, _ = cfg_of([{"q": "abc", "type": "single", "choices": ["a"]}],
                    {1: "1"})
    assert cfg == {}
