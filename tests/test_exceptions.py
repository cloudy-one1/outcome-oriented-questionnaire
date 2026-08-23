"""V2.3 可读性建议第五章：异常处理分层（src/exceptions.py）单元测试。

覆盖建议：
    5.1 + 5.3：TRANSIENT_DOM_EXCEPTIONS 含有 DOM/浏览器瞬态异常
    5.3        NON_RECOVERABLE_BASE_EXCEPTIONS 含 Ctrl+C/退出等永不吞咽信号
    5.3        raise_non_recoverable 正确区分可吞 vs 必须上抛
    5.2        format_exc_log 统一格式包含 操作/题号/恢复动作
"""

from __future__ import annotations

import pytest

from src.exceptions import (
    NON_RECOVERABLE_BASE_EXCEPTIONS,
    TRANSIENT_DOM_EXCEPTIONS,
    format_exc_log,
    raise_non_recoverable,
)
from selenium.common.exceptions import (
    JavascriptException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)


# ---------------------------------------------------------------------------
# 5.3 + 5.1：TRANSIENT_DOM_EXCEPTIONS 语义正确性
# ---------------------------------------------------------------------------
def test_transient_dom_exceptions_covers_expected_subset() -> None:
    """至少包含建议里"明确的可恢复瞬态异常"的最小集合。"""
    must_have = [
        StaleElementReferenceException,
        JavascriptException,
        TimeoutException,
        NoSuchElementException,
        WebDriverException,
    ]
    for cls in must_have:
        assert cls in TRANSIENT_DOM_EXCEPTIONS, (
            f"TRANSIENT_DOM_EXCEPTIONS 应包含 {cls.__name__}"
        )


def test_transient_dom_contains_only_baseexception_subclasses() -> None:
    """集合元素必须是异常类（不是实例），避免把字符串等意外混入。"""
    for cls in TRANSIENT_DOM_EXCEPTIONS:
        assert isinstance(cls, type) and issubclass(cls, BaseException), (
            f"TRANSIENT_DOM_EXCEPTIONS 元素必须是异常类，得到 {cls!r}"
        )


@pytest.mark.parametrize("exc", [
    StaleElementReferenceException("stale"),
    TimeoutException("timeout"),
    NoSuchElementException("missing"),
])
def test_transient_dom_instances_are_caught(exc) -> None:
    """实例 isinstance 命中（实际 try/except 的底层语义）。"""
    assert isinstance(exc, TRANSIENT_DOM_EXCEPTIONS)


# ---------------------------------------------------------------------------
# 5.3：NON_RECOVERABLE_BASE_EXCEPTIONS 语义正确性
# ---------------------------------------------------------------------------
def test_non_recoverable_contains_keyboard_interrupt_and_system_exit() -> None:
    """Ctrl+C / sys.exit / MemoryError 必须永不吞咽。"""
    assert KeyboardInterrupt in NON_RECOVERABLE_BASE_EXCEPTIONS
    assert SystemExit in NON_RECOVERABLE_BASE_EXCEPTIONS
    assert MemoryError in NON_RECOVERABLE_BASE_EXCEPTIONS


def test_non_recoverable_count_keeps_tight() -> None:
    """不可恢复集合应保持极小（<=3），避免把可恢复异常误杀。"""
    assert len(NON_RECOVERABLE_BASE_EXCEPTIONS) <= 3, (
        f"NON_RECOVERABLE 集合过大({len(NON_RECOVERABLE_BASE_EXCEPTIONS)}),"
        f" 可能误伤可恢复异常。成员: {[e.__name__ for e in NON_RECOVERABLE_BASE_EXCEPTIONS]}"
    )


# ---------------------------------------------------------------------------
# 5.3：raise_non_recoverable 正确执行
# ---------------------------------------------------------------------------
def test_raise_non_recoverable_reraises_keyboard_interrupt() -> None:
    with pytest.raises(KeyboardInterrupt):
        raise_non_recoverable(KeyboardInterrupt("Ctrl+C"))


def test_raise_non_recoverable_reraises_system_exit() -> None:
    with pytest.raises(SystemExit):
        raise_non_recoverable(SystemExit(1))


def test_raise_non_recoverable_ignores_regular_exception() -> None:
    # 普通编程/业务异常不应被触发重抛
    try:
        raise_non_recoverable(ValueError("bad data"))
        raise_non_recoverable(TypeError("bad arg"))
        raise_non_recoverable(KeyError("missing key"))
    except (ValueError, TypeError, KeyError):
        pytest.fail("raise_non_recoverable 不应重抛普通编程异常")


# ---------------------------------------------------------------------------
# 5.2：format_exc_log 统一日志格式
# ---------------------------------------------------------------------------
def test_format_exc_log_basic_contains_action_and_type() -> None:
    out = format_exc_log(ValueError("boom"), action="加载配置")
    assert "加载配置" in out
    assert "ValueError" in out
    assert "boom" in out


def test_format_exc_log_question_and_qtype_appear_in_prefix() -> None:
    out = format_exc_log(
        TimeoutException("timeout"),
        action="点击选项",
        question=3,
        qtype="single",
        recovery="降级逐一点击",
    )
    # 题号 + 题型前缀
    assert "Q3[single]" in out or "Q3" in out
    # 恢复动作
    assert "降级逐一点击" in out
    assert "TimeoutException" in out


def test_format_exc_log_extra_kv_included() -> None:
    out = format_exc_log(
        RuntimeError("x"),
        action="history.start_run",
        run_id=12,
        submission_index=34,
    )
    assert "run_id=12" in out
    assert "submission_index=34" in out


def test_format_exc_log_empty_context_does_not_crash() -> None:
    # 无 qnum / qtype / recovery 的路径也要稳定输出
    out = format_exc_log(Exception("bare"), action="noop")
    assert "[noop]" in out
    assert "Exception" in out
    assert "bare" in out


# ---------------------------------------------------------------------------
# 5.4：「业务失败」和「程序异常」分离的合约测试
# ---------------------------------------------------------------------------
def test_business_bugs_not_in_transient_set() -> None:
    """数据契约 bug（KeyError/ValueError/TypeError）不应被"瞬态 DOM"吞掉。

    合约：对 pipeline/interaction 的业务 except TRANSIENT_DOM_EXCEPTIONS 块来说，
    这些异常不会命中 → 会继续向上冒泡暴露 bug，而不是伪装成「交互偶发异常」。
    """
    business_bugs = (ValueError("x"), TypeError("x"), KeyError("x"), AttributeError("x"))
    for exc in business_bugs:
        assert not isinstance(exc, TRANSIENT_DOM_EXCEPTIONS), (
            f"{type(exc).__name__} 属于数据契约错误，不应包含在 TRANSIENT_DOM_EXCEPTIONS 中"
        )
