"""pipeline_stages 公共 API（第一章第 3 条拆分后兼容 re-export）。

pipeline.py 统一从本包根 import 子阶段函数，或调用方也可直接按阶段 import。
"""

from __future__ import annotations

# 页面加载阶段
from .page_loader import (
    QUESTION_CONTROL_SELECTOR,
    _ensure_questions_context,
    _robust_driver_get,
    _wait_for_ready_state,
)
# 验证码检查阶段
from .verification_stage import _check_verification_with_lock
# 题目等待 + 答题分发阶段
from .question_stage import _answer_one_question, _wait_for_questions


__all__ = [
    # page_loader
    "QUESTION_CONTROL_SELECTOR",
    "_ensure_questions_context",
    "_robust_driver_get",
    "_wait_for_ready_state",
    # verification
    "_check_verification_with_lock",
    # question
    "_wait_for_questions",
    "_answer_one_question",
]
