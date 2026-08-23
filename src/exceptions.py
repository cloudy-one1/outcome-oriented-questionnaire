"""集中定义可重试/不可重试异常分类 + 统一异常日志格式化（V2.3 可读性建议第五章整改）。

设计动机（对照《代码可读性改进建议.md》第五章「异常处理」）:
    5.1 避免大范围 `except Exception`。业务代码处只捕获"确实能处理"的异常类型。
    5.2 不静默忽略异常。至少记录操作/对象/题号/恢复动作，方便定位。
    5.3 统一定义可重试异常和不可重试异常，避免不同模块分别决定重试行为。
        （原先 interaction.js_execute_retry 有局部 _JS_RETRYABLE，GUI/cli.py 有
        各自 except Exception — 统一收拢到本模块）。
    5.4 将「业务失败」和「程序异常」分开表达：
        - 业务失败 → SubmitOutcome / RunState 等结果对象返回即可
        - 程序异常（KeyError/TypeError/ValueError 数据契约错误）→ 向上抛，保留堆栈
        - KeyboardInterrupt/SystemExit/MemoryError → 永不吞，必须继续上抛

关键导出:
    - ``TRANSIENT_DOM_EXCEPTIONS``: 浏览器/DOM 瞬态异常（可期望、可恢复的交互失败）
    - ``NON_RECOVERABLE_BASE_EXCEPTIONS``: 永不吞咽的基础异常（Interrupt/Exit/内存）
    - ``format_exc_log``: 统一异常消息格式化（建议 5.2）
    - ``raise_non_recoverable``: 若异常在 NON_RECOVERABLE 则重新抛出（给下游"兜底 except"用）
"""

from __future__ import annotations

from typing import Any

# 统一依赖 selenium 异常（集中一处，避免其他模块各自导入 selenium.exceptions）
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    ElementNotSelectableException,
    ElementNotVisibleException,
    InvalidElementStateException,
    InvalidSelectorException,
    InvalidSessionIdException,
    InvalidSwitchToTargetException,
    JavascriptException,
    MoveTargetOutOfBoundsException,
    NoAlertPresentException,
    NoSuchAttributeException,
    NoSuchElementException,
    NoSuchFrameException,
    NoSuchShadowRootException,
    NoSuchWindowException,
    ScreenshotException,
    SessionNotCreatedException,
    StaleElementReferenceException,
    TimeoutException,
    UnableToSetCookieException,
    UnexpectedAlertPresentException,
    UnexpectedTagNameException,
    WebDriverException,
)


# ============================================================================
#  5.3 统一：瞬态 DOM/浏览器异常（可恢复、可重试）
# ============================================================================
#
# 这些异常在问卷星动态 DOM 场景下是"偶发正常现象"：
#   - StaleElementReference: 元素被问卷星重新渲染
#   - Timeout: 页面加载慢 / 验证码等待
#   - ElementClickIntercepted: 悬浮层短暂盖住按钮
#   - NoSuchElement: 题目 DOM 还没渲染出来
#
# 捕获这些异常 + 降级或重试是合理的，不应向上抛出干扰主流程。
# 其他类型的异常（KeyError/TypeError/ValueError 等代码 bug）不在本集合中，
# 要么被上层单独处理，要么上抛暴露问题。
TRANSIENT_DOM_EXCEPTIONS: tuple[type[BaseException], ...] = (
    StaleElementReferenceException,
    JavascriptException,
    TimeoutException,
    ElementClickInterceptedException,
    ElementNotInteractableException,
    ElementNotSelectableException,
    ElementNotVisibleException,
    InvalidElementStateException,
    InvalidSelectorException,
    InvalidSessionIdException,
    InvalidSwitchToTargetException,
    MoveTargetOutOfBoundsException,
    NoAlertPresentException,
    NoSuchAttributeException,
    NoSuchElementException,
    NoSuchFrameException,
    NoSuchShadowRootException,
    NoSuchWindowException,
    ScreenshotException,
    SessionNotCreatedException,
    UnableToSetCookieException,
    UnexpectedAlertPresentException,
    UnexpectedTagNameException,
    # 最后兜底：其他 WebDriver 派生异常（远程协议层的各种偶发错误）
    # 注意：只捕获派生类而不是具体子类型时，会覆盖前面所有子类型；放在
    # tuple 末尾不影响 isinstance() 判断（isinstance 只要命中 tuple 内任一成员）
    WebDriverException,
)

# ============================================================================
#  5.3 统一：永不吞咽的基础异常
# ============================================================================
#
# 任何"兜底 except Exception"都必须先检查是否命中本集合；命中则重新抛出。
# 原因：这些异常属于进程/线程/运行时级别的终止信号，如果被业务代码的
# except Exception 意外吞掉，会导致 Ctrl+C 失效、进程无法干净退出、
# 内存错误继续执行而崩溃在更诡异的位置。
NON_RECOVERABLE_BASE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,   # Ctrl+C / SIGINT
    SystemExit,          # sys.exit()
    MemoryError,         # 内存耗尽（继续执行只会产出不可预期的结果）
    # GeneratorExit 不列入：生成器内部特殊，不属于业务场景
)


def raise_non_recoverable(exc: BaseException) -> None:
    """兜底 except Exception 必须调用的安全检查: 如果 exc 属于 NON_RECOVERABLE 则立刻重新抛出。

    典型用法::

        try:
            ...
        except Exception as _e:
            raise_non_recoverable(_e)
            # 到这里说明 _e 不是不可恢复的终止信号，可以按业务降级处理
            ...
    """
    if isinstance(exc, NON_RECOVERABLE_BASE_EXCEPTIONS):
        raise exc


def format_exc_log(
    exc: BaseException,
    *,
    action: str,
    question: int | None = None,
    qtype: str | None = None,
    recovery: str | None = None,
    **extra: Any,
) -> str:
    """统一异常日志格式化（建议 5.2：至少记录操作/对象/题号/恢复动作）。

    避免模块间各自用不同格式写 ``print(f"xxx: {type(_e).__name__}")``
    造成日志风格不一、缺关键上下文。输出格式稳定为一行，便于 grep。

    :param exc:      异常对象本身（会取 ``type.__name__`` + ``str(exc)``）
    :param action:   当前在做什么动作（例："批量点击选项"、"提交确认"、"写 history record_answer"）
    :param question: 题号（q number），无则省略
    :param qtype:    题型（single/multi/...），无则省略
    :param recovery: 采取的恢复动作（例："降级逐一点击"、"计失败继续"、"DELETE+INSERT 兜底"）
    :param extra:    其他任意上下文（run_id=..., submission_index=... 等）
    :return: 一行可读的日志字符串
    """
    parts: list[str] = []
    if question is not None:
        parts.append(f"Q{question}")
        if qtype:
            parts[-1] = f"Q{question}[{qtype}]"
    parts.append(f"[{action}]")
    parts.append(f"{type(exc).__name__}: {exc}")
    if recovery:
        parts.append(f"→ {recovery}")
    for k, v in extra.items():
        parts.append(f"{k}={v}")
    return " ".join(parts)
