"""两个宿主交给 ``run_batch`` 的东西必须逐字段相同（设计稿 §9：parity 的唯一硬证据）。

为什么需要这一份：webui 换掉的只有宿主，批次语义是 ``src.cli.run_batch`` 那一份。
但"宿主只负责把 RunState 填好交出去"这句话本身是可以出错的 —— 而且错得很安静：
少读一个复选框、把份数读成字符串、权重快照取错时机，症状全都是"网页版跑出来的
分布和桌面版不一样"，没有任何一处会报错。

这里不比较函数实现，只比较**交出去的东西**：同一套表单值，两个宿主递给
``run_batch`` 的 ``RunState`` 与关键字参数必须逐项相等。唯一的例外是写进
``runs.error_message`` 尾部的宿主标签（``GUI ·`` / ``Web ·``），那是刻意留的
区分，测试把它明写成例外而不是让它悄悄混进来。

桌面版一侧沿用 ``test_gui_run_loop.py`` 的基座约定：**不建真根窗口**
（同进程反复建/销 Tk 解释器会随机红），但这里需要的是 ``_on_start`` 的真实
读数路径，所以用 ``test_gui_user_data.py`` 那套"把 ``tk.Tk`` 临时换成共享
根窗口下的 Toplevel"的构造法 —— 建不出来（无显示的 runner）就整模块跳过，
CI 保持绿。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")

from gui import app as app_module  # noqa: E402
from gui.app import SurveyGUI  # noqa: E402
from src import cli as cli_module  # noqa: E402
from src import config as config_module  # noqa: E402
from src import dialogs  # noqa: E402
from webui.session import Availability, RunSession, SessionPaths  # noqa: E402
from webui.service import WebService  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 两边都必须一致的 RunState 字段（其余是运行期计数器，不属于"交出去的东西"）
COMPARED = (
    "attempts_cap", "total_target", "resume_start_idx", "browser", "use_uc",
    "survey_url", "no_record_text", "weight_config_snapshot",
)


@dataclass
class Form:
    url: str = "https://www.wjx.cn/vm/abc.aspx"
    count: int = 7
    browser: str = "chrome"
    use_uc: bool = True
    no_record_text: bool = False


@dataclass
class Call:
    """一次 ``run_batch`` 调用的完整记录。"""

    args: tuple
    kwargs: dict


# ------------------------------------------------------------------ 桌面版一侧


@pytest.fixture(scope="module")
def gui_window(tk_root):
    """一棵临时数据树里的真 ``SurveyGUI``。构造法与清理同 ``test_gui_user_data``。"""
    tmp = tempfile.mkdtemp(prefix="wjx_parity_gui_")
    prev_env = os.environ.get(app_module.USER_DATA_DIR_ENV)
    prev_weight = dict(config_module.WEIGHT_CONFIG)
    os.environ[app_module.USER_DATA_DIR_ENV] = tmp
    real_tk_ctor = tk.Tk
    tk.Tk = lambda *_a, **_k: tk.Toplevel(tk_root)  # type: ignore[assignment]
    app: SurveyGUI | None = None
    try:
        try:
            app = SurveyGUI()
        except Exception as exc:
            pytest.skip(f"无法构造 SurveyGUI: {type(exc).__name__}: {exc}")
        app.root.withdraw()
        yield app
    finally:
        tk.Tk = real_tk_ctor
        if app is not None:
            after_info = getattr(app.root, "after_info", None)
            if after_info is not None:
                for job in after_info():
                    app.root.after_cancel(job)
            app.running = False
            try:
                app.root.destroy()
            except Exception:
                pass
            dialogs.register_popup_handler(None)
            dialogs.register_file_picker(None)
        if prev_env is None:
            os.environ.pop(app_module.USER_DATA_DIR_ENV, None)
        else:
            os.environ[app_module.USER_DATA_DIR_ENV] = prev_env
        config_module.WEIGHT_CONFIG.clear()
        config_module.WEIGHT_CONFIG.update(prev_weight)
        shutil.rmtree(tmp, ignore_errors=True)


class _InlineThread:
    """把 worker 线程换成就地执行：对拍要的是同一次调用里的参数。"""

    def __init__(self, target=None, args=(), daemon=None, **_kw):
        self._target = target
        self._args = args

    def start(self) -> None:
        if self._target is not None:
            self._target(*self._args)

    def join(self, timeout=None) -> None:
        return None

    def is_alive(self) -> bool:
        return False


def _tk_call(gui: SurveyGUI, form: Form) -> Call:
    config_module.WEIGHT_CONFIG.clear()
    # _on_start 开头就是 `if self.running: return`，而窗口是 module 作用域复用的：
    # 上一次对拍留下的 running 会让这一次静默什么都不做（_on_run_finished 是
    # root.after 排出去的，测试里没人 drain）。
    gui.running = False
    gui.stop_flag = False
    gui._state = None
    gui.url_var.set(form.url)
    gui.count_var.set(form.count)
    gui.browser_var.set(form.browser)
    gui.use_uc_var.set(form.use_uc)
    gui.no_record_text_var.set(form.no_record_text)

    seen: list[Call] = []

    def recorder(*args, **kwargs) -> tuple[int, int]:
        seen.append(Call(args, kwargs))
        return (0, 0)

    def no_popup(*_a):
        return False

    dialogs.register_popup_handler(no_popup)
    original = cli_module.run_batch
    cli_module.run_batch = recorder
    real_thread = threading.Thread
    threading.Thread = _InlineThread  # type: ignore[assignment]
    try:
        gui._on_start()
    finally:
        threading.Thread = real_thread  # type: ignore[misc]
        cli_module.run_batch = original
        dialogs.register_popup_handler(None)
    assert len(seen) == 1, f"桌面版应该恰好调一次 run_batch，实际 {len(seen)} 次"
    return seen[0]


# -------------------------------------------------------------------- web 一侧


def _web_call(tmp_path, form: Form, questions: list | None = None,
              texts: dict | None = None) -> Call:
    config_module.WEIGHT_CONFIG.clear()
    session = RunSession(
        paths=SessionPaths(str(tmp_path / "web")),
        availability=Availability(config_io=True, history=True, qr=True,
                                  selenium=True),
    )
    seen: list[Call] = []

    def recorder(*args, **kwargs) -> None:
        seen.append(Call(args, kwargs))

    svc = WebService(session, run_batch_fn=recorder,
                     history_db_cls=lambda _p: None)
    session.set_field("url", form.url)
    session.set_field("count", form.count)
    session.set_field("browser", form.browser)
    session.set_field("use_uc", form.use_uc)
    session.set_field("no_record_text", form.no_record_text)
    if questions:
        session.set_questions(questions)
    if texts:
        session.set_weight_texts(dict(texts))
    svc.start_run()
    svc.wait_for_run(5)
    svc.close_db()
    assert len(seen) == 1, f"webui 应该恰好调一次 run_batch，实际 {len(seen)} 次"
    return seen[0]


# ---------------------------------------------------------------------- 对拍


def test_the_same_form_produces_the_same_run_state(gui_window, tmp_path) -> None:
    form = Form()
    tk_call = _tk_call(gui_window, form)
    web_call = _web_call(tmp_path, form)

    tk_state: Any = tk_call.kwargs["state"]
    web_state: Any = web_call.kwargs["state"]
    for field in COMPARED:
        assert getattr(web_state, field) == getattr(tk_state, field), (
            f"{field}: web={getattr(web_state, field)!r} "
            f"tk={getattr(tk_state, field)!r}")


def test_the_positional_arguments_are_identical(gui_window, tmp_path) -> None:
    """``run_batch(url, total, ...)`` 的位置参数：错位等于换一份问卷跑。"""
    form = Form()
    tk_call = _tk_call(gui_window, form)
    web_call = _web_call(tmp_path, form)
    assert web_call.args == tk_call.args


def test_every_keyword_except_the_host_tag_matches(gui_window, tmp_path) -> None:
    form = Form()
    tk_call = _tk_call(gui_window, form)
    web_call = _web_call(tmp_path, form)

    assert set(web_call.kwargs) == set(tk_call.kwargs), (
        f"只有桌面版传了: {set(tk_call.kwargs) - set(web_call.kwargs)}；"
        f"只有 webui 传了: {set(web_call.kwargs) - set(tk_call.kwargs)}")

    # 唯一的刻意差异：写进 runs.error_message 尾部的宿主标签
    assert tk_call.kwargs["error_suffix"].startswith("GUI · ")
    assert web_call.kwargs["error_suffix"].startswith("Web · ")
    tail = "browser=chrome uc=True"
    assert tk_call.kwargs["error_suffix"].endswith(tail)
    assert web_call.kwargs["error_suffix"].endswith(tail)

    for name, value in tk_call.kwargs.items():
        if name in ("error_suffix", "state", "on_round", "log", "stop_check",
                    "history_db"):
            continue
        assert web_call.kwargs[name] == value, f"{name} 两边不一样"


def test_the_weight_table_reaches_the_same_snapshot(gui_window, tmp_path) -> None:
    """权重表 → ``weight_config_snapshot`` 这一整条，两个宿主必须给出同一份。

    步骤 4 把解析合成了 ``src/weight_text`` 一份，这条是它存在的意义：
    同一张表在两个界面按下去，引擎看到的分布必须相同。
    """
    questions = [
        {"q": 1, "type": "single", "title": "浏览器", "choices": ["Edge", "Chrome"]},
        {"q": 2, "type": "scale", "title": "满意度", "scale": 10, "scale_min": 0},
        {"q": 3, "type": "text", "title": "姓名", "field": "name"},
    ]
    texts = {1: "0.2,0.8", 2: ",".join(["1"] * 11), 3: "张三,李四"}
    # SurveyGUI 与权重面板**共用同两个容器对象**（构造时传进去的），所以这里必须
    # 原地改：`gui.questions = [...]` 会把 GUI 这一侧换成新列表，面板还拿着旧的，
    # 于是 _build_weight_config 读到空表 —— 症状是快照 None，两边"相等"而什么都没测。
    gui_window.questions[:] = [dict(q) for q in questions]
    for qi, text in texts.items():
        gui_window.weight_entries[qi] = tk.StringVar(value=text)
    form = Form()

    tk_call = _tk_call(gui_window, form)
    tk_state = tk_call.kwargs["state"]

    web_call = _web_call(tmp_path, form, questions=questions, texts=texts)
    web_state = web_call.kwargs["state"]
    assert web_state.weight_config_snapshot == tk_state.weight_config_snapshot
    snapshot = tk_state.weight_config_snapshot
    assert sorted(snapshot) == [1, 2, 3], "三题都该进快照，而不是整体为 None"
    assert snapshot[2]["scale_min"] == 0 and len(snapshot[2]["weights"]) == 11
    gui_window.questions.clear()
    gui_window.weight_entries.clear()
