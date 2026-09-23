"""补漏轮（v3.1）：完整度自检拦下之后，把人工接进来。

``completeness.unanswered_required`` 判出的缺口按定义是"平台标了必答、而我们整题都没
探测到"的那些题 —— 那种题本工具答不了，**但人能**。这一步就是给那几分钟：把第一道
缺口题滚进视野，挂上人工介入锁等人在可见的浏览器窗口里补上，然后重跑一次自检。

四条边界，都是"不放宽任何既有判定"这条线上的：

  1. **默认不进这里**：只有 ``--rescue-gaps`` 打开才会走到这一步，不开就是原行为
     （判失败、不点提交），一行日志都不多打。
  2. **等不到就维持原判**：复检后缺口仍在、等待超时、无头模式 —— 一律把缺口原样
     交回调用方，那里照旧 ``SUBMIT_FAILED``。这一步没有能力、也没有权限去"猜"缺口
     已经被补上。
  3. **停止优先于提交**：等待期间 ``stop_check`` 一置位就抛 ``SubmissionAborted``，
     半补的问卷不会被交出去（与验证码人工等待同一条线，锁也一定被释放）。
  4. **不代答、不代点**：人在场只是把探测看不见的题**变成能被看见**，答的是人，
     点提交的还是本工具 —— 而且只在复检通过之后。

复检为什么不能只重跑 ``detect_questions``：缺口题之所以是缺口，正是因为我们的探测
看不见它，人工补完再探测一遍照样看不见 —— 那样这条路永远走不通，等着也只是白等。
所以复检额外把"页面上现在读得到值的题号"并进来当证据（``detect_answered_questions``
读的是提交用的存储字段 ``input[name=qN]``，不是我们的控件假设）。两种证据都只会让
缺口**变小**，判据本身（``unanswered_required``）一个字没改。
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
from ..platforms import WJX_QUESTION_ANCHOR_SELECTORS
from ..utils import ManualHoldLock

# 人工补答的等待上限。比验证码等待（VERIFICATION_TIMEOUT=120s）长：那边是"拉一下
# 滑块"，这边是"在一张我们读不懂的表单里找到那道题并答完"，几分钟是正常量级。
# 到了上限就维持原判失败 —— 宁可这一份白跑，也不要"没人看过一眼"地提交。
GAP_HOLD_TIMEOUT: float = 300.0

_HOLD_POLL: float = 2.0     # 复检间隔；与验证码等待同量级，一轮 3 次 execute_script
_PROGRESS_EVERY: float = 20.0   # 进度行节奏：每片都打就把运行信息埋了

# 题号 → 容器：候选选择器由调用方当参数传进来（与 detection 读平台结构同一写法），
# 免得题号被拼进 JS 源码里。命中即滚到视口中线，认不到容器返回 False（不是错误：
# 人工自己能在页面上找到那道题，我们只是顺手把它送到眼前）。
_SCROLL_JS = """
var sels = arguments[0];
for (var i = 0; i < sels.length; i++) {
    var box = null;
    try { box = document.querySelector(sels[i]); } catch (_) {}
    if (!box) continue;
    try { box.scrollIntoView({behavior: 'instant', block: 'center'}); } catch (_) {}
    return true;
}
return false;
"""


def _fmt_gap(gap: list[int]) -> str:
    return "、".join(f"Q{n}" for n in gap)


def scroll_question_into_view(driver: Any, qnum: int) -> None:
    """把第 ``qnum`` 题滚进视野。**任何失败都只当没滚**，不影响后面的等待。"""
    sels = [s.replace("{num}", str(int(qnum))) for s in WJX_QUESTION_ANCHOR_SELECTORS]
    try:
        driver.execute_script(_SCROLL_JS, sels)
    except TRANSIENT_DOM_EXCEPTIONS:
        # 人正在页面上操作，这条命令抖一下很正常：滚不动就算了，等待照旧
        return
    except Exception as _e:
        # 补漏是诊断路径，它自己出问题不该把一份本来能交的问卷变成一次失败
        raise_non_recoverable(_e)
        print("  " + format_exc_log(
            _e, action=f"补漏：把 Q{qnum} 滚进视野", recovery="跳过滚动，继续等人工",
        ))
        return


def hold_for_manual_fill(
    recheck: Callable[[], list[int]],
    *,
    lock: ManualHoldLock,
    stop_check: Callable[[], bool] | None = None,
    timeout: float = GAP_HOLD_TIMEOUT,
) -> list[int]:
    """等人工补齐缺口，返回**最后一次**复检的缺口集合（空 = 可以提交了）。

    :param recheck: 复检函数，返回仍然缺的题号；每 ``_HOLD_POLL`` 调一次
    :param lock: 人工介入锁。等待期间处于 holding —— ``utils.ManualHoldLock`` 的
        契约就是"这期间任何超时判断都不许判失败"，返回前必然释放。
    :param stop_check: 每片问一次；True → 抛 ``SubmissionAborted``（不提交）
    :param timeout: 等待上限，超时后原样返回最后一次读数
    """
    lock.acquire()
    waited = 0.0
    next_progress = _PROGRESS_EVERY
    try:
        while True:
            if stop_check is not None and stop_check():
                print("    收到停止请求，结束补漏等待（本轮不会提交）")
                raise SubmissionAborted("补漏等待期间收到停止请求")

            time.sleep(_HOLD_POLL)
            waited += _HOLD_POLL

            gap = recheck()
            if not gap:
                print(f"    [补漏] 人工已补齐（等了 {waited:.0f}s）→ 继续提交")
                return []
            if waited >= timeout:
                print(f"    [补漏] 等满 {timeout:.0f}s，{_fmt_gap(gap)} 仍然读不到答案"
                      " → 维持判失败，不点提交")
                return gap
            if waited >= next_progress:
                print(f"    [补漏] 仍在等人工补答 {_fmt_gap(gap)}"
                      f"（{waited:.0f}s / {timeout:.0f}s）")
                next_progress += _PROGRESS_EVERY
    finally:
        lock.release()


__all__ = [
    "GAP_HOLD_TIMEOUT",
    "hold_for_manual_fill",
    "scroll_question_into_view",
]
