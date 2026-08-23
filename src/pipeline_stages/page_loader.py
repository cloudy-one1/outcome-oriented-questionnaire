"""页面加载/iframe 切换/ready-state 探测阶段（第一章第 3 条拆分）。

导出：
    - _robust_driver_get     带重试的 driver.get
    - _wait_for_ready_state  等待 readyState === complete（或超时继续）
    - _ensure_questions_context  iframe 切换到含题目的 frame
"""

from __future__ import annotations

from typing import Any

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.support.ui import WebDriverWait

from ..config import PAGE_LOAD_INITIAL_DELAY, PAGE_LOAD_MAX_ATTEMPTS
from ..exceptions import TRANSIENT_DOM_EXCEPTIONS, raise_non_recoverable
from ..utils import retry_with_backoff


# ============================================================================
#  题目控件 selector（多个函数复用，集中一处可全局调整）
# ============================================================================
QUESTION_CONTROL_SELECTOR: str = (
    'input[type="radio"], input[type="checkbox"], select, textarea,'
    ' input[type="text"], input[type="tel"], input[type="number"]'
)


# ============================================================================
#  iframe 上下文切换（问卷星"题目嵌在 iframe"场景）
# ============================================================================
def _ensure_questions_context(driver: Any) -> bool:
    """确保当前 WebDriver 上下文指向包含题目的 frame。"""
    selector = QUESTION_CONTROL_SELECTOR
    has = driver.execute_script(
        f"return document.querySelectorAll('{selector}').length"
    )
    if has:
        return True

    iframes = driver.execute_script("return document.querySelectorAll('iframe').length")
    for i in range(iframes):
        driver.switch_to.frame(i)
        if driver.execute_script(
            f"return document.querySelectorAll('{selector}').length"
        ):
            return True
        driver.switch_to.default_content()

    driver.switch_to.default_content()
    return False


# ============================================================================
#  等待 readyState === complete（超时继续）
# ============================================================================
def _wait_for_ready_state(driver: Any, page_timeout: float = 25.0) -> None:
    """等待 document.readyState == 'complete'（失败不抛异常，继续流程）。"""
    try:
        WebDriverWait(driver, page_timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TRANSIENT_DOM_EXCEPTIONS:
        # 页面 ready 信号属于"最努力探测"，超时/瞬态 DOM 失败
        # 不影响后续答题（问卷星很多站点 readyState 永远是 interactive）
        pass
    except Exception as _e:
        # Ctrl+C/SystemExit 必须上抛；其他仍当作探测失败忽略
        raise_non_recoverable(_e)
        pass


# ============================================================================
#  页面打开 + 指数退避重试包装
# ============================================================================
@retry_with_backoff(
    max_attempts=PAGE_LOAD_MAX_ATTEMPTS,
    initial_delay=PAGE_LOAD_INITIAL_DELAY,
    backoff_factor=2.0,
    jitter=True,
    retry_on=(WebDriverException, TimeoutError),
)
def _robust_driver_get(driver: Any, survey_url: str) -> None:
    """稳定版 driver.get()：遇到浏览器/网络异常自动重试。"""
    driver.get(survey_url)


__all__ = [
    "QUESTION_CONTROL_SELECTOR",
    "_ensure_questions_context",
    "_wait_for_ready_state",
    "_robust_driver_get",
]
