"""排序题 DOM 交互实现（v3.0 新增）。

结构前提（与 ``src/detection.py`` 的 5b 节同一套约定）：
    ``ul.lisort`` 里每个 ``li`` 带 ``value`` / ``data-value`` / ``data-id`` 之一，
    提交值由同域的 ``input[name=qN]`` 承载（逗号串）。
真实页面上若这套约定不成立，``js_fill_sort`` 会返回 False，该题被记为答题失败 ——
宁可失败，也不交一份"只重排了 DOM、提交值是空的"排序题。
"""

from __future__ import annotations

from typing import Any

from ._common import js_execute_retry
from ._scripts import fill_sort_script


@js_execute_retry()
def js_fill_sort(driver: Any, q: int, order: list[Any]) -> bool:
    """把 Q``q`` 的排序列表按 ``order``（item id 序列）定稿。"""
    return driver.execute_script(fill_sort_script(q, order))
