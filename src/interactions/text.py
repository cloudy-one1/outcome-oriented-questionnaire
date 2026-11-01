"""填空题型 DOM 交互实现（第一章第 2 条拆分 + 第六章 JS 模块化）。

v4.4：js_fill_text 增加降级定位重试（``fallback_locate`` 开关 + 题干/属性
锚点，见 ``_relocate.py``）—— 主选择器失配（平台改版）时的第二次尝试，默认关。
"""

from __future__ import annotations

from typing import Any

from ._common import js_execute_retry
from ._relocate import relocate_fill_text
from ._scripts import fill_multi_text_script, fill_text_script


@js_execute_retry()
def js_fill_text(
    driver: Any,
    q: int,
    text: str,
    *,
    title: str | None = None,
    fallback_locate: bool = False,
) -> bool:
    """通过 JS 注入 + 人类行为事件链，将 ``text`` 填入 Q``q`` 的文本框/文本域。

    JS 实现集中在 `interactions._scripts.fill_text_script`（第六章模块化）。

    v4.4：主定位（6 个 id/name 候选）全部落空且 ``fallback_locate`` 开启时，
    走降级定位重试一次。多空填空（``js_fill_multi_text``）不接降级 —— 各框
    的提交槽名是探测记下的，槽名丢了说明探测层已失配，不是交互层能补的。
    """
    ok = driver.execute_script(fill_text_script(q, text))
    if ok is False and fallback_locate:
        ok = relocate_fill_text(driver, q, text, title)
    return ok


@js_execute_retry()
def js_fill_multi_text(driver: Any, q: int, texts: dict) -> bool:
    """多空填空（平台 type=9）：把每格文本写进对应的框（``texts``: {槽名: 文本}）。

    **全部写上才返回 true** —— 平台逐格校验，漏一格整题被弹回来，所以返回值
    不许把"写了一半"报成成功。JS 实现见 ``_scripts.fill_multi_text_script``。
    """
    return driver.execute_script(fill_multi_text_script(q, texts))
