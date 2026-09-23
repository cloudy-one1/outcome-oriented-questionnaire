"""弹窗间接层 + 断点续传决策的离线契约（v3.1 A 档第 1 条）。

对标来源是 SurveyController 的分层做法（`software/logging/log_utils.py:642` 的
``register_popup_handler`` + `:799-811` 的 ``log_popup_*``）：**只借结构，不取代码**
（它 GPL-3.0，我们 MIT）。它要解决的问题和我们一样 —— 模态框一调用就等真人点掉，
测试只能把整段分支绕开。

为什么值得测：README「仍然没有防线的地方」表里 `gui/app.py`、`gui/controller.py`
两行留着的理由都写着"模态对话框"。9 处 `tkinter.messagebox` 直调换成可注册出口
之后，"确认框点下去那 5 个字段必须互相自洽"这条全 GUI 最容易造成**重复提交**的
不变量，第一次有了离线防线。

本文件不建 Tk、不实例化 SurveyGUI（一建就打开真实 `data/history.db` 并起动画
`after` 循环）：假宿主 + 未绑定方法直接调，是不需要显示环境的写法。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("tkinter")

from gui.app import SurveyGUI  # noqa: E402
from src import config as config_module  # noqa: E402
from src import dialogs  # noqa: E402
from src.models import RunState  # noqa: E402

URL = "https://www.wjx.cn/vj/survey1.aspx"


@pytest.fixture(autouse=True)
def _restore_dialog_state():
    """``src.dialogs`` 是模块级单例，不还原现场就会串到下一个用例。"""
    prev_handler = dialogs.current_popup_handler()
    prev_picker = dialogs.current_file_picker()
    prev_weight = dict(config_module.WEIGHT_CONFIG)
    yield
    dialogs.register_popup_handler(prev_handler)
    dialogs.register_file_picker(prev_picker)
    config_module.WEIGHT_CONFIG.clear()
    config_module.WEIGHT_CONFIG.update(prev_weight)


# ---------------------------------------------------------------------------
#  1. src/dialogs.py —— 出口本身的契约
# ---------------------------------------------------------------------------
def test_unregistered_confirm_defaults_to_false() -> None:
    # 显式清空而不是假设"本来就没人注册"：同一进程里若有别的模块构造过
    # SurveyGUI，出口就被注册成真 messagebox 了（test_gui_user_data.py 就是）。
    dialogs.register_popup_handler(None)
    assert dialogs.current_popup_handler() is None
    assert dialogs.popup_confirm("断点续传", "是否继续？") is False


def test_unregistered_notices_are_noop_and_logged(caplog) -> None:
    dialogs.register_popup_handler(None)
    with caplog.at_level("WARNING", logger="wjx.dialogs"):
        dialogs.popup_warning("提示", "请填写问卷 URL")
        dialogs.popup_info("未识别", "没有二维码")
        dialogs.popup_error("错误", "读不到图")
    assert len(caplog.records) == 3, "无宿主时每次提示都要留痕，不能静默吞掉"
    assert "请填写问卷 URL" in caplog.text


@pytest.mark.parametrize("kind,fn", [
    ("info", dialogs.popup_info),
    ("warning", dialogs.popup_warning),
    ("error", dialogs.popup_error),
    ("confirm", dialogs.popup_confirm),
])
def test_handler_receives_kind_title_message(kind, fn) -> None:
    seen: list[tuple[str, str, str]] = []
    dialogs.register_popup_handler(lambda k, t, m: seen.append((k, t, m)) or True)
    fn("标题", "正文")
    assert seen == [(kind, "标题", "正文")]


def test_handler_exception_falls_back_to_default(caplog) -> None:
    """关窗竞态下 Tk 已销毁 —— 弹窗崩了不该把调用它的那轮提交一起带走。"""
    calls: list[str] = []

    def boom(kind: str, _t: str, _m: str) -> bool:
        calls.append(kind)
        raise RuntimeError("main window has been destroyed")

    dialogs.register_popup_handler(boom)
    with caplog.at_level("WARNING", logger="wjx.dialogs"):
        assert dialogs.popup_confirm("断点续传", "是否继续？") is False
        dialogs.popup_warning("提示", "正文")
    assert calls == ["confirm", "warning"], "抛错仍要发出出口调用，但不改判"
    assert "main window has been destroyed" in caplog.text


def test_register_none_clears_handler() -> None:
    dialogs.register_popup_handler(lambda k, t, m: True)
    dialogs.register_popup_handler(None)
    assert dialogs.current_popup_handler() is None
    assert dialogs.popup_confirm("t", "m") is False


# ---------------------------------------------------------------------------
#  2. SurveyGUI._apply_resumable_run —— 续传决策
# ---------------------------------------------------------------------------
class _FakeHost:
    """只实现被调方法真正用到的那两个成员（对标仓库的假宿主手法）。"""

    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []
        self.restored: list[dict] = []

    def _log(self, message: str, tag: str = "INFO") -> None:
        self.logs.append((str(message), tag))

    def _restore_weight_table_from_config(self, cfg: dict) -> None:
        self.restored.append(dict(cfg))


class _FakeDB:
    def __init__(self, prev: dict | None = None, error: Exception | None = None) -> None:
        self._prev = prev
        self._error = error
        self.queries: list[str] = []

    def find_resumable_run(self, url: str) -> dict | None:
        self.queries.append(url)
        if self._error is not None:
            raise self._error
        return self._prev

    @staticmethod
    def deserialize_weight_config(prev: dict) -> dict:
        payload = prev.get("weight_config")
        if isinstance(payload, Exception):
            raise payload
        return payload or {}


def _row(run_id: int = 7, done: int = 5, planned: int = 17, **extra) -> dict:
    out = {
        "id": run_id,
        "success_count": done,
        "total_submissions": planned,
        "status": "interrupted",
        "started_at": "2026-09-22 10:00:00",
    }
    out.update(extra)
    return out


def _apply(row, *, answer, db=None, total: int = 17):
    """跑一次续传决策。``answer=None`` → 不注册宿主，等价于无人应答的默认值。"""
    host = _FakeHost()
    state = RunState(
        attempts_cap=total, total_target=total, resume_start_idx=1, survey_url=URL
    )
    asked: list[tuple[str, str, str]] = []
    if answer is not None:
        def handler(kind: str, title: str, message: str) -> bool:
            asked.append((kind, title, message))
            return answer

        dialogs.register_popup_handler(handler)
    SurveyGUI._apply_resumable_run(
        host, state, URL, db if db is not None else _FakeDB(row)
    )
    return state, host, asked


def test_no_resumable_run_leaves_state_untouched() -> None:
    state, host, asked = _apply(None, answer=True)
    assert (state.resume_start_idx, state.run_id, state.attempts_cap) == (1, None, 17)
    assert asked == [], "没有可恢复批次就不该拦下一个确认框"
    assert host.logs == []


@pytest.mark.parametrize("done,planned", [(0, 17), (17, 17), (18, 17)])
def test_fully_done_or_overfilled_batch_is_not_offered(done, planned) -> None:
    """`0 < done < planned` 之外都不该问 —— 问一次就是白挡一次启动。"""
    state, _host, asked = _apply(_row(done=done, planned=planned), answer=True)
    assert asked == []
    assert state.resume_start_idx == 1 and state.run_id is None


def test_confirm_shown_once_and_names_the_run() -> None:
    _state, _host, asked = _apply(_row(run_id=7, done=5, planned=17), answer=True)
    assert [a[0] for a in asked] == ["confirm"], "只问一次，问两遍就能给出矛盾答案"
    kind, title, message = asked[0]
    assert title == "断点续传"
    assert "Run #7" in message and "已成功 5 / 17 份" in message
    assert "是否从第 6 份继续" in message


def test_yes_keeps_the_five_resume_fields_consistent() -> None:
    """这五个字段散在 if 分支里，历史上正是"漏改一个"就重复提交的地方。"""
    state, host, _asked = _apply(_row(run_id=7, done=5, planned=17), answer=True)
    assert state.resume_start_idx == 6
    assert state.run_id == 7
    assert state.success_count == 5
    assert state.total_target == 17
    assert state.attempts_cap == 12
    assert state.resume_start_idx + state.attempts_cap - 1 == state.total_target
    assert state.total_target - state.success_count == state.attempts_cap
    assert any("恢复 Run #7" in m for m, _t in host.logs)


def test_no_keeps_fresh_start_but_still_logs() -> None:
    state, host, _asked = _apply(_row(run_id=7, done=5, planned=17), answer=False)
    assert state.resume_start_idx == 1
    assert state.run_id is None and state.success_count == 0
    assert state.attempts_cap == 17 and state.total_target == 17
    assert any("已忽略上次中断批次" in m for m, _t in host.logs)


def test_headless_default_declines_resume() -> None:
    """无宿主（CLI / 离线测试）时确认默认 False：宁可重跑，绝不断言"人同意了"。"""
    state, _host, _asked = _apply(_row(done=5, planned=17), answer=None)
    assert state.resume_start_idx == 1 and state.run_id is None


def test_restored_weights_apply_even_when_resume_declined() -> None:
    """权重恢复发生在问人**之前**，所以拒绝续传也不该把上一份问卷的权重配置丢掉。"""
    payload = {2: {"type": "single", "weights": [1, 3]}}
    state, host, _asked = _apply(
        _row(done=5, planned=17, weight_config=payload), answer=False
    )
    assert config_module.WEIGHT_CONFIG[2]["weights"] == [1, 3]
    assert host.restored == [payload]
    assert state.weight_config_snapshot == payload
    assert any("已自动恢复上次权重配置" in m for m, _t in host.logs)


def test_weights_restored_with_replace_semantics() -> None:
    """replace=True 是刻意的：残留旧题号会让上一份问卷的权重静默生效。"""
    config_module.WEIGHT_CONFIG[99] = {"type": "single", "weights": [1]}
    _state, _host, _asked = _apply(
        _row(done=5, planned=17, weight_config={2: {"type": "single", "weights": [1, 3]}}),
        answer=True,
    )
    assert 99 not in config_module.WEIGHT_CONFIG
    assert set(config_module.WEIGHT_CONFIG) == {2}


def test_corrupt_weight_snapshot_does_not_block_the_ask() -> None:
    """快照读不出来只影响"权重要不要恢复"，不该连续传本身都问不出来。"""
    payload: Exception = ValueError("weight snapshot is not json")
    state, host, asked = _apply(
        _row(done=5, planned=17, weight_config=payload), answer=True
    )
    assert len(asked) == 1
    assert state.resume_start_idx == 6
    assert host.restored == []
    assert not any("恢复上次权重配置" in m for m, _t in host.logs)


def test_history_failure_degrades_to_fresh_start() -> None:
    """查库失败只记一条 WARN：续传是锦上添花，不该挡住"开始执行"这个动作。"""
    db = _FakeDB(error=OSError("database is locked"))
    state, host, _asked = _apply(None, answer=True, db=db)
    assert state.resume_start_idx == 1 and state.run_id is None
    msg, tag = host.logs[-1]
    assert tag == "WARN" and "不影响运行" in msg and "OSError" in msg


def test_query_uses_the_truncated_url_key() -> None:
    """runs 表按 `survey_url` 存，而库里那列是 500 截断过的 —— 查询侧必须同样截断，
    否则超长 URL 的批次永远查不到，症状是**静默不续传**。"""
    long_url = "https://www.wjx.cn/vj/survey1.aspx" + "?k" * 600
    db = _FakeDB(_row(done=5, planned=17))
    host = _FakeHost()
    state = RunState(survey_url=long_url)
    dialogs.register_popup_handler(lambda k, t, m: True)
    SurveyGUI._apply_resumable_run(host, state, long_url, db)
    assert db.queries == [long_url[:500]]


# ---------------------------------------------------------------------------
#  3. 文件选择框 —— 同一类阻塞，答案是路径
# ---------------------------------------------------------------------------
def test_picker_without_host_returns_none_and_logs(caplog) -> None:
    """``None`` 必须与"用户按了取消"不可区分：四个调用点就是靠它早退的。"""
    dialogs.register_file_picker(None)
    with caplog.at_level("WARNING", logger="wjx.dialogs"):
        assert dialogs.pick_open_path(title="导入权重配置") is None
        assert dialogs.pick_save_path(title="导出", defaultextension=".json") is None
    assert len(caplog.records) == 2


def test_picker_receives_kind_and_options_verbatim() -> None:
    seen: list[tuple[str, dict]] = []
    dialogs.register_file_picker(
        lambda kind, opts: seen.append((kind, opts)) or "C:/x/y.json"
    )
    opts = {"title": "导出权重配置", "defaultextension": ".json",
            "filetypes": [("JSON 配置", "*.json")]}
    assert dialogs.pick_save_path(**opts) == "C:/x/y.json"
    assert dialogs.pick_open_path(title="选择二维码图片") == "C:/x/y.json"
    assert seen == [("save", opts), ("open", {"title": "选择二维码图片"})]


def test_empty_string_from_picker_normalises_to_none() -> None:
    """tkinter 取消时返回 ``""``，而调用点判的是 ``if not filepath`` ——
    出口统一成 None，替身就不必模仿这个库细节。"""
    dialogs.register_file_picker(lambda kind, opts: "")
    assert dialogs.pick_save_path(title="导出") is None


def test_picker_exception_falls_back_to_none(caplog) -> None:
    dialogs.register_file_picker(
        lambda kind, opts: (_ for _ in ()).throw(RuntimeError("application destroyed"))
    )
    with caplog.at_level("WARNING", logger="wjx.dialogs"):
        assert dialogs.pick_open_path(title="导入") is None
    assert "application destroyed" in caplog.text

