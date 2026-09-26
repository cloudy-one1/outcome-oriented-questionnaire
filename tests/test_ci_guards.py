"""门禁自身的守卫（v3.1 A 档第 4 条）。

借的是 SurveyController 的思路（`CI/python_checks/common.py:32-41` 全面禁止
``# type: ignore`` / ``# pyright:``，报错文案直接给替代写法）：**只借规则，不取代码**。
它那份是"零豁免"，我们这份带一个**只允许变小**的基线 —— 因为现存两处豁免各自
代表一个真实的取舍（见下面 BASELINE 里的说明），把它们和"新加的豁免"混在一起
红掉，只会让人学会随手加第三条。

为什么用测试而不是 CI 脚本：pytest 已经是 ci.yml 的必填检查，规则放这儿就同时
在本地 `pytest` 和 CI 生效，不必新增一个 workflow 步骤，也不必给每条检查配
退出码协议。

没借的一条：它禁止 `\\uXXXX` 转义（`common.py:42-46`）。我们 `src/answering_v2.py`
用 `"\\u4e00" <= c <= "\\u9fff"` 判中日韩字符是**代码语义**不是文案，禁了只会逼出
更绕的写法。
"""

from __future__ import annotations

import io

import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 与 pyrightconfig.json 的 include 保持一致（下面第二条用例就是钉这个的）
SCOPED_PATHS = ("src", "webui",
                "run_cli.py", "run_web.py", "conftest.py")

_SUPPRESSION = re.compile(r"^#\s*(type:\s*ignore|pyright:)")

# 现存豁免基线：posix 相对路径 → 允许出现的抑制注释条数。**只准变小**。
# 摘掉一条就同时删掉这里的一行 —— 基线比实际大也是漂移（它在掩盖已经修好的东西）。
BASELINE: dict[str, int] = {
    # 这两处是同一个形状：题号来自 JSON，静态上是 object/Any，而 int() 只接受
    # SupportsInt。写 _as_int() 帮手能修，但那两个文件正被别的批次改（v3.1 的
    # 结构对拍与题干锚定），先记账不抢改。
    "src/anchoring.py": 1,
    "src/detection.py": 1,
    # _conn 声明成非 Optional（全类 20 多处 self._conn.execute 因此不用逐次判空），
    # 而 close() 语义上要把引用清掉。改成 Optional 会给那 20 多处各加一道守卫。
    "src/history.py": 1,
}


def _python_files() -> list[Path]:
    out: list[Path] = []
    for rel in SCOPED_PATHS:
        target = ROOT / rel
        if target.is_dir():
            out += sorted(p for p in target.rglob("*.py")
                          if "__pycache__" not in p.parts)
        elif target.is_file():
            out.append(target)
    return out


def _suppression_lines(path: Path) -> list[int]:
    """只认真正的抑制指令：注释 token 本身以 ``# type: ignore`` / ``# pyright:`` 开头。

    这样"解释为什么不用 ignore"的那些散文注释（`src/qr_utils.py:15`、
    `src/browser/driver_factory.py:393`）不会被误判 —— 它们是句子中间提到这个词。
    """
    src = path.read_text(encoding="utf-8")
    hits: list[int] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type != tokenize.COMMENT:
                continue
            if _SUPPRESSION.match(tok.string):
                hits.append(tok.start[0])
    except (SyntaxError, tokenize.TokenError) as exc:  # pragma: no cover
        raise AssertionError(f"{path} 连 tokenize 都过不去: {exc}") from exc
    return hits


def test_no_new_type_suppressions() -> None:
    found: dict[str, list[int]] = {}
    for path in _python_files():
        lines = _suppression_lines(path)
        if lines:
            found[path.relative_to(ROOT).as_posix()] = lines

    for rel, allowed in BASELINE.items():
        actual = len(found.get(rel, []))
        assert actual <= allowed, (
            f"{rel} 有 {actual} 条类型抑制注释，基线只允许 {allowed} 条。"
            "换法：写成明确的类型、缩小作用域的 cast，或像 src/utils.py 的 "
            "retry_with_backoff 那样用 cast(_Fn, wrapper)。"
        )
        assert actual == allowed, (
            f"{rel} 的抑制注释已经少于基线（{actual} < {allowed}）："
            "把 BASELINE 里这行删掉，否则它会在将来掩盖新加的那条。"
        )

    extra = {k: v for k, v in found.items() if k not in BASELINE}
    assert not extra, (
        f"新增了未经基线登记的类型抑制注释：{extra}。"
        "门禁的意义在于 0 豁免 —— 请改掉它，而不是往 BASELINE 里加一行。"
    )


def test_pyright_scope_and_guard_agree() -> None:
    """pyrightconfig.json 的 include 与本用例的 SCOPED_PATHS 必须是同一份清单。

    两边漂移的症状很安静：把 gui/ 从 include 里删掉，pyright 立刻少查 4k 行，
    而这条禁令还在假装覆盖它 —— v2.8 特意把 gui 纳入门禁的那笔账就白记了。
    """
    import json

    cfg_path = ROOT / "pyrightconfig.json"
    raw = cfg_path.read_text(encoding="utf-8")
    # 文件带 // 注释（人写的说明），json 严格模式读不动 —— 先逐行剥掉注释
    stripped = "\n".join(
        ln for ln in raw.splitlines() if not ln.lstrip().startswith("//")
    )
    include = json.loads(stripped)["include"]
    assert sorted(include) == sorted(SCOPED_PATHS), (
        f"pyright include={sorted(include)} 与守卫清单={sorted(SCOPED_PATHS)} 不一致"
    )
    assert cfg_path.exists()
    # 这两条规则一旦被关，上面的禁令就只剩纸面意义
    opts = json.loads(stripped)
    assert opts["reportUnnecessaryTypeIgnoreComment"] == "warning"
