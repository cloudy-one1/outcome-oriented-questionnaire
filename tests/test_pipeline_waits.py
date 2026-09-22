"""`_wait_for_questions` 与人工介入锁的契约测试（v2.6 新增）。

背景：ManualHoldLock 的 docstring 一直承诺"pipeline 中任何超时逻辑看到
is_holding 就不该判失败"，但生产代码里 **没有任何调用点实现它**
（旧套件之外 grep is_holding / wait_until_released 只有测试自己）。
README 也把这条当成产品特性写进了「智能验证码检测」一节。

v2.6 把接线点放在 _wait_for_questions：holding 期间不计入超时预算。
本文件锁住这个行为，同时覆盖 pipeline_stages/ 此前零测试的那块。
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.exceptions import SubmissionAborted  # noqa: E402
from src.pipeline_stages.question_stage import _wait_for_questions  # noqa: E402
from src.utils import ManualHoldLock  # noqa: E402


class ProbeDriver:
    """execute_script 在 `appear_after` 秒之后才开始返回 True。"""

    def __init__(self, appear_after: float) -> None:
        self._t0 = time.perf_counter()
        self._appear_after = appear_after
        self.calls = 0

    def execute_script(self, _script, *_args, **_kw):
        self.calls += 1
        return (time.perf_counter() - self._t0) >= self._appear_after


# ---------------------------------------------------------------------------
def test_returns_true_once_controls_appear() -> None:
    d = ProbeDriver(appear_after=0.3)
    assert _wait_for_questions(d, timeout=5.0) is True
    assert d.calls >= 2, "应是轮询而非一次性判断"


def test_times_out_without_lock() -> None:
    d = ProbeDriver(appear_after=10.0)
    t0 = time.perf_counter()
    assert _wait_for_questions(d, timeout=0.6) is False
    assert time.perf_counter() - t0 < 2.0, "超时预算本身失效会变成无限等待"


def test_holding_lock_suspends_the_timeout_budget() -> None:
    """人在拉验证码时，等待计时必须暂停。

    旧行为：WebDriverWait(driver, 15).until(...) 走墙钟，用户手还没滑完
    就超时 → 本轮 SUBMIT_FAILED → 人工介入完全白做。
    """
    lock = ManualHoldLock()
    d = ProbeDriver(appear_after=1.6)

    # 0.2s 时进入 holding（模拟验证码弹出），2.0s 时释放
    def hold_window() -> None:
        time.sleep(0.2)
        lock.acquire()
        time.sleep(1.8)
        lock.release()

    threading.Thread(target=hold_window, daemon=True).start()

    # 预算只有 0.8s，比控件出现所需的 1.6s 短：
    # 不暂停计时必然 False；暂停了就能等到。
    assert _wait_for_questions(d, timeout=0.8, hold_lock=lock) is True


def test_without_lock_arg_the_same_scenario_fails() -> None:
    """对照组：证明上一条通过是因为锁生效，而不是预算其实够用。"""
    d = ProbeDriver(appear_after=1.6)
    assert _wait_for_questions(d, timeout=0.8) is False


def test_holding_forever_still_returns_when_control_appears() -> None:
    """锁一直不放也不能变成死等：控件出现就该返回 True，
    而释放后的正常超时判断仍然生效。"""
    lock = ManualHoldLock()
    lock.acquire()
    try:
        d = ProbeDriver(appear_after=0.4)
        assert _wait_for_questions(d, timeout=0.2, hold_lock=lock) is True
    finally:
        lock.release()


def test_released_lock_does_not_extend_budget() -> None:
    """非 holding 状态下，budget 正常消耗 —— 别把"暂停"做成"永不超时"。"""
    lock = ManualHoldLock()
    d = ProbeDriver(appear_after=3.0)
    t0 = time.perf_counter()
    assert _wait_for_questions(d, timeout=0.5, hold_lock=lock) is False
    assert time.perf_counter() - t0 < 2.0


# ---------------------------------------------------------------------------
#  v3.0：轮内停止谓词
# ---------------------------------------------------------------------------
def test_stop_check_short_circuits_the_wait() -> None:
    """用户已经点了停止 → 不再轮询、不再耗预算，直接抛 SubmissionAborted。

    这条是「点了停止仍要等完整份问卷」的上半个洞：等题目渲染最长
    QUESTION_DETECT_TIMEOUT 秒，期间停止按钮完全没反应。
    刻意断言 ``calls == 0`` —— 停止必须在第一次探测**之前**就被问到，
    否则"每片轮询问一次"的接线就是假的。
    """
    d = ProbeDriver(appear_after=999.0)
    with pytest.raises(SubmissionAborted):
        _wait_for_questions(d, timeout=999.0, stop_check=lambda: True)
    assert d.calls == 0


def test_stop_check_absent_keeps_the_old_timeout_path() -> None:
    """不传 stop_check（CLI 之外的老调用点）→ 行为与 v2.8 逐位一致：返回 False 而非抛。"""
    d = ProbeDriver(appear_after=999.0)
    assert _wait_for_questions(d, timeout=0.3, stop_check=None) is False
