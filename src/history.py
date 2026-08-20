"""v2.0 — 提交历史 SQLite 持久化模块。

职责：
    - 记录一次批量运行（runs 表）：URL、份数、浏览器、成功/失败数、耗时
    - 记录每道题答案明细（answers 表）：run_id、题号、题型、选中项(JSON)、文本答案、耗时
    - 查询接口：query_runs / query_answers / stats_summary
    - 清理接口：purge_old(days_older_than=N)

设计原则：
    - 零外部依赖，仅用 Python 内置 sqlite3 + json + threading.Lock
    - 线程安全：所有 DB 操作串行化（适合 GUI + 后台线程并发写场景）
    - 连接不泄漏：__init__ 开连接，close() 关连接，支持 with 语法

使用示例::

    with SubmissionHistory("history.db") as db:
        rid = db.start_run(url, 100, "edge", False)
        ... (record_answer 每次记录) ...
        db.finish_run(rid, 98, 2, 1234.56)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional


# ============================================================================
#  SQL 建表语句
# ============================================================================
_SCHEMA_SQL: str = """
-- 一次批量运行的元信息
CREATE TABLE IF NOT EXISTS runs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_url              TEXT    NOT NULL,
    total_submissions       INTEGER NOT NULL,
    browser                 TEXT    NOT NULL,           -- 'edge' | 'chrome'
    use_uc                  INTEGER NOT NULL DEFAULT 0,  -- 0=False, 1=True
    status                  TEXT    NOT NULL DEFAULT 'running',  -- 'running' | 'finished' | 'failed'
    success_count           INTEGER NOT NULL DEFAULT 0,
    fail_count              INTEGER NOT NULL DEFAULT 0,
    total_elapsed_seconds   REAL    NOT NULL DEFAULT 0.0,
    error_message           TEXT,
    started_at              TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at             TEXT
);

-- 单道题的答案明细
CREATE TABLE IF NOT EXISTS answers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL,
    submission_index    INTEGER NOT NULL,            -- 本次运行的第几份提交(1-based)
    question_number     INTEGER NOT NULL,            -- 题号 q1, q2 ... 的数字部分
    question_type       TEXT    NOT NULL,            -- 'single' | 'multi' | 'text' | 'scale' | 'dropdown' | 'matrix'
    options_selected    TEXT,                        -- JSON 数组，选择题选中项索引（0-based）；填空题=NULL
    text_answer         TEXT,                        -- 自由文本内容；选择题=NULL
    elapsed_ms          INTEGER NOT NULL DEFAULT 0,  -- 本题从思考到点完的耗时(ms)
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

-- 常用查询索引
CREATE INDEX IF NOT EXISTS idx_runs_started  ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status   ON runs(status);
CREATE INDEX IF NOT EXISTS idx_answers_run   ON answers(run_id);
CREATE INDEX IF NOT EXISTS idx_answers_qnum  ON answers(question_number);
"""


class SubmissionHistory:
    """SQLite 历史记录持久化。线程安全。"""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def __init__(self, db_path: str) -> None:
        """打开（或创建）SQLite 数据库并执行建表。

        :param db_path: SQLite 文件路径；允许 ':memory:' 做内存库（测试用）。
        """
        self.db_path: str = db_path
        # 同一个库文件，第一次建表时目录必须存在
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

        self._conn: sqlite3.Connection = sqlite3.connect(
            db_path,
            check_same_thread=False,     # 允许跨线程访问（我们自己用 Lock 保护）
            isolation_level=None,        # autocommit 模式：每条 DML 立即落盘，GUI 实时可查
        )
        self._conn.row_factory = sqlite3.Row   # 查询返回 dict-like Row
        self._lock: threading.Lock = threading.Lock()

        # 开启外键级联（purge_old 需要删 runs 的同时删 answers）
        self._conn.execute("PRAGMA foreign_keys = ON;")
        # 建表
        with self._locked():
            self._conn.executescript(_SCHEMA_SQL)

    def close(self) -> None:
        """关闭数据库连接（幂等）。"""
        try:
            with self._lock:
                if self._conn is not None:
                    self._conn.close()
                    self._conn = None  # type: ignore[assignment]
        except Exception:
            pass

    # 支持 with 语法
    def __enter__(self) -> "SubmissionHistory":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 内部：线程安全 + 原始执行封装
    # ------------------------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[None]:
        """获取锁后 yield，保证 DB 访问串行。"""
        with self._lock:
            yield

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        """内部：执行一条 SQL，返回 cursor。调用方需在锁外或锁内使用。"""
        return self._conn.execute(sql, tuple(params))

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """内部：执行查询，返回 Row 列表（测试使用，暴露 public）。"""
        with self._locked():
            cur = self._conn.execute(sql, tuple(params))
            return cur.fetchall()

    def _query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        """内部：单行查询，无结果返回 None（测试使用，暴露 public）。"""
        with self._locked():
            cur = self._conn.execute(sql, tuple(params))
            return cur.fetchone()

    # ------------------------------------------------------------------
    # 写入 API
    # ------------------------------------------------------------------
    def start_run(
        self,
        survey_url: str,
        total_submissions: int,
        browser: str,
        use_uc: bool,
    ) -> int:
        """开始一次批量运行，插入 runs 表并返回 run_id。"""
        sql = (
            "INSERT INTO runs (survey_url, total_submissions, browser, use_uc, status)"
            " VALUES (?, ?, ?, ?, 'running')"
        )
        with self._locked():
            cur = self._conn.execute(
                sql,
                (survey_url, int(total_submissions), browser, 1 if use_uc else 0),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        success_count: int,
        fail_count: int,
        total_elapsed_seconds: float,
        status: str = "finished",
        error_message: Optional[str] = None,
    ) -> None:
        """结束一次批量运行，更新 runs 表计数、耗时、结束时间。"""
        sql = (
            "UPDATE runs SET status=?, success_count=?, fail_count=?,"
            " total_elapsed_seconds=?, error_message=?,"
            " finished_at=datetime('now') WHERE id=?"
        )
        with self._locked():
            self._conn.execute(
                sql,
                (
                    status,
                    int(success_count),
                    int(fail_count),
                    float(total_elapsed_seconds),
                    error_message,
                    int(run_id),
                ),
            )

    def record_answer(
        self,
        run_id: int,
        submission_index: int,
        question_number: int,
        question_type: str,
        options_selected: Optional[list[int]],
        text_answer: Optional[str] = None,
        elapsed_ms: int = 0,
    ) -> int:
        """记录单道题的答案明细，返回 answer_id。"""
        opts_json = json.dumps(options_selected, ensure_ascii=False) if options_selected is not None else None
        sql = (
            "INSERT INTO answers (run_id, submission_index, question_number,"
            " question_type, options_selected, text_answer, elapsed_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        with self._locked():
            cur = self._conn.execute(
                sql,
                (
                    int(run_id),
                    int(submission_index),
                    int(question_number),
                    question_type,
                    opts_json,
                    text_answer,
                    int(elapsed_ms),
                ),
            )
            return int(cur.lastrowid)

    # ------------------------------------------------------------------
    # 查询 API
    # ------------------------------------------------------------------
    def query_runs(
        self,
        limit: int = 50,
        status: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        """查询 runs，按 started_at 倒序（最新在前）。"""
        if status:
            sql = "SELECT * FROM runs WHERE status=? ORDER BY started_at DESC LIMIT ?"
            return self._query(sql, (status, int(limit)))
        sql = "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?"
        return self._query(sql, (int(limit),))

    def query_answers(
        self,
        run_id: Optional[int] = None,
        question_number: Optional[int] = None,
        limit: int = 5000,
    ) -> list[sqlite3.Row]:
        """查询 answers；可按 run_id / 题号过滤。"""
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(int(run_id))
        if question_number is not None:
            clauses.append("question_number = ?")
            params.append(int(question_number))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM answers {where} ORDER BY id ASC LIMIT ?"
        params.append(int(limit))
        return self._query(sql, params)

    def stats_summary(self) -> dict[str, Any]:
        """全库统计：总运行数、总提交数、总成功率。"""
        with self._locked():
            row = self._conn.execute(
                "SELECT COUNT(*) total_runs,"
                " COALESCE(SUM(success_count + fail_count), 0) total_submissions,"
                " COALESCE(SUM(success_count), 0) total_success,"
                " COALESCE(SUM(fail_count), 0) total_fail"
                " FROM runs"
            ).fetchone()
            total = int(row["total_submissions"])
            rate = (row["total_success"] / total) if total > 0 else 0.0
            return {
                "total_runs": int(row["total_runs"]),
                "total_submissions": total,
                "total_success": int(row["total_success"]),
                "total_fail": int(row["total_fail"]),
                "success_rate": round(rate, 4),
            }

    # ------------------------------------------------------------------
    # 清理 API
    # ------------------------------------------------------------------
    def purge_old(self, days_older_than: int) -> int:
        """清理 N 天前的 runs（含级联 answers）；返回被删除的 runs 数量。"""
        with self._locked():
            # 先查要删多少
            q = self._conn.execute(
                "SELECT COUNT(*) c FROM runs WHERE started_at < datetime('now', ?)",
                (f"-{int(days_older_than)} days",),
            ).fetchone()
            target = int(q["c"])
            if target == 0:
                return 0
            self._conn.execute(
                "DELETE FROM runs WHERE started_at < datetime('now', ?)",
                (f"-{int(days_older_than)} days",),
            )
            return target
