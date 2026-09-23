"""提交前完整度自检（v3.1）：整题没探测到的必答题，不要把那份交上去。

为什么单独一步：平台对必答题的拦截发生在**点提交之后** —— 弹一句"第 N 题未答"，
本工具今天的样子是提交返回失败 / unknown，再从窗口日志里猜是哪一题。如果某道题
我们**整份流程压根没探测到**（探测漏了某种真页面控件），那次提交是注定被拦的：
白点一次，还留下一条看不懂原因的失败。这一步在点提交之前把这种题挑出来，
判本轮失败并直接说出题号。

判据刻意收得极窄 —— 只用一个零歧义的事实：**我们连这道题都没见过**。

  * 平台标了 ``req``、题号又不在"整份流程探测到的题号集合"里 → 我们必定没答它
    → 平台必定拦 → 不点提交。
  * 探测到了、也照着"答"了，但题型判错（矩阵量表被判成 ``scale`` 那一类）→
    **这里不管**。那种缺口要读"已答扫描"，而已答扫描读的是同一套错误结构，
    拿它当判据等于让错的判定自己给自己背书。那条路真正要做的是把探测修对，
    并让已答判定按**平台的存储字段**读，而不是按我们的控件假设读。

两条降级说的是同一句话：**宁可少拦，不能拦错** —— 拦错一次就是一单本来能交的
问卷被判失败。平台没自报结构（拿不到 ``topic``）→ 空；容器上没有 ``req`` → 视为
不必答 → 空。
"""

from __future__ import annotations

from typing import Any

__all__ = ["describe_gap", "describe_not_written", "unanswered_required"]


def unanswered_required(platform_items: list[dict], detected_qnums: set[int]) -> list[int]:
    """平台标为必答、但整份流程都没探测到的题号（升序）。

    :param platform_items: ``detect_platform_questions(..., visible_only=False)``
        的返回 —— 是**整卷**的题，不是最后那一页的题
    :param detected_qnums: 这一份问卷逐页探测到的题号并集
    """
    if not platform_items:
        return []
    gap = set()
    for item in platform_items:
        if not item.get("required"):
            continue
        raw: Any = item.get("q")
        try:
            qi = int(raw)
        except (TypeError, ValueError):
            continue
        if qi not in detected_qnums:
            gap.add(qi)
    return sorted(gap)


def describe_gap(gap: list[int]) -> str:
    """缺口的一行说明；调用方决定打不打日志、判不判失败。"""
    nums = "、".join(f"Q{n}" for n in gap)
    return (
        f"[完整度] 平台标了必答题 {nums}，整份流程里一道都没探测到 → **不点提交**"
        "（交上去也会被平台按必填拦下；探测漏了题型，不是网络问题，"
        "上面的 [对拍] 提示就是它）"
    )


def describe_not_written(gap: list[int]) -> str:
    """"我们照这道题动了手、交互层回话说没落上"的一行说明（只报，不改判定）。

    与 ``describe_gap`` 是**两件事**，不要并成一句：那道是"我们连这道题都没见过"，
    零歧义，所以可以拦停；这道是"见过、也答了，但回执说没写上"。后者的回执有两种
    形状 —— 交互层明确说控件没找到（真的没落上），或者作答中途抛了异常被降级跳过
    （也许已经落上了一部分，只是没读到回执）。**这两种都没资格当拦停依据**：
    拦错一次就是一单本来能交的问卷被判失败（``宁可少拦，不能拦错``，与本模块同上）。

    那为什么要说：不说的话，这一份的症状是"点提交 → 平台报第 N 题未答 → 日志里一条
    看不出原因的失败"。而这里在点提交**之前**就已经知道是哪几题没落上。
    """
    nums = "、".join(f"Q{n}" for n in gap)
    return (
        f"[作答回执] {nums} 我们照着答了一遍，但交互层回话说页面上没落上 —— "
        "其中若有必答题，点提交会被平台拦下（不是网络问题）。"
        "本行**不拦停**：回执可能只是没读到，而认错一单的代价是一单本来能交的问卷被判失败"
    )
