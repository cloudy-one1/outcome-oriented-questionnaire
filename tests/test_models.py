"""``src/models.py`` 的数据契约测试。

覆盖三个仍在生产链路上的模型：

  - QuestionType.from_str 别名解析 + 大小写 + 未知抛 ValueError
  - QuestionType 与字符串字面量 == 兼容（QuestionType.SINGLE == "single"）
  - RunState 状态对象的计数与 history 状态判定

v2.3 曾引入的 4 个题型 dataclass（QuestionData/AnswerData/SubmitResult/
WeightConfigEntry）零生产调用、以"契约文档"名义空挂 9 个版本，v4.5 删除；
本文件当年的往返用例随之退役 —— 死代码不再用测试续命。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.models import (
    QUESTION_TYPE_ALIASES,
    QuestionType,
    RunState,
    normalize_question_type,
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


# ============================================================================
#  v2.6：题型命名表单一真相
# ============================================================================
def test_from_str_and_alias_table_share_one_source_of_truth() -> None:
    """QuestionType.from_str 与 QUESTION_TYPE_ALIASES 不可能再各自漂移。

    此前两张表手写、平行维护：v2.4 把别名收进 QUESTION_TYPE_ALIASES 时，
    from_str 里那份 alias dict 仍独立存在。矩阵题的
    "matrix_single（枚举 value）vs matrix（落库短名）"正是漂移高发点。
    """
    for qtype in QuestionType:
        # 枚举 value 与每个别名都要能被 from_str 认出来
        assert QuestionType.from_str(qtype.value) is qtype
        for alias in qtype.aliases:
            assert QuestionType.from_str(alias) is qtype, alias
            assert QUESTION_TYPE_ALIASES[alias] == qtype.storage_name
        # 以及：每张表对这个题型的说法必须一致
        assert QUESTION_TYPE_ALIASES[qtype.value] == qtype.storage_name
        assert normalize_question_type(qtype.value) == qtype.storage_name
        assert normalize_question_type(qtype.storage_name) == qtype.storage_name


def test_from_str_still_raises_value_error_for_unknown() -> None:
    """契约：未知题型抛 ValueError（调用方靠它兜底成 SINGLE），不能是 KeyError。"""
    with pytest.raises(ValueError):
        QuestionType.from_str("definitely_not_a_type")
    with pytest.raises(ValueError):
        QuestionType.from_str(None)


def test_storage_name_differs_from_value_only_for_matrix() -> None:
    """落库短名与枚举 value 的唯一差异点必须仍只有矩阵题。"""
    drifted = [q for q in QuestionType if q.storage_name != q.value]
    assert [q.name for q in drifted] == ["MATRIX_SINGLE"]
