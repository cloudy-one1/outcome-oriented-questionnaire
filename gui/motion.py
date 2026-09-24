"""GUI 动效原语层 —— 纯时钟驱动的状态机，与 Tk 完全无关，也不排任何定时器。

设计稿：``docs/design/DESIGN_motion_primitives.md``。对标 motion-primitives
（React + motion + Tailwind 的网页动效库）**只借时序与分层**，不借代码。

这一层存在的理由是仓库里那条测试债：``tests/test_gui_panels.py`` 明写"刻意不构造
``SurveyGUI``，因为它的 ``__init__`` 会启动动画 after 循环"。把动效做成只吃
``dt_ms`` 的状态机之后，同一套逻辑就能在不起 mainloop、不建控件的前提下逐帧断言。

调用方形如::

    tw = NumberTween(0, 12)
    while not tw.done:
        label.config(text=tw.step(30))
"""

from __future__ import annotations

import math
import os
import random
from collections.abc import Callable, Sequence

# ============================================================================
#  Motion token —— 时长与步长集中在这张表，别再散回 app.py
# ============================================================================

DUR_FAST = 120      # 折叠箭头、hover 态
DUR_BASE = 220      # 数字滚动、进度追赶
DUR_SLOW = 400      # 状态字溶解

STAGGER = 18        # 逐行入场步长
STAGGER_MAX = 24    # 超过这个行数不再逐行入场，直接给终态
TEXT_LOOP_PERIOD = 900

SCAN_FRAME_MS = 40  # 沿用改造前扫描线的节拍
SCAN_FRAMES = 6     # 每条新日志线推进这么多帧后停摆
# 改造前呼吸灯是"每 80ms 推进 0.06rad"，一整圈 = 80 * 2π / 0.06 ≈ 8378ms。
# 这个数不是调出来的审美，是把旧节拍原样搬过来，动效改造不该顺手改掉观感基线。
BREATH_CYCLE_MS = 8378.0
CURSOR_PERIOD_MS = 1060   # 改造前 530ms 翻转一次


def enabled() -> bool:
    """动效总开关：``WJX_MOTION=0`` 整体关掉（对应 web 的 prefers-reduced-motion）。

    关掉时每个原语第一次 ``step()`` 就返回终值，界面立刻是最终态，功能一项不少。
    """
    return os.environ.get("WJX_MOTION", "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def ease_out_cubic(t: float) -> float:
    """默认缓动：先快后慢，端点严格 0→1、单调不回退。"""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    u = 1.0 - t
    return 1.0 - u * u * u


def spring_damped(t: float) -> float:
    """带一点过冲的收尾，只给进度条用（计数类禁止过冲，见 §7A 的单调性断言）。"""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return 1.0 - math.exp(-5.0 * t) * math.cos(6.0 * t)


def _clamp01(t: float) -> float:
    return 0.0 if t <= 0.0 else (1.0 if t >= 1.0 else t)


# ============================================================================
#  一次性原语
# ============================================================================

class NumberTween:
    """计数滚动（参考 animated-number / sliding-number）。

    ``set_target`` 从**当前显示值**继续，不回到起点 —— 长跑时连点两下停止按钮，
    数字跳回 0 再滚一次是最刺眼的假故障。
    """

    def __init__(self, frm: float, to: float, dur: int = DUR_BASE,
                 *, integer: bool = True,
                 ease: Callable[[float], float] = ease_out_cubic) -> None:
        self._value = int(round(frm)) if integer else float(frm)
        self._frm = float(self._value)
        self._to = float(to)
        self._dur = max(1, int(dur))
        self._integer = integer
        self._ease = ease
        self._elapsed = 0.0
        self._done = self._frm == self._to

    @property
    def value(self) -> float | int:
        return self._value

    @property
    def done(self) -> bool:
        return self._done

    def set_target(self, to: float) -> None:
        self._frm = float(self._value)
        self._to = float(to)
        self._elapsed = 0.0
        self._done = self._frm == self._to

    def step(self, dt_ms: float) -> float | int:
        if self._done:
            return self._value
        if not enabled():
            return self._finish()
        self._elapsed += max(0.0, float(dt_ms))
        t = self._elapsed / self._dur
        if t >= 1.0:
            return self._finish()
        raw = self._frm + (self._to - self._frm) * self._ease(_clamp01(t))
        self._value = int(round(raw)) if self._integer else raw
        return self._value

    def _finish(self) -> float | int:
        self._value = int(round(self._to)) if self._integer else self._to
        self._done = True
        return self._value


class ScrambleText:
    """文字溶解（参考 text-scramble）：状态字切换时旧字碎成新字。

    长度从第一帧起就是新文案的长度，未揭示位从"新文案自身的字符 + 固定一小撮
    符号"里取 —— 不引入新随机源，也不出现中途长度跳动导致的 Canvas 抖动。
    """

    _SYMBOLS = "#@%&*+=/<>_·"

    def __init__(self, old: str, new: str, *, rng: random.Random | None = None,
                 dur: int = DUR_SLOW) -> None:
        del old  # 旧文案只作字符集来源之一，不参与逐帧插值
        self._new = new
        self._rng = rng or random.Random()
        self._dur = max(1, int(dur))
        self._elapsed = 0.0
        self._done = False
        pool = set(new) | set(self._SYMBOLS)
        self._pool = "".join(sorted(pool))

    @property
    def done(self) -> bool:
        return self._done

    def step(self, dt_ms: float) -> str:
        if self._done:
            return self._new
        if not enabled():
            self._done = True
            return self._new
        self._elapsed += max(0.0, float(dt_ms))
        t = self._elapsed / self._dur
        if t >= 1.0:
            self._done = True
            return self._new
        revealed = int(len(self._new) * ease_out_cubic(t))
        pool = self._pool
        rng = self._rng
        chars = list(self._new[:revealed])
        chars += [rng.choice(pool) for _ in range(len(self._new) - revealed)]
        return "".join(chars)


class Collapse:
    """卡片折叠的高度补间（参考 accordion / disclosure）。"""

    def __init__(self, h_from: int, h_to: int, dur: int = DUR_BASE) -> None:
        self._frm = int(h_from)
        self._to = int(h_to)
        self._dur = max(1, int(dur))
        self._elapsed = 0.0
        self._value = self._frm
        self._done = self._frm == self._to

    @property
    def value(self) -> int:
        return self._value

    @property
    def done(self) -> bool:
        return self._done

    def step(self, dt_ms: float) -> int:
        if self._done:
            return self._value
        if not enabled():
            self._value = self._to
            self._done = True
            return self._value
        self._elapsed += max(0.0, float(dt_ms))
        t = self._elapsed / self._dur
        if t >= 1.0:
            self._value = self._to
            self._done = True
            return self._value
        span = self._to - self._frm
        self._value = int(round(self._frm + span * ease_out_cubic(t)))
        return self._value


class EasedProgress:
    """进度追赶（参考 scroll-progress）：目标值随时可改，显示值从当前继续。

    默认带一点过冲（给画出来的进度条用）；读数那一侧要传 ``ease_out_cubic``，
    因为"100.0% → 99.8% → 100.0%"这种回弹在文字上是故障不是动效。
    """

    def __init__(self, value: float = 0.0, dur: int = DUR_BASE,
                 ease: Callable[[float], float] = spring_damped) -> None:
        self._value = _clamp01(value)
        self._frm = self._value
        self._to = self._value
        self._dur = max(1, int(dur))
        self._ease = ease
        self._elapsed = 0.0
        self._done = True

    @property
    def value(self) -> float:
        return self._value

    @property
    def done(self) -> bool:
        return self._done

    def set_target(self, to: float) -> None:
        to = _clamp01(to)
        if to == self._to and not self._done:
            return
        self._frm = self._value
        self._to = to
        self._elapsed = 0.0
        self._done = self._frm == self._to

    def step(self, dt_ms: float) -> float:
        if self._done:
            return self._value
        if not enabled():
            self._value = self._to
            self._done = True
            return self._value
        self._elapsed += max(0.0, float(dt_ms))
        t = self._elapsed / self._dur
        if t >= 1.0:
            self._value = self._to
            self._done = True
            return self._value
        span = self._to - self._frm
        self._value = _clamp01(self._frm + span * self._ease(t))
        return self._value


def stagger_delays(n: int, step_ms: int = STAGGER,
                   cap: int = STAGGER_MAX) -> list[float]:
    """逐行入场的延时表（参考 animated-group）。

    长跑 9999 份时历史表不能一行行等动画，所以超过 ``cap`` 一律给 0（直接终态）。
    """
    if n <= 0:
        return []
    if n > cap or not enabled():
        return [0.0] * n
    return [i * step_ms for i in range(n)]


# ============================================================================
#  循环型原语：由调用方 cancel，ticker 不会自己停它们
# ============================================================================

class Pulse:
    """一直推进的相位（呼吸灯）。到 ``cancel()`` 为止，``done`` 平时恒为 False。

    ``period_ms`` 是走完一整圈（``cycles`` 那么多弧度）所需时间，不是步长 ——
    调用方按任意 ``dt`` 喂它都能得到同一节奏。
    """

    def __init__(self, period_ms: float = BREATH_CYCLE_MS,
                 cycles: float = 2.0 * math.pi) -> None:
        self._rate = float(cycles) / max(1.0, float(period_ms))
        self._cycles = float(cycles)
        self._phase = 0.0
        self._cancelled = False

    @property
    def value(self) -> float:
        return self._phase

    @property
    def done(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def step(self, dt_ms: float) -> float:
        if self._cancelled:
            return self._phase
        if not enabled():
            self._phase = self._cycles / 4.0  # 峰值：静止时也要是最亮的那一态
            self._cancelled = True
            return self._phase
        self._phase = (self._phase + max(0.0, float(dt_ms)) * self._rate) % self._cycles
        return self._phase


class Run:
    """定长一段时间的一次性推进（扫描线：每条新日志线跑 SCAN_FRAMES 帧）。"""

    def __init__(self, duration_ms: int = SCAN_FRAME_MS * SCAN_FRAMES,
                 span: float = 0.072, wrap: float = 1.0, start: float = 0.0) -> None:
        self._dur = max(1, int(duration_ms))
        self._span = float(span)
        self._wrap = float(wrap)
        self._start = float(start)
        self._elapsed = 0.0
        self._value = float(start)
        self._done = False

    @property
    def value(self) -> float:
        return self._value

    @property
    def done(self) -> bool:
        return self._done

    def step(self, dt_ms: float) -> float:
        if self._done:
            return self._value
        if not enabled():
            self._done = True
            return self._value
        self._elapsed += max(0.0, float(dt_ms))
        if self._elapsed >= self._dur:
            self._done = True
            return self._value
        self._value = (self._start + self._span * self._elapsed / self._dur) % self._wrap
        return self._value


class Blink:
    """光标闪烁：每半个周期翻转，直到 ``cancel()``。"""

    def __init__(self, period_ms: int = CURSOR_PERIOD_MS, on: bool = True) -> None:
        self._half = max(1, int(period_ms)) / 2.0
        self._elapsed = 0.0
        self._on = bool(on)
        self._cancelled = False

    @property
    def value(self) -> bool:
        return self._on

    @property
    def done(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def step(self, dt_ms: float) -> bool:
        if self._cancelled:
            return self._on
        if not enabled():
            self._on = True
            self._cancelled = True
            return self._on
        self._elapsed += max(0.0, float(dt_ms))
        while self._elapsed >= self._half:
            self._elapsed -= self._half
            self._on = not self._on
        return self._on


class TextLoop:
    """等待态的短语轮播（参考 text-loop）。只登记真实阶段，拿到结果立刻 ``cancel()``。"""

    def __init__(self, phrases: Sequence[str],
                 period_ms: int = TEXT_LOOP_PERIOD) -> None:
        self._phrases = [p for p in phrases if p]
        self._period = max(1, int(period_ms))
        self._elapsed = 0.0
        self._idx = 0
        self._cancelled = False

    @property
    def value(self) -> str:
        return self._phrases[self._idx] if self._phrases else ""

    @property
    def done(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def step(self, dt_ms: float) -> str:
        if self._cancelled or len(self._phrases) <= 1:
            return self.value
        if not enabled():
            return self.value
        self._elapsed += max(0.0, float(dt_ms))
        while self._elapsed >= self._period:
            self._elapsed -= self._period
            self._idx = (self._idx + 1) % len(self._phrases)
        return self.value
