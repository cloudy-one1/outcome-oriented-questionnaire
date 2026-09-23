"""单选 / 多选三个 JS 包装函数的离线契约 —— 关掉 README 缺口表里 58.8% 那条。

此前"该调哪个填充器"有契约（``tests/test_question_stage_dispatch.py``），但填充器**自己**
发出去的是哪段 JS、平台回的东西怎么变成 ``is_ok``，离线一次都没执行过函数体 —— 而
``question_stage`` 正是拿这个返回值决定"批量点不成要不要退化成逐项点"的。
替身按脚本文本分派：发错脚本（比如单选题发成批量清空式）当场 AssertionError。
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selenium.common.exceptions import StaleElementReferenceException  # noqa: E402

from src.interactions._scripts import (  # noqa: E402
    click_option_script,
    click_question_options_script,
    fill_option_blank_script,
)
from src.interactions.choices import (  # noqa: E402
    js_click_option,
    js_click_question_options,
    js_fill_option_blank,
)

Q = 4
_CHOICES_RE = re.compile(r"var choices = (\[[^\]]*\]);")


class FakeChoiceDom:
    """只认三段生成脚本，其余一律炸 —— 站点与参数都在断言范围内。"""

    def __init__(self, reply: Any = True) -> None:
        self.reply = reply
        self.scripts: list[str] = []

    def execute_script(self, script: str, *_a: Any, **_k: Any) -> Any:
        self.scripts.append(script)
        if script in {
            click_option_script(Q, 2),
            click_option_script(Q, 1),
            click_option_script(Q, 3),
            click_question_options_script(Q, [1, 3]),
            click_question_options_script(Q, [2]),
            fill_option_blank_script(Q, 2, "其他"),
        }:
            return self.reply
        raise AssertionError(f"选项作答发出了预期之外的 JS：{script[:90]}")


def sent_choices(script: str) -> list[int]:
    match = _CHOICES_RE.search(script)
    assert match, "这段脚本里没有 choices 数组"
    return json.loads(match.group(1))


@pytest.mark.parametrize(
    "call,expected",
    [
        (lambda d: js_click_option(d, Q, 2), lambda: click_option_script(Q, 2)),
        (lambda d: js_fill_option_blank(d, Q, 2, "其他"), lambda: fill_option_blank_script(Q, 2, "其他")),
        (lambda d: js_click_question_options(d, Q, "multi", [1, 3]), lambda: click_question_options_script(Q, [1, 3])),
    ],
    ids=["single-click", "option-blank", "batch"],
)
def test_each_wrapper_sends_exactly_its_generated_script(
    call: Any, expected: Any
) -> None:
    fake = FakeChoiceDom(reply=True)
    assert call(fake) is True
    assert fake.scripts == [expected()]


def test_the_platform_reply_is_what_becomes_is_ok() -> None:
    """``question_stage`` 拿返回值决定"要不要退化成逐项点"，所以 False 必须是 False。"""
    fake = FakeChoiceDom(reply=False)
    assert js_click_option(fake, Q, 2) is False
    assert js_click_question_options(fake, Q, "multi", [1, 3]) is False
    assert js_fill_option_blank(fake, Q, 2, "其他") is False


def test_a_single_choice_question_degrades_to_the_single_click_script() -> None:
    """单选退化点第一项：批量脚本会先把该题原有选中态清空，对 radio 是多余的一脚。"""
    fake = FakeChoiceDom()
    assert js_click_question_options(fake, Q, "single", [2, 3]) is True
    assert fake.scripts == [click_option_script(Q, 2)]


def test_no_choice_chosen_touches_the_page_at_all() -> None:
    fake = FakeChoiceDom()
    assert js_click_question_options(fake, Q, "multi", []) is False
    assert fake.scripts == []


def test_batch_script_carries_the_whole_choice_list_in_one_injection() -> None:
    """多选题必须**一次**注入点完：逐点中途崩掉会留下半选题，而平台按提交时的状态收。"""
    fake = FakeChoiceDom()
    assert js_click_question_options(fake, Q, "multi", [1, 3]) is True
    assert len(fake.scripts) == 1
    assert sent_choices(fake.scripts[0]) == [1, 3]


def test_transient_js_error_is_retried_then_succeeds() -> None:
    """``@js_execute_retry`` 确实挂在这三个函数上：DOM 刚被重排时不该直接判这题失败。"""
    attempts = {"n": 0}
    calls: list[str] = []

    class Flaky:
        def execute_script(self, script: str, *_a: Any, **_k: Any) -> Any:
            attempts["n"] += 1
            calls.append(script)
            if attempts["n"] == 1:
                raise StaleElementReferenceException("选项刚被平台重排")
            return True

    assert js_click_option(Flaky(), Q, 2) is True
    assert attempts["n"] == 2
    assert calls == [click_option_script(Q, 2)] * 2


def test_a_contract_error_is_not_swallowed_by_the_retry_layer() -> None:
    """TypeError 不在 ``_JS_RETRYABLE`` 里 —— 代码 bug 必须原样上抛，别重试三次再吞掉。"""

    class Boom:
        def execute_script(self, script: str, *_a: Any, **_k: Any) -> Any:
            raise TypeError("q 不是整数")

    with pytest.raises(TypeError):
        js_click_option(Boom(), Q, 2)  # type: ignore[arg-type]
