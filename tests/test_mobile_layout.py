"""整页形态诊断（``detection.mobile_layout_notice``）的离线契约测试。

要补的失效面：一份移动端投放（jQuery-Mobile 形态）的问卷交进来，``detect_questions``
一道题都认不出，日志只有"探测不到题目 → 整批失败"。这句话把责任指向我们自己的适配
质量与用户的网络，而真正的原因只是**链接给错了** —— 换一个 PC 版地址就有解，
等改版没有解。所以这一步只负责把两种原因分开说清楚。

另一半同样重要：**不许在别的情况下出声**。判据只用 jQM 独有的 ``ui-*`` 控件类，
而且要求成规模命中：

  * ``.field`` 不能当触发条件 —— 问卷星电脑端模板的题目容器就写着 ``class="field"``
    （真卷实测 ``<div id="divN" class="field" topic="N">``），拿它判断等于到处误报。
  * 读数拿不到（None / 空串 / 页面向量被改写）一律沉默：**没有信号不是"有缺口"**。
  * 同一句话每进程只印一次。

本模块**不**答移动端页面，也不给作答路径加第二套选择器 —— 那些用例里的"不许出现"
就是这条边界的防线。
"""

from __future__ import annotations

import os
import sys
from typing import Any
import pytest
from selenium.common.exceptions import StaleElementReferenceException

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import detection  # noqa: E402
from src.platforms import WJX_MOBILE_LAYOUT_SELECTORS  # noqa: E402


class FakeDriver:
    """只实现 execute_script，并记下参数（本探针是"选择器当参数传"的写法）。"""

    def __init__(self, value: Any = 0) -> None:
        self.value = value
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute_script(self, script: str, *args: Any) -> Any:
        self.calls.append((script, args))
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


@pytest.fixture(autouse=True)
def _fresh_process() -> Any:
    """提示去重是**进程级**状态，用例之间必须互相独立。"""
    detection.reset_mobile_layout_notice()
    yield
    detection.reset_mobile_layout_notice()


# ---------------------------------------------------------------------------
#  出声的那一侧：只在"确实是移动端形态"时说
# ---------------------------------------------------------------------------
def test_mobile_layout_page_gets_one_explanatory_line() -> None:
    line = detection.mobile_layout_notice(FakeDriver(42))

    assert line is not None
    assert "移动端投放形态" in line and "[布局]" in line
    assert "PC 版链接" in line and "/jq/" in line
    # 要说清楚"不是你的问题"，否则用户会去查网络与浏览器
    assert "不是页面没加载" in line and "没有适配它" in line


def test_only_jqm_widget_classes_may_trigger_the_notice() -> None:
    """触发条件只有 jQM 独有的控件类。

    ``.field`` 一旦进这张表，PC 模板上每个题目容器都会算一次命中，这句提示就会
    在本来跑得好的问卷上到处冒出来 —— 而假警的代价是这句真话从此没人看。
    """
    assert ".field" not in WJX_MOBILE_LAYOUT_SELECTORS
    assert all(s.startswith(".ui-") for s in WJX_MOBILE_LAYOUT_SELECTORS)

    d = FakeDriver(42)
    detection.mobile_layout_notice(d)
    assert d.calls[0][1][0] == list(WJX_MOBILE_LAYOUT_SELECTORS), "候选必须当参数传"


# ---------------------------------------------------------------------------
#  沉默的那一侧：没题、题少、读数拿不到
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("hits", [0, 1, 2, None, "", "abc", [], {}, 2.9])
def test_weak_or_unreadable_signal_stays_silent(hits: Any) -> None:
    """读数不成规模（或根本不是个数）→ 一字不说。

    没有信号不是"有缺口"：把 PC 页面说成"你给错链接了"，用户会开始忽略这句真话。
    边界值 3 单独一条用例锁住（``3.9`` 这类形态 JS 的 ``.length`` 给不出来，
    取整后不够门槛就是不够）。
    """
    assert detection.mobile_layout_notice(FakeDriver(hits)) is None


def test_three_hits_is_the_threshold() -> None:
    assert detection.mobile_layout_notice(FakeDriver(2)) is None
    detection.reset_mobile_layout_notice()
    assert detection.mobile_layout_notice(FakeDriver(3)) is not None


def test_pc_page_with_its_own_field_containers_stays_silent() -> None:
    """PC 模板上 ``ui-*`` 一个都没有 → 探针读回来的就是 0，不许出声。"""
    d = FakeDriver(0)
    assert detection.mobile_layout_notice(d) is None
    assert len(d.calls) == 1, "沉默也要只花一次往返，不是一套新选择器再试一遍"


@pytest.mark.parametrize("error", [
    StaleElementReferenceException("页面正在跳转"),
    ValueError("JS 返回了不像话的东西"),
], ids=["transient", "logic"])
def test_probe_failure_never_changes_the_verdict(error: BaseException) -> None:
    """这只是一句说明：它自己出问题必须退化成"没说"，而不是抛给调用方改判一轮。"""
    assert detection.mobile_layout_notice(FakeDriver(error)) is None


def test_stop_signal_still_escapes() -> None:
    """但 Ctrl+C 优先：清理与诊断都不吃中断（异常分层的第一条红线）。"""
    with pytest.raises(KeyboardInterrupt):
        detection.mobile_layout_notice(FakeDriver(KeyboardInterrupt()))


def test_same_line_is_printed_once_per_process() -> None:
    """一批 17 份每份都会走到这里；重复只会把运行信息埋掉。"""
    assert detection.mobile_layout_notice(FakeDriver(80)) is not None
    assert detection.mobile_layout_notice(FakeDriver(80)) is None
    detection.reset_mobile_layout_notice()
    assert detection.mobile_layout_notice(FakeDriver(80)) is not None
