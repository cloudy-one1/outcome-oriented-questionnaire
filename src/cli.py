"""命令行入口：批量提交循环（v2.0 增强版）。

相比 v1 新增 CLI 参数：
    -c, --config PATH        从 JSON 加载权重配置（config_io.load_weight_config → 热更新）
    --save-config PATH       运行结束后把当前 WEIGHT_CONFIG 保存为 JSON 模板
    -H, --history PATH       指定 SQLite 历史记录库路径（启用 SubmissionHistory）
    --stats                  结束时打印全库统计（总成功率/总提交数等）

创建浏览器驱动 → 循环执行问卷提交 → 打印统计结果。
支持 Ctrl+C 优雅退出。
支持通过参数选择 Edge 或 Chrome（Chrome 可切换 undetected-chromedriver 模式）。

依赖说明：
    - selenium 是运行时强依赖（真正 run 需要），但 import 期不强绑；
      这样 parse_args 的单测能在无 selenium 环境下通过。
    - .browser / .pipeline / .utils 均延迟到 main() 内再导入。
"""

from __future__ import annotations

import argparse
import sys
from argparse import Namespace

from .config import (
    BROWSER_OPTIONS,
    DEFAULT_BROWSER,
    DEFAULT_SURVEY_URL,
    DEFAULT_TOTAL_SUBMISSIONS,
    DEFAULT_USE_UC,
    WEIGHT_CONFIG,
)


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
    """解析命令行参数（v2.0：新增 config / history / stats 参数）。

    参数：
      argv : 模拟的 sys.argv[1:]；为 None 时使用真实 sys.argv[1:]

    返回：
      argparse.Namespace，至少包含：
        url      : 问卷 URL
        count    : 提交份数（正整数）
        browser  : "edge" | "chrome"
        use_uc   : bool（仅 Chrome 生效，是否优先 undetected-chromedriver）
        config   : str | None（加载权重配置 JSON）
        save_config: str | None（保存权重配置 JSON 的路径）
        history  : str | None（history 数据库路径）
        stats    : bool（结束时打印统计）
    """
    parser = argparse.ArgumentParser(
        description="问卷星自动填写工具（命令行模式 · v2.0）",
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
    # ---------- V2 新增参数 ----------
    parser.add_argument(
        "-c", "--config",
        type=str,
        default=None,
        metavar="PATH",
        help="从 JSON 加载权重配置（config_io schema 2.0）→ 热更新到 WEIGHT_CONFIG。"
             " 使用 config_io.save_weight_config 生成的模板文件。",
    )
    parser.add_argument(
        "--save-config",
        type=str,
        default=None,
        metavar="PATH",
        help="运行结束后把当前 WEIGHT_CONFIG（含 --config 热更新后的）保存为 JSON 模板。",
    )
    parser.add_argument(
        "-H", "--history",
        type=str,
        default=None,
        metavar="DB_PATH",
        help="启用历史记录持久化：运行元信息 + 每题答案明细写入 SQLite 文件。"
             " 默认路径：history.db",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        default=False,
        help="结束时打印全库统计（需配合 -H 或 --history 使用，否则空库）。",
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
    history_db: Any | None = None,
    weight_config: dict | None = None,
) -> tuple[int, int]:
    """批量执行指定份数的问卷提交（v2.0：支持 history 逐题记录）。

    说明：selenium / 浏览器驱动等重型依赖在函数内部延迟导入，
         保证 parse_args() 的单测无需装 selenium 也能通过。

    V2 参数：
        history_db : SubmissionHistory 实例或 None；非 None 时会
                     start_run → 逐题 record_answer → finish_run 完整落盘。
    V2.1 参数：
        weight_config : 启用 history 时把当前 WEIGHT_CONFIG 一并持久化到
                        runs.weight_config_json，下次 CLI 调用可用 --resume 恢复。
    """
    # ----- 延迟导入（运行时强依赖） -----
    from selenium.common.exceptions import InvalidSessionIdException  # type: ignore

    from .browser import BROWSER_TYPES, create_driver  # noqa: F401
    from .config import (
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

    # 首次创建浏览器实例
    driver = create_driver(browser, use_uc=use_uc)
    success = 0
    fail = 0

    # ---- V2：start_run ----
    run_id: int | None = None
    total_elapsed_start = 0.0
    if history_db is not None:
        try:
            run_id = history_db.start_run(
                survey_url=survey_url,
                total_submissions=int(total_submissions),
                browser=browser,
                use_uc=bool(use_uc),
                weight_config=weight_config,
            )
            total_elapsed_start = sys.float_info.get("perf_counter", lambda: 0.0)()
            # 跨版本兼容：实际使用 time.perf_counter 统计
            import time as _t
            total_elapsed_start = _t.perf_counter()
        except Exception as _e:
            print(f"[history] start_run 失败，继续不记录: {type(_e).__name__}")
            run_id = None

    try:
        for idx in range(1, total_submissions + 1):
            # 打印进度（不换行，后续打印 OK/FAIL）
            print(f"[{idx}/{total_submissions}]", end=" ", flush=True)

            try:
                # V2：把 history_db + run_id + submission_index 通过关键字传进 pipeline
                ok = run_one_submission(
                    driver,
                    survey_url,
                    history_db=history_db,
                    run_id=run_id,
                    submission_index=idx,
                )

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
        # ---- V2：finish_run（无论成功/失败/中断都要写） ----
        if history_db is not None and run_id is not None:
            try:
                import time as _t2
                total_elapsed = _t2.perf_counter() - total_elapsed_start
                status = "running" if (success + fail == 0) else "finished"
                history_db.finish_run(
                    run_id=run_id,
                    success_count=success,
                    fail_count=fail,
                    total_elapsed_seconds=max(0.0, total_elapsed),
                    status=status,
                    error_message=None if success + fail > 0 else "Ctrl+C 中断",
                )
            except Exception as _e2:
                print(f"[history] finish_run 失败: {type(_e2).__name__}")

        # 无论如何都要关闭浏览器，避免进程残留
        try:
            driver.quit()
        except Exception:
            pass

    return success, fail


def main(argv: list[str] | None = None) -> None:
    """CLI 主入口函数（v2.0：支持 --config / --history / --stats）。

    优先级：命令行参数 > config.py 中的默认值。

    参数：
      argv : 模拟的 sys.argv[1:]；为 None 时使用真实 sys.argv[1:]
    """
    args = parse_args(argv)
    SURVEY_URL = args.url
    TOTAL_SUBMISSIONS = args.count
    BROWSER = args.browser
    USE_UC = args.use_uc

    # ---------- V2：--config 加载并热更新权重 ----------
    cfg_meta = None
    if args.config:
        try:
            from .config_io import load_weight_config, apply_weight_config
            cfg_dict, cfg_meta = load_weight_config(args.config)
            apply_weight_config(cfg_dict)
            n_q = len(cfg_dict)
            print(f"[config] 已加载权重配置：{args.config}（{n_q} 道题）")
            if cfg_meta.get("name"):
                print(f"[config] 预设名称: {cfg_meta['name']}")
        except Exception as e:
            print(f"[config] 加载失败: {type(e).__name__}: {e}")
            sys.exit(2)

    # ---------- V2：--history 初始化 SQLite DB ----------
    history_db = None
    if args.history:
        try:
            from .history import SubmissionHistory
            history_db = SubmissionHistory(args.history)
            print(f"[history] 历史记录已启用: {args.history}")
        except Exception as e:
            print(f"[history] 初始化失败，继续不记录: {type(e).__name__}: {e}")
            history_db = None

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
    if args.config:
        print(f"权重配置 : {args.config}")
    if history_db is not None:
        print(f"历史记录 : {args.history}")
    print("=" * 60)

    success, fail = run_batch(
        SURVEY_URL,
        TOTAL_SUBMISSIONS,
        browser=BROWSER,
        use_uc=USE_UC,
        history_db=history_db,
        weight_config=dict(WEIGHT_CONFIG) if WEIGHT_CONFIG else None,
    )
    print(f"运行结束 — 成功 {success}, 失败 {fail}")

    # ---------- V2：--save-config 保存当前配置模板 ----------
    if args.save_config:
        try:
            from .config_io import save_weight_config
            from .config import WEIGHT_CONFIG as _wc
            meta_out = dict(cfg_meta or {})
            meta_out.setdefault("name", "自动导出模板")
            saved = save_weight_config(args.save_config, dict(_wc), meta_out)
            print(f"[config] 已保存配置模板: {saved}")
        except Exception as e:
            print(f"[config] 保存失败: {type(e).__name__}: {e}")

    # ---------- V2：--stats 打印全库统计 ----------
    if args.stats:
        if history_db is not None:
            try:
                s = history_db.stats_summary()
                print("-" * 60)
                print(f"[history] 累计运行: {s['total_runs']} 次")
                print(f"[history] 累计提交: {s['total_submissions']} 份")
                print(f"[history] 累计成功: {s['total_success']} / 失败: {s['total_fail']}")
                print(f"[history] 累计成功率: {round(s['success_rate'] * 100, 2)}%")
                print("-" * 60)
            except Exception as e:
                print(f"[history] 统计失败: {type(e).__name__}: {e}")
        else:
            print("[history] --stats 需要配合 -H/--history 指定 DB 路径")

    # ---------- V2：关闭 history DB ----------
    if history_db is not None:
        try:
            history_db.close()
        except Exception:
            pass

    # 失败时以非零码退出，方便脚本判断成功/失败
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()
