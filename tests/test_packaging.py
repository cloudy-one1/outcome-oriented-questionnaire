"""v3.0 打包与运行形态的契约：pyproject / requirements / 版本三者不许各说各话。

为什么要钉：加了 pyproject 就有两份依赖清单，加了 [project.scripts] 就有两个入口
函数名。这类"看起来都写好了"的东西，出问题时的表现是用户照 README 装完却
起不来，而 CI 全绿 —— 所以逐字段比对，而不是只测"文件存在"。
"""

from __future__ import annotations

import os
import re
import sys

import pytest

# `tomllib` 是 Python 3.11 才进标准库的，而本仓库的支持下限是 3.10（CI 矩阵里就有
# 一条 3.10 的 leg）—— 裸 import 会在**收集阶段**抛 ModuleNotFoundError，
# 把整套离线测试一起拖红，而不是只红这一个模块。
# 这里按模块级 skip：本文件的契约（pyproject / requirements / 版本三方对齐）在
# CI 的 3.13 那条 leg 上是真跑的，所以少一条 leg 覆盖不损失任何判定，
# 而这些检查本身与解释器版本无关。
tomllib = pytest.importorskip("tomllib")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import __version__  # noqa: E402


@pytest.fixture(scope="module")
def pyproject() -> dict:
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)


def _requirement_pins(name: str) -> list[str]:
    """requirements*.txt 里的 `pkg==ver` 行（忽略注释与可选段）。"""
    pins: list[str] = []
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pins.append(line.replace(" ", "").lower())
    return pins


def test_project_version_matches_package_version(pyproject: dict) -> None:
    assert pyproject["project"]["version"] == __version__, (
        "pyproject 与 src.__version__ 漂了：装出来的包元数据，与 CLI --help 那行"
        " `v{__version__}` 和 GUI 窗口标题报的不是一个版本"
        "（本工具没有 --version 参数，版本号只出现在这两处）"
    )


def test_readme_version_badge_matches_package_version(pyproject: dict) -> None:
    """同一个版本号有**三处**手写：pyproject、``src.__version__``、README 顶上的徽章。

    前两处由上面那条钉住了，徽章这一处一直是散的 —— 定版时漏改它，CI 照样全绿，
    而访客在仓库首页看到的还是上一个版本。故一并纳入门禁（本轮定 v3.3.0 时就是
    按这条挨个改的三处）。
    """
    badge = f"Version-{pyproject['project']['version']}-brightgreen"
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        assert badge in f.read(), f"README 的版本徽章与包版本不一致：找 {badge!r}"


def test_required_python_floor_matches_readme(pyproject: dict) -> None:
    assert pyproject["project"]["requires-python"] == ">=3.10"


def test_core_dependencies_mirror_requirements_txt(pyproject: dict) -> None:
    assert [d.replace(" ", "").lower() for d in pyproject["project"]["dependencies"]] == (
        _requirement_pins("requirements.txt")
    ), "pyproject 的依赖必须与 requirements.txt 逐条一致"


def test_optional_extras_are_pinned_to_the_documented_versions(pyproject: dict) -> None:
    extras = pyproject["project"]["optional-dependencies"]
    assert extras["uc"] == ["undetected-chromedriver==3.5.5"]
    assert extras["qr"] == ["opencv-python==4.10.0.84"]
    assert [d.lower() for d in extras["dev"]] == _requirement_pins("requirements-dev.txt")


def test_console_scripts_point_at_real_entry_functions(pyproject: dict) -> None:
    from gui.app import main as gui_main
    from src.cli import main as cli_main
    from webui.__main__ import main as web_main

    scripts = pyproject["project"]["scripts"]
    assert scripts["wjx-fill"] == "src.cli:main"
    assert scripts["wjx-gui"] == "gui.app:main"
    assert scripts["wjx-web"] == "webui.__main__:main"
    assert callable(cli_main) and callable(gui_main) and callable(web_main)


def test_declared_packages_cover_every_imported_module(pyproject: dict) -> None:
    """[tool.setuptools].packages 漏一个子包 → 装出来的包 import 就炸。

    直接拿磁盘上的包目录比，不靠人记。
    """
    declared = set(pyproject["tool"]["setuptools"]["packages"])
    on_disk = set()
    for base in ("src", "gui", "webui"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, base)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            if "__init__.py" in filenames:
                rel = os.path.relpath(dirpath, ROOT).replace(os.sep, ".")
                on_disk.add(rel)
    assert on_disk - declared == set(), f"未声明的子包: {sorted(on_disk - declared)}"


def test_the_static_files_the_console_serves_are_declared_and_exist(
    pyproject: dict
) -> None:
    """静态文件漏进 wheel 的症状是"整个工具坏了"，而源码目录跑着一切正常。

    所以这里两头都钉：``server.STATIC_FILES`` 里每个名字都得在 ``package-data``
    里出现，并且磁盘上真有这个文件且非空。少任何一头，``pip install`` 之后
    ``wjx-web`` 起得来、端口听得见，页面却是一片空白。
    """
    from webui.server import STATIC_FILES

    declared = pyproject["tool"]["setuptools"]["package-data"]["webui"]
    served = {"static/" + name for name, _type in STATIC_FILES.values()}
    assert set(declared) == served, (
        "package-data 与 server.STATIC_FILES 不同名："
        "装出来的包会少文件")
    for name in declared:
        path = os.path.join(ROOT, "webui", *name.split("/"))
        assert os.path.getsize(path) > 0, f"{name} 是空的"


def test_dockerfile_is_referenced_honestly() -> None:
    """Dockerfile 的构建状态必须写在文件自己身上，且与 CI 的真实配置对得上。

    v3.1 之前这句话是"没有被构建验证过"；`docker-smoke.yml` 进来之后它反过来 ——
    断言随之改成点名那条 workflow、并如实写明它**不在 push 的必填检查里**。
    口径必须跟着配置走：这是全仓库唯一一份"镜像到底构建没构建"的说明。
    """
    with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as f:
        text = f.read()
    assert "没有被构建验证过" not in text, "构建状态变了，头注释还停在旧口径"
    assert "docker-smoke.yml" in text, "要说清是谁在构建它"
    assert "不在 push" in text, "周报构建 ≠ 每次提交都构建，这句不能省"
    assert re.search(r"^FROM python:3\.\d+-slim", text, re.M)

    with open(
        os.path.join(ROOT, ".github", "workflows", "docker-smoke.yml"),
        encoding="utf-8",
    ) as f:
        wf = f.read()
    # 只看真正的 YAML 键，注释里出现"push"是正常的（头注释正是在解释它不在 push 里）
    keys = [
        ln.strip()
        for ln in wf.split("jobs:")[0].splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert "schedule:" in keys and "workflow_dispatch:" in keys, "定时 + 手动，二者都得有"
    assert not any(k.startswith("push:") or k.startswith("pull_request:") for k in keys), (
        "docker 构建不该被每次提交触发 —— 那正是它单独成 workflow 的理由"
    )
    assert "docker build" in wf

    with open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8") as f:
        assert "docker" not in f.read().lower(), "主 CI 里没有 docker，别让人以为有"
