"""投递分布的在线纠正（v3.2 B 档第 4 条）。

WHY：加权随机只保证**每次抽样的期望分布**，不保证**真正投递成功的那几份**的分布。
失败 / UNKNOWN / 中断会随机吃掉若干份，目标 3:1 的两个选项在 17 份里完全可能落成
6:2 —— 而用户看到的配置还是 3:1。本模块按已成功提交的答案统计实际比例，对目标
权重做小幅指数修正，把落地分布往配置上拉。

默认**关**。它是"改变答题语义"的能力，不是防线，必须显式 `--drift-correct` 才生效；
关着的时候本模块一次都不会被调用到（`adjust` 直接原样返回权重）。

对标 SurveyController 的 `core/questions/distribution.py:116`
（`factor = math.exp(gain * sample_factor * gap)`，因子夹紧 + warmup 爬坡），
**只借公式不取码**（它 GPL-3.0，本仓库 MIT）。与它的关键差别：它统计的是计划与尝试，
我们只统计**提交成功**的那些 —— 计划上的比例从来不需要纠正，落地的才需要。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable

#: 每单位缺口的修正强度。gap=0.06（差 6 个百分点）时 factor=e^(2.5·0.06)≈1.16，
#: 也就是让这一项被抽中的相对概率提高约 16% —— 一次纠正不足以把分布扳平，
#: 但十几份下来会收敛，且不会因某一份失败就把分布掀翻。
GAIN = 2.5

#: 因子上下限。刻意不对称度小（0.75 / 1.35 都是 ±1/3 量级）：单份样本的影响必须有限。
FACTOR_MIN = 0.75
FACTOR_MAX = 1.35

#: 前 N 份完全不纠正、之后线性爬坡到满强度。样本个位数时 gap 基本是噪声，
#: 拿噪声去改权重等于把纠正变成抖动源。
WARMUP_DELIVERIES = 8


class _QStat:
    __slots__ = ("delivered", "counts", "options")

    def __init__(self) -> None:
        self.delivered: int = 0
        self.counts: dict[int, int] = defaultdict(int)
        self.options: int = 0


_enabled = False
_stats: dict[int, _QStat] = {}
_buffer: dict[int, tuple[list[int], int]] = {}


def enable(flag: bool) -> None:
    global _enabled
    _enabled = bool(flag)


def control_enabled() -> bool:
    return _enabled


def start_run() -> None:
    """新批次开始：清空计数与缓冲。开着纠正却不清，上一份问卷的分布会管这一份的。"""
    _stats.clear()
    _buffer.clear()


def buffer_answer(qnum: int, chosen: Iterable[int], total_options: int) -> None:
    """逐题作答成功时先攒着 —— 此刻还不知道这一份最终会不会提交成功。"""
    picks = [int(c) for c in chosen]
    if picks:
        _buffer[int(qnum)] = (picks, max(int(total_options), len(picks)))


def commit_buffer() -> int:
    """整份提交成功：把缓冲计入统计，返回计入的题数。"""
    n = 0
    for qnum, (picks, total) in _buffer.items():
        st = _stats.setdefault(qnum, _QStat())
        st.delivered += 1
        st.options = max(st.options, total)
        for pick in picks:
            st.counts[pick] += 1
        n += 1
    _buffer.clear()
    return n


def discard_buffer() -> int:
    """失败 / UNKNOWN / 中断：丢掉这一份的计数，绝不进统计。"""
    n = len(_buffer)
    _buffer.clear()
    return n


def adjust(qnum: int, weights: list[float]) -> list[float]:
    """按"目标份额 − 实际份额"修正权重。未开启时原样返回。

    ``weights`` 是目标权重（等权配置时调用方给均匀权重）；返回值只用于抽样，
    **不回写** ``WEIGHT_CONFIG`` —— 用户配的那份配置是意图，必须始终可读、可导出。
    """
    if not _enabled or not weights:
        return weights
    st = _stats.get(int(qnum))
    if st is None or st.delivered <= 0:
        return list(weights)
    n = st.delivered
    if n < WARMUP_DELIVERIES:
        return list(weights)          # 前 N 份**完全不纠正**：这时 gap 基本是噪声
    warm = min(1.0, (n - WARMUP_DELIVERIES) / WARMUP_DELIVERIES + 0.25)
    total_w = sum(max(float(w), 0.0) for w in weights)
    if total_w <= 0:
        return list(weights)
    out: list[float] = []
    for i, w in enumerate(weights):
        target = max(float(w), 0.0) / total_w
        actual = st.counts.get(i, 0) / n
        factor = math.exp(GAIN * warm * (target - actual))
        out.append(max(float(w), 0.0) * min(FACTOR_MAX, max(FACTOR_MIN, factor)))
    if sum(out) <= 0:                      # 目标里有 0 权重项被乘出来可能全 0
        return list(weights)
    return out


def drift_report() -> dict[int, dict[str, float]]:
    """给批次结束的报告用：每题最大缺口与实际份数。"""
    out: dict[int, dict[str, float]] = {}
    for qnum, st in sorted(_stats.items()):
        if st.delivered <= 0:
            continue
        worst = 0.0
        for idx, cnt in st.counts.items():
            worst = max(worst, abs(cnt / st.delivered))
        out[qnum] = {"delivered": float(st.delivered), "max_share": worst}
    return out
