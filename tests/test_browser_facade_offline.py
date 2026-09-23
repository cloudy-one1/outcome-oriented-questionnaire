"""`src/browser/__init__.py` 这层薄门面的离线契约 —— 关掉 README 缺口表里 52.2% 那条。

`driver_factory` 早在 v2.6 就有替身 driver 的契约，但它上面这层门面反而没人走过：
`create_driver` 是 CLI/GUI 唯一的入口，"edge 走 edge 工厂、chrome 的 `use_uc` 只发给
chrome、`--profile-dir` 的 `user_data_dir` 一路带到工厂"这三条全是**参数接线**，
正是本仓库用 F841 那三条规则专门防的"算了却没传"类 bug；而
`cleanup_browser_state` 是每份答卷之间必走的一次清场，它吞什么异常决定长批次会不会
被一次偶发的 DOM 失效打断。两条都不需要真浏览器。
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selenium.common.exceptions import StaleElementReferenceException  # noqa: E402

import src.browser as facade  # noqa: E402


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "driver"


class FakeCleanupDriver:
    """按真实现发出的三下（cookies + 两段 storage 清理）记账，可在任一下抛错。"""

    _MARK = {
        "window.localStorage.clear();": "localStorage",
        "window.sessionStorage.clear();": "sessionStorage",
    }

    def __init__(self, raise_on: str | None = None) -> None:
        self.raise_on = raise_on
        self.calls: list[str] = []

    def _hit(self, name: str) -> None:
        self.calls.append(name)
        if self.raise_on != name:
            return
        exc: BaseException = (
            StaleElementReferenceException("会话刚被回收") if name == "cookies"
            else RuntimeError("清理语句本身炸了") if name == "localStorage"
            else KeyboardInterrupt()
        )
        raise exc

    def delete_all_cookies(self) -> None:
        self._hit("cookies")

    def execute_script(self, script: str, *_a: Any, **_k: Any) -> Any:
        self._hit(self._MARK.get(script, script))


# ============================================================================
#  create_driver：浏览器分发
# ============================================================================
@pytest.fixture
def factories(monkeypatch: pytest.MonkeyPatch) -> dict[str, Recorder]:
    edge, chrome = Recorder(), Recorder()
    monkeypatch.setattr(facade, "create_edge_driver", edge)
    monkeypatch.setattr(facade, "create_chrome_driver", chrome)
    return {"edge": edge, "chrome": chrome}


@pytest.mark.parametrize("name", ["edge", "EDGE", "Edge", None, ""])
def test_edge_is_the_default_and_case_ignores(name: str | None, factories: dict[str, Recorder]) -> None:
    assert facade.create_driver(name) == "driver"
    assert len(factories["edge"].calls) == 1
    assert factories["chrome"].calls == []


def test_chrome_dispatches_to_the_chrome_factory(factories: dict[str, Recorder]) -> None:
    assert facade.create_driver("chrome") == "driver"
    assert len(factories["chrome"].calls) == 1
    assert factories["edge"].calls == []


def test_every_knob_reaches_the_factory_it_belongs_to(
    factories: dict[str, Recorder],
) -> None:
    """UA / 无头 / profile 目录一个都不能在门面这层掉地上（`--profile-dir` 就死在这里）。"""
    facade.create_driver(
        "edge", user_agent="UA-EDGE", headless=True, user_data_dir="profiles/edge"
    )
    facade.create_driver(
        "chrome", user_agent="UA-CHROME", headless=False, use_uc=True,
        user_data_dir="profiles/chrome",
    )
    assert factories["edge"].calls == [
        {"user_agent": "UA-EDGE", "headless": True, "user_data_dir": "profiles/edge"}
    ]
    assert factories["chrome"].calls == [
        {
            "user_agent": "UA-CHROME",
            "headless": False,
            "use_uc": True,
            "user_data_dir": "profiles/chrome",
        }
    ]


def test_use_uc_never_leaks_into_the_edge_factory(
    factories: dict[str, Recorder],
) -> None:
    """undetected-chromedriver 只对 Chrome 存在；漏给 Edge 工厂是一个真实参数签名错误。"""
    facade.create_driver("edge", use_uc=True)
    assert "use_uc" not in factories["edge"].calls[0]


def test_an_unknown_browser_fails_loudly_with_the_allowed_values(
    factories: dict[str, Recorder],
) -> None:
    with pytest.raises(ValueError) as err:
        facade.create_driver("firefox")
    assert "firefox" in str(err.value)
    for allowed in facade.BROWSER_TYPES:
        assert allowed in str(err.value)
    assert factories["edge"].calls == [] and factories["chrome"].calls == []


# ============================================================================
#  cleanup_browser_state：每份答卷之间的清场
# ============================================================================
def test_cleanup_clears_cookies_and_both_storages() -> None:
    driver = FakeCleanupDriver()
    facade.cleanup_browser_state(driver)
    assert driver.calls == ["cookies", "localStorage", "sessionStorage"]


def test_a_transient_dom_failure_degrades_and_still_returns() -> None:
    """清场是"最好情况"优化：DOM 偶发失效不该打断长批次，也不该往上抛。"""
    driver = FakeCleanupDriver(raise_on="cookies")
    assert facade.cleanup_browser_state(driver) is None
    assert driver.calls == ["cookies"]              # 第一步就失败则不再执行后两步


def test_a_non_transient_cleanup_failure_is_also_swallowed() -> None:
    """清理语句本身炸了（RuntimeError）也不影响答题 —— 它不是不可恢复的终止信号。"""
    driver = FakeCleanupDriver(raise_on="localStorage")
    assert facade.cleanup_browser_state(driver) is None
    assert driver.calls == ["cookies", "localStorage"]


def test_a_stop_signal_passes_straight_through() -> None:
    """Ctrl+C / SubmissionAborted 一类必须穿过这层兜底 except，否则停止会被吞成"继续"。"""
    driver = FakeCleanupDriver(raise_on="sessionStorage")
    with pytest.raises(KeyboardInterrupt):
        facade.cleanup_browser_state(driver)
