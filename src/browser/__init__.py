"""浏览器驱动子包。

支持两种浏览器：
  - Edge   : 基于 Selenium 原生 webdriver.Edge + Stealth CDP 注入（默认）
  - Chrome : 两套方案：
                ① Selenium 原生 webdriver.Chrome + Stealth CDP（零依赖默认）
                ② undetected-chromedriver（可选，通过 use_uc=True 启用）

使用方式：
    from src.browser import create_driver, create_edge_driver, create_chrome_driver

    driver = create_driver(browser="edge")                # 与 create_edge_driver() 等价
    driver = create_driver(browser="chrome")              # Selenium 原生 Chrome
    driver = create_driver(browser="chrome", use_uc=True) # 优先 undetected-chromedriver
"""

from __future__ import annotations

from typing import Any

from .driver_factory import create_chrome_driver, create_edge_driver

__all__ = [
    "create_driver",
    "create_edge_driver",
    "create_chrome_driver",
    "cleanup_browser_state",
    "BROWSER_TYPES",
]

# 支持的浏览器类型字符串（供 CLI/GUI 校验入参）
BROWSER_TYPES = ("edge", "chrome")


def cleanup_browser_state(driver: Any) -> None:
    """清理浏览器状态（Cookie / LocalStorage / SessionStorage），为下一轮提交做准备。

    CLI（run_batch）与 GUI（_run_loop）共用；清理失败不影响后续流程。
    """
    from ..exceptions import TRANSIENT_DOM_EXCEPTIONS, raise_non_recoverable

    try:
        driver.delete_all_cookies()
        driver.execute_script("window.localStorage.clear();")
        driver.execute_script("window.sessionStorage.clear();")
    except TRANSIENT_DOM_EXCEPTIONS:
        # Cookie/Storage 清理是"最好情况"优化，DOM/会话异常不影响答题
        pass
    except Exception as _e:
        # Ctrl+C/SystemExit 必须上抛；其他清理失败仍然忽略
        raise_non_recoverable(_e)
        pass


def create_driver(
    browser: str = "edge",
    *,
    user_agent: str | None = None,
    headless: bool = False,
    use_uc: bool = False,
) -> Any:
    """通用浏览器驱动工厂。

    参数：
      browser    : "edge" | "chrome"（大小写不敏感），默认 edge
      user_agent : 可选，自定义 UA；未指定时从对应浏览器 UA 池随机
      headless   : 是否无头模式（问卷星敏感，仅建议调试）
      use_uc     : 仅 Chrome 生效，是否优先用 undetected-chromedriver

    返回：
      selenium.webdriver 实例（或 uc.Chrome 实例，行为一致）

    异常：
      ValueError : 当 browser 不是 edge/chrome 时抛出
    """
    b = (browser or "edge").lower()
    if b == "edge":
        return create_edge_driver(user_agent=user_agent, headless=headless)
    if b == "chrome":
        return create_chrome_driver(
            user_agent=user_agent,
            headless=headless,
            use_uc=use_uc,
        )
    raise ValueError(
        f"不支持的浏览器类型：{browser!r}，可选值：{BROWSER_TYPES}"
    )
