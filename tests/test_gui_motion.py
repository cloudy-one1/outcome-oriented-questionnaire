"""``gui/motion.py`` 的离线契约测试（设计稿 §7A 组）。

为什么单独一个文件、为什么能不带 Tk 跑：这一层的每个原语只吃 ``dt_ms`` 返回显示值，
所以"动效对不对"第一次变成可以在毫秒刻度上逐帧断言的东西 —— 而改造前的三个
``after`` 循环（呼吸灯 80ms / 扫描线 40ms / 光标 530ms）全绑在 ``SurveyGUI.__init__`` 上，
``tests/test_gui_panels.py`` 因此明写"刻意不构造 SurveyGUI"。本文件钉住的是那层债的还法：

    1. 端点与精确性 —— 计数动画停在 0.9999 或比目标少一个是看得见的错，
       所以 ``t>=dur`` 必须**正好**落在目标值；
    2. 单调与改目标 —— 长跑时数字倒着走、或连点两下停止后进度跳回 0 重来，
       都是"界面在撒谎"级别的故障；
    3. 总开关 —— ``WJX_MOTION=0`` 时第一次 ``step()`` 就给终值，功能一项不少；
    4. 确定性 —— 溶解文字吃注入的 ``rng``，不注入就是每次跑一种花法。

刻意不测的：像素、真实帧率、"看起来在动"。那些在无显示 runner 上必然 flaky，
归 §7E 的 PrintWindow 人工对拍。
"""

from __future__ import annotations

import math
import random

import pytest

from gui import motion
from gui.motion import (
    STAGGER,
    STAGGER_MAX,
    Blink,
    Collapse,
    EasedProgress,
    NumberTween,
    Pulse,
    Run,
    ScrambleText,
    TextLoop,
    enabled,
    ease_out_cubic,
    spring_damped,
    stagger_delays,
)


@pytest.fixture(autouse=True)
def _motion_on(monkeypatch):
    """默认全部用例在"动效开着"的前提下跑；关开关的用例自己改回去。"""
    monkeypatch.setenv("WJX_MOTION", "1")


def run_to_end(prim, dt=30.0, limit=200):
    """按固定节拍把原语推到 done，返回逐帧的返回值序列。"""
    out = [prim.step(dt)]
    for _ in range(limit):
        if prim.done:
            break
        out.append(prim.step(dt))
    return out


# ---------------------------------------------------------------- 开关


@pytest.mark.parametrize("raw", ["1", "", "on", "YES"])
def test_enabled_accepts_anything_else_as_on(raw, monkeypatch):
    monkeypatch.setenv("WJX_MOTION", raw)
    assert enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "no", " OFF "])
def test_enabled_knows_the_off_words(raw, monkeypatch):
    monkeypatch.setenv("WJX_MOTION", raw)
    assert enabled() is False


def test_enabled_defaults_to_on_when_unset(monkeypatch):
    monkeypatch.delenv("WJX_MOTION", raising=False)
    assert enabled() is True


# ---------------------------------------------------------------- 缓动


def test_ease_endpoints_are_exact_and_monotonic():
    assert ease_out_cubic(-1.0) == 0.0
    assert ease_out_cubic(0.0) == 0.0
    assert ease_out_cubic(1.0) == 1.0
    assert ease_out_cubic(2.0) == 1.0
    samples = [ease_out_cubic(i / 20) for i in range(21)]
    assert samples == sorted(samples)
    assert ease_out_cubic(0.5) > 0.5  # 先快后慢


def test_spring_over_shoots_then_settles():
    assert spring_damped(0.0) == 0.0
    assert spring_damped(1.0) == 1.0
    mid = [spring_damped(i / 10) for i in range(11)]
    assert max(mid) > 1.0  # 进度条收尾那一点过冲是故意的
    assert mid[-1] == 1.0


# ---------------------------------------------------------------- NumberTween


def test_number_tween_starts_at_from_and_lands_exactly_on_target():
    tw = NumberTween(0, 12, dur=220)
    assert tw.step(0.0) == 0
    assert tw.done is False
    values = run_to_end(tw, dt=30.0)
    assert values[-1] == 12
    assert tw.value == 12
    assert tw.done is True


def test_number_tween_never_shows_a_fractional_count():
    tw = NumberTween(0, 7, dur=200)
    for _ in range(30):
        v = tw.step(17.0)
        assert isinstance(v, int)
        assert 0 <= v <= 7


def test_number_tween_is_monotonic_never_goes_backwards():
    tw = NumberTween(3, 41, dur=220)
    values = run_to_end(tw, dt=13.0)
    assert values == sorted(values)


def test_number_tween_can_stay_float_for_percentages():
    tw = NumberTween(0.0, 0.5, dur=100, integer=False)
    values = run_to_end(tw, dt=30.0)
    assert values[-1] == pytest.approx(0.5)
    assert any(not float(v).is_integer() for v in values)


def test_number_tween_retargets_from_current_value_not_from_zero():
    tw = NumberTween(0, 50, dur=200)
    for _ in range(4):
        mid = tw.step(30.0)
    assert 0 < mid < 50
    tw.set_target(90)
    assert tw.step(30.0) >= mid  # 关键：改目标不许把数字甩回 0
    assert run_to_end(tw)[-1] == 90


def test_number_tween_target_equal_to_current_is_already_done():
    tw = NumberTween(4, 4)
    assert tw.done is True
    assert tw.step(30.0) == 4


def test_number_tween_step_after_done_is_stable():
    tw = NumberTween(0, 3, dur=50)
    run_to_end(tw)
    assert tw.step(1000.0) == 3
    assert tw.done is True


def test_number_tween_with_switch_off_jumps_to_terminal_at_once(monkeypatch):
    monkeypatch.setenv("WJX_MOTION", "0")
    tw = NumberTween(0, 12, dur=400)
    assert tw.step(1.0) == 12
    assert tw.done is True


# ---------------------------------------------------------------- ScrambleText


def test_scramble_ends_on_the_exact_new_text():
    sc = ScrambleText("就绪", "探测中", rng=random.Random(7))
    assert sc.step(0.0) != "探测中"
    values = run_to_end(sc, dt=40.0)
    assert values[-1] == "探测中"
    assert sc.done is True


def test_scramble_length_is_the_new_text_length_from_the_first_frame():
    sc = ScrambleText("就绪", "运行中", rng=random.Random(1))
    first = sc.step(10.0)
    assert len(first) == len("运行中")


def test_scramble_is_deterministic_with_a_seeded_rng():
    def trace():
        sc = ScrambleText("就绪", "已完成", rng=random.Random(42), dur=400)
        return run_to_end(sc, dt=33.0)

    assert trace() == trace()


def test_scramble_only_uses_new_text_chars_and_symbols():
    allowed = set("运行中") | set(ScrambleText._SYMBOLS)
    sc = ScrambleText("就绪", "运行中", rng=random.Random(3))
    for _ in range(20):
        assert set(sc.step(20.0)) <= allowed


def test_scramble_with_empty_text_does_not_blow_up():
    sc = ScrambleText("运行中", "", rng=random.Random(0))
    assert sc.step(500.0) == ""
    assert sc.done is True


def test_scramble_with_switch_off_returns_new_text_immediately(monkeypatch):
    monkeypatch.setenv("WJX_MOTION", "off")
    sc = ScrambleText("就绪", "已停止", rng=random.Random(5))
    assert sc.step(1.0) == "已停止"


# ---------------------------------------------------------------- Collapse


def test_collapse_goes_from_height_to_height_and_stops():
    col = Collapse(320, 38, dur=180)
    assert col.step(0.0) == 320
    values = run_to_end(col, dt=30.0)
    assert values[-1] == 38
    assert values == sorted(values, reverse=True)


def test_collapse_noop_when_already_there():
    assert Collapse(100, 100).done is True
    assert Collapse(100, 100).step(30.0) == 100


def test_collapse_with_switch_off_is_instant(monkeypatch):
    monkeypatch.setenv("WJX_MOTION", "0")
    col = Collapse(320, 38, dur=180)
    assert col.step(1.0) == 38
    assert col.done is True


# ---------------------------------------------------------------- EasedProgress


def test_progress_catches_up_to_target_and_stays_in_range():
    pr = EasedProgress(0.0, dur=220)
    pr.set_target(0.5)
    values = run_to_end(pr, dt=30.0)
    assert values[-1] == pytest.approx(0.5)
    assert all(0.0 <= v <= 1.0 for v in values)


def test_progress_keeps_progress_when_target_moves():
    pr = EasedProgress(0.0, dur=200)
    pr.set_target(0.4)
    run_to_end(pr, dt=30.0)
    before = pr.value
    pr.set_target(0.8)
    after = pr.step(30.0)
    assert after >= before - 1e-9


def test_progress_ignores_retarget_to_the_same_pending_value():
    pr = EasedProgress(0.0, dur=200)
    pr.set_target(0.6)
    pr.step(30.0)
    mid = pr.value
    pr.set_target(0.6)  # 同一个目标重复登记，不该把补间重置回 mid
    assert pr.step(30.0) >= mid


def test_progress_starts_done_at_zero():
    pr = EasedProgress(0.0)
    assert pr.done is True
    assert pr.step(50.0) == 0.0


def test_progress_with_switch_off_is_instant(monkeypatch):
    monkeypatch.setenv("WJX_MOTION", "0")
    pr = EasedProgress(0.0, dur=220)
    pr.set_target(1.0)
    assert pr.step(1.0) == 1.0


# ---------------------------------------------------------------- stagger


def test_stagger_delays_are_evenly_spaced():
    assert stagger_delays(4) == [0, STAGGER, 2 * STAGGER, 3 * STAGGER]


def test_stagger_gives_up_past_the_cap():
    """长跑 9999 份时不能一行行等入场动画。"""
    assert stagger_delays(STAGGER_MAX + 1) == [0.0] * (STAGGER_MAX + 1)
    assert stagger_delays(9999, cap=10) == [0.0] * 9999


def test_stagger_of_nothing_is_nothing():
    assert stagger_delays(0) == []
    assert stagger_delays(-3) == []


def test_stagger_with_switch_off_is_all_zero(monkeypatch):
    monkeypatch.setenv("WJX_MOTION", "0")
    assert stagger_delays(5) == [0.0] * 5


# ---------------------------------------------------------------- 循环型


def test_pulse_advances_until_cancelled():
    p = Pulse(period_ms=10.0, cycles=10.0)
    assert p.step(4.0) == pytest.approx(4.0)
    assert p.done is False
    p.cancel()
    assert p.done is True
    assert p.step(100.0) == pytest.approx(4.0)  # 取消后不再走


def test_pulse_wraps_the_phase_instead_of_growing():
    p = Pulse(period_ms=5.0, cycles=10.0)
    assert p.step(30.0) == pytest.approx(0.0)   # 三整圈回到 0，而不是累到 60
    assert p.done is False


def test_pulse_with_switch_off_parks_at_the_brightest_state(monkeypatch):
    monkeypatch.setenv("WJX_MOTION", "0")
    p = Pulse(period_ms=100.0, cycles=8.0)
    assert p.step(1.0) == 2.0
    assert p.done is True


def test_run_advances_a_fixed_span_then_stops():
    r = Run(duration_ms=240, span=0.072, start=0.0)
    values = run_to_end(r, dt=40.0)
    assert r.done is True
    assert values[-1] == pytest.approx(0.06)  # 6 帧里走 5 个间隔
    assert all(v <= 0.072 for v in values)


def test_run_honours_wrap_and_start():
    r = Run(duration_ms=100, span=0.4, wrap=1.0, start=0.9)
    assert r.step(50.0) == pytest.approx((0.9 + 0.2) % 1.0)


def test_run_with_switch_off_does_not_move(monkeypatch):
    monkeypatch.setenv("WJX_MOTION", "0")
    r = Run(duration_ms=240, span=0.072, start=0.3)
    assert r.step(40.0) == 0.3
    assert r.done is True


def test_blink_flips_every_half_period():
    b = Blink(period_ms=1000, on=True)
    assert b.step(400.0) is True
    assert b.step(200.0) is False   # 累计 600ms 越过半个周期
    assert b.step(500.0) is True    # 累计 1100ms 越过第二个半周期
    b.cancel()
    assert b.done is True
    assert b.step(500.0) is True


def test_blink_with_switch_off_is_stuck_on(monkeypatch):
    monkeypatch.setenv("WJX_MOTION", "0")
    b = Blink(period_ms=1000, on=False)
    assert b.step(1.0) is True
    assert b.done is True


def test_text_loop_cycles_the_phrases():
    tl = TextLoop(["正在建浏览器", "正在等题目渲染", "正在读取选项"], period_ms=900)
    assert tl.step(0.0) == "正在建浏览器"
    assert tl.step(900.0) == "正在等题目渲染"
    assert tl.step(1800.0) == "正在建浏览器"
    assert tl.done is False


def test_text_loop_stops_when_the_wait_is_over():
    tl = TextLoop(["正在等题目渲染", "正在读取选项"], period_ms=900)
    tl.step(900.0)
    tl.cancel()
    assert tl.done is True
    assert tl.step(9999.0) == "正在读取选项"  # 冻结在取消那一刻的短语


def test_text_loop_with_one_phrase_never_advances():
    tl = TextLoop(["等待当前轮收尾"], period_ms=900)
    assert tl.step(5000.0) == "等待当前轮收尾"


def test_text_loop_with_no_phrases_is_empty():
    assert TextLoop([]).step(100.0) == ""


def test_text_loop_with_switch_off_freezes_on_the_first_phrase(monkeypatch):
    monkeypatch.setenv("WJX_MOTION", "0")
    tl = TextLoop(["甲", "乙"], period_ms=900)
    assert tl.step(5000.0) == "甲"


# ---------------------------------------------------------------- 层边界


def test_primitives_expose_their_current_value_and_survive_extra_steps():
    """``.value`` 是给"停摆之后还要重画一次"的调用方读的，逐条钉住；
    顺带钉住 done 之后再 ``step()`` 不会把值搅回中间态。"""
    col = Collapse(320, 38, dur=60)
    assert col.step(30.0) == col.value
    run_to_end(col)
    assert col.value == 38

    sc = ScrambleText("就绪", "运行中", rng=random.Random(9), dur=60)
    run_to_end(sc)
    assert sc.step(30.0) == "运行中"

    r = Run(duration_ms=60, span=0.1)
    assert r.step(30.0) == r.value
    run_to_end(r)
    assert r.step(30.0) == r.value

    p = Pulse(period_ms=100.0, cycles=10.0)
    assert p.step(10.0) == pytest.approx(p.value)

    b = Blink(period_ms=100.0, on=True)
    assert b.step(60.0) is False
    assert b.value is False

    tl = TextLoop(["甲", "乙"], period_ms=100.0)
    assert tl.step(100.0) == tl.value


def test_motion_layer_never_touches_tk_or_schedules_anything():
    """这一层的立身之本：不碰 Tk、不自己排定时器，才谈得上逐帧断言。

    扫源码而不是扫 ``sys.modules`` —— 同一 pytest 会话里别的 GUI 用例早就把
    tkinter 导进来了，那个断言在整套跑的时候永远是假的。
    """
    lines = open(motion.__file__, encoding="utf-8").read().splitlines()
    code = [ln.strip() for ln in lines]
    assert not [ln for ln in code
                if ln.startswith(("import tkinter", "from tkinter"))]
    assert not [ln for ln in code if ".after(" in ln]
    assert not [ln for ln in code if ln.startswith(("import tkinter.ttk",))]


def test_tokens_are_the_single_source_for_timing():
    assert (motion.DUR_FAST, motion.DUR_BASE, motion.DUR_SLOW) == (120, 220, 400)
    assert STAGGER_MAX == 24
    # 呼吸灯一圈必须等于改造前"80ms 一步、每步 0.06rad"的节拍
    assert math.isclose(motion.BREATH_CYCLE_MS, 80 * 2 * math.pi / 0.06, rel_tol=1e-3)
    # 光标周期必须等于改造前"530ms 翻转一次"的节拍
    assert motion.CURSOR_PERIOD_MS == 2 * 530
    assert motion.SCAN_FRAME_MS * motion.SCAN_FRAMES == 240
