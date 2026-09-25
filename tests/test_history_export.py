"""``src/history_export.py`` —— 历史导出的 CSV 格式契约（两个宿主共用的一份）。

为什么单独一个模块：桌面版 ``gui/history_panel.export_csv`` 与 Web 控制台的
``GET /api/history/export`` 必须导出**同一份字节**，否则"从界面导出的那份 CSV"
就没有确定含义。这里钉的是格式本身；两个宿主各自只测"字节去对了地方"。

注意这里刻意分两层断言：``csv_safe`` 的单元测试证明**函数**对，
而 ``runs_csv``/``answers_csv`` 的测试证明**导出这条路径真的调了它** ——
只测前者时，谁把某一列的防护漏掉都是绿的（v2.6 那条注入防线当初就是这么个形状）。
"""

from __future__ import annotations

import pytest

from src.history_export import (
    ANSWERS_HEADER,
    EXPORTS,
    RUNS_HEADER,
    answers_csv,
    build,
    csv_safe,
    runs_csv,
)

MALICIOUS = "=cmd|'/C calc'!A0"

RUN = {
    "id": 7, "started_at": "2026-09-25 11:30:07",
    "finished_at": "2026-09-25 11:30:50", "status": "finished",
    "survey_url": "https://www.wjx.cn/vm/abc.aspx",
    "total_submissions": 3, "success_count": 3, "fail_count": 0,
    "error_message": None,
}

ANSWER = {
    "run_id": 7, "submission_index": 1, "question_number": 2,
    "question_type": "multi", "options_selected": [0, 2],
    "text_answer": "张三", "elapsed_ms": 1234,
    "created_at": "2026-09-25 11:30:10",
}


# ------------------------------------------------------------ 注入防护本体


@pytest.mark.parametrize("dangerous", [
    MALICIOUS, "+1-1", "-2+3", "@SUM(A1)", "\t=tabbed", "\r=carriage",
])
def test_csv_safe_prefixes_formula_leading_chars(dangerous: str) -> None:
    out = csv_safe(dangerous)
    assert out == "'" + dangerous
    assert out[0] == "'", "首字符必须被单引号顶掉，Excel 才不会当公式求值"


@pytest.mark.parametrize("harmless", [
    "普通文本", "https://www.wjx.cn/vm/abc.aspx", "13800000000",
    "张", "[0, 1]", "", None, 42, 0.5,
])
def test_csv_safe_leaves_ordinary_values_alone(harmless) -> None:
    expected = "" if harmless is None else str(harmless)
    assert csv_safe(harmless) == expected


# --------------------------------------------------- 导出这条路径真的过了防护


def test_a_poisoned_survey_url_stays_poisoned_all_the_way_into_the_csv() -> None:
    """页面控制的文本（问卷 URL、报错信息）会以 = 开头 —— 必须带着前缀落进文件。

    这一条钉的不是 ``csv_safe`` 对不对，而是**导出有没有去调它**。
    """
    evil = {**RUN, "survey_url": MALICIOUS, "error_message": "@SUM(A1)"}
    lines = runs_csv([evil]).splitlines()
    assert lines[0] == ",".join(RUNS_HEADER)
    assert f"'{MALICIOUS}" in lines[1], lines[1]
    assert "'@SUM(A1)" in lines[1], lines[1]


def test_text_answer_and_selected_options_both_pass_the_guard() -> None:
    evil = {**ANSWER, "options_selected": ["=A1"], "text_answer": "+1-1"}
    row = answers_csv([evil]).splitlines()[1]
    assert "'=A1" in row and "'+1-1" in row, row


def test_options_selected_list_is_flattened_with_commas() -> None:
    row = answers_csv([ANSWER]).splitlines()[1]
    assert '"0,2"' in row, f"JSON 数组要拍成逗号串：{row}"


def test_the_header_and_column_order_are_pinned() -> None:
    """列序变了，别人手里那份"上周导出的 CSV"就对不上号 —— 所以逐列钉住。"""
    assert runs_csv([RUN]).splitlines()[0] == ",".join(RUNS_HEADER)
    assert answers_csv([ANSWER]).splitlines()[0] == ",".join(ANSWERS_HEADER)
    assert len(runs_csv([RUN]).splitlines()) == 2
    assert len(answers_csv([ANSWER]).splitlines()) == 2


def test_crlf_line_endings_survive_so_excel_sees_one_row_per_line() -> None:
    """``csv`` 默认就是 ``\\r\\n``；哪天有人改成 ``\\n``，Excel 会把整表挤成一行。"""
    assert runs_csv([RUN, RUN]).count("\r\n") == 3


def test_no_cell_is_invented_when_a_column_is_missing() -> None:
    """探测早期落库的行可能缺列 —— 缺就留空，不要写 "None" 让人以为是数据。"""
    row = runs_csv([{"id": 1}]).splitlines()[1]
    assert row.startswith("1,"), row
    assert "None" not in row


# --------------------------------------------------------------- 导出名单


def test_the_export_kinds_the_ui_offers_are_the_ones_build_understands() -> None:
    """界面按钮、HTTP 参数、文件名三处共用这份名单，写死两遍就会对不上。"""
    assert sorted(EXPORTS) == ["answers", "runs"]
    assert build("runs", [RUN])[0] == "history_runs.csv"
    assert build("answers", [ANSWER])[0] == "history_answers.csv"
    with pytest.raises(KeyError):
        build("../etc/passwd", [])
