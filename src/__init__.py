"""问卷自动填写工具 — 核心包。

该包按职责拆分为以下模块：
    - config       : 全局配置（权重、常量）
    - verification : 智能验证（验证码）检测与等待
    - detection    : 问卷页面题目结构自动探测
    - answering    : 权重合并 → 答案随机生成策略
    - interaction  : DOM/JS 级别选项点击、提交按钮查找
    - pipeline     : 单次问卷填写 + 提交流程编排
    - cli          : 命令行入口（批量提交循环）
    - browser      : Edge 驱动工厂（反检测配置）
"""

from .config import WEIGHT_CONFIG

__all__ = ["WEIGHT_CONFIG"]
__version__ = "1.0.0"
