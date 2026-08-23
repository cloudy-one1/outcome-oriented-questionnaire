"""提交确认模块（第一章第 2 条拆分 + 第六章 JS 模块化）。

包含：
    - SubmitOutcome (三态类型别名)
    - SUBMIT_SUCCESS / SUBMIT_FAILED / SUBMIT_UNKNOWN 常量
    - find_and_click_submit
    - _wait_until_submit_effect
"""

from __future__ import annotations

import time
from typing import Any, Literal

from ._common import (
    TRANSIENT_DOM_EXCEPTIONS,
    By,
    gaussian_seconds,
    js_execute_retry,
    raise_non_recoverable,
)
from ._scripts import submit_button_fallback_script, submit_success_detect_script


# ============================================================================
#  提交结果三态：成功 / 失败 / 未知（超时）
# ============================================================================
SubmitOutcome = Literal["success", "failed", "unknown"]
SUBMIT_SUCCESS: SubmitOutcome = "success"
SUBMIT_FAILED: SubmitOutcome = "failed"
SUBMIT_UNKNOWN: SubmitOutcome = "unknown"


# ============================================================================
#  提交按钮查找 + 提交后 URL 变化快进
# ============================================================================

SELECTORS = [
    "#divSubmit", "#submit_button", "#ctlNext",
    "button[type='submit']", "input[type='submit']",
    ".submitbtn", "#submitBtn", "#submitDiv", ".btn-submit",
    ".submitbtn.clickable", "#ctl00_ContentPlaceHolder1_ctlSubmit",
]


@js_execute_retry(max_attempts=3, initial_delay=0.15)
def find_and_click_submit(driver: Any, *, wait_url_change_timeout: float = 6.0) -> SubmitOutcome:
    """查找并点击问卷"提交"按钮；点击后等待 URL 变化。

    - (a) 依次尝试 SELECTORS 中的 CSS 选择器（Selenium 原生定位 + 点击）
    - (b) 全部失败 → JS 兜底（submit_button_fallback_script，第六章模块化）

    三态返回：success / failed / unknown
    """
    # (a) 先尝试 Selenium 原生定位
    for sel in SELECTORS:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior:'instant',block:'center'});",
                btn,
            )
            time.sleep(gaussian_seconds(0.18, 0.04, 0.08, 0.4))
            btn.click()
            return _wait_until_submit_effect(driver, wait_url_change_timeout)
        except TRANSIENT_DOM_EXCEPTIONS:
            # DOM 失败换选择器重试是合理动作；数据契约/编程错误不吞
            continue
        except Exception as _e:
            # Ctrl+C/SystemExit 必须上抛
            raise_non_recoverable(_e)
            continue

    # (b) Selenium 定位全部失败 → JS 兜底（详见 _scripts.submit_button_fallback_script）
    ok = driver.execute_script(submit_button_fallback_script())
    if not ok:
        return SUBMIT_FAILED
    return _wait_until_submit_effect(driver, wait_url_change_timeout)


# ============================================================================
#  提交效果探测：轮询 URL 变化 / 成功文本
# ============================================================================

def _wait_until_submit_effect(driver: Any, timeout: float) -> SubmitOutcome:
    """点击提交按钮后，检测效果（URL 变化 / 成功提示）。

    返回值三态：
        "success" : URL 变化，或页面出现「提交成功 / 感谢您的参与」等关键词
        "unknown" : 等待 timeout 内未观察到任何效果
    """
    old_url = driver.current_url
    start = time.perf_counter()
    success_check_js = submit_success_detect_script()
    while time.perf_counter() - start < timeout:
        try:
            cur = driver.current_url
            if cur != old_url:
                return SUBMIT_SUCCESS
            if driver.execute_script(success_check_js):
                return SUBMIT_SUCCESS
        except TRANSIENT_DOM_EXCEPTIONS:
            pass
        except Exception as _e:
            raise_non_recoverable(_e)
            pass
        time.sleep(0.15)
    return SUBMIT_UNKNOWN
