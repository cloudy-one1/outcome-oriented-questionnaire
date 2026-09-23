"""补漏轮等待阶段（``src/pipeline_stages/gap_rescue.py``）的离线契约测试。

这一层只干一件事：**在有限的时间里等一个在场的人**，所以用例全部围绕
"什么时候必须醒过来"组织 —— 补齐了立刻走、超时了维持原判、被要求停止时抛出去、
以及等待期间人工介入锁一定处于 holding（外部超时逻辑靠它让路）、返回时一定释放
（锁卡住 = 整批任务挂死，与验证码那边同一条红线）。

打桩方式与 ``tests/test_verification_flow.py`` 一致：假 ``time`` 模块替换掉
``gap_rescue.time``，于是"睡了多久、睡了几次"是断言出来的而不是等出来的。
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
from src.platforms import WJX_QUESTION_ANCHOR_SELECTORS  # noqa: E402
from src.pipeline_stages import gap_rescue  # noqa: E402
from src.utils import ManualHoldLock  # noqa: E402


class FakeTime:
    """替换 ``gap_rescue.time``：记录 sleep 但不消耗墙钟。"""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(gap_rescue, "time", fake)
    return fake


def _recheck_seq(*gaps: list[int]):
    """按调用次序返回缺口的复检替身；用完后一直返回最后一个（模拟"就这样了"）。"""
    seq = list(gaps)

    def _recheck() -> list[int]:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return _recheck


# ---------------------------------------------------------------------------
#  出口 1：人工补齐 → 立刻结束等待
# ---------------------------------------------------------------------------
def test_returns_as_soon_as_the_gap_closes(clock: FakeTime) -> None:
    lock = ManualHoldLock()
    recheck = _recheck_seq([2], [2], [])

    assert gap_rescue.hold_for_manual_fill(
        recheck, lock=lock, timeout=60.0,
    ) == []
    assert clock.slept == [gap_rescue._HOLD_POLL] * 3, "补齐即走，一秒都不多等"
    assert lock.is_holding is False, "返回后必须释放，否则整批挂死"


def test_lock_is_holding_while_the_human_is_typing(clock: FakeTime) -> None:
    """等待期间锁必须处于 holding —— 这是外部超时逻辑让路的唯一依据。"""
    lock = ManualHoldLock()
    seen: list[bool] = []

    def recheck() -> list[int]:
        seen.append(lock.is_holding)
        return [2] if len(seen) < 3 else []

    assert gap_rescue.hold_for_manual_fill(recheck, lock=lock, timeout=60.0) == []
    assert seen == [True, True, True]
    assert lock.is_holding is False


# ---------------------------------------------------------------------------
#  出口 2：等不到 → 原样交回缺口（调用方维持判失败）
# ---------------------------------------------------------------------------
def test_timeout_returns_the_last_gap(clock: FakeTime,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    lock = ManualHoldLock()
    recheck = _recheck_seq([2, 9])

    assert gap_rescue.hold_for_manual_fill(
        recheck, lock=lock, timeout=6.0,
    ) == [2, 9]
    assert len(clock.slept) == 3, "6s / 每片 2s → 三片之后收工"
    out = capsys.readouterr().out
    assert "仍然读不到答案" in out and "Q2、Q9" in out
    # 这里不许宣布判定：调用方手里两类题的处置不同（整题没探测到的拦停，
    # 作答回执说没落上的照提交），判定归 pipeline 那两句各自说自己的
    assert "维持判失败" not in out, out
    assert lock.is_holding is False


def test_progress_line_is_not_printed_every_slice(
    clock: FakeTime, capsys: pytest.CaptureFixture[str],
) -> None:
    """进度行按节奏打：每片都打就把运行信息埋掉了（这份日志还要给人看别的）。"""
    gap_rescue.hold_for_manual_fill(
        _recheck_seq([2]), lock=ManualHoldLock(), timeout=45.0,
    )
    out = capsys.readouterr().out
    assert out.count("仍在等人工补答") == 2, f"45s 里该打两条进度：{out}"


# ---------------------------------------------------------------------------
#  出口 3：被要求停止 → 抛出去，且锁一定释放
# ---------------------------------------------------------------------------
def test_stop_during_hold_aborts_and_releases_the_lock(clock: FakeTime) -> None:
    lock = ManualHoldLock()
    with pytest.raises(SubmissionAborted):
        gap_rescue.hold_for_manual_fill(
            _recheck_seq([2]), lock=lock, stop_check=lambda: True, timeout=60.0,
        )
    assert lock.is_holding is False
    assert clock.slept == [], "停止信号在睡之前问，不该再等一片"


def test_stop_is_polled_but_not_when_there_is_no_caller(clock: FakeTime) -> None:
    """不传 stop_check（CLI 默认）时行为不变：照样等到补齐或超时。"""
    assert gap_rescue.hold_for_manual_fill(
        _recheck_seq([]), lock=ManualHoldLock(), timeout=10.0,
    ) == []
    assert clock.slept == [gap_rescue._HOLD_POLL]


# ---------------------------------------------------------------------------
#  复检抛异常：等待不许把它咽下去（那是调用方的降级判定），但锁要还
# ---------------------------------------------------------------------------
def test_recheck_error_propagates_and_releases_the_lock(clock: FakeTime) -> None:
    lock = ManualHoldLock()

    def _boom() -> list[int]:
        raise ValueError("复检读到一个不像话的读数")

    with pytest.raises(ValueError, match="不像话"):
        gap_rescue.hold_for_manual_fill(_boom, lock=lock, timeout=60.0)
    assert lock.is_holding is False


# ---------------------------------------------------------------------------
#  滚动：把人工该补的那道题送到眼前，认不到容器就算了
# ---------------------------------------------------------------------------
class ScrollDriver:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._error = error

    def execute_script(self, script: str, *args: Any) -> Any:
        self.calls.append((script, args))
        if self._error is not None:
            raise self._error
        return True


def test_scroll_sends_the_numbered_candidates_not_a_interpolated_string() -> None:
    """题号当**参数**传，不拼进 JS 源码：选择器候选与 detection 读平台结构同一写法。"""
    d = ScrollDriver()
    gap_rescue.scroll_question_into_view(d, 7)

    script, args = d.calls[0]
    assert len(d.calls) == 1
    assert "scrollIntoView" in script
    assert "{num}" not in script, "占位符只存在于 Python 侧的候选表里"
    assert args[0] == [s.replace("{num}", "7") for s in WJX_QUESTION_ANCHOR_SELECTORS]
    assert "divquestion7" in args[0][0]


@pytest.mark.parametrize("error", [
    StaleElementReferenceException("页面正被人点着"),
    ValueError("JS 返回了不像话的东西"),
], ids=["transient", "logic"])
def test_scroll_failure_never_breaks_the_wait(error: BaseException) -> None:
    """滚不动不是失败：人工自己能在页面上找到那道题，我们只是顺手送到眼前。"""
    gap_rescue.scroll_question_into_view(ScrollDriver(error=error), 3)


def test_scroll_still_escapes_a_stop_signal() -> None:
    """Ctrl+C 不能被"只是滚一下"这种小事吃掉（异常分层的第一条红线）。"""
    with pytest.raises(KeyboardInterrupt):
        gap_rescue.scroll_question_into_view(
            ScrollDriver(error=KeyboardInterrupt()), 3,
        )


# ---------------------------------------------------------------------------
#  默认上限：写死在模块常量上，README/CLI 帮助里那句"最长等 N 秒"跟着它
# ---------------------------------------------------------------------------
def test_default_timeout_is_generous_but_bounded() -> None:
    """几分钟（不是无限）：无上限的等待就是"整批卡在这份问卷上"的另一种写法。"""
    assert 60.0 <= gap_rescue.GAP_HOLD_TIMEOUT <= 600.0
