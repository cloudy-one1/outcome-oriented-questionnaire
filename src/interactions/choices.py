"""单选 / 多选题型 DOM 交互实现（V2.3 第一章第 2 条拆分 + 第六章 JS 模块化）。

v4.4：js_click_question_options / js_fill_option_blank 增加降级定位重试
（``fallback_locate`` 开关 + 题干/属性锚点，见 ``_relocate.py``）——
主选择器失配（平台改版）时的第二次尝试，默认关。
"""

from __future__ import annotations

from typing import Any

from ._common import js_execute_retry
from ._relocate import relocate_click_options, relocate_fill_option_blank
from ._scripts import (
    click_option_script,
    click_question_options_script,
    fill_option_blank_script,
)


@js_execute_retry()
def js_click_option(driver: Any, q: int, choice: int) -> bool:
    """通过 JS 注入点击选中某道题的某个选项（人类行为版）。

    JS 实现集中在 `interactions._scripts.click_option_script`（第六章模块化）。
    """
    return driver.execute_script(click_option_script(q, choice))


@js_execute_retry()
def js_fill_option_blank(
    driver: Any,
    q: int,
    choice: int,
    text: str,
    *,
    all_choices: list | None = None,
    title: str | None = None,
    fallback_locate: bool = False,
) -> bool:
    """把 ``text`` 写进 Q{q} 第 ``choice`` 项**自带**的填空框（"其他____"）。

    只在 ``detection`` 报出 ``blank_options`` 且那一项确实被选中时才被调用；
    框的定位与探测侧共用同一个 JS 助手（见 ``fill_option_blank_script``）。

    v4.4：主定位失败且 ``fallback_locate`` 开启时，走降级定位重试一次。
    ``all_choices``（探测的完整选项值列表）用于序号映射；缺席时降级层
    的数量校验会拦下来（宁失败不猜点）。
    """
    ok = driver.execute_script(fill_option_blank_script(q, choice, text))
    if ok is False and fallback_locate:
        ok = relocate_fill_option_blank(
            driver, q, choice, list(all_choices or [choice]), text, title
        )
    return ok


@js_execute_retry()
def js_click_question_options(
    driver: Any,
    q: int,
    question_type: str,
    choices: list[int],
    *,
    all_choices: list | None = None,
    title: str | None = None,
    fallback_locate: bool = False,
) -> bool:
    """**一次性**设置单道题的所有被选中选项（返回布尔表示整题是否成功）。

    - 单选题：退化为 ``click_option_script(q, choices[0])``
    - 多选题：批量 JS（详见 _scripts.click_question_options_script）

    v4.4：主定位失败且 ``fallback_locate`` 开启时，走降级定位重试一次
    （``_relocate.relocate_click_options``）。``all_choices``（探测的完整选项
    值列表，``q["choices"]``）用于 value→DOM 序的映射；缺席时降级层的数量
    校验会拦下来（宁失败不猜点）。
    """
    if not choices:
        return False
    if question_type == "single":
        ok = driver.execute_script(click_option_script(q, choices[0]))
    else:
        ok = driver.execute_script(click_question_options_script(q, choices))
    if ok is False and fallback_locate:
        ok = relocate_click_options(
            driver,
            q,
            choices,
            list(all_choices or choices),
            title,
            is_multi=(question_type != "single"),
        )
    return ok
