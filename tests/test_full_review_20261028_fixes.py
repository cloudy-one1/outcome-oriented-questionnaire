"""全面审查报告（data/full_review_20261028）离线可复现缺陷的回归测试。

与报告编号一一对应；页面级（需要真浏览器）的 5 例在
``tests/test_review_20261028_pages_e2e.py``。

    P1-2  并发 start_run 只能启动一个批次执行器（启动锁）
    P1-3  批次配置隔离：运行中的导入被拒、全局热更新不穿透在跑批次、批次后快照摘下
    P1-4  WebUI 导入的高级配置在启动重建时保留（merge）+ 排序顺序渲染回表格
    P1-7  reap_stale_runs 按心跳（最后一次落库）判失活，不按启动时间一刀切
    P2-11 空 count_options 在校验期被拒；生成器对绕过校验的输入给可读报错
    P2-12 信度统计 success 口径：仅明确成功的提交计入，缺状态不补成成功
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest

from src import cli as cli_module
from src import config as cfg_module
from src import plan as plan_module
from src.answering_v2 import generate_answer
from src.config import (
    active_batch_config,
    effective_weight_config,
    set_active_batch_config,
)
from src.config_io import (
    apply_weight_config,
    load_weight_config,
    save_weight_config,
    validate_weight_config,
)
from src.history import SubmissionHistory
from src.reliability import measure_dimensions
from webui.service import (
    WebService,
    _merge_imported_advanced,
    format_weights_for_entry,
)
from webui.session import Availability, RunSession, SessionPaths, ValidationError


# ------------------------------------------------------------------ 假件

class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def __call__(self, kind: str, payload: object) -> None:
        self.events.append((kind, payload))


def make_service(root, **kw) -> tuple[RunSession, WebService]:
    """离线 WebService：探测/扫码等重依赖关闭，其余按用例注入。"""
    sess = RunSession(
        paths=SessionPaths(str(root)),
        availability=Availability(config_io=True, history=False, qr=False,
                                   selenium=True),
        emit=Recorder(),
    )
    params = {
        "save_weight_config": kw.get("save", save_weight_config),
        "load_weight_config": kw.get("load", load_weight_config),
        "validate_weight_config": kw.get("validate", validate_weight_config),
        "history_db_cls": None,
    }
    for name in ("confirm", "run_batch_fn", "spawn", "create_driver",
                 "detect_questions"):
        if name in kw:
            params[name] = kw[name]
    return sess, WebService(sess, **params)


def logs_of(sess: RunSession) -> list[tuple[str, str]]:
    return [(row["tag"], row["text"]) for row in sess.log_lines]


@pytest.fixture(autouse=True)
def _clean_weight_config():
    cfg_module.WEIGHT_CONFIG.clear()
    set_active_batch_config(None)
    yield
    cfg_module.WEIGHT_CONFIG.clear()
    set_active_batch_config(None)


# ================================================================== P1-2

def test_p1_2_second_start_while_first_is_preparing_is_refused(tmp_path) -> None:
    """复现案例 start-race 的修复版：第一个请求停在准备段时，第二个必须被拒。

    复现脚本靠 patch build_weight_config 把第一个请求钉在"检查已过、状态未占"
    的窗口里 —— 现在这个窗口被 _start_lock 原子占用，第二个请求进不来。
    """
    entered = threading.Event()
    release = threading.Event()
    calls: list[dict] = []

    def runner(*args, **kw):
        calls.append({"url": args[0], "target": args[1]})

    sess, svc = make_service(
        tmp_path, run_batch_fn=runner,
        spawn=lambda fn: threading.Thread(target=fn, daemon=True).start(),
    )
    original = svc.build_weight_config

    def paused_build():
        entered.set()              # 锁持有者已进入准备段
        release.wait(10)           # 留足窗口给第二个请求撞锁
        return original()

    svc.build_weight_config = paused_build  # type: ignore[method-assign]

    sess.set_field("url", "https://www.wjx.cn/vm/offline-only.aspx")
    sess.set_field("count", 2)
    sess.set_questions([{"q": 1, "type": "single", "choices": [1, 2]}])
    sess.set_weight_texts({1: "1,0"})

    first = threading.Thread(target=svc.start_run)
    first.start()
    assert entered.wait(5), "第一个请求应能进入准备段"

    second = threading.Thread(target=svc.start_run)
    second.start()
    second.join(5)
    assert not second.is_alive()
    assert calls == [], "第二个请求在第一个还拿着启动锁时不得启动批次"
    assert any(t == "WARN" and "忽略" in x for t, x in logs_of(sess)), (
        "被拒的请求要留痕（WARN），不能无声返回"
    )

    release.set()
    first.join(5)
    assert svc.wait_for_run(5)
    assert len(calls) == 1, "整个并发场景最终只允许一个批次执行器"


# ================================================================== P1-3

def test_p1_3_import_during_run_is_refused(tmp_path) -> None:
    """运行中导入配置：服务端直接拒绝（只禁界面按钮挡不住 API 调用）。"""
    sess, svc = make_service(tmp_path)
    cfg_path = tmp_path / "w.json"
    save_weight_config(str(cfg_path), {1: {"type": "single", "weights": [1, 0]}})
    svc.import_config(str(cfg_path))          # 未运行时正常

    sess.start_run(3)                          # 直接置运行态（不起真批次）
    try:
        save_weight_config(str(cfg_path), {1: {"type": "single", "weights": [0, 1]}})
        with pytest.raises(ValidationError, match="运行中"):
            svc.import_config(str(cfg_path))
    finally:
        sess.finish_run()


def test_p1_3_run_batch_scopes_generation_to_the_snapshot(monkeypatch) -> None:
    """run_batch 包装层：批次内生成读启动快照，热更新穿透不进去，收尾必摘。"""
    seen: dict = {}

    def fake_inner(**kw):
        q = {"q": 1, "type": "single", "choices": [1, 2]}
        seen["active_during"] = active_batch_config()
        seen["first"] = generate_answer(q)
        # 模拟"运行中有人动了全局"（Web 端导入 / 另一入口 apply 的效果）
        apply_weight_config({1: {"type": "single", "weights": [0, 1]}},
                            replace=True)
        seen["second"] = generate_answer(q)
        return 1, 0

    monkeypatch.setattr(cli_module, "_run_batch_inner", fake_inner)
    snap = {1: {"type": "single", "weights": [1, 0]}}
    assert cli_module.run_batch("https://x.test/s", 1, weight_config=snap) == (1, 0)

    assert seen["active_during"] is not None, "批次期间必须挂着快照"
    assert seen["active_during"][1]["weights"] == [1.0, 0.0]
    assert seen["first"]["selected"] == seen["second"]["selected"] == [1], (
        "运行中的全局改动不能穿透进在跑批次（两轮都必须按启动快照 [1,0] 生成）"
    )
    assert active_batch_config() is None, "批次正常结束后快照必须摘下"


def test_p1_3_snapshot_is_detached_even_when_batch_crashes(monkeypatch) -> None:
    def fake_inner(**kw):
        raise RuntimeError("批次炸了")

    monkeypatch.setattr(cli_module, "_run_batch_inner", fake_inner)
    with pytest.raises(RuntimeError):
        cli_module.run_batch("https://x.test/s", 1,
                             weight_config={1: {"type": "single", "weights": [1, 0]}})
    assert active_batch_config() is None, "异常路径同样要摘快照，不得残留到下一批"


def test_p1_3_effective_config_falls_back_to_global_outside_batch() -> None:
    assert effective_weight_config() is cfg_module.WEIGHT_CONFIG

    snap = {1: {"type": "single", "weights": [9, 1]}}
    set_active_batch_config(snap)
    assert effective_weight_config() is active_batch_config()
    assert effective_weight_config()[1]["weights"] == [9, 1]
    set_active_batch_config(None)

    # 快照是深拷贝：挂上之后再改原 dict，快照纹丝不动
    snap2 = {1: {"type": "single", "weights": [1, 0]}}
    set_active_batch_config(snap2)
    snap2[1]["weights"][0] = 99
    assert active_batch_config()[1]["weights"] == [1.0, 0.0]
    set_active_batch_config(None)


def test_p1_3_answering_and_plan_read_the_batch_snapshot() -> None:
    """作答与计划侧查配置的入口都走快照，全局相反时也不动摇。"""
    set_active_batch_config({1: {"type": "single", "weights": [1, 0]}})
    apply_weight_config({1: {"type": "single", "weights": [0, 1]}}, replace=True)

    q = {"q": 1, "type": "single", "choices": [1, 2]}
    picked = {generate_answer(q)["selected"][0] for _ in range(100)}
    assert picked == {1}, "生成器必须读快照 [1,0]，而不是被热更新成 [0,1] 的全局"

    assert plan_module.dimensions_from_config(effective_weight_config()) == {}
    set_active_batch_config({
        1: {"type": "single", "weights": [1, 1], "dimension": "满意度"},
        2: {"type": "single", "weights": [1, 1], "dimension": "满意度",
            "reverse": True},
    })
    assert plan_module.dimensions_from_config(
        effective_weight_config()) == {"满意度": [1, 2]}
    assert plan_module.reverse_from_config(effective_weight_config()) == [2]


# ================================================================== P1-4

def test_p1_4_imported_advanced_fields_survive_the_rebuild(tmp_path) -> None:
    """复现案例 web_import_loses_advanced_fields：count_options 与 sort.order。"""
    sess, svc = make_service(tmp_path)
    sess.set_questions([
        {"q": 1, "type": "multi", "choices": [1, 2, 3], "title": "1.多选测试"},
        {"q": 2, "type": "sort", "items": ["1", "2", "3"], "title": "2.排序测试"},
    ])
    cfg = {
        1: {"type": "multi", "weights": [8, 1, 1],
            "count_options": [2], "count_weights": [1]},
        2: {"type": "sort", "order": ["3", "2", "1"]},
    }
    path = tmp_path / "advanced.json"
    save_weight_config(str(path), cfg)
    svc.import_config(str(path))

    rebuilt = svc.build_weight_config()
    assert rebuilt[1]["count_options"] == [2], "「固定选几个」丢了"
    assert rebuilt[1]["count_weights"] == [1]
    assert rebuilt[2]["order"] == ["3", "2", "1"], "「指定排序」丢了"
    # 导入时表格里也得看得见排序顺序（渲染侧此前漏了 order）
    assert sess.weight_texts[2] == "3,2,1"

    # 用户改了第 1 题权重 → 改动兑现，高级字段不丢
    sess.set_weight_texts({1: "1,1,1"})
    rebuilt2 = svc.build_weight_config()
    assert rebuilt2[1]["weights"] == [1.0, 1.0, 1.0]
    assert rebuilt2[1]["count_options"] == [2]

    # 用户清空第 1 题的格子 → weights 被清掉，count_options 仍保留
    sess.set_weight_texts({1: ""})
    rebuilt3 = svc.build_weight_config()
    assert "weights" not in rebuilt3[1]
    assert rebuilt3[1]["count_options"] == [2]


def test_p1_4_start_run_hands_the_engine_the_merged_config(tmp_path) -> None:
    """端到端：导入 → 启动，引擎拿到的快照里高级字段原样在。"""
    captured: dict = {}

    def runner(*args, **kw):
        captured["snapshot"] = kw["state"].weight_config_snapshot

    sess, svc = make_service(
        tmp_path, run_batch_fn=runner,
        spawn=lambda fn: threading.Thread(target=fn, daemon=True).start(),
    )
    sess.set_questions([
        {"q": 1, "type": "multi", "choices": [1, 2, 3], "title": "1.多选"},
    ])
    path = tmp_path / "adv.json"
    save_weight_config(str(path), {
        1: {"type": "multi", "weights": [8, 1, 1], "count_options": [2]},
    })
    svc.import_config(str(path))
    sess.set_field("url", "https://www.wjx.cn/vm/offline-only.aspx")
    sess.set_field("count", 1)
    svc.start_run()
    assert svc.wait_for_run(5)

    snap = json.loads(json.dumps(captured["snapshot"]))
    assert snap["1"]["count_options"] == [2]
    assert [float(x) for x in snap["1"]["weights"]] == [8.0, 1.0, 1.0]


def test_p1_4_merge_helper_edges() -> None:
    """合并函数的四条边：只在导入里、只在解析里、解析键覆盖基座、清空即删除。"""
    imported = {
        1: {"type": "multi", "weights": [8, 1], "count_options": [2],
            "dimension": "满意度"},
        3: {"type": "scale", "scale": 5, "weights": [0, 0, 0, 0, 1]},
    }
    parsed = {1: {"type": "multi", "weights": [1, 1], "anchor": {"title": "x"}}}
    merged = _merge_imported_advanced(imported, parsed)
    assert merged[1]["weights"] == [1, 1]            # 表格改动兑现
    assert merged[1]["count_options"] == [2]         # 高级字段保留
    assert merged[1]["dimension"] == "满意度"
    assert merged[1]["anchor"] == {"title": "x"}     # 解析侧的键覆盖基座
    assert merged[3] == imported[3]                  # 表上没有的题号原样保留
    assert merged[3] is not imported[3]              # 且是拷贝，不动基座

    # 解析结果缺 weights（格子被清空）→ 基座的 weights 必须删除，不能复活
    parsed_cleared = {1: {"type": "multi", "anchor": {"title": "x"}}}
    merged2 = _merge_imported_advanced(imported, parsed_cleared)
    assert "weights" not in merged2[1]
    assert merged2[1]["count_options"] == [2]

    # 整题从解析结果里消失（表上有 Q1 但解析没产出 = 用户清空/写坏）→
    # 文本字段删除，高级字段保留（表格只动得了文本字段）
    merged3 = _merge_imported_advanced(imported, {}, known_qnums={1})
    assert merged3[1] == {"type": "multi", "count_options": [2],
                          "dimension": "满意度"}
    assert 3 in merged3 and merged3[3]["weights"] == [0, 0, 0, 0, 1]


# ================================================================== P1-7

def test_p1_7_record_touches_heartbeat_only_while_running() -> None:
    db = SubmissionHistory(":memory:")
    try:
        rid = db.start_run("https://x/hb", 5, "edge", False)
        row = db._query_one("SELECT heartbeat_at h FROM runs WHERE id=?", (rid,))
        assert row["h"] is None, "启动时没有落库证据，心跳留空（COALESCE 兜底）"
        db.record_submission_result(rid, 1, "success")
        h1 = db._query_one(
            "SELECT heartbeat_at h FROM runs WHERE id=?", (rid,))["h"]
        assert h1 is not None, "逐份落库必须刷新心跳"
        db.record_answer(rid, 1, 1, "single", [0])
        h2 = db._query_one(
            "SELECT heartbeat_at h FROM runs WHERE id=?", (rid,))["h"]
        assert h2 >= h1, "逐题落库同样刷新心跳"
        db.finish_run(rid, 1, 0, 1.0)
        db.record_submission_result(rid, 2, "success")   # 收尾后的行不再刷
        h3 = db._query_one(
            "SELECT heartbeat_at h FROM runs WHERE id=?", (rid,))["h"]
        assert h3 == h2
    finally:
        db.close()


def test_p1_7_live_batch_with_progress_is_not_reaped() -> None:
    """复现案例 reaper_removes_known_resume_progress：61 分钟前启动、已记 2 份成功
    且仍持有连接的批次，不许被另一个入口的清理判死。"""
    db = SubmissionHistory(":memory:")
    try:
        url = "https://www.wjx.cn/vm/offline-only.aspx"
        rid = db.start_run(url, 5, "edge", False)
        db.record_submission_result(rid, 1, "success")
        db.record_submission_result(rid, 2, "success")
        # 启动时间推到 61 分钟前（心跳仍是刚刚 —— 活着的证据）
        db._conn.execute(
            "UPDATE runs SET started_at=datetime('now','-61 minutes') WHERE id=?",
            (rid,))
        assert db.reap_stale_runs() == 0, "有心跳的活批次不许 reap"
        assert db.find_resumable_run(url) is not None
        assert db.resume_progress(rid) == (2, 0)
    finally:
        db.close()


def test_p1_7_silent_batch_is_still_reaped() -> None:
    """对照：启动后一次库都没写过、启动时间老于阈值的批次照旧判死（行为不回退）。"""
    db = SubmissionHistory(":memory:")
    try:
        rid = db.start_run("https://x/dead", 5, "edge", False)
        db._conn.execute(
            "UPDATE runs SET started_at=datetime('now','-2 hours') WHERE id=?",
            (rid,))
        assert db.reap_stale_runs(stale_after_minutes=60) == 1
        row = db._query_one("SELECT status s FROM runs WHERE id=?", (rid,))
        assert row["s"] == "failed"
    finally:
        db.close()


def test_p1_7_stale_heartbeat_reaps_even_with_fresh_start_time() -> None:
    """反方向也成立：启动时间新、但最后一次落库已老于阈值 → 判死。

    语义是"最后一次落库多老"，不是"启动多老"——启动后写过库、随后安静
    超过阈值（进程死了）同样该 reap。
    """
    db = SubmissionHistory(":memory:")
    try:
        rid = db.start_run("https://x/quiet", 5, "edge", False)
        db.record_submission_result(rid, 1, "success")
        db._conn.execute(
            "UPDATE runs SET heartbeat_at=datetime('now','-90 minutes') WHERE id=?",
            (rid,))
        assert db.reap_stale_runs(stale_after_minutes=60) == 1
    finally:
        db.close()


def test_p1_7_old_v3_db_migration_adds_heartbeat_column() -> None:
    """v3 老库打开 → 自动补 heartbeat_at 列（NULL 语义由 COALESCE 兜底）。"""
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "old.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            """
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
            );
            INSERT INTO runs (survey_url, total_submissions, browser, use_uc)
            VALUES ('https://x/old', 3, 'edge', 0);
            """
        )
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()

        db = SubmissionHistory(path)
        try:
            cols = {r[1] for r in db._conn.execute("PRAGMA table_info(runs)").fetchall()}
            assert "heartbeat_at" in cols
            assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 4
            # 老行刚被打开时不算过期（started_at=现在），reap 不动它
            assert db.reap_stale_runs(stale_after_minutes=60) == 0
        finally:
            db.close()


# ================================================================== P2-11

def test_p2_11_empty_count_options_is_rejected_by_validation() -> None:
    """复现案例 empty_count_options_passes_validation_then_crashes 的前半：校验拦截。"""
    cfg = {1: {"type": "multi", "weights": [1, 1], "count_options": []}}
    errors = validate_weight_config(cfg)
    assert errors, "空 count_options 必须在校验期被拒"
    assert any("count_options" in e and "空" in e for e in errors)


def test_p2_11_orphan_count_weights_is_rejected() -> None:
    """只有 count_weights 没有 count_options：对应关系残缺，直接拒。"""
    errors = validate_weight_config(
        {1: {"type": "multi", "weights": [1, 1], "count_weights": [2, 1]}})
    assert any("count_weights" in e for e in errors)


def test_p2_11_matrix_pick_options_share_the_same_guard() -> None:
    """矩阵多选的 pick_* 走同一套校验：空候选同样被拒。"""
    errors = validate_weight_config(
        {2: {"type": "matrix_multi", "row_weights": {"1": [1, 1]},
             "pick_options": []}})
    assert any("pick_options" in e for e in errors)


def test_p2_11_generator_gives_readable_error_for_empty_candidates() -> None:
    """复现案例的后半：绕过校验直接喂空候选 → 可读的 ValueError，不是裸 IndexError。"""
    apply_weight_config(
        {1: {"type": "multi", "weights": [1, 1], "count_options": []}},
        replace=True,
    )
    with pytest.raises(ValueError, match="count_options"):
        generate_answer({"q": 1, "type": "multi", "choices": [1, 2]})


# ================================================================== P2-12

_URL = "https://www.wjx.cn/vm/offline-only.aspx"


def _run_with_mixed_statuses(db: SubmissionHistory) -> int:
    """2 成功 + 1 失败 + 1 未知。答案值 = 第几份，success 两份拼出可算 α 的方差。"""
    rid = db.start_run(_URL, 4, "edge", False)
    for sub, status in ((1, "success"), (2, "success"), (3, "failed"),
                        (4, "unknown")):
        for q in (1, 2, 3):
            db.record_answer(rid, sub, q, "single", options_selected=[sub - 1])
        db.record_submission_result(rid, sub, status)
    return rid


def test_p2_12_success_scope_counts_only_proven_submissions() -> None:
    """复现案例 reliability_includes_failed_unknown 的修复版口径。

    复现里 1 成功 + 1 失败 + 1 未知把 n 报成 3；这里加到 2 成功（否则 success
    口径下 n=1 连 α 都不可算，区分不出"口径错了"和"样本不够"）。
    success 口径下 n=2；被排除的份数与口径声明随报告携带。
    """
    db = SubmissionHistory(":memory:")
    try:
        rid = _run_with_mixed_statuses(db)
        all_rep = measure_dimensions(db, rid, {"demo": [1, 2, 3]})[0]
        succ_rep = measure_dimensions(
            db, rid, {"demo": [1, 2, 3]}, status_scope="success")[0]
        assert all_rep.n == 4, "默认口径保持旧行为（含全部尝试）"
        assert succ_rep.n == 2, "success 口径只计明确成功的提交"
        assert succ_rep.alpha == pytest.approx(1.0), (
            "两份成功答卷三题同增 → 完全平行 → α=1"
        )
        assert "仅明确成功" in succ_rep.scope_caveat
        assert any("failed 1" in note and "unknown 1" in note
                   for note in succ_rep.notes), "排除明细要说出来"
    finally:
        db.close()


def test_p2_12_success_scope_never_promotes_unknown_or_stateless() -> None:
    """unknown / 无逐份状态的份数不许冒充成功：宁可 n=0，也不补数。"""
    db = SubmissionHistory(":memory:")
    try:
        rid = _run_with_mixed_statuses(db)
        # 把两份 success 都改成 unknown → success 口径下没有可算样本
        db._conn.execute(
            "UPDATE submissions SET final_status='unknown' "
            "WHERE run_id=? AND submission_index IN (1, 2)", (rid,))
        rep = measure_dimensions(
            db, rid, {"demo": [1, 2, 3]}, status_scope="success")[0]
        assert rep.n == 0 and rep.alpha is None

        # 老批次：answers 有行、submissions 没有任何行 → 全部不计入
        rid2 = db.start_run(_URL, 2, "edge", False)
        for sub in (1, 2):
            for q in (1, 2, 3):
                db.record_answer(rid2, sub, q, "single", options_selected=[0])
        rep2 = measure_dimensions(
            db, rid2, {"demo": [1, 2, 3]}, status_scope="success")[0]
        assert rep2.n == 0 and rep2.alpha is None
        assert any("没有逐份成败记录" in note for note in rep2.notes), (
            "缺状态必须显式说明，不许静默当成功"
        )
    finally:
        db.close()


def test_p2_12_submission_statuses_helper() -> None:
    db = SubmissionHistory(":memory:")
    try:
        rid = _run_with_mixed_statuses(db)
        assert db.submission_statuses(rid) == {
            1: "success", 2: "success", 3: "failed", 4: "unknown"}
        rid2 = db.start_run(_URL, 1, "edge", False)
        assert db.submission_statuses(rid2) == {}
    finally:
        db.close()


def test_p2_12_unknown_status_scope_is_rejected() -> None:
    db = SubmissionHistory(":memory:")
    try:
        rid = _run_with_mixed_statuses(db)
        with pytest.raises(ValueError, match="status_scope"):
            measure_dimensions(db, rid, {"demo": [1, 2, 3]}, status_scope="bogus")
    finally:
        db.close()


# ================================================================== 杂项钉点

def test_format_weights_for_entry_renders_sort_order() -> None:
    """排序配置渲染回第 4 列（解析侧本来就认，渲染侧此前漏了）。"""
    assert format_weights_for_entry({"type": "sort", "order": ["3", "2", "1"]}) \
        == "3,2,1"
    assert format_weights_for_entry({"type": "sort"}) == ""
    # 有 weights 的条目仍按旧优先级渲染
    assert format_weights_for_entry({"type": "single", "weights": [1, 0]}) \
        == "1.0000,0.0000"
