"""下拉选择题型 DOM 交互实现（第一章第 2 条拆分 + 第六章 JS 模块化）。"""

from __future__ import annotations

from typing import Any

from ._common import js_execute_retry
from ._scripts import select_dropdown_script


@js_execute_retry()
def js_select_dropdown(driver: Any, q: int, choice_value: Any) -> bool:
    """为 Q``q`` 的 <select> 下拉选择指定选项值 ``choice_value``。

    JS 实现集中在 `interactions._scripts.select_dropdown_script`（第六章模块化）。
    """
    return driver.execute_script(select_dropdown_script(q, choice_value))
