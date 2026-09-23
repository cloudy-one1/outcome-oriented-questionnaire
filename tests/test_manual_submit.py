"""人工提交等待（``src/pipeline_stages/manual_submit.py``）的离线契约测试。

这一步只做一件事：**把最后那一下点击交给人**，所以用例全部围绕四件事组织：

  1. 人点了（URL 变了，或页面出现强成功文案）→ 返回 ``SUBMIT_SUCCESS``；
  2. 没人点 → 超时返回 ``SUBMIT_FAILED``，而且话要说清"这一份没有交出去"——
     它不是"交了没确认"，把它算成 UNKNOWN 会谎报平台上有一条记录；
  3. 等待期间人工介入锁处于 holding，且**无论走哪个出口都必须释放**
     （锁不还是整批挂死，与补漏轮/验证码同一条红线）；被要求停止时抛
     ``SubmissionAborted``，这一份既不计成功也不计失败；
  4. 只滚不点：本模块没有任何一条路径能替人按下提交键。

时间用假 ``time`` 模块替换（``manual_submit.time``），于是一场 180 秒的等待
在几毫秒内跑完，"睡了几次、每次多长"是断言出来的而不是等出来的。
成功信号的判据**沿用** ``_wait_until_submit_effect`` 那个脚本（同源于
``interactions/_scripts.submit_success_detect_script``）—— 人提交的那一份必须和
自动提交的那一份用同一把尺子量，否则历史里的成功率不可比。
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest
from selenium.common.exceptions import StaleElementReferenceException

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.exceptions import SubmissionAborted  # noqa: E402
from src.interactions.submit import SELECTORS as SUBMIT_SELECTORS  # noqa: E402
from src.models import SUBMIT_FAILED, SUBMIT_SUCCESS  # noqa: E402
from src.pipeline_stages import manual_submit  # noqa: E402
from src.utils import ManualHoldLock  # noqa: E402


class FakeTime:
    """替换 ``manual_submit.time``：记录 sleep，墙钟由用例自己推。"""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(manual_submit, "time", fake)
    return fake


class FakeDriver:
    """假 WebDriver：只给 ``current_url`` 与 ``execute_script`` 两条面。

    :param urls:      每次读 ``current_url`` 依序返回（用完重复最后一个）
    :param success:   成功文案脚本的读数 —— 布尔直接用，序列则依序取
    :param url_error: 读 URL 时抛的异常（模拟提交瞬间页面正在跳转）
    """

    def __init__(
        self,
        *,
        urls: list[str] | None = None,
        success: Any = False,
        url_error: BaseException | None = None,
    ) -> None:
        self._urls = list(urls or ["https://www.wjx.cn/vm/x.aspx#0"])
        self._url_reads = 0
        self._success = success
        self._success_reads = 0
        self._url_error = url_error
        self.scripts: list[tuple[str, tuple[Any, ...]]] = []

    @property
    def current_url(self) -> str:
        if self._url_error is not None:
            raise self._url_error
        value = self._urls[min(self._url_reads, len(self._urls) - 1)]
        self._url_reads += 1
        return value

    def execute_script(self, script: str, *args: Any) -> Any:
        self.scripts.append((script, args))
        if "innerText" in script or "submit-succ" in script:
            if isinstance(self._success, (list, tuple)):
                value = self._success[min(self._success_reads, len(self._success) - 1)]
                self._success_reads += 1
                return value
            return self._success
        return True


def _run(
    driver: Any,
    *,
    lock: ManualHoldLock | None = None,
    stop_check: Any = None,
    timeout: float = manual_submit.MANUAL_SUBMIT_TIMEOUT,
) -> Any:
    return manual_submit.wait_for_manual_submit(
        driver, lock or ManualHoldLock(), stop_check=stop_check, timeout=timeout,
    )


# ---------------------------------------------------------------------------
#  出口 1：人点了
# ---------------------------------------------------------------------------
def test_returns_success_when_the_page_url_changes(clock: FakeTime) -> None:
    driver = FakeDriver(urls=["https://x.aspx#0", "https://x.aspx#0",
                              "https://www.wjx.cn/complete.aspx"])
    assert _run(driver) == SUBMIT_SUCCESS


def test_returns_success_on_the_same_strong_text_the_auto_path_uses(clock: FakeTime) -> None:
    """URL 不变的 AJAX 提交也要认得，且用的就是那个共用脚本（不另立判据）。"""
    driver = FakeDriver(success=True)
    assert _run(driver) == SUBMIT_SUCCESS
    success_scripts = [s for s, _a in driver.scripts if "innerText" in s]
    from src.interactions._scripts import submit_success_detect_script
    assert success_scripts and all(
        s == submit_success_detect_script() for s in success_scripts
    ), "成功判据被换了一份，人提交的份就与自动提交的份不可比了"


def test_lock_is_released_after_a_successful_handoff(clock: FakeTime) -> None:
    lock = ManualHoldLock()
    _run(FakeDriver(urls=["https://x#0", "https://y"]), lock=lock)
    assert not lock.is_holding


def test_lock_is_holding_while_we_wait(clock: FakeTime) -> None:
    """等待期间锁必须处于 holding —— 外部任何超时判断都靠它让路。"""
    lock = ManualHoldLock()
    seen: list[bool] = []

    class _Spy(FakeDriver):
        def execute_script(self, script: str, *args: Any) -> Any:
            if "innerText" in script:
                seen.append(lock.is_holding)
            return super().execute_script(script, *args)

    # 前三次读 URL 都停在原页（第 1 次是基线），所以第一次判断必然落到成功文案脚本上
    _run(_Spy(urls=["https://x#0", "https://x#0", "https://x#0", "https://y"]),
         lock=lock)
    assert seen and all(seen), f"等待期间锁没处于 holding: {seen}"


# ---------------------------------------------------------------------------
#  出口 2：没人点
# ---------------------------------------------------------------------------
def test_timeout_is_a_failure_and_says_nothing_was_submitted(
    clock: FakeTime, capsys: pytest.CaptureFixture[str],
) -> None:
    result = _run(FakeDriver(success=False), timeout=5.0)
    assert result == SUBMIT_FAILED
    out = capsys.readouterr().out
    assert "没有交出去" in out, "把它说成'没确认'会让人以为平台上多了一条记录"


def test_timeout_budget_is_consumed_in_slices(clock: FakeTime) -> None:
    """切片睡：一发 sleep(180) 期间"停止"根本来不及生效，而取消正是等待期唯一会做的事。"""
    _run(FakeDriver(success=False), timeout=5.0)
    assert clock.slept and all(s <= manual_submit._POLL for s in clock.slept)


def test_progress_line_is_not_printed_every_second(
    clock: FakeTime, capsys: pytest.CaptureFixture[str],
) -> None:
    _run(FakeDriver(success=False), timeout=95.0)
    out = capsys.readouterr().out
    assert out.count("仍在等你点提交") == 3, f"进度行节奏不对: {out}"


# ---------------------------------------------------------------------------
#  出口 3：被要求停止
# ---------------------------------------------------------------------------
def test_stop_during_the_wait_aborts_without_submitting(
    clock: FakeTime, capsys: pytest.CaptureFixture[str],
) -> None:
    lock = ManualHoldLock()
    with pytest.raises(SubmissionAborted):
        _run(FakeDriver(success=False), lock=lock, stop_check=lambda: True)
    assert not lock.is_holding, "锁不还是整批挂死"
    assert "不提交" in capsys.readouterr().out


def test_stop_is_honoured_midway_not_only_at_entry(clock: FakeTime) -> None:
    """谓词是每片问一次的：等到第 3 片才按停止也要能出来。"""
    calls = {"n": 0}

    def _stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    with pytest.raises(SubmissionAborted):
        _run(FakeDriver(success=False), stop_check=_stop, timeout=600.0)


# ---------------------------------------------------------------------------
#  降级与边界
# ---------------------------------------------------------------------------
def test_unreadable_baseline_url_does_not_fake_a_change(clock: FakeTime) -> None:
    """基线 URL 拿不到时不许拿 "" 兜底 —— 那会让任何 URL 都算"变化了"。

    错误页也算变化，于是把失败判成成功，是这条判据最危险的偏侧。
    """
    driver = FakeDriver(success=False, url_error=StaleElementReferenceException("nav"))
    assert _run(driver, timeout=3.0) == SUBMIT_FAILED


def test_transient_script_failure_keeps_polling(clock: FakeTime) -> None:
    """DOM 抖一下只是这一轮读不到：继续轮询，不许在这里就判失败。"""
    polls = {"n": 0}

    class _Flaky(FakeDriver):
        def execute_script(self, script: str, *args: Any) -> Any:
            if "innerText" in script:
                polls["n"] += 1
                if polls["n"] == 1:
                    raise StaleElementReferenceException("page moved")
                return True
            return super().execute_script(script, *args)

    assert _run(_Flaky(
        urls=["https://x#0", "https://x#0", "https://done"]
    )) == SUBMIT_SUCCESS


def test_submit_button_is_scrolled_into_view_but_never_clicked(clock: FakeTime) -> None:
    """本模块只许把按钮送到眼前，一次都不许点它。"""
    driver = FakeDriver(success=True)
    _run(driver)
    scroll_scripts = [s for s, _a in driver.scripts if "scrollIntoView" in s]
    assert scroll_scripts, "没滚 —— 人得自己在长页面里找提交键"
    for script, _args in driver.scripts:
        assert ".click()" not in script, "人工提交里出现了程序点击"


def test_scroll_uses_the_shared_submit_selector_list(clock: FakeTime) -> None:
    """选择器沿用 ``interactions.submit.SELECTORS``，不在这里抄第二份。"""
    driver = FakeDriver(success=True)
    _run(driver)
    scrolled = [args for script, args in driver.scripts if "scrollIntoView" in script]
    assert scrolled[0][0] == list(SUBMIT_SELECTORS)




def test_missing_submit_button_is_not_an_error(clock: FakeTime) -> None:
    """认不到按钮只是少一步贴心：等待照旧，人自己看得到提交键。"""

    class _NoButton(FakeDriver):
        def execute_script(self, script: str, *args: Any) -> Any:
            if "scrollIntoView" in script:
                raise StaleElementReferenceException("no button")
            return super().execute_script(script, *args)

    assert _run(_NoButton(
        success=False, urls=["https://x#0"]
    ), timeout=2.0) == SUBMIT_FAILED
