"""提交区协议框诊断（``detection.consent_notice``）的离线契约测试。

要补的失效面：移动端投放模板在提交按钮旁挂一个隐私协议同意框（``#checkxiexi``），
它**不在** ``#fieldset1`` 的题目容器里，所以逐题探测、结构对拍、提交前完整度自检
三道判据全都看不见它。于是日志里一切正常、提交却没反应，而"没反应"这句话在本工具
现有的词典里只对应"网络"或"探测漏题"两种解释 —— 都不是这次的原因。

另一半同样重要，而且这一半是**权限**而不是准确性：

  * **不代勾。** 代被调查者签署隐私协议与替他答一道题不是同一件事。同类油猴脚本把这
    一步叫"协议秒签"，本仓库不走这条路（与"只接管 ``alert``、不替页面回答 ``confirm``"
    同一条线）。所以这里断言的是"说一句话"，不是"点一下框"。
  * **不拦停。** 兜底判据是相邻文案含关键词，认错的代价是一单本来能交成的问卷被判失败。
  * 读数拿不到（``None`` / 非字符串 / 坏 JSON / 计数为 0）一律沉默：没有信号不是"有缺口"。
  * 同一句话每进程只印一次。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import pytest
from selenium.common.exceptions import StaleElementReferenceException

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import detection  # noqa: E402
from src.platforms import WJX_CONSENT_IDS, WJX_CONSENT_KEYWORDS  # noqa: E402


class FakeDriver:
    """只实现 execute_script，并记下参数（本探针是"id 与关键词当参数传"的写法）。"""

    def __init__(self, value: Any = None) -> None:
        self.value = value
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute_script(self, script: str, *args: Any) -> Any:
        self.calls.append((script, args))
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


def _hits(n: int = 1, where: str = "#checkxiexi") -> str:
    return json.dumps({"n": n, "where": where})


@pytest.fixture(autouse=True)
def _fresh_process() -> Any:
    """提示去重是**进程级**状态，用例之间必须互相独立。"""
    detection.reset_consent_notice()
    yield
    detection.reset_consent_notice()


# ---------------------------------------------------------------------------
#  出声的那一侧
# ---------------------------------------------------------------------------
def test_unchecked_consent_box_gets_one_line() -> None:
    line = detection.consent_notice(FakeDriver(_hits()))
    assert line is not None
    assert line.startswith("[协议]")
    # 关键事实三件：没勾的是协议框、我们不代勾、这一版仍会照常点提交
    assert "不代勾" in line
    assert "仍会照常点提交" in line


def test_id_and_keywords_are_passed_as_arguments_not_interpolated() -> None:
    driver = FakeDriver(_hits())
    detection.consent_notice(driver)
    script, args = driver.calls[0]
    assert list(WJX_CONSENT_IDS) == args[0]
    assert list(WJX_CONSENT_KEYWORDS) == args[1]
    # 平台常量不许被拼进 JS 源码：那会让"改一处漏一处"重新出现
    for word in (*WJX_CONSENT_IDS, *WJX_CONSENT_KEYWORDS):
        assert word not in script


# ---------------------------------------------------------------------------
#  沉默的那一侧：没有信号 ≠ 有缺口
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [None, "", 0, True, "not json", json.dumps({"n": 0})])
def test_no_signal_stays_silent(value: Any) -> None:
    assert detection.consent_notice(FakeDriver(value)) is None


def test_malformed_payload_stays_silent() -> None:
    assert detection.consent_notice(FakeDriver(json.dumps({"where": "#x"}))) is None


@pytest.mark.parametrize(
    "error",
    [
        StaleElementReferenceException("dom"),
        RuntimeError("boom"),
    ],
)
def test_probe_failure_never_becomes_a_verdict(error: BaseException) -> None:
    """这只是一句提醒：它自己出问题既不抛给调用方，也不产生任何文字。"""
    assert detection.consent_notice(FakeDriver(error)) is None


def test_notice_is_printed_at_most_once_per_process() -> None:
    driver = FakeDriver(_hits(n=2, where="文案含「同意」"))
    assert detection.consent_notice(driver) is not None
    again = detection.consent_notice(driver)
    assert again is None
    # 第二次连页面都不该再问一遍
    assert len(driver.calls) == 1


def test_reset_lets_a_new_process_ask_again() -> None:
    driver = FakeDriver(_hits())
    assert detection.consent_notice(driver) is not None
    detection.reset_consent_notice()
    assert detection.consent_notice(driver) is not None
    assert len(driver.calls) == 2
