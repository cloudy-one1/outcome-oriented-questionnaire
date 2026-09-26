"""V2.4 新增：统一 logging 配置（此前全项目无 logging 体系）。

背景：
    - CLI 用 print() 直出（无时间戳、不落盘）
    - GUI 日志只进 Tkinter 终端控件，进程退出即丢
    - 大量 ``except Exception: pass`` 静默吞异常，事后无法复盘

约定：
    - 日志名字空间：一律挂在 ``wjx`` 下，按模块起名（``wjx.cli``、``wjx.webui.service``）
    - CLI 通过 ``--log-file PATH`` 启用文件落盘（logs/ 目录建议）
    - 未调用 setup_logging 时，logging 走默认 lastResort
      （WARNING+ 输出 stderr），对库代码零侵入

用法::

    from src.logging_setup import setup_logging, get_logger
    setup_logging("logs/run.log")          # 入口处调用一次（幂等）
    logger = get_logger("wjx.cli")
    logger.warning("...", exc_info=True)   # 静默降级路径至少留痕
"""

from __future__ import annotations

import logging
import os
import sys

_LOGGER_NAMESPACE = "wjx"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_CONFIGURED = False


def _add_file_handler(logger: logging.Logger, log_file: str, level: int) -> None:
    """为 logger 追加一个落盘 handler；**同一个文件不重复挂**。

    FileHandler.baseFilename 已是绝对路径，可以直接拿来判重。此前没有这一步，
    ``setup_logging(p)`` 连调两次就会在同一文件上挂两个 handler，每条记录写两遍。
    """
    target = os.path.abspath(log_file)
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename == target:
            handler.setLevel(level)      # 已存在 → 只调级别，不再追加
            return
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fh = logging.FileHandler(target, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(fh)


def setup_logging(log_file: str | None = None, *, level: int = logging.INFO) -> None:
    """配置 ``wjx`` 名字空间的根 logger（幂等，可重复调用）。

    :param log_file: 可选日志文件路径；给出时追加 FileHandler（目录自动创建，
                     同一路径重复传不会挂第二个 handler）。
    :param level:    logger 级别（默认 INFO）。**每次调用都生效** —— 否则
                     「先 setup_logging() 再 setup_logging(p, level=DEBUG)」会
                     得到一个 DEBUG 的 handler 配 INFO 的 logger，落盘出来是空文件。
    """
    global _CONFIGURED
    root = logging.getLogger(_LOGGER_NAMESPACE)
    root.setLevel(level)
    if _CONFIGURED:
        # 幂等：console handler 只挂一次；file handler 按需追加
        if log_file:
            _add_file_handler(root, log_file, level)
        return
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(stream)
    if log_file:
        _add_file_handler(root, log_file, level)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str = _LOGGER_NAMESPACE) -> logging.Logger:
    """取 ``wjx`` 名字空间下的 logger（``get_logger("wjx.webui.service")`` 等）。"""
    if not name.startswith(_LOGGER_NAMESPACE):
        name = f"{_LOGGER_NAMESPACE}.{name}"
    return logging.getLogger(name)
