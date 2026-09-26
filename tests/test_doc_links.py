"""对外文档里的本地链接必须真的指得到地方 —— 文件在、锚点在、而且文件在版本库里。

为什么现在补这一条：README 瘦身把细节搬进 ``docs/``，搬家最容易留下的正好是这两类
—— 标题一改锚点就断；``.gitignore`` 的 ``docs/*`` 默认全忽略（只放行 ``design/`` 与
``reviews/``），新文档忘了显式放行的话，本地看一切正常，GitHub 上那个链接却是 404。
两类都在人眼能扫到的范围之外：锚点要按 GitHub 的 slug 规则算，忽略规则要问 git。

只扫**对用户讲话的那几页**（README / CONTRIBUTING / SECURITY 与 ``docs/`` 根下）；
``docs/design`` 与 ``docs/reviews`` 是当时的记录，冻结，不因为日后搬家而回头改。
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCAN = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md",
        *sorted((ROOT / "docs").glob("*.md"))]

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$", re.M)
FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")


def prose(text: str) -> list[str]:
    """去掉围栏代码块后的正文。

    bash 示例里的 ``# 注释`` 长得和标题一模一样，留着只会往锚点清单里塞假条目。
    """
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        head = FENCE_RE.match(line)
        if fence is None:
            if head:
                fence = head.group(1)
                continue
            out.append(line)
        elif head and head.group(1) == fence:
            fence = None
    return out


def slug(heading: str) -> str:
    """GitHub 的标题锚点规则：转小写、剥掉强调与代码标记、标点删掉、空格换成连字符。

    ``_`` 与 ``-`` 留下（它们是词的一部分）；``?`` 与中文全角括号一类删掉。
    """
    text = re.sub(r"[`*_]", "", heading).strip().lower()
    kept = "".join(ch for ch in text if unicodedata.category(ch)[0] != "P" or ch in "-_" or ch == " ")
    return kept.strip("-_ ").replace(" ", "-")


_ANCHORS: dict[str, set[str]] = {}


def anchors_of(target: Path) -> set[str]:
    key = str(target)
    if key not in _ANCHORS:
        body = "\n".join(prose(target.read_text(encoding="utf-8")))
        _ANCHORS[key] = {slug(m.group(2)) for m in HEADING_RE.finditer(body)}
    return _ANCHORS[key]


def local_links() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for doc in SCAN:
        for line in prose(doc.read_text(encoding="utf-8")):
            for raw in LINK_RE.findall(line):
                if not raw.startswith(("http://", "https://", "mailto:")):
                    out.append((doc, raw))
    return out


LINKS = local_links()
pytestmark = pytest.mark.skipif(not LINKS, reason="对外文档里一个本地链接都没有，没什么可查的")


@pytest.mark.parametrize("doc,link", LINKS, ids=[f"{d.name}:{l}" for d, l in LINKS])
def test_local_link_resolves(doc: Path, link: str) -> None:
    path, _, anchor = link.partition("#")
    target = (doc.parent / path).resolve() if path else doc
    assert target.is_file(), f"{doc.name} 链到 {link}，但 {path} 不是磁盘上的文件"
    assert target == ROOT or ROOT in target.parents, f"{doc.name} 链出了仓库：{link}"
    if anchor:
        assert anchor in anchors_of(target), (
            f"{doc.name} 链到 {link}，但 {target.name} 的标题里没有这一条 —— "
            f"标题改过名，或者当初就没这个标题")


@pytest.mark.parametrize("doc,link", LINKS, ids=[f"{d.name}:{l}" for d, l in LINKS])
def test_linked_file_is_in_the_repository(doc: Path, link: str) -> None:
    """本地存在不等于仓库里有 —— ``docs/*`` 那条忽略规则就是专坑这一步的。"""
    path = link.partition("#")[0]
    if not path:
        return
    target = (doc.parent / path)
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(target)],
            cwd=ROOT, capture_output=True, check=False)
    except FileNotFoundError:
        pytest.skip("环境里没有 git，这条无从判断")
    assert tracked.returncode == 0, (
        f"{doc.name} 链到 {path}，它却不在版本库里 —— 本地能打开，GitHub 上是 404。"
        f"如果它该入库（README 链着的对外文档），去 .gitignore 里显式放行")
