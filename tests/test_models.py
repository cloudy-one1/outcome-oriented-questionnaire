"""V2.3 数据模型单元测试。

覆盖 ``src/models.py`` 引入的 4 个 dataclass + QuestionType 枚举:

  - QuestionType.from_str 别名解析 + 大小写 + 未知抛 ValueError
  - QuestionType 与字符串字面量 == 兼容（QuestionType.SINGLE == "single"）
  - QuestionData.from_dict / as_dict 互逆（含 6 类题型）
  - AnswerData.from_dict / as_dict 互逆（含 6 类题型）
  - SubmitResult.success / failed / unknown 工厂 + is_* 属性
  - WeightConfigEntry.from_dict / as_dict 互逆（含 6 类题型）

设计要求（对照「代码可读性改进建议」第三章）:
  - 数据模型必须与现有 dict 接口双向兼容,保证旧代码无破坏
  - QuestionType 用枚举替代散落的字符串字面量,减少拼写错误风险
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.interaction import SUBMIT_FAILED, SUBMIT_SUCCESS, SUBMIT_UNKNOWN
from src.models import (
    AnswerData,
    QuestionData,
    QuestionType,
    RunState,
    SubmitResult,
    WeightConfigEntry,
)


class TestQuestionType(unittest.TestCase):
    """题型枚举行为。"""

    def test_from_str_accepts_canonical_names(self) -> None:
        """6 个标准题型字符串都能正确解析。"""
        self.assertEqual(QuestionType.from_str("single"), QuestionType.SINGLE)
        self.assertEqual(QuestionType.from_str("multi"), QuestionType.MULTI)
        self.assertEqual(QuestionType.from_str("dropdown"), QuestionType.DROPDOWN)
        self.assertEqual(QuestionType.from_str("scale"), QuestionType.SCALE)
        self.assertEqual(QuestionType.from_str("text"), QuestionType.TEXT)
        self.assertEqual(QuestionType.from_str("matrix_single"), QuestionType.MATRIX_SINGLE)

    def test_from_str_accepts_aliases(self) -> None:
        """别名映射:radio→SINGLE / checkbox→MULTI / rating→SCALE 等。"""
        self.assertEqual(QuestionType.from_str("radio"), QuestionType.SINGLE)
        self.assertEqual(QuestionType.from_str("checkbox"), QuestionType.MULTI)
        self.assertEqual(QuestionType.from_str("rating"), QuestionType.SCALE)
        self.assertEqual(QuestionType.from_str("textarea"), QuestionType.TEXT)
        self.assertEqual(QuestionType.from_str("input"), QuestionType.TEXT)
        self.assertEqual(QuestionType.from_str("fillblank"), QuestionType.TEXT)
        self.assertEqual(QuestionType.from_str("matrix"), QuestionType.MATRIX_SINGLE)

    def test_from_str_case_insensitive(self) -> None:
        """大小写不敏感:``SCALE`` / ``Scale`` 都能解析。"""
        self.assertEqual(QuestionType.from_str("SINGLE"), QuestionType.SINGLE)
        self.assertEqual(QuestionType.from_str("Scale"), QuestionType.SCALE)
        self.assertEqual(QuestionType.from_str("TEXT"), QuestionType.TEXT)

    def test_from_str_rejects_unknown(self) -> None:
        """未知题型字符串应抛 ValueError,不应静默落回默认。"""
        with self.assertRaises(ValueError):
            QuestionType.from_str("not_a_real_type")
        with self.assertRaises(ValueError):
            QuestionType.from_str(None)

    def test_enum_str_equality_with_string_literal(self) -> None:
        """QuestionType 继承 str,与字符串字面量 == 比较应返回 True。

        这是兼容性的关键:detection/answering_v2 内部用 ``q["type"] == "single"``
        的旧代码可以直接与 QuestionType.SINGLE 比较,无需 .value。
        """
        self.assertEqual(QuestionType.SINGLE, "single")
        self.assertEqual(QuestionType.MULTI, "multi")
        self.assertEqual(QuestionType.SCALE, "scale")

    def test_str_returns_value_not_enum_name(self) -> None:
        """``str(QuestionType.SINGLE)`` 应返回 ``"single"`` 而非 ``"QuestionType.SINGLE"``。"""
        self.assertEqual(str(QuestionType.SINGLE), "single")
        self.assertEqual(str(QuestionType.MATRIX_SINGLE), "matrix_single")


class TestQuestionDataRoundtrip(unittest.TestCase):
    """QuestionData from_dict / as_dict 互逆性。"""

    def test_single_question_roundtrip(self) -> None:
        """单选题 dict → QuestionData → dict 应等价。"""
        d = {"q": 1, "type": "single", "choices": [1, 2, 3, 4]}
        q = QuestionData.from_dict(d)
        self.assertEqual(q.q, 1)
        self.assertEqual(q.type, QuestionType.SINGLE)
        self.assertEqual(q.choices, [1, 2, 3, 4])
        self.assertEqual(q.as_dict(), d)

    def test_multi_question_roundtrip(self) -> None:
        d = {"q": 2, "type": "multi", "choices": [1, 2, 3]}
        q = QuestionData.from_dict(d)
        self.assertEqual(q.type, QuestionType.MULTI)
        self.assertEqual(q.as_dict(), d)

    def test_dropdown_question_roundtrip(self) -> None:
        """下拉题 choices 可以是 str 列表。"""
        d = {"q": 3, "type": "dropdown", "choices": ["A", "B", "C"]}
        q = QuestionData.from_dict(d)
        self.assertEqual(q.type, QuestionType.DROPDOWN)
        self.assertEqual(q.choices, ["A", "B", "C"])
        self.assertEqual(q.as_dict(), d)

    def test_scale_question_roundtrip(self) -> None:
        d = {"q": 4, "type": "scale", "scale": 5, "scale_min": 1}
        q = QuestionData.from_dict(d)
        self.assertEqual(q.type, QuestionType.SCALE)
        self.assertEqual(q.scale, 5)
        self.assertEqual(q.scale_min, 1)
        self.assertEqual(q.as_dict(), d)

    def test_text_question_roundtrip(self) -> None:
        d = {"q": 5, "type": "text", "field": "name"}
        q = QuestionData.from_dict(d)
        self.assertEqual(q.type, QuestionType.TEXT)
        self.assertEqual(q.field, "name")
        self.assertEqual(q.as_dict(), d)

    def test_matrix_question_roundtrip(self) -> None:
        d = {"q": 6, "type": "matrix_single", "rows": [1, 2, 3], "cols": [1, 2, 3, 4, 5]}
        q = QuestionData.from_dict(d)
        self.assertEqual(q.type, QuestionType.MATRIX_SINGLE)
        self.assertEqual(q.rows, [1, 2, 3])
        self.assertEqual(q.cols, [1, 2, 3, 4, 5])
        self.assertEqual(q.as_dict(), d)

    def test_question_with_unknown_type_falls_back_to_single(self) -> None:
        """未知题型字符串应落回 SINGLE（与 answering_v2 兜底一致）,不抛异常。"""
        q = QuestionData.from_dict({"q": 99, "type": "weird_type", "choices": [1]})
        self.assertEqual(q.type, QuestionType.SINGLE)


class TestAnswerDataRoundtrip(unittest.TestCase):
    """AnswerData from_dict / as_dict 互逆性。"""

    def test_single_answer_roundtrip(self) -> None:
        d = {"type": "single", "selected": [3]}
        a = AnswerData.from_dict(d)
        self.assertEqual(a.type, QuestionType.SINGLE)
        self.assertEqual(a.selected, [3])
        self.assertEqual(a.as_dict(), d)

    def test_multi_answer_roundtrip(self) -> None:
        d = {"type": "multi", "selected": [1, 3]}
        a = AnswerData.from_dict(d)
        self.assertEqual(a.type, QuestionType.MULTI)
        self.assertEqual(a.selected, [1, 3])
        self.assertEqual(a.as_dict(), d)

    def test_scale_answer_roundtrip(self) -> None:
        d = {"type": "scale", "value": 5}
        a = AnswerData.from_dict(d)
        self.assertEqual(a.type, QuestionType.SCALE)
        self.assertEqual(a.value, 5)
        self.assertEqual(a.as_dict(), d)

    def test_text_answer_roundtrip(self) -> None:
        d = {"type": "text", "text": "张三", "field": "name"}
        a = AnswerData.from_dict(d)
        self.assertEqual(a.type, QuestionType.TEXT)
        self.assertEqual(a.text, "张三")
        self.assertEqual(a.field, "name")
        self.assertEqual(a.as_dict(), d)

    def test_matrix_answer_roundtrip(self) -> None:
        d = {"type": "matrix_single", "rows": {1: 3, 2: 5, 3: 1}}
        a = AnswerData.from_dict(d)
        self.assertEqual(a.type, QuestionType.MATRIX_SINGLE)
        self.assertEqual(a.rows, {1: 3, 2: 5, 3: 1})
        self.assertEqual(a.as_dict(), d)


class TestSubmitResult(unittest.TestCase):
    """SubmitResult 工厂与状态属性。"""

    def test_success_factory_and_properties(self) -> None:
        r = SubmitResult.success(elapsed_ms=150, selector="#divSubmit")
        self.assertTrue(r.is_success)
        self.assertFalse(r.is_failed)
        self.assertFalse(r.is_unknown)
        self.assertEqual(r.outcome, SUBMIT_SUCCESS)
        self.assertEqual(r.elapsed_ms, 150)
        self.assertEqual(r.detail.get("selector"), "#divSubmit")

    def test_failed_factory_carries_reason(self) -> None:
        r = SubmitResult.failed("按钮定位失败", selector_list=["#a", "#b"])
        self.assertTrue(r.is_failed)
        self.assertFalse(r.is_success)
        self.assertEqual(r.outcome, SUBMIT_FAILED)
        self.assertEqual(r.reason, "按钮定位失败")
        self.assertEqual(r.detail.get("selector_list"), ["#a", "#b"])

    def test_unknown_factory_carries_reason(self) -> None:
        r = SubmitResult.unknown("等待 URL 变化超时", elapsed_ms=6000, timeout=6.0)
        self.assertTrue(r.is_unknown)
        self.assertFalse(r.is_failed)
        self.assertEqual(r.outcome, SUBMIT_UNKNOWN)
        self.assertEqual(r.reason, "等待 URL 变化超时")
        self.assertEqual(r.detail.get("timeout"), 6.0)


class TestWeightConfigEntryRoundtrip(unittest.TestCase):
    """WeightConfigEntry from_dict / as_dict 互逆性。"""

    def test_single_weight_roundtrip(self) -> None:
        d = {"type": "single", "weights": [0.1, 0.3, 0.5, 0.1]}
        w = WeightConfigEntry.from_dict(d)
        self.assertEqual(w.type, QuestionType.SINGLE)
        self.assertEqual(w.weights, [0.1, 0.3, 0.5, 0.1])
        self.assertEqual(w.as_dict(), d)

    def test_multi_weight_roundtrip_with_count(self) -> None:
        d = {
            "type": "multi",
            "weights": [0.1, 0.2, 0.3, 0.4],
            "count_options": [2, 3],
            "count_weights": [0.4, 0.6],
        }
        w = WeightConfigEntry.from_dict(d)
        self.assertEqual(w.type, QuestionType.MULTI)
        self.assertEqual(w.count_options, [2, 3])
        self.assertEqual(w.count_weights, [0.4, 0.6])
        self.assertEqual(w.as_dict(), d)

    def test_scale_weight_roundtrip(self) -> None:
        d = {"type": "scale", "scale": 5, "weights": [0.0, 0.0, 0.1, 0.4, 0.5]}
        w = WeightConfigEntry.from_dict(d)
        self.assertEqual(w.type, QuestionType.SCALE)
        self.assertEqual(w.scale, 5)
        self.assertEqual(w.as_dict(), d)

    def test_text_weight_roundtrip_with_options(self) -> None:
        d = {"type": "text", "field": "name", "options": ["张三", "李四"]}
        w = WeightConfigEntry.from_dict(d)
        self.assertEqual(w.type, QuestionType.TEXT)
        self.assertEqual(w.field, "name")
        self.assertEqual(w.options, ["张三", "李四"])
        self.assertEqual(w.as_dict(), d)

    def test_matrix_weight_roundtrip(self) -> None:
        d = {
            "type": "matrix_single",
            "rows": [1, 2, 3],
            "cols": [1, 2, 3, 4, 5],
            "row_weights": {
                "1": [0.0, 0.0, 0.1, 0.4, 0.5],
                "2": [0.1, 0.2, 0.3, 0.3, 0.1],
                "3": [1.0, 0.0, 0.0, 0.0, 0.0],
            },
        }
        w = WeightConfigEntry.from_dict(d)
        self.assertEqual(w.type, QuestionType.MATRIX_SINGLE)
        self.assertEqual(w.rows, [1, 2, 3])
        self.assertEqual(w.cols, [1, 2, 3, 4, 5])
        self.assertEqual(w.row_weights["1"], [0.0, 0.0, 0.1, 0.4, 0.5])
        # as_dict 应保留 row_weights 的键类型(JSON 友好 str)
        out = w.as_dict()
        self.assertEqual(out["row_weights"], d["row_weights"])


class TestRunState(unittest.TestCase):
    """RunState 状态对象行为（对照「代码可读性改进建议」第四章）。"""

    def test_initial_state_zero(self) -> None:
        """新建 RunState 默认零值,未中断。"""
        s = RunState()
        self.assertEqual(s.success_count, 0)
        self.assertEqual(s.fail_count, 0)
        self.assertEqual(s.unknown_count, 0)
        self.assertFalse(s.is_interrupted)
        self.assertEqual(s.total_count, 0)
        self.assertFalse(s.has_any_submission)

    def test_mark_success_increments_only_success(self) -> None:
        """mark_success 只累加 success_count,fail_count 不动。"""
        s = RunState()
        s.mark_success()
        s.mark_success()
        self.assertEqual(s.success_count, 2)
        self.assertEqual(s.fail_count, 0)
        self.assertEqual(s.unknown_count, 0)
        self.assertEqual(s.total_count, 2)
        self.assertTrue(s.has_any_submission)

    def test_mark_failure_increments_only_fail(self) -> None:
        s = RunState()
        s.mark_failure()
        self.assertEqual(s.success_count, 0)
        self.assertEqual(s.fail_count, 1)
        self.assertEqual(s.unknown_count, 0)
        self.assertEqual(s.total_count, 1)

    def test_mark_unknown_increments_both_fail_and_unknown(self) -> None:
        """mark_unknown 同时累加 unknown_count 与 fail_count(保守计失败)。

        这是审查 P1-1 的核心决策:unknown 不应被误判为成功,
        应计入失败但同时保留分项以便复盘。
        """
        s = RunState()
        s.mark_unknown()
        self.assertEqual(s.unknown_count, 1)
        self.assertEqual(s.fail_count, 1)
        self.assertEqual(s.success_count, 0)
        self.assertEqual(s.total_count, 1)

    def test_mark_interrupted_sets_flag(self) -> None:
        s = RunState()
        s.mark_interrupted()
        self.assertTrue(s.is_interrupted)

    def test_advance_attempt_returns_increasing_index(self) -> None:
        """advance_attempt 返回从 1 开始递增的尝试序号。"""
        s = RunState()
        self.assertEqual(s.advance_attempt(), 1)
        self.assertEqual(s.advance_attempt(), 2)
        self.assertEqual(s.advance_attempt(), 3)
        self.assertEqual(s.current_attempt, 3)

    def test_history_status_interrupted_when_user_ctrlc(self) -> None:
        """Ctrl+C 中断 + 已跑若干次 → status='interrupted'(find_resumable_run 可恢复)。"""
        s = RunState()
        s.mark_success()
        s.mark_failure()
        s.mark_interrupted()
        self.assertEqual(s.history_status(), "interrupted")
        self.assertEqual(s.history_error_message(), "Ctrl+C 用户中断")

    def test_history_status_interrupted_when_no_submission(self) -> None:
        """极端:连一次都没跑就退出 → status='interrupted'(可恢复)。"""
        s = RunState()
        self.assertEqual(s.history_status(), "interrupted")
        self.assertEqual(s.history_error_message(), "未执行任何提交即退出")

    def test_history_status_finished_when_completed(self) -> None:
        """正常完成(有提交且未中断) → status='finished'。"""
        s = RunState()
        s.mark_success()
        s.mark_failure()
        self.assertEqual(s.history_status(), "finished")
        self.assertIsNone(s.history_error_message())

    def test_mixed_scenario_counts(self) -> None:
        """混合场景:5 成功 + 2 失败 + 1 未知 + 中断 → 计数与状态正确。"""
        s = RunState()
        for _ in range(5):
            s.mark_success()
        for _ in range(2):
            s.mark_failure()
        s.mark_unknown()  # 1 次 unknown → unknown_count=1, fail_count += 1
        s.mark_interrupted()
        self.assertEqual(s.success_count, 5)
        self.assertEqual(s.fail_count, 3)  # 2 + 1(unknown)
        self.assertEqual(s.unknown_count, 1)
        self.assertEqual(s.total_count, 8)
        self.assertTrue(s.is_interrupted)
        self.assertEqual(s.history_status(), "interrupted")
        self.assertEqual(s.history_error_message(), "Ctrl+C 用户中断")

    def test_mark_crashed_yields_failed_status(self) -> None:
        """V2.4:未捕获异常 → mark_crashed → history_status()='failed'。

        此前 GUI/CLI 崩溃批次会被误标 finished 污染成功率（V2.4 修复的 P1 问题）。
        """
        s = RunState()
        s.mark_success()
        s.mark_crashed("TypeError: 'NoneType' object is not callable")
        self.assertEqual(s.history_status(), "failed")
        self.assertIn("运行异常", s.history_error_message() or "")
        self.assertIn("TypeError", s.history_error_message() or "")

    def test_crash_overrides_interrupted(self) -> None:
        """crash_message 优先级高于 interrupted（崩溃批次不可恢复）。"""
        s = RunState()
        s.mark_interrupted()
        s.mark_crashed("boom")
        self.assertEqual(s.history_status(), "failed")

    def test_crash_clearable_for_fresh_batch(self) -> None:
        """新批次默认无崩溃 → 状态语义不受影响。"""
        s = RunState()
        s.mark_success()
        self.assertEqual(s.history_status(), "finished")


if __name__ == "__main__":
    unittest.main(verbosity=2)
