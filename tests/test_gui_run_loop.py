"""GUI 批量循环的关键契约测试（v2.5 引入，v2.6 随 _run_loop 收敛重写）。

gui/ 此前零覆盖（约占生产代码 41%）。`no_record_text` 在 GUI 里根本不存在
（填空原文无条件落盘）、崩溃批次被误标可续传 —— 都因此长期不可见。

v2.6 后 ``SurveyGUI._run_loop`` 只是 ``src.cli.run_batch`` 的一层薄壳，
它需要的 self 成员一共只有 6 个（root / _log / _history_get_db /
_sync_ui_mirrors_from_state / _update_progress / _on_run_finished）。
所以这里用一个 stub host 承载它，**不创建任何 Tk 窗口**：
真实 tk.Tk() 在同进程内反复建/销解释器会间歇性抛
"TclError: this probably means that tk wasn't installed properly"，
用它做测试基座只会得到一套随机变红的用例。

_run_loop 与被它绑定的 _sync_ui_mirrors_from_state 都是**真实现**，
因此测的仍然是 GUI 的真实接缝。
"""

from __future__ import annotations

import os
import sys
import types
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import RunState  # noqa: E402


class StubDriver:
    """够用的假 driver：所有 JS 探测返回 falsy，流程快速走完。"""

    current_url = "https://example.test/x"

    class _Switch:
        def default_content(self) -> None:
            pass

        def frame(self, _i) -> None:
            pass

    def __init__(self) -> None:
        self.switch_to = StubDriver._Switch()
        self.quit_calls = 0

    def get(self, _url) -> None:
        pass

    def quit(self) -> None:
        self.quit_calls += 1

    def delete_all_cookies(self) -> None:
        pass

    def execute_script(self, _script, *_args, **_kw):
        return 0


class StubRoot:
    """记录被排队的 after 回调，测试自己决定何时兑现。"""

    def __init__(self) -> None:
        self.scheduled: list = []

    def after(self, delay, func=None, *args):
        self.scheduled.append(func)
        return "after-id"

    def drain(self) -> int:
        n = len(self.scheduled)
        for fn in self.scheduled:
            if fn is not None:
                fn()
        self.scheduled.clear()
        return n


class StubHost:
    """承载 SurveyGUI._run_loop 所需的最小 self 面。"""

    def __init__(self) -> None:
        from gui.app import SurveyGUI

        self.root = StubRoot()
        self.logs: list[tuple[str, str]] = []
        self.dbs: list = []
        self.running = False
        self._state: RunState | None = None
        self.finished_calls = 0
        # 真实现：UI 镜像同步是这次要测的契约之一
        self._sync_ui_mirrors_from_state = types.MethodType(
            SurveyGUI._sync_ui_mirrors_from_state, self
        )
        self._run_loop = types.MethodType(SurveyGUI._run_loop, self)

    def _log(self, msg, level="INFO") -> None:
        self.logs.append((str(msg), level))

    def _history_get_db(self):
        return self.dbs[0] if self.dbs else None

    def _update_progress(self) -> None:
        pass

    def _on_run_finished(self) -> None:
        self.finished_calls += 1


@pytest.fixture()
def host():
    from src import config as cfg
    cfg.WEIGHT_CONFIG.clear()
    yield StubHost()
    cfg.WEIGHT_CONFIG.clear()


def _state(**kw) -> RunState:
    base = dict(attempts_cap=1, total_target=1, browser="edge",
                survey_url="https://example.test/x")
    base.update(kw)
    return RunState(**base)


def _run_engine(host, state, submission_side_effect, *, db=None):
    """让**真实** run_batch 驱动假 driver 跑完 _run_loop（不 patch 引擎本身）。

    patch 的是引擎自己延迟导入的三个源模块符号。
    ``submission_side_effect(*args, **kwargs)`` 收到 run_one_submission 的原始实参。
    """
    calls: list[dict] = []

    def spy(driver, url, lock, **kwargs):
        calls.append({"lock": lock, **kwargs})
        return submission_side_effect(driver, url, lock, **kwargs)

    if db is not None:
        host.dbs.append(db)
    # 真实流程里 _on_start 会先绑定 self._state 再启动 worker 线程
    host._state = state

    with mock.patch("src.browser.create_driver", return_value=StubDriver()), \
         mock.patch("src.pipeline.run_one_submission", side_effect=spy), \
         mock.patch("src.utils.human_pause", return_value=0.0), \
         mock.patch("src.browser.cleanup_browser_state"):
        host.running = True
        host._run_loop(state)
    return calls


def by_round(fn):
    """把"按第几轮返回结果"的简写适配成 run_one_submission 的签名。"""
    def _wrapped(*args, **kwargs):
        idx = int(kwargs.get("submission_index") or 0)
        return fn(max(1, idx))
    return _wrapped


# ---------------------------------------------------------------------------
#  1. GUI → 引擎的参数交接
# ---------------------------------------------------------------------------
def test_run_loop_hands_state_and_privacy_flag_to_engine(host) -> None:
    """_run_loop 必须是"交状态"而不是"自己算一遍"。

    回归背景：v2.5 前 GUI 自带一份 170 行循环副本，no_record_text 在那里
    压根不存在 → 填空原文无条件写进 SQLite。
    """
    st = _state(no_record_text=True)
    captured: dict = {}

    import src.cli as cli_mod

    with mock.patch.object(
        cli_mod, "run_batch",
        side_effect=lambda *a, **k: (captured.update(args=a, kwargs=k), (0, 0))[1],
    ):
        host._run_loop(st)

    kw = captured["kwargs"]
    assert kw["state"] is st, "必须把 GUI 的 RunState 原样交给引擎"
    assert kw["no_record_text"] is True
    assert kw["browser"] == "edge"
    assert callable(kw["on_round"]) and callable(kw["stop_check"])
    assert captured["args"][0] == st.survey_url


def test_stop_check_reads_live_state_flag(host) -> None:
    """stop_check 必须是闭包读实时 state.stop_flag，而不是启动时快照的布尔值。"""
    st = _state()
    captured: dict = {}

    import src.cli as cli_mod

    with mock.patch.object(
        cli_mod, "run_batch",
        side_effect=lambda *a, **k: (captured.update(k), (0, 0))[1],
    ):
        host._run_loop(st)

    assert captured["stop_check"]() is False
    st.request_stop()
    assert captured["stop_check"]() is True, "停止按钮必须能被引擎看见"


def test_engine_uses_state_attempts_cap_not_recomputed(host) -> None:
    """续传时 attempts_cap 由 GUI 的对话框算好，引擎不得按 total-done 再算一遍。"""
    st = _state(total_target=50, attempts_cap=5, resume_start_idx=46)
    st.success_count = 45
    calls = _run_engine(host, st, lambda *a, **k: "failed")
    assert len(calls) == 5, (
        f"应严格跑满调用方给定的 attempts_cap=5，实际 {len(calls)}"
    )


# ---------------------------------------------------------------------------
#  2. 端到端：真引擎 + 假 driver，验证 GUI 侧收尾
# ---------------------------------------------------------------------------
def test_run_loop_forwards_lock_to_pipeline(host) -> None:
    """lock 必传到 run_one_submission（v2.3 曾在 CLI/GUI 两处同时漏传 → TypeError）。"""
    calls = _run_engine(host, _state(no_record_text=True), by_round(lambda n: "success"))
    assert len(calls) == 1
    from src.utils import ManualHoldLock
    assert isinstance(calls[0]["lock"], ManualHoldLock)
    assert calls[0]["no_record_text"] is True


def test_run_loop_updates_counters_through_on_round(host) -> None:
    """每轮回调必须把引擎计数反映到 GUI 影子镜像（进度条读的是这些字段）。"""
    _run_engine(host, _state(attempts_cap=3, total_target=3),
                by_round(lambda n: "success" if n < 3 else "failed"))
    assert host._state.success_count == 2
    assert host._state.fail_count == 1
    host.root.drain()                    # 兑现 after(0, _update_progress)
    assert host.finished_calls == 1, "收尾必须通知 UI 恢复按钮状态"


def test_single_round_webdriver_error_does_not_abort_batch(host) -> None:
    """v2.5 回归：一次 TimeoutException 不得让整批夭折并记为崩溃。"""
    from selenium.common.exceptions import TimeoutException

    def flaky(n):
        if n == 1:
            raise TimeoutException("slow page")
        return "success"

    calls = _run_engine(host, _state(attempts_cap=3, total_target=3), by_round(flaky))
    assert len(calls) == 3, f"应跑满 3 轮，实际 {len(calls)}"
    assert host._state.success_count == 2
    assert host._state.fail_count == 1


def test_stop_flag_mid_batch_marks_interrupted_and_is_persisted(host) -> None:
    """停止按钮 → 引擎 mark_interrupted → 批次以 interrupted 闭合（可续传）。"""
    from src.history import SubmissionHistory

    db = SubmissionHistory(":memory:")
    st = _state(attempts_cap=5, total_target=5)

    def stop_during_first_round(*args, **kwargs):
        if not st.stop_flag:
            st.request_stop()      # 模拟第 1 轮执行期间用户点了停止
        return "success"

    _run_engine(host, st, stop_during_first_round, db=db)

    row = db._query_one("SELECT * FROM runs")
    db.close()
    assert row is not None, "停止也必须闭合 history 行"
    assert row["status"] == "interrupted", (
        f"用户主动停止应记 interrupted 以便续传，实际 {row['status']}"
    )
    assert row["success_count"] >= 1


def test_crash_takes_precedence_over_stop_flag(host) -> None:
    """用户点了停止、在途那轮又崩了 → 必须记 failed，不能记成可续传的 interrupted。

    v2.5 前 GUI 收尾只要 stop_flag 置位就走 mark_interrupted，会把浏览器状态
    未知的崩溃批次当"可续传"提供给用户，并丢掉崩溃信息。
    """
    from src.history import SubmissionHistory

    st = _state(attempts_cap=1, total_target=1)

    # 前置自检：RunState 自身的优先级定义
    probe = _state()
    probe.request_stop()
    probe.mark_crashed("boom")
    assert probe.history_status() == "failed", "RunState 应崩溃优先于中断"

    def explodes(*args, **kwargs):
        st.request_stop()                  # 本轮执行途中用户点了停止
        raise RuntimeError("unexpected")   # 非 WebDriver 异常 → 引擎兜底 mark_crashed

    db = SubmissionHistory(":memory:")
    _run_engine(host, st, explodes, db=db)

    assert st.crash_message, "本轮异常应被记为崩溃"
    row = db._query_one("SELECT * FROM runs")
    db.close()
    assert row["status"] == "failed", (
        f"崩溃批次不能记成 interrupted（会被误当可续传），实际 {row['status']}"
    )
