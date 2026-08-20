"""单次问卷填写 + 提交流程编排模块（v2.0 全题型增强版）。

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

    V2 新题型（answering_v2 + 新 interaction 函数）
    ├─ text / textarea          → js_fill_text（填空，内置中文数据池：姓名/手机/邮箱/地址）
    ├─ scale / rating           → js_set_scale（1..N 分星评/量表）
    ├─ dropdown                 → js_select_dropdown（<select> 下拉选择）
    └─ matrix_single            → js_fill_matrix_single（每行一题的矩阵单选）

    V2 历史记录（SubmissionHistory SQLite 持久化）
    └─ 可接收 history_db + run_id + submission_index，逐题 record_answer 明细，
       全 V1 / V2 题型统一落盘。
"""

from __future__ import annotations

import time
from typing import Any, Optional

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

# V1 兼容：answering.build_answer_strategy 保留
from .answering import build_answer_strategy
# V2 新增：answering_v2.generate_answer（统一 dict 格式 + 新题型）
from .answering_v2 import generate_answer as generate_answer_v2

from .detection import detect_questions
from .interaction import (
    find_and_click_submit,
    js_click_option,
    js_click_question_options,
    js_fill_matrix_single,
    js_fill_text,
    js_select_dropdown,
    js_set_scale,
)
from .verification import is_smart_verification_showing, wait_for_manual_verification

# history 模块为可选（纯 import 期不强依赖；真正 record 时检查参数是否传入）
try:
    from .history import SubmissionHistory  # type: ignore
    _HAS_HISTORY: bool = True
except Exception:  # pragma: no cover
    SubmissionHistory = None  # type: ignore
    _HAS_HISTORY = False


def _ensure_questions_context(driver: Any) -> bool:
    """确保当前 WebDriver 上下文指向包含题目的 frame。"""
    selector = 'input[type="radio"], input[type="checkbox"], select, textarea, input[type="text"]'
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
    """等待题目输入框出现在 DOM 中（V2 扩展：覆盖 6 类题型的常见输入控件）。"""
    selector = (
        'input[type="radio"], input[type="checkbox"],'
        ' select, textarea, input[type="text"], input[type="tel"], input[type="number"]'
    )
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
#  V2 统一答题分发器：根据 q.type 调用 answering_v2 + 对应 interaction
# ---------------------------------------------------------------------------

def _answer_one_question(
    driver: Any,
    q: dict,
    history_db: Any | None = None,
    run_id: int | None = None,
    submission_index: int | None = None,
) -> bool:
    """为单道题生成答案并写入 DOM，可选地落盘 history.answers。

    - V1 题型 (single/multi)：沿用 ``build_answer_strategy`` +
      ``js_click_question_options``，保证 100% 行为不变。
    - V2 题型 (text/scale/dropdown/matrix_single)：使用
      ``answering_v2.generate_answer``（统一 dict）+ 对应 interaction 新函数。

    :return: 是否答题成功（不影响外层统计 —— 失败通常只是识别不到 DOM，
             整次提交会在提交后统一判断）。
    """
    qnum = int(q["q"])
    qtype = str(q.get("type", "single")).lower()

    # 用于 history 记录（options_selected / text_answer / elapsed_ms）
    options_selected: list[int] | None = None
    text_answer: str | None = None
    t0 = time.perf_counter()
    ok = False

    # ------------------------------------------------------------------
    #  V1 题型：单选 / 多选（完全保留原逻辑，不做任何破坏性改动）
    # ------------------------------------------------------------------
    if qtype in ("single", "multi"):
        answer_values = build_answer_strategy(q)  # list[int]
        try:
            ok = js_click_question_options(driver, qnum, qtype, answer_values)
        except Exception:
            ok = True
            for c in answer_values:
                if not js_click_option(driver, qnum, c):
                    ok = False
                    break
                human_pause(
                    Q_THINK_MU * 0.3, Q_THINK_SIGMA * 0.3,
                    0.04, 0.15,
                )
        # history 记录：选项值列表
        options_selected = list(answer_values) if answer_values else None

    # ------------------------------------------------------------------
    #  V2 题型：text / scale / dropdown / matrix_single
    # ------------------------------------------------------------------
    else:
        ans = generate_answer_v2(q)  # dict 结构
        ans_type = str(ans.get("type", qtype)).lower()

        try:
            if ans_type == "text":
                text_answer = str(ans.get("text", ""))
                ok = js_fill_text(driver, qnum, text_answer)

            elif ans_type == "scale":
                val = int(ans.get("value", 3))
                smax = q.get("scale")
                ok = js_set_scale(driver, qnum, val, scale_max=smax)
                options_selected = [val]

            elif ans_type == "dropdown":
                sel_list = ans.get("selected") or []
                if sel_list:
                    ok = js_select_dropdown(driver, qnum, sel_list[0])
                    options_selected = [sel_list[0]] if isinstance(sel_list[0], int) else None
                    # 文本型下拉值 → 写 text_answer 备查
                    if options_selected is None and sel_list:
                        text_answer = str(sel_list[0])

            elif ans_type in ("matrix_single", "matrix"):
                row_map = ans.get("rows") or {}  # {row_idx: col_idx/val}
                ok = js_fill_matrix_single(driver, qnum, row_map)
                # matrix 的 answers 表：把 {row: col} 作为 JSON 写到 options_selected？
                # 设计：把所有被选列值收集成一个 list，便于统计
                if isinstance(row_map, dict):
                    options_selected = [
                        v if isinstance(v, int) else int(v)
                        for v in row_map.values()
                        if isinstance(v, int) or (isinstance(v, str) and v.isdigit())
                    ]

            else:
                # 兜底：如果有 choices，降级成单选（与 answering_v2 的兜底一致）
                if q.get("choices"):
                    from .answering import build_answer_strategy as _ba
                    answer_values = _ba(q)
                    ok = js_click_question_options(driver, qnum, "single", answer_values)
                    options_selected = list(answer_values)
                else:
                    ok = False
        except Exception as _e:
            # V2 交互偶发异常不影响整次提交流程（只记失败，不中断）
            print(f"  [Q{qnum} {qtype}] 交互异常: {type(_e).__name__}: {_e}")
            ok = False

    # ------------------------------------------------------------------
    #  V2 可选：逐题答案明细落盘（history DB）
    # ------------------------------------------------------------------
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if (
        history_db is not None
        and run_id is not None
        and submission_index is not None
    ):
        try:
            # 统一类型名（与 history.answers 表约束对齐）
            norm_type = {
                "single": "single",
                "radio": "single",
                "multi": "multi",
                "checkbox": "multi",
                "scale": "scale",
                "rating": "scale",
                "dropdown": "dropdown",
                "text": "text",
                "input": "text",
                "textarea": "text",
                "fillblank": "text",
                "matrix": "matrix",
                "matrix_single": "matrix",
            }.get(qtype, qtype)

            history_db.record_answer(
                run_id=run_id,
                submission_index=submission_index,
                question_number=qnum,
                question_type=norm_type,
                options_selected=options_selected,
                text_answer=text_answer,
                elapsed_ms=elapsed_ms,
            )
        except Exception as _he:
            # history 写失败只打印提示，不影响主流程
            print(f"  [history] record_answer(Q{qnum}) 失败: {type(_he).__name__}")

    return bool(ok)


# ---------------------------------------------------------------------------
#  核心流程
# ---------------------------------------------------------------------------

def _do_one_submission_core(
    driver: Any,
    survey_url: str,
    lock: ManualHoldLock,
    *,
    history_db: Any | None = None,
    run_id: int | None = None,
    submission_index: int | None = None,
) -> bool:
    """单次提交的真正实现（无外层重试，由调用者包 retry）。

    V2 新增参数（全部可选，向后兼容）：
        history_db       : SubmissionHistory 实例（未传则不记录）
        run_id           : 本次批量运行在 runs 表中的 id
        submission_index : 当前是第几份提交（1-based）
    """
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

    # Step 4 等题目元素（V2 扩展到 6 类控件）
    if not _wait_for_questions(driver, QUESTION_DETECT_TIMEOUT):
        driver.switch_to.default_content()
        return False

    # Step 5 探测题目结构（V2 返回包含 text/scale/dropdown/matrix）
    questions = detect_questions(driver)
    if not questions:
        driver.switch_to.default_content()
        return False

    # Step 6 逐题作答（V2 统一分发 + 可选 history 落盘）
    for qi, q in enumerate(questions):
        # 每 N 题检查一次验证码（每 2 题 → 更敏感）
        if qi > 0 and qi % VERIFY_EVERY_N_QUESTIONS == 0:
            if not _check_verification_with_lock(driver, lock):
                driver.switch_to.default_content()
                return False

        _answer_one_question(
            driver,
            q,
            history_db=history_db,
            run_id=run_id,
            submission_index=submission_index,
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
def run_one_submission(
    driver: Any,
    survey_url: str,
    *,
    history_db: Any | None = None,
    run_id: int | None = None,
    submission_index: int | None = None,
) -> bool:
    """执行一次完整的问卷填写 + 提交流程（外层包 WebDriver 异常重试）。

    重试策略：
      - 过程中抛出 WebDriverException（如 StaleElementReference / 浏览器断开）→
        等待 SUBMISSION_INITIAL_DELAY * (2 ** attempt) 秒后重试
      - 普通业务失败（返回 False）→ 不重试，交给调用方统计
      - 所有重试用完 → 抛最后一次异常（外层捕获后记为失败）

    V2 新增可选关键字参数：
      history_db       : SubmissionHistory 实例（会逐题写入 answers 表）
      run_id           : 对应 history.start_run() 的返回值
      submission_index : 当前第几份（1-based），对应 answers.submission_index

    :return:
      True  本次提交成功
      False 本次提交失败
    """
    # 每轮提交流程共享一个人工介入锁，确保验证期间任何子步骤都被 hold
    lock = ManualHoldLock()
    try:
        return _do_one_submission_core(
            driver,
            survey_url,
            lock,
            history_db=history_db,
            run_id=run_id,
            submission_index=submission_index,
        )
    except Exception as e:
        print(f"  EX: {type(e).__name__}: {e}")
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        raise  # 重抛异常，让 retry_with_backoff 判定是否重试
