"""真实答卷回放 ``src/reverse_fill.py`` 的离线测试。

按对标源（SurveyController ``software/core/reverse_fill/``）的三条契约组织：
表头认题、三种编码嗅探、成功提交才推进队列。我们这边多出来的两条同样要钉住：
**认不到就明说**（blocked，绝不按位置猜）与**解析不出就降级**（fallback 交回
现有的加权随机），两者都必须能被 ``preflight`` 用人话讲出来 —— 因为"回放开了
却没生效"这件事本身没有症状，只能靠启动前的告警让它显形。
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Any

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import reverse_fill  # noqa: E402
from src.reverse_fill import (  # noqa: E402
    STATUS_BLOCKED,
    STATUS_FALLBACK,
    STATUS_REVERSE,
    ReplayQueue,
    encode_cell,
    headers_of,
    load_table,
    override_answer,
    preflight,
    resolve_question_columns,
)

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "replay_answers.csv")

# 样例表的同一份内容。为什么要在这里也留一份：仓库根的 .gitignore 有一条 ``*.csv``
# （管 data/ 那类运行产物），所以 ``tests/fixtures/replay_answers.csv`` 在 fresh
# clone 里可能压根没被跟踪 —— 那种情况下"真的从盘上读一次 CSV"的端到端链路会整条
# 消失，而它恰恰是本模块唯一能验证"读表→认列→覆盖"接得起来的一条。
# 提交那个文件时要 ``git add -f``；两边内容必须一致，改样例表就一起改这里。
FIXTURE_TEXT = (
    "序号,1、您的性别,2、您最常用的功能,姓名,4、 满意度,"
    "5、请选择关注品类,q6,7、出行方式评价,8、城市\n"
    "1,1,其他____希望增加夜间班次,张伟,4,1|2,1,1!4;2!5;3!3,北京\n"
    "2,女,2,李娜,5分,2|3,2,4,上海\n"
    "3,3,不在这几个里,王强,9,1|3,3,价格!5;2!5;3!5,广州\n"
)
_FALLBACK_DIR: list[str] = []


def fixture_csv() -> str:
    """样例表的可读路径；文件被 ignore 掉时按同一份内容现场生成一个。"""
    if os.path.exists(FIXTURE):
        return FIXTURE
    if not _FALLBACK_DIR:
        _FALLBACK_DIR.append(tempfile.mkdtemp(prefix="replay_answers_"))
    path = os.path.join(_FALLBACK_DIR[0], "replay_answers.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(FIXTURE_TEXT)
    return path


def fixture_table() -> list[dict[str, str]]:
    """读样例表（走的就是 ``load_table`` 那条 CSV 路径）。"""
    return load_table(fixture_csv())

# 与 detection.detect_questions 的返回结构同形：choices 是**选项 value**（radio 的
# DOM value，1..N），option_texts 才是页面上那行字 —— 回放要能文本匹配就得带它。
QUESTIONS: list[dict[str, Any]] = [
    {"q": 1, "type": "single", "choices": [1, 2, 3],
     "option_texts": ["男", "女", "保密"], "title": "1、您的性别"},
    {"q": 2, "type": "single", "choices": [1, 2, 3], "blank_options": [3],
     "option_texts": ["满意", "一般", "其他____"], "title": "您最常用的功能"},
    {"q": 3, "type": "text", "field": "name", "title": "姓名"},
    {"q": 4, "type": "scale", "scale": 5, "scale_min": 1, "title": "满意度"},
    {"q": 5, "type": "multi", "choices": [1, 2, 3], "title": "请选择关注品类"},
    {"q": 6, "type": "sort", "items": ["1", "2", "3"], "sort_mode": "click",
     "title": "请按喜爱程度排序"},
    {"q": 7, "type": "matrix_single", "rows": [1, 2, 3], "cols": [1, 2, 3, 4, 5],
     "title": "出行方式评价"},
    {"q": 8, "type": "dropdown", "choices": ["A", "B", "C"], "title": "城市"},
]


def _q(qnum: int) -> dict[str, Any]:
    return next(q for q in QUESTIONS if q["q"] == qnum)


def _plan(headers: list[str], rows: list[dict[str, str]] | None = None):
    return resolve_question_columns(headers, QUESTIONS, sample_rows=rows)


# ===========================================================================
#  1. 读表
# ===========================================================================
def test_load_table_returns_rows_keyed_by_header() -> None:
    table = load_table(fixture_csv())
    assert len(table) == 3
    assert headers_of(table) == [
        "序号", "1、您的性别", "2、您最常用的功能", "姓名", "4、 满意度",
        "5、请选择关注品类", "q6", "7、出行方式评价", "8、城市",
    ]
    assert table[0]["姓名"] == "张伟"
    assert all(isinstance(v, str) for row in table for v in row.values())


def test_load_table_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_table(os.path.join(ROOT, "tests", "fixtures", "nope_missing.csv"))


def test_load_table_rejects_unknown_extension(tmp_path) -> None:
    bogus = tmp_path / "答卷.json"
    bogus.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_table(str(bogus))
    assert "CSV" in str(exc.value).upper()


def test_duplicate_headers_get_column_scoped_keys(tmp_path) -> None:
    csv_path = tmp_path / "dup.csv"
    csv_path.write_text("序号,其他,其他\n1,甲,乙\n", encoding="utf-8")
    table = load_table(str(csv_path))
    assert headers_of(table) == ["序号", "其他", "其他#3"]
    assert table[0]["其他"] == "甲" and table[0]["其他#3"] == "乙"


def test_gbk_csv_is_still_readable(tmp_path) -> None:
    """问卷星后台直导的 CSV 常见是 GBK 系；只认 UTF-8 的症状是"整表读成乱码"。"""
    path = tmp_path / "gbk.csv"
    path.write_bytes("序号,姓名\n1,张伟\n".encode("gb18030"))
    assert load_table(str(path))[0]["姓名"] == "张伟"


def test_xlsx_table_matches_the_csv_fixture(tmp_path) -> None:
    if not reverse_fill.has_openpyxl():
        pytest.skip("本机未安装 openpyxl，跳过 .xlsx 读表分支")
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["序号", "1、您的性别", "4、 满意度"])
    sheet.append([1, 1, 4.0])          # 数字格：必须是 "4" 而不是 "4.0"
    sheet.append([2, "女", "5分"])
    xlsx = tmp_path / "答卷.xlsx"
    book.save(str(xlsx))

    table = load_table(str(xlsx))
    assert headers_of(table) == ["序号", "1、您的性别", "4、 满意度"]
    assert [r["4、 满意度"] for r in table] == ["4", "5分"]
    assert table[0]["1、您的性别"] == "1"


def test_xlsx_without_openpyxl_raises_explicitly(tmp_path, monkeypatch) -> None:
    """不静默降级成空表 —— 那的样子是"回放开了没生效"。"""
    monkeypatch.setattr(reverse_fill, "openpyxl", None)
    xlsx = tmp_path / "anything.xlsx"
    xlsx.write_bytes(b"not really a workbook")
    with pytest.raises(RuntimeError) as exc:
        load_table(str(xlsx))
    text = str(exc.value)
    assert "openpyxl" in text and "CSV" in text.upper()


# ===========================================================================
#  2. 表头归一化（全半角 / 带序号 / 带空格）
# ===========================================================================
@pytest.mark.parametrize("header", [
    "3、姓名",
    "3. 姓名",
    "3．姓名",
    "３、姓名",                     # 全角数字
    "第3题 姓名",
    "（3）姓名",
    "(3) 姓名",
    "  3、  姓名  ",
    "3、姓名（必填）",
    "q3",
    "Q3",
    "姓名",
    "  姓名 ",
])
def test_header_variants_all_resolve_to_q3(header: str) -> None:
    binding = _plan([header]).bindings[0]
    assert binding.status == STATUS_REVERSE, binding.reason
    assert binding.qnum == 3


def test_latin_title_ignores_case_and_spacing() -> None:
    questions = [{"q": 9, "type": "text", "field": "name", "title": "Your Name"}]
    plan = resolve_question_columns(["YOUR  NAME"], questions)
    assert plan.bindings[0].qnum == 9
    assert plan.bindings[0].status == STATUS_REVERSE


def test_truncated_title_still_hits_because_120_char_cap() -> None:
    """探测回来的 title 被截到 120 字，导出表头却是整句 —— 必须仍能对上。"""
    long = "您对我们本次活动的组织安排满意吗" * 8
    questions = [{"q": 1, "type": "single", "choices": [1, 2], "title": long[:120]}]
    plan = resolve_question_columns([f"1、{long}？"], questions)
    assert plan.bindings[0].status == STATUS_REVERSE


# ===========================================================================
#  3. blocked / fallback：认不到与解析不出都不许瞎猜
# ===========================================================================
def test_unmatched_column_is_blocked_not_guessed() -> None:
    binding = _plan(["序号"]).bindings[0]
    assert binding.status == STATUS_BLOCKED
    assert binding.qnum is None
    assert "认不到" in binding.reason or "没有" in binding.reason or "题号" in binding.reason


def test_index_outside_this_survey_is_blocked() -> None:
    binding = _plan(["99、备注说明"]).bindings[0]
    assert binding.status == STATUS_BLOCKED
    assert "Q99" in binding.reason


def test_header_index_and_title_disagreement_is_blocked() -> None:
    """"1、姓名" 这种表（序号说 Q1、题干说 Q3）一定有一边是错的，宁可不回放。"""
    binding = _plan(["1、姓名"]).bindings[0]
    assert binding.status == STATUS_BLOCKED
    assert "Q1" in binding.reason and "Q3" in binding.reason


def test_ambiguous_title_is_blocked() -> None:
    questions = [
        {"q": 1, "type": "single", "choices": [1, 2], "title": "满意度评分"},
        {"q": 2, "type": "single", "choices": [1, 2], "title": "满意度评分"},
    ]
    plan = resolve_question_columns(["满意度评分"], questions)
    assert plan.bindings[0].status == STATUS_BLOCKED
    assert "不能猜" in plan.bindings[0].reason


def test_second_column_for_same_question_is_blocked() -> None:
    plan = _plan(["1、您的性别", "您的性别"])
    assert plan.bindings[0].status == STATUS_REVERSE
    assert plan.bindings[1].status == STATUS_BLOCKED
    assert "已被" in plan.bindings[1].reason


@pytest.mark.parametrize("header, qnum, keyword", [
    ("5、请选择关注品类", 5, "多选题"),
    ("q6", 6, "排序题"),
])
def test_unsupported_types_are_blocked_with_reason(
    header: str, qnum: int, keyword: str
) -> None:
    binding = _plan([header]).bindings[0]
    assert binding.status == STATUS_BLOCKED
    assert binding.qnum == qnum
    assert keyword in binding.reason


def test_matrix_multi_is_blocked_too() -> None:
    questions = [{"q": 1, "type": "matrix_multi", "rows": [1, 2], "cols": [1, 2],
                  "title": "评价"}]
    plan = resolve_question_columns(["1、评价"], questions)
    assert plan.bindings[0].status == STATUS_BLOCKED
    assert "矩阵多选" in plan.bindings[0].reason


def test_missing_choices_make_the_column_fallback() -> None:
    """题号对得上、但探测没带回选项列表 → 整列交回加权随机（不是 blocked）。"""
    questions = [{"q": 1, "type": "single", "choices": [], "title": "空选项题"}]
    plan = resolve_question_columns(["1、空选项题"], questions)
    assert plan.bindings[0].status == STATUS_FALLBACK
    assert plan.bindings[0].qnum == 1


def test_samples_that_never_parse_downgrade_to_fallback() -> None:
    rows = [{"8、城市": "北京"}, {"8、城市": "上海"}]
    plan = _plan(["8、城市"], rows)
    assert plan.bindings[0].status == STATUS_FALLBACK
    assert "解析不出" in plan.bindings[0].reason


def test_one_parseable_sample_keeps_the_column_reverse() -> None:
    plan = _plan(["8、城市"], [{"8、城市": "北京"}, {"8、城市": "A"}])
    assert plan.bindings[0].status == STATUS_REVERSE


# ===========================================================================
#  4. 三种编码嗅探
# ===========================================================================
def test_encode_ordinal_is_folded_to_zero_based_value() -> None:
    assert encode_cell("1", _q(1)) == {"type": "single", "selected": [1]}
    assert encode_cell("2", _q(1)) == {"type": "single", "selected": [2]}
    assert encode_cell("３", _q(1)) == {"type": "single", "selected": [3]}


def test_encode_out_of_range_ordinal_returns_none() -> None:
    assert encode_cell("7", _q(1)) is None
    assert encode_cell("0", _q(1)) is None


def test_encode_option_full_text() -> None:
    assert encode_cell("女", _q(1)) == {"type": "single", "selected": [2]}
    assert encode_cell(" 保 密 ", _q(1)) == {"type": "single", "selected": [3]}


def test_encode_option_value_beats_ordinal_for_numeric_choices() -> None:
    """选项分值型：choices 是 10/20/30 时，"20" 该认成第二项的值而不是第 20 项。"""
    q = {"q": 1, "type": "dropdown", "choices": [10, 20, 30], "title": "预算"}
    assert encode_cell("20", q) == {"type": "dropdown", "selected": [20]}


def test_encode_dropdown_text_choices() -> None:
    assert encode_cell("B", _q(8)) == {"type": "dropdown", "selected": ["B"]}
    assert encode_cell("北京", _q(8)) is None


def test_encode_option_with_blank_produces_both_parts() -> None:
    ans = encode_cell("其他____希望增加夜间班次", _q(2))
    assert ans == {"type": "single", "selected": [3],
                   "option_blank_text": "希望增加夜间班次"}
    assert encode_cell("其他：临时有事", _q(2))["option_blank_text"] == "临时有事"


def test_encode_plain_option_without_blank_drops_extra_text() -> None:
    """题面没有填空框时不产出 option_blank_text：多出来的字没地方写。"""
    ans = encode_cell("满意", _q(2))
    assert ans == {"type": "single", "selected": [1]}
    assert "option_blank_text" not in ans


def test_encode_option_text_not_on_the_page_is_fallback() -> None:
    assert encode_cell("不在这几个里", _q(2)) is None


def test_encode_scale_accepts_unit_suffix_and_bounds() -> None:
    assert encode_cell("4", _q(4)) == {"type": "scale", "value": 4}
    assert encode_cell("5分", _q(4)) == {"type": "scale", "value": 5}
    assert encode_cell("9", _q(4)) is None
    assert encode_cell("非常满意", _q(4)) is None


def test_encode_scale_honours_non_one_minimum() -> None:
    q = {"q": 4, "type": "scale", "scale": 10, "scale_min": 2}
    assert encode_cell("1", q) is None
    assert encode_cell("2", q) == {"type": "scale", "value": 2}


def test_encode_text_question_is_verbatim() -> None:
    assert encode_cell("张伟", _q(3)) == {"type": "text", "text": "张伟", "field": "name"}


def test_encode_numeric_and_date_fields_validate_platform_constraints() -> None:
    age = {"q": 5, "type": "text", "field": "age", "min": 16, "max": 70}
    assert encode_cell("45", age) == {"type": "text", "text": "45", "field": "age"}
    assert encode_cell("保密", age) is None
    date = {"q": 6, "type": "text", "field": "date", "date_kind": "date",
            "date_min": "1990-01-01", "date_max": "2005-12-31"}
    assert encode_cell("1995-03-04", date) is not None
    assert encode_cell("2019-01-01", date) is None      # 越界会被平台清掉
    assert encode_cell("小时候", date) is None


def test_encode_matrix_row_bang_col_covers_every_row() -> None:
    assert encode_cell("1!4;2!5;3!3", _q(7)) == {
        "type": "matrix_single", "rows": {1: 4, 2: 5, 3: 3}}


def test_encode_matrix_single_value_applies_to_every_row() -> None:
    assert encode_cell("4", _q(7)) == {
        "type": "matrix_single", "rows": {1: 4, 2: 4, 3: 4}}


def test_encode_matrix_partial_rows_returns_none() -> None:
    """半张矩阵 = 剩下那些行整题空着，平台按未答拦下整题；不如整题交回随机。"""
    assert encode_cell("1!4;2!5", _q(7)) is None
    assert encode_cell("价格!5;2!5;3!5", _q(7)) is None   # 探测只带回行号，标签认不出


def test_encode_matrix_scale_keeps_slot_names_as_rows() -> None:
    q = {"q": 7, "type": "matrix_scale", "rows": ["q7_0", "q7_1"], "cols": [1, 2, 3]}
    assert encode_cell("q7_0!2;q7_1!3", q) == {
        "type": "matrix_scale", "rows": {"q7_0": 2, "q7_1": 3}}
    assert encode_cell("1", q) == {"type": "matrix_scale", "rows": {"q7_0": 1, "q7_1": 1}}


@pytest.mark.parametrize("qnum", [5, 6])
def test_encode_cell_refuses_unsupported_types(qnum: int) -> None:
    assert encode_cell("1", _q(qnum)) is None
    assert encode_cell("1|2", _q(qnum)) is None


@pytest.mark.parametrize("cell", ["", "   ", "\t"])
def test_empty_cell_is_not_an_answer(cell: str) -> None:
    assert encode_cell(cell, _q(1)) is None


def test_answer_shape_matches_generate_answer() -> None:
    """回放答案必须能原样替换生成器的返回值 —— 键与取值域都要一致。"""
    from src.answering_v2 import generate_answer

    for qnum in (1, 8):
        q = _q(qnum)
        mine = encode_cell("1", q)
        theirs = generate_answer(dict(q, weights=None))
        assert mine is not None
        assert mine["type"] == theirs["type"]
        assert set(mine) <= {"type", "selected", "option_blank_text"}
        assert all(v in q["choices"] for v in mine["selected"])
        assert theirs["selected"][0] in q["choices"]


# ===========================================================================
#  5. 作答期的接缝
# ===========================================================================
def test_override_answer_wins_for_reverse_columns() -> None:
    row = fixture_table()[0]
    plan = _plan(headers_of(fixture_table()), fixture_table())
    assert override_answer(_q(1), row, plan) == {"type": "single", "selected": [1]}
    assert override_answer(_q(3), row, plan) == {
        "type": "text", "text": "张伟", "field": "name"}


def test_override_answer_gives_up_quietly() -> None:
    plan = _plan(["1、您的性别"])
    row = {"1、您的性别": "女"}
    assert override_answer(_q(1), None, plan) is None           # 行用尽
    assert override_answer(_q(4), row, plan) is None            # 这道题没有列
    assert override_answer(_q(1), {"1、您的性别": ""}, plan) is None   # 空格子
    assert override_answer(_q(1), {"1、您的性别": "外星"}, plan) is None  # 解析不出
    assert override_answer({"type": "single"}, row, plan) is None      # 题号都没有


def test_override_answer_does_not_replay_blocked_or_fallback_columns() -> None:
    rows = [{"5、请选择关注品类": "1|2", "8、城市": "北京"}]
    plan = _plan(["5、请选择关注品类", "8、城市"], rows)
    assert override_answer(_q(5), rows[0], plan) is None        # blocked：多选
    assert override_answer(_q(8), rows[0], plan) is None        # fallback：城市列解析不出


# ===========================================================================
#  6. ReplayQueue：成功提交才推进
# ===========================================================================
def test_peek_is_idempotent_for_retries() -> None:
    table = fixture_table()
    queue = ReplayQueue(table)
    first = queue.peek(1)
    assert first == table[0]
    assert queue.peek(1) == first, "同一份重试必须拿到同一行（重复提交防线）"
    queue.mark_consumed(1)
    assert queue.peek(1) == first, "已消费过也仍按幂等返回，绝不换行"


def test_peek_advances_per_submission() -> None:
    table = fixture_table()
    queue = ReplayQueue(table)
    assert queue.peek(1) == table[0]
    assert queue.peek(2) == table[1]
    assert queue.peek(3) == table[2]


def test_exhausted_queue_returns_none_and_reports() -> None:
    table = fixture_table()
    queue = ReplayQueue(table)
    assert queue.remaining == 3
    for i in (1, 2, 3):
        queue.peek(i)
    assert queue.exhausted
    assert queue.peek(4) is None, "行用尽 → None，调用方回退正常生成"
    queue.mark_consumed(4)
    assert queue.peek(4) is None


def test_failed_submission_does_not_consume_a_second_row() -> None:
    table = fixture_table()
    queue = ReplayQueue(table)
    attempt1 = queue.peek(1)
    queue.peek(2)                     # 下一份已经拿到第 2 行
    assert queue.peek(1) == attempt1  # 第 1 份失败重投，仍是第 1 行
    assert queue.consumed_indices == set()
    queue.mark_consumed(1)
    assert queue.consumed_indices == {1}


def test_skip_lets_a_resumed_batch_continue_where_it_stopped() -> None:
    table = fixture_table()
    queue = ReplayQueue(table, skip=2)
    assert queue.peek(8) == table[2]
    assert queue.remaining == 0


def test_mark_consumed_without_peek_burns_a_row() -> None:
    queue = ReplayQueue(fixture_table())
    queue.mark_consumed(1)
    assert queue.remaining == 2
    assert queue.peek(2) == fixture_table()[1]


def test_queue_copies_rows_so_callers_cannot_mutate_the_table() -> None:
    table = fixture_table()
    queue = ReplayQueue(table)
    row = queue.peek(1)
    assert row is not None
    row["姓名"] = "篡改"
    again = queue.peek(1)
    assert again is not None and again["姓名"] == "张伟"


# ===========================================================================
#  7. preflight：只在启动前告警，不抛异常
# ===========================================================================
def test_preflight_reports_sample_shortfall() -> None:
    lines = preflight(fixture_table(), QUESTIONS, target_submissions=20)
    hits = [ln for ln in lines if "答卷表只有 3 行" in ln]
    assert len(hits) == 1
    assert "第 4 份" in hits[0] and "回退正常生成" in hits[0]


def test_preflight_silent_when_enough_samples() -> None:
    lines = preflight(fixture_table(), QUESTIONS, target_submissions=3)
    assert not [ln for ln in lines if "答卷表只有" in ln]


def test_preflight_lists_blocked_and_fallback_columns() -> None:
    lines = preflight(fixture_table(), QUESTIONS)
    text = "\n".join(lines)
    assert "序号" in text                                  # 认不到题号的列
    assert "多选题" in text and "排序题" in text            # 本版本不支持
    assert "城市" in text and "解析不出" in text            # 嗅探降级


def test_preflight_lists_questions_that_have_no_column() -> None:
    """探测到了、表里却没有对应列的题也要讲出来 —— 它不会报错，只会安静地随机生成。"""
    extra = list(QUESTIONS) + [{"q": 9, "type": "text", "field": None, "title": "备注"}]
    lines = preflight(fixture_table(), extra)
    assert any("Q9" in ln and "照旧随机生成" in ln for ln in lines)


def test_preflight_on_empty_table_does_not_raise() -> None:
    lines = preflight([], QUESTIONS, target_submissions=5)
    assert any("空的" in ln for ln in lines)
    assert any("答卷表只有 0 行" in ln for ln in lines)


def test_preflight_warns_when_detection_has_no_titles() -> None:
    """没有题干就只能按表头序号认题，题序一变整排错位 —— 必须提示。"""
    bare = [{"q": q["q"], "type": q["type"], "choices": [1, 2]} for q in QUESTIONS]
    lines = preflight(fixture_table(), bare)
    assert any("没带回题干" in ln for ln in lines)


# ===========================================================================
#  8. 端到端：样例表 → 列计划 → 逐份覆盖
# ===========================================================================
def test_fixture_end_to_end_column_plan() -> None:
    table = fixture_table()
    plan = _plan(headers_of(table), table)
    status = {b.header: b.status for b in plan.bindings}
    assert status["序号"] == STATUS_BLOCKED
    assert status["1、您的性别"] == STATUS_REVERSE
    assert status["2、您最常用的功能"] == STATUS_REVERSE
    assert status["姓名"] == STATUS_REVERSE
    assert status["4、 满意度"] == STATUS_REVERSE
    assert status["5、请选择关注品类"] == STATUS_BLOCKED
    assert status["q6"] == STATUS_BLOCKED
    assert status["7、出行方式评价"] == STATUS_REVERSE
    assert status["8、城市"] == STATUS_FALLBACK


def test_fixture_end_to_end_three_submissions() -> None:
    table = fixture_table()
    plan = _plan(headers_of(table), table)
    queue = ReplayQueue(table)

    got_first = queue.peek(1)
    assert override_answer(_q(1), got_first, plan) == {"type": "single", "selected": [1]}
    assert override_answer(_q(2), got_first, plan) == {
        "type": "single", "selected": [3], "option_blank_text": "希望增加夜间班次"}
    assert override_answer(_q(7), got_first, plan) == {
        "type": "matrix_single", "rows": {1: 4, 2: 5, 3: 3}}
    queue.mark_consumed(1)

    got_second = queue.peek(2)
    assert override_answer(_q(1), got_second, plan) == {"type": "single", "selected": [2]}
    assert override_answer(_q(4), got_second, plan) == {"type": "scale", "value": 5}
    assert override_answer(_q(7), got_second, plan) == {
        "type": "matrix_single", "rows": {1: 4, 2: 4, 3: 4}}
    queue.mark_consumed(2)

    got_third = queue.peek(3)
    # 第 3 行三处都解析不出 → 那三题该份交回现有加权随机，而不是硬塞一个值
    assert override_answer(_q(2), got_third, plan) is None
    assert override_answer(_q(4), got_third, plan) is None
    assert override_answer(_q(7), got_third, plan) is None
    assert override_answer(_q(1), got_third, plan) == {"type": "single", "selected": [3]}
    queue.mark_consumed(3)

    assert queue.peek(4) is None
    assert queue.remaining == 0


# ===========================================================================
#  8. 批次状态层（CLI --replay-file 开起来、作答链逐题来问的那一段）
# ===========================================================================
@pytest.fixture()
def replay_on():
    reverse_fill.begin_replay(fixture_csv(), target_submissions=2)
    yield
    reverse_fill.end_replay()


def test_begin_replay_reports_a_short_table_before_the_run_starts() -> None:
    notes = reverse_fill.begin_replay(fixture_csv(), target_submissions=99)
    try:
        assert reverse_fill.replaying() is True
        assert reverse_fill.remaining_rows() == 3
        assert any("99" in n and "回退" in n for n in notes), notes
    finally:
        reverse_fill.end_replay()
    assert reverse_fill.replaying() is False


def test_answer_for_question_overrides_with_the_row_value(replay_on) -> None:
    got = reverse_fill.answer_for_question(_q(1), 1)
    assert got is not None and got["type"] == "single"
    assert got["selected"] == [1], f"第 1 行那一格是 1（男）: {got}"


def test_answer_for_question_is_none_when_replay_is_off() -> None:
    reverse_fill.end_replay()
    assert reverse_fill.answer_for_question(_q(1), 1) is None


def test_answer_for_question_needs_a_submission_index(replay_on) -> None:
    assert reverse_fill.answer_for_question(_q(1), None) is None


def test_exhausted_table_falls_back_to_generation(replay_on, capsys) -> None:
    """队列是游标不是查表：同一个 submission_index 反复问拿同一行，新 index 才前进。

    样例表 3 行 → 前三个 index 各拿一行，第四个开始没有行，交回正常随机。
    """
    for index in (1, 2, 3):
        assert reverse_fill.answer_for_question(_q(1), index) is not None
    assert reverse_fill.answer_for_question(_q(1), 4) is None
    assert reverse_fill.answer_for_question(_q(1), 5) is None
    assert reverse_fill.answer_for_question(_q(1), 1) is not None, "已分过行的 index 必须还拿原来那行"
    out = capsys.readouterr().out
    assert out.count("答卷表已用尽") == 1, f"同一道题的降级只该说一次:\n{out}"


def test_mark_consumed_advances_only_on_success(replay_on) -> None:
    before = reverse_fill.remaining_rows()
    assert reverse_fill.mark_consumed(1) == before - 1
    assert reverse_fill.mark_consumed(None) == -1, "没开回放时返回 -1 而不是崩"


def test_reset_for_survey_drops_the_per_question_binding_cache(replay_on) -> None:
    reverse_fill.answer_for_question(_q(1), 1)
    session = reverse_fill._session
    assert session is not None and session._plans
    reverse_fill.reset_for_survey()
    assert session._plans == {} and session._noted == set()
