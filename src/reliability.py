"""v3.3 — 信度测量（Cronbach α，设计稿 P0「只测不改」）。

对应 ``DESIGN_reliability_alpha.md`` §5 P0：从历史库读已录入的答案，按维度报出
**实测** α。本阶段不改任何答题行为 —— 这些数字是用来决定 P1（计划矩阵 + 按秩
映射）值不值得立项的，所以"如实"比"好看"重要。

口径限制（必须显式声明，不要假装它不存在）
    ``answers`` 表唯一的写入口是**作答阶段**的 ``question_stage._answer_one_question``
    （逐题 record_answer，点提交之前），而 ``runs`` 只有整批的
    ``success_count`` / ``fail_count`` 计数 —— schema 里**没有**「这一份最终提交
    成功了没有」的逐份标记。因此本模块测出的 α 把失败 / UNKNOWN 的那几份也算了
    进去（它们可能根本没到达平台）。这是落库时机决定的既有上界，不是本模块的选择；
    每个 :class:`DimensionReport` 都随身携带 ``scope_caveat`` 一行说明。
    接缝已留好：:func:`measure_dimensions` 的 ``submission_filter`` 参数
    （按 submission_index 过滤）今天恒为 None，P2 若给逐份补上状态列，
    调用处不必再动签名。

其余决策的出处都在设计稿里：k<3 的维度不参与（§6「报了就是骗人」）、
填空/多选不参与 α（§1 非目标）、反向题必须显式声明（§4）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Mapping, Optional, Sequence

from .history import SubmissionHistory

__all__ = [
    "DimensionReport",
    "ExcludedItem",
    "cronbach_alpha",
    "measure_dimensions",
    "ordinal_score",
]

# 有自然等距分数的题型（history 归一化名）。其余一律不参与 α ——
# 设计稿 §1：多选/排序/填空没有自然的等距计分，硬套是伪科学。
_SCORABLE_TYPES = frozenset({"scale", "single", "dropdown", "matrix"})

# 每个报告都带的一句话。放进常量而不是散落措辞，是为了让测试能钉住它 ——
# 哪天 answers 表真有了逐份状态，这条常量的删除会先把测试弄红，
# 而不是让口径声明静默蒸发。
_SCOPE_CAVEAT: str = (
    "历史库无逐份成功标记（answers 在作答阶段落库、早于点提交），"
    "本 α 含 failed/UNKNOWN 那几份 —— 见 src/reliability.py 模块 docstring 的口径限制"
)

# query_answers 自带 LIMIT（默认 5000），一份 100×60 题的批次就会撞上。
# 这里显式传一个大值：静默吃截断 = 部分样本冒充全样本，比报错更糟。
_ANSWER_SCAN_LIMIT = 1_000_000


# ============================================================================
#  数学层
# ============================================================================
def cronbach_alpha(items: Sequence[Sequence[float]]) -> float | None:
    """k 行（题）× n 列（份）矩阵的 Cronbach α：``α = k/(k-1)·(1 − ΣVar(Xi)/Var(S))``。

    信息不足一律返回 None —— 不抛，也不用 0 冒充结果（0 会被读成"题之间不相关"，
    而真相是"数据不足以回答这个问题"，两者是完全不同的结论）：

      * ``k < 2``：定义里有 k/(k-1)，单题谈不上内部一致性；
      * ``n < 2``：一个样本没有方差；
      * ``Var(S) == 0``：所有份总分一模一样，没有方差可分解；
      * 行长度不一致：上游拼矩阵时漏了格（缺答案应在调用侧按完整份过滤掉，
        走到这里还长短不一就是 bug，不该被静默"取齐"）。

    α 允许为**负**（漏翻反向题、把不相干的题混进一个维度都会），也不夹到 [0,1]。
    P0 的全部价值就在这些负数上 —— 替读者把它夹回 0，就正好抹掉了设计稿 §4
    想暴露的"漏标反向"信号。
    """
    k = len(items)
    if k < 2:
        return None
    n = len(items[0])
    if n < 2:
        return None
    if any(len(row) != n for row in items):
        return None

    def _var(xs: Sequence[float]) -> float:
        # 两遍式（先均值再离差平方和），不用 E[X²]-E[X]² 的一遍式：
        # 后者在均值远大于方差时灾难性抵消。n 就几百，省这点遍历不值当。
        mean = math.fsum(xs) / n
        return math.fsum((x - mean) ** 2 for x in xs) / (n - 1)

    item_var_sum = math.fsum(_var(row) for row in items)
    totals = [math.fsum(col) for col in zip(*items)]
    var_total = _var(totals)
    if var_total <= 0.0:
        return None
    return (k / (k - 1)) * (1.0 - item_var_sum / var_total)


def ordinal_score(
    question_type: str,
    options_selected: list[int] | None,
    text_answer: str | None,
    total_options: int | None,
) -> float | None:
    """把历史库里的一格答案折成等距分；折不出（不参与 α）返回 None。

    scale / single / dropdown / matrix 存的是 0-based 选中索引
    （``history._SCHEMA_SQL`` 的注释即口径来源），+1 即等距分。

    两个刻意不折的情形：
      * ``len(options_selected) != 1``：多选没有唯一等距分（§1 非目标）；
        matrix 今天一题一条、多行索引混存在 ``options_selected`` 里
        （§3 接缝表），一行一个分数还没落库之前同样折不出 —— 宁缺毋伪。
      * ``total_options`` 给定且索引越界：题面选项数变过，硬折会凭空扩值域。
        （历史库本身不存选项数，所以 ``measure_dimensions`` 只能传 None，
        这个校验是给未来拿得到结构的调用方用的。）

    ``text_answer`` 不参与本函数的折算（填空按 §1 直接排除），保留参数是为了
    和 answers 行形状一一对应，调用侧不必挑列。
    """
    qtype = (question_type or "").strip().lower()
    if qtype not in _SCORABLE_TYPES:
        return None
    if not options_selected or len(options_selected) != 1:
        return None
    idx = options_selected[0]
    if idx < 0:
        return None
    if total_options is not None and idx >= total_options:
        return None
    return float(idx) + 1.0


# ============================================================================
#  报告模型
# ============================================================================
@dataclass(slots=True)
class ExcludedItem:
    """一题被排除出某维度：题号 + 给人看的理由。"""

    question_number: int
    reason: str


@dataclass(slots=True)
class DimensionReport:
    """一个维度的实测结果。``alpha is None`` 时看 ``k`` / ``excluded`` / ``notes`` 找原因。"""

    dimension: str
    k: int                          # 实际参与 α 的题数（None 场景下为 0 或 usable 数）
    n: int                          # 实际参与 α 的完整份数（k 题都有分的份）
    alpha: Optional[float]
    excluded: list[ExcludedItem] = field(default_factory=list)
    reversed_items: list[int] = field(default_factory=list)
    observed_range: Optional[tuple[float, float]] = None
    notes: list[str] = field(default_factory=list)
    scope_caveat: str = _SCOPE_CAVEAT   # 口径限制常驻字段，别等读者去翻 docstring


# ============================================================================
#  历史库 → 实测 α
# ============================================================================
def _exclusion_reason(
    qnum: int,
    types_seen: Mapping[int, set[str]],
) -> str:
    """qnum 一题都没折出分时，把原因翻成可读的一句话。

    值得区分"库里根本没这题"和"有但折不出"：前者是维度配置写错了题号，
    后者是题型不参战 —— 给用户的下一步动作完全不同。
    """
    seen = types_seen.get(qnum)
    if not seen:
        return "无有效答案：历史库里没有这道题的记录（题号写错还是批次没探到它？）"
    if seen.isdisjoint(_SCORABLE_TYPES):
        return f"题型不参与：{ '、'.join(sorted(seen)) or '未标注题型' }（设计稿 §1 非目标）"
    return "无有效答案：有记录，但没有一格能折成等距分（多选/索引 malformed）"


def measure_dimensions(
    history_db: SubmissionHistory,
    run_id: int,
    dimensions: Mapping[str, Sequence[int]],
    reverse: Collection[int] = (),
    submission_filter: Optional[Callable[[int], bool]] = None,
) -> list[DimensionReport]:
    """按维度分组，从某 run 的已落库答案算**实测** α（只读，不改任何行为）。

    :param dimensions: 「维度名 → 题号列表」。设计稿 §4：维度归属必须显式声明，
        自动聚类会静默把不相干的题算进同一个漂亮的数字里。
    :param reverse: 反向题题号。翻转用 ``flip = (max + min) - x``，min/max 取自
        **该维度的观测值域**而不是题面满分 —— 历史库没存题面满分（answers 只有
        0-based 索引，没有选项数），这是当前唯一可行的口径，报告 ``notes`` 里
        会注明；某极端选项从未被选到时，翻转因此可能整体偏心。
    :param submission_filter: 预留位，按 ``submission_index`` 过滤「哪些份算数」。
        **今天恒传 None** —— schema 里没有逐份成败标记（见模块 docstring 的
        口径限制），传别的也是无据可滤。P2 补上逐份状态后由它接入，
        本函数与调用方的签名都不用变。

    缺失答案（某题某份没记录）按**完整份**取交集处理：α 需要同一个人的一排分，
    把不同人混拼进一列会虚增方差 —— 少算几份是对的，拼出来才是错的。
    """
    rows = history_db.query_answers(run_id=run_id, limit=_ANSWER_SCAN_LIMIT)

    # scores[qnum][submission_index] = 等距分；types_seen 只为排除理由服务
    scores: dict[int, dict[int, float]] = {}
    types_seen: dict[int, set[str]] = {}
    for row in rows:
        try:
            qnum = int(row["question_number"])
            sub = int(row["submission_index"])
        except (TypeError, ValueError):
            continue
        if submission_filter is not None and not submission_filter(sub):
            continue
        qtype = str(row["question_type"] or "")
        types_seen.setdefault(qnum, set()).add(qtype.strip().lower())

        parsed: Optional[list[int]] = None
        raw_opts = row["options_selected"]
        if raw_opts is not None:
            try:
                loaded = json.loads(raw_opts)
            except (TypeError, ValueError):
                loaded = None
            if isinstance(loaded, list):
                # 排序题等会在这一列混存字符串（question_stage 的 sort 分支），
                # 数值化在这里挡掉，不让 TypeError 传染到折算层
                parsed = [
                    int(v) for v in loaded
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                ]
        raw_text = row["text_answer"]
        score = ordinal_score(
            qtype,
            parsed,
            str(raw_text) if raw_text is not None else None,
            None,   # 历史库不存选项数，见 ordinal_score 的 docstring
        )
        if score is not None:
            scores.setdefault(qnum, {})[sub] = score

    reverse_set = {int(q) for q in reverse}
    reports: list[DimensionReport] = []
    for name, qnums in dimensions.items():
        ordered = list(dict.fromkeys(int(q) for q in qnums))   # 去重且保声明顺序
        usable = [q for q in ordered if scores.get(q)]

        if len(usable) < 3:
            # k<3 不参战（§6：α 无意义，报了就是骗人）。被"题数不够"连带排除的
            # 题也要各自有一条理由，不然报告只说"没算"，不说"为什么没得算"。
            excluded = [
                ExcludedItem(
                    q,
                    _exclusion_reason(q, types_seen)
                    if q not in usable
                    else f"维度可用题数 {len(usable)} < 3，α 无意义（设计稿 §6）",
                )
                for q in ordered
            ]
            reports.append(DimensionReport(name, k=0, n=0, alpha=None, excluded=excluded))
            continue

        # 完整份：该维度每题都有分的 submission_index 交集（见 docstring 末段）
        common: Optional[set[int]] = None
        for q in usable:
            keys = set(scores[q])
            common = keys if common is None else (common & keys)
        subs = sorted(common or ())

        excluded = [
            ExcludedItem(q, _exclusion_reason(q, types_seen))
            for q in ordered if q not in usable
        ]
        if len(subs) < 2:
            reports.append(DimensionReport(
                name, k=len(usable), n=len(subs), alpha=None, excluded=excluded,
                notes=["完整份数 < 2，没有方差可分解（哪几题缺了哪几份见上）"],
            ))
            continue

        matrix = [[float(scores[q][s]) for s in subs] for q in usable]
        obs_min = min(min(r) for r in matrix)
        obs_max = max(max(r) for r in matrix)

        rev_applied = [q for q in usable if q in reverse_set]
        if rev_applied:
            flip = obs_min + obs_max
            matrix = [
                [flip - x for x in row] if q in reverse_set else row
                for q, row in zip(usable, matrix)
            ]

        notes: list[str] = []
        if rev_applied:
            notes.append(
                f"反向题 {rev_applied} 已按观测值域 [{obs_min:g}, {obs_max:g}] 翻转 "
                "—— 用观测值域而非题面满分：历史库不存选项数（口径见模块 docstring）"
            )
        reports.append(DimensionReport(
            name,
            k=len(usable),
            n=len(subs),
            alpha=cronbach_alpha(matrix),
            excluded=excluded,
            reversed_items=rev_applied,
            observed_range=(obs_min, obs_max),
            notes=notes,
        ))
    return reports


# ============================================================================
#  CLI ``--report-alpha`` 用的那一层：维度从哪来、报告怎么印
# ============================================================================
#: 能折成等距分数的题型。多选 / 排序 / 填空不参与（设计稿 §1 的非目标）。
ALPHA_TYPES = ("scale", "single", "dropdown", "matrix_single", "matrix")

UNDECLARED = "未声明维度（全部量表/单选题）"


def dimensions_from_config(
    weight_config: Mapping[Any, Any] | None,
) -> dict[str, list[int]]:
    """从权重配置里读**人显式声明**的维度归属（设计稿 §4）。"""
    out: dict[str, list[int]] = {}
    for qnum, cfg in (weight_config or {}).items():
        if not isinstance(cfg, dict):
            continue
        name = str(cfg.get("dimension") or "").strip()
        if not name:
            continue
        try:
            out.setdefault(name, []).append(int(qnum))
        except (TypeError, ValueError):
            continue
    return {k: sorted(set(v)) for k, v in out.items()}


def reverse_from_config(weight_config: Mapping[Any, Any] | None) -> list[int]:
    """权重配置里标了 ``reverse: true`` 的题号（反向题必须人声明，理由同上）。"""
    out: list[int] = []
    for qnum, cfg in (weight_config or {}).items():
        if isinstance(cfg, dict) and bool(cfg.get("reverse")):
            try:
                out.append(int(qnum))
            except (TypeError, ValueError):
                continue
    return sorted(set(out))


def implicit_dimension(
    history_db: SubmissionHistory, run_id: int
) -> dict[str, list[int]]:
    """一份配置都没声明维度时的兜底分组。

    名字里就写着"未声明维度" —— 这个数**不能**被当成某个构念的信度来引用，
    它只是"这批量表题彼此相关到什么程度"。把它悄悄叫"满意度信度"才是造假。
    """
    rows = history_db.query_answers(run_id=run_id)
    qnums = sorted({
        int(r["question_number"]) for r in rows
        if str(r["question_type"] or "") in ALPHA_TYPES
    })
    return {UNDECLARED: qnums} if len(qnums) >= 2 else {}


def format_reports(reports: Sequence[DimensionReport]) -> list[str]:
    """把报告摊成可打印的行（含口径限制那一行，不许蒸发）。"""
    out: list[str] = []
    for rep in reports:
        alpha = "不可计算" if rep.alpha is None else f"{rep.alpha:.3f}"
        line = f"[信度] 维度「{rep.dimension}」k={rep.k} n={rep.n} 实测 α = {alpha}"
        if rep.reversed_items:
            line += f"（反向题已翻转: {rep.reversed_items}）"
        out.append(line)
        for ex in rep.excluded:
            out.append(f"        排除 Q{ex.question_number}: {ex.reason}")
        if rep.scope_caveat:
            out.append(f"        口径: {rep.scope_caveat}")
    return out
