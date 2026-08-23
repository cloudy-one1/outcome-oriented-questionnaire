"""填空题型 DOM 交互实现（第一章第 2 条拆分 + 第六章 JS 模块化）。"""

from __future__ import annotations

from typing import Any

from ._common import js_execute_retry
from ._scripts import fill_text_script


@js_execute_retry()
def js_fill_text(driver: Any, q: int, text: str) -> bool:
    """通过 JS 注入 + 人类行为事件链，将 ``text`` 填入 Q``q`` 的文本框/文本域。

    JS 实现集中在 `interactions._scripts.fill_text_script`（第六章模块化）。
    """
    return driver.execute_script(fill_text_script(q, text))
