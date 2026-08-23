"""单选 / 多选题型 DOM 交互实现（V2.3 第一章第 2 条拆分 + 第六章 JS 模块化）。"""

from __future__ import annotations

from typing import Any

from ._common import js_execute_retry
from ._scripts import click_option_script, click_question_options_script


@js_execute_retry()
def js_click_option(driver: Any, q: int, choice: int) -> bool:
    """通过 JS 注入点击选中某道题的某个选项（人类行为版）。

    JS 实现集中在 `interactions._scripts.click_option_script`（第六章模块化）。
    """
    return driver.execute_script(click_option_script(q, choice))


@js_execute_retry()
def js_click_question_options(driver: Any, q: int, question_type: str,
                              choices: list[int]) -> bool:
    """**一次性**设置单道题的所有被选中选项（返回布尔表示整题是否成功）。

    - 单选题：退化为 ``js_click_option(driver, q, choices[0])``
    - 多选题：批量 JS（详见 _scripts.click_question_options_script）
    """
    if not choices:
        return False
    if question_type == "single":
        return js_click_option(driver, q, choices[0])
    return driver.execute_script(click_question_options_script(q, choices))
