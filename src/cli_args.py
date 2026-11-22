"""CLI 参数层：argparse 定义、自定义类型校验与预约等待（v4.5 自 cli.py 三拆而来）。

拆分动机（v4.5 全面审查）：cli.py 一个文件装着参数解析、批次编排、统计报告三件事
（约 1,450 行，main / _run_batch_inner 各约 400 行）。本模块只管"参数从哪来"：
argparse 参数表、类型校验（``_positive_int`` / ``_browser_type`` / ``_start_at``）
与预约等待（``_sleep_until``）。批次编排仍在 :mod:`src.cli`（run_batch / main），
收尾报告在 :mod:`src.cli_report`。

依赖纪律：本模块不得导入 selenium / 浏览器相关内容 —— ``parse_args`` 的单测
（tests/test_cli.py）在无 selenium 环境下也要能跑。

``src.cli`` 以原名再导出这里的核心符号：tests 对 ``cli._sleep_until`` 打桩、
对 ``cli.parse_args`` 直调的既有接缝不因搬家而变。
"""

from __future__ import annotations

import argparse
import time
from argparse import Namespace
from datetime import datetime, timedelta
from typing import Callable

from . import __version__
from .config import (
    BROWSER_OPTIONS,
    DEFAULT_BROWSER,
    DEFAULT_TOTAL_SUBMISSIONS,
    DEFAULT_USE_UC,
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


# 预约开跑接受的形态：带日期的三种 + 只给时刻的两种（"T" 分隔符归一化后同一路径）。
# 刻意不做"猜用户想说的是哪天"之外的宽容（相对量词、时区、闰秒一类），理由见 _start_at。
_START_AT_FORMATS: tuple[tuple[str, bool], ...] = (
    ("%Y-%m-%d %H:%M:%S", True),
    ("%Y-%m-%d %H:%M", True),
    ("%Y-%m-%d", True),
    ("%H:%M:%S", False),
    ("%H:%M", False),
)


def _start_at(value: str, *, now: Callable[[], datetime] = datetime.now) -> datetime:
    """把 ``--start-at`` 读成一个时间点（argparse 自定义类型，不合法即 ArgumentTypeError）。

    带日期的写法（``2030-01-01 08:00``）已经过去了就是写错了 → 直接拒绝：预约一个
    过去的时间，症状是"批次立刻开跑"，而用户以为自己在等 —— 与本仓库"宁可少做，
    不做静默不对"的取向冲突。只给时刻（``08:00``）说的是"今天这个点"，点已经过了
    则是明天的同一时刻，这是这种写法的通常意思，所以往后滚一天而不是报错。

    这里**只**保证"不早于 T"：不做时钟同步、不抢开跑瞬间、不预留准备时间。
    """
    text = (value or "").strip().replace("T", " ")
    if not text:
        raise argparse.ArgumentTypeError("--start-at 不能是空串")
    for fmt, has_date in _START_AT_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if not has_date:
            # 只给时刻：strptime 会把日期填成 1900-01-01，不换成今天的话任何时刻都
            # "已经过去"。语义是"今天这个点"，点过了就是明天（不猜"下一个工作日"之类）
            today = now().replace(hour=0, minute=0, second=0, microsecond=0)
            parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
            if parsed < now():
                parsed = parsed + timedelta(days=1)
        # 此刻已经过去（带日期的写法不会自己往后滚）→ 这不是"等 0 秒"，是写错了
        if parsed < now():
            raise argparse.ArgumentTypeError(
                f"--start-at 指定的时间已经过去：{value!r}（现在 {now():%Y-%m-%d %H:%M:%S}）。"
                "只写时刻（HH:MM）= 今天或明天这个点，带日期的写法不会自己往后滚。"
            )
        return parsed
    raise argparse.ArgumentTypeError(
        f"--start-at 看不懂的时间：{value!r}，"
        "可用 'YYYY-MM-DD HH:MM[:SS]' / 'YYYY-MM-DDTHH:MM' / 'HH:MM[:SS]'"
    )


_START_AT_SLICE: float = 1.0     # 等待切片：Ctrl+C / 停止信号最迟 1s 后能被看到
_START_AT_PROGRESS_EVERY: float = 60.0   # 进度行节奏：每秒一行会把日志埋掉


def _sleep_until(
    start_at: datetime,
    *,
    now: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> None:
    """睡到 ``start_at``（只保证不早于它，到点后的第一件事才是开浏览器）。

    分片睡（``_START_AT_SLICE``）而不是 ``sleep(整个剩余)``：预约动辄几小时，
    一发的 ``time.sleep`` 期间 Ctrl+C 只能等这一觉睡完才生效 —— 而"到点之前先停掉"
    正是等待期唯一会被做的事。

    等待结束后才返回给调用方去建浏览器，所以 ``--max-total-time`` 天然从 T 起算
    （那条上限的计时在 ``run_batch`` 里开始），不必为此改任何计数。
    """
    remaining = (start_at - now()).total_seconds()
    if remaining <= 0:      # 校验到开跑之间隔了几毫秒，到点即走
        return
    log(f"[预约] 不早于 {start_at:%Y-%m-%d %H:%M:%S} 开跑（还要等 {remaining:.0f}s；"
        "这期间不会启动浏览器，也不会访问问卷地址）")
    waited = 0.0
    next_progress = _START_AT_PROGRESS_EVERY
    while True:
        remaining = (start_at - now()).total_seconds()
        if remaining <= 0:
            break
        slice_ = min(_START_AT_SLICE, remaining)
        sleep(slice_)
        waited += slice_
        if waited >= next_progress:
            log(f"[预约] 还有 {remaining:.0f}s 开跑（已等 {waited:.0f}s）")
            next_progress += _START_AT_PROGRESS_EVERY
    log(f"[预约] 到点（{start_at:%H:%M:%S}），开始提交")


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
        help="v3.0 整批墙钟上限（秒）：到点按优雅停止收工，下次 --resume 可继续。"
             " 与 --start-at 同给时从预约时刻起算（不含等待）",
    )
    parser.add_argument(
        "-n", "--count",
        type=_positive_int,
        # v4.2：default 用 None 哨兵而不是默认份数 —— 此前 `count_explicit` 靠
        # "值不等于默认值"判断用户有没有显式传 -n，显式 `-n 17`（恰好等于默认）
        # 会被当成没传，续传时计划份数被上次批次覆盖。
        default=None,
        help=f"目标提交份数（必须 >= 1；不传按 {DEFAULT_TOTAL_SUBMISSIONS} 份）",
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
        "--drift-correct",
        dest="drift_correct",
        action="store_true",
        default=False,
        help="[v3.2] 按**已提交成功**的实际比例小幅纠正加权采样：目标 3:1 而落地被"
             " 失败 / UNKNOWN 拉歪时往配置上拉回来。默认关 —— 它改变答题结果，不是"
             " 防线；因子夹在 ±1/3、前 8 份完全不纠正。",
    )
    parser.add_argument(
        "--report-alpha",
        dest="report_alpha",
        action="store_true",
        default=False,
        help="[v3.2] 批次结束后按维度打印**实测** Cronbach α（读历史库，不改任何"
             " 作答行为）。维度取权重配置里的 dimension；未声明时按全部量表/单选题"
             " 兜底分组，并在输出里明说那是兜底组、不能当某个构念的信度引用。",
    )
    parser.add_argument(
        "--alpha-target",
        dest="alpha_target",
        metavar="0.60-0.95",
        type=float,
        default=None,
        help="[v3.2] 按 Cronbach α 控制整批答卷的量表结构：先按权重把每道题的选项"
             " 配额精确摊到总份数上，再用潜变量 + 秩映射兑现它。必须在权重配置里"
             " 用 dimension 显式声明哪些题属于同一构念（未声明就不建计划并说明原因）。"
             " 目标值硬夹在 0.60~0.95；边际配额优先，α 不达标只告警不返工。",
    )
    parser.add_argument(
        "--replay-file",
        dest="replay_file",
        metavar="PATH",
        default=None,
        help="[v3.2] 用一份真实答卷表（CSV，或装了 openpyxl 时的 .xlsx）逐份回放："
             " 表里有的题按表答，认不到列 / 解析不出的格子照旧随机。"
             " 第 N 份用第 N 行，只有提交成功才推进队列。",
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
        "--quota",
        dest="quota",
        action="store_true",
        default=False,
        help="[v4.2] 配额调度：单选/下拉**恰好**按配置比例兑现到整批的成功份数上"
             "（100 份 → 10/30/50/10 各恰好；每份生成时只在未满额的选项里按权重抽）。"
             " failed/UNKNOWN 不占配额，重投继续填未满的格 —— 因此 --count 被解释为"
             " 目标成功份数（隐含 --target-success），跑到达成或尝试上限。"
             " 多选/矩阵/排序/量表不走配额（语义见 src/quota.py）。默认关 —— 它改变"
             " 作答结果；与 --drift-correct、--alpha-target 互斥。",
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
    # ---------- v3.1 新增参数 ----------
    parser.add_argument(
        "--rescue-gaps",
        dest="rescue_gaps",
        action="store_true",
        default=False,
        help="[补漏轮] 提交前完整度自检拦下必答缺口题时，不直接判失败：把那道题滚进"
             " 视野、等在场的人工在浏览器窗口里补答（最长 300s），补齐了才点提交。"
             " 默认关（判失败、不点提交）。无头模式下不等待，与默认一致。",
    )
    parser.add_argument(
        "--manual-submit",
        dest="manual_submit",
        action="store_true",
        default=False,
        help="[人工提交] 答完之后不自己点提交：把提交按钮滚进视野并停住，"
             " 由在场的**人**核对（要改的直接改）后自己点。成功与否仍走同一套三态判定；"
             " 等到超时没人点算这一份失败（它没有交出去，平台上不留记录）。"
             " 与 --headless 互斥（无头窗口里没有可点的人）。",
    )
    parser.add_argument(
        "--fallback-locate",
        dest="fallback_locate",
        action="store_true",
        default=False,
        help="[降级定位] 主选择器失配（平台改版）时，用题干/属性锚点重新定位元素"
             " 再试一次（单选/多选/单框填空/选项自带填空）。默认关 —— 失配即本题"
             " 计失败，与历史行为一致。开了也只是多一次尝试，定位不到照旧回执失败。",
    )
    parser.add_argument(
        "--start-at",
        dest="start_at",
        type=_start_at,
        default=None,
        metavar="'YYYY-MM-DD HH:MM[:SS]' | 'HH:MM'",
        help="[预约开跑] 不早于指定时刻开跑（等待期间不启动浏览器、不访问问卷地址）。"
             " 只写 HH:MM 表示今天这个点，已经过了就顺延到明天；带日期而又已经过去的一律"
             " 拒绝（退出码 2）。只承诺 '不早于 T' —— 不做时钟同步，也不为抢开跑瞬间提前"
             " 加载页面。--max-total-time 从 T 起算。",
    )
    return parser.parse_args(argv)
