"""pytest 全局配置：确保项目根目录在 sys.path（从任意目录启动均可 import src/gui），
并提供整个会话唯一的 Tk 根窗口夹具。
"""

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture(scope="session")
def tk_root():
    """全会话只建一次的 Tk 根窗口；建不出来（无显示的 runner）就 skip 用它的用例。

    WHY：Windows 上一个进程里只能可靠地建**一个** Tk 根窗口 —— 第二个 ``tk.Tk()``
    会抛 "Can't find a usable tk.tcl"，而把前一个根销毁之后再建也不稳（见
    ``tests/test_gui_run_loop.py`` 开头那段）。此前各 GUI 模块自己建自己销，
    互相打翻的症状是整片用例静默 skip —— 比红更难发现。
    根窗口只 withdraw、留到进程结束，需要"窗口"的模块一律挂 ``Toplevel``。
    """
    tkinter = pytest.importorskip("tkinter")
    try:
        root = tkinter.Tk()
    except Exception as exc:
        pytest.skip(f"无法创建 Tk 根窗口: {type(exc).__name__}: {exc}")
    root.withdraw()
    root.update_idletasks()
    yield root
    # 不 destroy：见 docstring。解释器随 pytest 进程一起回收。
