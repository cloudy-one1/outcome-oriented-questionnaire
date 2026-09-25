"""权重表的**界面侧**：题型标签、"选项/空数"那一列、以及探测完预填的默认串。

设计稿：``docs/design/DESIGN_webui.md`` §5 末与 §10 步骤 4。

**解析不在这里**：第 4 列文本 → ``WEIGHT_CONFIG`` 那份规则已收进
``src.weight_text.parse_weight_texts``，桌面版与 webui 共用同一份。它的来历（先各写
一份、逐题型对拍、再合并）与逐条语义都写在那模块的 docstring 里。这里只剩
"怎么把一道题显示给人看"。
"""

from __future__ import annotations

from typing import Any

from src.models import normalize_question_type
from src.weight_text import (
    CHOICE_TYPES,
    MATRIX_LIKE_TYPES,
    SCALE_TYPES,
    SORT_TYPES,
    TEXT_TYPES,
    scale_levels,
)

# 存储名 → 展示标签，与 gui/weight_panel 的胶囊文案对齐
TYPE_LABELS: dict[str, str] = {
    "single": "单选",
    "multi": "多选",
    "dropdown": "下拉",
    "scale": "量表",
    "text": "填空",
    "matrix": "矩阵",
    "matrix_multi": "矩多",
    # 量表式矩阵：只在显示上归进矩阵一族，解析上它走未知题型兜底（理由见
    # src/weight_text.MATRIX_LIKE_TYPES 的注释）
    "matrix_scale": "矩量",
    "sort": "排序",
}

_FIELD_LABELS = {
    "name": "姓名字段", "phone": "手机字段", "mobile": "手机字段",
    "tel": "手机字段", "email": "邮箱字段",
    "address": "地址字段", "addr": "地址字段", "age": "年龄字段",
    "company": "公司字段", "org": "公司字段",
}


def type_label(qtype: Any) -> str:
    return TYPE_LABELS.get(normalize_question_type(str(qtype or "")), "其它")


def default_text_for(q: dict) -> str:
    """探测完给每行预填的默认串 —— 留空即"等权重随机"，所以矩阵/排序不预填。"""
    storage = normalize_question_type(str(q.get("type", "single")))
    if storage in CHOICE_TYPES:
        n = len(q.get("choices", []))
        if not n:
            return ""
        return ",".join(f"{1.0 / n:.4f}" for _ in range(n))
    if storage in SCALE_TYPES:
        return ",".join(["1"] * scale_levels(q))
    if storage in TEXT_TYPES:
        return ",".join(str(x) for x in (q.get("options") or []))
    return ""


def _int_or(value: Any, default: int) -> int:
    """题面没给这个字段时按默认值；给了 **0 就当 0**。

    ``int(q.get("scale_min", 1) or 1)`` 这种写法看着安全，实际把 0 起评的量表
    显示成 "1~10" —— 而 0~10（NPS）与 2~10 都是探测真会输出的形态。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def scale_label(q: dict) -> str:
    """第 3 列"选项/空数"该写什么。"""
    storage = normalize_question_type(str(q.get("type", "single")))
    if storage in CHOICE_TYPES:
        return str(len(q.get("choices", [])))
    if storage in SCALE_TYPES:
        return f"{_int_or(q.get('scale_min'), 1)}~{_int_or(q.get('scale'), 5)}"
    if storage in TEXT_TYPES:
        return _FIELD_LABELS.get(str(q.get("field") or ""), "自由文本")
    if storage in MATRIX_LIKE_TYPES:
        return f"{len(q.get('rows', []))}行 × {len(q.get('cols', []))}列"
    if storage in SORT_TYPES:
        return f"{len(q.get('items', []))} 项可排"
    return str(len(q.get("choices", [])))


__all__ = ["default_text_for", "scale_label", "type_label", "TYPE_LABELS"]
