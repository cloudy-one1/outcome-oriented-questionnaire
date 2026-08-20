"""v2.0 — 提交历史 SQLite 持久化模块单元测试。

TDD RED 阶段：先写测试。
覆盖：
  1. SubmissionHistory 初始化（含自动建表）
  2. 记录一次运行 start_run / finish_run（成功/失败）
  3. 记录每道题答案明细 record_answer
  4. 按日期范围查询历史 query_runs
  5. 查询单次运行的答案明细 query_answers
  6. 删除 N 天前的老记录 purge_old
  7. 统计总数 / 成功率统计
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSubmissionHistory(unittest.TestCase):
    """SQLite 历史记录模块行为测试。"""

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def setUp(self) -> None:
        """每个用例使用独立临时 DB 文件，避免互相影响。"""
        self._tmpfd, self._tmppath = tempfile.mkstemp(suffix=".db")
        os.close(self._tmpfd)
        # 懒加载：测试会保证模块存在
        from src.history import SubmissionHistory  # type: ignore
        self.Cls = SubmissionHistory
        self.db: Any = SubmissionHistory(self._tmppath)

    def tearDown(self) -> None:
        """清理临时文件。"""
        try:
            self.db.close()
        except Exception:
            pass
        try:
            if os.path.exists(self._tmppath):
                os.unlink(self._tmppath)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 1. 初始化 & 建表
    # ------------------------------------------------------------------
    def test_init_creates_runs_and_answers_tables(self) -> None:
        """初始化后 runs / answers 两张表必须存在。"""
        tables = self.db._query(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = [r[0] for r in tables]
        self.assertIn("runs", names)
        self.assertIn("answers", names)

    # ------------------------------------------------------------------
    # 2. start_run / finish_run
    # ------------------------------------------------------------------
    def test_start_run_returns_positive_run_id(self) -> None:
        """start_run 返回的 run_id 必须为正整数。"""
        rid = self.db.start_run(
            survey_url="https://v.wjx.cn/vm/abc.aspx",
            total_submissions=10,
            browser="edge",
            use_uc=False,
        )
        self.assertIsInstance(rid, int)
        self.assertGreater(rid, 0)

    def test_finish_run_updates_status_success(self) -> None:
        """finish_run 成功场景 → success_count 等字段正确持久化。"""
        rid = self.db.start_run(
            survey_url="https://v.wjx.cn/vm/abc.aspx",
            total_submissions=5,
            browser="chrome",
            use_uc=True,
        )
        self.db.finish_run(
            run_id=rid,
            success_count=4,
            fail_count=1,
            total_elapsed_seconds=123.45,
        )
        row = self.db._query_one("SELECT * FROM runs WHERE id=?", (rid,))
        self.assertEqual(row["status"], "finished")
        self.assertEqual(row["success_count"], 4)
        self.assertEqual(row["fail_count"], 1)
        self.assertAlmostEqual(row["total_elapsed_seconds"], 123.45, places=2)
        self.assertIsNotNone(row["finished_at"])  # finished_at 被写入

    def test_finish_run_with_failed_status(self) -> None:
        """finish_run 标记失败 → status='failed'，仍保留计数。"""
        rid = self.db.start_run("url", 3, "edge", False)
        self.db.finish_run(
            run_id=rid,
            success_count=0,
            fail_count=3,
            total_elapsed_seconds=10.0,
            status="failed",
            error_message="Ctrl+C interrupted",
        )
        row = self.db._query_one("SELECT * FROM runs WHERE id=?", (rid,))
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_message"], "Ctrl+C interrupted")

    # ------------------------------------------------------------------
    # 3. record_answer 答案明细
    # ------------------------------------------------------------------
    def test_record_answer_persists_choices(self) -> None:
        """答案明细（单题）能正确写入 options_selected JSON。"""
        rid = self.db.start_run("url", 1, "edge", False)
        ans_id = self.db.record_answer(
            run_id=rid,
            submission_index=1,
            question_number=7,
            question_type="multi",
            options_selected=[1, 3, 4],     # 多选：第2、4、5个选项（0-indexed）
            elapsed_ms=380,
        )
        self.assertIsInstance(ans_id, int)
        self.assertGreater(ans_id, 0)
        row = self.db._query_one("SELECT * FROM answers WHERE id=?", (ans_id,))
        self.assertEqual(row["question_number"], 7)
        self.assertEqual(row["question_type"], "multi")
        # JSON 字段解析
        import json
        self.assertEqual(json.loads(row["options_selected"]), [1, 3, 4])
        self.assertEqual(row["elapsed_ms"], 380)

    def test_record_answer_for_text_input(self) -> None:
        """填空题：自由文本写入 text_answer 列。"""
        rid = self.db.start_run("url", 1, "edge", False)
        aid = self.db.record_answer(
            run_id=rid,
            submission_index=1,
            question_number=99,
            question_type="text",
            options_selected=None,
            text_answer="张三 13800138000",
            elapsed_ms=1500,
        )
        row = self.db._query_one("SELECT * FROM answers WHERE id=?", (aid,))
        self.assertEqual(row["question_type"], "text")
        self.assertEqual(row["text_answer"], "张三 13800138000")
        self.assertIsNone(row["options_selected"])

    # ------------------------------------------------------------------
    # 4. 查询历史
    # ------------------------------------------------------------------
    def test_query_runs_returns_all(self) -> None:
        """query_runs() 默认返回全部，按 started_at 倒序。"""
        for i in range(3):
            rid = self.db.start_run(f"url{i}", 10, "edge", False)
            self.db.finish_run(rid, 10, 0, 60.0)
            time.sleep(0.01)
        runs = self.db.query_runs(limit=100)
        self.assertEqual(len(runs), 3)
        # 倒序：最新的第一条
        urls = [r["survey_url"] for r in runs]
        self.assertEqual(urls, ["url2", "url1", "url0"])

    def test_query_runs_with_limit(self) -> None:
        """limit 限制返回条数。"""
        for i in range(5):
            rid = self.db.start_run(f"url{i}", 5, "edge", False)
            self.db.finish_run(rid, 5, 0, 10.0)
        runs = self.db.query_runs(limit=2)
        self.assertEqual(len(runs), 2)

    def test_query_answers_by_run(self) -> None:
        """query_answers(run_id=X) 只返回对应 run 的答案。"""
        r1 = self.db.start_run("url1", 1, "edge", False)
        r2 = self.db.start_run("url2", 1, "edge", False)
        self.db.record_answer(r1, 1, 1, "single", [0], None, 100)
        self.db.record_answer(r2, 1, 1, "single", [2], None, 100)
        a1 = self.db.query_answers(run_id=r1)
        a2 = self.db.query_answers(run_id=r2)
        self.assertEqual(len(a1), 1)
        self.assertEqual(len(a2), 1)
        import json
        self.assertEqual(json.loads(a1[0]["options_selected"]), [0])
        self.assertEqual(json.loads(a2[0]["options_selected"]), [2])

    # ------------------------------------------------------------------
    # 5. 清理 / 统计
    # ------------------------------------------------------------------
    def test_purge_old_removes_stale_runs(self) -> None:
        """purge_old(days=0) 会删除 runs 及关联 answers。"""
        rid = self.db.start_run("url", 5, "edge", False)
        self.db.finish_run(rid, 5, 0, 10.0)
        aid = self.db.record_answer(rid, 1, 1, "single", [0], None, 100)
        # 手动把 started_at 往回拨 2 天（SQL 日期函数）
        self.db._execute(
            "UPDATE runs SET started_at = datetime('now', '-2 days') WHERE id=?",
            (rid,),
        )
        deleted = self.db.purge_old(days_older_than=1)
        self.assertGreaterEqual(deleted, 1)  # 至少删了 1 条 runs
        # 级联删除：answers 同步清理
        remaining = self.db._query_one("SELECT COUNT(*) c FROM answers WHERE id=?", (aid,))
        self.assertEqual(remaining["c"], 0)

    def test_stats_summary_empty_db(self) -> None:
        """空库 → 全 0，不抛异常。"""
        s = self.db.stats_summary()
        self.assertEqual(s["total_runs"], 0)
        self.assertEqual(s["total_submissions"], 0)
        self.assertEqual(s["success_rate"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
