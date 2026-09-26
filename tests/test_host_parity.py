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
