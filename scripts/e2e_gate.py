"""E2E 的"真的在浏览器里跑过吗"检查（配合 tests/conftest.py 的降级白名单）。

为什么需要它：pytest 全 skip 时退出码仍是 0，而 tests/conftest.py 会把"浏览器
自己起不来"的失败降成 skip —— 两件好事各自合理，叠在一起就成了"驱动装不上 =>
job 永远绿"。本脚本用 junit 报告的数字把这条路堵死。

用法：pytest --junitxml=e2e-junit.xml ... && python scripts/e2e_gate.py e2e-junit.xml
退出码：0 通过 / 1 没真跑过任何用例 / 2 报告读不了（文件缺失或格式不对）。
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

# 输出保持 ASCII：CI 的 Windows runner 控制台编码不固定（cp437/cp1252），
# 中文在这里会变成一次 UnicodeEncodeError 或者一串问号。
USAGE = "usage: python scripts/e2e_gate.py <junit.xml> [--min-run N]"


def counts(path: Path) -> tuple[int, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        raise ValueError(f"no <testsuite> element found in {path}")
    total = skipped = 0
    for suite in suites:
        total += int(suite.get("tests") or 0)
        skipped += int(suite.get("skipped") or 0)
    return total, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=USAGE, add_help=True)
    parser.add_argument("junit", type=Path)
    parser.add_argument("--min-run", type=int, default=1)
    args = parser.parse_args(argv)

    if not args.junit.exists():
        print(f"FAIL: junit report not found: {args.junit}")
        return 2
    try:
        total, skipped = counts(args.junit)
    except (ET.ParseError, ValueError) as exc:
        print(f"FAIL: cannot read junit report: {exc}")
        return 2

    ran = total - skipped
    print(f"e2e: tests={total} skipped={skipped} ran={ran} (min-run={args.min_run})")
    if total == 0:
        print("FAIL: no integration test was collected "
              "(marker not registered, or -m filter matched nothing)")
        return 1
    if ran < args.min_run:
        print("FAIL: every E2E case was skipped -> the browser leg never ran, "
              "so this job proves nothing. Check the driver on this runner.")
        return 1
    print("OK: E2E ran in a real browser")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
