"""pytest 全局配置：确保项目根目录在 sys.path（从任意目录启动均可 import src/gui）。"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
