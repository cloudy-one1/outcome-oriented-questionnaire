"""``scripts/ci_annotate_e2e.py`` 的契约。

这条脚本存在的唯一理由是"CI 红了而本地按 CI 口径全绿"，那种时候日志要登录才看得到，
只有**注解**能匿名读。所以它的输出格式本身就是契约：格式错了等于没有取证。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.ci_annotate_e2e import main  # noqa: E402

PROBE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3" errors="1" failures="1" skipped="1">
<testcase classname="tests.test_webui_e2e" name="test_run" time="1.0">
  <failure message="AssertionError: 计数不对">def test_run():&#10;E       AssertionError: 计数不对&#10;E       assert 0 == 3</failure>
</testcase>
<testcase classname="tests.test_webui_e2e" name="test_stop" time="0.2">
  <error message='failed on setup with "selenium.common.exceptions.SessionNotCreatedException: Message: session not created"'>Traceback&#10;E       selenium.common.exceptions.SessionNotCreatedException: Message: session not created</error>
</testcase>
<testcase classname="tests.test_webui_e2e" name="test_skipped" time="0.1">
  <skipped message="浏览器/驱动自身故障，降级为 skip"/>
</testcase>
</testsuite></testsuites>
"""


def _junit(tmp_path, text: str = PROBE) -> str:
    p = tmp_path / "e2e-junit.xml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_a_fixture_error_and_an_assertion_are_told_apart(tmp_path, capsys) -> None:
    """两种红的指向完全不同：一个是界面/代码回归，一个是 driver 或端口起不来。"""
    assert main([_junit(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "setup/teardown" in out and "call" in out
    assert "SessionNotCreatedException" in out, "异常原文要进注解，否则等于没取证"
    assert "assert 0 == 3" in out


def _split(line: str) -> tuple[str, str]:
    """把一行工作流命令拆成 (属性段, 正文段) —— 分隔符就是那两个冒号。"""
    head, _, body = line[2:].partition("::")
    return head, body


def test_the_command_grammar_survives_the_test_names(tmp_path, capsys) -> None:
    """`title=` 里不许有裸冒号 —— 用例名带 `::`，不转义标题就会被腰斩。"""
    assert main([_junit(tmp_path)]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines()
             if ln.startswith("::")]
    assert len(lines) >= 2
    for line in lines:
        props, body = _split(line)
        assert "::" not in body
        assert ":" not in props.split("title=", 1)[-1]
        assert body, f"正文空了：{line}"


def test_the_body_is_escaped_not_reformatted(tmp_path, capsys) -> None:
    """真换行与 `#` 会把后半句弄丢，必须转义成 %0A / %23。"""
    xml = PROBE.replace('message="AssertionError: 计数不对"',
                        'message="AssertionError: 计数不对 #3"')
    assert main([_junit(tmp_path, xml)]) == 0
    body = next(_split(ln)[1] for ln in capsys.readouterr().out.splitlines()
                if ln.startswith("::error") and "test_run" in ln)
    assert "%23" in body and "\n" not in body


def test_skips_are_counted_because_the_api_cannot_see_them(tmp_path, capsys) -> None:
    """降级成 skip 的那几条只在日志里，匿名 API 看不见 —— 计数行就是为它准备的。"""
    assert main([_junit(tmp_path)]) == 0
    notice = [ln for ln in capsys.readouterr().out.splitlines()
              if ln.startswith("::notice")]
    assert len(notice) == 1
    assert "case=3" in notice[0] and "skipped=1" in notice[0] and "ran=2" in notice[0]


def test_a_missing_junit_is_itself_reported_and_exits_zero(tmp_path, capsys) -> None:
    """取证脚本不许把 job 变得更红 —— 它是 `if: failure()` 之后才跑的。"""
    assert main([str(tmp_path / "nope.xml")]) == 0
    out = capsys.readouterr().out
    assert "::error" in out and "pytest" in out


def test_an_unparsable_junit_still_prints_the_counts_line(tmp_path, capsys) -> None:
    assert main([_junit(tmp_path, "<testsuites><")]) == 0
    out = capsys.readouterr().out
    assert "读不动" in out and "case=0" in out


def test_the_flood_is_capped_but_says_so(tmp_path, capsys) -> None:
    """注解有配额，全倒出去会把别的东西挤掉 —— 截断必须明说截了多少。"""
    cases = "".join(
        f'<testcase classname="c{i}" name="t{i}"><failure message="炸"/></testcase>'
        for i in range(15))
    xml = f'<testsuites><testsuite name="p">{cases}</testsuite></testsuites>'
    assert main([_junit(tmp_path, xml)]) == 0
    out = capsys.readouterr().out
    assert out.count("::error") == 12
    assert "还有 3 条失败没写出来" in out
