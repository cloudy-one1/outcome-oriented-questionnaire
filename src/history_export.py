"""历史记录导出成 CSV 的**格式** —— 谁写盘、谁发下载都共用这一份。

设计稿：``docs/design/DESIGN_webui.md`` §8（``/api/history/export``）。

为什么在有两个宿主之前就抽出来：桌面版 ``gui/history_panel.export_csv`` 已经把
表头、列顺序、``options_selected`` 的 list→逗号串、以及 CSV 注入防护写了一遍。
Web 控制台要导出的必须是**同一份字节**，否则两边各自演化之后，"从界面导出的
那份 CSV"就没有确定含义 —— 这正是权重解析那 173 行刚付过账的形状（见
``src/weight_text.py`` 的来历说明）。

所以本模块只管"给我 runs/answers，我给你 CSV 文本"，**不碰路径也不碰字节去向**：
桌面版写进用户选的文件，webui 作为 HTTP 下载发出去。

编码：``utf-8-sig``（带 BOM）由调用方负责 —— 那是"Excel 双击要认得出中文"这一条
针对文件的决定，而 webui 走下载头时同样需要它，所以放在这里说清楚，不藏进函数里。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping
from typing import Any

RUNS_HEADER = ["id", "started_at", "finished_at", "status", "survey_url",
               "total", "success", "fail", "error_message"]

ANSWERS_HEADER = ["run_id", "submission_index", "question_number",
                  "question_type", "options_selected", "text_answer",
                  "elapsed_ms", "created_at"]


def csv_safe(value: Any) -> str:
    """给可能被 Excel 当公式解析的单元格加前缀单引号（CSV injection）。

    导出的 text_answer / options_selected / survey_url / error_message 里
    含有**由问卷页面控制**的文本（下拉选项 label、driver 回传的报错信息），
    以 ``=`` ``+`` ``-`` ``@`` ``TAB`` ``CR`` 开头的值会在 Excel/WPS 里被当作
    公式求值，可外带数据（如 ``=CMD|' /C calc'!A0``）。csv 模块只管引号转义，
    不管这个 —— 必须在写入前拦截。
    """
    s = "" if value is None else str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _selected_to_text(raw: Any) -> str:
    """``options_selected`` 存的是 JSON 数组，导出时拍成逗号串。"""
    if isinstance(raw, (list, tuple)):
        return ",".join(str(x) for x in raw)
    return str(raw or "")


def runs_rows(runs: Iterable[Mapping[str, Any]]) -> list[list[Any]]:
    return [
        [r.get("id"), r.get("started_at"), r.get("finished_at"), r.get("status"),
         csv_safe(r.get("survey_url")), r.get("total_submissions"),
         r.get("success_count"), r.get("fail_count"),
         csv_safe(r.get("error_message"))]
        for r in runs
    ]


def answers_rows(answers: Iterable[Mapping[str, Any]]) -> list[list[Any]]:
    return [
        [a.get("run_id"), a.get("submission_index"), a.get("question_number"),
         a.get("question_type"),
         csv_safe(_selected_to_text(a.get("options_selected"))),
         csv_safe(a.get("text_answer")),
         a.get("elapsed_ms"), a.get("created_at")]
        for a in answers
    ]


def _to_text(header: list[str], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def runs_csv(runs: Iterable[Mapping[str, Any]]) -> str:
    return _to_text(RUNS_HEADER, runs_rows(runs))


def answers_csv(answers: Iterable[Mapping[str, Any]]) -> str:
    return _to_text(ANSWERS_HEADER, answers_rows(answers))


#: 导出 kinds —— 界面按钮与 HTTP 参数都以这份为准，避免两边各写一份名单
EXPORTS: dict[str, tuple[str, list[str]]] = {
    "runs": ("history_runs.csv", RUNS_HEADER),
    "answers": ("history_answers.csv", ANSWERS_HEADER),
}


def build(kind: str, rows: Iterable[Mapping[str, Any]]) -> tuple[str, str]:
    """``kind`` → ``(建议文件名, CSV 文本)``。未知 kind 抛 ``KeyError``（由 api 层转 400）。"""
    filename, _header = EXPORTS[kind]
    if kind == "answers":
        return filename, answers_csv(rows)
    return filename, runs_csv(rows)


__all__ = ["RUNS_HEADER", "ANSWERS_HEADER", "EXPORTS", "csv_safe", "runs_rows",
           "answers_rows", "runs_csv", "answers_csv", "build"]
