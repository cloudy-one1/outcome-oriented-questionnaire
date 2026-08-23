"""矩阵单选 DOM 交互实现（第一章第 2 条拆分 + 第六章 JS 模块化）。"""

from __future__ import annotations

from typing import Any

from ._common import js_execute_retry
from ._scripts import fill_matrix_single_script


@js_execute_retry()
def js_fill_matrix_single(
    driver: Any,
    q: int,
    row_selections: dict[Any, Any],
) -> bool:
    """矩阵单选：一次性设置 Q``q`` 的所有行选择。

    JS 实现集中在 `interactions._scripts.fill_matrix_single_script`（第六章模块化）。
    """
    return driver.execute_script(fill_matrix_single_script(q, row_selections))
