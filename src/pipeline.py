"""单次问卷填写 + 提交流程编排模块（增强版）。

相比 v1 的改进：
  反检测稳定性
  ├─ 全程共享同一个 ManualHoldLock：任何步骤检测到验证码都 hold 住
  ├─ 验证码检测频率从每 3 题 → 每 2 题（更敏感），且 URL/title/shadow 三信号并行
  ├─ 页面 driver.get() 失败使用指数退避重试（PAGE_LOAD_MAX_ATTEMPTS 次）
  └─ 整次提交 run_one_submission 使用指数退避重试

  速率提升
  ├─ 页面加载等待：readyState + 有题目元素立即返回，替换固定 sleep(1.5)
  ├─ 答题：整题一次性批量设置（js_click_question_options），多选题 N 选项 → 1 次往返
  ├─ 提交后：URL 变化 + 成功关键词立即返回，替换固定 sleep(2-3)
  └─ 所有思考/点击等待使用正态分布，均值 0.22s / 0.5s 比旧 uniform(0.15-0.35) / (0.3-0.8) 更快

  行为更"人"
  ├─ 每道题之间 3% 概率真的停 2~4.5 秒模拟思考
  ├─ 点击前 mouseover/mousemove/mouseenter/mousedown/click/mouseup 完整事件链
  └─ 每道题前先把题目区域滚动到屏幕中央
"""

from __future__ import annotations

import time
from typing import Any

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.support.ui import WebDriverWait

from .config import (
    PAGE_LOAD_INITIAL_DELAY,
    PAGE_LOAD_MAX_ATTEMPTS,
    Q_LONG_PAUSE_HI,
    Q_LONG_PAUSE_LO,
    Q_LONG_PAUSE_PROB,
    Q_THINK_HI,
    Q_THINK_LO,
    Q_THINK_MU,
    Q_THINK_SIGMA,
    QUESTION_DETECT_TIMEOUT,
    SUBMISSION_BACKOFF,
    SUBMISSION_INITIAL_DELAY,
    SUBMISSION_MAX_ATTEMPTS,
    VERIFICATION_TIMEOUT,
    VERIFY_EVERY_N_QUESTIONS,
)
from .utils import (
    ManualHoldLock,
    human_pause,
    retry_with_backoff,
)
from .answering import build_answer_strategy
from .detection import detect_questions
from .interaction import (
    find_and_click_submit,
    js_click_option,
    js_click_question_options,
)
from .verification import is_smart_verification_showing, wait_for_manual_verification


def _ensure_questions_context(driver: Any) -> bool:
    """确保当前 WebDriver 上下文指向包含题目的 frame。"""
    selector = 'input[type="radio"], input[type="checkbox"]'
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


def _wait_for_ready_state(driver: Any, page_timeout: float = 25.0) -> None:
    """等待 document.readyState == 'complete'（失败不抛异常，继续流程）。"""
    try:
        WebDriverWait(driver, page_timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except Exception:
        pass


def _wait_for_questions(driver: Any, timeout: float) -> bool:
    """等待题目输入框出现在 DOM 中。"""
    selector = 'input[type="radio"], input[type="checkbox"]'
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script(
                f"return document.querySelectorAll('{selector}').length > 0"
            )
        )
        return True
    except Exception:
        return False


def _check_verification_with_lock(driver: Any, lock: ManualHoldLock) -> bool:
    """检查是否弹出了验证，如果是 → 进入 hold 等待人工处理。

    返回 True ：验证已解决（或根本没弹）
    返回 False：验证超时
    """
    if not is_smart_verification_showing(driver):
        return True
    return wait_for_manual_verification(
        driver,
        timeout_seconds=VERIFICATION_TIMEOUT,
        hold_lock=lock,
    )


# ---------------------------------------------------------------------------
#  页面加载重试包装（指数退避）
# ---------------------------------------------------------------------------

@retry_with_backoff(
    max_attempts=PAGE_LOAD_MAX_ATTEMPTS,
    initial_delay=PAGE_LOAD_INITIAL_DELAY,
    backoff_factor=2.0,
    jitter=True,
    retry_on=(WebDriverException, TimeoutError),
)
def _robust_driver_get(driver: Any, survey_url: str) -> None:
    """稳定版 driver.get()：遇到 WebDriver/Timeout 自动重试。"""
    driver.get(survey_url)


# ---------------------------------------------------------------------------
#  核心流程
# ---------------------------------------------------------------------------

def _do_one_submission_core(driver: Any, survey_url: str, lock: ManualHoldLock) -> bool:
    """单次提交的真正实现（无外层重试，由调用者包 retry）。"""
    # Step 1 打开页面 + 等 ready
    _robust_driver_get(driver, survey_url)
    _wait_for_ready_state(driver)
    # 比原来的 sleep(1.5) 更快：仅等 DOM 有基本内容
    try:
        WebDriverWait(driver, 3).until(
            lambda d: d.execute_script("return document.body != null")
        )
    except Exception:
        pass

    # Step 2 验证码检查（打开页面立刻弹的情况）
    if not _check_verification_with_lock(driver, lock):
        return False

    # Step 3 iframe 适配
    if not _ensure_questions_context(driver):
        driver.switch_to.default_content()
        return False

    # Step 4 等题目元素
    if not _wait_for_questions(driver, QUESTION_DETECT_TIMEOUT):
        driver.switch_to.default_content()
        return False

    # Step 5 探测题目结构
    questions = detect_questions(driver)
    if not questions:
        driver.switch_to.default_content()
        return False

    # Step 6 逐题作答（批量设置 + 正态思考时间）
    for qi, q in enumerate(questions):
        # 每 N 题检查一次验证码（每 2 题 → 更敏感）
        if qi > 0 and qi % VERIFY_EVERY_N_QUESTIONS == 0:
            if not _check_verification_with_lock(driver, lock):
                driver.switch_to.default_content()
                return False

        answer = build_answer_strategy(q)

        try:
            ok = js_click_question_options(driver, q["q"], q["type"], answer)
        except Exception:
            # 批量函数抛异常时降级：逐个点击
            ok = True
            for c in answer:
                if not js_click_option(driver, q["q"], c):
                    ok = False
                    break
                human_pause(
                    Q_THINK_MU * 0.3, Q_THINK_SIGMA * 0.3,
                    0.04, 0.15,
                )

        # 每题之间的「思考时间」+ 偶发长停顿
        human_pause(
            Q_THINK_MU, Q_THINK_SIGMA,
            Q_THINK_LO, Q_THINK_HI,
            long_pause_prob=Q_LONG_PAUSE_PROB,
            long_lo=Q_LONG_PAUSE_LO,
            long_hi=Q_LONG_PAUSE_HI,
        )

    # Step 7 全题答完后再检查一次验证码（提交前问卷星最爱弹）
    if not _check_verification_with_lock(driver, lock):
        driver.switch_to.default_content()
        return False

    # Step 8 点击提交 + 提交后快进
    ok = find_and_click_submit(driver)
    if not ok:
        driver.switch_to.default_content()
        return False

    # 提交后偶尔也会弹最终验证（问卷星"提交时先做验证"逻辑）
    # 再检查一次，但只等较短时间（因为流程已接近结束）
    try:
        if is_smart_verification_showing(driver):
            wait_for_manual_verification(
                driver,
                timeout_seconds=min(VERIFICATION_TIMEOUT, 60),
                hold_lock=lock,
            )
    except Exception:
        pass

    driver.switch_to.default_content()
    return True


@retry_with_backoff(
    max_attempts=SUBMISSION_MAX_ATTEMPTS,
    initial_delay=SUBMISSION_INITIAL_DELAY,
    backoff_factor=SUBMISSION_BACKOFF,
    jitter=True,
    retry_on=(WebDriverException,),
)
def run_one_submission(driver: Any, survey_url: str) -> bool:
    """执行一次完整的问卷填写 + 提交流程（外层包 WebDriver 异常重试）。

    重试策略：
      - 过程中抛出 WebDriverException（如 StaleElementReference / 浏览器断开）→
        等待 SUBMISSION_INITIAL_DELAY * (2 ** attempt) 秒后重试
      - 普通业务失败（返回 False）→ 不重试，交给调用方统计
      - 所有重试用完 → 抛最后一次异常（外层捕获后记为失败）

    参数：
      driver     : Selenium WebDriver 实例
      survey_url : 问卷星问卷 URL

    返回：
      True  本次提交成功
      False 本次提交失败
    """
    # 每轮提交流程共享一个人工介入锁，确保验证期间任何子步骤都被 hold
    lock = ManualHoldLock()
    try:
        return _do_one_submission_core(driver, survey_url, lock)
    except Exception as e:
        print(f"  EX: {type(e).__name__}: {e}")
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        raise  # 重抛异常，让 retry_with_backoff 判定是否重试
