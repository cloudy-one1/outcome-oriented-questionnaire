"""``wjx-web`` 入口：数据根解析、路径快照与启动收尾。

``WJX_USER_DATA_DIR`` 指错地方，历史库与 ``configs/`` 就会分到两棵不同的树，
而这件事只在长跑结束、回头去找那个批次的时候才暴露。桌面版在时这些路径靠
"两边逐条比对"钉住（``tests/test_history_gui_contract.py`` 那个年代的同一条思路）；
v4.0 宿主退役后，期望值改成本文件里的字面量 —— 数据树的位置从此只有一个宿主
可以说错，说错了要能立刻读出来。

另外钉住 ``_reap_orphans``：它决定了下次启动会不会提示"从早已死掉的批次继续"，
而续传可能把同一份问卷交两遍。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webui import __main__ as entry  # noqa: E402
from webui.session import SessionPaths  # noqa: E402


# ---------------------------------------------------------- 数据根与三条路径

@pytest.mark.parametrize("case", ["absolute", "relative", "empty", "whitespace"])
def test_the_env_override_is_resolved_against_the_cwd(monkeypatch, tmp_path,
                                                     case) -> None:
    """覆盖值一律按**当前工作目录**绝对化，而不是按包目录 —— 相对路径的含义。"""
    default_root = entry.user_data_root()          # 还没设置环境变量：拿到默认根
    monkeypatch.delenv(entry.USER_DATA_DIR_ENV, raising=False)
    if case == "absolute":
        monkeypatch.setenv(entry.USER_DATA_DIR_ENV, str(tmp_path))
        expected = str(tmp_path)
    elif case == "relative":
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(entry.USER_DATA_DIR_ENV, "nested/deeper")
        expected = os.path.join(str(tmp_path), "nested", "deeper")
    elif case == "empty":
        monkeypatch.setenv(entry.USER_DATA_DIR_ENV, "")
        expected = default_root                    # 空串判假，落回默认根
    else:
        # 只含空白：``if override:`` 判真，于是绝对化成一个尾随空格的怪路径。
        # 这不是"修好的行为"，是原样保留的粗笨 —— 钉住是为了它别在不知不觉中被改。
        monkeypatch.setenv(entry.USER_DATA_DIR_ENV, " ")
        expected = os.path.abspath(" ")
    assert entry.user_data_root() == expected


def test_the_default_data_root_is_the_project_tree(monkeypatch) -> None:
    """不设环境变量时，数据根就是仓库自己那一棵树 —— 历史库与配置从此同根。"""
    monkeypatch.delenv(entry.USER_DATA_DIR_ENV, raising=False)
    root = entry.user_data_root()
    assert os.path.isfile(os.path.join(root, "pyproject.toml")), root
    assert os.path.isdir(os.path.join(root, "src")) and os.path.isdir(os.path.join(root, "webui"))


def test_session_paths_are_exactly_these_three_files(monkeypatch, tmp_path) -> None:
    """配置目录 / 默认配置 / 历史库：三个名字写死在这儿，改了要连带迁移。"""
    monkeypatch.setenv(entry.USER_DATA_DIR_ENV, str(tmp_path))
    root = entry.user_data_root()
    paths = SessionPaths(root)

    assert paths.config_dir == os.path.join(root, "configs")
    assert paths.default_config_path == os.path.join(
        root, "configs", "default_weight_config.json")
    assert paths.history_db_path == os.path.join(root, "data", "history.db")

def test_build_session_snapshots_the_temp_tree(monkeypatch, tmp_path) -> None:
    """会话构造时读一次环境变量 —— 之后改环境变量不该挪走已开的数据树。"""
    monkeypatch.setenv(entry.USER_DATA_DIR_ENV, str(tmp_path))
    session = entry.build_session()
    monkeypatch.setenv(entry.USER_DATA_DIR_ENV, str(tmp_path / "later"))

    assert session.paths.user_data_root == os.path.abspath(str(tmp_path))
    assert session.paths.history_db_path == os.path.join(
        str(tmp_path), "data", "history.db")


def test_build_service_actually_wires_the_three_config_io_names(
    monkeypatch, tmp_path
) -> None:
    """名字接不上时按钮自己变灰 —— 而变灰是静默的。

    ``build_service`` 把 import 失败咽成 ``pass``：对"用户少装了一个包"是对的，
    对"仓库里把 ``validate_weight_config`` 改了名"就是灾难。桌面版
    ``gui/controller.py`` 顶部那段注释记的就是同一件事付过的账（三个名字从
    不存在的模块路径导入，整块必然失败被吞掉，两个按钮静默失效）。
    """
    monkeypatch.setenv(entry.USER_DATA_DIR_ENV, str(tmp_path))
    service = entry.build_service(entry.build_session())

    for attr in ("_save_wc", "_load_wc", "_validate_wc"):
        assert getattr(service, attr) is not None, f"{attr} 没接上 src/config_io"


# ------------------------------------------------------------- 命令行参数

def test_the_console_defaults_to_a_private_port_and_no_browser(  # noqa: E402
) -> None:
    """默认端口 0（系统分配）：本机可能已经开着桌面版或第二个控制台。

    默认不弹浏览器：脚本与测试要能起来而不劫持前台。
    """
    args = entry.parse_args([])
    assert args.port == 0
    assert args.open_browser is False


def test_parse_args_accepts_port_and_open_browser() -> None:
    args = entry.parse_args(["--port", "8765", "--open-browser"])
    assert args.port == 8765
    assert args.open_browser is True


# ---------------------------------------------------------------- 孤儿批次

class _FakeRunDB:
    def __init__(self, reaped=0, boom=False):
        self._reaped = reaped
        self._boom = boom
        self.calls = 0

    def reap_stale_runs(self):
        self.calls += 1
        if self._boom:
            raise OSError("database is locked")
        return self._reaped


class _FakeService:
    def __init__(self, db):
        self._db = db
        self.logs: list[tuple[str, str]] = []

    def get_db(self):
        return self._db

    @property
    def session(self):
        return self

    def log(self, msg, tag="INFO"):
        self.logs.append((msg, tag))


def test_orphan_batches_are_reported_so_the_user_sees_the_rejudge() -> None:
    """改判成 failed 必须留一行日志：否则"续传提示不见了"会被当成新 bug。"""
    service = _FakeService(_FakeRunDB(reaped=2))
    entry._reap_orphans(service)

    assert service._db.calls == 1
    assert len(service.logs) == 1
    msg, tag = service.logs[0]
    assert "2" in msg and "failed" in msg
    assert tag == "WARN"


def test_a_clean_start_says_nothing() -> None:
    service = _FakeService(_FakeRunDB(reaped=0))
    entry._reap_orphans(service)
    assert service.logs == []


def test_no_history_db_is_not_an_error() -> None:
    service = _FakeService(None)
    entry._reap_orphans(service)
    assert service.logs == []


def test_a_failing_reap_never_blocks_the_console() -> None:
    """历史库被另一个进程占用时，界面照样要起 —— 这是纯锦上添花的一步。"""
    service = _FakeService(_FakeRunDB(boom=True))
    entry._reap_orphans(service)

    assert len(service.logs) == 1
    assert "孤儿批次收尾失败" in service.logs[0][0]


# ---------------------------------------------------------------- main 收尾

class _EntryService:
    """``main`` 用到的那四个方法的最小替身。"""

    def __init__(self) -> None:
        self.session = object()
        self.closed = 0

    def auto_load_default_config(self) -> None:
        pass

    def get_db(self):
        return None

    def close_db(self) -> None:
        self.closed += 1


@pytest.fixture()
def wired_entry(monkeypatch):
    """把 ``main`` 的四个外部接缝（建会话 / 建服务 / 收孤儿 / 跑服务）换成替身。"""
    service = _EntryService()
    monkeypatch.setattr(entry, "build_session", lambda: object())
    monkeypatch.setattr(entry, "build_service", lambda session: service)
    monkeypatch.setattr(entry, "_reap_orphans", lambda svc: None)
    return service


def test_main_closes_the_db_even_when_the_server_raises(
    wired_entry, monkeypatch
) -> None:
    """``finally`` 里那句 close_db 是唯一能收住 SQLite 句柄的地方。

    服务正常退出、Ctrl-C、以及起服务本身炸掉，三种收场都得走同一条收尾；
    否则历史库的连接会留在进程里，Windows 上连带 tmp 树删不掉。
    """
    def _boom(session, svc, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(entry, "run", _boom)
    with pytest.raises(KeyboardInterrupt):
        entry.main([])

    assert wired_entry.closed == 1


def test_main_returns_the_server_exit_code(wired_entry, monkeypatch) -> None:
    monkeypatch.setattr(entry, "run",
                        lambda session, svc, **kwargs: 3)

    assert entry.main(["--port", "0"]) == 3
    assert wired_entry.closed == 1
