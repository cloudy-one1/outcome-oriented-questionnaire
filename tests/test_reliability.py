"""v3.3 信度测量（P0 只测不改）的离线契约测试。

两条总原则：

  1. **数学用独立推导的期望值验，不拿被测函数自己证自己。**
     已知小矩阵的 α 是纸面上按定义式算出来的（注释里留了推导），
     交叉实现 ``_alpha_reference`` 刻意换成母体方差（分母 n）+ 列向累加，
     与被测函数的样本方差（分母 n-1）+ zip 转置共享的代码路径越少越好。
  2. **口径限制本身就是被测行为。** 历史库没有逐份成败标记 →
     每个报告必须随身带着那条声明（``scope_caveat``），
     ``submission_filter`` 接缝必须真实生效（传了就得少算）。
     哪天 answers 表补上逐份状态，把这条限制删掉的测试会先红。
"""

from __future__ import annotations

import math
import os
import random
import sys
from typing import Optional

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import reliability  # noqa: E402  维度/报告那一层按模块名调，读起来不像裸函数
from src.history import SubmissionHistory  # noqa: E402
from src.reliability import (  # noqa: E402
    DimensionReport,
    cronbach_alpha,
    measure_dimensions,
    ordinal_score,
)


# ============================================================================
#  独立参考实现（与被测函数换一条算路）
# ============================================================================
def _alpha_reference(items: list[list[float]]) -> float:
    """定义式 ``α = k/(k-1)·(1 − ΣVar(Xi)/Var(S))`` 的独立实现。

    与实现侧的两处刻意不同：这里用**母体方差**（分母 n），实现侧用样本方差
    （分母 n-1）——比值里分母恰好约掉，所以两者应给同一个 α；如果哪天有人
    把公式里的一处 n-1 改错，这个差异就会把 bug 暴露成红测试而不是红差值。
    """
    k = len(items)
    n = len(items[0])

    def pvar(xs: list[float]) -> float:
        m = sum(xs) / n
        return sum((x - m) ** 2 for x in xs) / n

    totals = [sum(items[i][j] for i in range(k)) for j in range(n)]
    return k / (k - 1) * (1.0 - sum(pvar(list(row)) for row in items) / pvar(totals))


# ============================================================================
#  数学层：cronbach_alpha
# ============================================================================
def test_perfectly_parallel_matrix_has_alpha_one() -> None:
    """完全平行（每行都是同一组数）→ α=1。

    纸面推导：X1=...=Xk=x ⇒ Var(Xi)=v，S=kx ⇒ Var(S)=k²v，
    α = k/(k-1)·(1 − kv/k²v) = k/(k-1)·(k-1)/k = 1，与 k、n 无关。
    """
    row = [1.0, 2.0, 4.0, 8.0, 5.0]
    alpha = cronbach_alpha([list(row) for _ in range(5)])
    assert alpha is not None
    assert alpha == pytest.approx(1.0)


def test_total_zero_variance_returns_none() -> None:
    """所有份总分一模一样 → Var(S)=0，返回 None（不许用 0 冒充"不相关"）。"""
    assert cronbach_alpha([[3.0, 3.0, 3.0], [1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]) is None


def test_hand_derived_small_matrix_matches_definition() -> None:
    """3 题 × 4 份，期望值全部纸面算出（样本方差，分母 n-1）：

    X1=[1,2,3,4]：均值 2.5，离差平方和 2.25+0.25+0.25+2.25=5   → Var=5/3
    X2=[2,2,4,4]：均值 3，  离差平方和 1+1+1+1=4              → Var=4/3
    X3=[1,3,2,4]：均值 2.5，离差平方和 5                        → Var=5/3
    ΣVar = 14/3
    S  =[4,7,9,12]：均值 8，离差平方和 16+1+1+16=34            → Var=34/3
    α = 3/2 · (1 − (14/3)/(34/3)) = 3/2 · 20/34 = 15/17 ≈ 0.882353
    """
    items = [[1.0, 2.0, 3.0, 4.0], [2.0, 2.0, 4.0, 4.0], [1.0, 3.0, 2.0, 4.0]]
    alpha = cronbach_alpha(items)
    assert alpha is not None
    assert alpha == pytest.approx(15.0 / 17.0)
    assert alpha == pytest.approx(_alpha_reference(items))  # 两条算路互验


def test_single_constant_item_does_not_poison_alpha() -> None:
    """一题零方差 + 两题完全平行时 α 仍可算：X1=[3,3]（Var=0）、X2=X3=[1,2]（各 Var=0.5）、
    S=[5,7]（Var=2）→ α = 1.5·(1 − 1/2) = 0.75（纸面值）。
    """
    alpha = cronbach_alpha([[3.0, 3.0], [1.0, 2.0], [1.0, 2.0]])
    assert alpha == pytest.approx(0.75)


@pytest.mark.parametrize(
    "items",
    [
        [],                                        # 空输入
        [[1.0, 2.0, 3.0]],                         # k=1，α 定义里没有单题
        [[1.0], [2.0], [3.0]],                     # n=1，一个样本没有方差
        [[1.0, 2.0], [1.0, 2.0, 3.0]],             # 行长短不一（上游拼矩阵漏格 = bug）
    ],
    ids=["empty", "k=1", "n=1", "ragged"],
)
def test_insufficient_or_malformed_returns_none(items: list[list[float]]) -> None:
    assert cronbach_alpha(items) is None


def test_alpha_rises_with_k_at_fixed_rho() -> None:
    """平行测量模型 α = kρ/(1+ρ(k−1))：固定 ρ=0.5，k 越大 α 越高。

    构造按设计稿 §2：X = θ + σ_e·ε，σ_e = sqrt(1/ρ − 1)，θ、ε ~ N(0,1)。
    固定种子（rng = random.Random(...)，**不是** random 模块本身），
    断言两件事：单调性 + 每个 k 的实测离理论值不远（±0.06）。
    """
    rng = random.Random(20260924)
    n = 1500
    rho = 0.5
    sigma_e = math.sqrt(1.0 / rho - 1.0)
    ks = (4, 8, 16)

    observed: list[float] = []
    for k in ks:
        theta = [rng.gauss(0.0, 1.0) for _ in range(n)]
        items = [[t + sigma_e * rng.gauss(0.0, 1.0) for t in theta] for _ in range(k)]
        alpha = cronbach_alpha(items)
        assert alpha is not None
        observed.append(alpha)

    assert observed[0] < observed[1] < observed[2]
    expected = [k * rho / (1.0 + rho * (k - 1)) for k in ks]   # 0.800 / 0.889 / 0.941
    for got, exp in zip(observed, expected):
        assert got == pytest.approx(exp, abs=0.06)


# ============================================================================
#  折算层：ordinal_score
# ============================================================================
@pytest.mark.parametrize("qtype", ["scale", "single", "dropdown", "matrix", "SINGLE "])
def test_scorable_types_use_index_plus_one(qtype: str) -> None:
    assert ordinal_score(qtype, [3], None, None) == 4.0
    assert ordinal_score(qtype, [0], None, None) == 1.0


@pytest.mark.parametrize(
    "qtype, opts, text, total, expected",
    [
        ("text", None, "很满意", None, None),        # 填空：非目标
        ("multi", [0, 2], None, None, None),         # 多选：没有唯一等距分
        ("matrix", [1, 3], None, None, None),        # 矩阵多行索引混存，折不出
        ("sort", [2, 0, 1], None, None, None),       # 排序：非目标
        ("scale", None, "5", None, None),            # 该存索引却没存
        ("scale", [], None, None, None),             # 空列表
        ("scale", [-1], None, None, None),           # 畸形负索引
        ("scale", [7], None, 5, None),               # 越界：硬折会凭空扩值域
        ("", [1], None, None, None),                 # 题型缺失
    ],
    ids=["text", "multi", "matrix_rows", "sort", "opts_none", "opts_empty",
         "negative_idx", "out_of_range", "empty_type"],
)
def test_non_scorable_or_malformed_returns_none(
    qtype: str,
    opts: Optional[list[int]],
    text: Optional[str],
    total: Optional[int],
    expected: None,
) -> None:
    assert ordinal_score(qtype, opts, text, total) == expected


# ============================================================================
#  接缝层：measure_dimensions（SubmissionHistory 内存库造数据）
# ============================================================================
_URL = "https://v.wjx.cn/vm/reliability-test.aspx"


def _make_run(db: SubmissionHistory, total: int) -> int:
    return db.start_run(_URL, total, "edge", False)


def _record_grid(
    db: SubmissionHistory,
    run_id: int,
    qnum: int,
    qtype: str,
    per_submission: list[Optional[int]],
) -> None:
    """按「每份一个索引」落一列答案；None 表示这份**没有**该题记录（模拟缺格）。"""
    for sub, idx in enumerate(per_submission, start=1):
        if idx is None:
            continue
        db.record_answer(run_id, sub, qnum, qtype, [idx], None)


@pytest.fixture()
def db() -> SubmissionHistory:
    history = SubmissionHistory(":memory:")
    yield history
    history.close()


def test_dimension_split_and_exclusion_reasons(db: SubmissionHistory) -> None:
    """维度切分各算各的；text/multi 被排除且理由可分辨；k<3 的维度直接不参战。"""
    rid = _make_run(db, 5)
    base = [0, 1, 2, 3, 4]
    for q in (1, 2, 3):
        _record_grid(db, rid, q, "scale", list(base))
    for sub in range(1, 6):                                # q7：填空题，一律不参战
        db.record_answer(rid, sub, 7, "text", None, "填写内容")
    for q in (8, 9):
        _record_grid(db, rid, q, "scale", [1, 2, 3, 2, 1])
    db.record_answer(rid, 1, 12, "multi", [0, 2])          # 多选，题型不参战

    reports = measure_dimensions(
        db, rid,
        dimensions={"满意度": [1, 2, 3, 7], "意愿": [8, 9], "杂项": [12]},
    )
    by_name = {r.dimension: r for r in reports}

    happy = by_name["满意度"]
    assert happy.k == 3 and happy.n == 5
    assert happy.alpha is not None and happy.alpha == pytest.approx(1.0)
    (excluded,) = happy.excluded
    assert excluded.question_number == 7
    assert "题型不参与" in excluded.reason

    # 维度里只凑出 2 道可用题 → k<3，整维度不参战，且每题带着各自没参战的理由
    small = by_name["意愿"]
    assert small.k == 0 and small.alpha is None
    assert any("< 3" in e.reason for e in small.excluded)

    odd = by_name["杂项"]
    assert odd.k == 0 and odd.alpha is None
    assert "题型不参与" in odd.excluded[0].reason


def test_reverse_item_flip_rescues_alpha(db: SubmissionHistory) -> None:
    """q3 是反向题（值域 1~5 整体倒着存）：不声明 → α 为负；声明 → α=1。

    纸面推导（x ∈ {1..5}，Var(x)=2.5）：
      不翻转：X1=X2=x，X3=6−x ⇒ ΣVar=7.5，S=x+6 ⇒ Var(S)=2.5，
             α = 1.5·(1 − 7.5/2.5) = −3；
      翻转后：三行同为 x ⇒ α = 1。
    负数如实报出来正是 P0 要暴露的"漏标反向"信号。
    """
    rid = _make_run(db, 5)
    _record_grid(db, rid, 1, "scale", [0, 1, 2, 3, 4])
    _record_grid(db, rid, 2, "scale", [0, 1, 2, 3, 4])
    _record_grid(db, rid, 3, "scale", [4, 3, 2, 1, 0])   # 反向记录
    dims = {"构念": [1, 2, 3]}

    naive = measure_dimensions(db, rid, dimensions=dims)[0]
    assert naive.alpha is not None and naive.alpha == pytest.approx(-3.0)

    fixed = measure_dimensions(db, rid, dimensions=dims, reverse=[3])[0]
    assert fixed.alpha is not None and fixed.alpha == pytest.approx(1.0)
    assert fixed.reversed_items == [3]
    assert fixed.observed_range == (1.0, 5.0)
    # 口径要求：报告里必须注明翻的是观测值域、不是题面满分
    assert any("观测值域" in note for note in fixed.notes)


def test_missing_cell_drops_only_that_submission(db: SubmissionHistory) -> None:
    """某题某份没记录 → 按完整份取交集算，而不是崩、也不是把半截份混进来。

    独立期望值：缺 (q2, 第3份) 后，剩余 4 份（第 1/2/4/5 份）三题同列 [1,2,3,5]
    ⇒ 完全平行 ⇒ α=1，n=4。另一维度 q4 只有第 3 份有记录 → 交集只剩这 1 份
    → n=1 → 方差无从谈起 → α=None。
    """
    rid = _make_run(db, 5)
    _record_grid(db, rid, 1, "scale", [0, 1, 3, 2, 4])   # 分数 1,2,4,3,5
    _record_grid(db, rid, 2, "scale", [0, 1, None, 2, 4])
    _record_grid(db, rid, 3, "scale", [0, 1, 3, 2, 4])
    _record_grid(db, rid, 4, "scale", [None, None, 3, None, None])

    reports = measure_dimensions(
        db, rid,
        dimensions={"完整": [1, 2, 3], "稀疏": [1, 3, 4]},
    )
    by_name = {r.dimension: r for r in reports}

    full = by_name["完整"]
    assert full.k == 3 and full.n == 4
    assert full.alpha is not None and full.alpha == pytest.approx(1.0)

    sparse = by_name["稀疏"]
    assert sparse.k == 3 and sparse.n == 1 and sparse.alpha is None
    assert "完整份数" in " ".join(sparse.notes)


def test_single_complete_submission_returns_none(db: SubmissionHistory) -> None:
    """只剩 1 份完整记录（缺 3 份）：n=1，方差无从谈起 → α=None 而不是崩。"""
    rid = _make_run(db, 4)
    _record_grid(db, rid, 1, "scale", [0, None, None, None])
    _record_grid(db, rid, 2, "scale", [1, None, None, None])
    _record_grid(db, rid, 3, "scale", [2, None, None, None])
    report = measure_dimensions(db, rid, dimensions={"只有独苗": [1, 2, 3]})[0]
    assert report.k == 3 and report.n == 1 and report.alpha is None


def test_submission_filter_hook_is_live(db: SubmissionHistory) -> None:
    """submission_filter 今天恒 None，但接缝必须真实生效：过滤后 n 变小、α 仍可算。

    P2 拿到逐份成败标记后就从这里接入（见模块 docstring 的口径限制）。
    """
    rid = _make_run(db, 5)
    _record_grid(db, rid, 1, "scale", [0, 1, 2, 3, 4])
    _record_grid(db, rid, 2, "scale", [0, 1, 2, 3, 4])
    _record_grid(db, rid, 3, "scale", [0, 1, 2, 3, 4])

    unfiltered = measure_dimensions(db, rid, dimensions={"构念": [1, 2, 3]})[0]
    filtered = measure_dimensions(
        db, rid, dimensions={"构念": [1, 2, 3]},
        submission_filter=lambda sub: sub <= 3,
    )[0]
    assert unfiltered.n == 5
    assert filtered.n == 3
    assert filtered.alpha == pytest.approx(1.0)


def test_scope_caveat_travels_with_every_report(db: SubmissionHistory) -> None:
    """口径限制不许只写在 docstring 里：失败/UNKNOWN 份被算进去这件事，
    每个报告（哪怕整个维度没算出 α）都必须自带一句声明。
    """
    rid = _make_run(db, 3)
    _record_grid(db, rid, 1, "scale", [0, 1, 2])
    _record_grid(db, rid, 2, "scale", [0, 1, 2])
    _record_grid(db, rid, 3, "scale", [0, 1, 2])
    reports: list[DimensionReport] = measure_dimensions(
        db, rid,
        dimensions={"算得出的": [1, 2, 3], "题不够的": [1, 2]},
    )
    assert len(reports) == 2
    for r in reports:
        assert "逐份" in r.scope_caveat and "UNKNOWN" in r.scope_caveat.upper()


def test_run_with_no_answers_reports_empty_reasons(db: SubmissionHistory) -> None:
    """空 run（或题号配错）：不抛，每题一条"库里没有这道题的记录"。"""
    rid = _make_run(db, 5)
    report = measure_dimensions(db, rid, dimensions={"构念": [1, 2, 3]})[0]
    assert report.k == 0 and report.alpha is None
    assert len(report.excluded) == 3
    assert all("没有这道题" in e.reason for e in report.excluded)


# ===========================================================================
#  CLI 那一层：维度从哪来、报告怎么印（v3.2 --report-alpha）
# ===========================================================================
def test_dimensions_and_reverse_come_from_the_config_only() -> None:
    cfg = {
        1: {"dimension": "满意度"}, 2: {"dimension": "满意度"},
        "3": {"dimension": "满意度", "reverse": True},
        4: {"type": "single"}, 5: "not-a-dict",
    }
    assert reliability.dimensions_from_config(cfg) == {"满意度": [1, 2, 3]}
    assert reliability.reverse_from_config(cfg) == [3]
    assert reliability.dimensions_from_config(None) == {}


def test_implicit_dimension_skips_runs_without_scalable_items() -> None:
    with SubmissionHistory(":memory:") as db:
        rid = db.start_run(_URL, 1, "edge", False)
        db.record_answer(rid, 1, 1, "text", None, text_answer="张伟")
        assert reliability.implicit_dimension(db, rid) == {}
        db.record_answer(rid, 1, 2, "scale", [3])
        db.record_answer(rid, 1, 3, "scale", [2])
        dims = reliability.implicit_dimension(db, rid)
        assert list(dims) == [reliability.UNDECLARED], dims
        assert dims[reliability.UNDECLARED] == [2, 3]


def test_cli_report_prints_the_caveat_and_survives_no_history(capsys) -> None:
    from src import cli

    with SubmissionHistory(":memory:") as db:
        rid = db.start_run(_URL, 3, "edge", False)
        for i in (1, 2, 3, 4, 5):
            for q, v in ((1, i % 5), (2, i % 5), (3, (i + 1) % 5)):
                db.record_answer(rid, i, q, "scale", [v + 1])
        cli._report_reliability(db, {
            1: {"dimension": "满意度"}, 2: {"dimension": "满意度"},
            3: {"dimension": "满意度"},
        })
    out = capsys.readouterr().out
    assert "满意度" in out and "α" in out, out
    assert "口径" in out, "历史库没有逐份成败标记这条限制必须随报告一起印出来"

    capsys.readouterr()
    cli._report_reliability(None, {})
    assert "没开历史库" in capsys.readouterr().out
