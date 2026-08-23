"""验证码检查阶段（第一章第 3 条拆分）。"""

from __future__ import annotations

from typing import Any

from ..config import VERIFICATION_TIMEOUT
from ..utils import ManualHoldLock
from ..verification import is_smart_verification_showing, wait_for_manual_verification


def _check_verification_with_lock(driver: Any, lock: ManualHoldLock) -> bool:
    """检查是否弹出了验证，如果是 → 进入 hold 等待人工处理。

    返回 True ：验证已解决（或根本没弹）
    返回 False：验证超时
    """
    if not is_smart_verification_showing(driver):
        return True
    return wait_for_manual_verification(
        driver,
        timeout_seconds=VERIFICATION_TIMEOUT,
        hold_lock=lock,
    )


__all__ = ["_check_verification_with_lock"]
