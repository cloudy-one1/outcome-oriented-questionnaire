"""问卷自动填写工具 — 核心包。

v2.0 模块变更日志：
    * 2026-07-04 新增：
        - history        ：SQLite 历史记录与复盘（运行元数据 + 每题答案明细）
        - answering_v2   ：新题型支持（填空/量表/下拉/矩阵 + 内置中文数据池）
        - config_io      ：权重配置 JSON 导入/导出 + 热更新 + 结构校验
    * 题型兼容：原 answering / detection / interaction / pipeline 保持不变，
                GUI 可按需调用 answering_v2 获取新题型答案。
"""

from .config import WEIGHT_CONFIG

__all__ = ["WEIGHT_CONFIG"]
__version__ = "2.0.0"
