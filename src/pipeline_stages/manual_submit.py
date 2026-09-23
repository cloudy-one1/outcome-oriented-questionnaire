"""人工提交（v3.3 ``--manual-submit``）：停在提交按钮前，把"交上去"这一下交给人。

为什么要有这一步：半份问卷一旦交上去就是平台上的一条真实回收记录，**收不回来**。
此前的"先验证再放开"只能跑一份然后读日志 —— 而读到日志时那份已经交了。本模块把
最后那一下点击交回给人：工具照样探测、照样逐题作答、照样跑完三道提交前判据，
只是不自己点提交；人可以在窗口里改任何一格，改完自己点。

与补漏轮（``gap_rescue``）的分工要说清，两者都"等人"但等的东西不同：那边等的是
**我们答不了的题**（人补完，工具点提交）；这里等的是**那一下点击本身**（人看完整份，
人自己点）。两个开关同时打开时是先补答、后交人点提交，顺序天然正确 —— 补漏轮在
Step 7.5，本步在 Step 8 的位置上。

三条不变量：

  1. **成功与否仍走同一套判定**。人点下去之后，判据是 `_wait_until_submit_effect`
     用的那同一个成功信号脚本（URL 变化或强文案），不另立标准 —— 否则"人提交的"
     那份会绕过三态判定，历史与统计就跟自动提交的那份不可比了。
  2. **没人点就不是"未知"，是"没交"**。超时返回 ``SUBMIT_FAILED`` 而不是 UNKNOWN：
     工具根本没点提交，平台上不会留下任何记录，把它算成"交了但没确认"是谎报。
  3. **停止优先**。等待期间 ``stop_check`` 一置位就抛 ``SubmissionAborted``（不提交、
     不计失败），人工介入锁在 ``finally`` 里必还 —— 与补漏轮同一条红线。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from ..exceptions import (
    TRANSIENT_DOM_EXCEPTIONS,
    SubmissionAborted,
    format_exc_log,
    raise_non_recoverable,
)
from ..interactions._scripts import submit_success_detect_script
from ..interactions.submit import SELECTORS as SUBMIT_SELECTORS
from ..models import SUBMIT_FAILED, SUBMIT_SUCCESS, SubmitOutcome
from ..utils import ManualHoldLock

# 等人的上限。比补漏轮的 300s 短一档：那边要"在一张读不懂的表单里找到题并答完"，
# 这里只是"看一眼、必要时改两格、点一下"。
MANUAL_SUBMIT_TIMEOUT: float = 180.0

_POLL: float = 1.0          # 轮询片；一次 execute_script + 一次 current_url
_PROGRESS_EVERY: float = 30.0   # 进度行节奏：每秒一条会把运行日志埋掉

# 只滚动不点击。选择器沿用 ``interactions.submit.SELECTORS``（同一份单一真相），
# 认不到按钮不算错：人自己能在页面上看到提交键，我们只是把它送到眼前。
_SCROLL_SUBMIT_JS = """
var sels = arguments[0];
for (var i = 0; i < sels.length; i++) {
    var btn = null;
    try { btn = document.querySelector(sels[i]); } catch (_) {}
    if (!btn) continue;
    try { btn.scrollIntoView({behavior: 'instant', block: 'center'}); } catch (_) {}
    return true;
}
return false;
"""


def _submitted(driver: Any, base_url: str | None, success_js: str) -> bool:
    """人点下去之后页面有没有出现提交效果（与 ``_wait_until_submit_effect`` 同判据）。"""
    try:
        cur = driver.current_url
    except TRANSIENT_DOM_EXCEPTIONS:
        # 点击瞬间页面正在跳转，这一轮读不到；下一轮再问
        return False
    if base_url is not None and cur != base_url:
        return True
    try:
        return bool(driver.execute_script(success_js))
    except TRANSIENT_DOM_EXCEPTIONS:
        return False
    except Exception as _e:
        raise_non_recoverable(_e)
        print("  " + format_exc_log(
            _e, action="人工提交：读提交效果", recovery="下一轮再问（不因此判失败）",
        ))
        return False


def wait_for_manual_submit(
    driver: Any,
    lock: ManualHoldLock,
    *,
    stop_check: Callable[[], bool] | None = None,
    timeout: float = MANUAL_SUBMIT_TIMEOUT,
) -> SubmitOutcome:
    """停在提交按钮前等人工点；返回**与自动提交同一种**三态结果。

    :return: ``SUBMIT_SUCCESS`` 观察到提交效果；``SUBMIT_FAILED`` 等到超时也没人点
             （即"这一份没有交出去"）
    :raises SubmissionAborted: ``stop_check`` 置位 —— 不提交也不计失败
    """
    try:
        base_url: str | None = driver.current_url
    except TRANSIENT_DOM_EXCEPTIONS:
        # 基线拿不到就只靠强文案判定：拿 "" 兜底会让"任何 URL"都算变化，把错误页
        # 判成提交成功 —— 与 _wait_until_submit_effect 同一取舍。
        base_url = None
    success_js = submit_success_detect_script()
    print("  [人工提交] 已停在提交按钮前 —— 请在窗口里核对（要改的直接改），"
          f"由**你**点提交；最长等 {timeout:.0f}s，停止/Ctrl+C 可取消这一份")
    try:
        driver.execute_script(_SCROLL_SUBMIT_JS, list(SUBMIT_SELECTORS))
    except TRANSIENT_DOM_EXCEPTIONS:
        pass    # 滚不动只是少一步贴心，等待照旧

    lock.acquire()
    waited = 0.0
    next_progress = _PROGRESS_EVERY
    try:
        while True:
            if stop_check is not None and stop_check():
                print("    收到停止请求，这一份不提交（也没交上去）")
                raise SubmissionAborted("人工提交等待期间收到停止请求")

            if _submitted(driver, base_url, success_js):
                print(f"    [人工提交] 已观察到提交效果（等了 {waited:.0f}s）")
                return SUBMIT_SUCCESS
            if waited >= timeout:
                print(f"    [人工提交] 等满 {timeout:.0f}s 没有点击 → "
                      "计本轮失败（这一份**没有交出去**，平台上不留记录）")
                return SUBMIT_FAILED
            if waited >= next_progress:
                print(f"    [人工提交] 仍在等你点提交（{waited:.0f}s / {timeout:.0f}s）")
                next_progress += _PROGRESS_EVERY
            time.sleep(_POLL)
            waited += _POLL
    finally:
        lock.release()
