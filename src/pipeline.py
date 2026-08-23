"""核心业务编排模块（V2.3 第一章第 3 条整改后：只剩「编排」职责，实现下沉到 pipeline_stages/*）。

本章第 3 条建议：pipeline.py 职责收窄为"协调各阶段顺序"，不承担具体实现。
拆分后的 4 类实现：
    1. 页面加载 / ready-state / iframe  →  ``src/pipeline_stages/page_loader.py``
    2. 题目识别 / 等待 / 单题答题分发      →  ``src/pipeline_stages/question_stage.py``
    3. 验证码检查 + 人工介入等待           →  ``src/pipeline_stages/verification_stage.py``
    4. 提交按钮查找 + 提交效果确认         →  ``src/interactions/submit.py``（第一章第 2 条拆分）

本文件剩余的 3 个公开函数就是：
    - ``run_one_submission(driver, url, ...)``        → 最外层（带 retry + 异常清理）
    - ``_do_one_submission_core(driver, url, lock)`` → 单次核心流程（Step 1~8 编排）
"""

from __future__ import annotations

import time
from typing import Any, Optional

from selenium.common.exceptions import WebDriverException

from .config import (
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
    VERIFY_EVERY_N_QUESTIONS,
)
# V2.3 第 1 章第 3 条：下沉各阶段实现到 pipeline_stages 子包
from .pipeline_stages import (
    _answer_one_question,
    _check_verification_with_lock,
    _ensure_questions_context,
    _robust_driver_get,
    _wait_for_questions,
    _wait_for_ready_state,
)
# 题目探测（断点续填/题目结构识别）来自 detection 模块，不属于 pipeline 职责
from .detection import detect_answered_questions, detect_questions
# 提交三态 + 查找提交按钮来自 interactions.submit（第一章第 2 条已拆分）
from .interactions.submit import (
    SUBMIT_FAILED,
    SUBMIT_SUCCESS,
    SUBMIT_UNKNOWN,
    SubmitOutcome,
    find_and_click_submit,
)
from .utils import (
    ManualHoldLock,
    human_pause,
    retry_with_backoff,
)
# V2.3 第五章：异常分层（只在编排层「兜底 + 重抛」时使用）
from .exceptions import (
    TRANSIENT_DOM_EXCEPTIONS,
    format_exc_log,
    raise_non_recoverable,
)

# history 模块为可选（纯 import 期不强依赖；真正 record 时检查参数是否传入）
try:
    from .history import SubmissionHistory  # type: ignore
    _HAS_HISTORY: bool = True
except TRANSIENT_DOM_EXCEPTIONS:
    # 理论上不会——history 是纯 Python 无 Selenium 依赖
    SubmissionHistory = None  # type: ignore
    _HAS_HISTORY = False
except Exception as _e:  # pragma: no cover
    raise_non_recoverable(_e)
    SubmissionHistory = None  # type: ignore
    _HAS_HISTORY = False


# ---------------------------------------------------------------------------
#  核心编排：单次提交真正实现（无外层重试）
# ---------------------------------------------------------------------------
def _do_one_submission_core(
    driver: Any,
    survey_url: str,
    lock: ManualHoldLock,
    *,
    history_db: Any | None = None,
    run_id: int | None = None,
    submission_index: int | None = None,
    no_record_text: bool = False,
) -> SubmitOutcome:
    """单次提交的真正实现（无外层重试，由调用者包 retry）。

    三态返回：
        "success" : 提交按钮已点击且页面出现成功信号
        "failed"  : 业务前置步骤失败（验证码超时 / 题目探测失败等）
        "unknown" : 提交按钮已点击但效果超时
    """
    # Step 1 打开页面 + 等 ready
    _robust_driver_get(driver, survey_url)
    _wait_for_ready_state(driver)
    # 比原来的 sleep(1.5) 更快：仅等 DOM 有基本内容
    try:
        from selenium.webdriver.support.ui import WebDriverWait as _WDWait
        _WDWait(driver, 3).until(
            lambda d: d.execute_script("return document.body != null")
        )
    except TRANSIENT_DOM_EXCEPTIONS:
        # body 存在性探测（短超时 3s），失败继续让后续 WebDriverWait 兜底
        pass
    except Exception as _e:
        raise_non_recoverable(_e)
        pass

    # Step 2 验证码检查（打开页面立刻弹的情况）
    if not _check_verification_with_lock(driver, lock):
        return SUBMIT_FAILED

    # Step 3 iframe 适配
    if not _ensure_questions_context(driver):
        driver.switch_to.default_content()
        return SUBMIT_FAILED

    # Step 4 等题目元素
    if not _wait_for_questions(driver, QUESTION_DETECT_TIMEOUT):
        driver.switch_to.default_content()
        return SUBMIT_FAILED

    # Step 5 探测题目结构
    questions = detect_questions(driver)
    if not questions:
        driver.switch_to.default_content()
        return SUBMIT_FAILED

    # Step 5.5 断点续填：扫描已填好的题号集合
    try:
        answered_set: set[int] = detect_answered_questions(driver)
    except TRANSIENT_DOM_EXCEPTIONS:
        answered_set = set()
    except Exception as _e:
        raise_non_recoverable(_e)
        answered_set = set()
    skipped_count = 0

    # Step 6 逐题作答
    for qi, q in enumerate(questions):
        if qi > 0 and qi % VERIFY_EVERY_N_QUESTIONS == 0:
            if not _check_verification_with_lock(driver, lock):
                driver.switch_to.default_content()
                return SUBMIT_FAILED

        q_num = int(q["q"])
        if q_num in answered_set:
            skipped_count += 1
            continue

        _answer_one_question(
            driver,
            q,
            history_db=history_db,
            run_id=run_id,
            submission_index=submission_index,
            no_record_text=no_record_text,
        )

        # 每题之间的「思考时间」+ 偶发长停顿
        human_pause(
            Q_THINK_MU, Q_THINK_SIGMA,
            Q_THINK_LO, Q_THINK_HI,
            long_pause_prob=Q_LONG_PAUSE_PROB,
            long_lo=Q_LONG_PAUSE_LO,
            long_hi=Q_LONG_PAUSE_HI,
        )

    if skipped_count > 0:
        print(f"  [续填] 跳过 {skipped_count} 道已填题，本次重答 {len(questions) - skipped_count} 道")

    # Step 7 全题答完后再检查一次验证码（提交前问卷星最爱弹）
    if not _check_verification_with_lock(driver, lock):
        driver.switch_to.default_content()
        return SUBMIT_FAILED

    # Step 8 点击提交 + 提交后快进
    submit_result = find_and_click_submit(driver)
    if submit_result == SUBMIT_FAILED:
        driver.switch_to.default_content()
        return SUBMIT_FAILED
    if submit_result == SUBMIT_UNKNOWN:
        print("  [提交] 状态未知：按钮已点击但未观察到成功信号（超时 / AJAX / 服务端拒绝）"
              "→ 保守计为失败")
        driver.switch_to.default_content()
        return SUBMIT_UNKNOWN

    # 提交后偶尔也会弹最终验证
    try:
        from .verification import is_smart_verification_showing, wait_for_manual_verification
        from .config import VERIFICATION_TIMEOUT
        if is_smart_verification_showing(driver):
            wait_for_manual_verification(
                driver,
                timeout_seconds=min(VERIFICATION_TIMEOUT, 60),
                hold_lock=lock,
            )
    except TRANSIENT_DOM_EXCEPTIONS:
        # 提交后验证码探测是二次检查，失败不影响最终成功判定
        pass
    except Exception as _e:
        raise_non_recoverable(_e)
        pass

    driver.switch_to.default_content()
    return SUBMIT_SUCCESS


# ---------------------------------------------------------------------------
#  最外层：带重试 + 清理的 run_one_submission 入口
# ---------------------------------------------------------------------------
@retry_with_backoff(
    max_attempts=SUBMISSION_MAX_ATTEMPTS,
    initial_delay=SUBMISSION_INITIAL_DELAY,
    backoff_factor=SUBMISSION_BACKOFF,
    jitter=True,
    retry_on=(WebDriverException,),
)
def run_one_submission(
    driver: Any,
    survey_url: str,
    lock: ManualHoldLock,
    *,
    history_db: Any | None = None,
    run_id: int | None = None,
    submission_index: int | None = None,
    no_record_text: bool = False,
) -> SubmitOutcome:
    """外层 retry + 清理 + 重抛异常（供 GUI / CLI 批处理循环调用）。

    - 指数退避重试（只在 WebDriverException 层做）
    - 异常时：打印日志 + 切回默认上下文 + 重抛给 retry_with_backoff 判定
    - 正常时：返回三态 SubmitOutcome
    """
    try:
        return _do_one_submission_core(
            driver,
            survey_url,
            lock,
            history_db=history_db,
            run_id=run_id,
            submission_index=submission_index,
            no_record_text=no_record_text,
        )
    except Exception as e:
        # Ctrl+C/SystemExit 直接上抛（不做任何清理尝试以免吞）
        raise_non_recoverable(e)
        print("  " + format_exc_log(
            e, action="单次提交核心流程", recovery="尝试回到默认上下文并重抛给外层重试",
            submission_index=submission_index,
        ))
        try:
            driver.switch_to.default_content()
        except TRANSIENT_DOM_EXCEPTIONS:
            # 清理：switch_to 失败属于正常（iframe 已销毁/会话已关闭）
            pass
        except Exception as _e2:
            raise_non_recoverable(_e2)
            pass
        raise  # 重抛异常，让 retry_with_backoff 判定是否重试
