"""`src/browser/driver_factory.py` 的离线契约测试（v2.6 新增）。

为什么要补这一块：当年 README「已知缺口」里该模块停在 9%（148 条语句），备注写的是
"需真实浏览器；E2E 走的是裸 selenium 而非本模块"。但它其实是整个反检测指纹层
（UA 池 / 屏幕与硬件预设 / CDP stealth 注入）+ 浏览器生命周期 owner —— 恰恰是最
需要自动化防线的地方；而它的全部逻辑都能靠替身 `webdriver.Edge/Chrome` 离线跑，
根本不需要真开浏览器。

v2.6 刚在里落了个真实修复：驱动已经建好、后续初始化步骤抛错时没人 quit，于是每次
回退都泄漏一个真实浏览器进程（长批次 + `RESTART_BROWSER_EVERY=30` 能攒出 ~20 个
孤儿），当时一行测试都没有。本文件把这些契约钉死：

  * `_cdp_extra_headers` / `_apply_user_agent_override` 的**确切** payload ——
    客户端提示与 UA 不一致本身就是自爆指纹；
  * `_apply_stealth_cdp` 三步里只有 addScript 失败能让"建驱动"这件事失败，
    另两步必须被吞掉（不同版本 driver 对 setExtraHTTPHeaders 的兼容性不一）；
  * Edge / Chrome 原生 / Chrome UC 回退 三条路径的 _discard 回收窗口期；
  * `set_window_position` 抛错不得影响建驱动。

安全：`df.webdriver`、`sys.modules["undetected_chromedriver"]`、`df.random`、
`df.pick_user_agent` 全部由替身接管 —— 任何路径都不会启动真实浏览器进程。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selenium import webdriver  # noqa: E402

from src.browser import driver_factory as df  # noqa: E402
from src.browser.driver_factory import (  # noqa: E402
    _apply_stealth_cdp,
    _apply_user_agent_override,
    _cdp_extra_headers,
    _discard,
    _ua_major_version,
    create_chrome_driver,
    create_edge_driver,
)
from src.browser.driver_factory_stealth import build_stealth_js  # noqa: E402
from src.config import (  # noqa: E402
    HW_PRESETS,
    PAGE_LOAD_TIMEOUT,
    SCREEN_PRESETS,
    TIMEZONE_OFFSET_MIN,
)

EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.2903.112"
)
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 既无 Edg/ 也无 Chrome/：版本号解析必然失败，用于验证 fallback 品牌串
NO_VERSION_UA = "Mozilla/5.0 (Windows NT 10.0; rv:128.0) like Gecko"

EXTRA_HEADERS_CMD = "Network.setExtraHTTPHeaders"
ADD_SCRIPT_CMD = "Page.addScriptToEvaluateOnNewDocument"
UA_OVERRIDE_CMD = "Network.setUserAgentOverride"

SCREEN_W, SCREEN_H, AVAIL_TOP = SCREEN_PRESETS[0]
DEVICE_MEMORY, HW_CONCURRENCY = HW_PRESETS[0]


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
class FakeDriver:
    """记录一切调用的 driver 替身。

    `fail_on` 可放 "page_load_timeout" / "window_position" 或 CDP 命令名，
    对应那一步被调用时就抛错 —— 用来精确制造 v2.6 泄漏修复的那个窗口期。
    """

    def __init__(
        self,
        label: str = "driver",
        fail_on: Any = (),
        quit_error: BaseException | None = None,
    ) -> None:
        self.label = label
        self.options: Any = None
        self.fail_on = set(fail_on)
        self.quit_error = quit_error
        self.cdp_calls: list[tuple[str, dict[str, Any]]] = []
        self.cdp_attempts: list[str] = []
        self.page_load_timeouts: list[float] = []
        self.window_positions: list[tuple[int, int]] = []
        self.quit_calls = 0

    def _boom(self, step: str) -> None:
        if step in self.fail_on:
            raise RuntimeError(f"{self.label}: {step} 失败")

    def set_page_load_timeout(self, seconds: float) -> None:
        self._boom("page_load_timeout")
        self.page_load_timeouts.append(seconds)

    def set_window_position(self, x: int, y: int) -> None:
        self._boom("window_position")
        self.window_positions.append((x, y))

    def execute_cdp_cmd(self, cmd: str, params: dict[str, Any] | None = None) -> dict:
        self.cdp_attempts.append(cmd)
        self._boom(cmd)
        self.cdp_calls.append((cmd, dict(params or {})))
        return {"ok": True}

    def cdp_for(self, cmd: str) -> list[dict[str, Any]]:
        return [p for c, p in self.cdp_calls if c == cmd]

    def quit(self) -> None:
        self.quit_calls += 1
        if self.quit_error is not None:
            raise self.quit_error


class FakeSelenium:
    """替身 `selenium.webdriver` 命名空间：只暴露 driver_factory 用到的四个名字。

    Options 用**真实** selenium 类，这样 `arguments` / `experimental_options`
    的语义与线上一致；Edge/Chrome 只是工厂，最多记录调用，绝不启动进程。
    """

    EdgeOptions = webdriver.EdgeOptions
    ChromeOptions = webdriver.ChromeOptions

    def __init__(self) -> None:
        self.created: list[FakeDriver] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.preset: dict[str, FakeDriver] = {}
        self.errors: dict[str, BaseException] = {}

    def Edge(self, **kwargs: Any) -> FakeDriver:
        return self._build("Edge", kwargs)

    def Chrome(self, **kwargs: Any) -> FakeDriver:
        return self._build("Chrome", kwargs)

    def _build(self, kind: str, kwargs: dict[str, Any]) -> FakeDriver:
        self.calls.append((kind, kwargs))
        error = self.errors.get(kind)
        if error is not None:
            raise error
        driver = self.preset.pop(kind, None) or FakeDriver(label=kind.lower())
        driver.options = kwargs.get("options")
        self.created.append(driver)
        return driver

    def kinds_called(self) -> list[str]:
        return [k for k, _ in self.calls]


class FakeUC:
    """替身 `undetected_chromedriver` 模块（该包是可选依赖，不能假设已安装）。"""

    ChromeOptions = webdriver.ChromeOptions

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.created: list[FakeDriver] = []
        self.driver: FakeDriver | None = None
        self.error: BaseException | None = None

    def Chrome(self, **kwargs: Any) -> FakeDriver:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        driver = self.driver or FakeDriver(label="uc")
        driver.options = kwargs.get("options")
        self.created.append(driver)
        return driver


class UAChooser:
    """替身 `pick_user_agent`：记录调用参数，按浏览器返回固定 UA。"""

    def __init__(self) -> None:
        self.edge_ua = EDGE_UA
        self.chrome_ua = CHROME_UA
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, browser: str = "edge", **kwargs: Any) -> str:
        self.calls.append((browser, kwargs))
        return self.edge_ua if browser == "edge" else self.chrome_ua


class FirstChoiceRandom:
    """把 `random.choice` 变成确定性的（永远取 seq[0]），让指纹可断言。"""

    def choice(self, seq: Any) -> Any:
        return seq[0]


class BlockedRealLaunch:
    """兜底替身：谁要是忘了用 `env` 打桩，这里只会拿到 AssertionError。

    autouse 夹具先于 `env` 执行，所以正常用例随后会把它覆盖掉；
    它的存在只保证一件事 —— 离线套件里绝无可能开出真实浏览器窗口。
    """

    EdgeOptions = webdriver.EdgeOptions
    ChromeOptions = webdriver.ChromeOptions

    def Edge(self, **_kwargs: Any) -> Any:
        raise AssertionError("离线测试不允许创建真实 Edge driver")

    def Chrome(self, **_kwargs: Any) -> Any:
        raise AssertionError("离线测试不允许创建真实 Chrome driver")


class Harness:
    """一次用例所需的全部替身 + 打桩（覆盖 autouse 的兜底替身）。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.selenium = FakeSelenium()
        self.uc = FakeUC()
        self.uas = UAChooser()
        monkeypatch.setattr(df, "webdriver", self.selenium)
        monkeypatch.setattr(df, "random", FirstChoiceRandom())
        monkeypatch.setattr(df, "pick_user_agent", self.uas)
        monkeypatch.setitem(sys.modules, "undetected_chromedriver", self.uc)

    def uc_is_not_installed(self) -> None:
        """让 `import undetected_chromedriver` 本身抛 ModuleNotFoundError。"""
        self._monkeypatch.setitem(sys.modules, "undetected_chromedriver", None)


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(df, "webdriver", BlockedRealLaunch())
    monkeypatch.setitem(sys.modules, "undetected_chromedriver", None)


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(monkeypatch)


def arguments_of(driver: FakeDriver) -> list[str]:
    return list(driver.options.arguments)


# ---------------------------------------------------------------------------
# 1. _cdp_extra_headers
# ---------------------------------------------------------------------------
def test_chrome_browser_gets_google_chrome_brand() -> None:
    headers = _cdp_extra_headers(browser="chrome")
    assert '"Google Chrome";v="131"' in headers["Sec-CH-UA"]
    assert "Microsoft Edge" not in headers["Sec-CH-UA"]


@pytest.mark.parametrize("browser", ["edge", "EDGE", None, "", "firefox", "safari"])
def test_everything_else_gets_edge_brand(browser: Any) -> None:
    """未知浏览器名也必须落到 Edge 品牌串：Sec-CH-UA 与真实浏览器不符就是自爆特征。"""
    headers = _cdp_extra_headers(browser=browser)
    assert '"Microsoft Edge";v="131"' in headers["Sec-CH-UA"]
    assert "Google Chrome" not in headers["Sec-CH-UA"]


def test_default_arg_is_edge() -> None:
    assert _cdp_extra_headers() == _cdp_extra_headers(browser="edge")


def test_accept_language_is_zh_cn_for_both() -> None:
    for browser in ("edge", "chrome"):
        headers = _cdp_extra_headers(browser=browser)
        assert headers["Accept-Language"] == "zh-CN,zh;q=0.9,en;q=0.8"
        assert headers["Sec-CH-UA-Platform"] == '"Windows"'
        assert headers["Upgrade-Insecure-Requests"] == "1"


# ---------------------------------------------------------------------------
# 1b. Sec-CH-UA 的大版本必须跟着本次真实使用的 UA 走（v2.7 修复）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("major", ["129", "130", "131"])
def test_sec_ch_ua_version_follows_the_real_ua(major: str) -> None:
    """此前这里硬编码 v="131"，而同一实例的 Network.setUserAgentOverride
    是按 UA 推导版本的。两个 UA 池各 4 条里有 2 条是 129/130 —— 也就是**一半**
    的实例会出现"请求头说 131、userAgentMetadata 说 129"的自相矛盾，
    而这正是本模块存在的意义所要防的 client-hints 特征。
    """
    ua = EDGE_UA.replace("Edg/131.", f"Edg/{major}.")
    headers = _cdp_extra_headers(browser="edge", ua=ua)
    assert f'"Microsoft Edge";v="{major}"' in headers["Sec-CH-UA"]
    assert f'"Chromium";v="{major}"' in headers["Sec-CH-UA"]


def test_headers_without_ua_keep_the_historical_default() -> None:
    """不传 ua 时仍是 131：这次修的是"两层不一致"，不是顺手改默认指纹。"""
    assert '"Microsoft Edge";v="131"' in _cdp_extra_headers(browser="edge")["Sec-CH-UA"]


def test_ua_major_version_prefers_the_edg_token() -> None:
    """Edge 的 UA 里同时含 Chrome/，必须先试 Edg/，否则读到的是内核版本。"""
    ua = "Mozilla/5.0 ... Chrome/120.0.0.0 Safari/537.36 Edg/130.0.2849.68"
    assert _ua_major_version(ua, browser="edge") == "130"
    assert _ua_major_version(ua, browser="chrome") == "120"
    assert _ua_major_version(None) is None
    assert _ua_major_version("") is None
    assert _ua_major_version(NO_VERSION_UA) is None


def test_stealth_cdp_keeps_both_cdp_layers_on_one_version() -> None:
    """一次实例化里，(a) 请求头与 (c) UA override 必须报同一个浏览器版本。

    这两步原本各算各的，本用例是它们共用一个来源的唯一防线。
    """
    d = FakeDriver()
    _apply_stealth_cdp(
        d,
        ua=EDGE_UA.replace("Edg/131.", "Edg/129."),
        browser="edge",
        screen_w=SCREEN_W,
        screen_h=SCREEN_H,
        avail_top=AVAIL_TOP,
        device_memory=DEVICE_MEMORY,
        hw_concurrency=HW_CONCURRENCY,
    )
    headers = d.cdp_for(EXTRA_HEADERS_CMD)[0]["headers"]
    brands = d.cdp_for(UA_OVERRIDE_CMD)[0]["userAgentMetadata"]["brands"]
    assert '"Microsoft Edge";v="129"' in headers["Sec-CH-UA"]
    assert [(b["brand"], b["version"]) for b in brands] == [
        ("Chromium", "129"),
        ("Microsoft Edge", "129"),
        ("Not_A Brand", "24"),
    ]


# ---------------------------------------------------------------------------
# 2. _apply_user_agent_override
# ---------------------------------------------------------------------------
def _expected_brands(*, brand: str, version: str) -> list[dict[str, str]]:
    return [
        {"brand": "Chromium", "version": version},
        {"brand": brand, "version": version},
        {"brand": "Not_A Brand", "version": "24"},
    ]


def _ua_override_params(driver: FakeDriver) -> dict[str, Any]:
    calls = driver.cdp_for(UA_OVERRIDE_CMD)
    assert len(calls) == 1
    return calls[0]


def test_edge_ua_builds_edge_metadata_payload() -> None:
    d = FakeDriver()
    _apply_user_agent_override(d, EDGE_UA, browser="edge")
    assert _ua_override_params(d) == {
        "userAgent": EDGE_UA,
        "acceptLanguage": "zh-CN,zh;q=0.9",
        "platform": "Windows",
        "userAgentMetadata": {
            "brands": _expected_brands(brand="Microsoft Edge", version="131"),
            "platform": "Windows",
            "platformVersion": "10.0.0",
            "architecture": "x86",
            "model": "",
            "mobile": False,
            "bitness": "64",
            "wow64": True,
        },
    }


def test_chrome_ua_builds_google_chrome_metadata_payload() -> None:
    d = FakeDriver()
    _apply_user_agent_override(d, CHROME_UA, browser="chrome")
    params = _ua_override_params(d)
    assert params["userAgentMetadata"]["brands"] == _expected_brands(
        brand="Google Chrome", version="131"
    )


def test_version_chunk_comes_from_edg_not_chrome() -> None:
    """Edg/<ver> 决定品牌版本，而不是 UA 里的 Chrome/<ver>。"""
    ua = EDGE_UA.replace("Edg/131.0.2903.112", "Edg/120.0.2210.91")
    d = FakeDriver()
    _apply_user_agent_override(d, ua, browser="edge")
    brands = _ua_override_params(d)["userAgentMetadata"]["brands"]
    assert brands == _expected_brands(brand="Microsoft Edge", version="120")


@pytest.mark.parametrize(
    ("browser", "expected_brand"),
    [("edge", "Microsoft Edge"), ("chrome", "Google Chrome")],
)
def test_unparsable_ua_falls_back_to_header_default_brand(
    browser: str, expected_brand: str
) -> None:
    """解析不出版本号时退回 _cdp_extra_headers 的默认品牌串（v=131），不能崩。"""
    d = FakeDriver()
    _apply_user_agent_override(d, NO_VERSION_UA, browser=browser)
    params = _ua_override_params(d)
    assert params["userAgentMetadata"]["brands"] == _expected_brands(
        brand=expected_brand, version="131"
    )


def test_browser_arg_is_case_insensitive() -> None:
    d = FakeDriver()
    _apply_user_agent_override(d, CHROME_UA, browser="CHROME")
    brands = _ua_override_params(d)["userAgentMetadata"]["brands"]
    assert {"brand": "Google Chrome", "version": "131"} in brands


# ---------------------------------------------------------------------------
# 3. _apply_stealth_cdp：三步中谁能杀死建驱动
# ---------------------------------------------------------------------------
def _apply(d: FakeDriver, **kw: Any) -> None:
    args: dict[str, Any] = {
        "ua": EDGE_UA,
        "browser": "edge",
        "screen_w": SCREEN_W,
        "screen_h": SCREEN_H,
        "avail_top": AVAIL_TOP,
        "device_memory": DEVICE_MEMORY,
        "hw_concurrency": HW_CONCURRENCY,
    }
    args.update(kw)
    _apply_stealth_cdp(d, **args)


def test_stealth_cdp_three_calls_in_order_with_stealth_js_source() -> None:
    d = FakeDriver()
    _apply(d)
    assert [cmd for cmd, _ in d.cdp_calls] == [
        EXTRA_HEADERS_CMD,
        ADD_SCRIPT_CMD,
        UA_OVERRIDE_CMD,
    ]
    expected_js = build_stealth_js(
        screen_w=SCREEN_W,
        screen_h=SCREEN_H,
        avail_top=AVAIL_TOP,
        device_memory=DEVICE_MEMORY,
        hw_concurrency=HW_CONCURRENCY,
        timezone_offset_min=TIMEZONE_OFFSET_MIN,
    )
    assert d.cdp_calls[1][1] == {"source": expected_js}
    assert d.cdp_for(EXTRA_HEADERS_CMD)[0]["headers"]["Sec-CH-UA"] == (
        _cdp_extra_headers(browser="edge")["Sec-CH-UA"]
    )


def test_stealth_cdp_swallows_header_and_ua_override_failures() -> None:
    """(a) 与 (c) 失败只是少一层伪装，不该让整个批次拿不到 driver。"""
    d = FakeDriver(fail_on={EXTRA_HEADERS_CMD, UA_OVERRIDE_CMD})
    _apply(d)
    # 三步都确实被尝试过，但只有成功的 (b) 留下记录
    assert d.cdp_attempts == [EXTRA_HEADERS_CMD, ADD_SCRIPT_CMD, UA_OVERRIDE_CMD]
    assert [cmd for cmd, _ in d.cdp_calls] == [ADD_SCRIPT_CMD]


def test_stealth_cdp_add_script_failure_propagates() -> None:
    """唯一能把建驱动判失败的一步：stealth JS 没注入进去，驱动就是裸的。"""
    d = FakeDriver(fail_on={ADD_SCRIPT_CMD})
    with pytest.raises(RuntimeError):
        _apply(d)
    assert d.cdp_attempts == [EXTRA_HEADERS_CMD, ADD_SCRIPT_CMD]
    assert [cmd for cmd, _ in d.cdp_calls] == [EXTRA_HEADERS_CMD]


def test_stealth_cdp_screen_fingerprint_matches_js() -> None:
    d = FakeDriver()
    _apply(d, screen_w=1366, screen_h=768, avail_top=40)
    source = d.cdp_for(ADD_SCRIPT_CMD)[0]["source"]
    assert "var screenW = 1366;" in source
    assert "var screenH = 768;" in source
    assert "hardwareConcurrency" in source


# ---------------------------------------------------------------------------
# 4 / 5. create_edge_driver：启动参数与 UA 来源
# ---------------------------------------------------------------------------
def test_create_edge_driver_options_and_timeout(env: Harness) -> None:
    d = create_edge_driver()
    assert isinstance(d, FakeDriver)
    assert env.selenium.kinds_called() == ["Edge"]
    assert d.page_load_timeouts == [PAGE_LOAD_TIMEOUT]

    args = arguments_of(d)
    assert "--inprivate" in args
    assert "--disable-blink-features=AutomationControlled" in args
    assert f"user-agent={EDGE_UA}" in args
    assert f"--window-size={SCREEN_W},{SCREEN_H}" in args
    assert "--headless=new" not in args

    opts = d.options
    assert opts.experimental_options["excludeSwitches"] == ["enable-automation"]
    assert opts.experimental_options["useAutomationExtension"] is False
    assert opts.experimental_options["prefs"][
        "profile.managed_default_content_settings.notifications"
    ] == 2


def test_create_edge_driver_headless_flag_only_when_requested(env: Harness) -> None:
    d = create_edge_driver(headless=True)
    assert "--headless=new" in arguments_of(d)


def test_create_edge_driver_respects_explicit_user_agent(env: Harness) -> None:
    ua = EDGE_UA.replace("Edg/131.0.2903.112", "Edg/129.0.0.0")
    d = create_edge_driver(ua)
    assert f"user-agent={ua}" in arguments_of(d)
    assert env.uas.calls == []  # 指定了 UA 就不该再去随机


def test_create_edge_driver_without_ua_consults_pick_user_agent(env: Harness) -> None:
    env.uas.edge_ua = "Mozilla/5.0 custom-edge-ua"
    d = create_edge_driver()
    assert env.uas.calls == [("edge", {})]
    assert f"user-agent={env.uas.edge_ua}" in arguments_of(d)


def test_create_edge_driver_applies_edge_flavored_cdp(env: Harness) -> None:
    d = create_edge_driver()
    headers = d.cdp_for(EXTRA_HEADERS_CMD)[0]["headers"]
    assert '"Microsoft Edge";v="131"' in headers["Sec-CH-UA"]
    brands = d.cdp_for(UA_OVERRIDE_CMD)[0]["userAgentMetadata"]["brands"]
    assert {"brand": "Microsoft Edge", "version": "131"} in brands
    assert d.window_positions == [(0, 0)]


# ---------------------------------------------------------------------------
# 6. v2.6 泄漏防线（Edge + Chrome 原生）
# ---------------------------------------------------------------------------
def test_edge_leak_guard_when_page_load_timeout_fails(env: Harness) -> None:
    d = FakeDriver(fail_on={"page_load_timeout"})
    env.selenium.preset["Edge"] = d
    with pytest.raises(RuntimeError):
        create_edge_driver()
    assert d.quit_calls == 1, "初始化失败却不 quit == 泄漏一个真实 Edge 进程"


def test_edge_leak_guard_when_stealth_cdp_fails(env: Harness) -> None:
    d = FakeDriver(fail_on={ADD_SCRIPT_CMD})
    env.selenium.preset["Edge"] = d
    with pytest.raises(RuntimeError):
        create_edge_driver()
    assert d.quit_calls == 1
    assert d.window_positions == [(0, 0)]  # quit 之前确实已经走到过 CDP 阶段


def test_edge_construction_failure_needs_no_cleanup(env: Harness) -> None:
    """驱动根本没建起来时没有东西可 quit，异常照常上抛即可。"""
    env.selenium.errors["Edge"] = RuntimeError("msedgedriver 启动失败")
    with pytest.raises(RuntimeError, match="msedgedriver"):
        create_edge_driver()
    assert env.selenium.created == []


def test_chrome_native_leak_guard_when_page_load_timeout_fails(env: Harness) -> None:
    d = FakeDriver(fail_on={"page_load_timeout"})
    env.selenium.preset["Chrome"] = d
    with pytest.raises(RuntimeError):
        create_chrome_driver()
    assert d.quit_calls == 1


def test_chrome_native_leak_guard_when_stealth_cdp_fails(env: Harness) -> None:
    d = FakeDriver(fail_on={ADD_SCRIPT_CMD})
    env.selenium.preset["Chrome"] = d
    with pytest.raises(RuntimeError):
        create_chrome_driver()
    assert d.quit_calls == 1


def test_chrome_native_leak_guard_on_step3_after_uc_fallback(env: Harness) -> None:
    """UC 回退后，Step 3 原生 Chrome 初始化失败同样要回收（与 Edge 同款窗口期）。"""
    env.uc.error = RuntimeError("uc 不可用")
    native = FakeDriver(label="chrome", fail_on={"page_load_timeout"})
    env.selenium.preset["Chrome"] = native
    with pytest.raises(RuntimeError):
        create_chrome_driver(use_uc=True)
    assert native.quit_calls == 1


# ---------------------------------------------------------------------------
# 7. set_window_position 失败不得影响建驱动
# ---------------------------------------------------------------------------
def test_window_position_failure_is_ignored(env: Harness) -> None:
    d = FakeDriver(fail_on={"window_position"})
    env.selenium.preset["Edge"] = d
    got = create_edge_driver()
    assert got is d
    assert d.quit_calls == 0
    assert d.window_positions == []
    assert [cmd for cmd, _ in d.cdp_calls] == [
        EXTRA_HEADERS_CMD,
        ADD_SCRIPT_CMD,
        UA_OVERRIDE_CMD,
    ]


def test_chrome_native_window_position_failure_is_ignored(env: Harness) -> None:
    """Chrome 原生路径同款：窗口挪不动只是指纹略歪，不能判死整个驱动。"""
    d = FakeDriver(label="chrome", fail_on={"window_position"})
    env.selenium.preset["Chrome"] = d
    assert create_chrome_driver() is d
    assert d.window_positions == []
    assert d.quit_calls == 0


# ---------------------------------------------------------------------------
# 8. create_chrome_driver：原生路径 + UC 路径
# ---------------------------------------------------------------------------
def test_create_chrome_native_options(env: Harness) -> None:
    d = create_chrome_driver()
    assert env.selenium.kinds_called() == ["Chrome"]
    args = arguments_of(d)
    assert "--incognito" in args
    assert "--disable-blink-features=AutomationControlled" in args
    assert f"user-agent={CHROME_UA}" in args
    assert f"--window-size={SCREEN_W},{SCREEN_H}" in args
    assert "--headless=new" not in args
    assert d.options.experimental_options["excludeSwitches"] == ["enable-automation"]
    assert env.uas.calls == [("chrome", {})]  # Chrome 路径必须显式带上 browser 参数
    assert '"Google Chrome";v="131"' in d.cdp_for(EXTRA_HEADERS_CMD)[0]["headers"]["Sec-CH-UA"]


def test_create_chrome_native_headless_and_explicit_ua(env: Harness) -> None:
    d = create_chrome_driver("Mozilla/5.0 pinned-chrome-ua", headless=True)
    args = arguments_of(d)
    assert "--headless=new" in args
    assert "user-agent=Mozilla/5.0 pinned-chrome-ua" in args
    assert env.uas.calls == []


def test_uc_success_returns_uc_driver_and_applies_stealth(env: Harness) -> None:
    d = create_chrome_driver(use_uc=True)
    assert d in env.uc.created
    assert env.uc.calls and "options" in env.uc.calls[0]
    assert env.selenium.calls == []  # UC 成功时不该再碰原生 Chrome
    assert d.page_load_timeouts == [PAGE_LOAD_TIMEOUT]
    assert [cmd for cmd, _ in d.cdp_calls] == [
        EXTRA_HEADERS_CMD,
        ADD_SCRIPT_CMD,
        UA_OVERRIDE_CMD,
    ]
    assert f"--window-size={SCREEN_W},{SCREEN_H}" in arguments_of(d)
    assert "--disable-blink-features=AutomationControlled" in arguments_of(d)


def test_uc_headless_flag_and_prefs(env: Harness) -> None:
    d = create_chrome_driver(use_uc=True, headless=True)
    assert "--headless=new" in arguments_of(d)
    assert d.options.experimental_options["prefs"]["credentials_enable_service"] is False


def test_uc_version_main_parsed_from_ua(env: Harness) -> None:
    create_chrome_driver(use_uc=True)
    assert env.uc.calls[0]["version_main"] == 131


def test_uc_version_main_omitted_when_ua_has_no_chrome_version(env: Harness) -> None:
    env.uas.chrome_ua = NO_VERSION_UA
    create_chrome_driver(use_uc=True)
    assert "version_main" not in env.uc.calls[0]
    # 回退到 CDP 覆写时也不该因为解析不到版本而崩
    assert env.uc.created[0].cdp_for(UA_OVERRIDE_CMD)


def test_uc_version_main_omitted_when_chrome_version_is_not_numeric(env: Harness) -> None:
    """`Chrome/132b` 这类能 split 出来但 int() 炸掉的 UA：必须退化为"不传版本"。"""
    env.uas.chrome_ua = "Mozilla/5.0 (Windows NT 10.0) Chrome/not-a-number Safari/537.36"
    d = create_chrome_driver(use_uc=True)
    assert "version_main" not in env.uc.calls[0]
    assert d.quit_calls == 0


def test_uc_import_failure_falls_back_to_native_chrome(env: Harness) -> None:
    env.uc_is_not_installed()
    d = create_chrome_driver(use_uc=True)
    assert env.selenium.kinds_called() == ["Chrome"]
    assert d in env.selenium.created
    assert d.quit_calls == 0


def test_uc_construction_failure_falls_back_to_native_chrome(env: Harness) -> None:
    env.uc.error = OSError("chromedriver 下载失败")
    d = create_chrome_driver(use_uc=True)
    assert env.uc.calls and env.uc.created == []
    assert env.selenium.kinds_called() == ["Chrome"]
    assert "--incognito" in arguments_of(d)
    assert d in env.selenium.created


def test_uc_failure_after_construction_quits_uc_driver(env: Harness) -> None:
    """v2.6 修复点：UC 已经起了浏览器再抛错，回退前必须先把那个浏览器 quit 掉。

    修复前这里直接 pass，于每回退一次就留下一个真实的孤儿 Chrome 窗口。
    """
    uc_driver = FakeDriver(label="uc", fail_on={"page_load_timeout"})
    env.uc.driver = uc_driver
    d = create_chrome_driver(use_uc=True)

    assert uc_driver.quit_calls == 1, "回退泄漏了已启动的 UC 浏览器"
    assert env.uc.created == [uc_driver]
    assert d in env.selenium.created  # 回退路径正常交付了原生 Chrome
    assert d.quit_calls == 0
    assert env.uas.calls == [("chrome", {})]  # UA 只挑一次，回退复用同一身份


def test_uc_interrupt_after_construction_is_quit_and_reraised(env: Harness) -> None:
    """v2.7：中断落在 `uc.Chrome()` 之后 → 回收浏览器 + 原样上抛，**不再**去开原生 Chrome。

    v2.6 的回收写在 ``except Exception`` 里，而 ``KeyboardInterrupt`` 不是 ``Exception``
    的子类：Ctrl+C 命中这个窗口期时异常直接穿出函数，那个已经启动的 UC 浏览器
    没人 quit —— Edge 与原生 Chrome 两条守护当时用的已经是 ``BaseException``。
    """
    uc_driver = FakeDriver(label="uc")

    def _interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    uc_driver.set_page_load_timeout = _interrupt
    env.uc.driver = uc_driver

    with pytest.raises(KeyboardInterrupt):
        create_chrome_driver(use_uc=True)

    assert uc_driver.quit_calls == 1, "中断时没回收已启动的 UC 浏览器"
    assert env.selenium.created == [], "被中断之后不该再启一个原生 Chrome"


def test_uc_window_position_failure_is_ignored(env: Harness) -> None:
    uc_driver = FakeDriver(label="uc", fail_on={"window_position"})
    env.uc.driver = uc_driver
    d = create_chrome_driver(use_uc=True)
    assert d is uc_driver
    assert uc_driver.quit_calls == 0


# ---------------------------------------------------------------------------
# 9. _discard
# ---------------------------------------------------------------------------
def test_discard_none_is_noop() -> None:
    assert _discard(None) is None


def test_discard_swallows_quit_errors_and_logs_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    d = FakeDriver(quit_error=RuntimeError("connection refused"))
    with caplog.at_level(logging.DEBUG, logger="src.browser.driver_factory"):
        _discard(d)
    assert d.quit_calls == 1
    assert any("回收 driver 失败" in r.message for r in caplog.records), (
        "吞掉异常可以，但必须留下可诊断痕迹"
    )


def test_discard_quits_a_healthy_driver() -> None:
    d = FakeDriver()
    _discard(d)
    assert d.quit_calls == 1


# ---------------------------------------------------------------------------
# 安全网：证明本文件真的开不出浏览器
# ---------------------------------------------------------------------------
def test_missing_stub_fails_instead_of_opening_a_browser() -> None:
    """不带 `env` 的用例只会拿到 AssertionError，而不是一个真实 Edge 窗口。"""
    with pytest.raises(AssertionError, match="不允许创建真实"):
        create_edge_driver()
    with pytest.raises(AssertionError, match="不允许创建真实"):
        create_chrome_driver(use_uc=True)  # uc 不在 sys.modules → 回退原生 → 同样被拦
