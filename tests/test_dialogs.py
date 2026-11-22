"""弹窗间接层的离线契约（v3.1 A 档第 1 条 + v4.0 桌面版退役后剩下的那一半）。

对标来源是 SurveyController 的分层做法：**只借结构，不取代码**（它 GPL-3.0，我们 MIT）。
它要解决的问题和我们一样 —— 模态框一调用就等真人点掉，测试只能把整段分支绕开。

为什么值得测：当年 README「仍然没有防线的地方」表里两行留着 GUI 的理由都写着
「模态对话框」。9 处 tkinter.messagebox 直调换成可注册出口之后，「确认框点下去那
5 个字段必须互相自洽」这条最容易造成重复提交的不变量，第一次有了离线防线。

v4.0 桌面版退役后这里只剩**出口本身**的契约；续传决策那 11 条跟着宿主一起搬走了，
它们现在的家是 tests/test_webui_service.py 的 resume 那一组。confirm 出口与
文件选择器随 GUI 一起没了调用方，v4.5 删除 —— 现存生产方（qr_utils / webui）
只用提示三件套。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import dialogs  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_dialog_state():
    """``src.dialogs`` 是模块级单例，不还原现场就会串到下一个用例。"""
    prev_handler = dialogs.current_popup_handler()
    yield
    dialogs.register_popup_handler(prev_handler)


# ---------------------------------------------------------------------------
#  1. src/dialogs.py —— 出口本身的契约
# ---------------------------------------------------------------------------
def test_unregistered_handler_is_none_until_registered() -> None:
    # 显式清空而不是假设「本来就没人注册」：同一进程里任何模块都可能注册过出口，
    # 这条判据挡的是用例互相打翻。
    dialogs.register_popup_handler(None)
    assert dialogs.current_popup_handler() is None


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
        dialogs.popup_warning("提示", "正文")
        dialogs.popup_error("错误", "正文")
    assert calls == ["warning", "error"], "抛错仍要发出出口调用，但不改判"
    assert "main window has been destroyed" in caplog.text


def test_register_none_clears_handler() -> None:
    dialogs.register_popup_handler(lambda k, t, m: True)
    dialogs.register_popup_handler(None)
    assert dialogs.current_popup_handler() is None
