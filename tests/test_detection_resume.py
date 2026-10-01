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


class _ScriptRecordingDriver:
    """只记录被注入的脚本体。"""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def execute_script(self, script: str) -> Any:
        self.scripts.append(script)
        return "[]"


class TestOptionBlankLocator(unittest.TestCase):
    """v3.0：探测侧与作答侧共用同一段"选项自带填空框"定位 JS。

    为什么专门钉这条：两份实现必然漂移，而漂移的样子很难看穿 —— 探测说
    "这一项要填空"、作答却按另一套判据找不到框，于是点击报成功、页面仍然空着，
    最后只剩一个看不出原因的 unknown 提交。
    """

    def test_both_detection_scripts_carry_the_locator(self) -> None:
        from src.detection import detect_answered_questions, detect_questions

        drv = _ScriptRecordingDriver()
        detect_questions(drv)
        detect_answered_questions(drv)
        self.assertEqual(len(drv.scripts), 2)
        for script in drv.scripts:
            self.assertIn("function optionBlankInput", script)
            self.assertEqual(
                script.count("function optionBlankInput"), 1,
                "同一次注入里定位函数被定义了两遍",
            )

    def test_detect_questions_reports_blank_options(self) -> None:
        """探测脚本里必须真的写下 blank_options 这个键（否则作答侧永远拿不到）。"""
        from src.detection import detect_questions

        drv = _ScriptRecordingDriver()
        detect_questions(drv)
        self.assertIn("s.blank_options.push(v)", drv.scripts[0])
        # 改判成量表/矩阵后要清掉按选择题记下的结论
        self.assertIn("delete it.blank_options", drv.scripts[0])

    def test_text_field_classification_carries_idcard(self) -> None:
        """v4.2：填空字段分类必须有 idcard（身份证）类目 —— verify 与题干两条路。

        漏了它，证件号的位置会被填进 _random_sentence() 的随机短句，
        这份数据必废；persona.id_card 也因此当了三版的死字段。
        """
        from src.detection import detect_questions

        drv = _ScriptRecordingDriver()
        detect_questions(drv)
        script = drv.scripts[0]
        self.assertIn("field = 'idcard'", script)
        self.assertIn("/身份证|证件/", script, "verify 属性这条平台信号要认身份证")
        self.assertIn("/身份证|证件号|证件号码/", script, "题干兜底也要认身份证")

    def test_answered_scan_treats_unwritten_blank_as_unanswered(self) -> None:
        """勾了带框的项但框是空的 → 该题不算已答，续填才会重做它。"""
        from src.detection import detect_answered_questions

        drv = _ScriptRecordingDriver()
        detect_answered_questions(drv)
        script = drv.scripts[0]
        self.assertIn("blankPending", script)
        self.assertIn("delete answered[parseInt(q)]", script)

    def test_answered_scan_guards_invisible_text_controls(self) -> None:
        """v4.1：已答扫描的填空分支必须带与 detect_questions 相同的隐藏控件守卫。

        真卷上量表每级的标注文字装在 display:none 的 textarea 里（detect_questions
        的注释自证）；缺这条守卫时，那段文字会把未答的量表题报成已答，
        续填扫描整题跳过、完整度自检也发现不了。
        """
        from src.detection import detect_answered_questions, detect_questions

        drv = _ScriptRecordingDriver()
        detect_questions(drv)
        detect_answered_questions(drv)
        probe, answered = drv.scripts[0], drv.scripts[1]
        guard = "if (el.offsetParent === null) return;"
        self.assertIn(guard, probe, "探测侧的守卫是基线，先在")
        self.assertIn(guard, answered, "已答扫描必须带上同一条守卫")
        # 守卫必须落在填空分支（fillables 遍历）里，而不是脚本其它角落
        fill_section = answered.split("4. 填空", 1)[1]
        self.assertTrue(fill_section.lstrip().startswith(guard.split("if")[0].strip())
                        or guard in fill_section,
                        "填空分支里找不到隐藏控件守卫")

    def test_locator_defined_exactly_once_across_src(self) -> None:
        """整个 src 里定位函数只允许有一个定义点（import 不算定义）。"""
        import pathlib

        here = pathlib.Path(__file__).resolve().parent.parent / "src"
        hits = [
            str(p) for p in here.rglob("*.py")
            if "function optionBlankInput" in p.read_text(encoding="utf-8")
        ]
        self.assertEqual(
            [str(p) for p in hits],
            [str(here / "interactions" / "_scripts.py")],
            f"定位 JS 出现了副本：{hits}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
