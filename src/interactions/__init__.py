"""`interactions` 包公共 API（原有 `interaction.py` 的兼容 re-export）。

调用方两种用法均可：
    # 按题型 import（V2.3 推荐）
    from src.interactions.choices import js_click_option

    # 兼容旧调用方式（所有符号都从包根 import）
    from src.interactions import js_click_option, SUBMIT_SUCCESS, ...
"""

from __future__ import annotations

# 题型：单选/多选
from .choices import js_click_option, js_click_question_options
# 题型：填空
from .text import js_fill_text
# 题型：量表
from .scale import js_set_scale
# 题型：下拉
from .dropdown import js_select_dropdown
# 题型：矩阵
from .matrix import js_fill_matrix_single
# 提交模块：三态常量 + 查找提交按钮 + 探测效果
from .submit import (
    SUBMIT_FAILED,
    SUBMIT_SUCCESS,
    SUBMIT_UNKNOWN,
    SubmitOutcome,
    _wait_until_submit_effect,
    find_and_click_submit,
)
# 通用：装饰器 + 点击后 sleep（保持原 interaction.py 的公开符号集合不变）
from ._common import click_after_pause, js_execute_retry


__all__ = [
    # 题型
    "js_click_option",
    "js_click_question_options",
    "js_fill_text",
    "js_set_scale",
    "js_select_dropdown",
    "js_fill_matrix_single",
    # 提交
    "SUBMIT_SUCCESS",
    "SUBMIT_FAILED",
    "SUBMIT_UNKNOWN",
    "SubmitOutcome",
    "find_and_click_submit",
    "_wait_until_submit_effect",
    # 通用
    "click_after_pause",
    "js_execute_retry",
]
