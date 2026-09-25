"""E2E 的"真的在浏览器里跑过吗"检查（配合 tests/conftest.py 的降级白名单）。

为什么需要它：pytest 全 skip 时退出码仍是 0，而 tests/conftest.py 会把"浏览器
自己起不来"的失败降成 skip —— 两件好事各自合理，叠在一起就成了"驱动装不上 =>
job 永远绿"。本脚本用 junit 报告的数字把这条路堵死。

用法：pytest --junitxml=e2e-junit.xml ... && python scripts/e2e_gate.py e2e-junit.xml
     [--min-run N] [--require-file tests/test_webui_e2e.py]...
退出码：0 通过 / 1 没真跑过任何用例（或某个 --require-file 那侧一条都没跑）
      / 2 报告读不了（文件缺失或格式不对）。

`--min-run` 只拦"整片 skip"。拦不住的是"一半被吞成 skip"：`tests/conftest.py` 会把
浏览器/驱动自身故障降级成 skip，于是 webui 那 8 项全部起不来时 `ran` 依然 >= 1、
job 依然绿 —— 而它是这条 leg 唯一验证"宿主这一侧的整条链在真浏览器里成立"的部分。
`--require-file` 按文件逐个要求"至少一条、且一条都不许是 skip"。
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

# 输出保持 ASCII：CI 的 Windows runner 控制台编码不固定（cp437/cp1252），
# 中文在这里会变成一次 UnicodeEncodeError 或者一串问号。
USAGE = ("usage: python scripts/e2e_gate.py <junit.xml> "
         "[--min-run N] [--require-file PATH]...")


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


def module_of(want: str) -> str:
    """把 `tests/test_x.py` 与 `tests.test_x` 统一成 junit 里 classname 的形状。"""
    name = want.strip().replace("\\", "/")
    if name.startswith("./"):
        name = name[2:]
    if name.endswith(".py"):
        name = name[:-3]
    return name.replace("/", ".")


def file_counts(root: ET.Element, want: str) -> tuple[int, int]:
    """返回该文件贡献的 (条数, 其中 skip 的条数)。"""
    module = module_of(want)
    total = skipped = 0
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        if classname != module and not classname.startswith(module + "."):
            continue
        total += 1
        if case.findall("skipped"):
            skipped += 1
    return total, skipped


def check_required(path: Path, required: list[str]) -> list[str]:
    """每个 --require-file 一条判词；返回需要判 FAIL 的消息列表。"""
    root = ET.parse(path).getroot()
    problems: list[str] = []
    for want in required:
        total, skipped = file_counts(root, want)
        ran = total - skipped
        if total == 0:
            problems.append(
                f"FAIL: required file contributed no testcases to the report: {want}\n"
                "      the module was renamed, deselected by -m, or never collected;"
                " the CI config and the test tree drifted apart")
        elif ran == 0:
            problems.append(
                f"FAIL: required file never ran: {want}"
                f" (all {skipped} case(s) were skipped)\n"
                "      tests/conftest.py degrades browser/driver failures to skip,"
                " so this leg proves nothing about that file. Check the driver.")
        else:
            print(f"OK: required file ran: {want} ({ran}/{total})")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=USAGE, add_help=True)
    parser.add_argument("junit", type=Path)
    parser.add_argument("--min-run", type=int, default=1)
    parser.add_argument("--require-file", action="append", default=[],
                        metavar="PATH",
                        help="this file must contribute >=1 non-skipped testcase")
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
    try:
        problems = check_required(args.junit, args.require_file)
    except ET.ParseError as exc:  # pragma: no cover - counts() 已经解析过一次
        print(f"FAIL: cannot read junit report: {exc}")
        return 2
    for message in problems:
        print(message)
    if problems:
        return 1
    print("OK: E2E ran in a real browser")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
