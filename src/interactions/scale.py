"""量表打分题型 DOM 交互实现（第一章第 2 条拆分 + 第六章 JS 模块化）。"""

from __future__ import annotations

from typing import Any

from ._common import js_execute_retry
from ._scripts import set_scale_script


@js_execute_retry()
def js_set_scale(driver: Any, q: int, value: int, scale_max: int | None = None) -> bool:
    """为 Q``q`` 打量表分数 ``value``（1-based）。

    JS 实现集中在 `interactions._scripts.set_scale_script`（第六章模块化）。
    """
    return driver.execute_script(set_scale_script(q, value, scale_max))
