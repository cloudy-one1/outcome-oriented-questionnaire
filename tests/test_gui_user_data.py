"""用户数据树隔离（v3.1 A 档第 3 条）—— `SurveyGUI` 第一次能在测试里被真的建出来。

此前 README「仍然没有防线的地方」里 `gui/app.py` 那行的理由是：
`SurveyGUI.__init__` 一建就打开真实 `data/history.db` 并启动动画 `after` 循环，
测试里无法安全实例化。现在 `WJX_USER_DATA_DIR` 能把 `configs/` + `data/` 整棵
挪到 tmp，于是那 500 来行未覆盖代码里最外面的一圈（构造、输入校验、历史库接线）
第一次有防线。

对标来源是 SurveyController 的做法（`CI/conftest.py:47-58` 用一个环境变量把
持久化配置指到 tmp，配上 autouse 夹具）：**借分层，不取代码**（它 GPL-3.0）。

窗口只在整模块构造一次，且挂在 ``conftest.py`` 那个会话级根窗口的 Toplevel 上
（一个进程只能可靠地建一个 Tk 根窗口，见该夹具的 WHY）；建不出来（无显示的
runner）就整模块 skip，CI 保持绿。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")

from gui import app as app_module  # noqa: E402
from gui.app import SurveyGUI  # noqa: E402
from src import config as config_module  # noqa: E402
from src import dialogs  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_DB = os.path.join(REPO_ROOT, "data", "history.db")
REAL_DEFAULT_CFG = os.path.join(
    REPO_ROOT, "configs", "default_weight_config.json"
)


def _sig(path: str) -> tuple[int, int] | None:
    """文件的"变没变过"指纹。不存在记 None —— 不存在却被打开了也要能看出来。"""
    if not os.path.exists(path):
        return None
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


@pytest.fixture(autouse=True)
def _restore_dialog_handler():
    """`src.dialogs` 是模块级单例，而 SurveyGUI 构造时会往里塞真 messagebox。"""
    prev = dialogs.current_popup_handler()
    prev_picker = dialogs.current_file_picker()
    prev_weight = dict(config_module.WEIGHT_CONFIG)
    yield
    dialogs.register_popup_handler(prev)
    dialogs.register_file_picker(prev_picker)
    config_module.WEIGHT_CONFIG.clear()
    config_module.WEIGHT_CONFIG.update(prev_weight)


@pytest.fixture(scope="module")
def gui(tk_root):
    """(app, tmp 数据根)。建不出来就 skip，销毁放在 finalizer 里。

    ``SurveyGUI.__init__`` 里写死的是 ``tk.Tk()``，而一个进程只能可靠地建一个根
    窗口（见 ``conftest.tk_root`` 的 WHY）—— 所以构造期间把 ``tkinter.Tk`` 临时
    换成"返回共享根窗口下的 Toplevel"。Toplevel 支持本类用到的全部根接口
    （title / geometry / minsize / configure / protocol / after / destroy），
    销毁它不动根窗口，后面的模块照样有用。
    """
    tmp = tempfile.mkdtemp(prefix="wjx_gui_app_")
    prev_env = os.environ.get(app_module.USER_DATA_DIR_ENV)
    os.environ[app_module.USER_DATA_DIR_ENV] = tmp
    repo_before = (_sig(REAL_DB), _sig(REAL_DEFAULT_CFG))
    app: SurveyGUI | None = None
    real_tk_ctor = tk.Tk
    tk.Tk = lambda *_a, **_k: tk.Toplevel(tk_root)  # type: ignore[assignment]
    try:
        try:
            app = SurveyGUI()
        except Exception as exc:  # 无显示 / 未装 tk
            pytest.skip(f"无法构造 SurveyGUI: {type(exc).__name__}: {exc}")
        app.root.withdraw()  # 别在 CI 上、也别在开发机上弹一个真窗口
        yield app, tmp
    finally:
        tk.Tk = real_tk_ctor
        if app is not None:
            after_info = getattr(app.root, "after_info", None)
            if after_info is not None:  # Tk.after_info 是 3.11 才进 tkinter 的
                for job in after_info():
                    app.root.after_cancel(job)
            app.running = False
            try:
                app.root.destroy()
            except Exception:  # pragma: no cover - 根窗口已随进程回收
                pass
            # 窗口没了，它注册的两个出口就成了死引用 —— 不摘掉的话同进程的下一个
            # 模块撞上它就是满屏 TclError（被 _ask 吞掉，但日志一片红）。
            dialogs.register_popup_handler(None)
            dialogs.register_file_picker(None)
        if prev_env is None:
            os.environ.pop(app_module.USER_DATA_DIR_ENV, None)
        else:
            os.environ[app_module.USER_DATA_DIR_ENV] = prev_env
        shutil.rmtree(tmp, ignore_errors=True)
        repo_after = (_sig(REAL_DB), _sig(REAL_DEFAULT_CFG))
        assert repo_after == repo_before, (
            "测试期间仓库里的真实数据树被动过："
            f"{repo_before} -> {repo_after}"
        )


def _recorder(seen: list[tuple[str, str, str]]) -> dialogs.PopupHandler:
    """把提示记进 `seen`；确认一律答"否"—— 本模块没有用例该真起批次。"""

    def handler(kind: str, title: str, message: str) -> bool:
        seen.append((kind, title, message))
        return False

    return handler


# ---------------------------------------------------------------------------
#  1. 路径解析（不需要 Tk）
# ---------------------------------------------------------------------------
def test_env_override_moves_all_three_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(app_module.USER_DATA_DIR_ENV, str(tmp_path))
    assert app_module.user_data_root() == os.path.abspath(str(tmp_path))
    assert app_module.default_config_dir() == os.path.join(
        os.path.abspath(str(tmp_path)), "configs"
    )
    assert app_module.default_weight_config_path() == os.path.join(
        os.path.abspath(str(tmp_path)), "configs", "default_weight_config.json"
    )
    assert app_module.default_history_db_path() == os.path.join(
        os.path.abspath(str(tmp_path)), "data", "history.db"
    )


def test_without_env_paths_stay_in_the_repo(monkeypatch) -> None:
    monkeypatch.delenv(app_module.USER_DATA_DIR_ENV, raising=False)
    assert app_module.user_data_root() == app_module._PROJECT_ROOT
    assert app_module.default_history_db_path() == os.path.join(
        app_module._PROJECT_ROOT, "data", "history.db"
    )


def test_relative_env_override_is_absolutized(monkeypatch, tmp_path) -> None:
    """相对路径若原样传下去，工作目录一变就写到别处去了 —— 所以入口就 abs。"""
    monkeypatch.chdir(str(tmp_path))
    monkeypatch.setenv(app_module.USER_DATA_DIR_ENV, "wjx_data")
    assert app_module.user_data_root() == os.path.join(str(tmp_path), "wjx_data")
    assert os.path.isabs(app_module.default_history_db_path())


# ---------------------------------------------------------------------------
#  2. 真的把窗口建出来
# ---------------------------------------------------------------------------
def test_constructed_gui_reads_only_from_the_tmp_tree(gui) -> None:
    app, tmp = gui
    root = os.path.abspath(tmp)
    assert os.path.isabs(root)
    assert app._history_db_path.startswith(root)
    assert app._default_weight_config_path.startswith(root)
    assert app._user_data_root == root


def test_construction_creates_the_db_inside_tmp_only(gui) -> None:
    app, tmp = gui
    assert os.path.exists(app._history_db_path), "历史库应在被指到的 tmp 树里被建出来"
    assert not os.path.exists(app._default_weight_config_path), (
        "自动载入是只读动作：没配置就是不落文件"
    )


def test_history_get_db_is_wired_to_the_tmp_path(gui) -> None:
    app, _tmp = gui
    db = app._history_get_db()
    assert db is not None, "装了 src.history 就该拿得出实例（缺库时才会是 None）"
    assert os.path.abspath(db.db_path) == os.path.abspath(app._history_db_path)


def test_construction_registers_both_dialog_hosts(gui) -> None:
    """这条是下面所有"弹窗/文件框"用例的前提：注册一旦被摘掉，测试里的
    "无弹窗"就成了 src.dialogs 的默认值而不是真行为，断言会失去意义。"""
    app, _tmp = gui
    assert dialogs.current_popup_handler() == app._popup_handler
    assert dialogs.current_file_picker() == app._file_picker


def test_on_start_rejects_empty_url_with_one_warning_and_no_thread(gui) -> None:
    app, _tmp = gui
    seen: list[tuple[str, str, str]] = []
    dialogs.register_popup_handler(_recorder(seen))
    app.url_var.set("")
    app.count_var.set(5)
    app._on_start()
    assert [s[0] for s in seen] == ["warning"]
    assert "URL" in seen[0][2]
    assert app.running is False
    assert app._run_thread is None, "校验没过就起了线程 = 白开一个浏览器"


def test_on_start_rejects_zero_target_count(gui) -> None:
    app, _tmp = gui
    seen: list[tuple[str, str, str]] = []
    dialogs.register_popup_handler(_recorder(seen))
    app.url_var.set("https://www.wjx.cn/vj/survey1.aspx")
    app.count_var.set(0)
    app._on_start()
    assert [s[0] for s in seen] == ["warning"]
    assert app.running is False and app._run_thread is None


# ---------------------------------------------------------------------------
#  3. 命令处理器 —— 文件框有替身之后，配置导入导出第一次可测
# ---------------------------------------------------------------------------
_CFG: dict[int, dict] = {
    1: {"type": "single", "weights": [1, 3]},
    2: {"type": "multi", "weights": [2, 1], "count_options": [1, 2]},
}


class _Logs(list):
    """顶掉 `app._log`：控制器每次都是 `self.host._log(...)` 现取，补丁打得开。"""

    def __init__(self) -> None:
        super().__init__()

    def __call__(self, msg: str, tag: str = "INFO") -> None:
        self.append((str(msg), tag))

    def tags(self, needle: str) -> list[str]:
        return [m for m, _t in self if needle in m]


@pytest.fixture()
def logs(gui, monkeypatch) -> _Logs:
    app, _tmp = gui
    rec = _Logs()
    monkeypatch.setattr(app, "_log", rec)
    return rec


def test_save_config_writes_the_picked_path(gui, monkeypatch, logs, tmp_path) -> None:
    app, _root = gui
    out = tmp_path / "picked" / "weight_config.json"
    asked: list[str] = []

    def picker(kind: str, opts: dict) -> str:
        asked.append(kind)
        return str(out)

    dialogs.register_file_picker(picker)
    monkeypatch.setattr(app, "_build_weight_config", lambda: dict(_CFG))
    app.url_var.set("https://www.wjx.cn/vj/survey1.aspx")
    app._controller.on_save_config()

    assert asked == ["save"]
    assert out.exists(), "替身给的路径要真的落盘（父目录由 config_io 建）"
    assert logs.tags("已导出配置"), f"日志只有成功这一条路：{logs}"


def test_load_config_roundtrips_what_save_wrote(gui, monkeypatch, logs, tmp_path) -> None:
    """导出→清空→经文件框导入，全局 WEIGHT_CONFIG 必须回到导出的那份。

    这条同时钉住 v2.x 修过的语义：导入是**整体替换**，残留旧题号会让上一份
    问卷的权重静默生效。
    """
    app, _root = gui
    out = tmp_path / "roundtrip.json"
    monkeypatch.setattr(app, "_build_weight_config", lambda: dict(_CFG))
    dialogs.register_file_picker(lambda kind, opts: str(out))
    app._controller.on_save_config()

    config_module.WEIGHT_CONFIG.clear()
    config_module.WEIGHT_CONFIG[99] = {"type": "single", "weights": [1]}
    dialogs.register_file_picker(lambda kind, opts: str(out))
    app._controller.on_load_config()          # path=None → 必须去问文件框

    assert dict(config_module.WEIGHT_CONFIG) == _CFG
    assert 99 not in config_module.WEIGHT_CONFIG
    assert logs.tags("已载入")


def test_cancelled_picker_changes_nothing(gui, monkeypatch, logs, tmp_path) -> None:
    """取消 = 整条命令什么都没做：不写文件、不改全局权重、不记成功日志。"""
    app, _root = gui
    monkeypatch.setattr(app, "_build_weight_config", lambda: dict(_CFG))
    dialogs.register_file_picker(lambda kind, opts: "")
    config_module.WEIGHT_CONFIG.clear()
    before = dict(config_module.WEIGHT_CONFIG)

    app._controller.on_save_config()
    app._controller.on_load_config()

    assert dict(config_module.WEIGHT_CONFIG) == before
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []
    assert logs == [], f"取消之后不该留下任何动作日志：{logs}"


def test_save_default_config_lands_in_the_tmp_tree(gui, monkeypatch, logs) -> None:
    """`另存为默认`写的是 `configs/default_weight_config.json` —— 有了
    `WJX_USER_DATA_DIR` 之后它落在被指到的那棵树里，仓库那份不动
    （动没动由 `gui` 夹具的 finalizer 统一比对）。"""
    app, root = gui
    monkeypatch.setattr(app, "_build_weight_config", lambda: dict(_CFG))
    app._controller.on_save_default_config()

    assert os.path.exists(app._default_weight_config_path)
    assert app._default_weight_config_path.startswith(root)
    assert logs.tags("已另存默认配置")
