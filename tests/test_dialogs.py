"""弹窗间接层的离线契约（v3.1 A 档第 1 条 + v4.0 桌面版退役后剩下的那一半）。

对标来源是 SurveyController 的分层做法：**只借结构，不取代码**（它 GPL-3.0，我们 MIT）。
它要解决的问题和我们一样 —— 模态框一调用就等真人点掉，测试只能把整段分支绕开。

为什么值得测：当年 README「仍然没有防线的地方」表里两行留着 GUI 的理由都写着
「模态对话框」。9 处 tkinter.messagebox 直调换成可注册出口之后，「确认框点下去那
5 个字段必须互相自洽」这条最容易造成重复提交的不变量，第一次有了离线防线。

v4.0 桌面版退役后这里只剩**出口本身**的契约；续传决策那 11 条跟着宿主一起搬走了，
它们现在的家是 tests/test_webui_service.py 的 resume 那一组。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config as config_module  # noqa: E402
from src import dialogs  # noqa: E402


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
    # 显式清空而不是假设「本来就没人注册」：同一进程里任何模块都可能注册过出口，
    # 注册成真 messagebox 的宿主虽已退役，这条判据留着 —— 它挡的是用例互相打翻。
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
#  2. 文件选择框 —— 同一类阻塞，答案是路径
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

