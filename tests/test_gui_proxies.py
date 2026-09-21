"""v2.8 类型门禁整改的副产物：`SurveyGUI` 上几个"薄代理"的契约测试。

`gui/app.py` 里这几个方法都是"面板可能还没建好"的懒构造代理，此前**零测试**
（`gui/app.py` 离线覆盖率 22%，代理方法正好落在缺口里）。v2.8 把它们从
`ensure() and panel.method()` 改写成显式 `if panel is not None`（为了让
`gui/` 进 pyright 门禁），改动虽小仍需钉住语义：

  - 面板拿得到 → 原样转发；
  - 面板拿不到（root 还没就绪）→ 静默跳过，不抛 AttributeError。

全部用 `types.MethodType` 把**未绑定**的方法挂到桩对象上，不建 Tk 窗口 ——
无显示 runner 上也能跑。
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui.app import SurveyGUI  # noqa: E402


class _Spy:
    """记录被调用的方法名与实参。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str):
        def _record(*args: Any) -> Any:
            self.calls.append((name, args))
            return None
        return _record


def _bind(func, host: object) -> Any:
    return types.MethodType(func, host)


class _PanelHost:
    """桩：`_history_ensure_panel()` 返回 `_panel`（可能是 None）。"""

    def __init__(self, panel: Any) -> None:
        self._panel = panel

    def _history_ensure_panel(self):
        return self._panel


@pytest.mark.parametrize("name,attr", [
    ("_history_refresh", "refresh"),
    ("_history_export_csv", "export_csv"),
    ("_history_purge_old", "purge_old"),
])
def test_history_proxy_forwards_to_panel(name: str, attr: str) -> None:
    panel = _Spy()
    host = _PanelHost(panel)
    _bind(getattr(SurveyGUI, name), host)()
    assert panel.calls == [(attr, ())], f"{name} 应恰好转发一次 {attr}()"


def test_history_proxy_survives_missing_panel() -> None:
    """root 未就绪时 ensure 返回 None：必须静默跳过而不是 AttributeError。"""
    host = _PanelHost(None)
    for name in ("_history_refresh", "_history_export_csv", "_history_purge_old"):
        _bind(getattr(SurveyGUI, name), host)()  # 不抛异常即为通过


def test_history_select_run_forwards_event_arg() -> None:
    panel = _Spy()
    host = _PanelHost(panel)
    _bind(SurveyGUI._history_select_run, host)("<<TreeviewSelect>>")
    assert panel.calls == [("select_run", ("<<TreeviewSelect>>",))]


class _LogHost:
    def __init__(self) -> None:
        self.view = _Spy()
        self.view._lineno_count = 7

    def _ensure_log_view(self):
        return self.view


def test_append_log_uses_ensured_view() -> None:
    """`_append_log` 改为直接用 ensure 的返回值（Optional 属性收窄需要）。"""
    host = _LogHost()
    _bind(SurveyGUI._append_log, host)("进度 3/10", "INFO")
    assert host.view.calls == [("_append", ("进度 3/10", "INFO"))]
    assert host.log_lineno_count == 7, "行号计数应同步回 app 的兼容字段"


class _WeightHost:
    def __init__(self, panel: Any) -> None:
        self._panel = panel

    def _ensure_weight_panel(self):
        return self._panel


def test_restore_weight_table_syncs_table_frame() -> None:
    """续传恢复权重后 `table_frame` 必须重新指向面板的 frame。"""
    panel = _Spy()
    panel.table_frame = "FRAME-REF"
    host = _WeightHost(panel)
    _bind(SurveyGUI._restore_weight_table_from_config, host)({1: {"type": "single"}})
    assert panel.calls == [("restore_from_config", ({1: {"type": "single"}},))]
    assert host.table_frame == "FRAME-REF"
