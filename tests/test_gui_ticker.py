"""``gui/ticker.py`` 的离线契约测试（设计稿 §7B 组）。

这里测的是"心跳"这件事本身，不涉及任何控件：``schedule`` 与 ``clock`` 都是注入的
假件，所以能把"空闲时一个 after 作业都不留"这种**结构**约束钉死 —— 而这条约束正是
改造前做不到的（呼吸灯 80ms / 扫描线 40ms / 光标 530ms 三个循环各自常驻，
只要窗口开着就一直重绘）。

钉住的行为：
    1. 空登记不排任何定时器；有活也只排**一个**循环；
    2. ``dt`` 按真实流逝时间分发，主线程被拖慢时不丢帧；
    3. 原语或 ``apply`` 抛异常只摘掉它自己，并记一条 WARN ——
       改造前是 ``except Exception: pass``，动效静默死掉没人知道；
    4. ``WJX_MOTION=0`` 时直接落终值，且一个作业都不排。
"""

from __future__ import annotations

import random
import time

import pytest

from gui.motion import NumberTween, ScrambleText
from gui.ticker import MotionTicker, monotonic_ms


class FakeLoop:
    """手动时间轴：``advance`` 跑掉所有到期作业，顺带记录排了多少个作业。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.pending: list[list] = []
        self.scheduled = 0

    def schedule(self, ms, fn):
        self.scheduled += 1
        self.pending.append([self.now + ms, fn])

    def clock(self):
        return self.now

    def advance(self, ms):
        target = self.now + ms
        guard = 0
        while True:
            guard += 1
            assert guard < 10_000, "时间轴没在推进，循环自己排了自己"
            due = [job for job in self.pending if job[0] <= target]
            if not due:
                break
            due.sort(key=lambda job: job[0])
            job = due[0]
            self.pending.remove(job)
            self.now = max(self.now, job[0])
            job[1]()
        self.now = target

    @property
    def idle(self) -> bool:
        return not self.pending


class Recorder:
    """只记 ``dt`` 的假原语，用于验证不丢帧。"""

    def __init__(self, stop_after: int | None = None, boom: bool = False) -> None:
        self.dts: list[float] = []
        self._n = 0
        self._stop_after = stop_after
        self._boom = boom

    @property
    def done(self) -> bool:
        return self._stop_after is not None and self._n >= self._stop_after

    def step(self, dt):
        if self._boom:
            raise RuntimeError("boom")
        self.dts.append(dt)
        self._n += 1
        return self._n


@pytest.fixture()
def loop() -> FakeLoop:
    return FakeLoop()


@pytest.fixture()
def ticker(loop) -> MotionTicker:
    return MotionTicker(schedule=loop.schedule, clock=loop.clock)


# ---------------------------------------------------------------- 空闲即停


def test_default_clock_measures_milliseconds_not_seconds():
    """``time.monotonic()`` 返回秒，而这里所有时长都是毫秒。

    开发机上真实踩过：默认时钟用秒制，每个补间慢一千倍，表现成"动画永远不动、
    心跳一直挂着"，而注入假毫秒时钟的其余用例全绿 —— 所以这条必须直接量墙钟。
    """
    start = monotonic_ms()
    deadline = time.monotonic() + 0.05
    while time.monotonic() < deadline:
        pass
    assert 40 <= monotonic_ms() - start <= 500


def test_real_clock_drives_a_tween_to_completion():
    """不注入假时钟，用真 after 语义走一遍：补间必须真的会走完并退场。"""
    jobs: list[int] = []
    t = MotionTicker(schedule=lambda ms, fn: jobs.append((ms, fn)))
    seen: list = []
    t.register("k", NumberTween(0, 4, dur=60), seen.append)
    assert jobs and jobs[0][0] <= 60          # 排的是毫秒量级的延时，不是 60 秒
    for _ in range(40):
        ms, fn = jobs.pop(0) if jobs else (0, None)
        if fn is None:
            break
        time.sleep(ms / 1000.0)
        fn()
    assert seen[-1] == 4
    assert t.running is False


def test_idle_ticker_schedules_nothing_at_all(loop, ticker):
    assert loop.scheduled == 0
    assert ticker.running is False
    assert len(ticker) == 0


def test_work_then_done_leaves_no_pending_job(loop, ticker):
    seen: list[int] = []
    ticker.register("cnt", NumberTween(0, 5, dur=60), seen.append)
    assert loop.scheduled == 1
    loop.advance(500)
    assert seen[-1] == 5
    assert ticker.running is False
    assert loop.idle
    after = loop.scheduled
    loop.advance(2000)
    assert loop.scheduled == after  # 停摆之后不再自己复活


def test_many_primitives_share_one_loop(loop, ticker):
    ticker.register("a", NumberTween(0, 3, dur=60), lambda v: None)
    ticker.register("b", NumberTween(0, 4, dur=60), lambda v: None)
    ticker.register("c", Recorder(), lambda v: None)
    assert loop.scheduled == 1
    loop.advance(30)
    assert loop.scheduled == 2  # 一个心跳一个作业，不是三个


def test_partial_completion_keeps_the_loop_alive(loop, ticker):
    ticker.register("short", NumberTween(0, 1, dur=30), lambda v: None)
    ticker.register("long", Recorder(), lambda v: None)
    loop.advance(120)
    assert "short" not in ticker._items
    assert "long" in ticker._items
    assert ticker.running is True


# ---------------------------------------------------------------- 时间轴


def test_dt_follows_real_elapsed_time_and_loses_no_frames(loop, ticker):
    rec = Recorder()
    ticker.register("r", rec, lambda v: None)
    loop.advance(100)
    assert sum(rec.dts) == pytest.approx(90.0)  # 100ms 里只有 30/60/90 三拍到期
    loop.advance(20)
    assert sum(rec.dts) == pytest.approx(120.0)  # 那剩下的 10ms 必须补上，不能被吞
    loop.advance(250)
    assert sum(rec.dts) == pytest.approx(360.0)
    assert 0 <= 370.0 - sum(rec.dts) < 30        # 未分发部分永远不足一拍


def test_a_lagging_main_thread_slows_the_clock_not_the_animation(loop, ticker):
    """主线程被 Selenium 拖住 400ms 之后，原语拿到的仍是那 400ms。"""
    rec = Recorder()
    ticker.register("r", rec, lambda v: None)
    loop.advance(30)          # 正常一拍
    loop.now += 400           # 模拟主线程卡住，作业晚点才兑现
    loop.advance(30)
    assert max(rec.dts) >= 400.0


def test_an_apply_that_cancels_a_sibling_skips_it_in_the_same_tick(loop, ticker):
    """状态字溶解完把呼吸灯取消掉，是接入时真实会发生的形状 —— 同一拍不该再喂它 dt。"""
    victim = Recorder()
    ticker.register("killer", Recorder(), lambda v: ticker.cancel("victim"))
    ticker.register("victim", victim, lambda v: None)
    loop.advance(30)
    assert victim.dts == []
    assert "victim" not in ticker._items
    assert len(ticker) == 1


def test_an_apply_that_replaces_its_own_key_keeps_the_new_one(loop, ticker):
    """溶解到一半来了新状态：旧原语收尾时不许把刚挂上去的那个一起摘掉。"""
    swapped: list[int] = []
    seen: list = []
    second = NumberTween(0, 3, dur=30)

    def first_apply(_v):
        if not swapped:
            swapped.append(1)
            ticker.register("status", second, seen.append)

    ticker.register("status", NumberTween(0, 1, dur=30), first_apply)
    loop.advance(200)
    assert swapped == [1]
    assert seen[-1] == 3
    assert ticker.running is False


def test_no_dispatch_before_the_first_interval(loop, ticker):
    rec = Recorder()
    ticker.register("r", rec, lambda v: None)
    loop.advance(29)
    assert rec.dts == []


# ---------------------------------------------------------------- 取消与覆盖


def test_cancel_drops_only_that_one(loop, ticker):
    keep = Recorder()
    ticker.register("gone", Recorder(), lambda v: None)
    ticker.register("keep", keep, lambda v: None)
    loop.advance(30)
    ticker.cancel("gone")
    n = len(keep.dts)
    loop.advance(60)
    assert len(keep.dts) > n
    assert "gone" not in ticker._items


def test_cancelling_everything_stops_the_loop(loop, ticker):
    ticker.register("a", Recorder(), lambda v: None)
    loop.advance(30)
    ticker.cancel("a")
    loop.advance(30)
    assert ticker.running is False
    assert loop.idle


def test_registering_the_same_key_replaces_the_old_primitive(loop, ticker):
    first = ScrambleText("就绪", "探测中", rng=random.Random(1), dur=400)
    ticker.register("status", first, lambda v: None)
    later = NumberTween(0, 9, dur=30)
    ticker.register("status", later, lambda v: v)
    assert len(ticker) == 1
    seen: list = []
    ticker.register("status", later, seen.append)
    loop.advance(200)
    assert seen[-1] == 9


def test_apply_may_register_more_work_without_losing_it(loop, ticker):
    """状态字溶解完顺手挂上呼吸灯，是接入时真实会发生的形状。"""
    def on_done(value):
        if not chained:
            chained.append(1)
            ticker.register("breath", Recorder(), lambda v: None)

    chained: list[int] = []
    ticker.register("status", NumberTween(0, 2, dur=30), on_done)
    loop.advance(120)
    assert chained == [1]
    assert "breath" in ticker._items
    assert ticker.running is True


# ---------------------------------------------------------------- 异常隔离


def test_a_throwing_primitive_is_dropped_and_reported(loop, ticker):
    logs: list[str] = []
    t = MotionTicker(schedule=loop.schedule, clock=loop.clock, log_fn=logs.append)
    healthy = Recorder()
    t.register("bad", Recorder(boom=True), lambda v: None)
    t.register("good", healthy, lambda v: None)
    loop.advance(90)
    assert "bad" not in t._items
    assert len(healthy.dts) >= 2          # 整条循环没被带死
    assert t.running is True
    assert len(logs) == 1                 # 只报一次，不刷屏
    assert "RuntimeError" in logs[0] and "boom" in logs[0]


def test_a_throwing_apply_is_isolated_the_same_way(loop, ticker):
    logs: list[str] = []
    t = MotionTicker(schedule=loop.schedule, clock=loop.clock, log_fn=logs.append)

    def bad_apply(_v):
        raise ValueError("widget is gone")

    healthy = Recorder()
    t.register("bad", Recorder(), bad_apply)
    t.register("good", healthy, lambda v: None)
    loop.advance(90)
    assert "bad" not in t._items
    assert len(healthy.dts) >= 2
    assert len(logs) == 1 and "ValueError" in logs[0]


def test_a_failing_primitive_that_was_already_replaced_leaves_the_new_one(loop):
    """旧原语的回调抛了，但这个 key 中途已被换新 —— 摘错就是把新动效误杀。"""
    logs: list[str] = []
    t = MotionTicker(schedule=loop.schedule, clock=loop.clock, log_fn=logs.append)
    healthy = NumberTween(0, 5, dur=60)
    landed: list = []

    def bad_apply(_v):
        t.register("k", healthy, landed.append)
        raise RuntimeError("boom")

    t.register("k", Recorder(), bad_apply)
    loop.advance(30)
    assert "k" in t._items          # 新登记的那个没被误杀
    assert len(logs) == 1
    loop.advance(300)
    assert landed[-1] == 5


def test_without_log_fn_a_throwing_primitive_still_cannot_kill_the_loop(loop, ticker):
    healthy = Recorder()
    ticker.register("bad", Recorder(boom=True), lambda v: None)
    ticker.register("good", healthy, lambda v: None)
    loop.advance(90)
    assert len(healthy.dts) >= 2


# ---------------------------------------------------------------- 总开关


def test_switch_off_lands_on_the_terminal_value_and_schedules_nothing(
        loop, ticker, monkeypatch):
    monkeypatch.setenv("WJX_MOTION", "0")
    seen: list = []
    ticker.register("cnt", NumberTween(0, 12, dur=400), seen.append)
    assert seen == [12]
    assert loop.scheduled == 0
    assert ticker.running is False
    assert len(ticker) == 0


def test_switch_off_still_reports_an_apply_that_throws(loop, ticker, monkeypatch):
    monkeypatch.setenv("WJX_MOTION", "0")
    logs: list[str] = []
    t = MotionTicker(schedule=loop.schedule, clock=loop.clock, log_fn=logs.append)

    def bad(_v):
        raise KeyError("no such widget")

    t.register("x", NumberTween(0, 3), bad)
    assert len(logs) == 1 and "KeyError" in logs[0]


def test_switch_back_on_after_a_shut_down_registers_nothing(loop, ticker):
    ticker.shutdown()
    ticker.register("late", Recorder(), lambda v: None)
    assert len(ticker) == 0
    assert loop.scheduled == 0


# ---------------------------------------------------------------- 关窗


def test_shutdown_clears_and_refuses_to_schedule_more(loop, ticker):
    ticker.register("a", Recorder(), lambda v: None)
    loop.advance(30)
    ticker.shutdown()
    assert len(ticker) == 0
    assert ticker.running is False
    scheduled_at = loop.scheduled
    loop.advance(500)
    assert loop.scheduled == scheduled_at


def test_a_job_already_in_flight_after_shutdown_does_nothing(loop, ticker):
    """关窗瞬间可能已有一个作业排在队列里，兑现它不该把循环重新点着。"""
    ticker.register("a", Recorder(), lambda v: None)
    job = loop.pending[0]
    ticker.shutdown()
    job[1]()
    assert ticker.running is False
    assert loop.scheduled == 1


# ---------------------------------------------------------------- 真原语过一遍


def test_real_primitives_reach_exact_final_values_through_the_loop(loop, ticker):
    texts: list[str] = []
    nums: list = []
    ticker.register("status",
                    ScrambleText("就绪", "运行中", rng=random.Random(7), dur=200),
                    texts.append)
    ticker.register("ok", NumberTween(0, 37, dur=200), nums.append)
    loop.advance(1000)
    assert texts[-1] == "运行中"
    assert nums[-1] == 37
    assert ticker.running is False
