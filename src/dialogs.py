"""弹窗间接层：把「要人看一眼 / 点一下」的动作从具体 toolkit 里解出来。

WHY：gui/ 此前有 9 处直调 ``tkinter.messagebox``，一调用就阻塞等真人点掉，离线
测试只能把整段分支绕开 —— 这是 ``gui/app.py``、``gui/controller.py`` 两块覆盖率
长期停在 25% / 13% 的直接原因（README「仍然没有防线的地方」表里那两行的理由就是
"模态对话框"）。现在出口收敛成一个可注册的 handler：GUI 启动时注入真 messagebox，
CLI 与测试不注入就拿到**确定性默认值**，于是"弹窗之后走哪条路"第一次变成能在 CI
里钉住的契约。

确认类默认 ``False`` 而不是 ``True``：确认框问的都是"要不要多做一件事"（续传上次
批次、覆盖当前配置），无人应答时不做比做错容易恢复。

文件选择框（``pick_open_path`` / ``pick_save_path``）走同一套分层，默认值是
``None`` —— 与"用户按了取消"同义，四个调用点本来就有那条分支。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("wjx.dialogs")

PopupKind = str  # "info" | "warning" | "error" | "confirm"
PopupHandler = Callable[[PopupKind, str, str], Any]

_handler: PopupHandler | None = None


def register_popup_handler(handler: PopupHandler | None) -> None:
    """注入真正的弹窗实现；传 ``None`` 清除（测试恢复现场用）。"""
    global _handler
    _handler = handler


def current_popup_handler() -> PopupHandler | None:
    return _handler


def _ask(kind: PopupKind, title: str, message: str, default: Any) -> Any:
    handler = _handler
    if handler is None:
        logger.warning(
            "[popup:%s] %s | %s（无 GUI 宿主，按默认值 %r 处理）",
            kind, title, message, default,
        )
        return default
    try:
        return handler(kind, title, message)
    except Exception as exc:
        # 弹窗从来不是值得让一轮提交崩掉的理由：关窗竞态下 Tk 已经销毁，异常抛回
        # 调用方会把 worker 线程一起带走。记一条日志，仍按默认值走。
        logger.warning(
            "[popup:%s] 弹窗宿主执行失败，按默认值 %r 处理: %s: %s",
            kind, default, type(exc).__name__, exc,
        )
        return default


def popup_info(title: str, message: str) -> None:
    _ask("info", title, message, None)


def popup_warning(title: str, message: str) -> None:
    _ask("warning", title, message, None)


def popup_error(title: str, message: str) -> None:
    _ask("error", title, message, None)


def popup_confirm(title: str, message: str) -> bool:
    """是/否确认。无宿主或宿主抛错时返回 ``False``（理由见模块 docstring）。"""
    return bool(_ask("confirm", title, message, False))


# ============================================================================
#  文件选择框：同一类阻塞，只是答案是路径而不是"是/否"
# ============================================================================
FilePickerKind = str  # "open" | "save"
FilePicker = Callable[[FilePickerKind, dict[str, Any]], "str | None"]

_picker: FilePicker | None = None


def register_file_picker(picker: FilePicker | None) -> None:
    global _picker
    _picker = picker


def current_file_picker() -> FilePicker | None:
    return _picker


def _pick(kind: FilePickerKind, options: dict[str, Any]) -> "str | None":
    picker = _picker
    if picker is None:
        logger.warning("[picker:%s] %s（无 GUI 宿主，视为未选文件）", kind, options)
        return None
    try:
        return picker(kind, options) or None
    except Exception as exc:
        # 与 popup 同一套理由：选文件失败不该让调用它的命令整段崩掉。
        logger.warning(
            "[picker:%s] 文件宿主执行失败，视为未选文件: %s: %s",
            kind, type(exc).__name__, exc,
        )
        return None


def pick_open_path(**options: Any) -> "str | None":
    """``None`` = 用户取消 —— 四个调用点本来就各有"没选就不做"的分支。"""
    return _pick("open", options)


def pick_save_path(**options: Any) -> "str | None":
    return _pick("save", options)

