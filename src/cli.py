"""命令行入口：批量提交循环。

创建浏览器驱动 → 循环执行问卷提交 → 打印统计结果。
支持 Ctrl+C 优雅退出。
支持通过参数选择 Edge 或 Chrome（Chrome 可切换 undetected-chromedriver 模式）。
"""

from __future__ import annotations

import argparse
import sys
from argparse import Namespace

from selenium.common.exceptions import InvalidSessionIdException

from .browser import BROWSER_TYPES, create_driver
from .config import (
    BROWSER_OPTIONS,
    DEFAULT_BROWSER,
    DEFAULT_SURVEY_URL,
    DEFAULT_TOTAL_SUBMISSIONS,
    DEFAULT_USE_UC,
    RESTART_BROWSER_EVERY,
    ROUND_LONG_PAUSE_HI,
    ROUND_LONG_PAUSE_LO,
    ROUND_LONG_PAUSE_PROB,
    ROUND_WAIT_HI,
    ROUND_WAIT_LO,
    ROUND_WAIT_MU,
    ROUND_WAIT_SIGMA,
)
from .pipeline import run_one_submission
from .utils import human_pause


def _positive_int(value: str) -> int:
    """argparse 自定义类型校验：必须是 >= 1 的整数。"""
    try:
        ivalue = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"必须是整数，收到：{value!r}") from exc
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"提交份数必须 >= 1，收到：{ivalue}")
    return ivalue


def _browser_type(value: str) -> str:
    """argparse 自定义类型校验：必须是允许的浏览器类型。"""
    b = (value or "").strip().lower()
    if b not in BROWSER_OPTIONS:
        raise argparse.ArgumentTypeError(
            f"不支持的浏览器：{value!r}，可选值：{', '.join(BROWSER_OPTIONS)}"
        )
    return b


def parse_args(argv: list[str] | None = None) -> Namespace:
    """解析命令行参数。

    参数：
      argv : 模拟的 sys.argv[1:]；为 None 时使用真实 sys.argv[1:]

    返回：
      argparse.Namespace，至少包含：
        url      : 问卷 URL
        count    : 提交份数（正整数）
        browser  : "edge" | "chrome"
        use_uc   : bool（仅 Chrome 生效，是否优先 undetected-chromedriver）
    """
    parser = argparse.ArgumentParser(
        description="问卷星自动填写工具（命令行模式）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-u", "--url",
        type=str,
        default=DEFAULT_SURVEY_URL,
        help="问卷星问卷的完整 URL",
    )
    parser.add_argument(
        "-n", "--count",
        type=_positive_int,
        default=DEFAULT_TOTAL_SUBMISSIONS,
        help="目标提交份数（必须 >= 1）",
    )
    parser.add_argument(
        "-b", "--browser",
        type=_browser_type,
        default=DEFAULT_BROWSER,
        help=f"使用的浏览器（可选：{', '.join(BROWSER_OPTIONS)}）",
    )
    parser.add_argument(
        "--uc",
        dest="use_uc",
        action="store_true",
        default=DEFAULT_USE_UC,
        help="[仅 Chrome] 优先使用 undetected-chromedriver（需先 pip install undetected-chromedriver）。"
             " 未安装或启动失败时会自动回退到 Selenium 原生 Chrome。",
    )
    return parser.parse_args(argv)


def _cleanup_browser_state(driver) -> None:
    """清理浏览器状态（Cookie / LocalStorage / SessionStorage）。"""
    try:
        driver.delete_all_cookies()
        driver.execute_script("window.localStorage.clear();")
        driver.execute_script("window.sessionStorage.clear();")
    except Exception:
        pass  # 清理失败不影响后续流程


def run_batch(
    survey_url: str = DEFAULT_SURVEY_URL,
    total_submissions: int = DEFAULT_TOTAL_SUBMISSIONS,
    *,
    browser: str = DEFAULT_BROWSER,
    use_uc: bool = DEFAULT_USE_UC,
) -> tuple[int, int]:
    """批量执行指定份数的问卷提交。

    参数：
      survey_url         : 问卷星问卷的完整 URL
      total_submissions  : 目标提交份数
      browser            : "edge" | "chrome"
      use_uc             : 仅 Chrome 生效，是否优先使用 undetected-chromedriver

    返回：
      (success_count, fail_count) 元组
    """
    # 首次创建浏览器实例
    driver = create_driver(browser, use_uc=use_uc)
    success = 0
    fail = 0

    try:
        for idx in range(1, total_submissions + 1):
            # 打印进度（不换行，后续打印 OK/FAIL）
            print(f"[{idx}/{total_submissions}]", end=" ", flush=True)

            try:
                ok = run_one_submission(driver, survey_url)

            except InvalidSessionIdException:
                # 浏览器窗口被用户手动关闭，或进程崩溃
                print("BROWSER_DEAD", end=" ", flush=True)
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = create_driver(browser, use_uc=use_uc)  # 重新创建浏览器
                fail += 1
                continue

            # --- 统计本轮结果 ---
            if ok:
                success += 1
                print("OK")
            else:
                fail += 1
                print("FAIL")

            # --- 清理浏览器状态（为下一轮做准备） ---
            _cleanup_browser_state(driver)

            # --- 每 N 轮主动重启浏览器 ---
            # 原因：长时间运行会导致浏览器内存堆积，最终崩溃。
            if idx % RESTART_BROWSER_EVERY == 0:
                print("RESTART", end=" ", flush=True)
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = create_driver(browser, use_uc=use_uc)

            # --- 轮次间隔：正态分布 + 5% 概率真的去"看手机/喝水" ---
            human_pause(
                ROUND_WAIT_MU, ROUND_WAIT_SIGMA,
                ROUND_WAIT_LO, ROUND_WAIT_HI,
                long_pause_prob=ROUND_LONG_PAUSE_PROB,
                long_lo=ROUND_LONG_PAUSE_LO,
                long_hi=ROUND_LONG_PAUSE_HI,
            )

    except KeyboardInterrupt:
        # 用户按下 Ctrl+C → 优雅退出
        print("\n用户中断")

    finally:
        # 无论如何都要关闭浏览器，避免进程残留
        try:
            driver.quit()
        except Exception:
            pass

    return success, fail


def main(argv: list[str] | None = None) -> None:
    """CLI 主入口函数。

    优先级：命令行参数 > config.py 中的默认值。

    参数：
      argv : 模拟的 sys.argv[1:]；为 None 时使用真实 sys.argv[1:]
    """
    args = parse_args(argv)
    SURVEY_URL = args.url
    TOTAL_SUBMISSIONS = args.count
    BROWSER = args.browser
    USE_UC = args.use_uc

    print("=" * 60)
    print(f"问卷 URL : {SURVEY_URL}")
    print(f"提交份数 : {TOTAL_SUBMISSIONS}")
    print(f"浏览器   : {BROWSER.upper()}", end="")
    if BROWSER == "chrome":
        print(f"{' (undetected-chromedriver 优先)' if USE_UC else ' (Selenium 原生)'}")
    else:
        if USE_UC:
            print(" (注：--uc 仅对 Chrome 生效，已忽略)")
        else:
            print()
    print("=" * 60)

    success, fail = run_batch(
        SURVEY_URL,
        TOTAL_SUBMISSIONS,
        browser=BROWSER,
        use_uc=USE_UC,
    )
    print(f"运行结束 — 成功 {success}, 失败 {fail}")
    # 失败时以非零码退出，方便脚本判断成功/失败
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()
