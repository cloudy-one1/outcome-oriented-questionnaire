"""Edge 浏览器驱动工厂（Stealth 增强版）。

相比 v1 的改进（显著降低问卷星触发人机验证的概率）：

  反检测指纹层
  ├─ 启动参数：Edge 隐私模式 / 禁用 AutomationControlled / 排除 enable-automation
  ├─ UA 多样性：每次启动从 4 个主流 Edge UA 中随机挑选
  ├─ 屏幕指纹多样性：从 7 套常见分辨率随机挑 + 任务栏高度
  ├─ 硬件指纹多样性：6 组 deviceMemory / hardwareConcurrency 组合随机
  ├─ CDP 注入 Stealth JS（每个新文档执行，且在 page.document_idle 前完成）
  │   ├─ navigator.webdriver 隐藏 + Proxy 防御防止被脚本覆写
  │   ├─ window.chrome.csi/loadTimes/app 返回真实可执行函数
  │   ├─ navigator.plugins / mimeTypes 伪造真实 PDF/NativeClient 插件
  │   ├─ navigator.vendor / platform / languages / onLine / vendorSub / productSub
  │   ├─ Date.getTimezoneOffset() 返回中国 UTC+8
  │   ├─ screen.width/height/availTop/colorDepth/pixelDepth 匹配本次挑选的分辨率
  │   ├─ outerWidth/outerHeight 非零（无头特征消除）
  │   ├─ permissions.query 劫持（notifications/geolocation/midi/clipboard 等）
  │   ├─ Function.prototype.toString 劫持（Selenium 注入的函数会有[native code]检查）
  │   ├─ __defineProperty__ 劫持，阻止问卷星把 webdriver 写回
  │   ├─ 清理 cdc_ / __driver_evaluate / __webdriver_script_fn 等 selenium 属性
  │   ├─ WebGL debugRendererInfo 指纹扰动（vendor/renderer 轻微抖动）
  │   └─ navigator.keyboard / navigator.webkitPersistentStorage 伪造
  └─ CDP Network.setExtraHTTPHeaders
      ├─ 添加 Accept / Sec-CH-UA* / Client Hints 等真实浏览器的请求头
      └─ Accept-Language: zh-CN,zh;q=0.9,en;q=0.8

  稳定性层
  ├─ 启动参数加 --disable-backgrounding-occluded-windows 防止页面被降频
  ├─ 加 --disable-renderer-backgrounding 防止 renderer 进程被后台化
  ├─ 加 --window-size 显式窗口尺寸配合 screen 指纹
  └─ 加 --dns-prefetch-disable 减少 DNS 波动引起的加载异常

关于验证码：以上措施降低触发概率，但问卷星的滑块/图形验证码仍需人工。
人工介入锁（ManualHoldLock）在 verification 模块中确保人工处理时不被误判超时。
"""

from __future__ import annotations

import json
import random
from typing import Any

from selenium import webdriver

from ..config import (
    HW_PRESETS,
    PAGE_LOAD_TIMEOUT,
    SCREEN_PRESETS,
    TIMEZONE_OFFSET_MIN,
)
from .driver_factory_stealth import build_stealth_js  # 见下一个模块

from ..utils import pick_user_agent


def _cdp_extra_headers(*, browser: str = "edge") -> dict[str, str]:
    """通过 CDP 给所有请求追加的 HTTP 头（模拟真实浏览器的客户端提示）。

    参数：
      browser : "edge" | "chrome" — 决定 Sec-CH-UA 品牌字段的内容
    """
    b = (browser or "edge").lower()
    if b == "chrome":
        sec_ch_ua = '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"'
    else:
        sec_ch_ua = '"Chromium";v="131", "Microsoft Edge";v="131", "Not_A Brand";v="24"'
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8,"
                  "application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        # User-Agent Client Hints（与 UA 字符串一致）
        "Sec-CH-UA": sec_ch_ua,
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-CH-UA-Platform-Version": '"10.0.0"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def create_edge_driver(
    user_agent: str | None = None,
    *,
    headless: bool = False,
) -> Any:
    """创建 Stealth 版 Edge 浏览器实例。

    参数：
      user_agent : 若未指定 → 自动从 USER_AGENT_POOL 中随机
      headless   : 是否无头模式（注意：问卷星对无头非常敏感，只建议本地调试用）
    """
    opts = webdriver.EdgeOptions()

    # ==================================================================
    #  Step 1. 挑选本实例的"身份"：UA + 屏幕 + 硬件配置（每次启动不同）
    # ==================================================================
    ua = user_agent or pick_user_agent()
    screen_w, screen_h, avail_top = random.choice(SCREEN_PRESETS)
    device_memory, hw_concurrency = random.choice(HW_PRESETS)

    # ==================================================================
    #  Step 2. 浏览器启动参数
    # ==================================================================
    opts.add_argument("--inprivate")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")

    # ---- 新增稳定/反检测参数 ----
    opts.add_argument("--disable-backgrounding-occluded-windows")  # 不把后台标签页降频
    opts.add_argument("--disable-renderer-backgrounding")          # 不把 renderer 进程后台化
    opts.add_argument("--dns-prefetch-disable")                    # 减少 DNS 预取造成的"请求风暴"
    opts.add_argument("--disable-hang-monitor")                    # 禁用浏览器页面挂起监视器（避免误杀）

    # UA + 窗口尺寸（配合 screen 指纹）
    opts.add_argument(f"user-agent={ua}")
    # 窗口略大于可用区域（模拟真实窗口有标题栏 + 边框）
    opts.add_argument(f"--window-size={screen_w},{screen_h}")

    if headless:
        opts.add_argument("--headless=new")  # Edge 新版无头模式

    # 禁用"Chrome 正受到自动测试软件的控制"提示气泡
    prefs = {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "profile.managed_default_content_settings.notifications": 2,  # 通知禁用
    }
    opts.add_experimental_option("prefs", prefs)

    # ==================================================================
    #  Step 3. 创建驱动实例 + CDP 全局配置
    # ==================================================================
    d = webdriver.Edge(options=opts)
    d.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

    # 移动窗口到屏幕 (0, 0)，保证窗口位置与 availTop 指纹匹配
    try:
        d.set_window_position(0, 0)
    except Exception:
        pass

    # --- CDP (a) ：HTTP 请求头伪装（每次 HTTP 请求都生效） ---
    try:
        d.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": _cdp_extra_headers(browser="edge")})
    except Exception:
        pass  # 某些 Selenium Manager 版本不支持，不影响主流程

    # --- CDP (b) ：每个新文档加载前注入 Stealth JS（比 addScriptToEvaluate 更早） ---
    stealth_js = build_stealth_js(
        screen_w=screen_w,
        screen_h=screen_h,
        avail_top=avail_top,
        device_memory=device_memory,
        hw_concurrency=hw_concurrency,
        timezone_offset_min=TIMEZONE_OFFSET_MIN,
    )
    d.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": stealth_js},
    )

    # --- CDP (c) ：UA 也通过 CDP 再覆写一次（双重保险，兼容 Selenium bug） ---
    try:
        _apply_user_agent_override(d, ua, browser="edge")
    except Exception:
        pass  # 老版本 Selenium 可能不支持 userAgentMetadata

    return d


def _apply_user_agent_override(driver: Any, ua: str, *, browser: str) -> None:
    """通过 CDP 执行 Network.setUserAgentOverride，自动推断 Sec-CH-UA brand。

    参数：
      driver  : webdriver 实例（Edge/Chrome 都可以）
      ua      : User-Agent 字符串（来自 pick_user_agent 或用户指定）
      browser : "edge" | "chrome"
    """
    b = (browser or "edge").lower()
    # 从 UA 中猜测浏览器大版本号：Chrome/<ver> / Edg/<ver>
    version_chunk: str | None = None
    if b == "edge" and "Edg/" in ua:
        version_chunk = ua.split("Edg/")[1].split(".")[0]
    elif "Chrome/" in ua:
        version_chunk = ua.split("Chrome/")[1].split(".")[0]

    if version_chunk:
        if b == "chrome":
            brand_full = (
                f'"Chromium";v="{version_chunk}", '
                f'"Google Chrome";v="{version_chunk}", '
                f'"Not_A Brand";v="24"'
            )
        else:
            brand_full = (
                f'"Chromium";v="{version_chunk}", '
                f'"Microsoft Edge";v="{version_chunk}", '
                f'"Not_A Brand";v="24"'
            )
    else:
        brand_full = _cdp_extra_headers(browser=b)["Sec-CH-UA"]

    driver.execute_cdp_cmd(
        "Network.setUserAgentOverride",
        {
            "userAgent": ua,
            "acceptLanguage": "zh-CN,zh;q=0.9",
            "platform": "Windows",
            "userAgentMetadata": {
                "brands": [
                    {
                        "brand": brand_part.split('";v="')[0].strip('"'),
                        "version": brand_part.split('";v="')[1].rstrip('"'),
                    }
                    for brand_part in [
                        chunk.strip() for chunk in brand_full.split(",")
                    ]
                ],
                "platform": "Windows",
                "platformVersion": "10.0.0",
                "architecture": "x86",
                "model": "",
                "mobile": False,
                "bitness": "64",
                "wow64": True,
            },
        },
    )


def _apply_stealth_cdp(
    driver: Any,
    *,
    ua: str,
    browser: str,
    screen_w: int,
    screen_h: int,
    avail_top: int,
    device_memory: int,
    hw_concurrency: int,
) -> None:
    """浏览器创建后统一应用 Stealth CDP 配置（Edge/Chrome 通用）。"""
    # (a) HTTP 额外头
    try:
        driver.execute_cdp_cmd(
            "Network.setExtraHTTPHeaders",
            {"headers": _cdp_extra_headers(browser=browser)},
        )
    except Exception:
        pass

    # (b) 每个新文档注入 Stealth JS
    stealth_js = build_stealth_js(
        screen_w=screen_w,
        screen_h=screen_h,
        avail_top=avail_top,
        device_memory=device_memory,
        hw_concurrency=hw_concurrency,
        timezone_offset_min=TIMEZONE_OFFSET_MIN,
    )
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": stealth_js},
    )

    # (c) UA + Client Hints 覆写
    try:
        _apply_user_agent_override(driver, ua, browser=browser)
    except Exception:
        pass


def create_chrome_driver(
    user_agent: str | None = None,
    *,
    headless: bool = False,
    use_uc: bool = False,
) -> Any:
    """创建 Stealth 版 Chrome 浏览器实例。

    参数：
      user_agent : 若未指定 → 自动从 Chrome UA 池中随机
      headless   : 是否无头模式（注意：问卷星对无头非常敏感，只建议本地调试用）
      use_uc     : 是否优先使用 undetected-chromedriver（需要已 `pip install undetected-chromedriver`）；
                   若启用但未安装 UC，则自动回退到 Selenium 原生 Chrome

    设计：参考用户提供的 `问卷自动填写脚本2.0（chrome）.py` 的 UC 方案，
    同时保留 Selenium 原生 + Stealth CDP 注入方案作为零依赖默认路径。
    """
    browser = "chrome"

    # ============================================================
    #  Step 1. 挑选本实例的"身份"（每次启动不同）
    # ============================================================
    ua = user_agent or pick_user_agent(browser=browser)
    screen_w, screen_h, avail_top = random.choice(SCREEN_PRESETS)
    device_memory, hw_concurrency = random.choice(HW_PRESETS)

    # ============================================================
    #  Step 2. 优先尝试 undetected-chromedriver（use_uc=True 时）
    # ============================================================
    if use_uc:
        try:
            import undetected_chromedriver as uc  # type: ignore

            uc_opts = uc.ChromeOptions()
            # UC 的参数风格：headless / user-data-dir 等
            if headless:
                uc_opts.add_argument("--headless=new")
            uc_opts.add_argument("--disable-gpu")
            uc_opts.add_argument("--no-sandbox")
            uc_opts.add_argument("--disable-dev-shm-usage")
            uc_opts.add_argument(f"--window-size={screen_w},{screen_h}")
            uc_opts.add_argument(
                "--disable-blink-features=AutomationControlled"
            )
            uc_opts.add_argument("--disable-backgrounding-occluded-windows")
            uc_opts.add_argument("--disable-renderer-backgrounding")
            uc_opts.add_argument("--dns-prefetch-disable")
            uc_opts.add_argument("--disable-hang-monitor")

            # 通知/密码气泡禁用
            prefs = {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
                "profile.managed_default_content_settings.notifications": 2,
            }
            uc_opts.add_experimental_option("prefs", prefs)

            # 尝试从 UA 中提取 Chrome 主版本号，交给 UC 自动匹配 driver
            version_main: int | None = None
            if "Chrome/" in ua:
                try:
                    version_main = int(ua.split("Chrome/")[1].split(".")[0])
                except (ValueError, IndexError):
                    version_main = None

            if version_main is not None:
                d = uc.Chrome(options=uc_opts, version_main=version_main)
            else:
                d = uc.Chrome(options=uc_opts)

            d.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
            try:
                d.set_window_position(0, 0)
            except Exception:
                pass
            # UC 已经做了很多反检测工作，我们再叠一层 Stealth CDP 注入更保险
            _apply_stealth_cdp(
                d,
                ua=ua,
                browser=browser,
                screen_w=screen_w,
                screen_h=screen_h,
                avail_top=avail_top,
                device_memory=device_memory,
                hw_concurrency=hw_concurrency,
            )
            return d
        except Exception:
            # UC 不可用（未安装 / driver 下载失败 / 权限问题） → 回退原生 Selenium
            pass

    # ============================================================
    #  Step 3. 回退路径：Selenium 原生 Chrome + Stealth CDP
    # ============================================================
    opts = webdriver.ChromeOptions()

    # Chrome 隐私模式（对应 Edge 的 --inprivate）
    opts.add_argument("--incognito")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")

    # 稳定性 / 反检测参数（与 Edge 对齐）
    opts.add_argument("--disable-backgrounding-occluded-windows")
    opts.add_argument("--disable-renderer-backgrounding")
    opts.add_argument("--dns-prefetch-disable")
    opts.add_argument("--disable-hang-monitor")

    # UA + 窗口尺寸
    opts.add_argument(f"user-agent={ua}")
    opts.add_argument(f"--window-size={screen_w},{screen_h}")

    if headless:
        opts.add_argument("--headless=new")

    # 禁用气泡
    prefs = {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "profile.managed_default_content_settings.notifications": 2,
    }
    opts.add_experimental_option("prefs", prefs)

    # 创建驱动 + 全局 CDP 配置
    d = webdriver.Chrome(options=opts)
    d.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

    try:
        d.set_window_position(0, 0)
    except Exception:
        pass

    _apply_stealth_cdp(
        d,
        ua=ua,
        browser=browser,
        screen_w=screen_w,
        screen_h=screen_h,
        avail_top=avail_top,
        device_memory=device_memory,
        hw_concurrency=hw_concurrency,
    )
    return d
