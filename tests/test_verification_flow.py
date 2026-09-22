"""``src/verification.py`` 的离线契约测试（v2.6 新增）。

背景：README 的「已知缺口」表把这个模块标成 34% 覆盖、备注"人工介入路径难以自动化"。
这条备注是错的 —— 整个模块只依赖 ``driver.execute_script`` / ``driver.refresh`` /
``time.sleep`` / ``ctypes``，用一个假 driver + 替换模块属性就能全部驱动，不需要浏览器。

为什么值得补：它是唯一决定「要不要麻烦真人」的模块，也是全流水线里唯一持有
ManualHoldLock 的地方。这里回归的后果不是"测试不绿"，而是：

  * 检测函数误判 → 无人值守时凭空弹人工介入，或该介入时直接跳过；
  * 锁没在 finally 里释放 → 外部 pipeline 永久挂起（test_pipeline_waits 依赖
    is_holding 暂停超时预算，锁不放就是整批任务卡死）；
  * force_focus 退回同步模态框 → 没人点确定的 MessageBoxW 永远打断不了
    （Ctrl+C 吃不到、GUI 停止按钮只是置位），一整批提交原地蒸发。

安全底线（本文件每个测试都被 autouse fixture 罩住）：
  1. ``verification.ctypes`` / ``wintypes`` 全程是假的 → 真实 Win32 API 一次都不会被调；
  2. ``verification.threading`` 默认换成"只记录、不起线程"的假命名空间 → 任何测试
     意外走到 force_focus 都不会真的产生弹窗线程；
  3. ``verification.force_focus`` 默认换成 no-op 记录器（等待循环每轮都会调它）；
  4. ``verification.time`` 按需换成假时钟 → 每轮 2 秒的 sleep 不消耗墙钟。

2 和 4 的做法是**替换模块属性**而不是 patch 标准库本身：time/threading 全局共享，
patch 它们会波及同进程内其它并发测试和真实线程。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import verification  # noqa: E402
from src.exceptions import SubmissionAborted  # noqa: E402
from src.utils import ManualHoldLock  # noqa: E402

# 本文件的测试默认把 force_focus 换成 no-op 记录器（见 focus_calls fixture）。
# 直测 force_focus 本身时显式用导入期抓到的原始函数。
_REAL_FORCE_FOCUS = verification.force_focus

_WM_CLOSE = verification._WM_CLOSE
_TITLE = verification._FOCUS_TITLE


# ---------------------------------------------------------------------------
#  测试替身
# ---------------------------------------------------------------------------
def classify(script: str) -> str:
    """把 verification.py 里那四段 JS 归一成可读标签（靠各自的独有选择器区分）。"""
    if "layui-layer-close" in script:
        return "close"
    if "#antispam" in script:
        return "probe-dom"
    if "searchShadow" in script:
        return "probe-shadow"
    if "location.href" in script:
        return "probe-body"
    return "other"


class FakeDriver:
    """只实现 verification.py 用到的 execute_script / refresh 的假 driver。

    verdicts: {标签: bool 或 list[bool]}。list 按"该标签第几次被问"取值，
              用来表达"用户拉了 N 轮之后验证码才消失"。
    raises  : 哪些标签抛异常（含 "refresh"），用来验证各处 try/except 的容忍度。
    observer: 每次 execute_script 前被调用 (标签,)，用于在轮询中途读锁状态。
    """

    def __init__(
        self,
        verdicts: dict[str, Any] | None = None,
        raises: tuple[str, ...] = (),
        observer: Any = None,
    ) -> None:
        self.verdicts: dict[str, Any] = dict(verdicts or {})
        self.raises = set(raises)
        self.observer = observer
        self.scripts: list[str] = []
        self.refreshes = 0
        self._seen: dict[str, int] = {}

    def execute_script(self, script: str, *_args: Any, **_kw: Any):
        tag = classify(script)
        self.scripts.append(tag)
        if self.observer is not None:
            self.observer(tag)
        if tag in self.raises:
            raise RuntimeError(f"模拟 WebDriver 异常：{tag}")
        return self._verdict(tag)

    def refresh(self) -> None:
        self.refreshes += 1
        if "refresh" in self.raises:
            raise RuntimeError("模拟 refresh 失败")

    def _verdict(self, tag: str) -> bool:
        value = self.verdicts.get(tag, False)
        if isinstance(value, (list, tuple)):
            idx = self._seen.get(tag, 0)
            self._seen[tag] = idx + 1
            return bool(value[min(idx, len(value) - 1)])
        return bool(value)

    def count(self, tag: str) -> int:
        return self.scripts.count(tag)


class NoScriptDriver:
    """连 execute_script 都没有的对象：探测本身会抛 AttributeError。"""


class FakeTime:
    """替换 verification.time：记录 sleep 但不消耗墙钟。"""

    def __init__(self, raises: Exception | None = None) -> None:
        self.slept: list[float] = []
        self.raises = raises

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.raises is not None:
            raise self.raises


class _RecordedThread:
    def __init__(self, target, daemon: bool | None, real_factory: Any) -> None:
        self.target = target
        self.daemon = daemon
        self.started = False
        self._real_factory = real_factory
        self._handle: Any = None

    def start(self) -> None:
        self.started = True
        if self._real_factory is not None:
            self._handle = self._real_factory(target=self.target, daemon=True)
            self._handle.start()

    def join(self, timeout: float | None = None) -> None:
        if self._handle is not None:
            self._handle.join(timeout)


class _RecordedTimer:
    """只记录、不启动：绝不让自动关闭计时器真的活到 teardown 之后。"""

    def __init__(self, delay: float, fn: Any) -> None:
        self.delay = delay
        self.fn = fn
        self.daemon = False
        self.started = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        pass


class FakeThreading:
    """替换 verification.threading。

    run_threads=False（默认）：Thread/Timer 只被记录，测试自己决定何时同步调用 target。
    于是"弹窗线程"在本文件里基本不存在，也就没有 teardown 后误触真 MessageBoxW 的窗口。
    """

    def __init__(self, run_threads: bool = False) -> None:
        self.threads: list[_RecordedThread] = []
        self.timers: list[_RecordedTimer] = []
        self._real_thread = threading.Thread if run_threads else None

    def Thread(self, target: Any = None, daemon: bool | None = None, **_kw: Any):
        thread = _RecordedThread(target, daemon, self._real_thread)
        self.threads.append(thread)
        return thread

    def Timer(self, interval: float, fn: Any, *_args: Any, **_kw: Any):
        timer = _RecordedTimer(interval, fn)
        self.timers.append(timer)
        return timer

    def run_thread(self, index: int = 0) -> Any:
        """同步跑一次弹窗体（真线程版没有意义，仅供记录模式调用）。"""
        assert self._real_thread is None, "真线程模式下不要手动跑 target"
        return self.threads[index].target()

    @property
    def only_thread(self) -> _RecordedThread:
        assert len(self.threads) == 1, f"预期恰好 1 个线程，实际 {len(self.threads)}"
        return self.threads[0]

    @property
    def only_timer(self) -> _RecordedTimer:
        assert len(self.timers) == 1, f"预期恰好 1 个计时器，实际 {len(self.timers)}"
        return self.timers[0]


class FakeUser32:
    """ctypes.windll.user32 的替身，记录全部调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.find_window_result = 0
        self.raises: set[str] = set()
        self.messagebox_hook: Any = None

    def _maybe_raise(self, name: str) -> None:
        if name in self.raises:
            raise OSError(f"模拟 Win32 调用失败：{name}")

    def MessageBoxW(self, *args):
        self.calls.append(("MessageBoxW", args))
        self._maybe_raise("MessageBoxW")
        if self.messagebox_hook is not None:
            self.messagebox_hook(*args)
        return 1

    def FindWindowW(self, *args):
        self.calls.append(("FindWindowW", args))
        self._maybe_raise("FindWindowW")
        return self.find_window_result

    def PostMessageW(self, *args):
        self.calls.append(("PostMessageW", args))
        self._maybe_raise("PostMessageW")
        return 1

    def names(self) -> list[str]:
        return [name for name, _args in self.calls]

    def args_of(self, name: str) -> tuple:
        for called, args in self.calls:
            if called == name:
                return args
        raise AssertionError(f"{name} 未被调用，实际调用序列：{self.names()}")


class FakeWinTypes:
    """wintypes 替身：HWND/WPARAM/LPARAM 打成元组，便于断言 hwnd 被原样传出。"""

    def HWND(self, value: Any) -> tuple:
        return ("HWND", value)

    def WPARAM(self, value: Any) -> tuple:
        return ("WPARAM", value)

    def LPARAM(self, value: Any) -> tuple:
        return ("LPARAM", value)


# ---------------------------------------------------------------------------
#  autouse 安全网：真 Win32 / 真线程 / 真弹窗都不许发生
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def user32(monkeypatch) -> FakeUser32:
    fake = FakeUser32()
    windll = type("_W", (), {"user32": fake})()
    monkeypatch.setattr(verification, "ctypes", type("_C", (), {"windll": windll})())
    monkeypatch.setattr(verification, "wintypes", FakeWinTypes())
    return fake


@pytest.fixture(autouse=True)
def vthreading(monkeypatch) -> FakeThreading:
    fake = FakeThreading()
    monkeypatch.setattr(verification, "threading", fake)
    return fake


@pytest.fixture(autouse=True)
def focus_calls(monkeypatch) -> list[dict]:
    """force_focus 默认置空并记录：等待循环每轮都会调它，测试里绝不能弹窗。"""
    calls: list[dict] = []
    monkeypatch.setattr(verification, "force_focus", lambda **kw: calls.append(kw))
    return calls


@pytest.fixture
def clock(monkeypatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(verification, "time", fake)
    return fake


# ---------------------------------------------------------------------------
#  契约 1/2：三信号检测与短路顺序
# ---------------------------------------------------------------------------
def test_dom_signal_hit_returns_true_and_skips_other_two() -> None:
    """DOM 信号排第一；命中后另两个信号根本不该被问（省 2 次 execute_script）。"""
    d = FakeDriver(verdicts={"probe-dom": True, "probe-body": True})
    assert verification.is_smart_verification_showing(d) is True
    assert d.scripts == ["probe-dom"]


def test_shadow_iframe_signal_is_second_probe() -> None:
    d = FakeDriver(verdicts={"probe-shadow": True, "probe-body": True})
    assert verification.is_smart_verification_showing(d) is True
    assert d.scripts == ["probe-dom", "probe-shadow"]


def test_url_title_body_signal_is_last_probe() -> None:
    """关键词信号（url/title/body）排在最后 —— 它的正则最宽，最容易误报。"""
    d = FakeDriver(verdicts={"probe-body": True})
    assert verification.is_smart_verification_showing(d) is True
    assert d.scripts == ["probe-dom", "probe-shadow", "probe-body"]


def test_all_signals_falsy_probes_three_times_then_false() -> None:
    """三信号全阴性 → False，且确实扫了 3 次（没有缓存、没有跳过）。"""
    d = FakeDriver()
    assert verification.is_smart_verification_showing(d) is False
    assert d.scripts == ["probe-dom", "probe-shadow", "probe-body"]


def test_falsy_non_bool_values_read_as_no_verification() -> None:
    """JS 的回传形态是 None/0/'' （Selenium 对 null/0/'' 的翻译），不是 Python False。"""
    for value in (None, 0, "", 0.0):
        assert verification.is_smart_verification_showing(
            FakeDriver(verdicts={"probe-dom": [value]})
        ) is False


def test_execute_script_exception_returns_false_instead_of_raising() -> None:
    """探测期驱动抛异常（导航中 / 切 frame 失效）必须被吞掉并返回 False。

    broad ``except Exception: pass`` 是刻意的：验证码检测是旁路判断，
    它自己出问题不能把整条提交流程带崩。
    """
    assert verification.is_smart_verification_showing(
        FakeDriver(raises=("probe-dom",))
    ) is False


def test_exception_also_short_circuits_the_scan() -> None:
    """异常同样触发短路：后面的信号不再被尝试，扫到第 2 个就结束。

    把"调用次数"钉死 —— 有人把三信号改成各自 try、或改成无条件全扫，这里会红。
    """
    d = FakeDriver(raises=("probe-shadow",))
    assert verification.is_smart_verification_showing(d) is False
    assert d.scripts == ["probe-dom", "probe-shadow"]


def test_driver_without_execute_script_is_tolerated() -> None:
    """AttributeError 也在容忍范围内（except Exception，不是只捕 WebDriverException）。"""
    assert verification.is_smart_verification_showing(NoScriptDriver()) is False


# ---------------------------------------------------------------------------
#  契约 3/5/8：人工等待的成功路径与锁的生命周期
# ---------------------------------------------------------------------------
def test_returns_true_once_captcha_clears_and_releases_lock(user32, clock) -> None:
    """用户滑完 → 下一轮探测不到验证码 → True，且外部共享的锁被释放。"""
    lock = ManualHoldLock()
    d = FakeDriver(verdicts={"probe-dom": [True, True, False]})
    assert verification.wait_for_manual_verification(
        d, timeout_seconds=60, hold_lock=lock
    ) is True
    assert clock.slept == [2, 2, 2], "每轮 2 秒，滑完即止，不该多睡"
    assert d.count("probe-dom") == 3
    assert lock.is_holding is False, "返回后必须已 release，否则 pipeline 永久挂起"
    assert user32.names() == [], "真实 Win32 API 不得被触碰"


def test_lock_is_holding_while_waiting_for_the_human(clock) -> None:
    """等待期间锁必须处于 holding —— 这是 pipeline 暂停超时预算的唯一依据。

    观察点放在 driver 里：每轮探测都读一次 is_holding，循环内任何一刻掉了都会被抓到。
    """
    lock = ManualHoldLock()
    seen: list[bool] = []
    d = FakeDriver(
        verdicts={"probe-dom": [True, True, False]},
        observer=lambda tag: seen.append(lock.is_holding) if tag == "probe-dom" else None,
    )
    assert verification.wait_for_manual_verification(
        d, timeout_seconds=60, hold_lock=lock
    ) is True
    assert seen == [True, True, True], "整个等待过程都该处于 holding"
    assert lock.is_holding is False


def test_clearing_poll_runs_the_full_three_signal_scan(clock) -> None:
    """判定"验证码消失"要求三信号全阴性，不能只看 DOM 一个选择器。"""
    d = FakeDriver(verdicts={"probe-dom": [True, False]})
    assert verification.wait_for_manual_verification(d, timeout_seconds=60) is True
    assert d.scripts == ["probe-dom", "probe-dom", "probe-shadow", "probe-body"]


def test_hold_lock_none_self_creates_a_lock(clock, monkeypatch) -> None:
    """不传锁也要能跑（gui/controller.py 就是这么调的）：内部自建锁并正常释放。"""
    created: list[ManualHoldLock] = []

    class SpyLock(ManualHoldLock):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

    monkeypatch.setattr(verification, "ManualHoldLock", SpyLock)
    d = FakeDriver(verdicts={"probe-dom": [True, False]})
    assert verification.wait_for_manual_verification(d, timeout_seconds=10) is True
    assert len(created) == 1, "hold_lock=None 时应自建一把一次性锁"
    assert created[0].is_holding is False


def test_lock_released_even_when_the_wait_body_raises(monkeypatch) -> None:
    """finally 兜底：轮询里任何意外（这里用假时钟模拟 sleep 之外的抛出）都要放锁。

    回归后果最严重的一条：锁卡住 → is_holding 永真 → 外部永远等待 → 整批任务挂死。
    """
    lock = ManualHoldLock()
    lock.acquire()  # 外部也持有过一次，确认 release 真的落到底
    d = FakeDriver(verdicts={"probe-dom": True})
    monkeypatch.setattr(verification, "time", FakeTime(raises=RuntimeError("driver 之外的意外")))
    with pytest.raises(RuntimeError, match="意外"):
        verification.wait_for_manual_verification(d, timeout_seconds=120, hold_lock=lock)
    assert lock.is_holding is False


def test_force_focus_is_called_once_before_the_wait_loop(focus_calls, clock) -> None:
    """提醒只弹一次（在 acquire 之前）：循环里反复弹会不停抢焦点。"""
    d = FakeDriver(verdicts={"probe-dom": [True, True, False]})
    verification.wait_for_manual_verification(d, timeout_seconds=60)
    assert focus_calls == [{}], "默认参数的单次调用"


def test_progress_and_result_are_printed(capsys, clock) -> None:
    """进度/结果提示走 print：CLI 把这段 stdout 当成唯一的人机反馈。"""
    d = FakeDriver(verdicts={"probe-dom": [True] * 5 + [False]})
    assert verification.wait_for_manual_verification(d, timeout_seconds=60) is True
    out = capsys.readouterr().out
    assert "[10s / 60s]" in out, "每 10 秒一次进度提示"
    assert "20s /" not in out, "第 20 秒不该再来一次（间隔是 10 秒，不是 2 秒）"
    assert "!!" in out, "进入人工介入时有醒目横幅"


# ---------------------------------------------------------------------------
#  契约 4：超时分支
# ---------------------------------------------------------------------------
def test_timeout_returns_false_then_closes_dialog_and_refreshes(user32, clock) -> None:
    lock = ManualHoldLock()
    d = FakeDriver(verdicts={"probe-dom": True})
    assert verification.wait_for_manual_verification(
        d, timeout_seconds=6, hold_lock=lock
    ) is False
    assert d.count("close") == 1, "超时分支要真的去点关闭按钮"
    assert d.scripts[-1] == "close"
    assert d.refreshes == 1
    assert d.count("probe-dom") == 3, "6 秒预算 = 3 轮"
    assert clock.slept == [2, 2, 2, 2], "超时后还要再等 2 秒让页面稳定"
    assert lock.is_holding is False


def test_timeout_tolerates_raising_refresh(clock) -> None:
    """refresh 抛异常（浏览器窗口已经崩了）也必须返回 False，而不是把异常冒给调用方。"""
    lock = ManualHoldLock()
    lock.acquire()
    d = FakeDriver(verdicts={"probe-dom": True}, raises=("refresh",))
    assert verification.wait_for_manual_verification(
        d, timeout_seconds=4, hold_lock=lock
    ) is False
    assert d.refreshes == 1
    assert lock.is_holding is False


def test_timeout_tolerates_raising_close_script(clock) -> None:
    """关闭按钮 JS 失败不能拖累刷新：两段各有自己的 try/except，不能合并成一个。"""
    d = FakeDriver(verdicts={"probe-dom": True}, raises=("close",))
    assert verification.wait_for_manual_verification(d, timeout_seconds=4) is False
    assert d.count("close") == 1
    assert d.refreshes == 1


def test_zero_timeout_gives_up_without_polling(clock) -> None:
    """timeout_seconds=0：循环一次都不进，直接关闭 + 刷新 → False。

    钉死"预算为 0 时不做任何判定"，避免有人把 while 改成 do-while 白等一轮。
    """
    d = FakeDriver(verdicts={"probe-dom": True})
    assert verification.wait_for_manual_verification(d, timeout_seconds=0) is False
    assert d.scripts == ["close"]
    assert d.refreshes == 1
    assert clock.slept == [2]


# ---------------------------------------------------------------------------
#  探测失败 ≠ 验证已消失（v2.7 修复）
# ---------------------------------------------------------------------------
def test_transient_probe_error_keeps_waiting_instead_of_declaring_pass(
    clock, capsys
) -> None:
    """探测本身抛异常时**不能**判成"验证已通过"。

    v2.7 之前 ``is_smart_verification_showing`` 用 ``except Exception: pass``
    把"探测失败"和"页面确实没有验证"压成同一个 False，等待循环据此打印
    "验证已通过"并**释放 ManualHoldLock** —— 用户手上的滑块还没拉完，流程就继续
    撞进一个仍被挡住的页面，下一轮 SUBMIT_FAILED。
    现在探测失败是三态里的 None：继续等，上限仍是 timeout_seconds，最终走超时分支。
    """
    lock = ManualHoldLock()
    d = FakeDriver(raises=("probe-dom",))
    assert verification.wait_for_manual_verification(
        d, timeout_seconds=6, hold_lock=lock
    ) is False
    assert d.count("probe-dom") == 3, "6 秒预算 = 3 轮，每轮都因探测失败继续等"
    out = capsys.readouterr().out
    assert "验证已通过" not in out, "探测失败被当成了放行信号"
    assert "探测失败" in out, "继续等的原因要可见，否则像是在空转"
    assert lock.is_holding is False, "超时收尾仍必须释放锁，否则整批挂死"


def test_probe_failure_followed_by_a_clean_page_still_returns_true(clock) -> None:
    """探测失败只是"这一轮未知"：下一轮拿到确认的 False 仍然立刻放行。

    防止修复过头 —— 把"未知"当成"仍在验证"永久等下去，会让一个早已通过的
    验证码白等满 timeout。
    """
    lock = ManualHoldLock()
    # 第 1 次探测抛异常（上下文被跳转打断 → 状态未知），随后三信号全部判否
    # （确认页面确实没有验证）。用脚本化序列而非 FakeDriver，因为它的 raises
    # 优先级高于 verdicts，表达不了"只坏一轮"。
    seq: list[Any] = [RuntimeError("上下文被跳转打断"), False, False, False]

    class Scripted:
        def __init__(self) -> None:
            self.calls = 0

        def execute_script(self, _script, *_a, **_kw):
            item = seq[min(self.calls, len(seq) - 1)]
            self.calls += 1
            if isinstance(item, Exception):
                raise item
            return item

    scripted = Scripted()
    assert verification.wait_for_manual_verification(
        scripted, timeout_seconds=60, hold_lock=lock
    ) is True
    assert scripted.calls == 4, "1 次抛异常 + 1 轮三信号全否 = 4 次 execute_script"
    assert lock.is_holding is False


# ---------------------------------------------------------------------------
#  契约 6/7：force_focus 非阻塞 + 自动关闭计时器
# ---------------------------------------------------------------------------
def test_force_focus_returns_while_the_messagebox_is_still_open(monkeypatch, user32) -> None:
    """v2.5 修复的核心回归点，也是本文件价值最高的一条。

    旧实现直接在批量提交的工作线程上调用同步模态 MessageBoxW：没人点确定时
    Ctrl+C 打不断、GUI 停止按钮也只是置位，无人值守的机器整批任务永久挂起。
    现在弹窗挪到守护线程 —— 所以"弹窗还阻塞着"的时候 force_focus 必须已经返回。
    """
    entered = threading.Event()
    released = threading.Event()
    where: list[str] = []

    def blocking_messagebox(*_args) -> None:
        where.append(threading.current_thread().name)
        entered.set()
        released.wait(5)

    user32.messagebox_hook = blocking_messagebox
    live = FakeThreading(run_threads=True)
    monkeypatch.setattr(verification, "threading", live)

    try:
        started = time.perf_counter()
        _REAL_FORCE_FOCUS(auto_close_seconds=30)
        elapsed = time.perf_counter() - started

        assert elapsed < 0.5, f"force_focus 阻塞了调用线程 {elapsed:.2f}s（弹窗必须异步）"
        assert entered.wait(2) is True, "弹窗根本没被触发：异步化改错了地方"
        assert not released.is_set(), "MessageBoxW 仍未返回 —— 调用线程却已经解放"
        assert where and where != ["MainThread"], "弹窗必须跑在非调用线程上"
    finally:
        # 释放被卡住的假 MessageBoxW，并确认没有线程活过本测试
        released.set()
        for thread in live.threads:
            thread.join(5)


def test_messagebox_is_a_one_shot_daemon_thread(vthreading, user32) -> None:
    """只起 1 个 daemon 线程，且 MessageBoxW 拿到标题与 MB_ICONWARNING 标志。"""
    _REAL_FORCE_FOCUS(auto_close_seconds=0)
    assert len(vthreading.threads) == 1
    assert vthreading.only_thread.daemon is True
    assert vthreading.only_thread.started is True

    vthreading.run_thread()  # 同步跑弹窗体，不留真线程
    assert user32.names() == ["MessageBoxW"]
    args = user32.args_of("MessageBoxW")
    assert args[0] == 0, "hwnd=0 → 无父窗口的独立对话框"
    assert args[2] == _TITLE
    assert args[3] & 0x30 == 0x30, "MB_ICONWARNING"


def test_show_swallows_win32_errors(vthreading, user32) -> None:
    """弹窗失败（无桌面会话 / 远程桌面断连）只记 debug 日志，绝不能影响等待流程。"""
    user32.raises = {"MessageBoxW"}
    _REAL_FORCE_FOCUS(auto_close_seconds=0)
    vthreading.run_thread()  # 不抛即通过
    assert user32.names() == ["MessageBoxW"]


def test_auto_close_timer_is_scheduled_with_given_delay(vthreading) -> None:
    _REAL_FORCE_FOCUS(auto_close_seconds=7.5)
    assert len(vthreading.timers) == 1
    assert vthreading.only_timer.delay == 7.5
    assert vthreading.only_timer.daemon is True, "非守护计时器会拖住进程退出"
    assert vthreading.only_timer.started is True


def test_default_auto_close_is_25_seconds(vthreading) -> None:
    _REAL_FORCE_FOCUS()
    assert vthreading.timers[0].delay == 25.0


@pytest.mark.parametrize("seconds", [0, -1, -30.0])
def test_non_positive_auto_close_schedules_no_timer(vthreading, seconds: float) -> None:
    """auto_close_seconds<=0 表示"只提醒、不自动关"，此时连计时器都不该建。"""
    _REAL_FORCE_FOCUS(auto_close_seconds=seconds)
    assert vthreading.timers == []


def test_auto_close_closes_the_box_by_handle(vthreading, user32) -> None:
    """计时器回调真的按标题找窗口并投 WM_CLOSE —— 否则 25 秒后框还挂在屏幕上。"""
    user32.find_window_result = 0x1234
    _REAL_FORCE_FOCUS(auto_close_seconds=5)
    user32.calls.clear()

    vthreading.only_timer.fn()  # 手动到点
    assert user32.names() == ["FindWindowW", "PostMessageW"]
    assert user32.args_of("FindWindowW")[1] == _TITLE
    posted = user32.args_of("PostMessageW")
    assert posted[0] == ("HWND", 0x1234)
    assert posted[1] == _WM_CLOSE


def test_auto_close_is_a_noop_when_window_already_gone(vthreading, user32) -> None:
    """用户已经自己点掉框 → 找不到句柄就什么都不做（乱发消息会打到别的窗口）。"""
    user32.find_window_result = 0
    _REAL_FORCE_FOCUS(auto_close_seconds=5)
    user32.calls.clear()

    vthreading.only_timer.fn()
    assert user32.names() == ["FindWindowW"]


def test_auto_close_swallows_win32_errors(vthreading, user32) -> None:
    """自动关闭失败只是"框多留一会儿"，不能让后台计时器线程崩掉。"""
    user32.raises = {"FindWindowW"}
    _REAL_FORCE_FOCUS(auto_close_seconds=5)
    vthreading.only_timer.fn()  # 不抛即通过


# ---------------------------------------------------------------------------
#  v3.0：停止按钮打断人工验证码等待
# ---------------------------------------------------------------------------
def test_abort_check_ends_the_wait_and_releases_the_lock(clock) -> None:
    """一次验证码等待最长 VERIFICATION_TIMEOUT(120s)，此前期间点停止毫无反应。

    锁不释放的话 pipeline 的超时判断会一直挂起 —— 卡住的不是这一份，是整批。
    """
    lock = ManualHoldLock()
    d = FakeDriver(verdicts={"probe-dom": True})
    with pytest.raises(SubmissionAborted):
        verification.wait_for_manual_verification(
            d, timeout_seconds=60, hold_lock=lock, abort_check=lambda: True,
        )
    assert lock.is_holding is False, "finally 必须释放人工介入锁"
    assert d.refreshes == 0, "已被要求停止，就别再往页面发刷新请求"
    assert clock.slept == [2], "第一轮轮询即退出，不再耗满预算"


def test_abort_check_none_keeps_the_full_wait(clock) -> None:
    """不传 abort_check（CLI 未开停止通道时的原路径）→ 行为与 v2.8 一致：超时返回 False。"""
    lock = ManualHoldLock()
    d = FakeDriver(verdicts={"probe-dom": True})
    assert verification.wait_for_manual_verification(
        d, timeout_seconds=6, hold_lock=lock
    ) is False
    # 3 轮轮询 + 超时收尾前的那一次 sleep(2)
    assert clock.slept == [2, 2, 2, 2]
    assert d.refreshes == 1, "超时路径仍要刷新页面"
    assert lock.is_holding is False
