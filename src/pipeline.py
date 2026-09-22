"""核心业务编排模块（V2.3 第一章第 3 条整改后：只剩「编排」职责，实现下沉到 pipeline_stages/*）。

本章第 3 条建议：pipeline.py 职责收窄为"协调各阶段顺序"，不承担具体实现。
拆分后的 4 类实现：
    1. 页面加载 / ready-state / iframe  →  ``src/pipeline_stages/page_loader.py``
    2. 题目识别 / 等待 / 单题答题分发      →  ``src/pipeline_stages/question_stage.py``
    3. 验证码检查 + 人工介入等待           →  ``src/pipeline_stages/verification_stage.py``
    4. 提交按钮查找 + 提交效果确认         →  ``src/interactions/submit.py``（第一章第 2 条拆分）

本文件剩余的 2 个公开函数就是：
    - ``run_one_submission(driver, url, ...)``        → 最外层（带 retry + 异常清理）
    - ``_do_one_submission_core(driver, url, lock)`` → 单次核心流程（Step 1~8 编排）
"""

from __future__ import annotations

from typing import Any, Callable

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
    MAX_SURVEY_PAGES,
    VERIFY_EVERY_N_QUESTIONS,
    WEIGHT_CONFIG,
)
# v3.0 权重锚定：按题干认领权重 + 未命中提示
from .anchoring import report_unmatched_anchors
# v3.1 结构对拍：我们的探测结果 ↔ 平台在题目容器上自报的 topic/type
from .crosscheck import crosscheck_questions, report_structure_drift
# v3.1 提交前完整度自检：平台标了必填、我们整题没探测到的那些，别点提交
from .completeness import describe_gap, unanswered_required
# V2.3 第 1 章第 3 条：下沉各阶段实现到 pipeline_stages 子包
from .pipeline_stages import (
    _answer_one_question,
    _check_verification_with_lock,
    _ensure_questions_context,
    _robust_driver_get,
    _wait_for_questions,
    _wait_for_ready_state,
    advance_to_next_page,
    collect_blocked_alerts,
    describe_blocked_alerts,
    install_alert_recorder,
    # v3.1 补漏轮（--rescue-gaps）：完整度自检拦下之后接人工补答
    GAP_HOLD_TIMEOUT,
    hold_for_manual_fill,
    scroll_question_into_view,
)
# 题目探测（断点续填/题目结构识别）来自 detection 模块，不属于 pipeline 职责
from .detection import (
    detect_answered_questions,
    detect_platform_questions,
    detect_questions,
)
# 无头判定：补漏轮等的是"坐在窗口前的人"，无头下没有这个人
from .browser.driver_factory import driver_is_headless
# v3.1 结构对拍的题型码表在平台常量层（对拍读的是问卷星 DOM 上的 topic/type）
from .platforms import WJX
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
    SubmissionAborted,
    format_exc_log,
    raise_non_recoverable,
)

# history 模块为可选（纯 import 期不强依赖；真正 record 时检查参数是否传入）
# V2.4 整改：收窄到 ImportError——history 是纯 Python 无 Selenium 依赖，
# 旧版捕获 TRANSIENT_DOM_EXCEPTIONS 的分支永不可达，只会误导维护者。
try:
    from .history import SubmissionHistory
    _HAS_HISTORY: bool = True
except ImportError:  # pragma: no cover
    SubmissionHistory = None
    _HAS_HISTORY = False


# ---------------------------------------------------------------------------
#  单页作答（v3.0：分页问卷按页调用，单页问卷只调一次）
# ---------------------------------------------------------------------------
def _abort_if_stopped(
    stop_check: Callable[[], bool] | None,
    stage: str,
    question: int | None = None,
) -> None:
    if stop_check is not None and stop_check():
        raise SubmissionAborted(stage, question=question)


def _platform_structure(driver: Any, *, visible_only: bool = True) -> list[dict]:
    """平台自报的题目结构（题号 + 题型码 + 必答标记）；**任何失败都退化成"没有信号"**。

    对拍与提交前完整度自检都靠它，而两者都只是诊断：它们自己出问题时不能把一份本来
    能提交成功的问卷判失败，所以这里把异常吞干净、返回空列表 —— 调用方拿到空就是
    "没信号"，对拍整体静默、完整度自检不拦停。
    （与 ``detect_answered_questions`` 那处的降级同构，区别只在：那边降级会重答题目，
    这边降级什么都不损失。）

    :param visible_only: 逐页对拍用 ``True``；提交前的整卷自检用 ``False``。
    """
    try:
        return detect_platform_questions(driver, WJX, visible_only=visible_only)
    except TRANSIENT_DOM_EXCEPTIONS:
        return []
    except Exception as _e:
        raise_non_recoverable(_e)
        print("  " + format_exc_log(
            _e, action="读平台自报题型", recovery="跳过对拍与完整度自检",
        ))
        return []


def _answer_current_page(
    driver: Any,
    lock: ManualHoldLock,
    *,
    stop_check: Callable[[], bool] | None = None,
    history_db: Any | None = None,
    run_id: int | None = None,
    submission_index: int | None = None,
    no_record_text: bool = False,
) -> tuple[str, set[int], int]:
    """等题 → 探测 → 断点续填扫描 → 逐题作答，只管**当前可见的那一页**。

    :return: ``(status, 本页题号集合, 跳过已答数)``；
             status 为 ``"ok"`` 表示这一页该答的都过了，``"failed"`` 表示
             等待/探测环节就没过（调用方负责切回默认上下文并计本轮失败）。
    """
    # Step 4 等题目元素
    if not _wait_for_questions(
        driver, QUESTION_DETECT_TIMEOUT, hold_lock=lock, stop_check=stop_check
    ):
        return "failed", set(), 0

    # Step 5 探测题目结构
    questions = detect_questions(driver)
    if not questions:
        return "failed", set(), 0

    # v3.0 权重锚定：带了 anchor 却认不到题的条目**不会**退回答题号（见 anchoring 契约 1），
    # 但必须说出来 —— 静默走等权，和用户没配一样，只是没人知道预设其实没生效。
    for _anchor_line in report_unmatched_anchors(WEIGHT_CONFIG, questions):
        print("  " + _anchor_line)

    # v3.1 结构对拍：拿平台自报的 topic/type 与上面的探测结果比一次。锚定只能发现
    # "预设里的题干在这份卷上找不到"，发现不了"我们把这道题判成了别的题型"
    # —— 签名本来就是从探测结果算的，探测错了签名跟着错。纯诊断，不改作答行为。
    for _drift_line in report_structure_drift(
        crosscheck_questions(questions, _platform_structure(driver), WJX)
    ):
        print("  " + _drift_line)

    qnums = {int(q["q"]) for q in questions if isinstance(q.get("q"), int)}

    # Step 5.5 断点续填：扫描已填好的题号集合
    try:
        answered_set: set[int] = detect_answered_questions(driver)
    except TRANSIENT_DOM_EXCEPTIONS:
        answered_set = set()
    except Exception as _e:
        raise_non_recoverable(_e)
        print("  " + format_exc_log(
            _e, action="断点续填：扫描已填题号", submission_index=submission_index,
            recovery="降级为本轮全部重答",
        ))
        answered_set = set()
    skipped_count = 0

    # Step 6 逐题作答
    for qi, q in enumerate(questions):
        _abort_if_stopped(stop_check, "收到停止请求，本份问卷不再继续作答",
                          question=int(q["q"]))

        if qi > 0 and qi % VERIFY_EVERY_N_QUESTIONS == 0:
            if not _check_verification_with_lock(driver, lock, stop_check):
                return "failed", qnums, skipped_count

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
        # stop_check 作为 abort_check 传入：停顿被分片成 ≤0.2s，点停止后不必
        # 等满这一题的均值 ≈4.5s（含 5% 概率的 2~5s 长停顿）。
        human_pause(
            Q_THINK_MU, Q_THINK_SIGMA,
            Q_THINK_LO, Q_THINK_HI,
            long_pause_prob=Q_LONG_PAUSE_PROB,
            long_lo=Q_LONG_PAUSE_LO,
            long_hi=Q_LONG_PAUSE_HI,
            abort_check=stop_check,
        )
        # 停顿可能因停止而提前返回：必须在下一题之前问清楚，
        # 否则「停止」只省时间、不省动作，半份问卷照样被提交出去。
        _abort_if_stopped(stop_check, "逐题停顿期间收到停止请求，本份问卷不提交",
                          question=q_num)

    return "ok", qnums, skipped_count


# ---------------------------------------------------------------------------
#  提交失败的补充诊断：把页面自己说出来的原因捞回日志
# ---------------------------------------------------------------------------
def _report_submit_diagnostics(driver: Any) -> None:
    """打印本轮被拦下的 ``alert`` 文案（如果有的话）。

    问卷星的必填校验就是拿 alert 说"您第 N 题未填写"的。接管之前有两种结局，
    一种比一种难看：原生弹窗让下一条命令抛 ``UnexpectedAlertPresentException``
    （整轮按瞬态异常重跑，重跑之后原因早就没了），或者干脆什么都没留下，
    只剩一行看不出所以然的 unknown。
    """
    for _line in describe_blocked_alerts(collect_blocked_alerts(driver)):
        print("  " + _line)


# ---------------------------------------------------------------------------
#  v3.1 补漏轮（--rescue-gaps）：完整度自检拦下之后，把人工接进来
# ---------------------------------------------------------------------------
def _recheck_gap(driver: Any, detected_all: set[int]) -> list[int]:
    """人工补答之后的复检：**判据不变**，只是把"现在读得到的答案"也算成见过这道题。

    缺口题按定义不在我们的探测里，只重跑 ``detect_questions`` 的复检永远不会变空，
    等人工就等成了形式。所以这里额外并进出题探测与已答扫描两个读数 —— 二者都只能
    让缺口变小，不能凭空造出缺口。读不到就当没有新证据（原样返回缺口的口径由
    ``unanswered_required`` 保证），复检自身出问题绝不把本轮改成失败以外的样子。
    """
    seen: set[int] = set(detected_all)
    try:
        seen |= {int(q["q"]) for q in detect_questions(driver)
                 if isinstance(q.get("q"), int)}
        seen |= detect_answered_questions(driver)
    except TRANSIENT_DOM_EXCEPTIONS:
        pass
    except Exception as _e:
        raise_non_recoverable(_e)
        print("  " + format_exc_log(
            _e, action="补漏复检：读人工已补的题", recovery="按「没有新证据」处理",
        ))
    return unanswered_required(
        _platform_structure(driver, visible_only=False), seen
    )


def _rescue_gap(
    driver: Any,
    lock: ManualHoldLock,
    *,
    gap: list[int],
    detected_all: set[int],
    stop_check: Callable[[], bool] | None,
) -> list[int]:
    """把缺口交给人工补，返回复检后的缺口（空 = 可以点提交）。

    无头模式在这里直接跳过：等的是一个不存在的人，与验证码那处的取舍同一条线。
    """
    nums = "、".join(f"Q{n}" for n in gap)
    if driver_is_headless(driver):
        print(f"  [补漏] 无头模式下没有能补答的人 → 不等待，维持判失败（{nums}）")
        return gap
    print(f"  [补漏] 请在浏览器窗口里手动补答 {nums}，"
          f"最长等 {GAP_HOLD_TIMEOUT:.0f}s（停止/Ctrl+C 可中断，期间不会点提交）")
    scroll_question_into_view(driver, gap[0])
    return hold_for_manual_fill(
        lambda: _recheck_gap(driver, detected_all),
        lock=lock, stop_check=stop_check,
    )


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
    stop_check: Callable[[], bool] | None = None,
    rescue_gaps: bool = False,
) -> SubmitOutcome:
    """单次提交的真正实现（无外层重试，由调用者包 retry）。

    三态返回：
        "success" : 提交按钮已点击且页面出现成功信号
        "failed"  : 业务前置步骤失败（验证码超时 / 题目探测失败等）
        "unknown" : 提交按钮已点击但效果超时

    :param stop_check: 用户「停止」谓词。传入后逐题边界、每题之间的思考停顿、
        等待题目渲染、验证码人工等待都会以 ≈0.2s 的粒度问一次；返回 True 即抛
        ``SubmissionAborted`` —— **绝不**继续答下一题，更不点提交按钮。
        不传时行为与 v2.8 逐位一致（CLI/GUI 不传即不变）。
    :param rescue_gaps: v3.1 补漏轮开关。默认 False —— 完整度自检判出的缺口直接
        维持"判失败、不点提交"。打开后先把缺口交给在场的人工补答，补上了才点提交
        （无头模式下不等待，行为与默认一致）。
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
        # 走到这里只剩纯 Python bug（TRANSIENT_DOM_EXCEPTIONS 已含 WebDriverException
        # 基类）。按 exceptions.py 的契约留痕，不静默吞掉。
        print("  " + format_exc_log(
            _e, action="等待 document.body 就绪", submission_index=submission_index,
            recovery="交给后续 _wait_for_questions 兜底",
        ))

    # Step 2 验证码检查（打开页面立刻弹的情况）
    if not _check_verification_with_lock(driver, lock, stop_check):
        return SUBMIT_FAILED

    # Step 3 iframe 适配
    if not _ensure_questions_context(driver):
        driver.switch_to.default_content()
        return SUBMIT_FAILED

    # Step 3.5 接管 window.alert —— 必须在 frame 切换之后，弹窗是页面自己
    # 那个 realm 里弹的，钩子挂在 default content 上等于没挂。
    # 装不上不影响本轮提交：这是诊断能力，不是流程的一环。
    try:
        install_alert_recorder(driver)
    except Exception as _e:
        raise_non_recoverable(_e)
        print("  " + format_exc_log(
            _e, action="接管页面弹窗", submission_index=submission_index,
            recovery="本轮没有弹窗诊断，其余流程照常",
        ))

    # Step 4~6 逐页作答（v3.0 多分页问卷；单页问卷恰好走一圈）
    page_index = 0
    skipped_total = 0
    detected_total = 0
    # 整份问卷（跨所有页）探测到的题号并集：完整度自检要的是"这一份从头到尾见过哪些
    # 题"，只看最后那一页会把前面几页的必答题误判成漏答。
    detected_all: set[int] = set()
    while True:
        page_index += 1
        if page_index > MAX_SURVEY_PAGES:
            print(f"  [分页] 翻了 {MAX_SURVEY_PAGES} 页仍未到底 → 判失败，不提交"
                  "（只交了前几页的问卷可能被服务端当成一份完整回收）")
            driver.switch_to.default_content()
            return SUBMIT_FAILED

        page_status, qnums, page_skipped = _answer_current_page(
            driver, lock,
            stop_check=stop_check,
            history_db=history_db,
            run_id=run_id,
            submission_index=submission_index,
            no_record_text=no_record_text,
        )
        skipped_total += page_skipped
        detected_total += len(qnums)
        detected_all |= qnums
        if page_status != "ok":
            driver.switch_to.default_content()
            return SUBMIT_FAILED

        verdict, detail = advance_to_next_page(driver, qnums)
        if verdict == "advanced":
            print(f"  [分页] {detail}")
            continue
        if verdict == "failed":
            print(f"  [分页] {detail} → 判失败，不点提交")
            driver.switch_to.default_content()
            return SUBMIT_FAILED
        break   # no_more —— 已在最后一页，可以提交了

    if skipped_total > 0:
        print(f"  [续填] 跳过 {skipped_total} 道已填题，"
              f"本次重答 {detected_total - skipped_total} 道")

    # Step 7 全题答完后再检查一次验证码（提交前问卷星最爱弹）
    _abort_if_stopped(stop_check, "本页已答完但收到停止请求，不点提交")
    if not _check_verification_with_lock(driver, lock, stop_check):
        driver.switch_to.default_content()
        return SUBMIT_FAILED

    # Step 7.5 提交前完整度自检：平台标了必答、而我们**整题都没探测到**的题，
    # 交上去注定被必填拦下 —— 那就别点提交，并直接说是哪几题。
    # 判据只收零歧义的那一种（探测到了但题型判错的归 [对拍] 管），
    # 契约与"为什么不管另一半"见 src/completeness.py。
    _gap = unanswered_required(
        _platform_structure(driver, visible_only=False), detected_all
    )
    if _gap:
        print("  " + describe_gap(_gap))
        # v3.1 补漏轮：这类题我们答不了，但在场的人能。默认关 —— 不开就是
        # "判失败、不点提交"，一行都不多打。
        if rescue_gaps:
            _gap = _rescue_gap(
                driver, lock, gap=_gap, detected_all=detected_all,
                stop_check=stop_check,
            )
        if _gap:
            driver.switch_to.default_content()
            return SUBMIT_FAILED

    # Step 8 点击提交 + 提交后快进
    _abort_if_stopped(stop_check, "点击提交前收到停止请求")
    submit_result = find_and_click_submit(driver)
    if submit_result == SUBMIT_FAILED:
        _report_submit_diagnostics(driver)
        driver.switch_to.default_content()
        return SUBMIT_FAILED
    if submit_result == SUBMIT_UNKNOWN:
        print("  [提交] 状态未知：按钮已点击但未观察到成功信号（超时 / AJAX / 服务端拒绝）"
              "→ 保守计为失败")
        _report_submit_diagnostics(driver)
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
                abort_check=stop_check,
            )
    except TRANSIENT_DOM_EXCEPTIONS:
        # 提交后验证码探测是二次检查，失败不影响最终成功判定
        pass
    except SubmissionAborted:
        # 此刻 SUBMIT_SUCCESS 已是既成事实（按钮点过、成功信号看过）。
        # 停止只该让批次不再开下一份；在这里继续上抛会把这一份凭空抹掉，
        # 成功计数少一、续传起点跟着错。
        pass
    except Exception as _e:
        raise_non_recoverable(_e)
        print("  " + format_exc_log(
            _e, action="提交后验证码二次探测", submission_index=submission_index,
            recovery="跳过二次探测，按已成功进入收尾",
        ))

    # 成功已成事实：此处清理绝不能向上抛。
    # 此前它裸在 try 外，提交跳转导致 NoSuchWindowException 时会冒泡到
    # run_one_submission 的 @retry_with_backoff(retry_on=WebDriverException)，
    # 把**已经成功提交**的问卷整份重填重交一遍（重复提交）。
    try:
        driver.switch_to.default_content()
    except TRANSIENT_DOM_EXCEPTIONS:
        pass
    except Exception as _e:
        raise_non_recoverable(_e)
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
    stop_check: Callable[[], bool] | None = None,
    rescue_gaps: bool = False,
) -> SubmitOutcome:
    """外层 retry + 清理 + 重抛异常（供 GUI / CLI 批处理循环调用）。

    - 指数退避重试（只在 WebDriverException 层做）
    - 异常时：打印日志 + 切回默认上下文 + 重抛给 retry_with_backoff 判定
    - 正常时：返回三态 SubmitOutcome
    - ``stop_check`` 返回 True 时抛 ``SubmissionAborted``（BaseException 派生，
      既不被本函数的 ``except Exception`` 清理分支吞掉，也不在重试范围内）
    - ``rescue_gaps``：v3.1 补漏轮，见 :func:`_do_one_submission_core`
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
            stop_check=stop_check,
            rescue_gaps=rescue_gaps,
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
