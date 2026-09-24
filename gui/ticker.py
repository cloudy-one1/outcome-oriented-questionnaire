"""GUI 唯一的动效心跳 —— 把散在 ``app.py`` 的三个常驻 ``after`` 循环收成一个可停摆的。

设计稿：``docs/design/DESIGN_motion_primitives.md`` §3 与 §5。

三条不可让的行为，都有对应用例钉着（``tests/test_gui_ticker.py``）：

  1. **空闲时一个 ``after`` 作业都不留。** 改造前呼吸灯 80ms、扫描线 40ms、光标 530ms
     三个循环各排各的，窗口只要开着就一直重绘；现在没有东西在动就不续排。
  2. **``dt`` 按真实流逝时间分发。** Tk 主线程被 Selenium 拖慢时定时器会晚到，
     但补间拿到的仍是真实间隔，动画只会掉帧不会变慢动作。
  3. **某个原语抛异常只摘掉它自己。** 改造前是 ``except Exception: pass``，
     动效静默死掉没人知道；这里摘除并记一条 WARN。

``schedule`` 是注入参数：生产传 ``root.after``，测试传手动队列 —— 这是"动效测得了"
这件事的全部依赖。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, NamedTuple

from gui.motion import enabled

INTERVAL_MS = 30


def monotonic_ms() -> float:
    """毫秒单位的单调时钟。

    ``time.monotonic()`` 返回**秒**，而本模块所有时长都是毫秒 —— 直接拿它当默认
    时钟会让每个补间慢一千倍，真实 GUI 里的表现是"动画永远不动、心跳一直排着"，
    而注入假时钟的单测全绿（开发机上就是这么踩到的）。
    """
    return time.monotonic() * 1000.0


class _Job(NamedTuple):
    """一个登记中的原语：``every_ms`` 是它自己的节拍，``due`` 是下次该动的时候。"""

    prim: Any
    apply: Callable[[Any], None]
    every_ms: int
    last: float
    due: float


class MotionTicker:
    """登记原语 → 到点把 ``dt`` 分发给它们并执行 ``apply(value)``。"""

    def __init__(self,
                 schedule: Callable[[int, Callable[[], None]], Any],
                 log_fn: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = monotonic_ms,
                 interval: int = INTERVAL_MS) -> None:
        self._schedule = schedule
        self._log_fn = log_fn
        self._clock = clock
        self._interval = max(1, int(interval))
        self._items: dict[Any, _Job] = {}
        self._running = False
        self._shutdown = False

    def __len__(self) -> int:
        return len(self._items)

    @property
    def running(self) -> bool:
        return self._running

    def register(self, key: Any, prim: Any, apply: Callable[[Any], None],
                 every_ms: int | None = None) -> None:
        """挂上一个原语。同 ``key`` 后登记的覆盖前一个（状态字连点两次只留最后一次）。

        总开关关掉时不进队列：直接 ``step(0.0)`` 取终值交给 ``apply``，
        于是"关掉动效"既不会留下循环，也不会让界面停在半路。
        """
        if self._shutdown:
            return
        if not enabled():
            try:
                apply(prim.step(0.0))
            except Exception as exc:  # 关掉动效之后这只是一次赋值，抛了也只影响这一处
                self._warn(key, exc)
            return
        now = self._clock()
        step_ms = self._interval if every_ms is None else max(1, int(every_ms))
        self._items[key] = _Job(prim, apply, step_ms, now, now + step_ms)
        self._start()

    def cancel(self, key: Any) -> None:
        self._items.pop(key, None)

    def has(self, key: Any) -> bool:
        """该 key 上是否还挂着活着的原语 —— 循环型原语靠它避免重复登记把相位归零。"""
        return key in self._items

    def shutdown(self) -> None:
        """关窗用：清空并拒绝后续登记，不再留下会打到已销毁控件的回调。"""
        self._shutdown = True
        self._items.clear()
        self._running = False

    def pump(self, now: float | None = None) -> None:
        """把**到期**的原语各推进一次。

        ``dt`` 用每个原语自己的 ``last`` 算，所以慢节拍的原语（光标 530ms）一次拿到
        的就是那 530ms，不会因为心跳是 30ms 而被摊薄或被吞掉。
        """
        now = self._clock() if now is None else float(now)
        for key in list(self._items):
            job = self._items.get(key)
            if job is None or job.due > now:
                continue  # 前一个回调的 apply 里把它取消了
            prim, apply = job.prim, job.apply
            self._items[key] = job._replace(last=now)
            try:
                value = prim.step(now - job.last)
                apply(value)
            except Exception as exc:
                # 比较原语而不是条目本身：上面 _replace 过，元组已经不是同一个了
                cur = self._items.get(key)
                if cur is not None and cur.prim is prim:
                    self._items.pop(key, None)
                self._warn(key, exc)
                continue
            cur = self._items.get(key)
            if cur is None or cur.prim is not prim:
                continue  # apply 里换掉了这个 key，尊重新登记
            if getattr(prim, "done", False):
                self._items.pop(key, None)
            else:
                self._items[key] = cur._replace(due=now + cur.every_ms)

    def _start(self) -> None:
        if self._running or self._shutdown or not self._items:
            return
        self._running = True
        self._schedule(self._delay(), self._on_timer)

    def _delay(self) -> int:
        """离最近一个到期任务还有多久 —— 一个心跳一个作业，不是每个原语一个。"""
        now = self._clock()
        return max(1, int(min(job.due for job in self._items.values()) - now))

    def _on_timer(self) -> None:
        self.pump()
        if self._items:
            self._schedule(self._delay(), self._on_timer)
        else:
            self._running = False  # 空登记即停摆：不续排

    def _warn(self, key: Any, exc: BaseException) -> None:
        if self._log_fn is None:
            return
        self._log_fn(f"动效 {key!r} 已停用（{type(exc).__name__}: {exc}）")
