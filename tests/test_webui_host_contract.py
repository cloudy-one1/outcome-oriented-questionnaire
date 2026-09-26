"""webui 宿主自己的界面契约：数字、措辞与落盘内容一律写成**字面量**。

来历：这些断言原本住在 ``tests/test_host_parity.py``，靠"桌面版给多少、webui 给多少，
两边相等"来保证正确。桌面版退役之后那个对照物就不存在了 —— 期望值必须就地钉死，
否则删掉 Tk 的同时也删掉了"这些数就是这些"的唯一记录。

所以这个文件不许出现 ``gui``，也不许拿另一个宿主当参照：每条断言右边都是字面量。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import config as config_module  # noqa: E402
from src.cli import RoundOutcome  # noqa: E402
from src.config_io import load_weight_config, save_weight_config  # noqa: E402
from src.history import SubmissionHistory  # noqa: E402
from src.models import RunState  # noqa: E402
from webui import service as service_module  # noqa: E402
from webui.session import Availability, RunSession, SessionPaths  # noqa: E402
from webui.service import WebService  # noqa: E402

URL = "https://www.wjx.test/vm/contract.aspx"


def make(tmp_path, *, sub="web", **flags):
    """一个独立数据树 + 事件出口可记录的会话（``events`` 用来查 SSE 载荷）。"""
    events: list[tuple[str, Any]] = []
    session = RunSession(
        paths=SessionPaths(str(tmp_path / sub)),
        availability=Availability(**{"config_io": True, "history": True,
                                     "qr": True, "selenium": True, **flags}),
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    return session, events


def start(session, *, rounds=()):
    """跑一次批次并返回交给引擎的位置参数与关键字。

    假引擎按 ``src.cli.run_batch`` 的约定副作用齐全：推进尝试计数、按结果分类
    记数、在轮边界回调。少做一样，下面的断言就是在测一个不存在的引擎。
    """
    seen: list[tuple[tuple, dict]] = []

    def recorder(*args, **kwargs) -> None:
        seen.append((args, kwargs))
        state = kwargs["state"]
        for outcome in rounds:
            state.advance_attempt()
            if outcome.outcome == "success":
                state.mark_success()
            elif outcome.outcome == "unknown":
                state.mark_unknown()      # 保守计失败，但单独计数
            elif outcome.outcome in ("failed", "error"):
                state.mark_failure()
            # aborted / browser_dead：这一份没交出去，两个数都不动
            kwargs["on_round"](outcome)

    svc = WebService(session, run_batch_fn=recorder,
                     history_db_cls=lambda _p: None)
    svc.start_run()
    assert svc.wait_for_run(5), "worker 没收尾 = 这条用例在等一个不会来的人"
    assert len(seen) == 1, f"应该恰好交出去一次，实际 {len(seen)} 次"
    args, kwargs = seen[0]
    svc.close_db()
    return args, kwargs


QUESTIONS = [
    {"q": 1, "type": "single", "title": "浏览器", "choices": ["a", "b", "c"]},
    {"q": 2, "type": "scale", "title": "满意度", "scale": 5, "scale_min": 1},
]
TEXTS = {1: "0.2,0.3,0.5", 2: "5,4,3,2,1"}


def test_the_form_becomes_exactly_this_run_state(tmp_path) -> None:
    """宿主对跑批的全部责任就是填好这一份 ``RunState`` —— 每个字段各是一个数。

    原来这条是"和桌面版逐字段比"。桌面版走了之后，值本身留在这里当字面量：
    7 份、chrome + UC、不记录填空文本，进度从第 1 份起算。
    """
    session, _events = make(tmp_path)
    session.set_field("url", URL)
    session.set_field("count", 7)
    session.set_field("browser", "chrome")
    session.set_field("use_uc", True)
    session.set_field("no_record_text", True)

    args, kwargs = start(session)
    state = kwargs["state"]
    assert args == (URL, 7), "位置参数错位等于换一份问卷去跑"
    assert (state.total_target, state.attempts_cap, state.resume_start_idx) == (7, 7, 1)
    assert (state.browser, state.use_uc, state.no_record_text) == ("chrome", True, True)
    assert state.survey_url == URL
    assert state.success_count == 0 and state.fail_count == 0
    assert kwargs["no_record_text"] is True
    # 宿主标签是唯一一处刻意写进库里的宿主信息（runs.error_message 尾部）
    assert kwargs["error_suffix"] == "Web · browser=chrome uc=True"
    assert set(kwargs) == {
        "browser", "use_uc", "history_db", "weight_config", "no_record_text",
        "state", "on_round", "log", "stop_check", "error_suffix",
    }, "交给引擎的关键字集合变了 —— 少一个参数就是少一条契约"


def test_the_stop_button_reaches_the_engine_through_this_callable(tmp_path) -> None:
    """``stop_check`` 是长跑唯一的刹车踏板：按钮按下去，它要在**下一轮边界**变 True。

    关键字的"存在"由上面那条集合断言管，这一条管它的**内容**：它是
    ``lambda: state.stop_flag``，不是一个恒 False 的占位符 —— 后者能让界面显示
    "正在停止"而引擎一路跑到天亮。
    """
    session, _events = make(tmp_path)
    session.set_field("url", URL)
    session.set_field("count", 3)
    seen: list[bool] = []

    def recorder(*args, **kwargs) -> None:
        seen.append(kwargs["stop_check"]())          # 刚开跑：没人按过停止
        kwargs["state"].advance_attempt()
        kwargs["on_round"](RoundOutcome(index=1, outcome="success",
                                        message="提交成功"))
        svc.request_stop()
        seen.append(kwargs["stop_check"]())          # 按下去之后立刻可读

    svc = WebService(session, run_batch_fn=recorder, history_db_cls=lambda _p: None)
    svc.start_run()
    assert svc.wait_for_run(5)
    assert seen == [False, True], f"停止信号没穿过 stop_check：{seen}"
    assert session.running is False
    svc.close_db()


def test_the_table_becomes_exactly_this_weight_snapshot(tmp_path) -> None:
    """权重表 → ``weight_config_snapshot``：两个宿主共用解析之后，值钉在这里。"""
    session, _events = make(tmp_path)
    session.set_field("url", URL)
    session.set_field("count", 2)
    session.set_questions([dict(q) for q in QUESTIONS])
    session.set_weight_texts(dict(TEXTS))

    _args, kwargs = start(session)
    snap = kwargs["state"].weight_config_snapshot
    assert isinstance(snap, dict), "快照在建 RunState 时就定下来，worker 不再读界面"
    assert sorted(snap) == [1, 2], "题号键必须是 int，字符串键引擎不认"
    assert snap[1]["weights"] == [0.2, 0.3, 0.5]
    assert snap[2]["scale_min"] == 1 and len(snap[2]["weights"]) == 5
    assert snap[1]["anchor"]["signature"] == "single:3", "锚点掉了就会静默错位"


@pytest.mark.parametrize("resume_start_idx,current_attempt,shown,total,pct", [
    (1, 0, 1, 7, 100 / 7),   # 全新批次、还没跑第 1 次：显示起点而不是 0
    (4, 0, 4, 7, 400 / 7),   # 续传批次、还没开跑：显示第 4 份（不是第 3，也不是第 1）
    (4, 1, 4, 7, 400 / 7),
    (4, 2, 5, 7, 500 / 7),
    (1, 7, 7, 7, 100.0),
    (1, 0, 1, 0, 0.0),       # 总数 0 不许除零
])
def test_the_progress_shows_exactly_these_numbers(tmp_path, resume_start_idx,
                                                  current_attempt, shown, total,
                                                  pct) -> None:
    """进度条上那几个数的算法（原为两份抄本，值钉在这里）。"""
    session, events = make(tmp_path)
    config_module.WEIGHT_CONFIG.clear()

    run_state = RunState(attempts_cap=total, total_target=total,
                         resume_start_idx=resume_start_idx, browser="edge",
                         use_uc=False, survey_url=URL)
    run_state.current_attempt = current_attempt
    svc = WebService(session, run_batch_fn=lambda *_a, **_k: None,
                     history_db_cls=lambda _p: None)
    svc._state = run_state
    svc._sync_progress()

    assert (session.current_round, session.total_rounds) == (shown, total)
    progress = [payload for kind, payload in events if kind == "progress"]
    assert progress, "进度没有推给前端"
    assert progress[-1]["round"] == shown
    assert progress[-1]["percent"] == pytest.approx(pct)
    assert progress[-1]["total"] == total
    svc.close_db()


def test_round_outcomes_map_to_exactly_these_levels() -> None:
    """每轮结果用什么级别写日志 —— 长跑几小时的人靠这一列颜色决定要不要停。"""
    assert service_module._ROUND_LEVELS == {
        "success": "OK",
        "failed": "FAIL",
        "unknown": "FAIL",   # 交了但没确认成功：宁可算失败
        "error": "FAIL",
        "browser_dead": "WARN",
        "aborted": "WARN",    # 人按了停止，这一份没交出去，不算失败
    }
    assert service_module._ROUND_LEVELS.get("还没定义过的结果", "INFO") == "INFO"


def test_the_probe_still_counts_every_control_family() -> None:
    """探测脚本那段 JS 值不值得逐选择器钉：它一旦少一家，那类题就"根本没被探测到"。

    原来这条只保证"两份抄本一模一样"—— 两份同时漏掉同一家选择器时它是绿的。
    现在钉的是覆盖面本身：radio/checkbox/text/textarea/select、量表四写法、
    矩阵与排序共用的 ``div.field div.label``。
    """
    js = service_module._QUESTION_COUNT_JS
    assert js.strip().startswith("return ("), "JS 必须 return，否则拿到的永远是 None"
    for selector in ("radio", "checkbox", "text", "textarea", "select",
                     "ui-slider", "star", "question-rating", "scale-span",
                     "div.field div.label"):
        assert selector in js, f"探测脚本不再数 {selector} —— 这一类题会被当成不存在"


def test_a_detected_type_is_called_exactly_this_in_the_summary() -> None:
    """探测摘要里的题型中文名（原来只和桌面版比过）。"""
    assert service_module._TYPE_LABELS == {
        "single": "单选",
        "multi": "多选",
        "dropdown": "下拉",
        "scale": "量表",
        "text": "填空",
        "matrix": "矩阵",
        "matrix_multi": "矩多",
        "sort": "排序",
    }


def test_a_round_line_says_this_and_a_stopped_run_says_that(tmp_path) -> None:
    """逐轮那一行的形状，以及"停止"落在轮边界时谁说话。"""
    session, _events = make(tmp_path)
    session.set_field("url", URL)
    session.set_field("count", 2)
    _args, kwargs = start(session, rounds=[
        RoundOutcome(index=1, outcome="success", message="提交成功"),
        RoundOutcome(index=2, outcome="aborted", message="已按停止收尾"),
    ])
    lines = [(row["tag"], row["text"]) for row in session.log_lines]
    assert ("OK", "[1/2] 提交成功  (✓1 ✕0)") in lines, lines
    assert ("WARN", "[2/2] 已按停止收尾  (✓1 ✕0)") in lines, lines
    assert any(tag == "HEADER" and "执行结束" in text for tag, text in lines)


def test_exporting_writes_exactly_this_document(tmp_path) -> None:
    """导出的那份 JSON 是用户唯一能带走的工件 —— 结构与宿主标签都钉在这里。"""
    session, _events = make(tmp_path)
    svc = WebService(session, save_weight_config=save_weight_config,
                     load_weight_config=load_weight_config,
                     history_db_cls=lambda _p: None)
    session.set_field("url", URL)
    session.set_questions([dict(q) for q in QUESTIONS])
    session.set_weight_texts(dict(TEXTS))
    out = str(tmp_path / "out" / "weight_config.json")   # 父目录该自动建出来
    svc.export_config(out)

    with open(out, encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc["schema_version"]
    assert doc["meta"]["name"] == "weight_config"
    assert doc["meta"]["survey_url"] == URL
    assert doc["meta"]["description"] == "Web 界面导出 · 共 2 道题"
    assert sorted(int(k) for k in doc["config"]) == [1, 2]
    assert doc["config"]["1"]["weights"] == [0.2, 0.3, 0.5]
    assert doc["saved_at"], "没有落盘时间，事后没法判断这份是哪一次导的"
    svc.close_db()


def test_what_this_host_exports_this_host_reads_back(tmp_path) -> None:
    """导出的那份文件要能被同一个宿主原样读回来。

    原来这条是"A 宿主导出、B 宿主导入，两边结果相同"，靠第二个宿主当证人。
    证人走了，剩下的问题只有一句：**自己写的格式自己认不认**。这条钉的就是它，
    而且中间不经过手写的样例文件 —— 样例文件会跟着实现一起被改错。
    """
    session, _events = make(tmp_path)
    svc = WebService(session, save_weight_config=save_weight_config,
                     load_weight_config=load_weight_config,
                     history_db_cls=lambda _p: None)
    session.set_field("url", URL)
    session.set_questions([dict(q) for q in QUESTIONS])
    session.set_weight_texts(dict(TEXTS))
    out = str(tmp_path / "roundtrip.json")
    svc.export_config(out)

    config_module.WEIGHT_CONFIG.clear()
    session.set_weight_texts({1: "", 2: ""})       # 模拟表上被清空
    svc.import_config(out)

    assert sorted(config_module.WEIGHT_CONFIG) == [1, 2]
    assert config_module.WEIGHT_CONFIG[1]["weights"] == [0.2, 0.3, 0.5]
    assert session.weight_texts[1] == "0.2000,0.3000,0.5000"
    assert session.weight_texts[2] == "5.0000,4.0000,3.0000,2.0000,1.0000"
    assert session.table_rows()[1]["text"] == "5.0000,4.0000,3.0000,2.0000,1.0000"
    svc.close_db()


def test_importing_replaces_weights_and_says_this_number(tmp_path) -> None:
    """导入配置：全局被整体替换，那句"同步到表格 N 道"只数表上有的行。"""
    cfg = {1: {"type": "single", "weights": [0.25, 0.75]},
           9: {"type": "single", "weights": [0.5, 0.5]}}
    path = str(tmp_path / "preset.json")
    save_weight_config(path, cfg, meta={"name": "preset"})

    session, _events = make(tmp_path)
    config_module.WEIGHT_CONFIG.clear()
    # 先塞一道这份配置里没有的题：合并语义会让它静默残留，所以"替换"这件事
    # 必须靠一个**在这份文件之外的旧状态**才看得出来。
    config_module.WEIGHT_CONFIG.update({5: {"type": "single", "weights": [1, 1]}})
    session.set_questions([dict(QUESTIONS[0])])
    svc = WebService(session, history_db_cls=lambda _p: None,
                     save_weight_config=save_weight_config,
                     load_weight_config=load_weight_config)
    svc.import_config(path)
    assert sorted(config_module.WEIGHT_CONFIG) == [1, 9], "没整体替换 = 上一份配置残留"
    assert session.weight_texts[1] == "0.2500,0.7500"
    assert session.weight_texts[9] == "0.5000,0.5000", "表上没有的行号也要记下"
    assert [r["q"] for r in session.table_rows()] == [1], "记下不等于凭空多一行"
    texts = [r["text"] for r in session.log_lines]
    assert any("配置载入 · 校验警告 0 条" in t for t in texts), texts
    assert any("配置 2 道 · 同步到表格 1 道" in t for t in texts), texts
    svc.close_db()


def test_a_default_config_is_loaded_at_startup_without_a_word(tmp_path) -> None:
    """开机自动载入：有文件就静默把表填好，没文件时**一句日志都不写**。

    原来这条对拍比的是"两个宿主从同一份默认文件得到同一张表"。证人走了之后留下
    两条字面量：载入结果就是这些数，以及"没有文件 = 不吓人"（每次启动红一条，
    等于把真正的失败淹掉）。
    """
    session, _events = make(tmp_path)
    config_module.WEIGHT_CONFIG.clear()
    svc = WebService(session, save_weight_config=save_weight_config,
                     load_weight_config=load_weight_config,
                     history_db_cls=lambda _p: None)

    svc.auto_load_default_config()
    assert not session.log_lines, "没有默认文件也要安静，否则每次启动一条假错"

    session.set_field("url", URL)
    session.set_questions([dict(QUESTIONS[0])])
    session.set_weight_texts({1: TEXTS[1]})
    svc.save_default_config()

    config_module.WEIGHT_CONFIG.clear()
    session.set_weight_texts({1: ""})
    svc.auto_load_default_config()
    assert config_module.WEIGHT_CONFIG[1]["weights"] == [0.2, 0.3, 0.5]
    assert session.weight_texts[1] == "0.2000,0.3000,0.5000"
    tagged = [r["text"] for r in session.log_lines]
    assert any("已载入「default_weight_config」· 配置 1 道 · 同步到表格 1 道" in t
               for t in tagged), tagged

    same_tree = make(tmp_path, config_io=False)[0]
    WebService(same_tree, save_weight_config=save_weight_config,
               load_weight_config=None,
               history_db_cls=lambda _p: None).auto_load_default_config()
    assert not same_tree.log_lines, "config_io 没加载时同样要静默跳过"
    svc.close_db()


def test_a_stale_running_batch_is_rejudged_and_says_this(tmp_path) -> None:
    """启动收尾孤儿批次：改判成什么、说哪一句，都是给下次续传判断用的。"""
    import datetime as dt

    from webui.__main__ import _reap_orphans

    db = SubmissionHistory(str(tmp_path / "hist.db"))
    run_id = db.start_run(URL, 5, "edge", False, weight_config=None)
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=90)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    db._conn.execute("UPDATE runs SET started_at=? WHERE id=?", (old, run_id))
    db._conn.commit()

    session, _events = make(tmp_path)
    svc = WebService(session, history_db_cls=lambda _p: db)
    _reap_orphans(svc)
    row = db._query_one("SELECT status FROM runs WHERE id=?", (run_id,))
    assert row is not None, "批次行不见了 = 断言的是空气"
    assert row["status"] == "failed", f"没改判：{row['status']}"
    assert [r["text"] for r in session.log_lines if "改判" in r["text"]] == [
        "[历史] 已把 1 个未正常收尾的批次改判为 failed"]
    svc.close_db()
    db.close()


def test_a_qr_import_walks_this_sequence(tmp_path) -> None:
    """扫码：先说"正在解析"，成功/失败各说一句，并且 busy 要收回去。"""
    session, _events = make(tmp_path)
    svc = WebService(session, decode_qr=lambda _p: URL,
                     history_db_cls=lambda _p: None)
    svc.import_qr("qr.png")
    lines = [(r["tag"], r["text"]) for r in session.log_lines]
    assert ("INFO", "正在解析二维码: qr.png") in lines
    assert ("OK", f"二维码解析成功 ✓: {URL}") in lines
    assert session.url == URL
    assert not session.is_busy("qr"), "busy 没收回 = 三个按钮永久灰着"

    session2, _ev2 = make(tmp_path, sub="web2")
    svc2 = WebService(session2, decode_qr=lambda _p: None,
                      history_db_cls=lambda _p: None)
    before = session2.url
    svc2.import_qr("qr.png")
    assert any("未识别到二维码内容" in r["text"] for r in session2.log_lines)
    assert session2.url == before, "解不出来却动了用户原来填的 URL"
    assert not session2.is_busy("qr")
    svc.close_db()
    svc2.close_db()


def test_a_missing_qr_module_refuses_without_doing_anything(tmp_path) -> None:
    """二维码模块整个没加载：一句拒绝，不解析、不改 URL、不留 busy。"""
    session, _events = make(tmp_path)
    svc = WebService(session, decode_qr=None, history_db_cls=lambda _p: None)
    svc.import_qr("qr.png")
    assert [r["text"] for r in session.log_lines] == ["二维码模块未加载，无法导入"]
    assert session.url == "" and not session.is_busy("qr")
    svc.close_db()
