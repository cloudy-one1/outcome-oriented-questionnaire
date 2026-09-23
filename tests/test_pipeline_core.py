"""``src/pipeline.py`` 编排层 Step 1~8 的分支契约测试（v2.7 新增）。

为什么必须有这个文件：README「已知缺口（诚实记录）」里 ``src/pipeline.py`` 只有
**26%**，注释写的是"编排层，E2E 已覆盖主干"。但 E2E 是 **非阻塞** job，没有浏览器
时整个 job 自己 skip → 主干之外的**失败分支**在 CI 里其实一点防御都没有，而那正是
两个 P0 藏身的地方：

1. **重复提交同一份问卷**：成功返回前的 ``driver.switch_to.default_content()``
   曾裸在 try 之外。提交本身会让页面跳转，此时这句清理恰好最容易抛
   ``NoSuchWindowException``；异常冒泡到 ``run_one_submission`` 的
   ``@retry_with_backoff(retry_on=(WebDriverException,))`` → 把**已经交成功**的
   问卷整份重填重交一遍。本文件用 test_success_reset_* 与
   test_run_one_submission_never_retries_a_success_result 一对夹逼锁死它：
   SUCCESS 不重试、真异常才重试。
2. **一次验证码超时干掉整批**：所以"业务前置失败 → 返回 SUBMIT_FAILED"和
   "unknown 保守计失败"必须是**返回值**而不是异常，本文件逐步骤锁住出口。

另外锁两件事：
  - **上下文复位的不对称**（Step 2 不切、Step 3/4/5/6/7/8 切、成功路径切一次）——
    少切会把半截 iframe 上下文带给下一份问卷，多切（在已跳转的页面上切）就是
    上面那个 P0 的触发点，所以每个分支都断言精确次数。
  - **参数接线**：``hold_lock`` / ``QUESTION_DETECT_TIMEOUT`` /
    ``history_db`` / ``run_id`` / ``submission_index`` / ``no_record_text``
    历史上出现过"算了却没传"的同类 bug（v2.3 漏 lock、--resume 静默失效）。

打桩方式：阶段函数是从 ``pipeline_stages`` / ``detection`` / ``interactions.submit``
**按名字** import 进 ``src.pipeline`` 的，所以 patch ``src.pipeline.<name>`` 才生效；
只有 Step 8 之后的 ``is_smart_verification_showing`` / ``wait_for_manual_verification``
是函数体内延迟 import，必须 patch 源模块 ``src.verification``。全程无浏览器、无网络、
``human_pause`` 被桩成 no-op，退避 sleep 也被清零。
"""

from __future__ import annotations

import contextlib
import os
import sys
import types
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from selenium.common.exceptions import (
    NoSuchWindowException,
    StaleElementReferenceException,
    WebDriverException,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import pipeline, verification  # noqa: E402
from src.config import (  # noqa: E402
    QUESTION_DETECT_TIMEOUT,
    SUBMISSION_MAX_ATTEMPTS,
    VERIFICATION_TIMEOUT,
    VERIFY_EVERY_N_QUESTIONS,
)
from src.exceptions import SubmissionAborted  # noqa: E402
from src.interactions.submit import (  # noqa: E402
    SUBMIT_FAILED,
    SUBMIT_SUCCESS,
    SUBMIT_UNKNOWN,
)
from src.utils import ManualHoldLock  # noqa: E402

SURVEY_URL = "https://www.wjx.cn/vm/xxxx.aspx"


def _questions(*nums: int) -> list[dict]:
    """构造 ``detect_questions`` 形状的题目列表。"""
    return [{"q": n, "type": "single"} for n in nums]


class FakeSwitchTo:
    """可注入故障的 switch_to：default_content 正是两个历史缺陷的抛点。"""

    def __init__(self, error: BaseException | None = None) -> None:
        self._error = error
        self.calls = 0

    def default_content(self) -> None:
        self.calls += 1
        if self._error is not None:
            raise self._error

    def frame(self, _target: Any) -> None:
        pass


class FakeDriver:
    """假 WebDriver：编排层真实碰到的只有 ``switch_to`` 与 ``execute_script``。"""

    def __init__(
        self,
        *,
        switch_error: BaseException | None = None,
        script_error: BaseException | None = None,
    ) -> None:
        self.switch_to = FakeSwitchTo(switch_error)
        self.current_url = SURVEY_URL
        self.scripts: list[str] = []
        self._script_error = script_error

    def execute_script(self, script: str, *_args: Any, **_kw: Any) -> Any:
        self.scripts.append(script)
        if self._script_error is not None:
            raise self._script_error
        # Step 1 的 body 存在性探测用 WebDriverWait(3s) 轮询：返回假值会白等 3 秒，
        # 这里恒真让它一次通过（其余脚本探测都被 stages() 桩掉了）
        return True


# 这两个验证码函数在 pipeline 里是函数体内延迟 import，必须打到源模块上
_IN_VERIFICATION_MODULE = frozenset({
    "is_smart_verification_showing",
    "wait_for_manual_verification",
})

_HAPPY: dict[str, Any] = {
    # Step 1 页面加载
    "_robust_driver_get": None,
    "_wait_for_ready_state": None,
    # Step 2 开页验证码 / Step 3 iframe / Step 4 等题目
    "_check_verification_with_lock": True,
    "_ensure_questions_context": True,
    "_wait_for_questions": True,
    # Step 5 题目结构 / Step 5.5 断点续填扫描
    "detect_questions": _questions(1),
    "detect_answered_questions": set(),
    # Step 6 逐题作答（human_pause no-op：单题「思考时间」不该出现在单测里）
    "_answer_one_question": True,
    "human_pause": None,
    # Step 7 提交前复查 + Step 8 提交三态与提交后复查
    "find_and_click_submit": SUBMIT_SUCCESS,
    "is_smart_verification_showing": False,
    "wait_for_manual_verification": True,
}


def _is_exc_spec(value: Any) -> bool:
    return isinstance(value, BaseException) or (
        isinstance(value, type) and issubclass(value, BaseException)
    )


@contextlib.contextmanager
def stages(**overrides: Any) -> Iterator[types.SimpleNamespace]:
    """把 Step 1~8 的全部阶段在 ``src.pipeline`` 打桩成「一路成功」。

    用例只覆盖自己关心的那一个分支：值传异常（类或实例）→ side_effect；
    已经是 Mock 的原样使用（需要 side_effect 序列时用 ``mock.Mock(side_effect=…)``）；
    其余值 → return_value。
    """
    mocks: dict[str, mock.Mock] = {}
    for name, value in {**_HAPPY, **overrides}.items():
        if isinstance(value, mock.Mock):
            m = value
        elif _is_exc_spec(value):
            m = mock.Mock(name=name, side_effect=value)
        else:
            m = mock.Mock(name=name, return_value=value)
        mocks[name] = m

    with contextlib.ExitStack() as stack:
        for name, m in mocks.items():
            target = verification if name in _IN_VERIFICATION_MODULE else pipeline
            stack.enter_context(mock.patch.object(target, name, m))
        yield types.SimpleNamespace(**mocks)


def _core(driver: Any, lock: ManualHoldLock, **kw: Any) -> Any:
    return pipeline._do_one_submission_core(driver, SURVEY_URL, lock, **kw)


def _answered_nums(answer: mock.Mock) -> list[int]:
    """``_answer_one_question`` 实际被作答的题号序列（顺序敏感）。"""
    return [c.args[1]["q"] for c in answer.call_args_list]


# ---------------------------------------------------------------------------
#  Step 1：body 存在性探测只是「提前优化」，抖动不该影响后面步骤
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("probe_error", [
    StaleElementReferenceException("navigating"),
    ValueError("unexpected js payload"),
], ids=["transient", "logic"])
def test_body_probe_errors_do_not_abort_the_run(probe_error: BaseException) -> None:
    """3s 短探测失败后有后续 WebDriverWait 兜底，不该在 Step 1 就翻车。"""
    driver = FakeDriver(script_error=probe_error)
    with stages():
        assert _core(driver, ManualHoldLock()) == SUBMIT_SUCCESS


def test_body_probe_keyboard_interrupt_still_escapes() -> None:
    """兜底 except Exception 不能吃 Ctrl+C（异常分层的第一条红线）。"""
    driver = FakeDriver(script_error=KeyboardInterrupt())
    with stages(), pytest.raises(KeyboardInterrupt):
        _core(driver, ManualHoldLock())


# ---------------------------------------------------------------------------
#  Step 2/3/4/5：前置失败必须在正确的步骤以 SUBMIT_FAILED 出口
# ---------------------------------------------------------------------------
def test_step2_verification_timeout_fails_before_any_context_reset() -> None:
    """开页即弹验证且人工超时 → 立刻失败。

    这一步还停在主文档（没进过 iframe），所以**不该**调 default_content：
    上下文复位的不对称是刻意设计，多调一次就是 P0-1 的触发面。
    """
    driver = FakeDriver()
    lock = ManualHoldLock()
    with stages(_check_verification_with_lock=False) as st:
        assert _core(driver, lock) == SUBMIT_FAILED

    st._check_verification_with_lock.assert_called_once_with(driver, lock, None)
    st._ensure_questions_context.assert_not_called()
    assert driver.switch_to.calls == 0


def test_step3_iframe_context_failure_resets_default_content() -> None:
    """iframe 定位失败：半截上下文必须清掉，否则污染下一份问卷。"""
    driver = FakeDriver()
    with stages(_ensure_questions_context=False) as st:
        assert _core(driver, ManualHoldLock()) == SUBMIT_FAILED

    assert driver.switch_to.calls == 1
    st._wait_for_questions.assert_not_called()


def test_step4_questions_timeout_forwards_budget_and_lock() -> None:
    """等题目超时 → 复位 + 失败；超时预算与人工介入锁必须真的接线。"""
    driver = FakeDriver()
    lock = ManualHoldLock()
    with stages(_wait_for_questions=False) as st:
        assert _core(driver, lock) == SUBMIT_FAILED

    st._wait_for_questions.assert_called_once_with(
        driver, QUESTION_DETECT_TIMEOUT, hold_lock=lock, stop_check=None,
    )
    assert driver.switch_to.calls == 1
    st.detect_questions.assert_not_called()


def test_step5_empty_question_structure_resets_context() -> None:
    driver = FakeDriver()
    with stages(detect_questions=[]) as st:
        assert _core(driver, ManualHoldLock()) == SUBMIT_FAILED

    assert driver.switch_to.calls == 1
    st._answer_one_question.assert_not_called()


# ---------------------------------------------------------------------------
#  Step 5 的另一半：一道题都没有时先问一句"是不是整页形态不对"（v3.1）
#
#  只提示、不拦停 —— 判定必须仍是 SUBMIT_FAILED（不能因为"知道是移动端形态"就
#  放行，也不能因此改成第三种结果）。探针本身的形状判据在 tests/test_mobile_layout.py。
# ---------------------------------------------------------------------------
def test_layout_notice_is_printed_when_nothing_was_detected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    notice = mock.Mock(name="mobile_layout_notice", return_value="[布局] 移动端投放形态")
    with stages(detect_questions=[], mobile_layout_notice=notice):
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_FAILED

    assert "[布局]" in capsys.readouterr().out
    notice.assert_called_once()


def test_layout_notice_is_quiet_when_questions_were_detected() -> None:
    """探测到题了就别去问形态：那是纯噪声，外加一次没必要的 JS 往返。"""
    notice = mock.Mock(name="mobile_layout_notice",
                       side_effect=AssertionError("有题就不该问形态"))
    with stages(mobile_layout_notice=notice):
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_SUCCESS


def test_no_signal_page_still_fails_without_any_notice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """接线用**真**探针跑一遍空页面：读不出整数（FakeDriver 恒真）就必须一字不说。

    刻意不 patch 掉它 —— "import 了却没调用"与"没信号还瞎报"都是这条接线会坏的形状。
    """
    from src import detection

    detection.reset_mobile_layout_notice()
    try:
        with stages(detect_questions=[]):
            assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_FAILED
        assert "[布局]" not in capsys.readouterr().out
    finally:
        detection.reset_mobile_layout_notice()


# ---------------------------------------------------------------------------
#  Step 5.5：断点续填 —— 已填题不重答，扫描抖动不影响本轮
# ---------------------------------------------------------------------------
def test_resume_skips_already_answered_questions(capsys: pytest.CaptureFixture[str]) -> None:
    driver = FakeDriver()
    with stages(
        detect_questions=_questions(1, 2, 3),
        detect_answered_questions={1, 3},
    ) as st:
        assert _core(driver, ManualHoldLock()) == SUBMIT_SUCCESS

    assert _answered_nums(st._answer_one_question) == [2], "已填题号不得重答"
    st.human_pause.assert_called_once()  # 只有真作答的题才花「思考时间」
    out = capsys.readouterr().out
    assert "[续填] 跳过 2 道已填题" in out
    assert "本次重答 1 道" in out
    assert driver.switch_to.calls == 1  # 成功路径恰好一次（Step 8 之后）


def test_resume_line_absent_when_nothing_answered(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """负向对照：skipped_count==0 时不打「续填」行，免得日志骗人。"""
    driver = FakeDriver()
    with stages(detect_questions=_questions(1, 2)) as st:
        assert _core(driver, ManualHoldLock()) == SUBMIT_SUCCESS

    assert _answered_nums(st._answer_one_question) == [1, 2]
    assert "续填" not in capsys.readouterr().out


@pytest.mark.parametrize("scan_error", [
    StaleElementReferenceException("dom replaced"),
    ValueError("bad js json"),
], ids=["transient", "logic"])
def test_resume_scan_error_continues_with_empty_set(scan_error: BaseException) -> None:
    """续填扫描只是优化：它挂了要当作「没填过」继续答完，而不是让本轮失败。"""
    driver = FakeDriver()
    with stages(
        detect_questions=_questions(1, 2),
        detect_answered_questions=scan_error,
    ) as st:
        assert _core(driver, ManualHoldLock()) == SUBMIT_SUCCESS

    assert _answered_nums(st._answer_one_question) == [1, 2]


def test_resume_scan_keyboard_interrupt_escapes() -> None:
    driver = FakeDriver()
    with stages(detect_answered_questions=KeyboardInterrupt()), \
            pytest.raises(KeyboardInterrupt):
        _core(driver, ManualHoldLock())


# ---------------------------------------------------------------------------
#  Step 6 中途复查：长问卷中途弹验证
# ---------------------------------------------------------------------------
def test_midloop_reverification_failure_returns_failed_and_resets_context() -> None:
    """第 VERIFY_EVERY_N_QUESTIONS 题必须复查一次验证码。

    题目数按配置推导（N+1 道），保证换配置后这条用例仍然跨过阈值。
    """
    driver = FakeDriver()
    nums = list(range(1, VERIFY_EVERY_N_QUESTIONS + 2))
    with stages(
        detect_questions=_questions(*nums),
        # 第 1 次 = Step 2 开页检查（放行），第 2 次 = 中途复查（人工超时）
        _check_verification_with_lock=mock.Mock(side_effect=[True, False]),
    ) as st:
        assert _core(driver, ManualHoldLock()) == SUBMIT_FAILED

    assert st._check_verification_with_lock.call_count == 2
    assert _answered_nums(st._answer_one_question) == list(range(1, VERIFY_EVERY_N_QUESTIONS + 1))
    assert driver.switch_to.calls == 1
    st.find_and_click_submit.assert_not_called()


def test_step7_pre_submit_reverification_failure_returns_failed() -> None:
    """全部答完、点提交之前的最后一次复查（问卷星最爱在这一步弹窗）。

    单题设计：qi=0 不触发中途复查，所以第 2 次调用必然是 Step 7。
    """
    driver = FakeDriver()
    with stages(
        detect_questions=_questions(1),
        _check_verification_with_lock=mock.Mock(side_effect=[True, False]),
    ) as st:
        assert _core(driver, ManualHoldLock()) == SUBMIT_FAILED

    assert _answered_nums(st._answer_one_question) == [1], "失败前题目应已答完"
    assert st._check_verification_with_lock.call_count == 2
    assert driver.switch_to.calls == 1
    st.find_and_click_submit.assert_not_called()


# ---------------------------------------------------------------------------
#  Step 8：提交三态 + 提交后验证码复查
# ---------------------------------------------------------------------------
def test_submit_unknown_is_reported_as_unknown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """按钮点下去了但没看到成功信号 → 返回 "unknown"，由上游保守计失败。"""
    driver = FakeDriver()
    with stages(find_and_click_submit=SUBMIT_UNKNOWN) as st:
        result = _core(driver, ManualHoldLock())

    assert result == SUBMIT_UNKNOWN == "unknown"
    out = capsys.readouterr().out
    assert "状态未知" in out and "保守计为失败" in out
    assert driver.switch_to.calls == 1
    # 状态未知时不再等最终验证码（可能压根没提交成功，等 60s 纯属折磨）
    st.is_smart_verification_showing.assert_not_called()


def test_submit_failed_resets_context(capsys: pytest.CaptureFixture[str]) -> None:
    driver = FakeDriver()
    with stages(find_and_click_submit=SUBMIT_FAILED) as st:
        assert _core(driver, ManualHoldLock()) == SUBMIT_FAILED

    assert driver.switch_to.calls == 1
    st.is_smart_verification_showing.assert_not_called()
    assert "状态未知" not in capsys.readouterr().out


def test_post_submit_captcha_waits_with_shared_lock_and_capped_timeout() -> None:
    """提交后仍弹验证要等，但预算被截到 min(VERIFICATION_TIMEOUT, 60)：
    整批不能因为一份问卷的人工介入停 120s。"""
    driver = FakeDriver()
    lock = ManualHoldLock()
    with stages(
        is_smart_verification_showing=True,
        wait_for_manual_verification=True,
    ) as st:
        assert _core(driver, lock) == SUBMIT_SUCCESS

    st.wait_for_manual_verification.assert_called_once_with(
        driver, timeout_seconds=min(VERIFICATION_TIMEOUT, 60), hold_lock=lock,
        abort_check=None,
    )


@pytest.mark.parametrize("recheck_error", [
    WebDriverException("page jumped"),
    ValueError("unexpected probe result"),
], ids=["transient", "logic"])
def test_post_submit_recheck_error_does_not_change_success(recheck_error: BaseException) -> None:
    """提交后的验证码探测是二次检查：它挂了绝不改判成功。"""
    driver = FakeDriver()
    with stages(
        is_smart_verification_showing=recheck_error,
        wait_for_manual_verification=mock.Mock(name="should_not_wait"),
    ) as st:
        assert _core(driver, ManualHoldLock()) == SUBMIT_SUCCESS

    st.wait_for_manual_verification.assert_not_called()


# ---------------------------------------------------------------------------
#  P0-1 回归夹逼：成功已成事实 + SUCCESS 永不被重试
# ---------------------------------------------------------------------------
def test_success_path_reset_failure_does_not_propagate() -> None:
    """提交跳转使 default_content 抛 NoSuchWindowException → 仍返回 SUCCESS。

    以前这句裸在 try 外：异常冒泡到 @retry_with_backoff(retry_on=WebDriverException)
    → 已成功提交的问卷被整份重填重交（重复提交）。
    """
    driver = FakeDriver(switch_error=NoSuchWindowException("no such window"))
    with stages():
        assert _core(driver, ManualHoldLock()) == SUBMIT_SUCCESS
    assert driver.switch_to.calls == 1


def test_success_path_reset_survives_programming_error_too() -> None:
    """非瞬态清理异常同样不能推翻「已提交成功」这个事实。"""
    driver = FakeDriver(switch_error=ValueError("switch_to is broken"))
    with stages():
        assert _core(driver, ManualHoldLock()) == SUBMIT_SUCCESS


def test_keyboard_interrupt_in_success_cleanup_still_escapes() -> None:
    """但 Ctrl+C 优先于返回值：清理阶段可以吞 WebDriver 抖动，不能吞中断。"""
    driver = FakeDriver(switch_error=KeyboardInterrupt())
    with stages(), pytest.raises(KeyboardInterrupt):
        _core(driver, ManualHoldLock())


def test_run_one_submission_never_retries_a_successful_core() -> None:
    """夹逼的另一半：core 返回 SUCCESS 时只跑一次，参数原样转发。"""
    driver = FakeDriver()
    lock = ManualHoldLock()
    core = mock.Mock(return_value=SUBMIT_SUCCESS)
    with mock.patch.object(pipeline, "_do_one_submission_core", core):
        assert pipeline.run_one_submission(driver, SURVEY_URL, lock) == SUBMIT_SUCCESS

    core.assert_called_once_with(
        driver, SURVEY_URL, lock,
        history_db=None, run_id=None, submission_index=None, no_record_text=False,
        stop_check=None, rescue_gaps=False, manual_submit=False,
    )


def test_webdriver_exception_from_core_is_retried() -> None:
    """对照组：还没提交成功就抛 WebDriverException，才应该重试第二次。

    两条合起来才是"不重复提交"的完整语义 —— 只断言上面那条，
    有人把 retry_on 改成 () 也照样全绿。
    """
    core = mock.Mock(side_effect=[NoSuchWindowException("jump"), SUBMIT_SUCCESS])
    with mock.patch.object(pipeline, "_do_one_submission_core", core), \
            mock.patch("src.utils.random.uniform", return_value=0.0):  # 退避延迟清零
        result = pipeline.run_one_submission(FakeDriver(), SURVEY_URL, ManualHoldLock())
    assert result == SUBMIT_SUCCESS

    assert core.call_count == 2


def test_retry_is_bounded_and_reraises_the_last_error() -> None:
    """重试上限必须是 SUBMISSION_MAX_ATTEMPTS —— 它决定重复提交的最坏放大倍数。"""
    core = mock.Mock(side_effect=WebDriverException("session died"))
    with mock.patch.object(pipeline, "_do_one_submission_core", core), \
            mock.patch("src.utils.random.uniform", return_value=0.0), \
            pytest.raises(WebDriverException, match="session died"):
        pipeline.run_one_submission(FakeDriver(), SURVEY_URL, ManualHoldLock())

    assert core.call_count == SUBMISSION_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
#  run_one_submission 的异常出口：记日志 + 尽力复位 + 原样上抛
# ---------------------------------------------------------------------------
def test_non_webdriver_error_is_logged_reset_and_reraised(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """数据契约类异常（ValueError 等）不进重试：日志要带 submission_index 便于定位。"""
    driver = FakeDriver()
    core = mock.Mock(side_effect=ValueError("题目字典缺 q 键"))
    with mock.patch.object(pipeline, "_do_one_submission_core", core):
        with pytest.raises(ValueError, match="题目字典缺 q 键"):
            pipeline.run_one_submission(
                driver, SURVEY_URL, ManualHoldLock(), submission_index=9,
            )

    out = capsys.readouterr().out
    assert "[单次提交核心流程]" in out
    assert "submission_index=9" in out
    assert "尝试回到默认上下文并重抛给外层重试" in out
    assert driver.switch_to.calls == 1
    core.assert_called_once()  # 不在 retry_on 里 → 一次都不重跑


@pytest.mark.parametrize("cleanup_error", [
    NoSuchWindowException("frame already gone"),
    ValueError("context is dead"),
], ids=["transient", "logic"])
def test_cleanup_failure_never_replaces_the_original_error(
    cleanup_error: BaseException,
) -> None:
    """复位自身又抛错时，上抛的必须还是原始异常，否则日志里只剩噪声。"""
    driver = FakeDriver(switch_error=cleanup_error)
    with mock.patch.object(pipeline, "_do_one_submission_core",
                           mock.Mock(side_effect=ValueError("原始错误"))):
        with pytest.raises(ValueError, match="原始错误"):
            pipeline.run_one_submission(driver, SURVEY_URL, ManualHoldLock())

    assert driver.switch_to.calls == 1


@pytest.mark.parametrize("fatal", [KeyboardInterrupt(), SystemExit(3)],
                         ids=["sigint", "systemexit"])
def test_non_recoverable_signals_are_never_swallowed(fatal: BaseException) -> None:
    """清理链路（日志 + 复位 + 重试判定）一口都不能吃掉 Ctrl+C / sys.exit。"""
    driver = FakeDriver()
    with mock.patch.object(pipeline, "_do_one_submission_core", mock.Mock(side_effect=fatal)):
        with pytest.raises(type(fatal)):
            pipeline.run_one_submission(driver, SURVEY_URL, ManualHoldLock())

    assert driver.switch_to.calls == 0, "raise_non_recoverable 必须早于任何清理动作"


# ---------------------------------------------------------------------------
#  参数接线：history/身份参数与 no_record_text 一路传到单题作答
# ---------------------------------------------------------------------------
def test_history_and_identity_params_reach_the_answer_step() -> None:
    history_db = mock.Mock(name="history_db")
    driver = FakeDriver()
    lock = ManualHoldLock()
    with stages(detect_questions=_questions(1, 2)) as st:
        assert _core(
            driver, lock,
            history_db=history_db, run_id=7, submission_index=3, no_record_text=True,
        ) == SUBMIT_SUCCESS

    assert _answered_nums(st._answer_one_question) == [1, 2]
    # 题号走位置参数：换成全关键字/漏传 driver 都属于契约破坏（历史 bug 形态）
    seen = [(c.args[0] is driver, c.args[1]["q"])
            for c in st._answer_one_question.call_args_list]
    assert seen == [(True, 1), (True, 2)]
    for call in st._answer_one_question.call_args_list:
        assert call.kwargs == {
            "history_db": history_db,
            "run_id": 7,
            "submission_index": 3,
            # 隐私开关（审查 P2-3）：漏传一次就等于把明文答案写进 SQLite
            "no_record_text": True,
        }
    st._robust_driver_get.assert_called_once_with(driver, SURVEY_URL)


def test_answer_step_defaults_are_conservative_when_omitted() -> None:
    driver = FakeDriver()
    with stages() as st:
        assert _core(driver, ManualHoldLock()) == SUBMIT_SUCCESS

    assert st._answer_one_question.call_args.kwargs == {
        "history_db": None, "run_id": None, "submission_index": None,
        "no_record_text": False,
    }


# ---------------------------------------------------------------------------
#  v3.0：轮内停止谓词（stop_check → SubmissionAborted）
#
#  补的是 v2.7 CHANGELOG「已知缺口」明说留给下轮的那条：v2.6 只让**轮间**停顿可打断，
#  逐题边界与每题之间的思考停顿（均值 ≈4.5s/题）打不断，点停止仍要等整份问卷答完
#  并点到提交。这里锁三件事：
#    1. 停止一定发生在**下一题之前**，且提交按钮根本不会被点；
#    2. 停顿真的收到了 abort_check（接线，不是又算了一遍没传出去）；
#    3. 提交成功之后才来的停止不改判这一份 —— 否则成功数会凭空少一。
# ---------------------------------------------------------------------------
def _stop_after_answers(answer: mock.Mock, n: int):
    """作答满 n 题后停止信号才为真：把"何时能停"绑在真实进度上，不数轮询次数。"""
    return lambda: answer.call_count >= n


def test_stop_before_first_question_answers_nothing_and_never_submits() -> None:
    driver = FakeDriver()
    with stages() as st:
        with pytest.raises(SubmissionAborted):
            _core(driver, ManualHoldLock(), stop_check=lambda: True)

    st._answer_one_question.assert_not_called()
    st.find_and_click_submit.assert_not_called()
    assert driver.switch_to.calls == 0, "抛出路径不该顺手切上下文（Step 2 的不对称契约）"


def test_stop_mid_survey_stops_at_the_next_question_boundary() -> None:
    """答完 Q1 时按下停止 → Q2/Q3 不作答、不提交。"""
    answer = mock.Mock(return_value=True)
    driver = FakeDriver()
    with stages(_answer_one_question=answer, detect_questions=_questions(1, 2, 3)) as st:
        with pytest.raises(SubmissionAborted):
            _core(driver, ManualHoldLock(), stop_check=_stop_after_answers(answer, 1))

    assert _answered_nums(answer) == [1]
    st.find_and_click_submit.assert_not_called()


def test_stop_after_all_questions_never_clicks_submit() -> None:
    """全题答完才停：这份问卷只答不交（半份提交会真占用一次名额）。"""
    answer = mock.Mock(return_value=True)
    driver = FakeDriver()
    with stages(
        _answer_one_question=answer,
        detect_questions=_questions(1, 2),
        find_and_click_submit=mock.Mock(return_value=SUBMIT_SUCCESS),
    ) as st:
        with pytest.raises(SubmissionAborted):
            _core(driver, ManualHoldLock(), stop_check=_stop_after_answers(answer, 2))

    assert _answered_nums(answer) == [1, 2]
    st.find_and_click_submit.assert_not_called()


def test_per_question_pause_receives_the_stop_predicate() -> None:
    """每题之间的思考停顿必须真的拿到 abort_check —— 否则停止只是省不了 4.5s。"""
    def chk() -> bool:
        return False

    pause = mock.Mock(return_value=0.0)
    driver = FakeDriver()
    with stages(human_pause=pause):
        assert _core(driver, ManualHoldLock(), stop_check=chk) == SUBMIT_SUCCESS

    assert pause.call_args.kwargs["abort_check"] is chk


def test_per_question_pause_gets_no_abort_check_when_not_stopping() -> None:
    """对照组：不传 stop_check 时 abort_check 仍是 None，CLI/老调用点一字不变。"""
    pause = mock.Mock(return_value=0.0)
    driver = FakeDriver()
    with stages(human_pause=pause):
        assert _core(driver, ManualHoldLock()) == SUBMIT_SUCCESS

    assert pause.call_args.kwargs["abort_check"] is None


def test_stop_during_post_submit_captcha_wait_keeps_the_success() -> None:
    """提交成功**之后**的验证码等待被打断：既成事实不能被抹成"未计数"。

    成功份数少一，续传起点就会跟着错（count_done_submissions 按已落盘份数算）。
    """
    waiter = mock.Mock(side_effect=SubmissionAborted("验证码等待期间收到停止"))
    driver = FakeDriver()
    with stages(
        is_smart_verification_showing=True,
        wait_for_manual_verification=waiter,
    ) as st:
        result = _core(driver, ManualHoldLock(), stop_check=lambda: False)

    assert result == SUBMIT_SUCCESS
    st.find_and_click_submit.assert_called_once_with(driver)
    assert waiter.call_args.kwargs["abort_check"] is not None


def test_run_one_submission_does_not_retry_an_abort() -> None:
    """停止不是 WebDriverException：既不能被清理分支吞掉，也不能触发第二次提交。"""
    def chk() -> bool:
        return True

    core = mock.Mock(side_effect=SubmissionAborted("stop"))
    with mock.patch.object(pipeline, "_do_one_submission_core", core):
        with pytest.raises(SubmissionAborted):
            pipeline.run_one_submission(
                FakeDriver(), SURVEY_URL, ManualHoldLock(), stop_check=chk,
            )

    core.assert_called_once()
    assert core.call_args.kwargs["stop_check"] is chk


def test_stale_anchor_is_reported_once_across_submissions(capsys) -> None:
    """v3.0：认不到题的锚点必须说一次，而且只说一次（17 份刷 17 行会埋掉运行信息）。

    刻意不 patch ``report_unmatched_anchors`` —— 接线断在"import 了却没调用"时，
    patch 出来的绿毫无意义（v2.4 的 --resume 就是这个形状）。
    """
    from src import anchoring, config

    saved = dict(config.WEIGHT_CONFIG)
    config.WEIGHT_CONFIG.clear()
    config.WEIGHT_CONFIG[4] = {
        "type": "single", "weights": [1, 1],
        "anchor": {"title": "您对客服的态度满意吗", "signature": "single:5"},
    }
    try:
        driver = FakeDriver()
        with stages():
            assert _core(driver, ManualHoldLock()) == SUBMIT_SUCCESS
        assert "不生效" in capsys.readouterr().out

        with stages():
            assert _core(driver, ManualHoldLock()) == SUBMIT_SUCCESS
        assert "不生效" not in capsys.readouterr().out
    finally:
        config.WEIGHT_CONFIG.clear()
        config.WEIGHT_CONFIG.update(saved)
        anchoring.reset_reported_anchors()


# ---------------------------------------------------------------------------
#  Step 7.5：提交前完整度自检（v3.1）
# ---------------------------------------------------------------------------
_REQUIRED_TWO = [
    {"q": 1, "code": "3", "required": True},
    {"q": 2, "code": "11", "required": True},
]


def test_undetected_required_question_blocks_the_submit_click() -> None:
    """平台标了必答、整份流程却没探测到它 → 不点提交，直接判失败。

    真卷上的日期题与排序题就是这个形状（探测看不见那道题）。今天的行为是白点一次
    提交、换一句平台的"第 N 题未答"，日志里只剩一条看不出原因的失败。
    """
    with stages(detect_questions=_questions(1),
                detect_platform_questions=_REQUIRED_TWO) as st:
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_FAILED
    st.find_and_click_submit.assert_not_called()


def test_gap_only_counts_questions_we_never_saw() -> None:
    """逐页探测到的题号是**并集**：第 1 页答过的题不该在最后一页被判漏答。

    这条是"跨页累积"的接线证明（单页 fixture 证不到它 —— 那里 detected_all
    恰好等于最后一页的题号）。
    """
    pages = [_questions(1), _questions(2)]
    nav = mock.Mock(side_effect=[("advanced", "下一页题号 [2]"), ("no_more", "到底了")])
    with stages(
        detect_questions=mock.Mock(side_effect=pages),
        advance_to_next_page=nav,
        detect_platform_questions=_REQUIRED_TWO,
    ):
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_SUCCESS


def test_no_platform_signal_never_blocks_submission() -> None:
    """模板不标 topic → 拿不到平台读数 → 一律照常提交。

    缺信号不是"有缺口"，拦错一次就是一单本来能交的问卷被判失败。
    """
    with stages(detect_questions=_questions(1),
                detect_platform_questions=[]) as st:
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_SUCCESS
    st.find_and_click_submit.assert_called_once()


def test_unanswered_optional_question_does_not_block() -> None:
    """探测不到但**没标必答**的题不拦：那是题型缺口，[对拍] 负责说。"""
    with stages(
        detect_questions=_questions(1),
        detect_platform_questions=[
            {"q": 1, "code": "3", "required": True},
            {"q": 2, "code": "11", "required": False},
        ],
    ) as st:
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_SUCCESS
    st.find_and_click_submit.assert_called_once()


def test_gap_scan_reads_whole_survey_while_drift_scan_reads_the_page() -> None:
    """对拍只看本页（``visible_only=True``），完整度自检必须看整卷（``False``）。"""
    seen: list[bool] = []

    def _probe(_driver: Any, _platform: Any, *, visible_only: bool = True) -> list[dict]:
        seen.append(visible_only)
        return [] if visible_only else _REQUIRED_TWO

    with stages(detect_questions=_questions(1, 2),
                detect_platform_questions=mock.Mock(side_effect=_probe)):
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_SUCCESS
    assert seen[0] is True and seen[-1] is False, f"两次扫描的分页口径不对: {seen}"


# ---------------------------------------------------------------------------
#  Step 7.5 的另一半：补漏轮（v3.1 ``--rescue-gaps``）
#
#  开关的语义是"拦下之后先把人工接进来"，所以这里锁的全是**边界**：
#    1. 默认关 —— 不打扰人工、不等待、判定与 v3.0 逐位一致（等一次就是白等）；
#    2. 只有复检真的空了才点提交，人工没补齐（含超时）时维持原判失败；
#    3. 停止优先于提交；
#    4. 无头模式下根本没有可补答的人，不等待。
#  复检本身（为什么要把已答扫描并进来当证据）见 ``test_recheck_accepts_manual_answer_evidence``。
# ---------------------------------------------------------------------------
def _rescue_run(
    hold: mock.Mock,
    *,
    core_kw: dict[str, Any] | None = None,
    driver: FakeDriver | None = None,
) -> tuple[Any, Any, FakeDriver, mock.Mock]:
    """跑一份"Q2 是必答题、而我们只探测到 Q1"的提交。

    :return: ``(结果或异常, 阶段替身, 驱动, 滚动替身)``
    """
    scroll = mock.Mock(name="scroll_question_into_view")
    d = driver or FakeDriver()
    with stages(
        detect_questions=_questions(1),
        detect_platform_questions=_REQUIRED_TWO,
        hold_for_manual_fill=hold,
        scroll_question_into_view=scroll,
    ) as st:
        try:
            result: Any = _core(d, ManualHoldLock(), **(core_kw or {}))
        except BaseException as e:      # noqa: BLE001 - 用例自己判定抛出的那一类
            result = e
    return result, st, d, scroll


def _waiting(hold_result: list[int]) -> mock.Mock:
    return mock.Mock(name="hold_for_manual_fill", return_value=hold_result)


_NEVER_WAIT = mock.Mock(name="hold_for_manual_fill",
                        side_effect=AssertionError("这条路不该等人工"))


def test_rescue_gaps_off_by_default_never_holds_for_a_human() -> None:
    """默认关：判失败、不点提交、一次都不等人工 —— 这是"行为不变"的接线证明。"""
    result, st, _driver, scroll = _rescue_run(_NEVER_WAIT)

    assert result == SUBMIT_FAILED
    scroll.assert_not_called()
    st.find_and_click_submit.assert_not_called()


def test_rescue_gaps_submits_only_after_the_recheck_comes_back_empty() -> None:
    """人工补齐（复检返回空缺口）→ 这一份照常提交，且提交前把缺口题滚进过视野。"""
    hold = _waiting([])
    result, st, _driver, scroll = _rescue_run(hold, core_kw={"rescue_gaps": True})

    assert result == SUBMIT_SUCCESS
    st.find_and_click_submit.assert_called_once()
    # 滚的是**第一道**缺口题（Q1 探测到了，缺的是 Q2）
    assert scroll.call_args.args[1] == 2
    assert hold.call_args.kwargs["lock"] is not None
    assert callable(hold.call_args.args[0]), "复检是以回调形式交给等待循环的"


def test_rescue_gaps_keeps_failed_verdict_when_the_human_fills_nothing() -> None:
    """人工没补齐（这里模拟等待到超时）→ 维持今天的判失败，不发明第三种结果。"""
    result, st, driver, _scroll = _rescue_run(
        _waiting([2]), core_kw={"rescue_gaps": True},
    )

    assert result == SUBMIT_FAILED
    st.find_and_click_submit.assert_not_called()
    assert driver.switch_to.calls == 1


def test_rescue_gaps_reports_the_ask_and_keeps_the_gap_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """原有的 describe_gap 那行不许被替换掉：补漏轮是接在它后面的，不是取代它。"""
    _result, _st, _d, _s = _rescue_run(_waiting([2]), core_kw={"rescue_gaps": True})
    out = capsys.readouterr().out

    assert "[完整度]" in out and "不点提交" in out
    assert "[补漏]" in out and "Q2" in out


def test_rescue_hold_is_interrupted_by_stop_and_never_submits() -> None:
    """停止优先于提交：等待中被要求停止 → SubmissionAborted 上抛，一次都不点提交。"""
    hold = mock.Mock(name="hold_for_manual_fill",
                     side_effect=SubmissionAborted("补漏等待期间收到停止请求"))
    result, st, _driver, _scroll = _rescue_run(hold, core_kw={"rescue_gaps": True})

    assert isinstance(result, SubmissionAborted)
    st.find_and_click_submit.assert_not_called()


def test_headless_run_skips_the_rescue_hold(capsys: pytest.CaptureFixture[str]) -> None:
    """无头里等人工 = 等一个不存在的人：直接维持判失败，并说清楚为什么。"""
    d = FakeDriver()
    d.wjx_headless = True      # driver_is_headless 读的就是 driver_factory 打的那个标记
    result, _st, _driver, _scroll = _rescue_run(
        _NEVER_WAIT, core_kw={"rescue_gaps": True}, driver=d,
    )

    assert result == SUBMIT_FAILED
    assert "无头" in capsys.readouterr().out


def test_recheck_accepts_manual_answer_evidence_we_never_detected() -> None:
    """复检的立命之处：缺口题按定义不在探测里，所以"页面上读得到值"必须算证据。

    只重跑 ``detect_questions`` 的话这条永远不会变空，等人工就等成了形式。
    """
    driver = FakeDriver()
    with stages(
        detect_questions=_questions(1),
        detect_answered_questions={2},
        detect_platform_questions=_REQUIRED_TWO,
    ):
        assert pipeline._recheck_gap(driver, {1}) == []

    with stages(
        detect_questions=_questions(1),
        detect_answered_questions=set(),
        detect_platform_questions=_REQUIRED_TWO,
    ):
        assert pipeline._recheck_gap(driver, {1}) == [2], "没人补过就不许放行"


@pytest.mark.parametrize("probe_error", [
    StaleElementReferenceException("human is clicking"),
    ValueError("bad js payload"),
], ids=["transient", "logic"])
def test_recheck_degrades_to_no_new_evidence(probe_error: BaseException) -> None:
    """复检自己出问题只是"没有新证据"：缺口原样交回，绝不在这里改判或抛出。"""
    driver = FakeDriver()
    with stages(
        detect_questions=_questions(1),
        detect_answered_questions=probe_error,
        detect_platform_questions=_REQUIRED_TWO,
    ):
        assert pipeline._recheck_gap(driver, {1}) == [2]


# ---------------------------------------------------------------------------
#  Step 7.6：提交区协议框（v3.1）—— 只提示、不拦停
#
#  探针自己的形状判据在 tests/test_consent_notice.py；这里锁的是接线的三件事：
#    1. 说完那句话**仍然照常点提交** —— 兜底判据是文案关键词，认错框的代价不能是
#       白拦一单本来能交成的问卷；
#    2. 完整度自检已经把提交拦下时，不必再去问提交区（那一次 JS 往返没有读者）；
#    3. 用**真**探针跑一遍也要一字不说："import 了没调用"与"没信号还瞎报"都在这条眼下。
# ---------------------------------------------------------------------------
def test_consent_notice_is_printed_but_submit_still_happens(
    capsys: pytest.CaptureFixture[str],
) -> None:
    notice = mock.Mock(name="consent_notice", return_value="[协议] 提交区有 1 处没勾的协议框")
    with stages(consent_notice=notice) as st:
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_SUCCESS

    assert "[协议]" in capsys.readouterr().out
    notice.assert_called_once()
    st.find_and_click_submit.assert_called_once()


def test_consent_box_is_not_asked_when_the_submit_is_already_blocked() -> None:
    """必答题缺口拦下时直接 return，不该再去扫一遍提交区。"""
    notice = mock.Mock(name="consent_notice",
                       side_effect=AssertionError("都不点提交了，问提交区做什么"))
    with stages(detect_questions=_questions(1),
                detect_platform_questions=_REQUIRED_TWO,
                consent_notice=notice):
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_FAILED


def test_real_consent_probe_stays_silent_in_the_wiring(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from src import detection

    detection.reset_consent_notice()
    try:
        with stages():
            assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_SUCCESS
        assert "[协议]" not in capsys.readouterr().out
    finally:
        detection.reset_consent_notice()


# ---------------------------------------------------------------------------
#  v3.3 逐题作答回执：把 `_answer_one_question` 的返回值收上来
#
#  这条线的全部意义是"别再让 mystery failure 出现"：回执 False 的题过去被直接丢掉，
#  于是症状推迟到点提交之后（平台弹"第 N 题未答"）。锁的三件事：
#    1. 收到、并报出来（题号要对得上）；
#    2. 报归报，**判定与提交行为一个字都不变** —— 回执可能只是没读到；
#    3. 只有开了 ``--rescue-gaps`` 才把这类题一起交给人工等，等不到也照提交。
#  续填跳过的题不算回执失败（我们根本没动手）。
# ---------------------------------------------------------------------------
def test_per_question_receipt_is_collected_and_named(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Q2 的作答返回 False → 点提交之前就把 Q2 报出来（而不是等平台说）。"""
    answer = mock.Mock(name="_answer_one_question", side_effect=[True, False])
    with stages(detect_questions=_questions(1, 2), _answer_one_question=answer):
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_SUCCESS

    out = capsys.readouterr().out
    assert "[作答回执]" in out and "Q2" in out, out
    assert "Q1" not in out.split("[作答回执]")[1].splitlines()[0], "落上的那题不许被牵连进来"


def test_resume_skipped_questions_are_not_reported_as_receipt_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """续填跳过的题压根没动手，不许出现在回执里。"""
    answer = mock.Mock(name="_answer_one_question", return_value=True)
    with stages(
        detect_questions=_questions(1, 2),
        detect_answered_questions={2},
        _answer_one_question=answer,
    ):
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_SUCCESS

    assert "[作答回执]" not in capsys.readouterr().out
    assert _answered_nums(answer) == [1], "已答题应当被跳过而不是重答"


def test_receipt_failures_never_block_the_submit_by_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """默认关：一行说明 + 照常点提交。拦错一次 = 一单本来能交的问卷被判失败。"""
    answer = mock.Mock(name="_answer_one_question", return_value=False)
    with stages(detect_questions=_questions(1), _answer_one_question=answer) as st:
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_SUCCESS

    assert "[作答回执]" in capsys.readouterr().out
    st.find_and_click_submit.assert_called_once()


def test_clean_run_says_nothing_about_receipts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    answer = mock.Mock(name="_answer_one_question", return_value=True)
    with stages(detect_questions=_questions(1), _answer_one_question=answer):
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_SUCCESS
    assert "[作答回执]" not in capsys.readouterr().out


def test_rescue_gaps_waits_for_receipt_failures_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """开了补漏轮：没有"整题没探测到"的缺口，也要为回执失败的题等人工。"""
    answer = mock.Mock(name="_answer_one_question", return_value=False)
    hold = mock.Mock(name="hold_for_manual_fill", return_value=[1])
    scroll = mock.Mock(name="scroll_question_into_view")
    with stages(
        detect_questions=_questions(1),
        _answer_one_question=answer,
        hold_for_manual_fill=hold,
        scroll_question_into_view=scroll,
    ) as st:
        assert _core(FakeDriver(), ManualHoldLock(), rescue_gaps=True,
                     stop_check=lambda: False) == SUBMIT_SUCCESS

    # 等的是那道题（滚进视野的就是它），停止谓词照旧透传（等着期间点"停止"不许提交）
    assert scroll.call_args.args[1] == 1
    assert hold.call_args.kwargs["stop_check"] is not None
    assert st.find_and_click_submit.call_count == 1
    out = capsys.readouterr().out
    assert "[补漏]" in out and "照提交" in out, out


def test_rescue_gaps_submits_quietly_once_the_human_discharges_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """人工补齐（复检空了）→ 提交，且不再补一句"照提交"的解释。"""
    answer = mock.Mock(name="_answer_one_question", return_value=False)
    with stages(
        detect_questions=_questions(1),
        _answer_one_question=answer,
        hold_for_manual_fill=mock.Mock(name="hold_for_manual_fill", return_value=[]),
    ):
        assert _core(FakeDriver(), ManualHoldLock(),
                     rescue_gaps=True) == SUBMIT_SUCCESS
    assert "照提交" not in capsys.readouterr().out


def test_recheck_discharges_receipt_failures_by_reading_the_page() -> None:
    """复检对第二类的消解条件只有一个：已答扫描现在读得到那格的值。"""
    base: dict[str, Any] = {
        "detect_questions": _questions(1, 2),
        "detect_platform_questions": [],
    }
    with stages(**{**base, "detect_answered_questions": {2}}):
        assert pipeline._recheck_gap(FakeDriver(), {1, 2}, {2}) == []
    with stages(**{**base, "detect_answered_questions": set()}):
        assert pipeline._recheck_gap(FakeDriver(), {1, 2}, {2}) == [2]


def test_recheck_receipt_channel_survives_a_failed_answered_scan() -> None:
    """已答扫描抖一下 → 按"没有新证据"处理：第二类继续挂着等，不许在这里放行。"""
    with stages(
        detect_questions=_questions(1, 2),
        detect_answered_questions=StaleElementReferenceException("page moved"),
        detect_platform_questions=[],
    ):
        assert pipeline._recheck_gap(FakeDriver(), {1, 2}, {2}) == [2]


# ---------------------------------------------------------------------------
#  Step 8 的另一半：--manual-submit（v3.3）—— 只把"点这一下"交给人
#
#  分叉点必须只有一处：走人工路径时 ``find_and_click_submit`` 一次都不许被调用
#  （那是"替人交卷"，正是这个开关要消灭的动作），而提交后的三态判定、必填诊断、
#  上下文复原必须**完全共用** —— 否则人提交的那一份与自动提交的那一份在历史里不可比。
# ---------------------------------------------------------------------------
def test_manual_submit_never_clicks_the_submit_button() -> None:
    wait = mock.Mock(name="wait_for_manual_submit", return_value=SUBMIT_SUCCESS)
    with stages(wait_for_manual_submit=wait) as st:
        assert _core(FakeDriver(), ManualHoldLock(),
                     manual_submit=True) == SUBMIT_SUCCESS

    st.find_and_click_submit.assert_not_called()
    wait.assert_called_once()


def test_manual_submit_timeout_is_reported_as_failure_without_a_click() -> None:
    """等到超时 = 这一份没交出去 → 判失败，而且不去"补一次自动提交"。"""
    wait = mock.Mock(name="wait_for_manual_submit", return_value=SUBMIT_FAILED)
    with stages(wait_for_manual_submit=wait) as st:
        assert _core(FakeDriver(), ManualHoldLock(),
                     manual_submit=True) == SUBMIT_FAILED
    st.find_and_click_submit.assert_not_called()


def test_manual_submit_off_does_not_touch_the_wait_path() -> None:
    """默认关：连问都不问（一次 execute_script 都不该多），行为与此前逐位一致。"""
    wait = mock.Mock(name="wait_for_manual_submit",
                     side_effect=AssertionError("没开开关不该等人工"))
    with stages(wait_for_manual_submit=wait) as st:
        assert _core(FakeDriver(), ManualHoldLock()) == SUBMIT_SUCCESS
    st.find_and_click_submit.assert_called_once()


def test_manual_submit_forwards_the_stop_predicate() -> None:
    """停止谓词必须传进等待循环 —— 否则"停止"要等满 180s 才生效。"""
    wait = mock.Mock(name="wait_for_manual_submit", return_value=SUBMIT_SUCCESS)
    chk = lambda: False  # noqa: E731
    with stages(wait_for_manual_submit=wait):
        _core(FakeDriver(), ManualHoldLock(), stop_check=chk, manual_submit=True)
    assert wait.call_args.kwargs["stop_check"] is chk
