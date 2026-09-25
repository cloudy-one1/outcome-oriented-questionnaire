"""Web 控制台启动脚本 — 在项目根目录直接运行：python run_web.py

装过本仓库（``pip install -e .``）之后等价于 ``wjx-web``。
"""

import sys

from webui.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
