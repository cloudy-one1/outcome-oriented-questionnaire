"""v2.0 — 提交历史 SQLite 持久化模块。

职责：
    - 记录一次批量运行（runs 表）：URL、份数、浏览器、成功/失败数、耗时
    - 记录每道题答案明细（answers 表）：run_id、题号、题型、选中项(JSON)、文本答案、耗时
    - 查询接口：query_runs / query_answers / stats_summary
    - 清理接口：purge_old(days_older_than=N)

设计原则：
    - 零外部依赖，仅用 Python 内置 sqlite3 + json + threading.Lock
    - 进程内线程安全：所有 DB 操作经一把 Lock 串行化。
      前提只有一个连接实例 —— GUI 侧务必复用 SubmissionHistory 单例
      （各开各的连接的话，这把锁跨不了连接，串行化就是空话）。
      跨进程（GUI 开着时再跑 CLI）靠 WAL + busy_timeout 兜。
    - 连接不泄漏：__init__ 开连接，close() 关连接，支持 with 语法
    - schema 版本化：迁移按 PRAGMA user_version 只跑一次，
      含全表扫描的升级脚本绝不放在每次打开库的路径上

使用示例::

    with SubmissionHistory("history.db") as db:
        db.reap_stale_runs()                       # 收尾上次被强杀的批次
        rid = db.start_run(url, 100, "edge", False)
        ... (record_answer 每次记录) ...
        db.finish_run(rid, 98, 2, 1234.56)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)


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
    status                  TEXT    NOT NULL DEFAULT 'running',  -- 'running' | 'finished' | 'failed' | 'interrupted'
    success_count           INTEGER NOT NULL DEFAULT 0,
    fail_count              INTEGER NOT NULL DEFAULT 0,
    total_elapsed_seconds   REAL    NOT NULL DEFAULT 0.0,
    error_message           TEXT,
    weight_config_json      TEXT,                       -- V2.1 断点续传：本次批次的权重配置 JSON
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

-- 注意：(run_id, submission_index, question_number) 的 UNIQUE 索引不放在这里
-- （_SCHEMA_SQL 由 executescript 无条件执行，老库若有重复行会导致 CREATE UNIQUE INDEX
--  直接失败）。唯一索引改在 _apply_migrations 中先 dedup 再创建，保证幂等。
"""

# ============================================================================
#  Schema 迁移：老 DB 升级到 V2.1（新增 weight_config_json 列）
#                    + V2.2（answers 唯一索引 + 重复数据清理）
# ============================================================================
_MIGRATION_ADD_WEIGHT_COLUMN: str = (
    # SQLite 没有 IF NOT EXISTS 的 ADD COLUMN 语法，需要先 PRAGMA table_info 检测
    # 这里只存 SQL 文本，实际执行见 _apply_migrations
    "ALTER TABLE runs ADD COLUMN weight_config_json TEXT;"
)

# answers 唯一索引（V2.2）：保证 (run_id, submission_index, question_number) 唯一
_MIGRATION_ANSWERS_UNIQUE_INDEX: str = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_answers_unique"
    " ON answers(run_id, submission_index, question_number);"
)

# 老库可能已存在重复数据（旧版 record_answer 无幂等保证时 retry 产生）：
# 删除重复行只保留 max(id) 的那条，否则建唯一索引会失败。
# 注意：这是**全表扫描 + 删除**，只允许在版本升级时跑一次（见 _MIGRATIONS），
# 不能放在每次打开库的路径上 —— 此前它每次 GUI 打开历史 Tab 都会执行一遍。
_DEDUP_ANSWERS_SQL: str = """
DELETE FROM answers
WHERE id NOT IN (
    SELECT MAX(id) FROM answers
    GROUP BY run_id, submission_index, question_number
);
"""

# 目标 schema 版本（写入 SQLite 的 ``PRAGMA user_version``）
_SCHEMA_VERSION: int = 2


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2：补 weight_config_json 列 + answers 幂等化（去重 + 唯一索引）。"""
    # SQLite 没有 ALTER TABLE ADD COLUMN IF NOT EXISTS，先看列存不存在
    cur = conn.execute("PRAGMA table_info(runs)")
    cols = {row[1] for row in cur.fetchall()}
    if "weight_config_json" not in cols:
        conn.execute(_MIGRATION_ADD_WEIGHT_COLUMN)

    # 建唯一索引失败的唯一原因就是有重复数据，所以先 dedup
    try:
        conn.execute(_DEDUP_ANSWERS_SQL)
    except sqlite3.Error:
        pass
    try:
        conn.execute(_MIGRATION_ANSWERS_UNIQUE_INDEX)
    except sqlite3.Error:
        # dedup 没清干净（极端并发）时跳过建索引：
        # record_answer 里 DELETE+INSERT 的应用层幂等仍然兜得住
        logger.warning(
            "answers 唯一索引创建失败，重复答案防护降级为应用层幂等",
            exc_info=True,
        )


# 迁移脚本表：key = 升级**到**的版本号。只跑一次，跑完写进 user_version。
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v1_to_v2,
}


def _require_lastrowid(cur: sqlite3.Cursor, what: str) -> int:
    """取 INSERT 的自增主键；lastrowid 为 None 时给出可定位的报错。

    sqlite3 的类型桩把 lastrowid 标成 int | None。直接 int(...) 在 None 上
    抛的是 "TypeError: int() argument must be a string..."，
    调用方看不出是哪条 INSERT 出的问题。
    """
    rid = cur.lastrowid
    if rid is None:
        raise sqlite3.Error(f"{what}: INSERT 未返回 lastrowid")
    return int(rid)


class SubmissionHistory:
    """SQLite 历史记录持久化。线程安全。"""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def __init__(self, db_path: str, busy_timeout_ms: int = 5000) -> None:
        """打开（或创建）SQLite 数据库并执行建表 + 版本化迁移。

        :param db_path: SQLite 文件路径；允许 ':memory:' 做内存库（测试用）。
        :param busy_timeout_ms: 库被其他连接写锁住时的等待上限。
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
            timeout=busy_timeout_ms / 1000.0,
        )
        self._conn.row_factory = sqlite3.Row   # 查询返回 dict-like Row
        self._lock: threading.Lock = threading.Lock()

        # 开启外键级联（purge_old 需要删 runs 的同时删 answers）
        self._conn.execute("PRAGMA foreign_keys = ON;")
        # 显式忙等超时：默认 5s 太短且报错被上层静默吞掉，导致 runs 行永停 running。
        # 这里同时把它调大，并在下方开 WAL 让"GUI 读 + CLI 写"不再互相阻塞。
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)};")
        # WAL：:memory: 不支持（会原样返回 'memory'），故不检查结果、也不报错。
        # 有了 WAL，历史 Tab 的读查询不会再和批量提交的写事务抢同一把 RESERVED 锁。
        try:
            self._conn.execute("PRAGMA journal_mode = WAL;")
        except sqlite3.Error:
            pass
        # 建表
        with self._locked():
            self._conn.executescript(_SCHEMA_SQL)
        # 版本化迁移：只跑尚未应用的版本（幂等）
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        """按 ``PRAGMA user_version`` 逐版升级，跑完立刻落版本号。

        为什么要有版本号：v2.2 的"去重 + 建唯一索引"里含一次**全表**
        ``DELETE ... WHERE id NOT IN (SELECT MAX(id) ... GROUP BY ...)``。
        此前它写在构造路径上，于是每次打开库（GUI 每点一次历史 Tab）都要重扫一遍，
        而且是在 Tk 主线程上。有了版本号之后它只在真正需要升级的老库上跑一次。
        """
        with self._locked():
            row = self._conn.execute("PRAGMA user_version").fetchone()
            current = int(row[0]) if row else 0
            for version in sorted(_MIGRATIONS):
                if version <= current:
                    continue
                _MIGRATIONS[version](self._conn)
                # user_version 不接受参数绑定，只能拼字符串；version 来自
                # _MIGRATIONS 的整型键，不是外部输入。
                self._conn.execute(f"PRAGMA user_version = {int(version)};")

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
        weight_config: Optional[dict] = None,
    ) -> int:
        """开始一次批量运行，插入 runs 表并返回 run_id。

        :param weight_config: V2.1 断点续传 —— 把本次批次的权重配置序列化为 JSON
                              存入 ``runs.weight_config_json``，下次启动时可反序列化恢复。
                              传 None 则留空，下次无法续传权重。
        """
        wc_json: Optional[str] = None
        if weight_config is not None:
            # 序列化时键名一律转 str（JSON 规范要 str key）；
            # 反序列化时再转回 int（WEIGHT_CONFIG 是 dict[int, dict]）
            serializable: dict[str, Any] = {
                str(k): (v if isinstance(v, dict) else {"value": v})
                for k, v in weight_config.items()
            }
            wc_json = json.dumps(serializable, ensure_ascii=False)

        sql = (
            "INSERT INTO runs (survey_url, total_submissions, browser, use_uc, status,"
            " weight_config_json)"
            " VALUES (?, ?, ?, ?, 'running', ?)"
        )
        with self._locked():
            cur = self._conn.execute(
                sql,
                (
                    survey_url,
                    int(total_submissions),
                    browser,
                    1 if use_uc else 0,
                    wc_json,
                ),
            )
            return _require_lastrowid(cur, "start_run")

    @staticmethod
    def deserialize_weight_config(row: sqlite3.Row) -> dict[int, dict]:
        """从 runs row 反序列化 weight_config_json，返回 WEIGHT_CONFIG 兼容的 dict。

        - 空 / 损坏 JSON → 返回空 dict（调用方应判断空，避免覆盖已有配置）
        - JSON 合法 → 把 str 键转回 int 键
        - 单值字段（如 scale 的整数权重）会保留原结构
        """
        raw = row["weight_config_json"] if "weight_config_json" in row.keys() else None
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        result: dict[int, dict] = {}
        for k, v in data.items():
            try:
                qi = int(k)
            except (ValueError, TypeError):
                continue
            if isinstance(v, dict):
                result[qi] = v
            else:
                result[qi] = {"value": v}
        return result

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
        """记录单道题的答案明细，返回 answer_id。

        幂等保证（审查 P1-2 修复）：
          - 同一 ``(run_id, submission_index, question_number)`` 唯一约束由
            ``idx_answers_unique`` 索引保证（建表 / 迁移时创建）
          - 使用 ``INSERT OR REPLACE`` 在约束冲突时整体覆盖旧行，
            保证 retry 重试同一份提交不会产生重复记录；最新一次的
            options_selected / text_answer / elapsed_ms 永远覆盖旧值。
          - 即使唯一索引因老库 dedup 失败而未创建，DELETE+INSERT 的兜底
            逻辑（见 ``_record_answer_fallback``）依然保证幂等。
        """
        opts_json = json.dumps(options_selected, ensure_ascii=False) if options_selected is not None else None
        sql = (
            "INSERT OR REPLACE INTO answers (run_id, submission_index, question_number,"
            " question_type, options_selected, text_answer, elapsed_ms, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))"
        )
        with self._locked():
            try:
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
                return _require_lastrowid(cur, "record_answer")
            except sqlite3.IntegrityError:
                # 可读性建议 5.1：收窄到唯一能触发兜底的异常——违反唯一索引
                # （老库 dedup 失败或极端并发产生重复）。
                # 其他异常（连接关闭、磁盘 IO 错误等）必须上抛暴露问题，
                # 不应被误当作"幂等冲突"走 DELETE+INSERT。
                return self._record_answer_fallback(
                    run_id, submission_index, question_number,
                    question_type, opts_json, text_answer, elapsed_ms,
                )

    def _record_answer_fallback(
        self,
        run_id: int,
        submission_index: int,
        question_number: int,
        question_type: str,
        opts_json: Optional[str],
        text_answer: Optional[str],
        elapsed_ms: int,
    ) -> int:
        """应用层 DELETE + INSERT 兜底幂等（仅在 INSERT OR REPLACE 失败时调用）。"""
        del_sql = (
            "DELETE FROM answers WHERE run_id=? AND submission_index=? AND question_number=?"
        )
        ins_sql = (
            "INSERT INTO answers (run_id, submission_index, question_number,"
            " question_type, options_selected, text_answer, elapsed_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        self._conn.execute(
            del_sql,
            (int(run_id), int(submission_index), int(question_number)),
        )
        cur = self._conn.execute(
            ins_sql,
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
        return _require_lastrowid(cur, "_record_answer_fallback")

    # ------------------------------------------------------------------
    # 查询 API
    # ------------------------------------------------------------------
    def query_runs(
        self,
        limit: int = 50,
        status: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        """查询 runs，按 started_at 倒序（最新在前）。

        必须带 ``id DESC`` 兜底：``started_at`` 用 ``datetime('now')`` 写入，
        **精度只有 1 秒**，同一秒内建的多个批次排序不确定。
        """
        if status:
            sql = (
                "SELECT * FROM runs WHERE status=?"
                " ORDER BY started_at DESC, id DESC LIMIT ?"
            )
            return self._query(sql, (status, int(limit)))
        sql = "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?"
        return self._query(sql, (int(limit),))

    def find_resumable_run(
        self,
        survey_url: str,
        max_age_hours: int = 24,
    ) -> Optional[sqlite3.Row]:
        """查找同一问卷 URL 下最近一次未完成的 run（status='interrupted' 或 'running'）。

        用于「断点续传」场景：上次批量提交因网络/进程崩溃中断，
        下次启动时调用此方法找到上次的 run_id 与已成功份数 K，
        然后从 K+1 份继续。

        :param survey_url:    问卷 URL（完全匹配，含 hash 片段）
        :param max_age_hours: 只查最近 N 小时内的 run（避免把几天前的老 run 误恢复）
        :return:              Row(run.id, total_submissions, success_count, ...) 或 None
        """
        sql = (
            "SELECT * FROM runs "
            "WHERE survey_url = ? "
            "  AND status IN ('interrupted', 'running') "
            "  AND started_at >= datetime('now', ?) "
            # id DESC 兜底：started_at 只有秒级精度，同秒内建的两个批次若不加兜底，
            # LIMIT 1 命中哪一条是不确定的 —— 续传挑错批次会直接导致重复提交。
            "ORDER BY started_at DESC, id DESC LIMIT 1"
        )
        return self._query_one(sql, (survey_url, f"-{int(max_age_hours)} hours"))

    def count_done_submissions(self, run_id: int) -> int:
        """统计某个 run 已成功提交的份数（success_count 字段，简单可靠）。

        等价于直接读 runs.success_count；保留方法名是为了和
        「answers 表精确统计」未来切换时接口不变。
        """
        sql = "SELECT success_count FROM runs WHERE id = ?"
        row = self._query_one(sql, (int(run_id),))
        return int(row["success_count"]) if row else 0

    def reap_stale_runs(self, stale_after_minutes: int = 60) -> int:
        """把卡在 ``running`` 的孤儿批次改判为 ``failed``，返回处理条数。

        为什么需要：``status='running'`` 只有一处能写（start_run），也只有在
        正常收尾时才会被 finish_run 覆写。进程被强杀 / 断电 / 任务管理器结束
        进程时，收尾代码根本没跑，那一行就永远停在 ``running``。
        而 ``find_resumable_run`` 把 ``running`` 也算可恢复，于是下次启动会
        提示"从这份继续"—— 一个早已死掉的进程留下的批次，页面与浏览器状态
        完全未知，续传它等于赌博（最坏是重复提交）。

        崩溃批次应有的语义是 ``failed``（RunState.mark_crashed 就是这么定的），
        本方法就是把漏掉的那步在下次启动时补上。

        :param stale_after_minutes: 只处理开始时间早于此阈值的行，避免把
                                    **另一个进程正在跑**的批次误判掉
                                    （GUI 开着同时跑 CLI 是正常用法）。
        """
        sql = (
            "UPDATE runs "
            "SET status = 'failed', "
            "    error_message = COALESCE(error_message, '') "
            "        || ' · 未正常收尾，判为崩溃批次', "
            "    finished_at = COALESCE(finished_at, started_at) "
            "WHERE status = 'running' "
            "  AND started_at < datetime('now', ?) "
        )
        with self._locked():
            cur = self._conn.execute(sql, (f"-{int(stale_after_minutes)} minutes",))
            return int(cur.rowcount or 0)

    def mark_interrupted(
        self,
        run_id: int,
        success_count: int,
        fail_count: int,
        total_elapsed_seconds: float = 0.0,
        error_message: Optional[str] = None,
    ) -> None:
        """把 run 标记为「中断」（区别于 finished/failed）：下次启动时可恢复。

        适用场景：
          - 用户主动点「停止」按钮 → 调用本方法
          - 进程异常退出（无法调用，但可在下次启动时通过 find_resumable_run 恢复）
          - 浏览器崩溃但 GUI 进程还在 → 调用本方法后重启浏览器继续
        """
        # 兼容旧调用方：如果有人已经用 finish_run(status='interrupted')，
        # 这里只是更语义化的别名
        self.finish_run(
            run_id,
            success_count=success_count,
            fail_count=fail_count,
            total_elapsed_seconds=total_elapsed_seconds,
            status="interrupted",
            error_message=error_message,
        )

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
