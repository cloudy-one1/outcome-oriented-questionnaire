"""V2.2 审查 P1-1 整改：``_wait_until_submit_effect`` / ``find_and_click_submit``
三态返回值（success / failed / unknown）单元测试。

测试要点：
  - URL 变化 → "success"
  - 出现"提交成功 / 感谢您的参与"等关键词 → "success"
  - 按钮已点击但等待效果超时（什么都没观察到）→ "unknown"（不能再误判为 success）
  - 提交按钮定位失败 / JS 兜底也失败 → "failed"

使用 fake driver 模拟，避免依赖真实浏览器。
"""

from __future__ import annotations

import os
import sys
import threading
import time as _time
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.interaction import (
    SUBMIT_FAILED,
    SUBMIT_SUCCESS,
    SUBMIT_UNKNOWN,
    _wait_until_submit_effect,
    find_and_click_submit,
)


class _FakeElement:
    """模拟 Selenium WebElement。"""

    def __init__(self, found: bool = True) -> None:
        self._found = found

    def click(self) -> None:
        if not self._found:
            raise RuntimeError("element not found")


class _ScriptResult:
    """模拟 execute_script 的可配置返回值 + 页面状态。"""

    def __init__(self) -> None:
        self.current_url_value: str = "https://wjx.cn/start"
        self.body_text: str = ""
        self.has_success_selector: bool = False
        self.find_submit_btn: bool = False  # JS 兜底找按钮

    def change_url(self) -> None:
        self.current_url_value = "https://wjx.cn/done"

    def set_success_text(self, txt: str) -> None:
        self.body_text = txt


class _FakeDriver:
    """模拟 selenium WebDriver 的最小可观测行为。"""

    def __init__(self, state: _ScriptResult) -> None:
        self._state = state
        self.find_element_raises: bool = False  # True = 抛异常模拟元素找不到

    @property
    def current_url(self) -> str:
        return self._state.current_url_value

    def find_element(self, by: Any, selector: str) -> _FakeElement:
        if self.find_element_raises:
            raise RuntimeError(f"找不到元素 {selector}")
        return _FakeElement(found=True)

    def execute_script(self, script: str, *args: Any) -> Any:
        # 成功文本检测脚本（含 "提交成功" / "感谢" / "已完成" 关键词）
        if "提交成功" in script or "感谢" in script or "已完成" in script:
            if self._state.has_success_selector:
                return True
            return any(k in self._state.body_text for k in (
                "提交成功", "感谢您的参与", "感谢您的认真填写", "已完成",
            ))
        # JS 兜底找提交按钮（脚本里含 var sels = [...]）
        if "sels" in script:
            return self._state.find_submit_btn
        # scrollIntoView 等其他脚本 → None
        return None

    def switch_to(self) -> Any:
        class _Switch:
            def frame(self, *a, **kw) -> None: pass
            def default_content(self, *a, **kw) -> None: pass
        return _Switch()


class TestSubmitTriState(unittest.TestCase):
    """三态返回值测试。"""

    # ------------------------------------------------------------------
    #  _wait_until_submit_effect 直接测试
    # ------------------------------------------------------------------
    def test_url_change_returns_success(self) -> None:
        """URL 变化 → 返回 "success"。"""
        state = _ScriptResult()
        driver = _FakeDriver(state)
        # 在第一次轮询后改变 URL（模拟按钮点击后页面跳转）
        def _change() -> None:
            _time.sleep(0.2)
            state.change_url()
        threading.Thread(target=_change, daemon=True).start()
        result = _wait_until_submit_effect(driver, timeout=2.0)
        self.assertEqual(result, SUBMIT_SUCCESS)

    def test_success_text_returns_success(self) -> None:
        """出现"提交成功"关键词 → 返回 "success"。"""
        state = _ScriptResult()
        driver = _FakeDriver(state)
        def _set_text() -> None:
            _time.sleep(0.2)
            state.set_success_text("提交成功！感谢您的参与")
        threading.Thread(target=_set_text, daemon=True).start()
        result = _wait_until_submit_effect(driver, timeout=2.0)
        self.assertEqual(result, SUBMIT_SUCCESS)

    def test_timeout_returns_unknown_not_success(self) -> None:
        """超时（什么都没观察到）→ 返回 "unknown" 而非 "success"。

        这是审查 P1-1 的核心修复点：旧版返回 True（误判为成功），
        新版返回 "unknown"，由上层保守计为失败。
        """
        state = _ScriptResult()  # URL 不变，body 无成功文本
        driver = _FakeDriver(state)
        result = _wait_until_submit_effect(driver, timeout=0.4)
        # 关键断言：不再是 True，而是 "unknown"
        self.assertNotEqual(result, SUBMIT_SUCCESS,
                            "超时不能再被误判为 success（审查 P1-1 修复点）")
        self.assertEqual(result, SUBMIT_UNKNOWN)

    # ------------------------------------------------------------------
    #  find_and_click_submit 端到端测试（public API 三态传播）
    # ------------------------------------------------------------------
    def test_find_and_click_submit_success_on_url_change(self) -> None:
        """按钮找到 + 点击后 URL 变化 → find_and_click_submit 返回 "success"。"""
        state = _ScriptResult()
        driver = _FakeDriver(driver=None) if False else _FakeDriver(state)
        # Selenium 原生 find_element 会成功（默认）；按钮点击后 URL 变化
        # 注意：find_and_click_submit 内部 btn.click() 之前会做一次
        # gaussian_seconds(0.18, 0.04, 0.08, 0.4) 的人类停顿（最坏 ~0.4s），
        # 然后才进入 _wait_until_submit_effect 读取 old_url。如果改 URL 的
        # 时机早于该停顿，old_url 会读到新 URL，后续 cur 不再变化 → 误判 unknown。
        # 故延迟必须 > 0.4s + 余量，取 0.6s。
        def _change_url() -> None:
            _time.sleep(0.6)
            state.change_url()
        threading.Thread(target=_change_url, daemon=True).start()
        result = find_and_click_submit(driver, wait_url_change_timeout=2.0)
        self.assertEqual(result, SUBMIT_SUCCESS)

    def test_find_and_click_submit_unknown_on_timeout(self) -> None:
        """按钮找到 + 点击后无任何效果超时 → find_and_click_submit 返回 "unknown"。

        端到端验证：从 find_and_click_submit 入口进入（不只测内部 _wait），
        确保超时分支的三态语义在 public API 上正确传播为 "unknown"。
        """
        state = _ScriptResult()
        driver = _FakeDriver(state)
        # Selenium 原生 find_element 默认成功 → 走 btn.click() → _wait_until_submit_effect
        # state 不修改 → URL 不变 + 无成功文本 → 超时
        result = find_and_click_submit(driver, wait_url_change_timeout=0.4)
        self.assertEqual(result, SUBMIT_UNKNOWN,
                         "按钮点击后超时应返回 unknown，而非 success（审查 P1-1 修复点）")

    def test_find_and_click_submit_failed_when_no_button(self) -> None:
        """提交按钮全部定位失败（Selenium + JS 兜底都失败）→ 返回 "failed"。"""
        state = _ScriptResult()
        state.find_submit_btn = False  # JS 兜底也找不到按钮
        driver = _FakeDriver(state)
        driver.find_element_raises = True  # Selenium 原生也找不到
        result = find_and_click_submit(driver, wait_url_change_timeout=0.4)
        self.assertEqual(result, SUBMIT_FAILED,
                         "按钮定位失败应是 failed，不是 unknown")

    def test_find_and_click_submit_success_via_js_fallback(self) -> None:
        """Selenium 原生找不到按钮但 JS 兜底成功 + URL 变化 → "success"。"""
        state = _ScriptResult()
        state.find_submit_btn = True  # JS 兜底找到按钮
        driver = _FakeDriver(state)
        driver.find_element_raises = True  # Selenium 原生失败
        # JS 兜底"点击"后 URL 变化（延迟 0.6s，理由同 success_on_url_change 用例）
        def _change_url() -> None:
            _time.sleep(0.6)
            state.change_url()
        threading.Thread(target=_change_url, daemon=True).start()
        result = find_and_click_submit(driver, wait_url_change_timeout=2.0)
        self.assertEqual(result, SUBMIT_SUCCESS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
