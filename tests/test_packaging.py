"""v3.0 打包与运行形态的契约：pyproject / requirements / 版本三者不许各说各话。

为什么要钉：加了 pyproject 就有两份依赖清单，加了 [project.scripts] 就有两个入口
函数名。这类"看起来都写好了"的东西，出问题时的表现是用户照 README 装完却
起不来，而 CI 全绿 —— 所以逐字段比对，而不是只测"文件存在"。
"""

from __future__ import annotations

import os
import re
import sys
import tomllib

import pytest

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
        "pyproject 与 src.__version__ 漂了：装出来的包和 --version 报的不是一个版本"
    )


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
    from src.cli import main as cli_main
    from gui.app import main as gui_main

    scripts = pyproject["project"]["scripts"]
    assert scripts["wjx-fill"] == "src.cli:main"
    assert scripts["wjx-gui"] == "gui.app:main"
    assert callable(cli_main) and callable(gui_main)


def test_declared_packages_cover_every_imported_module(pyproject: dict) -> None:
    """[tool.setuptools].packages 漏一个子包 → 装出来的包 import 就炸。

    直接拿磁盘上的包目录比，不靠人记。
    """
    declared = set(pyproject["tool"]["setuptools"]["packages"])
    on_disk = set()
    for base in ("src", "gui"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, base)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            if "__init__.py" in filenames:
                rel = os.path.relpath(dirpath, ROOT).replace(os.sep, ".")
                on_disk.add(rel)
    assert on_disk - declared == set(), f"未声明的子包: {sorted(on_disk - declared)}"


def test_dockerfile_is_referenced_honestly() -> None:
    """镜像没进 CI 这件事必须写在 Dockerfile 自己身上，而不是只写在 README 里。"""
    with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as f:
        text = f.read()
    assert "没有被构建验证过" in text
    assert re.search(r"^FROM python:3\.\d+-slim", text, re.M)
