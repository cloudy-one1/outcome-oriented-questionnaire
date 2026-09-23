"""README 的覆盖率口径段落由本脚本从 coverage.json 生成，`--check` 是门禁。

v2.x~v3.0 期间 README / CHANGELOG / ci.yml 的数字要人手对齐，光 2026-09-22 一天就
发了两次标题为「口径同步」的提交。派生化之后，改这些数字的唯一合法路径是跑本脚本。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
BEGIN = "<!-- BEGIN AUTO-GENERATED 覆盖率口径 · scripts/readme_coverage.py · 不要手改 -->"
END = "<!-- END AUTO-GENERATED 覆盖率口径 -->"
FLOOR_RE = re.compile(r"--cov-fail-under=(\d+(?:\.\d+)?)")
NUM_RE = re.compile(r"\d+(?:\.\d+)?%?")


def usage_fail(message: str) -> NoReturn:
    """跑不动（不是口径漂移）：退出码 2，与"漂移即红"的 1 分开 —— 参照 CLI 的 0/1/2 约定。"""
    print(message, file=sys.stderr)
    raise SystemExit(2)


@dataclass(frozen=True)
class Stat:
    statements: int
    missing: int

    @property
    def percent(self) -> float:
        if self.statements <= 0:
            return 100.0
        return 100.0 * (self.statements - self.missing) / self.statements

    @staticmethod
    def of(members: list["Stat"]) -> "Stat":
        return Stat(sum(m.statements for m in members), sum(m.missing for m in members))


def canon(key: str) -> str:
    path = key.replace("\\", "/")
    root_posix = str(ROOT).replace("\\", "/")
    if path.startswith(root_posix):
        path = path[len(root_posix):]
    path = re.sub(r"^\.?/+/", "", path)
    positions = [path.find(pkg) for pkg in ("src/", "gui/") if path.find(pkg) >= 0]
    return path[min(positions):] if positions else path.lstrip("/")


def load_coverage(path: Path) -> dict[str, Stat]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    stats: dict[str, Stat] = {}
    for key, entry in raw.get("files", {}).items():
        summary = entry.get("summary", {})
        stats[canon(key)] = Stat(
            int(summary.get("num_statements", 0)),
            int(summary.get("missing_lines", 0)),
        )
    return stats


def load_floor() -> str:
    match = FLOOR_RE.search(CI_YML.read_text(encoding="utf-8"))
    if match is None:
        usage_fail(f"FAIL: {CI_YML.name} 里没有 --cov-fail-under=，地板无从生成")
    return match.group(1)


def under(stats: dict[str, Stat], prefix: str) -> Stat:
    members = [stat for name, stat in stats.items() if name.startswith(prefix)]
    if not members:
        usage_fail(f"FAIL: coverage.json 里 {prefix} 一个文件都没有")
    return Stat.of(members)


def known(stats: dict[str, Stat], module: str) -> Stat:
    if module not in stats:
        raise SystemExit(f"FAIL: {module} 在缺口清单里，但 coverage.json 找不到它（改名或删文件后没同步）")
    return stats[module]


def pct(value: float) -> str:
    return f"{value:.1f}%"


def module_label(row: dict[str, Any], stats: dict[str, Stat]) -> str:
    if row.get("group"):
        count = len([name for name in stats if name.startswith(row["group"])])
        return f"`{row['group']}`（{count} 个文件合计）"
    return f"`{row['module']}`"


def stat_of_row(row: dict[str, Any], stats: dict[str, Stat]) -> Stat:
    if row.get("group"):
        return under(stats, row["group"])
    return known(stats, row["module"])


def full_file_note(stats: dict[str, Stat], prefix: str) -> str:
    names = sorted(
        Path(name).stem for name, stat in stats.items()
        if name.startswith(prefix) and stat.percent >= 100.0 and Path(name).name != "__init__.py"
    )
    return f"（{'、'.join(f'`{n}`' for n in names)} 已 100%）" if names else ""


def render(spec: dict[str, Any], stats: dict[str, Stat], floor: str) -> str:
    closed, opens = spec["closed_gaps"], spec["open_gaps"]
    out: list[str] = [
        f"**口径**：{spec['caliber']}。地板 `--cov-fail-under={floor}`"
        f"（从 `{CI_YML.relative_to(ROOT).as_posix()}` 读出来，不是手抄的）。",
        "",
        "| 范围 | 离线覆盖率 |",
        "|---|---|",
        f"| 全部 | **{pct(Stat.of(list(stats.values())).percent)}** |",
    ]
    for prefix in ("src/", "gui/"):
        stat = under(stats, prefix)
        out.append(f"| `{prefix}` | {pct(stat.percent)}（{stat.statements} 条语句剩 {stat.missing} 行） |")

    out += [
        "",
        f"#### {closed['title']}",
        "",
        f"| 模块 | {closed['baseline_header']} | 现在 | 契约测试 |",
        "|---|---|---|---|",
    ]
    for row in closed["rows"]:
        stat = stat_of_row(row, stats)
        now = f"**{pct(stat.percent)}**"
        if row.get("group"):
            now += full_file_note(stats, row["group"])
        else:
            now += f"（剩 {stat.missing} 行" + (f"，{row['note']}" if row.get("note") else "") + "）"
        tests = "、".join(f"`{t}`" for t in row.get("covered_by", []))
        out.append(f"| {module_label(row, stats)} | {row['baseline']} | {now} | {tests or '—'} |")

    out += ["", f"#### {opens['title']}", "", "| 模块 | 离线覆盖率 | 为什么还留着 |", "|---|---|---|"]
    for row in opens["rows"]:
        reason = row.get("reason", "").strip()
        if not reason:
            raise SystemExit(f"FAIL: {row['module']} 的缺口没有写 reason，不许留空")
        out.append(f"| {module_label(row, stats)} | {pct(stat_of_row(row, stats).percent)} | {reason} |")

    declared = {r["module"] for r in closed["rows"] if r.get("module")} | {r["module"] for r in opens["rows"]}
    threshold = spec["declare_below_pct"]
    undeclared = sorted(
        name for name, stat in stats.items()
        if name not in declared
        and not name.startswith(spec["group_exempt_prefix"])
        and stat.percent < threshold
    )
    if undeclared:
        raise SystemExit(
            f"FAIL: 以下模块离线覆盖率低于 {threshold}% 却没登记理由，要么补测要么写进缺口清单："
            f"{', '.join(undeclared)}"
        )
    out += [
        "",
        "> 本块由 `python scripts/readme_coverage.py --write` 从 `coverage.json` 生成，`--check` 已进 CI 当门禁",
        "> —— 手改这里的数字会在下次推送时红掉。`--write` 会拒绝装了 `opencv-python` /",
        "> `undetected-chromedriver` 的解释器，因为本块的口径就是 CI 那个不装可选依赖的环境。",
        "> 模块清单与缺口理由维护在 `scripts/coverage_gaps.json`（reason 留空同样是红）。",
    ]
    return "\n".join(out)


def splice(readme: str, block: str) -> str:
    start, end = readme.find(BEGIN), readme.find(END)
    if start < 0 or end <= start:
        usage_fail("FAIL: README 里找不到生成块哨兵，块被删掉了")
    head = readme[: start + len(BEGIN)]
    tail = readme[end:]
    newline = "" if head.endswith("\n") else "\n"
    lead = "" if tail.startswith("\n") else "\n"
    return head + newline + block + lead + tail


def number_delta(old_line: str, new_line: str, spec: dict[str, Any]) -> bool:
    """形状一致、且每个数字只差在抖动量级内时，认为这一行没有真的漂移。"""
    if NUM_RE.sub("#", old_line) != NUM_RE.sub("#", new_line):
        return False
    old = list(NUM_RE.finditer(old_line))
    new = list(NUM_RE.finditer(new_line))
    if len(old) != len(new):
        return False
    for left, right in zip(old, new):
        limit = spec["tolerance_pp"] if left.group().endswith("%") else spec["tolerance_lines"]
        if abs(float(left.group().rstrip("%")) - float(right.group().rstrip("%"))) > limit:
            return False
    return True


def compare(readme: str, block: str, spec: dict[str, Any]) -> list[str]:
    want = splice(readme, block).split("\n")
    have = readme.split("\n")
    problems: list[str] = []
    for index, (want_line, have_line) in enumerate(zip(want, have), start=1):
        if want_line != have_line and not number_delta(want_line, have_line, spec):
            problems.append(f"  README:{index}\n    落盘: {have_line}\n    应为: {want_line}")
    return problems


def optional_deps_present() -> list[str]:
    """当前解释器能不能 import 那两处可选依赖 —— README 的口径是不装它们的环境。"""
    return [m for m in ("cv2", "undetected_chromedriver") if importlib.util.find_spec(m) is not None]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 / 校验 README 的覆盖率口径块")
    parser.add_argument("--coverage", type=Path, default=ROOT / "coverage.json")
    parser.add_argument("--spec", type=Path, default=ROOT / "scripts" / "coverage_gaps.json")
    parser.add_argument("--force-env", action="store_true",
                        help="明知当前环境装了两处可选依赖，仍要按它的数字重写 README")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="比对落盘与生成结果，漂移即失败（门禁）")
    mode.add_argument("--write", action="store_true", help="把生成结果写回 README")
    args = parser.parse_args(argv)

    if not args.coverage.exists():
        usage_fail(
            f"FAIL: 找不到 {args.coverage}，先跑：pytest tests/ -m \"not integration\" "
            f"--cov=src --cov=gui --cov-report=json"
        )
    if args.write and not args.force_env:
        extras = optional_deps_present()
        if extras:
            usage_fail(
                f"FAIL: 当前解释器装了 {', '.join(extras)}，而 README 的口径是不装可选依赖的 CI 环境。"
                f"在干净 venv 里跑本脚本（coverage.json 也要是那个环境产出的），确实要写就加 --force-env"
            )
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    block = render(spec, load_coverage(args.coverage), load_floor())
    readme = README.read_text(encoding="utf-8")

    if args.write:
        updated = splice(readme, block)
        README.write_text(updated, encoding="utf-8")
        print("已重写" if updated != readme else "已是最新")
        return 0

    problems = compare(readme, block, spec)
    if problems:
        print(f"FAIL: README 的覆盖率口径与 {args.coverage.name} 实测漂移（容忍 "
              f"{spec['tolerance_pp']}pp / {spec['tolerance_lines']} 行），修法是跑 "
              f"`python scripts/readme_coverage.py --write`：")
        print("\n".join(problems))
        return 1
    print("OK: README 覆盖率口径与实测一致（缺口清单也在覆盖全部低覆盖模块）")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
