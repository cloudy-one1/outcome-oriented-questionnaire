#!/usr/bin/env python
"""把 e2e 的 junit 失败写成 GitHub **注解**。

为什么单独要这么一条：Actions 的**日志**必须登录才看得到（匿名 REST 拿它是 403），
而 check-run 的**注解**匿名就能读。于是"本地按 CI 口径（干净检出 + 只装
`requirements*` + cp437）跑同一命令全绿、runner 上却红"这种分歧，此前只有两条路：
要么有人把日志复制出来，要么永远查不下去。注解是第三条。

判据留在 junit 里，不在这里重新发明：`<failure>` 是用例自己断言失败（界面、时序、
代码回归），`<error>` 是 fixture 在 setup/teardown 炸的（driver 起不来、端口 bind 不上）。
这两种红指向完全不同的东西，所以注解标题里把阶段写明白。

本脚本**永远退 0**：它是 `if: failure()` 的事后取证，把 job 变得更红没有意义。
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_ANNOTATIONS = 12
MESSAGE_CAP = 900

# junit 的 <failure> / <error> 之外还有一类值得报出来：整个 collection 失败
# （import 就炸），pytest 会写成 <testcase name="tests/..." classname=""> 带 <error>。
_PHASE = {"failure": "call 阶段断言失败（界面/时序/代码回归）",
          "error": "setup/teardown 阶段报错（driver 或端口起不来）"}


def _esc(text: str) -> str:
    """GitHub workflow 命令的三个转义：`%`、换行、`#`（注释符会吞掉后半句）。"""
    return (text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
            .replace("#", "%23"))


def _esc_prop(text: str) -> str:
    """属性值（`title=` 那一段）要多转义 `:` 与 `,`。

    用例名长这样：`tests.test_x::test_y` —— 不转义冒号的话，GitHub 会把它当成
    "标题到此为止"的分隔符，标题从中间被腰斩，测试名反而掉回正文里。
    """
    return _esc(text).replace(":", "%3A").replace(",", "%2C")


def _body_of(node: ET.Element) -> str:
    """异常原文 + 正文里那几条 `E ` 行（TimeoutException 的信息几乎全在正文）。"""
    parts: list[str] = []
    attr = (node.get("message") or "").strip()
    if attr:
        parts.append(attr)
    for line in (node.text or "").splitlines():
        if line.startswith("E "):
            parts.append(line.strip())
    text = " | ".join(parts)
    return text[:MESSAGE_CAP] if text else "(junit 里这条没有正文)"


def findings(junit: Path) -> list[tuple[str, str, str]]:
    """返回 (标题, 正文, 结局种类) 列表，先 error 后 failure 排（起不来比测不准严重）。"""
    root = ET.parse(junit).getroot()
    out: list[tuple[str, str, str]] = []
    for case in root.iter("testcase"):
        for kind in ("error", "failure"):
            for node in case.findall(kind):
                name = f'{case.get("classname") or ""}::{case.get("name") or "?"}'
                out.append((f'[{_PHASE[kind]}] {name}'[:120], _body_of(node), kind))
    return out


def counts(junit: Path) -> tuple[int, int, int]:
    root = ET.parse(junit).getroot()
    cases = list(root.iter("testcase"))
    ran = sum(1 for c in cases if not c.findall("skipped"))
    skipped = sum(1 for c in cases if c.findall("skipped"))
    return len(cases), ran, skipped


def _utf8_stdout() -> None:
    """注解正文里必然有中文，而 runner 的控制台默认编码不是 UTF-8（Windows 上是 cp437）。

    不先把输出流换掉，这条**取证**脚本会自己先 `UnicodeEncodeError` 死 —— 本机中文
    控制台（cp936）反而测不出来，实测踩过。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):  # pragma: no cover - 老 wrapper 流
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("junit", help="pytest --junitxml 产出的文件")
    parser.add_argument("--max", type=int, default=MAX_ANNOTATIONS)
    args = parser.parse_args(argv)

    path = Path(args.junit)
    if not path.exists():
        print("::error title=e2e-junit 不存在::"
              + _esc(f"{path} 没有写出来：pytest 可能在生成报告之前就崩了"
                     "（收集期 import 失败、或被 timeout 掐死）"))
        return 0
    try:
        found = findings(path)
    except ET.ParseError as e:
        found = []
        total, ran, skipped = 0, 0, 0
        print("::error title=e2e-junit 读不动::"
              + _esc(f"{type(e).__name__}: {e}"))
    else:
        total, ran, skipped = counts(path)
    for title, body, _kind in found[:args.max]:
        print(f"::error title={_esc_prop(title)}::{_esc(body)}")
    if len(found) > args.max:
        print(f"::warning title=注解被截断::还有 {len(found) - args.max} 条失败没写出来")
    print("::notice title=e2e junit 计数::"
          + _esc(f"case={total} ran={ran} skipped={skipped} "
                 f"error={sum(1 for f in found if f[2] == 'error')} "
                 f"failure={sum(1 for f in found if f[2] == 'failure')}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
