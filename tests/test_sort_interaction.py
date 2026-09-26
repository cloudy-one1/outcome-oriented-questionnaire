"""点击式排序题（``js_fill_sort(mode="click")``）的离线契约 —— 关掉当年 README 缺口表里 22.2% 那条。

为什么这条最值得钉：真卷实测（v3.1，2026-09-22）确认新形态的隐藏域 ``value`` **恒为
1,2,3 不变**，排名只活在 ``li`` 的 DOM 顺序里 —— 照老办法往第一个隐藏框写 "3,1,2"
会交上去一份脏数据，而"看起来答了"。这套状态机（清残留 → 逐项点 → 每项都回读名次 →
点不上就整题判失败）此前只有 E2E 走过，函数体在离线测试里一次都没执行过。

``FakeSortList`` 按真卷那三条语义建模：点未勾项 = 追加到队尾并拿到下一个名次；点已勾项
= **取消**（不是排到第二位）；名次要过 ``lag`` 次读取才写上（平台那 400ms 动画）。
替身只按脚本内容分派，出现任何预期之外的 JS 直接 AssertionError —— "发错脚本"本身也是
要被钉住的一环。
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selenium.common.exceptions import StaleElementReferenceException  # noqa: E402

from src.interactions import sort as sort_mod  # noqa: E402
from src.interactions._scripts import (  # noqa: E402
    click_sort_item_script,
    fill_sort_script,
    sort_state_script,
)
from src.interactions.sort import _state, js_fill_sort  # noqa: E402

Q = 12
_CLICK_VALUE_RE = re.compile(r'var want = ("(?:[^"\\]|\\.)*");')


class Clock:
    """假时钟：``_sleep`` 推进它，于是 ``timeout`` 预算能确定地走完而不真等。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeSortList:
    """点击式排序列表。DOM 顺序 = 已点中的（按点击序）+ 未点中的（原序）。"""

    def __init__(
        self,
        items: tuple[str, ...] = ("1", "2", "3"),
        *,
        ranked: tuple[str, ...] = (),
        lag: int = 0,
        cancel_works: bool = True,
        container: bool = True,
        container_dies_after: int | None = None,
    ) -> None:
        self.items = list(items)
        self.ranked = list(ranked)
        self.lag = lag
        self.cancel_works = cancel_works
        self.container = container
        self.container_dies_after = container_dies_after
        self.scripts: list[str] = []
        self.clicks: list[str] = []
        self.reads = 0
        self._stale_reply = ""
        self._stale_left = 0

    def _snapshot(self) -> str:
        ordered = [{"value": v, "rank": str(i + 1)} for i, v in enumerate(self.ranked)]
        ordered += [{"value": v, "rank": ""} for v in self.items if v not in self.ranked]
        return json.dumps(ordered)

    def state(self) -> str:
        self.reads += 1
        if self.container_dies_after is not None and self.reads > self.container_dies_after:
            self.container = False
        if not self.container:
            return "null"                      # 脚本找不到 ul 时返回 null
        if self._stale_left > 0:
            self._stale_left -= 1
            return self._stale_reply
        return self._snapshot()

    def click(self, value: str) -> bool:
        if not self.container or value not in self.items:
            return False                       # 脚本没有匹配 value 的 li 时 return false
        self.clicks.append(value)
        self._stale_reply, self._stale_left = self._snapshot(), self.lag
        if value in self.ranked:
            if not self.cancel_works:
                return True                    # 点了却没取消：状态机不按预期走
            self.ranked.remove(value)
        else:
            self.ranked.append(value)
        return True

    def execute_script(self, script: str, *_a: Any, **_k: Any) -> Any:
        self.scripts.append(script)
        if script == sort_state_script(Q):
            return self.state()
        match = _CLICK_VALUE_RE.search(script)
        if match:
            value = json.loads(match.group(1))
            if script == click_sort_item_script(Q, value):
                return self.click(value)
        raise AssertionError(f"排序作答发出了预期之外的 JS：{script[:90]}")


def answer(
    fake: FakeSortList,
    order: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "click",
    **kw: Any,
) -> tuple[bool, Clock]:
    """跑一次作答，返回结果与假时钟（``mode`` 默认新形态；老形态另有两组用例）。"""
    clock = Clock()
    monkeypatch.setattr(sort_mod, "time", clock)
    return js_fill_sort(fake, Q, order, mode=mode, _sleep=clock.sleep, **kw), clock


def clicked_values(fake: FakeSortList) -> list[str]:
    return [
        json.loads(match.group(1))
        for script in fake.scripts
        if (match := _CLICK_VALUE_RE.search(script))
    ]


# ============================================================================
#  老形态：一次写逗号串
# ============================================================================
def test_value_mode_sends_exactly_the_comma_script() -> None:
    fake = FakeSortList()
    fake.execute_script = lambda script, *a, **k: (fake.scripts.append(script), True)[1]  # type: ignore
    assert js_fill_sort(fake, Q, [3, 1, 2], mode="value") is True
    assert fake.scripts == [fill_sort_script(Q, [3, 1, 2])]
    # 不写 mode 就是老形态：探测没给出 sort_mode 时的默认必须是这条
    fake2 = FakeSortList()
    fake2.execute_script = lambda script, *a, **k: (fake2.scripts.append(script), True)[1]  # type: ignore
    assert js_fill_sort(fake2, Q, [3, 1, 2]) is True
    assert fake2.scripts == [fill_sort_script(Q, [3, 1, 2])]


@pytest.mark.parametrize("mode", ["value", "", "unknown"], ids=["value", "blank", "unknown"])
def test_anything_but_click_writes_the_comma_string_and_reports_the_reply(mode: str) -> None:
    """探测没说清形态时走老形态；平台回 False 必须如实传出去，不能"看起来答了"。"""
    fake = FakeSortList()
    replies = iter([False, True])
    fake.execute_script = lambda script, *a, **k: (fake.scripts.append(script), next(replies))[1]  # type: ignore
    assert js_fill_sort(fake, Q, [1, 2], mode=mode) is False
    assert js_fill_sort(fake, Q, [1, 2], mode=mode) is True


# ============================================================================
#  新形态：只能按目标顺序点击
# ============================================================================
def test_click_mode_never_touches_the_hidden_field_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSortList()
    ok, _ = answer(fake, [3, 1, 2], monkeypatch)
    assert ok
    assert fill_sort_script(Q, [3, 1, 2]) not in fake.scripts   # 写逗号串 = 脏数据
    assert clicked_values(fake) == ["3", "1", "2"]
    assert fake.ranked == ["3", "1", "2"]


def test_click_mode_sends_option_values_not_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """按 value 点而不是按索引点：点中一项之后平台会重排 li，索引在两次点击之间就飘了。"""
    fake = FakeSortList()
    ok, _ = answer(fake, [2, 3, 1], monkeypatch)              # 调用方给的是 int
    assert ok
    assert clicked_values(fake) == ["2", "3", "1"]


def test_click_mode_waits_for_the_platform_to_write_each_rank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """名次有 ≈400ms 动画：点完不看等于没点。"""
    fake = FakeSortList(lag=2)
    ok, clock = answer(fake, [1, 2, 3], monkeypatch)
    assert ok
    assert fake.ranked == ["1", "2", "3"]
    assert all(step == sort_mod._POLL for step in clock.slept)
    assert len(clock.slept) >= 3 * (fake.lag + 1)             # 每项都轮询到名次写上


def test_click_mode_gives_up_when_a_rank_never_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台迟迟不写名次 → 整题判失败，而且不许闷头点下一项（那会点出个错顺序）。"""
    fake = FakeSortList(lag=99)
    ok, _ = answer(fake, [1, 2, 3], monkeypatch, timeout=1.0)
    assert ok is False
    assert clicked_values(fake) == ["1"]                      # 第一项就没排上，后面两项不该点
    assert len(fake.clicks) == 1


def test_click_mode_clears_residue_before_clicking_the_target_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """续填 / 重试时已勾项的第二次点击是"取消"，不清就把顺序点反。"""
    fake = FakeSortList(ranked=("2", "1"))
    ok, _ = answer(fake, [3, 1, 2], monkeypatch)
    assert ok
    assert clicked_values(fake) == ["1", "2", "3", "1", "2"]  # 残留从队尾往前退
    assert fake.ranked == ["3", "1", "2"]


def test_click_mode_refuses_to_submit_when_residue_will_not_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消不动 = 状态机不按预期走，宁可不交也别硬交一份反的。"""
    fake = FakeSortList(ranked=("2", "1"), cancel_works=False)
    ok, _ = answer(fake, [3, 1, 2], monkeypatch)
    assert ok is False
    assert fake.ranked == ["2", "1"]                          # 一次真作答都没落上


def test_a_residue_item_that_is_no_longer_an_option_fails_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台还标着名次、但那一项已不在选项里（问卷改过）：清残留那一下点不到，别硬往下走。"""
    fake = FakeSortList(items=("1", "2", "3"), ranked=("9",))
    ok, _ = answer(fake, [1, 2, 3], monkeypatch)
    assert ok is False
    assert fake.clicks == []


def test_missing_container_fails_instead_of_looking_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSortList(container=False)
    ok, _ = answer(fake, [1, 2, 3], monkeypatch)
    assert ok is False
    assert fake.clicks == []


def test_container_vanishing_mid_click_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSortList(container_dies_after=1)               # 第一次读状态之后就没了
    ok, _ = answer(fake, [1, 2, 3], monkeypatch)
    assert ok is False


def test_click_on_an_option_that_is_not_there_fails_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSortList()
    ok, _ = answer(fake, [7, 1, 2], monkeypatch)
    assert ok is False
    assert fake.ranked == []                                  # 没点中就不该继续往下走


@pytest.mark.parametrize(
    "raw",
    [None, 42, "not json", '{"value": "1"}', "null"],
    ids=["none", "number", "garbage", "object", "null"],
)
def test_state_readings_the_platform_did_not_shape_are_not_guessed(raw: Any) -> None:
    """``_state`` 只认"JSON 数组"这一种形状，其余一律 None 让上层判失败。"""
    driver = type("D", (), {"execute_script": staticmethod(lambda s, *a, **k: raw)})()
    assert _state(driver, Q) is None


def test_an_empty_state_array_is_the_clean_list_case_not_a_failure() -> None:
    driver = type("D", (), {"execute_script": staticmethod(lambda s, *a, **k: "[]")})()
    assert _state(driver, Q) == []


def test_a_good_state_reading_is_parsed_as_is() -> None:
    payload = json.dumps([{"value": "2", "rank": "1"}])
    driver = type("D", (), {"execute_script": staticmethod(lambda s, *a, **k: payload)})()
    assert _state(driver, Q) == [{"value": "2", "rank": "1"}]


# ============================================================================
#  重试装饰器确实挂在这个函数上
# ============================================================================
def test_transient_js_error_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSortList()
    real = fake.execute_script
    attempts = {"n": 0}

    def flaky(script: str, *a: Any, **k: Any) -> Any:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise StaleElementReferenceException("列表刚被平台重排")
        return real(script, *a, **k)

    fake.execute_script = flaky  # type: ignore
    clock = Clock()
    monkeypatch.setattr(sort_mod, "time", clock)
    assert js_fill_sort(fake, Q, [1, 2, 3], mode="click", _sleep=clock.sleep) is True
    assert attempts["n"] > 1


def test_a_contract_error_is_not_swallowed_by_the_retry_layer() -> None:
    """TypeError 不在 ``_JS_RETRYABLE`` 里：它是代码 bug，必须原样上抛而不是被重试层吞掉。"""
    fake = FakeSortList()

    def boom(script: str, *a: Any, **k: Any) -> Any:
        raise TypeError("探测给的题号不是整数")

    fake.execute_script = boom  # type: ignore
    with pytest.raises(TypeError):
        js_fill_sort(fake, Q, [1, 2, 3])
