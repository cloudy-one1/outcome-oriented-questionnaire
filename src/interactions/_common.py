"""`interactions` 子模块共享的通用装饰器 / 常量。

不建议直接 import 本模块（前面的 `_` 表示内部）；
题型模块通过 `from ._common import js_execute_retry, click_after_pause, _JS_RETRYABLE` 复用。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Literal, TypeVar

from selenium.webdriver.common.by import By

from ..config import CLICK_AFTER_HI, CLICK_AFTER_LO, CLICK_AFTER_MU, CLICK_AFTER_SIGMA
from ..exceptions import (
    TRANSIENT_DOM_EXCEPTIONS,
    format_exc_log,
    raise_non_recoverable,
)
from ..utils import gaussian_seconds, human_pause, retry_with_backoff


_Fn = TypeVar("_Fn", bound=Callable[..., Any])


# ============================================================================
#  JS execute 重试：窄子集（JS/DOM 级瞬态失败，不包含会话崩溃等大异常）
# ============================================================================
_JS_RETRYABLE: tuple[type[BaseException], ...] = tuple(
    e for e in TRANSIENT_DOM_EXCEPTIONS
    if e.__name__ in ("StaleElementReferenceException", "JavascriptException",
                      "WebDriverException")
)


# ============================================================================
#  装饰器：JS 执行偶发异常自动重试
# ============================================================================
def js_execute_retry(max_attempts: int = 3,
                     initial_delay: float = 0.2,
                     backoff_factor: float = 2.0):
    """把对 Selenium `execute_script` / DOM 操作的函数包一层指数退避重试。

    只在抛出 `_JS_RETRYABLE`（StaleElement / JavascriptException / WebDriverException）
    时重试——其他异常视为数据契约 bug，要向上暴露。
    """
    return retry_with_backoff(
        max_attempts=max_attempts,
        initial_delay=initial_delay,
        backoff_factor=backoff_factor,
        jitter=True,
        retry_on=_JS_RETRYABLE,
    )


# ============================================================================
#  点击后人类反应时间 sleep
# ============================================================================
def click_after_pause() -> float:
    """在每次点击后 sleep 一段"人类反应时间"，返回实际 sleep 秒数。"""
    return human_pause(
        CLICK_AFTER_MU, CLICK_AFTER_SIGMA, CLICK_AFTER_LO, CLICK_AFTER_HI
    )


# ============================================================================
#  常见 DOM 工具（submit 模块会用到 By，题型模块通常用纯 JS execute_script 所以不需要）
# ============================================================================
__all__ = [
    "By",
    "TRANSIENT_DOM_EXCEPTIONS",
    "format_exc_log",
    "raise_non_recoverable",
    "_JS_RETRYABLE",
    "js_execute_retry",
    "click_after_pause",
    "gaussian_seconds",
    "time",
]
