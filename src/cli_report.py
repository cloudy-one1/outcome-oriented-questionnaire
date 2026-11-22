"""CLI 收尾报告层（v4.5 自 cli.py 三拆而来）。

"批次跑完之后、按开关打印的那几份报告"归这里：实测 Cronbach α（--report-alpha）
与投递分布纠正摘要（--drift-correct）。两者只读历史库 / 统计模块，不碰浏览器，
可在无 selenium 环境下单测。

``src.cli`` 以原名再导出：tests/test_cli_exit_and_reports.py 对
``cli._report_reliability`` 打桩的接缝不变；``cli.distribution`` 打的是
distribution 模块自身的属性，报告层调用的是同一个模块对象，桩照样生效。
"""

from __future__ import annotations

from typing import Any

from . import reliability


def _report_reliability(history_db: Any, weight_config: dict) -> None:
    """批次结束后报**实测** α（v3.2 B3-P0：只测不改，先看现状值不值得做控制）。

    读的是历史库而不是计划表 —— 设计稿 §3 的核心判断：计划上的 α 不需要测，
    落地的才需要。
    """
    if history_db is None:
        print("[信度] 没开历史库（-H / --history），无数据可测 → 跳过")
        return
    runs = history_db.query_runs(limit=1)
    if not runs:
        print("[信度] 历史库里没有批次记录 → 跳过")
        return
    run_id = int(runs[0]["id"])
    dims = reliability.dimensions_from_config(weight_config)
    reverse = reliability.reverse_from_config(weight_config)
    how = "维度取自权重配置里声明的 dimension"
    if not dims:
        dims = reliability.implicit_dimension(history_db, run_id)
        how = "配置没声明 dimension → 按全部量表/单选题兜底分组（不是某个构念的信度）"
    if not dims:
        print(f"[信度] Run #{run_id} 没有可参与 α 的题（需要量表/单选/下拉/矩阵单选）")
        return
    print(f"[信度] Run #{run_id} 实测 Cronbach α —— {how}")
    # v4.2 全面审查 P2-12：默认只统计**明确成功**的提交 —— 这份报告要回答的是
    # "已入库答卷可不可信"，failed / unknown / 无状态的尝试混进来会把 n 撑大、
    # 把"3 份尝试"说成"3 份成功答卷"。口径与排除明细随报告打印（scope_caveat）。
    for line in reliability.format_reports(
        reliability.measure_dimensions(
            history_db, run_id, dims, reverse, status_scope="success"
        )
    ):
        print(line)


def print_drift_report() -> None:
    """批次结束后报投递分布的纠正效果（--drift-correct 开着才有内容）。

    报的是"统计口径下每题最高份额"，用来判断纠正到底有没有把分布拉回来；
    没开纠正时什么都不印 —— 空报告印出来只会多一行噪声。
    """
    from . import distribution

    if not distribution.control_enabled():
        return
    rep = distribution.drift_report()
    if rep:
        worst = max(rep.items(), key=lambda kv: kv[1]["max_share"])
        print(f"[drift] 参与统计 {len(rep)} 题；"
              f"份额最高 Q{worst[0]} = {worst[1]['max_share']:.0%}"
              f"（已投递 {int(worst[1]['delivered'])} 份）")
