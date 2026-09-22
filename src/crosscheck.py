"""结构对拍（v3.1）：把我们的探测结果与平台自报的题目结构放在一起比。

为什么要有这一步：``detect_questions`` 是从控件形状反推结构的，而它**没法发现自己
判错了** —— 权重锚定的 ``question_signature`` 算签名用的就是探测结果，探测把矩阵
量表认成 ``scale`` 时签名跟着错，锚点比对一路放行；逐题作答又照着同一个错结果去
点控件，于是症状一路延后到「提交后平台说该题未答」，日志里看不出是哪儿开始错的。

平台在题目容器上明写着题号与题型码（问卷星是 ``<div id="divN" topic="N" type="C">``），
那是一条独立于我们探测逻辑的读数。``detection.detect_platform_questions`` 把它取回来，
本模块负责比、并把差异翻成一行行给人看的话。

三条契约：

  1. **只提示、不拦停、不改作答。** 对拍是诊断，不是校验门：平台码表没收录的码、
     模板没标的属性、我们暂时解释不了的形状差异，都不构成"这份问卷不能跑"的依据。
     与 ``platforms.unsupported_url_notice`` 同一条线。
  2. **没有平台信号就彻底静默。** 返回空列表，一行都不印。宁可少一个诊断，也不要
     用假警训练用户忽略提示 —— 假警的代价是真错位也没人看了。
  3. **同一句提示每进程只印一次。** 分页问卷每页都要对拍一次、一批又要跑十几份，
     结构差异是问卷本身的属性，重复印只会把运行信息埋掉（与 anchoring 的
     ``_REPORTED`` 同一类处理，测试用 ``reset_reported_drift`` 还原）。

码表在 ``platforms.WJX_TYPE_CODES``，其来源与"哪些码故意不列"记在那儿。
"""

from __future__ import annotations

import threading
from typing import Any

from .platforms import SurveyPlatform

__all__ = [
    "crosscheck_questions",
    "report_structure_drift",
    "reset_reported_drift",
]

# 提示前缀：与「[锚定]」「[分页]」「[平台]」并列，便于在日志里 grep 一类问题
_PREFIX = "[对拍]"


def _fmt_ours(qitem: dict) -> str:
    """我们这一侧的读数：``scale`` 带上级数，其他题型给名字。

    级数是排查时唯一还想知道的数（"判成 scale 几级"直接指向是数错了格子还是认错了容器），
    其他题型的规模对定位这条差异没帮助。
    """
    qtype = str(qitem.get("type") or "?")
    if qtype == "scale":
        return f"scale（{qitem.get('scale_min', 1)}~{qitem.get('scale')}）"
    return qtype


def _question_num(raw: Any) -> int | None:
    """题号取成 int；取不到就返回 ``None``（畸形行两边都跳过，绝不抛）。"""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def crosscheck_questions(
    questions: list[dict],
    platform_items: list[dict],
    platform: SurveyPlatform,
) -> list[str]:
    """比对"我们探测到的题目"与"平台自报的题目"，返回一行一条可读提示。

    :param questions:      ``detect_questions`` 的返回（**当前可见页**）
    :param platform_items: ``detect_platform_questions`` 的返回（同一页）
    :param platform:       取题型码表用；码表里没有的码不参与题型比对
    :return: 提示行列表；无平台信号时为空列表（契约 2）

    三类差异分别报：平台有、我们没有（该题会被留空）；两边都有但题型不符
    （会点错控件）；我们有、平台没标（多半是把非题目控件当成了题）。
    """
    if not platform_items:
        return []

    codes = platform.question_type_codes
    ours: dict[int, dict] = {}
    for qitem in questions:
        qi = _question_num(qitem.get("q"))
        if qi is None:
            continue
        ours[qi] = qitem

    lines: list[str] = []
    platform_nums: set[int] = set()
    for item in platform_items:
        qi = _question_num(item.get("q"))
        if qi is None:
            continue
        platform_nums.add(qi)
        code = str(item.get("code") or "").strip()
        known = codes.get(code)
        label = known.label if known else f"题型码 {code}"

        if qi not in ours:
            lines.append(
                f"{_PREFIX} 平台把 Q{qi} 标为 {label}，我们**没探测到这道题** → "
                "它会留空；必填时整份提交会被平台拦下（探测漏题型，不是网络问题）"
            )
            continue
        if known is None:
            continue    # 码没收录：题型比对无据可依，跳过（契约 1）
        if str(ours[qi].get("type") or "") not in known.accepted:
            lines.append(
                f"{_PREFIX} Q{qi} 我们识别为 {_fmt_ours(ours[qi])}，"
                f"平台标为 {label} → 作答会点错控件，"
                "提交后大概率是该题未答（换问卷/改版后常见，请核对探测的题型判定）"
            )

    for qi in sorted(set(ours) - platform_nums):
        lines.append(
            f"{_PREFIX} Q{qi}（我们识别为 {_fmt_ours(ours[qi])}）平台**没有标对应题目** → "
            "可能是把页面其他控件当成了题，答了也不会被收进这份问卷"
        )
    return lines


# ============================================================================
#  去重打印（契约 3）
# ============================================================================
_REPORTED: set[str] = set()
_REPORTED_LOCK = threading.Lock()


def report_structure_drift(lines: list[str]) -> list[str]:
    """过滤掉本进程已经印过的提示行，返回**新**的那些（调用方负责打印）。"""
    fresh: list[str] = []
    with _REPORTED_LOCK:
        for line in lines:
            if line in _REPORTED:
                continue
            _REPORTED.add(line)
            fresh.append(line)
    return fresh


def reset_reported_drift() -> None:
    """清空去重集合（仅测试用）。"""
    with _REPORTED_LOCK:
        _REPORTED.clear()
