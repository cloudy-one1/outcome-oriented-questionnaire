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
    collect_blocked_alerts,
    describe_blocked_alerts,
    install_alert_recorder,
)
# 验证码检查阶段
from .verification_stage import _check_verification_with_lock
# 补漏轮（v3.1 --rescue-gaps）：完整度自检拦下后接人工补答
from .gap_rescue import GAP_HOLD_TIMEOUT, hold_for_manual_fill, scroll_question_into_view
# 题目等待 + 答题分发阶段
from .question_stage import _answer_one_question, _wait_for_questions
# 分页问卷翻页阶段（v3.0）
from .page_nav import advance_to_next_page, page_counts


__all__ = [
    # page_loader
    "QUESTION_CONTROL_SELECTOR",
    "_ensure_questions_context",
    "_robust_driver_get",
    "_wait_for_ready_state",
    "collect_blocked_alerts",
    "describe_blocked_alerts",
    "install_alert_recorder",
    # verification
    "_check_verification_with_lock",
    # 补漏轮
    "GAP_HOLD_TIMEOUT",
    "hold_for_manual_fill",
    "scroll_question_into_view",
    # question
    "_wait_for_questions",
    "_answer_one_question",
    # paging
    "advance_to_next_page",
    "page_counts",
]
