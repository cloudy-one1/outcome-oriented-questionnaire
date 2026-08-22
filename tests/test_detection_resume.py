"""V2 断点续填：detect_answered_questions 容错与解析测试。

由于真正的 JS 在浏览器内执行，Python 单元测试只覆盖：
  1. driver.execute_script 返回合法 JSON 数组 → 正确解析为 set[int]
  2. driver.execute_script 返回 None / 非法 JSON → 返回空集合（容错）
  3. driver.execute_script 抛异常 → 函数向上抛（由 pipeline 层 try/except 兜底）

完整 JS 行为验证在 tests/test_e2e_integration.py 的 headless 浏览器里做。
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeDriver:
    """模拟 Selenium WebDriver：只实现 execute_script。"""

    def __init__(self, return_value: Any, raise_exc: Exception | None = None) -> None:
        self._return_value = return_value
        self._raise_exc = raise_exc
        self.call_count = 0

    def execute_script(self, script: str) -> Any:
        self.call_count += 1
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._return_value


class TestDetectAnsweredQuestions(unittest.TestCase):

    def test_returns_set_of_ints_on_valid_json_array(self) -> None:
        """driver 返回 '[1, 3, 5]' → set{1, 3, 5}。"""
        from src.detection import detect_answered_questions
        drv = _FakeDriver(return_value='[1, 3, 5]')
        result = detect_answered_questions(drv)
        self.assertIsInstance(result, set)
        self.assertEqual(result, {1, 3, 5})

    def test_returns_empty_on_none(self) -> None:
        """driver 返回 None（execute_script 没返回值）→ 空集合。"""
        from src.detection import detect_answered_questions
        drv = _FakeDriver(return_value=None)
        result = detect_answered_questions(drv)
        self.assertIsInstance(result, set)
        self.assertEqual(result, set())

    def test_returns_empty_on_invalid_json(self) -> None:
        """driver 返回非 JSON 字符串 → 空集合（容错）。"""
        from src.detection import detect_answered_questions
        drv = _FakeDriver(return_value='not a json')
        result = detect_answered_questions(drv)
        self.assertEqual(result, set())

    def test_returns_empty_on_empty_array(self) -> None:
        """driver 返回 '[]' → 空集合。"""
        from src.detection import detect_answered_questions
        drv = _FakeDriver(return_value='[]')
        result = detect_answered_questions(drv)
        self.assertEqual(result, set())

    def test_propagates_driver_exception(self) -> None:
        """driver.execute_script 抛异常时函数向上抛（由 pipeline 层兜底）。"""
        from src.detection import detect_answered_questions
        drv = _FakeDriver(return_value=None, raise_exc=RuntimeError("session gone"))
        with self.assertRaises(RuntimeError):
            detect_answered_questions(drv)

    def test_filters_non_int_entries(self) -> None:
        """JSON 数组含非整数（合法 JSON 但元素非 int）→ ValueError 被兜底 → 空集合。"""
        from src.detection import detect_answered_questions
        drv = _FakeDriver(return_value='["abc", "def"]')
        result = detect_answered_questions(drv)
        self.assertEqual(result, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
