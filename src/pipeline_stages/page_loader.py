"""页面加载/iframe 切换/ready-state 探测 + alert 接管（第一章第 3 条拆分）。

导出：
    - _robust_driver_get     带重试的 driver.get
    - _wait_for_ready_state  等待 readyState === complete（或超时继续）
    - _ensure_questions_context  iframe 切换到含题目的 frame
    - install_alert_recorder     把 window.alert 换成记录器（防阻塞 + 捞原因）
    - collect_blocked_alerts     读回被拦下的弹窗文案
    - describe_blocked_alerts    文案 → 可打印的诊断行（去重 / 截断 / 限量）
"""

from __future__ import annotations

from typing import Any, Sequence

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.support.ui import WebDriverWait

from ..platforms import WJX
from ..config import PAGE_LOAD_INITIAL_DELAY, PAGE_LOAD_MAX_ATTEMPTS
from ..exceptions import TRANSIENT_DOM_EXCEPTIONS, raise_non_recoverable
from ..interactions._scripts import (
    install_alert_recorder_script,
    read_blocked_alerts_script,
)
from ..utils import retry_with_backoff


# ============================================================================
#  题目控件 selector（多个函数复用，集中一处可全局调整）
# ============================================================================
# v3.0：选择器本体已收进平台层（src/platforms.py 的 SurveyPlatform），这里保留
# 这个名字是因为 question_stage 等调用点按它 import；换平台改 WJX 那份常量。
QUESTION_CONTROL_SELECTOR: str = WJX.question_control_selector


# ============================================================================
#  iframe 上下文切换（问卷星"题目嵌在 iframe"场景）
# ============================================================================
def _ensure_questions_context(driver: Any) -> bool:
    """确保当前 WebDriver 上下文指向包含题目的 frame。"""
    selector = QUESTION_CONTROL_SELECTOR
    has = driver.execute_script(
        f"return document.querySelectorAll('{selector}').length"
    )
    if has:
        return True

    iframes = driver.execute_script("return document.querySelectorAll('iframe').length")
    for i in range(iframes):
        driver.switch_to.frame(i)
        if driver.execute_script(
            f"return document.querySelectorAll('{selector}').length"
        ):
            return True
        driver.switch_to.default_content()

    driver.switch_to.default_content()
    return False


# ============================================================================
#  等待 readyState === complete（超时继续）
# ============================================================================
def _wait_for_ready_state(driver: Any, page_timeout: float = 25.0) -> None:
    """等待 document.readyState == 'complete'（失败不抛异常，继续流程）。"""
    try:
        WebDriverWait(driver, page_timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TRANSIENT_DOM_EXCEPTIONS:
        # 页面 ready 信号属于"最努力探测"，超时/瞬态 DOM 失败
        # 不影响后续答题（问卷星很多站点 readyState 永远是 interactive）
        pass
    except Exception as _e:
        # Ctrl+C/SystemExit 必须上抛；其他仍当作探测失败忽略
        raise_non_recoverable(_e)
        pass


# ============================================================================
#  页面打开 + 指数退避重试包装
# ============================================================================
@retry_with_backoff(
    max_attempts=PAGE_LOAD_MAX_ATTEMPTS,
    initial_delay=PAGE_LOAD_INITIAL_DELAY,
    backoff_factor=2.0,
    jitter=True,
    retry_on=(WebDriverException, TimeoutError),
)
def _robust_driver_get(driver: Any, survey_url: str) -> None:
    """稳定版 driver.get()：遇到浏览器/网络异常自动重试。"""
    driver.get(survey_url)


# ============================================================================
#  alert 接管：原生弹窗既阻塞 WebDriver，又藏着提交失败的原因
# ============================================================================
_ALERT_LINE_MAX_CHARS = 160
_ALERT_LINES_SHOWN = 3


def install_alert_recorder(driver: Any) -> bool:
    """把 ``window.alert`` 换成记录器。必须在切到题目 frame **之后**调用。

    弹窗是页面 JS 在自己的 realm 里弹的，钩子挂在 default content 上等于没挂。
    """
    return bool(driver.execute_script(install_alert_recorder_script()))


def collect_blocked_alerts(driver: Any) -> list[str]:
    """读回被拦下的弹窗文案；拿不到就返回空列表。

    调用点在提交流程的末尾，那一刻页面可能正在跳转 —— 诊断路径绝不能把
    异常抛出去盖掉真正的失败原因，所以这里连瞬态 DOM 异常一起咽掉。
    """
    try:
        raw = driver.execute_script(read_blocked_alerts_script())
    except TRANSIENT_DOM_EXCEPTIONS:
        return []
    except Exception as _e:
        raise_non_recoverable(_e)
        return []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item) for item in raw]


def describe_blocked_alerts(alerts: Sequence[str]) -> list[str]:
    """弹窗文案 → 一行行可打印的诊断文本：去重、压掉换行、限长、限量。

    去重是必要的：页面常在轮询里反复弹同一句，原样打出来会有几十行同一条
    "第 3 题未填写"，把日志里其它有用的行挤掉。
    """
    lines: list[str] = []
    seen: set[str] = set()
    overflow = 0
    for raw in alerts:
        text = " ".join(str(raw).split())
        if not text or text in seen:
            continue
        seen.add(text)
        if len(lines) >= _ALERT_LINES_SHOWN:
            overflow += 1
            continue
        if len(text) > _ALERT_LINE_MAX_CHARS:
            text = text[:_ALERT_LINE_MAX_CHARS] + "…"
        lines.append(f"[页面弹窗] {text}")
    if overflow:
        lines.append(f"[页面弹窗] 另有 {overflow} 条不同的提示未列出")
    return lines


__all__ = [
    "QUESTION_CONTROL_SELECTOR",
    "_ensure_questions_context",
    "_robust_driver_get",
    "_wait_for_ready_state",
    "collect_blocked_alerts",
    "describe_blocked_alerts",
    "install_alert_recorder",
]
