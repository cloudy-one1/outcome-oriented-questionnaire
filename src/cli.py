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
import time
from argparse import Namespace
from dataclasses import dataclass
from typing import Any, Callable

from .config import (
    BROWSER_OPTIONS,
    DEFAULT_BROWSER,
    DEFAULT_TOTAL_SUBMISSIONS,
    DEFAULT_USE_UC,
    WEIGHT_CONFIG,
)
from .exceptions import (
    TRANSIENT_DOM_EXCEPTIONS,
    SubmissionAborted,
    format_exc_log,
    raise_non_recoverable,
)
from .models import RunState, SubmitOutcome
from .platforms import unsupported_url_notice

from . import __version__


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
        description=f"问卷星自动填写工具（命令行模式 · v{__version__}）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-u", "--url",
        type=str,
        default=None,
        help="问卷星问卷的完整 URL（必填；出于合规考虑不再内置默认线上问卷）",
    )
    parser.add_argument(
        "--url-file",
        dest="url_file",
        type=str,
        default=None,
        help="v3.0 顺序队列：每行一条 'URL[,份数]'，与 -u 同时给出时 -u 先跑。"
             "共用同一份 --config 与 --history（不做断点续传，--resume 与此项互斥）。",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="v3.0 无头模式（仅调试：问卷星对无头敏感，且智能验证一出现就判本轮失败）",
    )
    parser.add_argument(
        "--profile-dir",
        dest="profile_dir",
        type=str,
        default=None,
        help="v3.0 复用浏览器 profile 目录（登录态/磁盘痕迹）；同一目录不能同时开两个实例",
    )
    parser.add_argument(
        "--max-total-time",
        dest="max_total_time",
        type=_positive_int,
        default=None,
        metavar="SECONDS",
        help="v3.0 整批墙钟上限（秒）：到点按优雅停止收工，下次 --resume 可继续",
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


def _quit_quietly(driver) -> None:
    """关闭浏览器；失败时强杀驱动进程，绝不向上抛。

    调用点全部在清理路径（重建 driver / 周期性重启 / finally 收尾），
    这里抛出会把一次已成功的提交变成批次崩溃。

    v2.6：此前各处手写 ``try: driver.quit() except: pass``。quit() 失败时
    旧 driver 被解绑、再无人引用，浏览器 + 驱动进程就此孤儿化且**无任何痕迹** ——
    而 RESTART_BROWSER_EVERY=30 的长批次里，恰恰是内存堆积严重时 quit() 最容易失败，
    600 份能泄漏 20 个浏览器。现在至少把驱动进程按掉并留一行日志。
    """
    from .exceptions import format_exc_log

    exc: BaseException | None = None
    try:
        driver.quit()
    except TRANSIENT_DOM_EXCEPTIONS as e:
        exc = e
    except Exception as e:  # noqa: BLE001 - 清理路径，只排除不可恢复的终止信号
        raise_non_recoverable(e)
        exc = e
    if exc is None:
        return                       # 正常关闭，什么都不做

    # quit() 失败 → 直接终止 WebDriver 服务进程（浏览器会随之退出）
    killed = False
    try:
        proc = getattr(getattr(driver, "service", None), "process", None)
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
            killed = True
    except Exception:
        killed = False

    print("  " + format_exc_log(
        exc,
        action="driver.quit",
        recovery="已强杀驱动进程" if killed
        else "无法终止驱动进程，浏览器可能残留，需手工清理",
    ))


def _cleanup_browser_state(driver) -> None:
    """清理浏览器状态（Cookie / LocalStorage / SessionStorage）。

    实现已收敛到 ``src.browser.cleanup_browser_state``（CLI/GUI 共用一份）；
    这里保留薄壳以延迟 selenium 依赖导入（parse_args 单测无需 selenium）。
    """
    from .browser import cleanup_browser_state
    cleanup_browser_state(driver)


@dataclass
class RoundOutcome:
    """一份提交的收尾结果，供 ``run_batch(on_round=...)`` 回调消费。

    存在意义：GUI 需要在每轮结束后刷日志 + 进度条，此前它的做法是把整个
    run_batch 循环复刻一份。有了这个回调载荷，GUI 只剩"读字段、更新界面"。
    """

    index: int                 # 绝对第几份（续传时从 resume_start_idx 起算）
    outcome: SubmitOutcome | str
    """三态之一，或引擎内部标记：'error'（本轮抛异常）、'browser_dead'（浏览器重建）、
    'aborted'（用户在轮内请求停止，该份未提交、不计入成功也不计入失败）"""
    message: str = ""          # 一行人类可读摘要（GUI 直接显示）
    state: "RunState | None" = None   # 实时状态引用（计数器已更新）

    @property
    def is_success(self) -> bool:
        return self.outcome == "success"


def _start_run_quietly(
    history_db: Any,
    survey_url: str,
    total_submissions: int,
    browser: str,
    use_uc: bool,
    weight_config: dict | None,
    log: "Callable[[str], None]",
) -> int | None:
    """写 runs 起点；IO 类失败降级为"本次不记录历史"，代码 bug 照旧上抛。

    建议 5.1 / 5.3 的分层：OSError（磁盘/权限/DB 损坏）允许降级继续跑，
    ValueError/KeyError 等数据契约错误必须暴露。
    """
    try:
        return int(history_db.start_run(
            survey_url=survey_url,
            total_submissions=int(total_submissions),
            browser=browser,
            use_uc=bool(use_uc),
            weight_config=weight_config,
        ))
    except OSError as _e:
        log("  " + format_exc_log(
            _e, action="history.start_run", recovery="降级为本次不记录历史，继续运行",
        ))
        return None
    except Exception as _e:  # noqa: BLE001 - 先排除不可恢复的终止信号
        raise_non_recoverable(_e)
        log("  " + format_exc_log(
            _e, action="history.start_run", recovery="降级为本次不记录历史，继续运行",
        ))
        return None


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
    state: RunState | None = None,
    on_round: "Callable[[RoundOutcome], None] | None" = None,
    log: "Callable[[str], None] | None" = None,
    stop_check: "Callable[[], bool] | None" = None,
    error_suffix: str = "",
    headless: bool = False,
    user_data_dir: str | None = None,
    max_total_seconds: float | None = None,
) -> tuple[int, int]:
    """批量执行指定份数的问卷提交 —— **CLI 与 GUI 共用的唯一批次引擎**。

    说明：selenium / 浏览器驱动等重型依赖在函数内部延迟导入，
         保证 parse_args() 的单测无需装 selenium 也能通过。

    V2.6：GUI 的 ``gui/app.py::_run_loop`` 原本把本函数的 11 步逐字复刻了一份
    （170 行）。重复的代价已经用两个真实 bug 付过账：v2.4 的 ``lock`` 漏传要在
    两处各修一次，v2.5 的 ``no_record_text`` / 单轮异常韧性 / 崩溃优先级又是三处
    双份修改。下面三个回调就是让 GUI 得以删掉那份副本的接缝。

    V2.6 参数（GUI 接缝，CLI 全部不传即保持原行为）：
        state      : 外部预先构造好的 RunState（GUI 已把 browser / 续传 /
                     no_record_text / attempts_cap 填进去）。传入时本函数**直接用它**，
                     且不再从 resume_* 推导计数 —— 续传状态由调用方负责。
                   为 None 时按 resume_* 新建（CLI 路径）。
        on_round   : 每完成一份回调一次 ``RoundOutcome``，供 UI 刷日志与进度条。
        log        : 文本输出目的地，默认 ``print``。GUI 传自己的日志面板方法。
        stop_check : 返回 True → 优雅停止。三个生效点（v3.0 起）：
                     ① 每轮开始前（v2.6 起）；② 轮内逐题边界与每题之间的思考停顿
                     （v3.0 起，见 SubmissionAborted —— 该份不提交、不计失败，
                     以 'aborted' 回调后结束批次）；③ 轮间 human_pause 的 abort_check。
                     返回 True → mark_interrupted 并结束批次。
        error_suffix : 追加到 runs.error_message 尾部的备注（GUI 用它标注
                     "GUI · browser=edge uc=False"）。CLI 不传，保持原样。

    v3.0 运行形态参数（都只在 CLI 传）：
        headless        : 无头浏览器。问卷星对无头敏感，且**智能验证一出现就判
                          本轮失败**（无头里没有"抬手就能拉滑块的那个人"）。
        user_data_dir   : 浏览器 profile 目录，跨批次复用登录态与磁盘痕迹。
                          同一目录不能被两个实例同时占用，所以队列是顺序跑的。
        max_total_seconds : 整批墙钟上限。到点按"优雅停止"处理：mark_interrupted
                          后结束，因此下次 --resume 能接着跑（区别于崩溃的 failed）。

    V2 参数：
        history_db : SubmissionHistory 实例或 None；非 None 时会
                     start_run → 逐题 record_answer → finish_run 完整落盘。
    V2.1 参数：
        weight_config : 启用 history 时把当前 WEIGHT_CONFIG 一并持久化到
                        runs.weight_config_json，下次 CLI 调用可用 --resume 恢复。
    V2.4 参数（断点续传 --resume，仅当未传 ``state`` 时生效）：
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
    from selenium.common.exceptions import (  # noqa: F401 - 供下方 except 分支使用
        InvalidSessionIdException,
        NoSuchWindowException,
        WebDriverException,
    )

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
    from .utils import ManualHoldLock, human_pause

    def _log(msg: str) -> None:
        (log or print)(msg)

    # v3.0 平台层：域名对不上已适配平台时，批次一定会失败在"探测题目结构"，
    # 而日志看上去像"DOM 适配出了问题"。提前一行说清楚，不拦停（分享域名变体多）。
    _notice = unsupported_url_notice(survey_url)
    if _notice:
        _log(_notice)

    # 首次创建浏览器实例
    driver_kwargs = {"use_uc": use_uc, "headless": headless,
                     "user_data_dir": user_data_dir}
    driver = create_driver(browser, **driver_kwargs)

    # V2.4 修复：人工介入锁贯穿整轮批次（验证码等待期间 pipeline 依赖它避免误判超时）。
    # 此前 v2.3 重构后本函数从未创建 lock 也未传入 run_one_submission，
    # 导致 CLI 首次提交即 TypeError（P0 级回归）。
    lock = ManualHoldLock()

    # 审查 P2-2：target_success 模式下决定循环上限
    #   - 默认模式：total_submissions 解释为"总尝试次数"，跑满即结束（旧行为）
    #   - target_success=True：total_submissions 是"目标成功数"，循环直到达成
    #     或 max_attempts 用尽（防死循环），submission_index 仍递增以保 history 连续
    # V2.4 续传：resume_done 计入两侧上限（剩余尝试 = 原上限 - 已完成）
    # 审查 P3-4 修正口径：两个分支的上限都是**尝试次数**语义，而上一次批次已经
    # 用掉 done+fail 次尝试。此前只减 done，续传后总尝试数会凭空多出 fail 次
    # （计划 20 份、已完成 12、失败 3 → 旧算法再给 8 次 = 合计 23 次）。
    consumed_attempts = int(resume_done) + int(resume_fail)
    attempts_cap: int
    if target_success:
        base_cap = int(max_attempts) if max_attempts else (int(total_submissions) * 2)
        attempts_cap = max(1, base_cap - consumed_attempts)
        _log(
            f"[模式] 目标成功数 = {total_submissions}，最大尝试次数 = {attempts_cap}"
            + ("（不记录填空文本）" if no_record_text else "")
        )
    else:
        attempts_cap = max(0, int(total_submissions) - consumed_attempts)

    # V2.3 命名整改（建议第四章）：用 RunState 集中管理批次状态,
    # 替代散落的 success/fail/unknown_count/is_interrupted/run_id 等局部变量
    # V2.6：GUI 传入自己的 state 时直接沿用 —— 它的 attempts_cap / run_id /
    # success_count 已由 GUI 的续传对话框算好，不能被这里覆盖。
    caller_owned_state = state is not None
    if state is None:
        state = RunState(attempts_cap=attempts_cap)
    elif not state.attempts_cap:
        state.attempts_cap = attempts_cap

    # ---- V2：start_run（V2.4 续传时改为复用旧 run，不新建） ----
    if caller_owned_state:
        # GUI 路径：run_id / 计数 / resume_start_idx 都已在 state 里，
        # 仅在还没有 run_id 时补一个起点。
        if history_db is not None and state.run_id is None:
            state.run_id = _start_run_quietly(
                history_db, survey_url, int(total_submissions),
                browser, use_uc, weight_config, _log,
            )
        import time as _t3
        state.total_elapsed_start = _t3.perf_counter()
        if history_db is not None and state.run_id is not None:
            if state.resume_start_idx > 1:
                _log(f"[历史] 续传 Run #{state.run_id}（计数沿用上次，不重置）")
            else:
                _log(f"[历史] Run #{state.run_id} 已记录起点")
    elif history_db is not None and resume_run_id is not None:
        state.run_id = int(resume_run_id)
        state.success_count = int(resume_done)          # 绝对计数，finish_run 覆写为累计值
        state.fail_count = int(resume_fail)
        state.resume_start_idx = int(resume_done) + 1   # 进度显示 / submission_index 接续
        import time as _t0
        state.total_elapsed_start = _t0.perf_counter()
        _log(f"[resume] 续传 Run #{state.run_id}：已完成 {resume_done} 份，"
             f"本次继续提交 {attempts_cap} 份")
    elif history_db is not None:
        import time as _t
        state.run_id = _start_run_quietly(
            history_db, survey_url, int(total_submissions),
            browser, use_uc, weight_config, _log,
        )
        state.total_elapsed_start = _t.perf_counter()

    if not state.total_elapsed_start:
        state.total_elapsed_start = time.perf_counter()

    def _emit(res: RoundOutcome) -> None:
        """回调 on_round（若给了）—— 计数器已在 state 上更新完毕。"""
        if on_round is not None:
            try:
                on_round(res)
            except Exception as _cb_e:
                # UI 回调失败绝不能拖垮正在跑的批次
                raise_non_recoverable(_cb_e)
                _log("  " + format_exc_log(
                    _cb_e, action="on_round 回调", recovery="忽略，继续批次",
                    submission_index=res.index,
                ))

    try:
        # 审查 P2-2：循环上限改为 attempts_cap
        # target_success 模式下 success_count == total_submissions 时也跳出
        while state.current_attempt < state.attempts_cap:
            # V2.6：GUI 的"停止"按钮通过 stop_check 轮询生效
            # （CLI 不传该回调，行为不变；Ctrl+C 仍走 KeyboardInterrupt 分支）
            if stop_check is not None and stop_check():
                state.mark_interrupted()
                _log("已停止运行（已成功份数可下次恢复）")
                break

            # v3.0 --max-total-time：到点按优雅停止处理（不是崩溃），因此可续传
            if max_total_seconds is not None and (
                time.perf_counter() - state.total_elapsed_start >= float(max_total_seconds)
            ):
                state.mark_interrupted()
                _log(f"[时限] 已达 --max-total-time={max_total_seconds:.0f}s，"
                     "停止运行（下次 --resume 可继续）")
                break

            # target_success 模式：达成目标成功数即可提前结束
            if target_success and state.success_count >= int(total_submissions):
                _log(f"[达成] 成功数 {state.success_count} 已达目标 "
                     f"{int(total_submissions)}，停止")
                break
            state.advance_attempt()
            # V2.4 续传：displayed_round = resume_start_idx + current_attempt - 1（绝对第几份）
            displayed_idx = state.displayed_round
            if target_success:
                prefix = (f"[尝试{displayed_idx} · "
                          f"成功{state.success_count}/{total_submissions}]")
            else:
                prefix = f"[{displayed_idx}/{total_submissions}]"

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
                    stop_check=stop_check,
                )

            except SubmissionAborted as _ab:
                # 用户在这份问卷答到一半时点了停止：这一份**从未被提交**，
                # 所以既不计成功也不计失败（计失败会把成功率人为压低）。
                # 批次以 interrupted 闭合，下次 --resume 从这一份重来。
                # v3.0：此前该谓词只在轮间生效，答到一半必须等完整份（≈4.5s/题）。
                _log(f"{prefix} ABORT · {_ab.reason}")
                state.mark_interrupted()
                _emit(RoundOutcome(
                    index=displayed_idx, outcome="aborted",
                    message=f"已停止：{_ab.reason}", state=state,
                ))
                break

            except (InvalidSessionIdException, NoSuchWindowException):
                # 浏览器窗口被用户手动关闭，或进程崩溃 —— 重建 driver 后继续
                _log(f"{prefix} BROWSER_DEAD")
                _quit_quietly(driver)
                driver = create_driver(browser, **driver_kwargs)  # 重新创建浏览器
                state.mark_failure()
                _emit(RoundOutcome(
                    index=displayed_idx, outcome="browser_dead",
                    message="浏览器断开，已重建后继续", state=state,
                ))
                continue

            except WebDriverException as e:
                # V2.5：单轮 WebDriver 异常（TimeoutException 等）降级为该份失败，
                # 不再冒泡终止整批。此前第 3/17 轮遇到一次网络慢就会让整个批次
                # 走 mark_crashed → history_status()='failed' → find_resumable_run
                # 拒绝续传，前面已成功的份数被静默丢弃。
                _log(f"{prefix} FAIL")
                _log("  " + format_exc_log(
                    e, action="单次提交", recovery="计该份失败，继续下一份",
                    submission_index=displayed_idx,
                ))
                state.mark_failure()
                _emit(RoundOutcome(
                    index=displayed_idx, outcome="error",
                    message=f"本轮异常: {type(e).__name__}", state=state,
                ))
                _cleanup_browser_state(driver)
                continue

            # --- 统计本轮结果（审查 P1-1：三态判定；V2.3 用 RunState 集中更新） ---
            if outcome == "success":
                state.mark_success()
                _log(f"{prefix} OK")
                _emit(RoundOutcome(index=displayed_idx, outcome="success",
                                   message="提交成功", state=state))
            elif outcome == "unknown":
                # 按钮已点击但效果超时 → 保守计为失败，但单独打 UNKNOWN 便于复盘
                state.mark_unknown()
                _log(f"{prefix} UNKNOWN")
                _emit(RoundOutcome(
                    index=displayed_idx, outcome="unknown",
                    message="提交状态未知（按钮已点击但效果超时）", state=state,
                ))
            else:
                state.mark_failure()
                _log(f"{prefix} FAIL")
                _emit(RoundOutcome(index=displayed_idx, outcome="failed",
                                   message="提交失败", state=state))

            # --- 清理浏览器状态（为下一轮做准备） ---
            _cleanup_browser_state(driver)

            # --- 每 N 轮主动重启浏览器 ---
            # 原因：长时间运行会导致浏览器内存堆积，最终崩溃。
            if state.current_attempt % RESTART_BROWSER_EVERY == 0:
                _log(f"{prefix} RESTART")
                _quit_quietly(driver)
                driver = create_driver(browser, **driver_kwargs)

            # --- 轮次间隔：正态分布 + 5% 概率真的去"看手机/喝水" ---
            # V2.6：传入 stop_check，用户点停止时不必等满整段高斯停顿
            human_pause(
                ROUND_WAIT_MU, ROUND_WAIT_SIGMA,
                ROUND_WAIT_LO, ROUND_WAIT_HI,
                long_pause_prob=ROUND_LONG_PAUSE_PROB,
                long_lo=ROUND_LONG_PAUSE_LO,
                long_hi=ROUND_LONG_PAUSE_HI,
                abort_check=stop_check,
            )

    except KeyboardInterrupt:
        # 用户按下 Ctrl+C → 优雅退出（审查 P1-3：标记 interrupted 而非 finished）
        state.mark_interrupted()
        _log("\n用户中断")

    except Exception as e:
        # V2.4：未捕获异常 → 批次记 failed（此前会被误标 finished 污染成功率）。
        # CLI 保持"崩溃带 traceback"的既有行为（原样上抛）；
        # GUI 走 on_round 之外的 finally 收尾，标记后不再上抛（由调用方决定）。
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
                    error_message=state.history_error_message(suffix=error_suffix),
                )
            except OSError as _e2:
                # 建议 5.1：IO/磁盘异常允许降级；ValueError/KeyError 往上抛
                _log("  " + format_exc_log(
                    _e2, action="history.finish_run",
                    recovery="忽略，统计结果仍已输出",
                    run_id=state.run_id,
                ))
            except Exception as _e2:
                # 建议 5.3：Ctrl+C/SystemExit 必须上抛
                raise_non_recoverable(_e2)
                # 其他异常：统一格式日志
                _log("  " + format_exc_log(
                    _e2, action="history.finish_run",
                    recovery="忽略，统计结果仍已输出",
                    run_id=state.run_id,
                ))

        # UNKNOWN 分项统计日志（便于事后复盘服务端是否真未收到提交）
        if state.unknown_count > 0:
            _log(f"[统计] 其中 {state.unknown_count} 次提交结果未知"
                 f"（按钮已点击但未观察到成功信号），已保守计入失败数。")

        # 无论如何都要关闭浏览器，避免进程残留
        _quit_quietly(driver)

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
    # v3.0：--url-file 也算给了 URL
    if not (args.url and str(args.url).strip()) and not args.url_file:
        print("[error] 必须通过 -u/--url 或 --url-file 指定问卷 URL")
        sys.exit(2)

    SURVEY_URL = (args.url or "").strip()
    TOTAL_SUBMISSIONS = args.count
    BROWSER = args.browser
    USE_UC = args.use_uc

    # 权重配置的加载/校验在 --config 与 --resume 两条路径上都要用
    from .config_io import (
        apply_weight_config,
        load_weight_config,
        validate_weight_config,
    )

    # ---------- V2：--config 加载并热更新权重 ----------
    cfg_meta = None
    if args.config:
        try:
            cfg_dict, cfg_meta = load_weight_config(args.config)
            problems = validate_weight_config(cfg_dict)
            if problems:
                # CLI 是批量入口，一旦跑错代价是真实提交数 —— 硬失败而非警告
                print(f"[config] 校验未通过（{len(problems)} 项），已拒绝运行：")
                for p in problems[:10]:
                    print(f"  - {p}")
                sys.exit(2)
            apply_weight_config(cfg_dict, replace=True)
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
    # V2.6：先收尾"进程被强杀"留下的孤儿 running 行。
    # running 被 find_resumable_run 当作可恢复状态，续传一个早已死掉的批次
    # 等于在页面状态未知的前提下重复提交。
    if history_db is not None:
        try:
            reaped = history_db.reap_stale_runs()
            if reaped:
                print(f"[history] 已把 {reaped} 个未正常收尾的批次改判为 failed")
        except OSError as e:
            print("  " + format_exc_log(
                e, action="history.reap_stale_runs", recovery="忽略，继续运行",
            ))
        except Exception as e:  # noqa: BLE001
            raise_non_recoverable(e)
            print("  " + format_exc_log(
                e, action="history.reap_stale_runs", recovery="忽略，继续运行",
            ))
    # 续传时沿用上次批次的计划份数（用户显式 -n 时以用户为准）；
    # run_batch 把 total_submissions 当作「绝对目标份数」，attempts_cap = 目标 - 已尝试
    # （已尝试 = 上次已成功 + 已失败），
    # 若这里仍传 args.count 的默认值，续传会按默认 17 份重新计划。
    count_explicit = args.count != DEFAULT_TOTAL_SUBMISSIONS
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
                if not count_explicit:
                    TOTAL_SUBMISSIONS = prev_planned
                    print(f"[resume] 沿用上次计划份数 {prev_planned}（显式 -n 可覆盖）")
                # 权重快照随批次持久化，续传时恢复，保证「最后使用的权重就是用户设置的」
                # --config 显式给出时以文件为准
                if not args.config:
                    snap = history_db.deserialize_weight_config(prev)
                    if snap:
                        apply_weight_config(snap, replace=True)
                        print(f"[resume] 已从批次快照恢复权重配置（{len(snap)} 道题）")
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

    # ---------- v3.0：--url-file 顺序队列 ----------
    # 队列是**顺序**跑的，而且刻意不做并发：同时开多个浏览器会直接稀释
    # 「正态分布人类行为」这条立身点（README「明确不做」里记着）。
    targets: list[tuple[str, int]] = []
    if SURVEY_URL:
        targets.append((SURVEY_URL, TOTAL_SUBMISSIONS))
    if args.url_file:
        try:
            with open(args.url_file, "r", encoding="utf-8-sig") as _qf:
                _lines = _qf.read().splitlines()
        except OSError as _qe:
            print(f"[queue] 读取 --url-file 失败: {type(_qe).__name__}: {_qe}")
            sys.exit(2)
        for _ln in _lines:
            _line = _ln.strip()
            if not _line or _line.startswith("#"):
                continue
            _u, _sep, _n = _line.partition(",")
            _u = _u.strip()
            if not _u:
                continue
            _count = TOTAL_SUBMISSIONS
            if _n.strip():
                try:
                    _count = _positive_int(_n.strip())
                except argparse.ArgumentTypeError as _ce:
                    print(f"[queue] 跳过非法行 {_line!r}: {_ce}")
                    continue
            targets.append((_u, _count))
        if not targets:
            print("[error] --url-file 里没有任何有效 URL")
            sys.exit(2)
    if len(targets) > 1 and args.resume:
        print("[error] --resume 只对一份问卷有意义，不能与 --url-file 队列同用")
        sys.exit(2)

    total_success = 0
    total_fail = 0
    for _ti, (_url, _count) in enumerate(targets, 1):
        if len(targets) > 1:
            print(f"\n[{_ti}/{len(targets)}] {_url[:70]}（{_count} 份）")
        success, fail = run_batch(
            _url,
            _count,
            browser=BROWSER,
            use_uc=USE_UC,
            history_db=history_db,
            weight_config=dict(WEIGHT_CONFIG) if WEIGHT_CONFIG else None,
            no_record_text=args.no_record_text,
            target_success=args.target_success,
            max_attempts=args.max_attempts,
            resume_run_id=resume_run_id,
            resume_done=resume_done,
            resume_fail=resume_fail,
            headless=bool(args.headless),
            user_data_dir=args.profile_dir,
            max_total_seconds=args.max_total_time,
        )
        total_success += success
        total_fail += fail
    print(f"运行结束 — 成功 {total_success}, 失败 {total_fail}"
          + (f"（共 {len(targets)} 份问卷）" if len(targets) > 1 else ""))

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
