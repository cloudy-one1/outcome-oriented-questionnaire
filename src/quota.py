"""自定义比例的配额调度（v4.2 设计提案 A1+C1 拍板落地，默认关，``--quota`` 开）。

口径（拍板记录见 ``docs/design/DESIGN_quota_scheduling.md`` §5）：

  - **A1 总量精确配额**：整批**明确成功**的 N 份，在配额题（单选/下拉）上恰好
    按配置比例兑现 —— ``quota_i = N·w_i/Σw``，最大余数法取整，总和恒等于 N。
    生成侧只在**未满配额**的选项里按权重抽：每份成功扣一格，收尾时
    「未满额的选项集合」恰好等于「还没兑现的份数」，最后一格不用挑，
    收敛是**精确**的而不是概率的。
  - **C1 按明确成功兑现**：failed / unknown / 中断不消耗配额（discard）——
    「有效答卷比例」语义。因此 ``--quota`` 隐含 ``--target-success``：
    循环跑到 N 份成功或尝试上限。
  - **B1 维持相对频率**：多选/矩阵/排序/量表不走配额（多选的边际配额存在
    不可行布局，见设计稿 §2 口径 B）—— 本模块只对**单选形**（每份恰好选
    一个的题型）生效。

接线与 :mod:`src.distribution` 完全同构（buffer → 成功 commit / 失败 discard，
生成侧单点修权），因为两者回答的是同一个问题的强弱两档：纠正把落地分布往
配置上**拉**，配额把它**钉死**。二者互斥（``--quota`` 与 ``--drift-correct``
同给退出码 2）。

线程语义与 distribution 相同：一个进程一个活动批次（Web 端由启动锁保证），
模块级状态不加锁。默认关：关着时本模块一个函数都不会产生效果。
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

_enabled = False
_total_target = 0
_warned_exhausted = False
#: qnum -> 每个选项的**初始配额**（已扣续传历史；报告用它算"还差几份"）
_allocated: dict[int, list[int]] = {}
#: qnum -> 每个选项的**剩余配额**
_remaining: dict[int, list[int]] = {}
#: qnum -> 选项下标 -> 续传历史里已消耗、但账本还没初始化时的暂记
_pre_consumed: dict[int, dict[int, int]] = {}
#: 本次作答的单选形选中项（0-based 下标），提交出结果前先攒着
_buffer: dict[int, int] = {}
_consumed_total = 0


def enable(flag: bool) -> None:
    global _enabled
    _enabled = bool(flag)


def enabled() -> bool:
    return _enabled


def start_run(total_target: int) -> None:
    """新批次开始：清空账本并记下目标成功份数 N。开着配额却不清，
    上一份问卷的账会管到这一份（与 distribution.start_run 同一条规矩）。"""
    global _total_target, _consumed_total, _warned_exhausted
    _total_target = int(total_target)
    _consumed_total = 0
    _warned_exhausted = False
    _allocated.clear()
    _remaining.clear()
    _pre_consumed.clear()
    _buffer.clear()


def _alloc(total: int, weights: list[float]) -> list[int]:
    """把 N 份按权重最大余数法摊成整数配额，总和恒等于 total。

    ``1:1:1`` 摊 100 → ``[34, 33, 33]``；``3:1`` 摊 20 → ``[15, 5]``。
    全零权重（上游已保证可用性，防御）摊不出 → 全 0，该题事实上无配额。
    """
    n = len(weights)
    total_w = sum(max(float(w), 0.0) for w in weights)
    if n == 0 or total_w <= 0:
        return [0] * n
    raw = [total * max(float(w), 0.0) / total_w for w in weights]
    floors = [int(x) for x in raw]
    order = sorted(range(n), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[: max(0, total - sum(floors))]:
        floors[i] += 1
    return floors


def effective_weights(qnum: int | None, weights: Any, n: int) -> Any:
    """配额模式下把**已满额**选项的权重清零；未开启 / 非单选形调用原样返回。

    懒初始化：某题第一次被生成路径问到时，按当时的权重（批次快照，运行中
    不可变 —— 见全面审查 P1-3）把 N 份摊成配额，并扣掉续传历史已消耗的部分。
    返回值只用于抽样，**不回写** ``WEIGHT_CONFIG``（与 distribution.adjust
    同一条纪律：用户配置是意图，必须始终可读、可导出）。
    """
    if not _enabled or qnum is None or not weights or len(weights) != n:
        return weights
    q = int(qnum)
    if q not in _remaining:
        alloc = _alloc(_total_target, [float(w) for w in weights])
        rem = list(alloc)
        pre = _pre_consumed.get(q)
        if pre:
            for idx, cnt in pre.items():
                if not (0 <= idx < len(rem)):
                    print(f"  WARNING: [配额] Q{q} 历史成功答卷选了下标 {idx}，"
                          f"超出当前选项数 {len(rem)} —— 该笔消耗无法入账"
                          "（配置与历史不一致，续传前请核对）")
                    continue
                if rem[idx] >= cnt:
                    rem[idx] -= cnt
                else:
                    print(f"  WARNING: [配额] Q{q} 选项 {idx} 的历史成功数 {cnt} "
                          f"已超过当前配额 {alloc[idx]} —— 夹到 0 继续续传，"
                          "收尾分布会略偏离目标（改过权重后续传的既有代价）")
        _allocated[q] = list(rem)
        _remaining[q] = rem
    rem = _remaining[q]
    out = [float(w) if rem[i] > 0 else 0.0 for i, w in enumerate(weights)]
    if sum(out) <= 0:
        # 全部满额还来要权重：正常只发生在"成功数已达标、批次收尾中"。但若
        # 某些配额没能随成功扣减（点击未落地 is_ok=False、平台跳题），账本会
        # 提前全满而批次仍在继续 —— 后续份数不再受配额约束，这是"精确收敛"
        # 的已知失守路径，必须大声说出来而不是静默放过（每批提醒一次）。
        global _warned_exhausted
        if not _warned_exhausted and _consumed_total < _total_target:
            _warned_exhausted = True
            print(f"  WARNING: [配额] 账本已全满但成功数 {_consumed_total}/"
                  f"{_total_target} 尚未达标 —— 有题目在成功答卷里缺席了"
                  "（点击未落地或平台跳题），后续份数不再受配额约束，"
                  "收尾报告会列出缺口")
        return list(weights)
    return out


def buffer_answer(qnum: int, picks: list[int]) -> None:
    """单选形作答先攒着 —— 此刻还不知道这一份会不会提交成功。

    只收**恰好选 1 个**的题（多选的入选率语义与配额无关，见模块 docstring B1）；
    量表等数值题经 question_stage 的换算进来的单元素 picks 会在 commit 时因
    "该题没有配额账本"而自然忽略。
    """
    if not _enabled or len(picks) != 1:
        return
    _buffer[int(qnum)] = int(picks[0])


def commit_buffer() -> int:
    """整份提交成功：按缓冲扣减配额，返回扣账的题数。

    满额项不扣成负数 —— 生成侧已屏蔽满额项，走到这里说明有账目不一致
    （防御；收尾报告里"已兑现/应兑现"会露出破绽）。
    """
    global _consumed_total
    n = 0
    for qnum, pick in _buffer.items():
        rem = _remaining.get(qnum)
        if rem is None or not (0 <= pick < len(rem)):
            continue
        if rem[pick] > 0:
            rem[pick] -= 1
            _consumed_total += 1
        n += 1
    _buffer.clear()
    return n


def discard_buffer() -> int:
    """失败 / UNKNOWN / 中断：这一份不消耗配额（C1 语义的核心），返回丢弃题数。"""
    n = len(_buffer)
    _buffer.clear()
    return n


def rebuild_consumed(history_db: Any, run_id: int) -> int:
    """续传恢复：按历史库把已成功份数消耗掉的配额暂记下来（返回扣账题次）。

    单一真相与 ``resume_progress`` 相同：只认 ``submissions.final_status =
    'success'`` 的那几份，明细按 ``answers.options_selected``（0-based，单选形
    恰一元素）扣。账本懒初始化在生成路径上，这里先记 ``_pre_consumed``，
    首次问到该题时从配额里扣。
    """
    if not _enabled:
        return 0
    statuses = history_db.submission_statuses(run_id)
    rows = history_db.query_answers(run_id=run_id, limit=1_000_000)
    counted = 0
    for row in rows:
        try:
            sub = int(row["submission_index"])
            qnum = int(row["question_number"])
        except (TypeError, ValueError):
            continue
        if statuses.get(sub) != "success":
            continue
        raw = row["options_selected"]
        if raw is None:
            continue
        try:
            loaded = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(loaded, list) or len(loaded) != 1:
            continue
        try:
            pick = int(loaded[0])
        except (TypeError, ValueError):
            continue
        _pre_consumed.setdefault(qnum, defaultdict(int))[pick] += 1
        counted += 1
    return counted


def progress_report() -> list[str]:
    """批次收尾报告：整体兑现数 + 没填满的配额题，并区分两种缺口的成因。"""
    if not _enabled or not _allocated:
        return []
    lines = [f"[配额] 本批成功 {_consumed_total}/{_total_target} 份，"
             f"配额题 {len(_allocated)} 道"]
    for qnum in sorted(_allocated):
        rem = _remaining.get(qnum, _allocated[qnum])
        outstanding = sum(rem)
        if outstanding > 0:
            detail = "、".join(
                f"选项 {i + 1} 差 {r}" for i, r in enumerate(rem) if r > 0
            )
            if _consumed_total >= _total_target > 0:
                reason = ("已达目标成功数但部分成功答卷缺席了这些选项"
                          "—— 点击未落地或平台跳题，配额无法对它们记账")
            else:
                reason = "尝试上限先到，配额没有兑完"
            lines.append(f"[配额] Q{qnum} 未填满（差 {outstanding} 份：{detail}）"
                         f"—— {reason}")
    return lines
