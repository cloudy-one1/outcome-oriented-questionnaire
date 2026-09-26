"""``wjx-web`` 入口：起本地服务、开浏览器、Ctrl-C 收尾。

设计稿：``docs/design/DESIGN_webui.md`` §8。

数据根目录的解析在这里**自己写了一份**（5 行）。它曾是 ``gui.app`` 那份的第二份抄本，
对岸退役之后这里是唯一一处 —— 三条路径的落点、覆盖值相对 **cwd** 解析、以及
空白值判真这种粗笨行为，全部由 ``tests/test_webui_entry.py`` 按字面量钉着：
``WJX_USER_DATA_DIR`` 指错地方会让历史库与配置分到两棵不同的树，而那只在
长跑结束回头找批次时才看得见。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from webui.server import run
from webui.session import Availability, RunSession, SessionPaths

USER_DATA_DIR_ENV = "WJX_USER_DATA_DIR"


def user_data_root() -> str:
    override = os.environ.get(USER_DATA_DIR_ENV)
    if override:
        return os.path.abspath(override)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_session() -> RunSession:
    return RunSession(paths=SessionPaths(user_data_root()),
                      availability=Availability.probe())


def build_service(session: RunSession) -> Any:
    from webui.service import WebService

    kwargs: dict[str, Any] = {}
    try:
        from src.config_io import (load_weight_config, save_weight_config,
                                   validate_weight_config)
        kwargs.update(save_weight_config=save_weight_config,
                      load_weight_config=load_weight_config,
                      validate_weight_config=validate_weight_config)
    except Exception:  # pragma: no cover - 缺依赖时按钮自己变灰
        pass
    return WebService(session, **kwargs)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wjx-web",
        description="问卷星自动填写工具 —— 本地 Web 控制台（只监听 127.0.0.1）")
    parser.add_argument("--port", type=int, default=0,
                        help="监听端口，默认 0 由系统分配并打印实际地址")
    parser.add_argument("--open-browser", action="store_true",
                        help="启动后自动打开默认浏览器（默认不打开）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session = build_session()
    service = build_service(session)
    service.auto_load_default_config()
    _reap_orphans(service)
    try:
        return run(session, service, port=args.port,
                   open_browser=args.open_browser)
    finally:
        service.close_db()


def _reap_orphans(service: Any) -> None:
    """启动时收尾上次被强杀留下的 ``running`` 批次 —— 与桌面版同一步。

    ``running`` 会被 find_resumable_run 当成可恢复；不改判的话下次会提示
    "从某个早已死掉的批次继续"，而那个批次的页面状态完全未知，续传可能重复提交。
    """
    db = service.get_db()
    if db is None:
        return
    try:
        reaped = db.reap_stale_runs()
        if reaped:
            service.session.log(
                f"[历史] 已把 {reaped} 个未正常收尾的批次改判为 failed", "WARN")
    except Exception as e:
        service.session.log(f"[历史] 孤儿批次收尾失败（不影响运行）: "
                            f"{type(e).__name__}: {e}", "WARN")


if __name__ == "__main__":
    sys.exit(main())
