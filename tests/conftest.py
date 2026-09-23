"""E2E 的环境抖动降级（v3.1 A 档第 5 条）。

借的是 SurveyController 的回归白名单思路（
`CI/live_tests/test_live_runtime_regression.py:16-22`：命中"外部页面抖动"正则的
失败判 skip 而不是 fail）。**没借**它那份里的"已知不支持题型"白名单 —— 那条是为
真卷准备的，我们的 E2E 跑的是 `tests/fixtures/` 里的本地 mock 页面，平台改版不会
影响它。

为什么值得做：ci.yml 的 e2e job 现在是 `continue-on-error: true`，因为驱动在
runner 上偶发起不来，不该红掉主流程 —— 代价是这个 job 从此没人看，而它是唯一能
验证"拼出来的 JS 在真 DOM 里成立"的环节（selector 被误拼进 JS 字符串字面量这类
"语法合法、语义非法"的错误，node --check 和所有离线替身都看不见）。
把可归因于浏览器的失败转成 skip 之后，这个 job 可以变成真门禁：剩下的红只剩
"代码回归"一种解释。

配套的 `scripts/e2e_gate.py` 管住这套机制唯一的作弊面：全 skip 的 job 看起来
也是绿的。
"""

from __future__ import annotations

import re

import pytest

# 只有一类失败该被原谅：浏览器/驱动自己起不来或半路死了。
# 判定用异常类型名 + 文案，宁可写窄 —— 误把真回归降成 skip，比多一条红糟得多。
ENV_FLAKY_PATTERNS = (
    r"cannot connect to (Chrome|Edge|the browser)",
    r"chrome not reachable",
    r"browser has already closed",
    r"session not created",
    r"element (not )?interactable.*(target already exited|no such window)",
    r"(unable to obtain|error trying to read status).*driver",
    r"chrome process (ended|died) before",
    r"unable to (connect|obtain).*(devtools|renderer)",
    r"winerror (10060|10061|10054)",  # 拉驱动时的网络抖动（Selenium Manager）
)

_ENV_FLAKY = re.compile("|".join(ENV_FLAKY_PATTERNS), re.IGNORECASE)

_flaky_downgrades: list[str] = []


def _text_of(exc_info: BaseException | None) -> str:
    if exc_info is None:
        return ""
    return f"{type(exc_info).__name__}: {exc_info}"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """integration 用例失败在浏览器自己身上时，改判 skip 并说明理由。

    改的是 report.outcome，而 `session.testsfailed` 要到
    ``pytest_runtest_logreport`` 才累加 —— 在这一步翻结局，退出码才会跟着变绿。
    """
    report = yield
    if report.when != "call" or not report.failed:
        return report
    if item.get_closest_marker("integration") is None:
        return report
    text = _text_of(call.excinfo.value if call.excinfo else None)
    if not _ENV_FLAKY.search(text):
        return report
    report.outcome = "skipped"
    # longrepr 必须是 (文件, 行号, 理由) 三元组 —— skipped 报告走的是
    # junit-xml / `-rs` 那条格式化路径，给它字符串会当场 INTERNALERROR。
    path, lineno, _domain = item.location
    reason = f"浏览器/驱动自身故障，降级为 skip：{text[:300]}"
    report.longrepr = (str(path), int(lineno), reason)
    _flaky_downgrades.append(f"{item.name}: {text[:120]}")
    return report


def pytest_sessionfinish(session, exitstatus):
    """把降级条数打出来：悄悄吞掉 20 条回归是这套机制唯一的作弊面。"""
    if not _flaky_downgrades:
        return
    print(
        f"\n[e2e-gate] {len(_flaky_downgrades)} 条 integration 失败因浏览器/驱动"
        f"自身故障降级为 skip：\n  " + "\n  ".join(_flaky_downgrades[:10])
    )
