"""提交确认模块（第一章第 2 条拆分 + 第六章 JS 模块化）。

包含：
    - SubmitOutcome (三态类型别名)
    - SUBMIT_SUCCESS / SUBMIT_FAILED / SUBMIT_UNKNOWN 常量
    - find_and_click_submit
    - _wait_until_submit_effect
"""

from __future__ import annotations

import time
from typing import Any

from ._common import (
    TRANSIENT_DOM_EXCEPTIONS,
    By,
    gaussian_seconds,
    js_execute_retry,
    raise_non_recoverable,
)
from ..models import SUBMIT_FAILED, SUBMIT_SUCCESS, SUBMIT_UNKNOWN, SubmitOutcome
from ._scripts import (
    SUBMIT_SELECTORS,
    submit_button_fallback_script,
    submit_success_detect_script,
)


# ============================================================================
#  提交结果三态：成功 / 失败 / 未知（超时）
# ============================================================================
# 定义已上移到 src.models（纯数据层不应反向依赖 selenium 栈），这里再导出以保持
# ``from .interactions.submit import SUBMIT_SUCCESS`` 的既有调用点不变。
__all__ = [
    "SubmitOutcome",
    "SUBMIT_SUCCESS",
    "SUBMIT_FAILED",
    "SUBMIT_UNKNOWN",
    "SELECTORS",
    "find_and_click_submit",
    "_click_submit_button",
    "_wait_until_submit_effect",
]


# ============================================================================
#  提交按钮查找 + 提交后 URL 变化快进
# ============================================================================

# V2.4 整改：选择器单一真相在 _scripts.SUBMIT_SELECTORS（JS 兜底脚本同源派生），
# 这里拷贝一份供 Python 侧 for 循环使用（拷贝避免调用方意外改动影响 JS 生成）。
SELECTORS: list[str] = list(SUBMIT_SELECTORS)


@js_execute_retry(max_attempts=3, initial_delay=0.15)
def _click_submit_button(driver: Any) -> bool:
    """定位并点击提交按钮；返回是否已点击。

    本函数**只负责点击**，可安全重试；点击后的效果确认必须留在外面。
    此前 ``find_and_click_submit`` 整体被 ``js_execute_retry`` 包住，
    ``btn.click()`` 之后的等待一旦抛 ``WebDriverException``（例如提交导致页面
    跳转使 ``driver.current_url`` 短暂不可用）就会**重新点一次提交按钮**，
    造成同一份问卷被重复提交。
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
            return True
        except TRANSIENT_DOM_EXCEPTIONS:
            # DOM 失败换选择器重试是合理动作；数据契约/编程错误不吞
            continue
        except Exception as _e:
            # Ctrl+C/SystemExit 必须上抛
            raise_non_recoverable(_e)
            continue

    # (b) Selenium 定位全部失败 → JS 兜底（详见 _scripts.submit_button_fallback_script）
    return bool(driver.execute_script(submit_button_fallback_script()))


def find_and_click_submit(driver: Any, *, wait_url_change_timeout: float = 6.0) -> SubmitOutcome:
    """查找并点击问卷"提交"按钮；点击后等待 URL 变化。

    - (a) 依次尝试 SELECTORS 中的 CSS 选择器（Selenium 原生定位 + 点击）
    - (b) 全部失败 → JS 兜底（submit_button_fallback_script，第六章模块化）

    三态返回：success / failed / unknown
    """
    if not _click_submit_button(driver):
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
    try:
        old_url: str | None = driver.current_url
    except TRANSIENT_DOM_EXCEPTIONS:
        # 点击瞬间页面正在跳转，基线 URL 拿不到。不能用 ""/固定值兜底：
        # 那样下一轮 "cur != old_url" 恒真，会把任何跳转（含错误页）误判为提交成功。
        # 这里置 None，等首次能读到 URL 时再补建基线，期间只靠成功文案判定。
        old_url = None
    start = time.perf_counter()
    success_check_js = submit_success_detect_script()
    while time.perf_counter() - start < timeout:
        try:
            cur = driver.current_url
            if old_url is None:
                old_url = cur          # 补建基线，后续轮次才能做真实的 URL 变化对比
                continue
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
