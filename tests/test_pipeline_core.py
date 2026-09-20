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

    st._check_verification_with_lock.assert_called_once_with(driver, lock)
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
        driver, QUESTION_DETECT_TIMEOUT, hold_lock=lock,
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
