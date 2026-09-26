"""pytest 全局配置：确保项目根目录在 sys.path（从任意目录启动均可 import src/webui）。

会话级 Tk 根窗口夹具 ``tk_root`` 随桌面版宿主一起在 v4.0 退役。它存在的理由是
"Windows 上一个进程里只能可靠地建一个 ``tk.Tk()``"，第二个根会抛
"Can't find a usable tk.tcl"，而各 GUI 模块自建自销会互相打翻成整片静默 skip ——
那件事只在有 Tk 宿主时才会发生，现在没有消费者了。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
