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
from typing import Any

from .config import (
    BROWSER_OPTIONS,
    DEFAULT_BROWSER,
    DEFAULT_TOTAL_SUBMISSIONS,
    DEFAULT_USE_UC,
    WEIGHT_CONFIG,
)
from .exceptions import (
    TRANSIENT_DOM_EXCEPTIONS,
    format_exc_log,
    raise_non_recoverable,
)
from .models import RunState


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
        default=None,
        help="问卷星问卷的完整 URL（必填；出于合规考虑不再内置默认线上问卷）",
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
    # ---------- V2.2 审查整改新增参数 ----------
    parser.add_argument(
        "--no-record-text",
        dest="no_record_text",
        action="store_true",
        default=False,
        help="[隐私保护] 不把填空题答案写入 SQLite（text_answer 列写 NULL 占位）。"
             " 避免明文保存姓名/手机/邮箱等敏感内容；DOM 仍会填入实际文本（流程需要）。",
    )
    parser.add_argument(
        "--target-success",
        dest="target_success",
        action="store_true",
        default=False,
        help="[语义] 把 --count 解释为「目标成功提交数」而非「总尝试次数」。"
             " 循环会持续到成功数达标或 max_attempts 用尽。"
             " 默认 False（旧行为：count = 总尝试次数，跑满即结束）。",
    )
    parser.add_argument(
        "--max-attempts",
        dest="max_attempts",
        type=_positive_int,
        default=None,
        metavar="N",
        help="[仅 --target-success 时生效] 最大尝试次数上限，防止死循环。"
             " 默认为 --count 的 2 倍。",
    )
    # ---------- V2.4 新增参数 ----------
    parser.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=False,
        help="[断点续传] 从同 URL 最近一次未完成的批次（interrupted/running，24h 内）"
             "继续提交，复用其 run_id 与计数，submission_index 接续编号。"
             " 需配合 -H/--history 使用。",
    )
    parser.add_argument(
        "--log-file",
        dest="log_file",
        type=str,
        default=None,
        metavar="PATH",
        help="可选：启用 logging 并把运行日志写入指定文件。",
    )
    return parser.parse_args(argv)


def _cleanup_browser_state(driver) -> None:
    """清理浏览器状态（Cookie / LocalStorage / SessionStorage）。

    实现已收敛到 ``src.browser.cleanup_browser_state``（CLI/GUI 共用一份）；
    这里保留薄壳以延迟 selenium 依赖导入（parse_args 单测无需 selenium）。
    """
    from .browser import cleanup_browser_state
    cleanup_browser_state(driver)


def run_batch(
    survey_url: str = "",
    total_submissions: int = DEFAULT_TOTAL_SUBMISSIONS,
    *,
    browser: str = DEFAULT_BROWSER,
    use_uc: bool = DEFAULT_USE_UC,
    history_db: Any | None = None,
    weight_config: dict | None = None,
    no_record_text: bool = False,
    target_success: bool = False,
    max_attempts: int | None = None,
    resume_run_id: int | None = None,
    resume_done: int = 0,
    resume_fail: int = 0,
) -> tuple[int, int]:
    """批量执行指定份数的问卷提交（v2.0：支持 history 逐题记录）。

    说明：selenium / 浏览器驱动等重型依赖在函数内部延迟导入，
         保证 parse_args() 的单测无需装 selenium 也能通过。

    V2 参数：
        history_db : SubmissionHistory 实例或 None；非 None 时会
                     start_run → 逐题 record_answer → finish_run 完整落盘。
    V2.1 参数：
        weight_config : 启用 history 时把当前 WEIGHT_CONFIG 一并持久化到
                        runs.weight_config_json，下次 CLI 调用可用 --resume 恢复
                        （V2.4 起 --resume 已实现，见下）。
    V2.4 参数（断点续传 --resume）：
        resume_run_id : 非 None 时复用该 runs 行（不新建），计数在其上累计。
        resume_done   : 上次已完成的成功份数（success_count 绝对起点；
                        submission_index 从 resume_done+1 接续编号）。
        resume_fail   : 上次已累计的失败份数（fail_count 绝对起点）。
    V2.2 参数（审查整改）：
        no_record_text : True 时填空题答案不会写入 SQLite（隐私保护，避免
                          明文保存用户自定义的姓名/手机/邮箱等敏感内容）。
        target_success : 审查 P2-2 语义厘清 ——
                          False（默认）：``total_submissions`` 解释为
                          "总尝试次数"，跑满 N 次即结束（不论成功失败，旧行为）。
                          True：``total_submissions`` 解释为"目标成功提交数"，
                          循环会持续到 ``success_count == total_submissions`` 为止，
                          并以 ``max_attempts`` 作为最大尝试次数上限防止死循环。
        max_attempts   : 仅当 ``target_success=True`` 时生效；为 None 时
                          默认 ``total_submissions * 2``（最多 2 倍尝试达成目标）。
                          防止极端失败场景下死循环刷接口。

    返回值（审查 P1-1 修复）：
        (success_count, fail_count) ——
        其中 fail_count 包含 "failed" 和 "unknown" 两类，
        "unknown" 会单独打印一行 UNKNOWN 以便事后复盘。
        run_one_submission 返回 "success" / "failed" / "unknown" 三态字符串。
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
    from .interaction import SUBMIT_SUCCESS  # noqa: F401  - 明确"成功"判定
    from .pipeline import run_one_submission
    from .utils import ManualHoldLock, human_pause

    # 首次创建浏览器实例
    driver = create_driver(browser, use_uc=use_uc)

    # V2.4 修复：人工介入锁贯穿整轮批次（验证码等待期间 pipeline 依赖它避免误判超时）。
    # 此前 v2.3 重构后本函数从未创建 lock 也未传入 run_one_submission，
    # 导致 CLI 首次提交即 TypeError（P0 级回归）。
    lock = ManualHoldLock()

    # 审查 P2-2：target_success 模式下决定循环上限
    #   - 默认模式：total_submissions 解释为"总尝试次数"，跑满即结束（旧行为）
    #   - target_success=True：total_submissions 是"目标成功数"，循环直到达成
    #     或 max_attempts 用尽（防死循环），submission_index 仍递增以保 history 连续
    # V2.4 续传：resume_done 计入两侧上限（剩余尝试 = 原上限 - 已完成）
    attempts_cap: int
    if target_success:
        base_cap = int(max_attempts) if max_attempts else (int(total_submissions) * 2)
        attempts_cap = max(1, base_cap - int(resume_done))
        print(
            f"[模式] 目标成功数 = {total_submissions}，最大尝试次数 = {attempts_cap}"
            + (f"（不记录填空文本）" if no_record_text else "")
        )
    else:
        attempts_cap = max(0, int(total_submissions) - int(resume_done))

    # V2.3 命名整改（建议第四章）：用 RunState 集中管理批次状态,
    # 替代散落的 success/fail/unknown_count/is_interrupted/run_id 等局部变量
    state = RunState(attempts_cap=attempts_cap)

    # ---- V2：start_run（V2.4 续传时改为复用旧 run，不新建） ----
    if history_db is not None and resume_run_id is not None:
        state.run_id = int(resume_run_id)
        state.success_count = int(resume_done)          # 绝对计数，finish_run 覆写为累计值
        state.fail_count = int(resume_fail)
        state.resume_start_idx = int(resume_done) + 1   # 进度显示 / submission_index 接续
        import time as _t0
        state.total_elapsed_start = _t0.perf_counter()
        print(f"[resume] 续传 Run #{state.run_id}：已完成 {resume_done} 份，"
              f"本次继续提交 {attempts_cap} 份")
    elif history_db is not None:
        try:
            state.run_id = history_db.start_run(
                survey_url=survey_url,
                total_submissions=int(total_submissions),
                browser=browser,
                use_uc=bool(use_uc),
                weight_config=weight_config,
            )
            # 跨版本兼容：实际使用 time.perf_counter 统计
            import time as _t
            state.total_elapsed_start = _t.perf_counter()
        except OSError as _e:
            # 建议 5.1：把 IO/SQLite 异常和代码 bug 分开——只有磁盘/权限/DB 损坏允许降级
            # （ValueError/KeyError 等数据契约错误应该上抛暴露问题）
            print("  " + format_exc_log(
                _e, action="history.start_run", recovery="降级为本次不记录历史，继续运行",
            ))
            state.run_id = None
        except Exception as _e:
            # 建议 5.3：Ctrl+C/SystemExit 必须上抛
            raise_non_recoverable(_e)
            # 其他异常：仍用降级策略，但统一格式日志
            print("  " + format_exc_log(
                _e, action="history.start_run", recovery="降级为本次不记录历史，继续运行",
            ))
            state.run_id = None

    try:
        # 审查 P2-2：循环上限改为 attempts_cap
        # target_success 模式下 success_count == total_submissions 时也跳出
        while state.current_attempt < state.attempts_cap:
            # target_success 模式：达成目标成功数即可提前结束
            if target_success and state.success_count >= int(total_submissions):
                print(f"[达成] 成功数 {state.success_count} 已达目标 {int(total_submissions)}，停止")
                break
            state.advance_attempt()
            # 打印进度（不换行，后续打印 OK/FAIL/UNKNOWN）
            # V2.4 续传：displayed_round = resume_start_idx + current_attempt - 1（绝对第几份）
            displayed_idx = state.displayed_round
            if target_success:
                print(f"[尝试{displayed_idx} · 成功{state.success_count}/{total_submissions}]",
                      end=" ", flush=True)
            else:
                print(f"[{displayed_idx}/{total_submissions}]", end=" ", flush=True)

            try:
                # V2：把 history_db + run_id + submission_index 通过关键字传进 pipeline
                # 审查 P1-1：run_one_submission 返回 "success" / "failed" / "unknown" 三态
                # 审查 P2-3：no_record_text 透传到 _answer_one_question 屏蔽填空文本落盘
                # V2.4 修复：lock 必传（V2.3 重构后两个入口都漏传 → TypeError 回归）
                outcome = run_one_submission(
                    driver,
                    survey_url,
                    lock,
                    history_db=history_db,
                    run_id=state.run_id,
                    submission_index=displayed_idx,
                    no_record_text=no_record_text,
                )

            except InvalidSessionIdException:
                # 浏览器窗口被用户手动关闭，或进程崩溃
                print("BROWSER_DEAD", end=" ", flush=True)
                try:
                    driver.quit()
                except TRANSIENT_DOM_EXCEPTIONS:
                    pass  # 清理失败不影响后续流程
                except Exception as _e:
                    # 建议 5.3：Ctrl+C/SystemExit 必须上抛
                    raise_non_recoverable(_e)
                    pass
                driver = create_driver(browser, use_uc=use_uc)  # 重新创建浏览器
                state.mark_failure()
                continue

            # --- 统计本轮结果（审查 P1-1：三态判定；V2.3 用 RunState 集中更新） ---
            if outcome == "success":
                state.mark_success()
                print("OK")
            elif outcome == "unknown":
                # 按钮已点击但效果超时 → 保守计为失败，但单独打 UNKNOWN 便于复盘
                state.mark_unknown()
                print("UNKNOWN")
            else:
                state.mark_failure()
                print("FAIL")

            # --- 清理浏览器状态（为下一轮做准备） ---
            _cleanup_browser_state(driver)

            # --- 每 N 轮主动重启浏览器 ---
            # 原因：长时间运行会导致浏览器内存堆积，最终崩溃。
            if state.current_attempt % RESTART_BROWSER_EVERY == 0:
                print("RESTART", end=" ", flush=True)
                try:
                    driver.quit()
                except TRANSIENT_DOM_EXCEPTIONS:
                    pass  # 清理失败不影响后续流程
                except Exception as _e:
                    # 建议 5.3：Ctrl+C/SystemExit 必须上抛
                    raise_non_recoverable(_e)
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
        # 用户按下 Ctrl+C → 优雅退出（审查 P1-3：标记 interrupted 而非 finished）
        state.mark_interrupted()
        print("\n用户中断")

    except Exception as e:
        # V2.4：未捕获异常 → 批次记 failed（此前会被误标 finished 污染成功率）。
        # 标记后原样上抛，保持"CLI 崩溃带 traceback"的既有行为。
        state.mark_crashed(f"{type(e).__name__}: {e}")
        raise

    finally:
        # ---- V2：finish_run（无论成功/失败/中断都要写） ----
        if history_db is not None and state.run_id is not None:
            try:
                import time as _t2
                total_elapsed = _t2.perf_counter() - state.total_elapsed_start
                # V2.3：状态判定下沉到 RunState.history_status / history_error_message
                # 审查 P1-3：Ctrl+C → status='interrupted'（区别于 finished/failed）
                #            历史模块 find_resumable_run 会把 interrupted/running 都视作可恢复
                history_db.finish_run(
                    run_id=state.run_id,
                    success_count=state.success_count,
                    fail_count=state.fail_count,
                    total_elapsed_seconds=max(0.0, total_elapsed),
                    status=state.history_status(),
                    error_message=state.history_error_message(),
                )
            except OSError as _e2:
                # 建议 5.1：IO/磁盘异常允许降级；ValueError/KeyError 往上抛
                print("  " + format_exc_log(
                    _e2, action="history.finish_run", recovery="忽略，统计结果仍已打印到 stdout",
                    run_id=state.run_id,
                ))
            except Exception as _e2:
                # 建议 5.3：Ctrl+C/SystemExit 必须上抛
                raise_non_recoverable(_e2)
                # 其他异常：统一格式日志
                print("  " + format_exc_log(
                    _e2, action="history.finish_run", recovery="忽略，统计结果仍已打印到 stdout",
                    run_id=state.run_id,
                ))

        # UNKNOWN 分项统计日志（便于事后复盘服务端是否真未收到提交）
        if state.unknown_count > 0:
            print(f"[统计] 其中 {state.unknown_count} 次提交结果未知（按钮已点击但未观察到成功信号），"
                  f"已保守计入失败数。")

        # 无论如何都要关闭浏览器，避免进程残留
        try:
            driver.quit()
        except TRANSIENT_DOM_EXCEPTIONS:
            pass  # 清理失败不影响后续流程
        except Exception as _e:
            # 建议 5.3：Ctrl+C/SystemExit 必须上抛
            raise_non_recoverable(_e)
            pass

    return state.success_count, state.fail_count


def main(argv: list[str] | None = None) -> None:
    """CLI 主入口函数（v2.0：支持 --config / --history / --stats；
    V2.2 审查整改：支持 --no-record-text / --target-success / --max-attempts）。

    优先级：命令行参数 > config.py 中的默认值。

    参数：
      argv : 模拟的 sys.argv[1:]；为 None 时使用真实 sys.argv[1:]
    """
    args = parse_args(argv)

    # V2.4：URL 必填（合规——不再内置真实线上问卷作为默认值）
    if not args.url or not str(args.url).strip():
        print("[error] 必须通过 -u/--url 指定问卷 URL")
        sys.exit(2)

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
        except (FileNotFoundError, ValueError, OSError) as e:
            # 建议 5.1：只收窄到预期失败（文件不存在/JSON 结构非法/IO 错误）
            # ValueError 覆盖 JSONDecodeError/结构校验错误；其他异常往上抛
            print(f"[config] 加载失败: {type(e).__name__}: {e}")
            sys.exit(2)
        except Exception as e:
            # 建议 5.3：Ctrl+C/SystemExit 必须上抛；其他异常仍按失败退出
            raise_non_recoverable(e)
            print(f"[config] 加载失败: {type(e).__name__}: {e}")
            sys.exit(2)

    # ---------- V2：--history 初始化 SQLite DB ----------
    history_db = None
    if args.history:
        try:
            from .history import SubmissionHistory
            history_db = SubmissionHistory(args.history)
            print(f"[history] 历史记录已启用: {args.history}")
        except OSError as e:
            # 建议 5.1：磁盘/权限/SQLite 打开失败 → 降级为不记录
            print("  " + format_exc_log(
                e, action="history 初始化", recovery="降级为本次不记录历史，继续运行",
                path=args.history,
            ))
            history_db = None
        except Exception as e:
            # 建议 5.3：Ctrl+C/SystemExit 必须上抛
            raise_non_recoverable(e)
            # 其他异常：仍用降级策略
            print("  " + format_exc_log(
                e, action="history 初始化", recovery="降级为本次不记录历史，继续运行",
                path=args.history,
            ))
            history_db = None

    # ---------- V2.4：可选文件日志（logging） ----------
    if args.log_file:
        from .logging_setup import setup_logging
        setup_logging(args.log_file)
        print(f"[log] 运行日志写入: {args.log_file}")

    # ---------- V2.4：--resume 断点续传（读取侧） ----------
    resume_run_id: int | None = None
    resume_done = 0
    resume_fail = 0
    if args.resume:
        if history_db is None:
            print("[resume] --resume 需要配合 -H/--history 指定 DB 路径")
            sys.exit(2)
        prev = history_db.find_resumable_run(SURVEY_URL)
        if prev is None:
            print("[resume] 未找到 24h 内可恢复的未完成批次，按全新批次开始")
        else:
            prev_done = int(prev["success_count"] or 0)
            prev_planned = int(prev["total_submissions"] or 0)
            if 0 < prev_done < prev_planned:
                resume_run_id = int(prev["id"])
                resume_done = history_db.count_done_submissions(resume_run_id)
                resume_fail = int(prev["fail_count"] or 0)
                print(
                    f"[resume] 恢复 Run #{resume_run_id}（状态 {prev['status']}）："
                    f"已完成 {resume_done}/{prev_planned} 份，从第 {resume_done + 1} 份继续"
                )
            else:
                print("[resume] 上次批次已完成或无有效进度，按全新批次开始")

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
    # V2.2 审查整改：模式提示
    if args.target_success:
        print(f"运行模式 : 目标成功数 {TOTAL_SUBMISSIONS}"
              + (f"，最大尝试 {args.max_attempts or TOTAL_SUBMISSIONS * 2}" ))
    else:
        print(f"运行模式 : 总尝试次数 {TOTAL_SUBMISSIONS}（成功与否都跑满）")
    if args.no_record_text:
        print("隐私保护 : 填空题答案不写入 SQLite（--no-record-text）")
    if resume_run_id is not None:
        print(f"断点续传 : Run #{resume_run_id}（已完成 {resume_done} 份）")
    print("=" * 60)

    success, fail = run_batch(
        SURVEY_URL,
        TOTAL_SUBMISSIONS,
        browser=BROWSER,
        use_uc=USE_UC,
        history_db=history_db,
        weight_config=dict(WEIGHT_CONFIG) if WEIGHT_CONFIG else None,
        no_record_text=args.no_record_text,
        target_success=args.target_success,
        max_attempts=args.max_attempts,
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
        except OSError as e:
            # 建议 5.1：收窄到文件/磁盘 IO 类异常；ValueError/TypeError 等数据契约异常上抛
            print(f"[config] 保存失败: {type(e).__name__}: {e}")
        except Exception as e:
            # 建议 5.3：Ctrl+C/SystemExit 必须上抛
            raise_non_recoverable(e)
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
            except OSError as e:
                # 建议 5.1：磁盘/DB 查询异常降级为打印失败；KeyError/ValueError 等数据契约异常上抛
                print("  " + format_exc_log(e, action="history.stats_summary"))
            except Exception as e:
                # 建议 5.3：Ctrl+C/SystemExit 必须上抛
                raise_non_recoverable(e)
                print("  " + format_exc_log(e, action="history.stats_summary"))
        else:
            print("[history] --stats 需要配合 -H/--history 指定 DB 路径")

    # ---------- V2：关闭 history DB ----------
    if history_db is not None:
        try:
            history_db.close()
        except OSError:
            pass  # 清理：关闭失败不影响退出
        except Exception as _e:
            # 建议 5.3：Ctrl+C/SystemExit 必须上抛
            raise_non_recoverable(_e)
            pass  # 其他清理失败仍然忽略

    # 失败时以非零码退出，方便脚本判断成功/失败
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()
