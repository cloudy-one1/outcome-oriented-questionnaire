"""DOM 级别交互模块（V2.3 第一章第 2 条拆分后：兼容转发层）。

为保持对上层 `from .interaction import ...` 的调用契约不变，所有符号
均从拆分后的 `src/interactions/` 子包 re-export。

**新代码**推荐直接按题型 import：
    from .interactions.choices import js_click_option
    from .interactions.submit import find_and_click_submit, SUBMIT_SUCCESS
"""

from __future__ import annotations

# 公共 API：与原 interaction.py 完全相同的符号集合（保证 `from src.interaction import X` 不变）
from .interactions import (
    SUBMIT_FAILED,
    SUBMIT_SUCCESS,
    SUBMIT_UNKNOWN,
    SubmitOutcome,
    _wait_until_submit_effect,
    click_after_pause,
    find_and_click_submit,
    js_click_option,
    js_click_question_options,
    js_execute_retry,
    js_fill_matrix_single,
    js_fill_text,
    js_select_dropdown,
    js_set_scale,
)

__all__ = [
    "SUBMIT_SUCCESS",
    "SUBMIT_FAILED",
    "SUBMIT_UNKNOWN",
    "SubmitOutcome",
    "find_and_click_submit",
    "_wait_until_submit_effect",
    "click_after_pause",
    "js_execute_retry",
    "js_click_option",
    "js_click_question_options",
    "js_fill_text",
    "js_set_scale",
    "js_select_dropdown",
    "js_fill_matrix_single",
]
