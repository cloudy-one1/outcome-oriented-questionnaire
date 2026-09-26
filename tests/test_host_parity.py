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
from src.config_io import apply_weight_config  # noqa: E402
from src.weight_text import reconstruct_questions  # noqa: E402
from src import config as config_module  # noqa: E402
from src import dialogs  # noqa: E402
from webui.session import Availability, RunSession, SessionPaths  # noqa: E402
from webui import service as service_module  # noqa: E402
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
            panel = getattr(app, "_history_panel", None)
            if panel is not None:
                # 构造期那次"孤儿批次收尾"会懒开一个 SQLite 连接，不关就是一路
                # 挂到进程退出（ResourceWarning: unclosed database）。
                panel.close_db()
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


def _tk_call(gui: SurveyGUI, form: Form, *, accept_resume: bool = False) -> Call:
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
        return accept_resume

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


def test_resume_restores_the_same_table_on_both_hosts(gui_window, tmp_path) -> None:
    """§4 那行「反向回填」：同一份持久化权重喂给两个宿主，表必须长出同一副样子。

    两侧的文本走的是**两条不同的代码**：桌面版的 entry 由 ``populate`` 从全局
    ``WEIGHT_CONFIG`` 取默认值，webui 由 ``format_weights_for_entry`` 逐行写。
    共用 ``reconstruct_questions`` 只保证行结构同源，文本对得上才是这条用例的意义 ——
    续传时用户被告知"权重已恢复到表格，可检查/修改"，两边都得真的能检查。
    """
    restored = {
        1: {"type": "single", "weights": [0.2, 0.3, 0.5]},
        2: {"type": "scale", "scale": 5, "weights": [1, 2, 3, 4, 5]},
        3: {"type": "text", "field": "phone", "options": ["13800000000"]},
        4: {"type": "matrix", "row_weights": {"1": [1, 2], "2": [3, 4]}},
    }
    config_module.WEIGHT_CONFIG.clear()
    apply_weight_config(restored, replace=True)

    gui_window.questions[:] = []
    gui_window.weight_entries.clear()
    gui_window._restore_weight_table_from_config(dict(restored))
    tk_table = {int(q["q"]): q for q in gui_window.questions}
    tk_texts = {qi: var.get() for qi, var in gui_window.weight_entries.items()}

    session = RunSession(paths=SessionPaths(str(tmp_path / "web")),
                         availability=Availability(config_io=True, history=True,
                                                   qr=True, selenium=True))
    svc = WebService(session, history_db_cls=lambda _p: None, **CONFIG_IO)
    svc.restore_table_from_config(dict(restored))
    rows = session.table_rows()
    web_texts = {row["q"]: row["text"] for row in rows}
    svc.close_db()

    # 桌面版那边的行必须真的来自共用规则（搬家时漏接一行，这条就红）
    assert tk_table == {int(q["q"]): q for q in reconstruct_questions(restored)}
    # 两侧的第 4 列是**两条不同的代码**算出来的，这才是这条用例的意义
    assert web_texts == tk_texts, "同一份权重在两个宿主的第 4 列文本不一样"
    assert [row["q"] for row in rows] == sorted(restored), "行的题号序要一致"
    assert sorted(tk_texts) == [1, 2, 3, 4]
    assert tk_texts[1] == "0.2000,0.3000,0.5000"
    assert tk_texts[1] == "0.2000,0.3000,0.5000"
    assert tk_texts[4] == "1:1,2 | 2:3,4"
    gui_window.questions.clear()
    gui_window.weight_entries.clear()


#: 真入口（webui/__main__.build_service）会把这三个注进 WebService；对拍这边也必须注，
#: 否则测的是"缺依赖"那条降级分支，而不是导出/导入本身。
from src import config_io as _config_io  # noqa: E402

CONFIG_IO = {
    "save_weight_config": _config_io.save_weight_config,
    "load_weight_config": _config_io.load_weight_config,
    "validate_weight_config": _config_io.validate_weight_config,
}

EXPORT_URL = "https://www.wjx.test/vm/parity-export.aspx"

TABLE_QUESTIONS = [
    {"q": 1, "type": "single", "title": "浏览器", "choices": ["Edge", "Chrome", "其他"]},
    {"q": 2, "type": "scale", "title": "满意度", "scale": 5, "scale_min": 1},
    {"q": 3, "type": "text", "title": "手机", "field": "phone"},
]
TABLE_TEXTS = {1: "0.2,0.3,0.5", 2: "1,2,3,4,5", 3: "13800000000,13900000000"}


def _tk_table(gui_window, *, url: str = EXPORT_URL) -> None:
    """把桌面版的表摆成 ``TABLE_QUESTIONS`` + ``TABLE_TEXTS``（原地改，共享容器）。"""
    gui_window.questions[:] = [dict(q) for q in TABLE_QUESTIONS]
    gui_window.weight_entries.clear()
    for qi, text in TABLE_TEXTS.items():
        gui_window.weight_entries[qi] = tk.StringVar(value=text)
    gui_window.url_var.set(url)


def _web_table(tmp_path, *, url: str = EXPORT_URL):
    session = RunSession(paths=SessionPaths(str(tmp_path / "web")),
                         availability=Availability(config_io=True, history=True,
                                                   qr=True, selenium=True))
    svc = WebService(session, history_db_cls=lambda _p: None, **CONFIG_IO)
    session.set_field("url", url)
    session.set_questions([dict(q) for q in TABLE_QUESTIONS])
    session.set_weight_texts(dict(TABLE_TEXTS))
    return session, svc


def _read_json(path):
    import json

    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_exporting_the_same_table_writes_the_same_file(gui_window, tmp_path) -> None:
    """§4 第 4 行：同一张表按「导出配置」，两个宿主落到盘上的东西必须一样。

    导出是用户唯一能带走的工件 —— 桌面版导出的文件在 webui 里读出另一副分布，
    等于两个界面在互相毁配置，而没有任何一处会报错。
    """
    config_module.WEIGHT_CONFIG.clear()
    _tk_table(gui_window)
    tk_out = tmp_path / "tk" / "weight_config.json"
    asked: list[str] = []
    def _pick_save(kind, _opts):
        asked.append(kind)
        return str(tk_out)

    dialogs.register_file_picker(_pick_save)
    try:
        gui_window._on_save_config()
    finally:
        dialogs.register_file_picker(None)
    assert asked == ["save"], "桌面版应该开一次另存为框"

    session, svc = _web_table(tmp_path)
    web_out = tmp_path / "web" / "weight_config.json"
    svc.export_config(str(web_out))
    svc.close_db()

    tk_doc, web_doc = _read_json(tk_out), _read_json(web_out)
    assert tk_doc["schema_version"] == web_doc["schema_version"]
    assert tk_doc["config"] == web_doc["config"], "导出的权重内容两边不一样"
    assert tk_doc["config"]["1"]["weights"] == [0.2, 0.3, 0.5]
    assert tk_doc["meta"]["name"] == web_doc["meta"]["name"] == "weight_config"
    assert tk_doc["meta"]["survey_url"] == web_doc["meta"]["survey_url"] == EXPORT_URL
    # 唯一的刻意差异：宿主标签（与 run_batch 的 error_suffix 同一手法）
    assert tk_doc["meta"]["description"].startswith("GUI ")
    assert web_doc["meta"]["description"].startswith("Web ")
    assert tk_doc["meta"]["description"].endswith("共 3 道题")
    assert web_doc["meta"]["description"].endswith("共 3 道题")
    gui_window.questions.clear()
    gui_window.weight_entries.clear()


def test_a_file_exported_by_one_host_loads_identically_on_the_other(gui_window,
                                                                    tmp_path) -> None:
    """往返：桌面版导出的文件给 webui 导入，反过来也一样，``WEIGHT_CONFIG`` 要同一份。"""
    config_module.WEIGHT_CONFIG.clear()
    _tk_table(gui_window)
    tk_file = tmp_path / "from_tk.json"
    dialogs.register_file_picker(lambda _k, _o: str(tk_file))
    try:
        gui_window._on_save_config()
    finally:
        dialogs.register_file_picker(None)

    session, svc = _web_table(tmp_path)
    web_file = tmp_path / "from_web.json"
    svc.export_config(str(web_file))

    config_module.WEIGHT_CONFIG.clear()
    svc.import_config(str(tk_file))
    loaded_by_web = {k: dict(v) for k, v in config_module.WEIGHT_CONFIG.items()}

    config_module.WEIGHT_CONFIG.clear()
    gui_window.questions[:] = []
    gui_window.weight_entries.clear()
    gui_window._on_load_config(str(web_file))
    loaded_by_tk = {k: dict(v) for k, v in config_module.WEIGHT_CONFIG.items()}
    svc.close_db()

    assert loaded_by_web == loaded_by_tk, "交叉导入之后引擎看到的分布不一样"
    assert sorted(loaded_by_web) == [1, 2, 3]
    assert loaded_by_web[2]["scale_min"] == 1 and len(loaded_by_web[2]["weights"]) == 5
    gui_window.questions.clear()
    gui_window.weight_entries.clear()


def test_a_missing_config_io_refuses_with_the_same_words(gui_window,
                                                        monkeypatch) -> None:
    """缺可选依赖时不许半动：两边的话术与"什么都没写"要一致。"""
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(gui_window, "_log",
                        lambda msg, tag="INFO": logs.append((tag, msg)))
    controller = gui_window._controller
    monkeypatch.setattr(controller, "_has_config_io", False)
    monkeypatch.setattr(controller, "_save_wc", None)
    controller.on_save_config()
    controller.on_save_default_config()
    controller.on_load_config("unused.json")

    assert [(tag, text) for tag, text in logs] == [
        ("FAIL", "未加载 src/config_io，无法导出配置"),
        ("FAIL", "未加载 src/config_io，无法另存默认配置"),
        ("FAIL", "未加载 src/config_io，无法导入配置"),
    ], "桌面版在缺依赖时多做了事或换了措辞 —— webui 那边是另一套话"


DEFAULT_CFG = {
    1: {"type": "single", "weights": [0.25, 0.75]},
    2: {"type": "scale", "scale": 5, "scale_min": 1,
        "weights": [5, 4, 3, 2, 1]},
}


def test_the_same_default_config_autoloads_the_same_way(gui_window, tmp_path,
                                                        monkeypatch) -> None:
    """§4 第 5 行：开机静默载入默认配置。同一份文件，两边的分布、表格与日志要一致。

    这一步没有点任何人、没有任何对话框，是"打开工具就生效"的那条路 —— 它一旦在两个
    宿主上走向不同，用户看到的是同一份默认配置在网页版里静默改了另一副分布。
    """
    import json

    from src.config_io import save_weight_config

    _tk_table(gui_window)
    tk_logs: list[tuple[str, str]] = []
    monkeypatch.setattr(gui_window, "_log",
                        lambda msg, tag="INFO": tk_logs.append((tag, msg)))
    monkeypatch.setattr(gui_window, "_history_get_db", lambda: None)
    save_weight_config(gui_window._default_weight_config_path,
                       {k: dict(v) for k, v in DEFAULT_CFG.items()},
                       meta={"name": "default_weight_config"})
    config_module.WEIGHT_CONFIG.clear()
    gui_window._auto_load_default_config()
    tk_cfg = {k: dict(v) for k, v in config_module.WEIGHT_CONFIG.items()}
    tk_texts = {qi: var.get() for qi, var in gui_window.weight_entries.items()}

    session, svc = _web_table(tmp_path)
    web_logs_start = len(session.log_lines)
    save_weight_config(session.paths.default_config_path,
                       {k: dict(v) for k, v in DEFAULT_CFG.items()},
                       meta={"name": "default_weight_config"})
    config_module.WEIGHT_CONFIG.clear()
    svc.auto_load_default_config()
    web_cfg = {k: dict(v) for k, v in config_module.WEIGHT_CONFIG.items()}
    web_rows = {int(r["q"]): r["text"] for r in session.table_rows()}
    web_logs = [(row["tag"], row["text"])
                for row in list(session.log_lines)[web_logs_start:]]
    svc.close_db()

    assert web_cfg == tk_cfg, "同一份默认配置在两个宿主里落成了不同的分布"
    assert web_rows == tk_texts, "自动载入之后表格里的文本不一样"
    assert tk_texts[1] == "0.2500,0.7500"
    assert [text for _tag, text in web_logs] == [text for _tag, text in tk_logs], (
        f"webui={web_logs} 桌面版={tk_logs}")
    assert any("已载入" in text for _tag, text in tk_logs), "别把静默失败当成正常"
    assert json.dumps(sorted(tk_cfg)) == '[1, 2]'
    gui_window.questions.clear()
    gui_window.weight_entries.clear()


def test_a_stale_running_batch_is_rejudged_the_same_way(gui_window, tmp_path,
                                                        monkeypatch) -> None:
    """§4 第 11 行：上次被强杀留下的 ``running`` 批次，启动时两个宿主都要改判并说一句。

    判错方向的代价是对称的：不改判 → 下次提示"从早已死掉的批次继续"，续传它可能
    直接重复提交；改判过头 → 真该续的那批永远问不出来。
    """
    import datetime as _dt

    from src.history import SubmissionHistory

    url = "https://www.wjx.test/vm/orphan.aspx"
    tk_db_path = str(tmp_path / "tk" / "history.db")
    tk_db = SubmissionHistory(tk_db_path)
    run_id = tk_db.start_run(url, 5, "edge", False, weight_config=None)
    tk_db._conn.execute(
        "UPDATE runs SET started_at=? WHERE id=?",
        ((_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=90))
         .strftime("%Y-%m-%d %H:%M:%S"), run_id))
    tk_db._conn.commit()

    tk_logs: list[tuple[str, str]] = []
    monkeypatch.setattr(gui_window, "_log",
                        lambda msg, tag="INFO": tk_logs.append((tag, msg)))
    monkeypatch.setattr(gui_window, "_history_get_db", lambda: tk_db)
    gui_window._reap_orphan_runs()

    session = RunSession(paths=SessionPaths(str(tmp_path / "web")),
                         availability=Availability(config_io=True, history=True,
                                                   qr=True, selenium=True))
    web_db_path = str(tmp_path / "web" / "history.db")
    web_db = SubmissionHistory(web_db_path)
    web_id = web_db.start_run(url, 5, "edge", False, weight_config=None)
    web_db._conn.execute(
        "UPDATE runs SET started_at=? WHERE id=?",
        ((_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=90))
         .strftime("%Y-%m-%d %H:%M:%S"), web_id))
    web_db._conn.commit()
    svc = WebService(session, history_db_cls=lambda _p: web_db, **CONFIG_IO)
    web_logs_start = len(session.log_lines)
    from webui.__main__ import _reap_orphans

    _reap_orphans(svc)
    web_logs = [(row["tag"], row["text"])
                for row in list(session.log_lines)[web_logs_start:]]

    def _row(db, rid):
        return db._query_one("SELECT status, success_count FROM runs WHERE id=?",
                             (rid,))

    assert _row(web_db, web_id)[:2] == _row(tk_db, run_id)[:2], (
        "同一个孤儿批次，两个宿主改判成了不同的状态")
    assert _row(tk_db, run_id)["status"] != "running", "改判没发生 = 下次照样提示续传"
    assert [text for _tag, text in web_logs] == [text for _tag, text in tk_logs], (
        f"webui={web_logs} 桌面版={tk_logs}")
    tk_db.close()
    web_db.close()
    svc.close_db()


@pytest.mark.parametrize("accept", (False, True))
def test_resume_answers_produce_the_same_state_on_both_hosts(gui_window, tmp_path,
                                                             monkeypatch,
                                                             accept) -> None:
    """§4 第 10 行：续传弹窗答"是"与答"否"，两个宿主交给引擎的 state 要逐字段相同。

    这两格最容易写坏，而且坏法相反：把"不续传"实现成"什么都不恢复"，用户看到的是
    "我明明存过权重，第二次跑却像没存过"；把"续传"实现成从第 K 份（而不是 K+1）开始，
    平台上就多出一条**收不回来**的重复回收记录。所以两个方向都要钉，且各配一条绝对值
    断言 —— 只比"两边相等"的话，两个宿主一起错也照样绿。
    """
    from src.history import SubmissionHistory

    form = Form()
    restored = {1: {"type": "single", "weights": [0.2, 0.8]},
                2: {"type": "scale", "scale": 5, "scale_min": 1,
                    "weights": [1, 2, 3, 4, 5]}}
    done_before = 3

    def seed(path):
        db = SubmissionHistory(path)
        run_id = db.start_run(form.url, form.count, form.browser, form.use_uc,
                              weight_config={k: dict(v) for k, v in restored.items()})
        db.mark_interrupted(run_id, done_before, 0)
        return db, run_id

    tk_db, tk_prev = seed(str(tmp_path / "tk" / "history.db"))
    web_db, _web_prev = seed(str(tmp_path / "web" / "history.db"))

    tk_logs: list[tuple[str, str]] = []
    monkeypatch.setattr(gui_window, "_log",
                        lambda msg, tag="INFO": tk_logs.append((tag, msg)))
    monkeypatch.setattr(gui_window, "_history_get_db", lambda: tk_db)
    gui_window.questions[:] = []
    gui_window.weight_entries.clear()
    tk_call = _tk_call(gui_window, form, accept_resume=accept)
    tk_state = tk_call.kwargs["state"]
    tk_texts = {qi: var.get() for qi, var in gui_window.weight_entries.items()}

    session = RunSession(paths=SessionPaths(str(tmp_path / "web")),
                         availability=Availability(config_io=True, history=True,
                                                   qr=True, selenium=True))
    web_logs_start = len(session.log_lines)
    seen: list[Call] = []
    svc = WebService(session, run_batch_fn=lambda *a, **k: seen.append(Call(a, k)),
                     history_db_cls=lambda _p: web_db,
                     confirm=lambda _title, _message: accept, **CONFIG_IO)
    config_module.WEIGHT_CONFIG.clear()
    for name in ("url", "count", "browser", "use_uc", "no_record_text"):
        session.set_field(name, getattr(form, name))
    svc.start_run()
    assert svc.wait_for_run(5)
    web_state = seen[0].kwargs["state"]
    web_texts = {int(r["q"]): r["text"] for r in session.table_rows()}
    web_logs = [(row["tag"], row["text"])
                for row in list(session.log_lines)[web_logs_start:]]
    svc.close_db()

    marker = "断点续传启动" if accept else "已忽略上次中断批次"

    def _marked(lines):
        return [t for tag, t in lines if marker in t]

    if accept:
        # 从"已成功 3 份"的下一份继续，只补剩下的 4 份
        assert (tk_state.resume_start_idx, tk_state.attempts_cap) == (4, 4)
        assert tk_state.run_id == tk_prev, "认领的必须是刚查到的那条批次"
        assert tk_state.success_count == done_before
    else:
        assert (tk_state.resume_start_idx, tk_state.attempts_cap) == (1, form.count)
        assert tk_state.run_id is None, "拒绝了续传还认领旧 run_id = 会写回同一行"
        assert tk_state.success_count == 0
        assert _marked(tk_logs), "没说不续传 = 用户不知道为什么又从第 1 份开始"

    assert web_state.resume_start_idx == tk_state.resume_start_idx
    assert web_state.attempts_cap == tk_state.attempts_cap
    assert web_state.total_target == tk_state.total_target
    assert web_state.success_count == tk_state.success_count
    assert (web_state.run_id is None) == (tk_state.run_id is None)
    assert web_state.weight_config_snapshot == tk_state.weight_config_snapshot, (
        "两边交给引擎的权重快照不一样 —— 同一个配置文件在两个界面跑出两副分布")
    assert web_texts == tk_texts, "续传恢复出来的表格文本不一样"
    assert tk_texts[1] == "0.2000,0.8000" and sorted(tk_texts) == [1, 2]
    assert _marked(web_logs) == _marked(tk_logs), (
        f"webui={web_logs} 桌面版={tk_logs}")
    tk_db.close()
    web_db.close()
    gui_window.questions.clear()
    gui_window.weight_entries.clear()


DETECT_URL = "https://www.wjx.test/vm/parity-detect.aspx"

PROBE_QUESTIONS = [
    {"q": 1, "type": "single", "title": "浏览器", "choices": ["Edge", "Chrome", "其他"]},
    {"q": 2, "type": "scale", "title": "满意度", "scale": 5, "scale_min": 1},
    {"q": 3, "type": "text", "title": "手机", "field": "phone"},
    {"q": 4, "type": "matrix_multi", "title": "关注点", "rows": ["1", "2"],
     "cols": ["a", "b"]},
]


class _FakeDriver:
    """同一台假浏览器喂两个宿主。``counts`` 按"主文档、iframe 0、iframe 1…"排。"""

    class _Switch:
        def __init__(self, owner):
            self.owner = owner

        def default_content(self) -> None:
            self.owner.frame = None

        def frame(self, index) -> None:
            self.owner.frame = int(index)

    def __init__(self, counts=(), ready="complete", script_error=None):
        self.counts = list(counts)
        self.ready = ready
        self.frame = None
        self.visited: list[str] = []
        self.quit_calls = 0
        self.detect_frame: object = "unset"
        self.script_error = script_error
        self.switch_to = _FakeDriver._Switch(self)

    def get(self, url) -> None:
        self.visited.append(url)

    def execute_script(self, js, *_a):
        if self.script_error:
            raise self.script_error
        if "readyState" in js:
            return self.ready
        if "iframe" in js:
            return max(0, len(self.counts) - 1)
        idx = 0 if self.frame is None else self.frame + 1
        return self.counts[idx] if idx < len(self.counts) else 0

    def quit(self) -> None:
        self.quit_calls += 1


def _tk_detect(gui, monkeypatch, driver, *, questions=None, showing=False,
               wait_ok=True, create_error=None,
               import_first: str | None = None) -> list[tuple[str, str]]:
    """桌面版探测：线程、``root.after`` 与四个可选导入都换成可控件，返回日志。

    开头清一次全局 ``WEIGHT_CONFIG`` —— 不清的话这个模块里前一条用例载入的配置会漏进来，
    而 ``populate`` 的默认值正是从那份全局取的，两侧"预填不一样"就变成测试自身的假象。
    """
    from gui import controller as ctl

    config_module.WEIGHT_CONFIG.clear()
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(gui, "_log",
                        lambda msg, tag="INFO": logs.append((tag, msg)))
    monkeypatch.setattr(gui.root, "after",
                        lambda _delay, fn=None: fn() if fn else None)
    if create_error is not None:
        def _boom(*_a, **_k):
            raise create_error
        monkeypatch.setattr(ctl, "create_driver", _boom)
    else:
        monkeypatch.setattr(ctl, "create_driver", lambda *_a, **_k: driver)
    def _tk_detect_questions(_d):
        _d.detect_frame = _d.frame      # 记录"在哪个帧里读的题"
        return [dict(q) for q in (questions or [])]

    monkeypatch.setattr(ctl, "detect_questions", _tk_detect_questions)
    monkeypatch.setattr(ctl, "is_smart_verification_showing", lambda _d: showing)
    monkeypatch.setattr(ctl, "wait_for_manual_verification",
                        lambda _d, **_k: wait_ok)
    monkeypatch.setattr(ctl.threading, "Thread", _InlineThread)
    gui.url_var.set(DETECT_URL)
    gui.questions[:] = []
    gui.weight_entries.clear()
    if import_first is not None:
        gui._on_load_config(import_first)     # 真实顺序：先导入配置，再探测
    gui._on_detect_questions()
    return logs


def _web_detect(monkeypatch, tmp_path, driver, *, questions=None, showing=False,
                wait_ok=True, create_error=None,
                import_first: str | None = None):
    """webui 侧同一套注入：``spawn`` 换成就地执行，超时与停顿都设成 0。"""
    session = RunSession(paths=SessionPaths(str(tmp_path / "web")),
                         availability=Availability(config_io=True, history=True,
                                                   qr=True, selenium=True))
    config_module.WEIGHT_CONFIG.clear()     # 与桌面版同一前置
    if create_error is not None:
        def _boom(*_a, **_k):
            raise create_error
        create = _boom
    else:
        create = lambda *_a, **_k: driver      # noqa: E731
    def _web_detect_questions(_d):
        _d.detect_frame = _d.frame      # 与桌面版同一处记录点
        return [dict(q) for q in (questions or [])]

    svc = WebService(session, create_driver=create,
                     detect_questions=_web_detect_questions,
                     is_smart_verification_showing=lambda _d: showing,
                     wait_for_manual_verification=lambda _d, **_k: wait_ok,
                     spawn=lambda fn: fn(), settle_seconds=0.0, sleeper=lambda _s: None,
                     page_ready_timeout=1, question_ready_timeout=1,
                     history_db_cls=lambda _p: None, **CONFIG_IO)
    session.set_field("url", DETECT_URL)
    if import_first is not None:
        svc.import_config(import_first)     # 真实顺序：先导入配置，再探测
    svc.detect_questions()
    logs = [(row["tag"], row["text"]) for row in session.log_lines]
    return session, svc, logs


def _texts_from_tk(gui) -> dict[int, str]:
    return {qi: var.get() for qi, var in gui.weight_entries.items()}


def _texts_from_web(session) -> dict[int, str]:
    return {int(r["q"]): r["text"] for r in session.table_rows()}


PROBE_PRESET = {
    1: {"type": "single", "weights": [0.2, 0.3, 0.5]},
    2: {"type": "scale", "scale": 5, "scale_min": 1, "weights": [5, 4, 3, 2, 1]},
    3: {"type": "text", "field": "phone", "options": ["13800000000"]},
    4: {"type": "matrix_multi", "rows": ["1", "2"], "cols": ["a", "b"],
        "row_weights": {"1": [1, 2], "2": [3, 4]}},
}


@pytest.mark.parametrize("with_config", (False, True))
def test_both_hosts_land_the_same_table_after_detecting(gui_window, monkeypatch,
                                                        tmp_path,
                                                        with_config) -> None:
    """§4 第 2 行：探测回流 + 预填。同一台假 driver、同一份探测结果，表必须一模一样。

    两种前置各跑一遍：空配置（预填=等权重）与**先导入一份配置再探测**（预填=那份配置）。
    后半截是这条用例的价值所在 —— 两侧的预填走的是两条代码：桌面版在 ``populate`` 里
    按全局 ``WEIGHT_CONFIG`` 算，webui 按 ``weight_texts`` 里记住的串。差一格的样子不是
    报错，是"我明明载入了配置，探完之后表上又变回等权重"，而点开始时表会被重新解析
    并整体写回全局 —— 那份配置就真的没了。
    """
    from src.config_io import save_weight_config

    import_file = None
    if with_config:
        import_file = str(tmp_path / "preset.json")
        save_weight_config(import_file, {k: dict(v) for k, v in PROBE_PRESET.items()},
                           meta={"name": "preset"})

    driver = _FakeDriver(counts=[len(PROBE_QUESTIONS)])
    tk_logs = _tk_detect(gui_window, monkeypatch, driver,
                         questions=PROBE_QUESTIONS, import_first=import_file)
    tk_questions = [dict(q) for q in gui_window.questions]
    tk_texts = _texts_from_tk(gui_window)
    tk_quit = driver.quit_calls

    web_driver = _FakeDriver(counts=[len(PROBE_QUESTIONS)])
    session, svc, web_logs = _web_detect(monkeypatch, tmp_path, web_driver,
                                         questions=PROBE_QUESTIONS,
                                         import_first=import_file)
    svc.close_db()

    assert [dict(q) for q in session.questions] == tk_questions, "回流的题目表不一样"
    assert _texts_from_web(session) == tk_texts, "探测回流的预填串不一样"
    assert tk_texts[1] == ("0.2000,0.3000,0.5000" if with_config
                           else "0.3333,0.3333,0.3333")
    assert tk_texts[2] == ("5.0000,4.0000,3.0000,2.0000,1.0000" if with_config
                           else "1,1,1,1,1")
    assert tk_texts[4] == ("1:1,2 | 2:3,4" if with_config else "")
    assert [t for _tag, t in web_logs][-2:] == [t for _tag, t in tk_logs][-2:], (
        "探测完成那两句总结（题数与题型分布）是用户判断探对没探对的唯一依据")
    assert web_driver.quit_calls == tk_quit == 1, "探测收尾必须关浏览器，且只关一次"
    gui_window.questions.clear()
    gui_window.weight_entries.clear()


def test_questions_are_found_inside_an_iframe_on_both_hosts(gui_window, monkeypatch,
                                                            tmp_path) -> None:
    """iframe 兜底：主文档 0 题、第一个 iframe 有题。

    真正要比的是**在哪个帧里读的题**：读错帧就是"探测成功但题是空的"，
    而探完之后两边都会切回主文档（收尾动作一致，不是这条的看点）。
    """
    tk_driver = _FakeDriver(counts=[0, 2])
    _tk_detect(gui_window, monkeypatch, tk_driver, questions=PROBE_QUESTIONS[:2])

    web_driver = _FakeDriver(counts=[0, 2])
    session, svc, _logs = _web_detect(monkeypatch, tmp_path, web_driver,
                                      questions=PROBE_QUESTIONS[:2])
    svc.close_db()
    assert (tk_driver.detect_frame, web_driver.detect_frame) == (0, 0), (
        "没在命中的 iframe 里读题 = 探到了主文档的空气")
    assert tk_driver.frame == web_driver.frame is None, "收尾都得切回主文档"
    assert len(session.questions) == len(gui_window.questions) == 2
    gui_window.questions.clear()
    gui_window.weight_entries.clear()


def test_no_question_elements_anywhere_fails_the_same_way(gui_window, monkeypatch,
                                                          tmp_path) -> None:
    """主文档与所有 iframe 都没有题：一句"未能在页面中找到题目元素"，两边都得说。"""
    tk_logs = _tk_detect(gui_window, monkeypatch, _FakeDriver(counts=[0]),
                         questions=[])
    _session, _svc, web_logs = _web_detect(monkeypatch, tmp_path,
                                           _FakeDriver(counts=[0]), questions=[])
    assert [t for tag, t in web_logs if "未能在页面中找到题目元素" in t] == \
        [t for tag, t in tk_logs if "未能在页面中找到题目元素" in t]
    assert gui_window.questions == [] and _session.questions == []
    assert "探测完成" not in "".join(t for _tag, t in web_logs), "探不到题却报成功"


def test_a_verification_timeout_stops_the_probe_on_both_hosts(gui_window, monkeypatch,
                                                              tmp_path) -> None:
    """滑块没做完：两边都停在这里，且不落任何题目。"""
    tk_logs = _tk_detect(gui_window, monkeypatch, _FakeDriver(counts=[3]),
                         questions=PROBE_QUESTIONS, showing=True, wait_ok=False)
    _session, svc, web_logs = _web_detect(monkeypatch, tmp_path,
                                          _FakeDriver(counts=[3]),
                                          questions=PROBE_QUESTIONS,
                                          showing=True, wait_ok=False)
    svc.close_db()
    assert [t for t in (x for _g, x in web_logs) if "验证超时" in t] == \
           [t for _g, t in tk_logs if "验证超时" in t]
    assert gui_window.questions == [] and _session.questions == []
    assert "探测完成" not in "".join(t for _tag, t in tk_logs)


def test_a_driver_that_never_started_is_reported_the_same_way(gui_window, monkeypatch,
                                                              tmp_path) -> None:
    """driver 起不来：两边都只报一行，不 stack、不落题、不谎报成功。"""
    boom = RuntimeError("浏览器启动失败")
    tk_logs = _tk_detect(gui_window, monkeypatch, _FakeDriver(counts=[3]),
                         questions=PROBE_QUESTIONS, create_error=boom)
    _session, svc, web_logs = _web_detect(monkeypatch, tmp_path,
                                          _FakeDriver(counts=[3]),
                                          questions=PROBE_QUESTIONS,
                                          create_error=boom)
    svc.close_db()

    def _failed(lines):
        return [t for tag, t in lines if t.startswith("探测失败")]

    assert len(_failed(tk_logs)) == len(_failed(web_logs)) == 1
    assert _failed(tk_logs)[0].split(":")[0] == _failed(web_logs)[0].split(":")[0]
    assert "浏览器启动失败" in _failed(tk_logs)[0]
    assert gui_window.questions == [] and _session.questions == []


def test_the_probe_js_and_type_labels_are_one_copy_not_two() -> None:
    """``_QUESTION_COUNT_JS`` 与 ``_TYPE_LABELS`` 是**刻意复制**的两份（service.py 顶部
    写了理由：不 import gui 那份）。复制就有漂的风险，而漂的样子是"网页版把矩阵数成
    单选题" —— 所以这里直接比那两个常量本身。
    """
    from gui import controller as tk_ctl

    assert tk_ctl._QUESTION_COUNT_JS == service_module._QUESTION_COUNT_JS
    assert tk_ctl._TYPE_LABELS == service_module._TYPE_LABELS


def test_a_missing_qr_module_refuses_before_the_picker_on_both_hosts(
    gui_window, monkeypatch, tmp_path
) -> None:
    """§4 第 3 行：二维码模块整个没加载（import 失败）→ 先拒绝、不开选择框。

    注意这与"装了模块但没装 opencv"是两件事：后者**必须**照旧开选择框、选完才提示缺依赖
    （那条顺序钉在 ``tests/test_qr_utils.py``，因为两个宿主共用同一个解码函数）。
    """
    from gui import controller as ctl

    asked: list[str] = []
    monkeypatch.setattr(ctl, "decode_qr_from_image", None)
    dialogs.register_file_picker(lambda kind, _o: asked.append(kind) or None)
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(gui_window, "_log",
                        lambda msg, tag="INFO": logs.append((tag, msg)))
    monkeypatch.setattr(gui_window, "_history_get_db", lambda: None)
    try:
        gui_window._on_import_qr()
    finally:
        dialogs.register_file_picker(None)

    session, svc = _qr_session(tmp_path, decode=None)
    svc.import_qr("whatever.png")
    web_logs = _web_log_pairs(session)
    svc.close_db()

    assert asked == [], "模块没加载还开选择框 = 让人白选一遍文件"
    assert [t for _tag, t in web_logs if "二维码模块未加载" in t] == \
        [t for _tag, t in logs if "二维码模块未加载" in t], (
        f"webui={web_logs} 桌面版={logs}")


def test_a_decoded_qr_lands_in_the_url_field_on_both_hosts(gui_window, monkeypatch,
                                                           tmp_path) -> None:
    """解出 URL：两边都写进字段、都说同一句成功话。"""
    url = "https://www.wjx.test/vm/scanned.aspx"
    from gui import controller as ctl

    monkeypatch.setattr(ctl, "decode_qr_from_image", lambda _p: url)
    monkeypatch.setattr(ctl.threading, "Thread", _InlineThread)
    dialogs.register_file_picker(lambda _k, _o: "Z:/qr.png")
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(gui_window, "_log",
                        lambda msg, tag="INFO": logs.append((tag, msg)))
    try:
        gui_window._on_import_qr()
    finally:
        dialogs.register_file_picker(None)
    assert gui_window.url_var.get() == url

    session, svc = _qr_session(tmp_path, decode=lambda _p: url)
    svc.import_qr("qr.png")
    web_logs = _web_log_pairs(session)
    svc.close_db()
    assert session.url == url
    assert [t for _tag, t in web_logs if "解析成功" in t] == \
        [t for _tag, t in logs if "解析成功" in t], (
        f"webui={web_logs} 桌面版={logs}")


def test_an_unreadable_qr_says_the_same_thing_on_both_hosts(gui_window, monkeypatch,
                                                            tmp_path) -> None:
    from gui import controller as ctl

    monkeypatch.setattr(ctl, "decode_qr_from_image", lambda _p: None)
    dialogs.register_file_picker(lambda _k, _o: "Z:/qr.png")
    logs: list[tuple[str, str]] = []
    url_before = gui_window.url_var.get()
    monkeypatch.setattr(gui_window, "_log",
                        lambda msg, tag="INFO": logs.append((tag, msg)))
    try:
        gui_window._on_import_qr()
    finally:
        dialogs.register_file_picker(None)

    session, svc = _qr_session(tmp_path, decode=lambda _p: None)
    svc.import_qr("qr.png")
    web_logs = _web_log_pairs(session)
    svc.close_db()

    assert [t for _tag, t in web_logs if "未识别到二维码内容" in t] == \
        [t for _tag, t in logs if "未识别到二维码内容" in t]
    assert gui_window.url_var.get() == url_before
    assert session.url == DETECT_URL, "解不出来却改字段 = 把用户原来的 URL 弄丢"


def _qr_session(tmp_path, *, decode):
    session = RunSession(paths=SessionPaths(str(tmp_path / "web")),
                         availability=Availability(config_io=True, history=True,
                                                   qr=True, selenium=True))
    svc = WebService(session, decode_qr=decode, spawn=lambda fn: fn(),
                     history_db_cls=lambda _p: None)
    session.set_field("url", DETECT_URL)
    return session, svc


def _web_log_pairs(session) -> list[tuple[str, str]]:
    """日志要在**动作之后**读：构造时抓的那一份永远不会长出新的行。"""
    return [(row["tag"], row["text"]) for row in session.log_lines]


ROUND_OUTCOMES = ("success", "failed", "unknown", "error", "browser_dead",
                  "aborted", "还没定义过的结果")


def test_the_round_outcome_tag_table_is_checked_against_the_other_copy() -> None:
    """每轮结果用什么级别写日志，是两个宿主各抄一份的表 —— 所以必须逐键比。

    抄本漂了的样子不是报错，是"同一轮失败在网页版是绿的、在桌面版是红的"，
    而长跑几小时的人就是靠这一列颜色判断要不要停下来看的。
    """
    assert set(app_module._ROUND_LEVELS) == set(service_module._ROUND_LEVELS)
    assert set(ROUND_OUTCOMES) - {"还没定义过的结果"} <= set(
        service_module._ROUND_LEVELS), "引擎会报的结果种类没被表覆盖全"
    assert app_module._ROUND_LEVELS == service_module._ROUND_LEVELS
    # 认不出的结果两边都退回 INFO（谁都不许把未知当成失败）
    assert app_module._ROUND_LEVELS.get("啥也不是", "INFO") == \
        service_module._ROUND_LEVELS.get("啥也不是", "INFO") == "INFO"


def test_the_same_log_stream_numbers_and_degrades_the_same_way(gui_window,
                                                               tmp_path) -> None:
    """§4 第 12 行：同一条消息流，两边的行号序列、时间戳形状与"不认识的 tag"处理一致。

    时间戳两边都是 ``HH:MM:SS``；不认识的 tag 谁都不许丢字 —— 桌面版退成 INFO，
    webui 保留原样交给 CSS，没有规则就落回默认色。
    """
    stream = [("开跑", "HEADER"), ("第 1 份成功", "OK"), ("第 2 份失败", "FAIL"),
              ("警告", "WARN"), ("没见过的级别", "BOGUS")]

    view = gui_window._log_view
    assert view is not None, "前置：整窗构造时日志面板就该在"
    for message, tag in stream:
        gui_window._log(message, tag)
    gui_window._drain_log_queue()
    tk_text = view.log_text.get("1.0", tk.END)
    tk_gutter = view.log_lineno.get("1.0", tk.END)

    session = RunSession(paths=SessionPaths(str(tmp_path / "web")),
                         availability=Availability(config_io=True, history=True,
                                                   qr=True, selenium=True))
    web_rows = [session.log(msg, tag) for msg, tag in stream]

    # 队列里可能还压着构造期与前面用例留下的行，所以只比"这一段"的形状
    cells = [c for c in tk_gutter.split("\n") if c]
    tail = cells[-len(stream):]
    assert [int(c) for c in tail] == list(
        range(int(tail[0]), int(tail[0]) + len(stream))), "行号断号或重号"
    assert all(len(c) == 4 for c in tail), f"行号列不再右对齐 4 位：{tail!r}"
    assert len(cells) == view._lineno_count, "行号计数器和 gutter 对不上"
    assert [r["n"] for r in web_rows] == list(range(1, 6)), "webui 的行号得从 1 连号"
    assert (int(tail[-1]) - int(tail[0])) == (web_rows[-1]["n"] - web_rows[0]["n"]) == 4
    for row in web_rows:
        ts = row["ts"]
        assert len(ts) == 8 and ts[2] == ts[5] == ":", f"时间戳形状变了：{ts!r}"
    for message, _tag in stream:
        assert message in tk_text, f"桌面版把 {message!r} 弄丢了"
    assert "没见过的级别" in tk_text and "BOGUS" not in tk_text
    assert view.log_text.tag_names(), "Tk 侧的 tag 命名表空了 = 全部退回默认色"
    assert "BOGUS" not in view.log_text.tag_names(), "未知 tag 不该被注册成一个没配色的标签"


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
