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

    # ==================================================================
    #  V2 新增：断点续传相关接口
    # ==================================================================
    def test_mark_interrupted_sets_status(self) -> None:
        """mark_interrupted 应把 status 写成 'interrupted'，区别于 finished/failed。"""
        rid = self.db.start_run("https://wjx.example/x", 10, "edge", False)
        self.db.mark_interrupted(rid, success_count=5, fail_count=1, total_elapsed_seconds=42.0)
        row = self.db._query_one("SELECT * FROM runs WHERE id=?", (rid,))
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["success_count"], 5)
        self.assertEqual(row["fail_count"], 1)
        self.assertGreater(row["total_elapsed_seconds"], 0.0)
        self.assertIsNotNone(row["finished_at"])

    def test_find_resumable_run_returns_interrupted(self) -> None:
        """同 URL 下最近一次 interrupted 的 run 应被 find_resumable_run 命中。"""
        # 第一条：3 个月前的旧 run，已 finished —— 不应被恢复
        rid_old = self.db.start_run("https://wjx.example/x", 10, "edge", False)
        self.db._execute(
            "UPDATE runs SET started_at = datetime('now', '-90 days') WHERE id=?",
            (rid_old,),
        )
        self.db.finish_run(rid_old, 10, 0, 100.0, status="finished")

        # 第二条：今天刚中断的 run —— 应被命中
        rid_new = self.db.start_run("https://wjx.example/x", 20, "edge", False)
        self.db.mark_interrupted(rid_new, success_count=8, fail_count=1, total_elapsed_seconds=600.0)

        found = self.db.find_resumable_run("https://wjx.example/x")
        self.assertIsNotNone(found)
        self.assertEqual(int(found["id"]), rid_new)
        self.assertEqual(found["status"], "interrupted")
        self.assertEqual(int(found["success_count"]), 8)

    def test_find_resumable_run_filters_by_url(self) -> None:
        """不同 URL 的中断批次互不影响。"""
        rid_a = self.db.start_run("https://wjx.example/A", 10, "edge", False)
        self.db.mark_interrupted(rid_a, 3, 0, 100.0)

        # 找 B 的 → 应为 None
        self.assertIsNone(self.db.find_resumable_run("https://wjx.example/B"))

    def test_find_resumable_run_ignores_finished(self) -> None:
        """finished / failed 状态的 run 不应被恢复。"""
        rid = self.db.start_run("https://wjx.example/x", 10, "edge", False)
        self.db.finish_run(rid, 5, 5, 100.0, status="failed")
        self.assertIsNone(self.db.find_resumable_run("https://wjx.example/x"))

    def test_find_resumable_run_respects_age_limit(self) -> None:
        """超过 max_age_hours 的旧中断批次不应被恢复。"""
        rid = self.db.start_run("https://wjx.example/x", 10, "edge", False)
        # 把 started_at 拨到 3 天前
        self.db._execute(
            "UPDATE runs SET started_at = datetime('now', '-3 days') WHERE id=?",
            (rid,),
        )
        self.db.mark_interrupted(rid, 3, 0, 100.0)
        # 24 小时窗口外 → None
        self.assertIsNone(self.db.find_resumable_run("https://wjx.example/x", max_age_hours=24))
        # 100 小时窗口内 → 命中
        found = self.db.find_resumable_run("https://wjx.example/x", max_age_hours=100)
        self.assertIsNotNone(found)

    def test_count_done_submissions_reads_success_count(self) -> None:
        """count_done_submissions 直接读 runs.success_count 字段。"""
        rid = self.db.start_run("https://wjx.example/x", 10, "edge", False)
        self.db.mark_interrupted(rid, 7, 1, 300.0)
        self.assertEqual(self.db.count_done_submissions(rid), 7)
        # 不存在的 run_id → 0
        self.assertEqual(self.db.count_done_submissions(99999), 0)

    # ==================================================================
    #  V2.1 新增：weight_config 持久化与反序列化
    # ==================================================================
    def test_start_run_persists_weight_config(self) -> None:
        """start_run 传入 weight_config 时应序列化到 weight_config_json 列。"""
        wc = {
            1: {"type": "single", "weights": [0.2, 0.5, 0.3]},
            2: {"type": "scale", "scale": 5, "weights": [0, 0, 0.1, 0.4, 0.5]},
            3: {"type": "text", "field": "name"},
        }
        rid = self.db.start_run("https://wjx.example/x", 10, "edge", False, weight_config=wc)
        row = self.db._query_one("SELECT weight_config_json FROM runs WHERE id=?", (rid,))
        self.assertIsNotNone(row["weight_config_json"])
        # JSON 合法且能反序列化
        import json as _json
        data = _json.loads(row["weight_config_json"])
        self.assertIn("1", data)  # 键已被转成 str
        self.assertEqual(data["1"]["weights"], [0.2, 0.5, 0.3])
        self.assertEqual(data["2"]["scale"], 5)
        self.assertEqual(data["3"]["field"], "name")

    def test_start_run_weight_config_none_leaves_column_null(self) -> None:
        """weight_config=None 时该列应为 NULL（向后兼容）。"""
        rid = self.db.start_run("https://wjx.example/x", 10, "edge", False, weight_config=None)
        row = self.db._query_one("SELECT weight_config_json FROM runs WHERE id=?", (rid,))
        self.assertIsNone(row["weight_config_json"])

    def test_deserialize_weight_config_roundtrip(self) -> None:
        """序列化 → 反序列化应保持 WEIGHT_CONFIG 兼容的 dict[int, dict] 结构。"""
        wc = {
            1: {"type": "single", "weights": [0.2, 0.5, 0.3]},
            2: {"type": "scale", "scale": 5},
            7: {"type": "matrix_single", "rows": [1, 2], "cols": [1, 2, 3]},
        }
        rid = self.db.start_run("https://wjx.example/x", 10, "edge", False, weight_config=wc)
        row = self.db._query_one("SELECT * FROM runs WHERE id=?", (rid,))

        result = self.Cls.deserialize_weight_config(row)

        self.assertIsInstance(result, dict)
        # 键被转回 int
        self.assertEqual(set(result.keys()), {1, 2, 7})
        self.assertEqual(result[1]["weights"], [0.2, 0.5, 0.3])
        self.assertEqual(result[2]["scale"], 5)
        self.assertEqual(result[7]["rows"], [1, 2])

    def test_deserialize_empty_row_returns_empty_dict(self) -> None:
        """weight_config_json 为 NULL 的 row → 空字典。"""
        rid = self.db.start_run("https://wjx.example/x", 10, "edge", False)  # 不传 wc
        row = self.db._query_one("SELECT * FROM runs WHERE id=?", (rid,))
        self.assertEqual(self.Cls.deserialize_weight_config(row), {})

    def test_deserialize_corrupted_json_returns_empty(self) -> None:
        """损坏的 JSON → 空字典，不抛异常。"""
        rid = self.db.start_run("https://wjx.example/x", 10, "edge", False)
        # 手动写入损坏 JSON
        self.db._execute(
            "UPDATE runs SET weight_config_json = ? WHERE id=?",
            ("{ this is not json", rid),
        )
        row = self.db._query_one("SELECT * FROM runs WHERE id=?", (rid,))
        self.assertEqual(self.Cls.deserialize_weight_config(row), {})

    def test_schema_migration_idempotent_on_old_db(self) -> None:
        """老 DB（无 weight_config_json 列）打开时应自动迁移；新 DB 再次打开不应报错。"""
        # 模拟"老 DB"：手动创建一个 V2.0 schema（无 weight_config_json 列）
        self.db.close()
        import sqlite3 as _sqlite3
        # 在原临时文件上手动建一个老 schema
        conn = _sqlite3.connect(self._tmppath)
        conn.execute("DROP TABLE IF EXISTS runs")
        conn.execute("DROP TABLE IF EXISTS answers")
        # 老的 runs schema（V2.0，无 weight_config_json）
        conn.execute("""
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                survey_url TEXT NOT NULL,
                total_submissions INTEGER NOT NULL,
                browser TEXT NOT NULL,
                use_uc INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running',
                success_count INTEGER NOT NULL DEFAULT 0,
                fail_count INTEGER NOT NULL DEFAULT 0,
                total_elapsed_seconds REAL NOT NULL DEFAULT 0.0,
                error_message TEXT,
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            )
        """)
        # 老的 answers schema（V2.0 完整版，含 run_id）
        conn.execute("""
            CREATE TABLE answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                submission_index INTEGER NOT NULL,
                question_number INTEGER NOT NULL,
                question_type TEXT NOT NULL,
                options_selected TEXT,
                text_answer TEXT,
                elapsed_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
        """)
        # 插入一条老记录
        conn.execute(
            "INSERT INTO runs (survey_url, total_submissions, browser, use_uc) VALUES (?, ?, ?, ?)",
            ("https://wjx.example/old", 5, "edge", 0),
        )
        conn.commit()
        conn.close()

        # 用 SubmissionHistory 重新打开 → 应自动 ALTER TABLE 加列
        from src.history import SubmissionHistory
        db2 = SubmissionHistory(self._tmppath)
        # 验证列已添加
        cols = {r["name"] for r in db2._query("PRAGMA table_info(runs)")}
        self.assertIn("weight_config_json", cols)
        # 老记录应该能正常查询且 weight_config_json 为 NULL
        row = db2._query_one("SELECT * FROM runs WHERE survey_url=?", ("https://wjx.example/old",))
        self.assertIsNone(row["weight_config_json"])
        # 反序列化应返回空字典
        self.assertEqual(SubmissionHistory.deserialize_weight_config(row), {})

        # 幂等性：再次打开不应报错
        db2.close()
        db3 = SubmissionHistory(self._tmppath)
        cols3 = {r["name"] for r in db3._query("PRAGMA table_info(runs)")}
        self.assertIn("weight_config_json", cols3)
        db3.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
