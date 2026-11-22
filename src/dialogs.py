"""弹窗间接层：把「要人看一眼」的动作从具体 toolkit 里解出来。

WHY：gui/ 此前有 9 处直调 ``tkinter.messagebox``，一调用就阻塞等真人点掉，离线
测试只能把整段分支绕开。现在出口收敛成一个可注册的 handler：宿主启动时注入
真实现（webui 注册的是"转 ValidationError"），CLI 与测试不注入就拿到**确定性
默认值**，于是"弹窗之后走哪条路"第一次变成能在 CI 里钉住的契约。

v4.0 桌面版退役后剩下的生产调用方是 ``src/qr_utils.py``（二维码导入的三种
提示）与 webui 的服务层。曾经的 confirm 出口与文件选择器（``pick_open_path`` /
``pick_save_path``）随 GUI 一起没了调用方，v4.5 删除 —— 现存生产方只用提示
三件套，见 ``_ask``。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("wjx.dialogs")

PopupKind = str  # "info" | "warning" | "error"
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
