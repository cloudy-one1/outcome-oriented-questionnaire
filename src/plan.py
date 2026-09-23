"""信度计划矩阵的**数学内核**（``docs/design/DESIGN_reliability_alpha.md`` §5 P1 的前半段）。

职责边界（先说清，免得顺手把接线也做了）
    输入「每道参与题的目标选项配额 + 目标 Cronbach α」，输出一张
    「第 i 份 × 第 j 题 → 选哪个选项」的计划矩阵。P0（``src/reliability.py``）只
    **测**，本模块开始**控制**。以下三件事本模块一律不做，它们属于接线那一半
    （设计稿 §3 接缝表）：CLI/GUI 参数、``answering_v2.generate_answer`` 的查表入口
    与回退、config schema 3.1 的 ``dimension`` / ``reverse`` / ``alpha_target`` 字段。

算法（设计稿 §2，一步不藏）
    1. 每份抽一个潜变量 ``θ ~ N(0,1)``；每道参与题算得分 ``X = ±θ + σ_e·ε``
       （``±`` 由该题的 ``reverse`` 决定，``ε ~ N(0,1)`` 独立）。
    2. 把一题的 n 个得分**按秩映射**到该题的配额向量上：排名落在哪一段就取哪个选项。
       秩映射对得分的任何单调变换都不变，于是**边际配额是构造性精确的**（整数分配
       出来的，不靠概率收敛），而 α 只由 ``σ_e`` 这一个自由度控制。
    3. ``σ_e`` 一维搜索：以解析值（:func:`alpha_to_sigma`）为中心取 9 个候选，
       再在跨零区间线性细化 ≤4 次 —— 总共 10~13 次 ``O(k·n)`` 全量扫描，纯 stdlib。

一个必须写下来的口径（否则计划 α 会静默地不等于实测 α）
    α 算在**映射之后的选项索引**上（``索引 + 1``，与 P0 的 ``ordinal_score`` 同基），
    不是算在那串连续得分上。真正投递出去、也真正被 ``reliability.cronbach_alpha``
    度量的是前者。只按连续得分的解析值一次定死 σ_e 会系统性偏高 —— 粗粒度配额把
    每题都"钝化"了一遍，这正是设计稿 §3 说的"边际 + α 两个约束不一定同时可达"，
    所以要搜。反向题在算 α 时按声明取负号，这与 ``measure_dimensions`` 用的
    ``(obs_min + obs_max) − x`` 翻转在 α 上**完全等价**（仿射变换不改单题方差，
    逐行加减常数也不改总分方差），所以计划值与 P0 的实测值直接可比。

降级与取舍全在 :func:`build_plan` 的 docstring 与"边际优先"那一段。
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .config import WEIGHT_CONFIG
from .reliability import cronbach_alpha, dimensions_from_config, reverse_from_config

__all__ = [
    "ALPHA_OK_TOLERANCE",
    "ItemSpec",
    "MIN_ITEMS_FOR_ALPHA",
    "Plan",
    "SMALL_SAMPLE_N",
    "alpha_to_sigma",
    "build_plan",
    "begin_submission",
    "configure",
    "configured",
    "end_session",
    "ensure_plan",
    "forced_choice",
    "reset_survey",
]

#: 设计稿 §6：参与题数 ``k < 3`` 的维度整体不参战 —— α 无意义，报了就是骗人。
MIN_ITEMS_FOR_ALPHA = 3

#: 小样本线（设计稿 §6：``n < 30`` 的维度只报实测、不做控制）。本内核不拒收，
#: 但会把这句话原样记进 notes —— 要不要因此关掉控制，是接线侧（CLI/GUI）的决定。
SMALL_SAMPLE_N = 30

#: |计划 α − 目标 α| 在此之内算"达标"。量级取设计稿 §5 P1 那条测试承诺（±0.02）。
ALPHA_OK_TOLERANCE = 0.02

#: 搜索的停机线，刻意比达标线更紧：不为第 4 位小数多烧几轮全量扫描。
ALPHA_STOP_TOLERANCE = 0.002

#: σ_e 候选相对解析值的倍率。**刻意不对称偏小**：秩映射到有限个选项之后每题都被
#: 钝化过一遍，同样的 σ_e 实测 α 系统性低于连续得分的解析值，
#: 所以要往"小 σ_e（高相关）"那一侧多留几格，否则高目标会整片落在候选区间之外。
_SIGMA_FACTORS = (0.25, 0.4, 0.55, 0.7, 0.85, 1.0, 1.2, 1.5, 2.0)

#: 跨零区间线性细化的次数上限（含"把 σ_e=0 这个零误差端点拉进区间"的那一次探针）。
_MAX_REFINEMENTS = 4


# ============================================================================
#  数学：目标 α → 误差标准差
# ============================================================================
def _require_unit_alpha(alpha: float, label: str) -> float:
    """α 的定义域校验。单独一个函数是因为 ``alpha_to_sigma`` 与 ``build_plan``
    都得校验同一个东西，而两处的报错措辞必须一字不差（用户看到的就这一行）。"""
    a = float(alpha)
    if not math.isfinite(a):
        raise ValueError(f"{label} 必须是有限实数，收到 {alpha!r}")
    if not 0.0 < a < 1.0:
        raise ValueError(
            f"{label} 必须落在开区间 (0, 1)，收到 {a!r}："
            "0 意味着题间毫无互相关（α 无从反解误差方差），"
            "1 意味着零误差（σ_e 恒等于 0，控制失去自由度）"
        )
    return a


def alpha_to_sigma(alpha: float, k: int) -> float:
    """把目标 Cronbach α 折成平行测量模型下的误差标准差 σ_e（设计稿 §2）。

    ``ρ = α / (k − α(k−1))``，再取 ``σ_e = sqrt(1/ρ − 1)``。归一取 ``Var(θ) = 1``，
    于是 ``Cov(Xi,Xj) = 1``、``Var(Xi) = 1 + σ_e²``、``ρ = 1/(1 + σ_e²)``。

    两端的行为值得记住（测试钉的就是这两条）：``α → 1`` 时 ``ρ → 1``、``σ_e → 0``
    （题间完全平行，没有误差）；``α → 0`` 时 ``ρ → 0``、``σ_e → ∞``。
    同一个 α 下 k 越大 ρ 越小、σ_e 越大 —— 题多的量表本来就容易拿到高 α。

    :raises ValueError: ``α ∉ (0, 1)``（含 NaN/inf），或 ``k < 2``（α 的定义里有
        ``k/(k−1)``，单题谈不上内部一致性）。
    """
    a = _require_unit_alpha(alpha, "alpha")
    items = int(k)
    if items < 2:
        raise ValueError(
            f"k 必须 ≥ 2，收到 {k!r}：α = k/(k−1)·(1 − ΣVar(Xi)/Var(S)) 的定义里"
            "分母是 k−1，单题谈不上内部一致性"
        )
    rho = a / (items - a * (items - 1))
    if rho <= 0.0:
        # 数学上不可达（α∈(0,1)、k≥2 ⇒ k−α(k−1) > 1 > 0），纯防御：
        # 真走到这里说明上面两条校验漏了东西，宁可报错也不要 sqrt 里冒出负数。
        raise ValueError(f"反解出的题间互相关 ρ={rho!r} 非正：α/k 组合不在定义域内")
    # ρ ≤ 1 恒成立，但 α 贴近 1 时浮点会让 1/ρ−1 掉到 -1e-16 这种值，
    # max(0, ·) 钳一下 —— 为这个凭空造一条 ValueError 太刻薄，σ_e=0 才是真相。
    return math.sqrt(max(0.0, 1.0 / rho - 1.0))


# ============================================================================
#  输入 / 输出模型
# ============================================================================
@dataclass(slots=True)
class ItemSpec:
    """一道参与题的计划需求。

    :param qnum: 题号（与探测结果、``history.answers.question_number`` 同一套编号）
    :param options: 该题选项数
    :param quota: 长度 = ``options`` 的**份数**向量，逐选项写明"这个选项要落到几份"，
        总和必须等于 ``n``。写成份数而不是比例，是因为承诺的是精确边际：
        比例 ``[0.33, 0.33, 0.34] × n=10`` 这种账谁也兑不出整数，份数能。
    :param reverse: 反向题。生成时取 ``−θ``；算计划 α 时按声明取负号。
    """

    qnum: int
    options: int
    quota: Sequence[int]
    reverse: bool = False

    def __post_init__(self) -> None:
        # 归一化成 tuple：配额是"这一批要精确兑现"的承诺，不能在被搜索消费的期间
        # 被上游那份 list 悄悄改掉（调用方复用同一个 list 是很常见的写法）。
        self.qnum = int(self.qnum)
        self.options = int(self.options)
        self.quota = tuple(int(v) for v in self.quota)
        self.reverse = bool(self.reverse)


@dataclass(slots=True)
class Plan:
    """一张计划矩阵：``题号 →（第 i 份 → 0-based 选项索引）``。

    ``sample_index`` 与 ``history.answers.submission_index`` **同基（1-based）**，
    接线时可以把那份索引直接传进来。查不到（该题不参与 / 索引越界）返回 None，
    调用方按设计稿 §3 回退现有的逐题加权随机 —— 计划矩阵不是闸门，是加速器。
    """

    n: int                                    # 计划覆盖的份数
    k: int                                    # 实际参与计划的题数（降级时为 0）
    alpha_target: float
    alpha_planned: Optional[float]            # None = 没算/算不出，语义见 notes
    sigma_e: Optional[float]
    rows: dict[int, tuple[int, ...]] = field(default_factory=dict)
    quotas: dict[int, tuple[int, ...]] = field(default_factory=dict)
    reversed_items: list[int] = field(default_factory=list)
    excluded: list[tuple[int, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    evaluations: int = 0                      # σ_e 搜索花掉几次 O(k·n) 全量扫描

    def choice_for(self, qnum: int, sample_index: int) -> Optional[int]:
        """第 ``sample_index`` 份（1-based）在第 ``qnum`` 题上该点哪个选项。

        返回 None 的三种情形都是"这一格没有计划"，不是错误：该题没参与（被排除、
        或整个维度因题数不足而不参战）、``sample_index`` 越界（批次比计划长）、
        以及 ``sample_index < 1``（0-based 传错进来了 —— 宁缺毋错，别悄悄错一位）。
        """
        row = self.rows.get(int(qnum))
        if row is None:
            return None
        index = int(sample_index)
        if index < 1 or index > len(row):
            return None
        return row[index - 1]

    def participating(self) -> list[int]:
        """真正有计划的那些题的题号（按 spec 声明顺序）。"""
        return list(self.rows)


# ============================================================================
#  内部小工具
# ============================================================================
def _check_specs(specs: Sequence[ItemSpec], total: int) -> list[ItemSpec]:
    """结构校验：形状不对一律抛，不猜。

    为什么不"顺手修正"（比例归一化、补齐长度、截断多余项）：配额是用户显式配的
    （设计稿 §6 的最后一条），静默改掉一个数就意味着投递出去的分布没人认领过。
    抛错的代价是一次重跑，静默修正的代价是一批数据作废。
    """
    out: list[ItemSpec] = []
    seen: set[int] = set()
    for spec in specs:
        if spec.qnum in seen:
            raise ValueError(
                f"题号 {spec.qnum} 重复：计划矩阵按题号索引，重复条目会互相覆盖，"
                "最后一份悄悄生效"
            )
        seen.add(spec.qnum)
        if spec.options < 1:
            raise ValueError(f"题 {spec.qnum} 的选项数 {spec.options} 非法：至少 1 个")
        if len(spec.quota) != spec.options:
            raise ValueError(
                f"题 {spec.qnum} 的配额长度 {len(spec.quota)} 与选项数 {spec.options} 不符："
                "配额是逐选项给份数的，少一项就是有一项永远轮不到"
            )
        if any(v < 0 for v in spec.quota):
            raise ValueError(f"题 {spec.qnum} 的配额含负数：{list(spec.quota)!r}")
        if sum(spec.quota) != total:
            raise ValueError(
                f"题 {spec.qnum} 的配额总和 {sum(spec.quota)} ≠ 份数 {total}："
                "配额写的是「每个选项落到几份」，两者必须同量纲。"
                "要么改配额，要么改份数 —— 内核不做按比例缩放，那等于替用户重订目标"
            )
        out.append(spec)
    return out


def _rank_to_option(quota: Sequence[int]) -> list[int]:
    """``名次 → 选项索引`` 的查表：把配额向量摊成一份长度 = n 的名次分段。

    名次最小的那份拿第 0 个选项，依次向上。长度恰好等于 ``sum(quota) == n``，
    所以「第 r 名取哪个」是一次数组取值，不必二分也不必排序配额。
    """
    out: list[int] = []
    for option_index, count in enumerate(quota):
        out.extend([option_index] * int(count))
    return out


def _realized_counts(row: Sequence[int], options: int) -> tuple[int, ...]:
    seen = Counter(row)
    return tuple(seen.get(i, 0) for i in range(options))


@dataclass(slots=True)
class _Search:
    """σ_e 一维搜索的结果（内部结构体，只服务于 notes 的措辞）。"""

    sigma: float
    alpha: Optional[float]
    achieved_min: Optional[float]
    achieved_max: Optional[float]
    evaluations: int
    bracketed: bool


# ============================================================================
#  主流程
# ============================================================================
def build_plan(
    specs: Sequence[ItemSpec],
    n: int,
    alpha_target: float,
    rng_seed: Optional[int] = None,
) -> Plan:
    """产出「第 i 份 × 第 j 题 → 选哪个选项」的计划矩阵（设计稿 §5 P1 内核）。

    步骤：校验 specs → 抽样 ``θ`` 与每题的 ``ε``（各一次，之后搜索**不再动随机数**，
    这既省成本又让 α(σ_e) 成为确定函数、比较不同候选时才可比）→
    一维搜 σ_e → 用最优 σ_e 做一次秩映射定稿。

    参数与返回
        :param specs: 参与题的需求，见 :class:`ItemSpec`
        :param n: 份数（与配额同量纲）
        :param alpha_target: 目标 α，开区间 (0,1)；夹到 0.60~0.95 是 GUI 滑条的事
        :param rng_seed: 传整数则整个计划可复现（同 seed 两次调用逐格相同）。
            ``None`` = 每次不同，走系统熵。注意 ``random.Random(seed)`` 是**实例**，
            本函数从不碰 ``random`` 模块本身的全局状态，多线程/多批次互不污染。
        :return: :class:`Plan`

    硬约束与降级（设计稿 §6，四条各有归属）
        * 结构非法一律 ``ValueError``：``α ∉ (0,1)``、``n < 2``、题号重复、
          配额长度 ≠ 选项数、配额含负、**配额总和 ≠ n**。
        * 选项数 < 2 的题：排除、记进 ``excluded`` 与 ``notes``，其余题照常计划。
        * 参与题数 < :data:`MIN_ITEMS_FOR_ALPHA`：**整个维度不参战** —— 返回一个
          ``rows`` 为空、``alpha_planned is None``（None 语义 = "没有这个数"，
          不是 "α = 0"）的 Plan，``notes`` 说明原因，调用侧一律退回加权随机。
        * ``n < 30``：仍出计划，但 notes 里明写"小样本下 σ_e 搜索在拟合噪声"。
        * 搜索后 α 仍不达标：**保边际**（配额逐题精确，一格未动），只在 notes 里
          写"α 未达标：计划 x.xxx / 目标 y.yy"，并给出本组配额下的可达 α 区间。
          绝不为了凑 α 去改配额 —— 见函数末尾"边际优先"那一段。
    """
    total = int(n)
    if total < 2:
        raise ValueError(
            f"份数 n={n!r} 非法：α 靠份与份之间的总分方差来分解，至少要 2 份"
            "（``reliability.cronbach_alpha`` 对 n<2 也是直接返回 None）"
        )
    target = _require_unit_alpha(alpha_target, "alpha_target")
    checked = _check_specs(specs, total)

    notes: list[str] = []
    excluded: list[tuple[int, str]] = []
    items: list[ItemSpec] = []
    for spec in checked:
        if spec.options < 2:
            reason = (
                f"题 {spec.qnum} 不参与：选项数 {spec.options} < 2，"
                "只有一个选项的题谈不上内部一致性（设计稿 §6）"
            )
            excluded.append((spec.qnum, reason))
            notes.append(reason)
            continue
        items.append(spec)

    k = len(items)
    if k < MIN_ITEMS_FOR_ALPHA:
        reason = (
            f"参与题数 {k} < {MIN_ITEMS_FOR_ALPHA}，整个维度不做计划"
            "（设计稿 §6：k<3 时 α 无意义，报了就是骗人）；"
            "这几道题连同本批次一律退回现有的逐题加权随机，不做半计划半随机"
        )
        notes.append(reason)
        for spec in items:
            excluded.append((spec.qnum, reason))
        # rows 留空 → choice_for 恒返回 None → 接线侧自然走回退分支。
        return Plan(
            n=total, k=0, alpha_target=target, alpha_planned=None, sigma_e=None,
            excluded=excluded, notes=notes,
        )

    if total < SMALL_SAMPLE_N:
        notes.append(
            f"份数 n={total} < {SMALL_SAMPLE_N}：小样本下 σ_e 搜索是在拟合噪声"
            "（设计稿 §6 建议这种维度只报实测、不做控制），下面的计划值只当近似看"
        )

    analytic = alpha_to_sigma(target, k)
    rng = random.Random(rng_seed)
    theta = [rng.gauss(0.0, 1.0) for _ in range(total)]
    noise = [[rng.gauss(0.0, 1.0) for _ in range(total)] for _ in range(k)]
    option_maps = [_rank_to_option(spec.quota) for spec in items]
    signs = [-1.0 if spec.reverse else 1.0 for spec in items]

    def materialize(sigma: float) -> list[tuple[int, ...]]:
        """给定 σ_e，按秩映射出 k × n 的选项索引矩阵（配额精确是这里的构造性质）。"""
        rows: list[tuple[int, ...]] = []
        for j in range(k):
            scores = [signs[j] * theta[i] + sigma * noise[j][i] for i in range(total)]
            ranked = sorted(range(total), key=scores.__getitem__)
            row = [0] * total
            for rank, sample in enumerate(ranked):
                row[sample] = option_maps[j][rank]
            rows.append(tuple(row))
        return rows

    def planned_alpha(rows: Sequence[Sequence[int]]) -> Optional[float]:
        """映射后的选项索引 → 等距分（+1，与 P0 的 ``ordinal_score`` 同基），
        反向题按声明取负号，再交给 **P0 那份** ``cronbach_alpha`` 算（不复算一套）。"""
        matrix: list[list[float]] = []
        for j in range(k):
            scores = [float(v) + 1.0 for v in rows[j]]
            if items[j].reverse:
                scores = [-v for v in scores]
            matrix.append(scores)
        return cronbach_alpha(matrix)

    cache: dict[float, Optional[float]] = {}

    def evaluate(sigma: float) -> Optional[float]:
        """候选 σ_e 的 α。同一个 σ_e 只扫一遍（细化时会反复踩到已算过的点）。"""
        if sigma not in cache:
            cache[sigma] = planned_alpha(materialize(sigma))
        return cache[sigma]

    def search() -> _Search:
        """以解析值为中心的 9 个候选 + 跨零区间线性细化（≤4 次，含端点探针）。"""
        grid = sorted({analytic * factor for factor in _SIGMA_FACTORS})
        scored: list[tuple[float, float]] = []
        for sigma in grid:
            alpha = evaluate(sigma)
            if alpha is not None:
                scored.append((sigma, alpha))

        budget = _MAX_REFINEMENTS
        if not scored:
            # 一个 α 都算不出来 = 总分零方差（配额把每题都压进同一个选项之类）。
            # 这不挡出计划：配额仍然是精确的，只是没有 α 可声明。
            return _Search(analytic, None, None, None, len(cache), False)

        best_sigma, best_alpha = min(scored, key=lambda pair: abs(pair[1] - target))
        achieved = [a for _s, a in scored]

        # α(σ_e) 随 σ_e 单调下降（噪声越大题间互相关越小），所以"相邻两点把 target
        # 夹在中间"就是细化窗口。取第一个跨零区间；浮点噪声造成的局部反号在这里
        # 最多让窗口选偏一格，后面 4 次线性内插会收回来。
        bracket: Optional[tuple[tuple[float, float], tuple[float, float]]] = None
        for lo, hi in zip(scored, scored[1:]):
            if (lo[1] - target) * (hi[1] - target) <= 0.0:
                bracket = (lo, hi)
                break

        if bracket is None and target > scored[0][1] and scored[0][0] > 0.0:
            # 目标比最小候选的 α 还高：把 σ_e=0（零误差端点，可达 α 的上确界）
            # 拉进区间。这一次探针算细化的第一次 —— 配额很粗时（例如 1 份 vs 99 份）
            # 目标只有零误差端点附近可达，不探针就会静默停在"最好的候选其实差得远"。
            zero_alpha = evaluate(0.0)
            budget -= 1
            if zero_alpha is not None:
                bracket = ((0.0, zero_alpha), scored[0])
                if zero_alpha > best_alpha:
                    best_sigma, best_alpha = 0.0, zero_alpha
                achieved.append(zero_alpha)

        while bracket is not None and budget > 0 and abs(best_alpha - target) > ALPHA_STOP_TOLERANCE:
            (lo_sigma, lo_alpha), (hi_sigma, hi_alpha) = bracket
            if hi_sigma == lo_sigma or hi_alpha == lo_alpha:
                break                     # 区间已经塌成一个点，再插也是原地踏步
            mid = lo_sigma + (target - lo_alpha) * (hi_sigma - lo_sigma) / (hi_alpha - lo_alpha)
            mid = min(max(mid, lo_sigma), hi_sigma)     # 钳回区间内：α(σ) 只是近线性
            mid_alpha = evaluate(mid)
            budget -= 1
            if mid_alpha is None:
                break
            achieved.append(mid_alpha)
            if abs(mid_alpha - target) < abs(best_alpha - target):
                best_sigma, best_alpha = mid, mid_alpha
            # 收窄：α 偏大说明噪声还不够，往 σ_e 上端走；反之往下端走。
            if mid_alpha > target:
                bracket = ((mid, mid_alpha), (hi_sigma, hi_alpha))
            else:
                bracket = ((lo_sigma, lo_alpha), (mid, mid_alpha))

        return _Search(
            sigma=best_sigma,
            alpha=best_alpha,
            achieved_min=min(achieved),
            achieved_max=max(achieved),
            evaluations=len(cache),
            bracketed=bracket is not None,
        )

    found = search()
    rows = materialize(found.sigma)

    planned: dict[int, tuple[int, ...]] = {}
    quotas: dict[int, tuple[int, ...]] = {}
    for spec, row in zip(items, rows):
        planned[spec.qnum] = row
        quotas[spec.qnum] = tuple(spec.quota)

    # ---- 边际优先（设计稿 §6 的取舍，也是本函数唯一的行为承诺）----------------
    # 搜索到这里已经结束了：达标就报达标，不达标**也只往 notes 里写一句话**。
    # rows 来自 materialize(found.sigma)，而 materialize 只做秩映射 ——
    # 配额向量是它唯一的真相来源，整个过程里被改动的自变量只有 σ_e。
    # 所以无论 α 落不落在目标上，逐题配额都是精确的；绝不为了把 α 凑上去动一格。
    # 下面这段是把这件事**验证**一遍（正常永不触发，触发即内核 bug），
    # 而不是给它一条"那就牺牲配额"的退路。
    for spec, row in zip(items, rows):
        realized = _realized_counts(row, spec.options)
        if realized != tuple(spec.quota):
            notes.append(
                f"题 {spec.qnum} 的配额未精确落地：实际 {list(realized)} ≠ "
                f"目标 {list(spec.quota)} —— 内核 bug，请带 seed 复现后上报"
            )

    if found.alpha is None:
        notes.append(
            "α 算不出：候选得分矩阵的总分零方差（配额把题都压到了同一个选项上？）。"
            "计划矩阵仍然逐题精确兑现配额，只是不声明任何 α"
        )
    elif abs(found.alpha - target) <= ALPHA_OK_TOLERANCE:
        notes.append(
            f"σ_e 搜索达标：目标 α={target:.3f} → 计划 α={found.alpha:.3f}"
            f"（σ_e={found.sigma:.4f}，解析值 {analytic:.4f}，"
            f"{found.evaluations} 次全量扫描）"
        )
    else:
        notes.append(
            f"α 未达标：计划 {found.alpha:.3f} / 目标 {target:.2f}"
            " —— 配额已按用户设定精确保留，一格未动（设计稿 §6：保边际，"
            "α 只报告不强行凑）"
        )
        if found.achieved_min is not None and found.achieved_max is not None:
            notes.append(
                f"本组配额下可达 α 约 [{found.achieved_min:.3f}, {found.achieved_max:.3f}]"
                f"，σ_e 搜索用了 {found.evaluations} 次全量扫描"
                + ("；目标在可达区间之外，多半是配额太粗（选项数少或分段极端）"
                   if not found.bracketed else "")
            )
    reversed_qnums = [spec.qnum for spec in items if spec.reverse]
    if reversed_qnums:
        notes.append(
            f"反向题 {reversed_qnums} 按 −θ 生成，计划 α 也已按声明翻转"
            "（对行取负号，与 measure_dimensions 的 (obs_min+obs_max)−x 翻转在 α 上等价）"
            f"；实测口径必须传 reverse={reversed_qnums}，漏标会把 α 打成负数（设计稿 §4）"
        )
    else:
        notes.append(
            "本维度全部为正向题：若实测 α 异常高，先核对是否漏标反向（设计稿 §4）"
        )

    return Plan(
        n=total,
        k=k,
        alpha_target=target,
        alpha_planned=found.alpha,
        sigma_e=found.sigma,
        rows=planned,
        quotas=quotas,
        reversed_items=reversed_qnums,
        excluded=excluded,
        notes=notes,
        evaluations=found.evaluations,
    )


# ============================================================================
#  本次批次的计划状态：CLI ``--alpha-target`` 开起来，生成器逐题来问
# ============================================================================
#: 能等距计分、且计划矩阵能兑现的题型。多选 / 排序 / 矩阵行**不参与**（设计稿 §1
#: 非目标：它们没有自然的等距分数，硬套出来的 α 是伪科学而不是低精度科学）。
PARTICIPATING_TYPES = ("single", "scale", "rating", "dropdown")

#: 少于这个题数，α 这个数本身就没有意义（k=2 的 Cronbach α 完全由两题的偶然相关决定）。
MIN_ITEMS = 3

#: 少于这个份数，一维搜索是在拟合噪声 —— 直接不建计划，只报"样本不够"。
MIN_SAMPLES = 30


@dataclass
class _Session:
    alpha_target: float
    total: int
    plan: Optional[Plan] = None
    built: bool = False
    notes_shown: bool = False
    submission_index: Optional[int] = None


_session: _Session | None = None


def configure(alpha_target: float, total_submissions: int) -> None:
    """开启信度控制：目标 α 与本次要投的总份数。"""
    global _session
    _session = _Session(alpha_target=float(alpha_target), total=int(total_submissions))


def end_session() -> None:
    global _session
    _session = None


def reset_survey() -> None:
    """换一份问卷：留着目标 α 与总份数，丢掉按当前问卷建出来的计划。

    ``ensure_plan`` 的"只建一次"是为**一份问卷的分页**服务的（第二页再建会把第一页
    已兑现的行改掉），而 ``--url-file`` 队列里每份问卷题号都从 1 重新开始 —— 不清的
    症状是第二份卷沿用第一份的配额，或者干脆不建也听不到一句原因。
    """
    if _session is not None:
        _session.plan = None
        _session.built = False
        _session.submission_index = None


def configured() -> bool:
    return _session is not None


def begin_submission(submission_index: int | None) -> None:
    """告诉计划"这一份是第几份"。

    刻意不把 index 一路传进 ``generate_answer``：那条签名要穿过 CLI / GUI / 分页 /
    逐题四层，而"当前正在答第几份"本来就是单线程一次提交内的一个事实。传参换来的是
    四层改动，换来的收益是零。
    """
    if _session is not None:
        _session.submission_index = (
            None if submission_index is None else int(submission_index)
        )


def _option_count(q: Mapping[str, Any]) -> int:
    qtype = str(q.get("type", "single")).lower()
    if qtype in ("scale", "rating"):
        lo = int(q.get("scale_min", 1))
        return int(q.get("scale", 5)) - lo + 1
    return len(list(q.get("choices") or []))


def _quotas(weights: Sequence[float], n: int) -> list[int]:
    """把目标权重精确摊成 n 份（最大余数法）。

    摊不成整数是事实，不是可以四舍五入掉的小数：``[1,1,1] × 10`` 只能落 4/3/3，
    差的那一份落在哪个选项上由余数大小决定，且必须可复现。
    """
    total_w = float(sum(weights))
    if total_w <= 0:
        weights = [1.0] * len(weights)
        total_w = float(len(weights))
    raw = [float(w) / total_w * n for w in weights]
    base = [int(math.floor(v)) for v in raw]
    left = n - sum(base)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - base[i]), i))
    for i in range(max(left, 0)):
        base[order[i % len(order)]] += 1
    return base


def ensure_plan(questions: Sequence[Mapping[str, Any]]) -> list[str]:
    """第一次拿到探测结果时建计划；建不出来就返回一句人话说明为什么不建。

    只建一次：多页问卷每页都会调这里，而计划必须覆盖整批 —— 第二页再建一遍会把
    第一页已经答过的行改掉，症状是配额被兑现两次。
    """
    session = _session
    if session is None or session.built:
        return []
    session.built = True
    if session.total < MIN_SAMPLES:
        return [f"[信度] 本次只投 {session.total} 份，少于 {MIN_SAMPLES} 份 "
                "→ 样本不够，一维搜索是在拟合噪声，不建计划"]

    declared = dimensions_from_config(WEIGHT_CONFIG)
    if not declared:
        return ["[信度] 权重配置里没有任何 dimension 声明 → 维度归属必须人写，"
                "自动聚类会把不相干的题算进同一个漂亮的数字，不建计划"]
    reverse = set(reverse_from_config(WEIGHT_CONFIG))

    by_qnum = {int(q["q"]): q for q in questions if q.get("q") is not None}
    specs: list[ItemSpec] = []
    items: list[int] = []
    for name, qnums in declared.items():
        if len(qnums) < MIN_ITEMS:
            return [f"[信度] 维度「{name}」只有 {len(qnums)} 题（少于 {MIN_ITEMS}）"
                    " → 不建计划"]
        for qnum in qnums:
            q = by_qnum.get(qnum)
            if q is None:
                return [f"[信度] 维度「{name}」声明的 Q{qnum} 在这份问卷上没探测到 "
                        "→ 计划与题量对不上，整批退回加权随机"]
            if str(q.get("type", "")).lower() not in PARTICIPATING_TYPES:
                return [f"[信度] Q{qnum}（维度「{name}」）是 "
                        f"{q.get('type')} 题，不能等距计分 → 不建计划"]
            options = _option_count(q)
            if options < 2:
                return [f"[信度] Q{qnum}（维度「{name}」）只有 {options} 个可选项 "
                        "→ 不建计划"]
            specs.append(ItemSpec(
                qnum=qnum, options=options,
                quota=_quotas(_weights_for(q), session.total),
                reverse=qnum in reverse,
            ))
            items.append(qnum)

    try:
        plan = build_plan(specs, session.total, session.alpha_target)
    except ValueError as exc:
        return [f"[信度] 计划建不出来：{exc}"]
    session.plan = plan
    notes = [f"[信度] 计划已建：{len(set(items))} 题参与，目标 α = "
             f"{session.alpha_target:.2f}，计划 α = "
             + ("不可计算" if plan.alpha_planned is None
                else f"{plan.alpha_planned:.3f}")
             + f"（参与题号 {sorted(set(items))}，按秩映射兑现配额）"]
    return notes + [f"[信度]   {n}" for n in plan.notes]


def _weights_for(q: Mapping[str, Any]) -> list[float]:
    """该题的目标权重；没配就是等权（计划要兑现的边际与生成器用的是同一份配置）。"""
    cfg = WEIGHT_CONFIG.get(int(q["q"])) if q.get("q") is not None else None
    raw = q.get("weights") or (cfg.get("weights") if isinstance(cfg, dict) else None)
    n = _option_count(q)
    try:
        w = [float(x) for x in (raw or [])]
    except (TypeError, ValueError):
        w = []
    return w if len(w) == n and sum(v for v in w if v > 0) > 0 else [1.0] * n


def forced_choice(qnum: int | None) -> int | None:
    """这一题这一份按计划该选第几个选项（0-based）；``None`` = 不干预。"""
    session = _session
    if session is None or session.plan is None or qnum is None:
        return None
    if session.submission_index is None:
        return None
    return session.plan.choice_for(int(qnum), int(session.submission_index))
