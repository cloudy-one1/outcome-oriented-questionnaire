"""Stealth 注入层的真浏览器验收（v4.4，对照 docs/stealth_audit.md）。

E2E 主套件（``test_e2e_integration.py``）刻意用**裸 selenium**——它要隔离的是
交互层，浏览器配置（指纹/参数）一概不带，所以它天然看不见 stealth。本文件用
项目自己的 driver factory（``create_edge_driver`` / ``create_chrome_driver``，
含 CDP stealth 注入 + 浏览器级反检测参数）开 headless 浏览器，加载本地探针页
``tests/fixtures/stealth_probe.html``，逐项断言指纹伪装真的生效。

为什么必须有这一环：v4.4 之前 stealth JS 里三个连字符键没加引号，整段 JS 是
SyntaxError、CDP 静默不执行，**全部指纹伪装从未生效过**——而当时的三层防线
（离线字符串比对、裸 selenium E2E、node --check 只覆盖 _scripts.py）全都看不见。
真浏览器 + 真注入路径的验收是缺的最后一环。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parent.parent
PROBE_HTML = PROJ / "tests" / "fixtures" / "stealth_probe.html"

sys.path.insert(0, str(PROJ))

from src.browser.driver_factory import create_chrome_driver, create_edge_driver  # noqa: E402

pytestmark = pytest.mark.integration

# 探针页记录的就绪信号轮询上限（含 800ms 延迟清扫 + 余量）
_PROBE_READY_TIMEOUT = 10.0

# 必须全部为 True 的检查项（'no-*' 开头的字符串值 = 环境缺该 API，单独对待）
MUST_PASS = (
    "webdriver_false",
    "webdriver_locked",
    "webdriver_write_blocked",
    "webdriver_getter_tostring",
    "language",
    "plugins_five",
    "plugins_prototypes",
    "mime_prototypes",
    "plugins_named_item",
    "pdf_viewer_enabled",
    "device_memory_capped",
    "connection_stable",
    "notification_default",
    "intl_timezone",
    "tz_offset",
    "chrome_csi",
    "csi_tostring",
    "screen_outer_consistent",
    "webgl_masked",
    "webgl2_masked",
    "cdc_sweep",
)


@pytest.fixture(scope="module")
def stealth_driver():
    """经项目 driver factory 启动一次 headless 浏览器（Edge → Chrome 回退）。

    与 E2E 主套件的裸 driver 不同：这条路径带完整的 CDP stealth 注入与
    v4.4 浏览器级反检测参数 —— 这正是被测对象。
    """
    driver = None
    errors: list[str] = []
    try:
        driver = create_edge_driver(headless=True)
    except Exception as e:  # pragma: no cover - 仅环境失败时走
        errors.append(f"edge:{e!r}")
    if driver is None:
        try:
            driver = create_chrome_driver(headless=True)
        except Exception as e:  # pragma: no cover - 仅环境失败时走
            errors.append(f"chrome:{e!r}")
    if driver is None:
        pytest.skip(f"无可用 webdriver（{'; '.join(errors)}）")

    driver.set_script_timeout(10)
    driver.get("file:///" + PROBE_HTML.as_posix())
    yield driver
    try:
        driver.quit()
    except Exception:
        pass


def _wait_probe_ready(driver) -> dict:
    """轮询就绪位后读取探针结果；超时就把现状抛出来便于诊断。"""
    deadline = time.time() + _PROBE_READY_TIMEOUT
    while time.time() < deadline:
        try:
            if driver.execute_script("return window.__PROBE_READY === true"):
                return driver.execute_script("return window.__PROBE") or {}
        except Exception:
            pass  # 导航/脚本窗口期抖动，继续等
        time.sleep(0.2)
    pytest.fail(f"探针页 {_PROBE_READY_TIMEOUT}s 内未就绪（stealth JS 可能整体未执行）")


def test_stealth_fingerprint_layers_all_hold(stealth_driver) -> None:
    probe = _wait_probe_ready(stealth_driver)
    assert probe, "探针结果为空 —— stealth JS 大概率整体没跑起来"

    failures = {k: v for k, v in probe.items() if k in MUST_PASS and v is not True}
    assert not failures, (
        "stealth 探针存在未通过的检查项（值 False 或 ERR:... 均算失败）: "
        + ", ".join(f"{k}={v!r}" for k, v in sorted(failures.items()))
    )
    # 探针页至少要覆盖到声明过的检查项，防止探针自身缩水造成假绿
    missing = [k for k in MUST_PASS if k not in probe]
    assert not missing, f"探针页缺少检查项: {missing}"


def test_connection_fingerprint_is_instance_stable(stealth_driver) -> None:
    """再跨一次读取间隔验证 rtt/downlink 稳定（与探针页内部双读互补）。"""
    _wait_probe_ready(stealth_driver)
    r1 = stealth_driver.execute_script("return navigator.connection.rtt")
    d1 = stealth_driver.execute_script("return navigator.connection.downlink")
    r2 = stealth_driver.execute_script("return navigator.connection.rtt")
    d2 = stealth_driver.execute_script("return navigator.connection.downlink")
    assert r1 == r2, f"rtt 连读不稳定: {r1} != {r2}"
    assert d1 == d2, f"downlink 连读不稳定: {d1} != {d2}"
