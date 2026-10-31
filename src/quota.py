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
  - **B2 多选入选率校准（2026-10-30 立项，推翻最初的 B1）**：配额模式下
    多选的 weights = 目标入选率 —— 引擎选"剩余边际配额最大的 k 个"（平局
    随机），使各选项实际入选率 ∝ 权重（夹 100% 后重分配，总和恒 = k）。
    要求 ``count_options`` 固定单一值；不满足的题回落 B1 相对频率并告警。
    矩阵多选/量表仍维持相对频率（B1 边界不变）。

接线与 :mod:`src.distribution` 完全同构（buffer → 成功 commit / 失败 discard，
生成侧单点修权），因为两者回答的是同一个问题的强弱两档：纠正把落地分布往
配置上**拉**，配额把它**钉死**。二者互斥（``--quota`` 与 ``--drift-correct``
同给退出码 2）。

线程语义与 distribution 相同：一个进程一个活动批次（Web 端由启动锁保证），
模块级状态不加锁。默认关：关着时本模块一个函数都不会产生效果。
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from typing import Any, Optional

_enabled = False
_total_target = 0
_warned_exhausted = False
_success_count = 0
#: qnum -> 每个选项的**初始配额**（已扣续传历史；报告用它算"还差几份"）
_allocated: dict[int, list[int]] = {}
#: qnum -> 每个选项的**剩余配额**
_remaining: dict[int, list[int]] = {}
#: qnum -> 选项下标 -> 续传历史里已消耗、但账本还没初始化时的暂记
_pre_consumed: dict[int, dict[int, int]] = {}
#: 本次作答的选中项（0-based 下标列表），提交出结果前先攒着
_buffer: dict[int, list[int]] = {}
_consumed_total = 0

# ---- B2（多选入选率校准，2026-10-30 立项）：多选在配额模式下的第二本账 ----
#: qnum -> 每个选项的边际剩余配额（该选项还应出现在几份成功答卷里）
_multi_remaining: dict[int, list[int]] = {}
_multi_allocated: dict[int, list[int]] = {}
_multi_k: dict[int, int] = {}
_multi_pre_consumed: dict[int, dict[int, int]] = {}
_multi_fallback_warned: set[str] = set()


def enable(flag: bool) -> None:
    global _enabled
    _enabled = bool(flag)


def enabled() -> bool:
    return _enabled


def start_run(total_target: int) -> None:
    """新批次开始：清空账本并记下目标成功份数 N。开着配额却不清，
    上一份问卷的账会管到这一份（与 distribution.start_run 同一条规矩）。"""
    global _total_target, _consumed_total, _warned_exhausted, _success_count
    _total_target = int(total_target)
    _consumed_total = 0
    _success_count = 0
    _warned_exhausted = False
    _allocated.clear()
    _remaining.clear()
    _pre_consumed.clear()
    _buffer.clear()
    _multi_remaining.clear()
    _multi_allocated.clear()
    _multi_k.clear()
    _multi_pre_consumed.clear()
    _multi_fallback_warned.clear()


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
        if not _warned_exhausted and _success_count < _total_target:
            _warned_exhausted = True
            print(f"  WARNING: [配额] 账本已全满但成功数 {_consumed_total}/"
                  f"{_total_target} 尚未达标 —— 有题目在成功答卷里缺席了"
                  "（点击未落地或平台跳题），后续份数不再受配额约束，"
                  "收尾报告会列出缺口")
        return list(weights)
    return out


def buffer_answer(qnum: int, picks: list[int]) -> None:
    """作答先攒进缓冲 —— 此刻还不知道这一份会不会提交成功。

    单选形（恰好一个下标）进单选账；多选校准题（账本已在生成时建好）进
    多选账 —— k=1 的多选与单选形状相同，靠"该题在哪本账里"区分，不靠长度。
    其余（未配置 / 矩阵等）自然忽略。
    """
    if not _enabled or not picks:
        return
    q = int(qnum)
    if q in _multi_remaining or len(picks) == 1:
        _buffer[q] = [int(p) for p in picks]


def commit_buffer() -> int:
    """整份提交成功：按缓冲扣减配额，返回扣账的题数。

    单选账扣一格；多选校准账按这一份的勾选集合逐项扣（全有账才扣 ——
    部分越界的防御性丢弃会让 Σ剩余配额与成功数脱钩）。满额项不扣成负数。
    """
    global _consumed_total
    n = 0
    for qnum, picks in _buffer.items():
        srem = _remaining.get(qnum)
        if srem is not None:
            if len(picks) == 1 and 0 <= picks[0] < len(srem) and srem[picks[0]] > 0:
                srem[picks[0]] -= 1
                _consumed_total += 1
            n += 1
        mrem = _multi_remaining.get(qnum)
        if mrem is not None:
            if all(0 <= p < len(mrem) and mrem[p] > 0 for p in picks) \
                    and len(set(picks)) == len(picks):
                for p in picks:
                    mrem[p] -= 1
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
    'success'`` 的那几份。单选账收 len==1 的明细；多选校准账收该题**全部**
    明细（k=1 的多选与单选形状相同，靠题号落在哪本账区分，不会双计 ——
    一个题号只会建其中一本账）。暂记在账本懒初始化时扣减。

    v4.3.1（独立复审 P1）：``options_selected`` 落库是 **value 域**（问卷星
    单选/多选 1..n），而配额账本是 **0-based 下标域** —— 此前把 value 直接
    当下标扣，整条账错一位：目标 2:2 的题续跑后实际 3:1（多选同病）。
    修正：下标 = value - 1（0 与负值防护性跳过 —— 量表 min=0 的卷没有
    配额账，不受影响）。这与活路径 ``question_stage._distribution_picks``
    的 value→下标换算同一语义。
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
        if not isinstance(loaded, list) or not loaded:
            continue
        try:
            picks = [int(p) for p in loaded]
        except (TypeError, ValueError):
            continue
        # value 域（1..n）→ 下标域（0..n-1）：差一修正是本函数的存在理由
        idxs = [p - 1 for p in picks if p - 1 >= 0]
        if not idxs:
            continue
        if len(idxs) == 1:
            _pre_consumed.setdefault(qnum, defaultdict(int))[idxs[0]] += 1
        _multi_pre_consumed.setdefault(qnum, defaultdict(int))
        for idx in idxs:
            _multi_pre_consumed[qnum][idx] += 1
        counted += 1
    return counted


def note_success() -> None:
    """批次层每拿到一个明确成功的提交记一次 —— 报告与"账本全满"告警的
    达标判断用它，与逐份成败表同口径（failed/unknown 不计）。"""
    global _success_count
    if _enabled:
        _success_count += 1


# ---------------------------------------------------------------------------
#  B2：多选入选率校准（2026-10-30 立项，推翻此前的 B1 选择）
# ---------------------------------------------------------------------------
def _alloc_multi(total: int, weights: Any, n: int, k: int) -> list[int]:
    """B2 边际配额：rate_i = min(1, k·w_i/Σw)（夹 100%，差额在未封顶项间按
    权重水位重分配），q_i = N·rate_i，最大余数法取整，总和恒 = k·N。

    例：权重 8,1,1、每份选 2、N=10 → q = [10, 5, 5]（入选率 100/50/50%）。
    每个 q_i ≤ N（rate ≤ 1 的直接推论），这是贪心可行性的前提。
    """
    ws = [max(float(w), 0.0) for w in weights] if isinstance(weights, (list, tuple)) \
        else []
    if len(ws) != n:
        ws = [1.0] * n
    total_w = sum(ws)
    if total_w <= 0:
        ws = [1.0] * n
        total_w = float(n)

    alloc_f = [0.0] * n
    active = set(range(n))
    remaining_share = float(k * total)
    for _ in range(n):
        if not active:
            break
        wsum = sum(ws[i] for i in active)
        newly_capped = False
        for i in sorted(active):
            share = remaining_share * (ws[i] / wsum if wsum > 0 else 1.0 / len(active))
            if share >= total and len(active) > 1:
                alloc_f[i] = float(total)      # rate = 100%，封顶退出重分配
                active.discard(i)
                remaining_share -= total
                newly_capped = True
        if not newly_capped:
            break
    wsum = sum(ws[i] for i in active)
    if active:
        if wsum > 0:
            for i in active:
                alloc_f[i] = remaining_share * ws[i] / wsum
        else:
            for i in active:
                alloc_f[i] = remaining_share / len(active)

    floors = [int(x) for x in alloc_f]
    order = sorted(range(n), key=lambda i: (-(alloc_f[i] - floors[i]), i))
    for i in order[: max(0, k * total - sum(floors))]:
        floors[i] += 1
    return floors


def multi_calibrated_k(cfg: Any, n: int, qi: Any) -> Optional[int]:
    """B2 资格判定：``count_options`` 必须恰好一个值且 ≤ n，返回该 k。

    "每个选项出现在几成答卷里"只在 k 固定时良定义 —— k 随 count 分布漂移
    的题校准不了，回落 B1 相对频率并按题告警一次（告警随批次清零）。
    """
    co = cfg.get("count_options") if isinstance(cfg, dict) else None
    if not isinstance(co, (list, tuple)) or len(co) != 1:
        k = None
    else:
        try:
            k = int(co[0])
        except (TypeError, ValueError):
            k = None
    if k is not None and not (1 <= k <= n):
        k = None
    if k is None:
        key = f"q{qi}"
        if key not in _multi_fallback_warned:
            _multi_fallback_warned.add(key)
            print(f"  WARNING: [配额] Q{qi} 的多选未进入入选率校准 —— "
                  "count_options 必须固定为单一值（k 随卷漂移时『入选率』没有"
                  "定义）；该题按相对频率（B1）作答")
        return None
    return k


def multi_pick(qnum: Any, n: int, k: int, weights: Any) -> Optional[list[int]]:
    """B2：返回这一份该勾的 k 个选项下标（0-based，升序）；未校准返回 None。

    选**剩余配额最大的 k 个**（平局均匀随机 —— 哪些具体卷次勾哪个平局项
    不影响最终边际）。可行性论证：Σ剩余 = k×剩余成功份数，且"剩余 = 剩余
    成功份数"的选项必然全部落在最大的 k 个里（至多 k 个），每份成功扣一格
    后不变量保持 —— 贪心永不卡死，收尾精确到格。
    """
    if not _enabled or qnum is None or k < 1 or k > n:
        return None
    q = int(qnum)
    if f"q{q}" in _multi_fallback_warned and q not in _multi_remaining:
        return None
    if q not in _multi_remaining:
        rem = _alloc_multi(_total_target, weights, n, k)
        pre = _multi_pre_consumed.get(q)
        if pre:
            for idx, cnt in pre.items():
                if not (0 <= idx < len(rem)):
                    print(f"  WARNING: [配额] Q{q} 历史成功答卷选了下标 {idx}，"
                          f"超出当前选项数 {len(rem)} —— 该笔消耗无法入账"
                          "（配置与历史不一致，续传前请核对）")
                    continue
                allocated = rem[idx]
                if allocated >= cnt:
                    rem[idx] = allocated - cnt
                else:
                    print(f"  WARNING: [配额] Q{q} 选项 {idx} 的历史成功数 {cnt} "
                          f"已超过当前配额 {allocated} —— 夹到 0 继续续传，"
                          "收尾分布会略偏离目标（改过权重后续传的既有代价）")
                    rem[idx] = 0
        _multi_allocated[q] = list(rem)
        _multi_remaining[q] = rem
        _multi_k[q] = k
    if _multi_k.get(q) != k:
        # count 候选漂移成了多值（首份后才发现）—— 该题退回 B1，账本作废
        _multi_remaining.pop(q, None)
        _multi_allocated.pop(q, None)
        if f"q{q}" not in _multi_fallback_warned:
            _multi_fallback_warned.add(f"q{q}")
            print(f"  WARNING: [配额] Q{q} 的多选 count 在批次中发生了漂移 —— "
                  "入选率校准无法继续，该题回落相对频率（B1）")
        return None
    rem = _multi_remaining[q]
    order = sorted(range(n), key=lambda i: (-rem[i], random.random()))
    take = [i for i in order[:k] if rem[i] > 0]
    if len(take) < k:
        global _warned_exhausted
        if not _warned_exhausted and _success_count < _total_target:
            _warned_exhausted = True
            print(f"  WARNING: [配额] 多选账本剩余不足 k 个但成功数 "
                  f"{_success_count}/{_total_target} 尚未达标 —— 有勾选没能随"
                  "成功扣账（点击未落地或平台跳题），后续份数不再受校准约束")
        return None
    return sorted(take)


def progress_report() -> list[str]:
    """批次收尾报告：整体兑现数 + 没填满的配额题（单选与多选校准两本账），
    并区分两种缺口的成因。"""
    if not _enabled or not (_allocated or _multi_allocated):
        return []
    lines = [f"[配额] 本批成功 {_success_count}/{_total_target} 份，"
             f"配额题 {len(_allocated) + len(_multi_allocated)} 道"
             + (f"（其中多选校准 {len(_multi_allocated)} 道）"
                if _multi_allocated else "")]
    for qnum in sorted(_allocated) + sorted(_multi_allocated):
        is_multi = qnum in _multi_allocated
        alloc, rem = (
            (_multi_allocated[qnum], _multi_remaining.get(qnum, _multi_allocated[qnum]))
            if is_multi
            else (_allocated[qnum], _remaining.get(qnum, _allocated[qnum]))
        )
        outstanding = sum(rem)
        if outstanding > 0:
            detail = "、".join(
                f"选项 {i + 1} 差 {r}" for i, r in enumerate(rem) if r > 0
            )
            if _success_count >= _total_target > 0:
                reason = ("已达目标成功数但部分成功答卷缺席了这些选项"
                          "—— 点击未落地或平台跳题，配额无法对它们记账")
            else:
                reason = "尝试上限先到，配额没有兑完"
            tag = "Q" if not is_multi else "Q（多选校准）"
            lines.append(f"[配额] {tag}{qnum} 未填满（差 {outstanding} 份：{detail}）"
                         f"—— {reason}")
    return lines
