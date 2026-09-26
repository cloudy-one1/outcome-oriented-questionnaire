"""V2.4 新增：GUI ←→ history schema 契约测试。

背景：gui/history_panel.py 曾全面引用不存在的列名（ok_count / q_number /
q_type / recorded_at / note），并把 sqlite3.Row 当 dict 用（Row 没有 .get()），
历史 Tab 每次刷新/导出/清理都失败且被 ``except Exception`` 吞成一条 WARN。
本测试锁定 GUI 消费的字段名与 purge_old 签名，schema 一旦漂移立即报警。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history import SubmissionHistory


class TestHistoryGuiContract(unittest.TestCase):
    """锁定 gui/history_panel.py 依赖的 schema 字段名。"""

    def setUp(self) -> None:
        self.db = SubmissionHistory(":memory:")
        rid = self.db.start_run("https://example.com/survey", 3, "edge", False)
        self.rid = rid
        self.db.record_answer(rid, 1, 1, "single", [0], None, elapsed_ms=120)
        self.db.record_answer(rid, 1, 2, "text", None, "张三", elapsed_ms=300)
        self.db.finish_run(rid, success_count=1, fail_count=0,
                           total_elapsed_seconds=12.5, status="finished")

    def tearDown(self) -> None:
        self.db.close()

    def test_runs_rows_are_dict_convertible_with_expected_columns(self) -> None:
        """GUI 用 dict(row).get(...) 消费 → Row 必须可转 dict 且包含这些列。"""
        runs = [dict(r) for r in self.db.query_runs()]
        self.assertTrue(runs)
        for r in runs:
            for col in ("id", "survey_url", "total_submissions", "status",
                        "success_count", "fail_count", "error_message",
                        "started_at", "finished_at"):
                self.assertIn(col, r)

    def test_runs_rows_have_no_legacy_columns(self) -> None:
        """历史面板曾误用的旧列名不得再出现（防 GUI 侧回退）。"""
        for r in self.db.query_runs():
            keys = set(r.keys())
            self.assertNotIn("ok_count", keys)
            self.assertNotIn("note", keys)

    def test_answers_rows_expose_expected_columns(self) -> None:
        """答题明细列名（question_number/question_type/created_at）锁定。"""
        answers = [dict(a) for a in self.db.query_answers(run_id=self.rid)]
        self.assertEqual(len(answers), 2)
        for a in answers:
            for col in ("run_id", "submission_index", "question_number",
                        "question_type", "options_selected", "text_answer",
                        "elapsed_ms", "created_at"):
                self.assertIn(col, a)
        self.assertEqual(answers[0]["question_number"], 1)
        self.assertEqual(answers[0]["question_type"], "single")
        self.assertEqual(answers[1]["question_type"], "text")

    def test_purge_old_signature_days_older_than(self) -> None:
        """GUI 调用 purge_old(days_older_than=7)——锁定关键字签名防回归。"""
        removed = self.db.purge_old(days_older_than=7)
        self.assertEqual(removed, 0)

    def test_find_resumable_run_finds_interrupted(self) -> None:
        """续传读取侧：24h 内同 URL 的 interrupted run 必须能被找到。"""
        rid2 = self.db.start_run("https://example.com/survey", 5, "edge", False)
        self.db.finish_run(rid2, success_count=2, fail_count=0,
                           total_elapsed_seconds=1.0, status="interrupted")
        row = self.db.find_resumable_run("https://example.com/survey")
        self.assertIsNotNone(row)
        self.assertEqual(int(row["id"]), rid2)
        self.assertIn(dict(row)["status"], ("interrupted", "running"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
