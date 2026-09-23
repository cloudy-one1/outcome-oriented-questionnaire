"""信度计划内核（``src/plan.py``，设计稿 §5 P1 的数学那一半）的离线测试。

三条总原则：

  1. **数学用独立推导的期望值验，不拿被测函数自己证自己。**
     ``alpha_to_sigma`` 的对照值是纸面算出来的（注释里留了推导），
     并额外用反演式 ``α = kρ/(1+ρ(k−1))``、``ρ = 1/(1+σ_e²)`` 做闭环校验；
     计划 α 的复算是从 ``plan.rows``（投递出去的选项索引）**重拼矩阵**再交给
     ``reliability.cronbach_alpha``，与内核内部"从潜变量得分算"不共享中间量。
  2. **降级口径本身就是被测行为。** k<3 整维度不参战、单选项题被排除、
     配额和 ≠ n 直接抛 —— 这三条各自都有用例钉住，改动必然弄红。
  3. **"边际优先"是最容易被悄悄违背的那条。** 所以专门有一个 α 不达标的用例，
     断言它 notes 里认了账、而配额仍然逐题精确（而不是把配额改掉去凑 α）。
"""

from __future__ import annotations

import math
import os
import sys
from collections import Counter
from typing import Optional

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import plan as plan_module  # noqa: E402
from src.plan import ItemSpec, Plan, alpha_to_sigma, build_plan  # noqa: E402
from src.reliability import cronbach_alpha  # noqa: E402

SEED = 20260924


# ============================================================================
#  造需求的帮手
# ============================================================================
def _spread_quota(n: int, options: int) -> list[int]:
    """尽量摊平、且**总和恰好等于 n** 的配额（余数从前往后各摊 1 份）。"""
    base, extra = divmod(n, options)
    quota = [base] * options
    for i in range(extra):
        quota[i] += 1
    return quota


def _flat_specs(
    n: int,
    k: int,
    options: int = 5,
    reverse_at: tuple[int, ...] = (),
    quota: Optional[list[int]] = None,
) -> list[ItemSpec]:
    """k 道同构题（题号 1..k）。``reverse_at`` 里的题号标成反向；
    ``quota`` 不给就是摊平的那一份。"""
    actual = quota if quota is not None else _spread_quota(n, options)
    return [
        ItemSpec(
            qnum=q,
            options=options,
            quota=actual,
            reverse=(q in reverse_at),
        )
        for q in range(1, k + 1)
    ]


def _counts(row: tuple[int, ...], options: int) -> list[int]:
    seen: Counter[int] = Counter(row)
    return [seen.get(i, 0) for i in range(options)]


def _raw_matrix(plan: Plan, specs: list[ItemSpec]) -> list[list[float]]:
    """从**公开产物**重拼等距分矩阵：选项索引 +1（与 P0 的 ordinal_score 同基），
    一律不翻转 —— 模拟"分析方拿到数据、但没按声明翻反向题"的那个口径。
    """
    return [[float(v) + 1.0 for v in plan.rows[spec.qnum]] for spec in specs]


# ============================================================================
#  alpha_to_sigma：解析值与边界
# ============================================================================
def test_alpha_to_sigma_matches_hand_derived_value() -> None:
    """α=0.7、k=5 的纸面推导：

    ρ = 0.7 / (5 − 0.7·4) = 0.7/2.2 = 7/22
    σ_e = sqrt(1/ρ − 1) = sqrt(22/7 − 1) = sqrt(15/7) ≈ 1.46385011
    """
    assert alpha_to_sigma(0.7, 5) == pytest.approx(math.sqrt(15.0 / 7.0), abs=1e-12)
    # 另一条独立算路：反演式 α = kρ/(1+ρ(k−1))，ρ = 1/(1+σ_e²) —— 闭环必须回到原 α。
    for alpha, k in [(0.5, 3), (0.7, 5), (0.9, 12), (0.95, 4)]:
        sigma = alpha_to_sigma(alpha, k)
        rho = 1.0 / (1.0 + sigma * sigma)
        assert k * rho / (1.0 + rho * (k - 1)) == pytest.approx(alpha, abs=1e-12)


def test_sigma_collapses_to_zero_as_alpha_approaches_one() -> None:
    """α→1 ⇒ ρ→1 ⇒ σ_e→0（题间完全平行、没有误差），且 σ_e 对 α 严格单调下降。"""
    assert alpha_to_sigma(0.999999, 3) < 0.01
    ladder = [alpha_to_sigma(a, 5) for a in (0.3, 0.5, 0.7, 0.85, 0.95, 0.99, 0.999)]
    assert ladder == sorted(ladder, reverse=True)      # 严格递减
    assert ladder[0] > 1.0 > ladder[-1]                # 低目标要很大噪声才压得下 α
    # 同一 α 下题越多、每对题所需互相关越低 ⇒ 允许更大误差：σ_e 随 k 单调上升。
    assert alpha_to_sigma(0.8, 3) < alpha_to_sigma(0.8, 10)


@pytest.mark.parametrize(
    "alpha, k, needle",
    [
        (0.0, 5, "开区间"),           # 退化左端：题间毫无互相关
        (1.0, 5, "开区间"),           # 退化右端：σ_e 恒为 0，控制没有自由度
        (-0.2, 5, "开区间"),
        (1.5, 5, "开区间"),
        (float("nan"), 5, "有限实数"),
        (float("inf"), 5, "有限实数"),
        (0.7, 1, "k 必须 ≥ 2"),       # 单题谈不上内部一致性
        (0.7, 0, "k 必须 ≥ 2"),
    ],
    ids=["alpha=0", "alpha=1", "negative", "above_one", "nan", "inf", "k=1", "k=0"],
)
def test_alpha_to_sigma_rejects_illegal_inputs(alpha: float, k: int, needle: str) -> None:
    with pytest.raises(ValueError) as exc:
        alpha_to_sigma(alpha, k)
    assert needle in str(exc.value)


# ============================================================================
#  build_plan：α 命中 + 配额精确
# ============================================================================
@pytest.mark.parametrize(
    "n, k, options, target, quota",
    [
        (300, 5, 5, 0.80, [20, 40, 60, 80, 100]),   # 摊得不匀：这才是真卷的样子
        (1200, 8, 5, 0.85, None),                   # §5 P1 那条承诺：±0.02
        (200, 4, 2, 0.70, [140, 60]),               # 二分 + 偏斜：钝化最明显
    ],
    ids=["300x5_skewed", "1200x8", "200x4_binary"],
)
def test_plan_hits_target_and_honors_quotas_exactly(
    n: int, k: int, options: int, target: float, quota: Optional[list[int]]
) -> None:
    """核心契约：独立复算的 α 既贴着计划值、也贴着目标，配额**逐题精确**。

    复算走的是 ``plan.rows``（投递出去的选项索引）而不是内核内部的得分矩阵，
    所以"计划 α"与"实测 α"确实是两条算路在对账（设计稿 G3 的口径）。
    """
    specs = _flat_specs(n, k, options=options, quota=quota)
    plan = build_plan(specs, n=n, alpha_target=target, rng_seed=SEED)

    measured = cronbach_alpha(_raw_matrix(plan, specs))
    assert measured is not None
    assert plan.alpha_planned is not None
    assert abs(measured - plan.alpha_planned) < 1e-12          # 计划值可复算，不是嘴上说的
    assert abs(measured - target) <= 0.02                      # 设计稿 §5 P1 的验收线
    assert plan.k == k and plan.n == n
    assert plan.evaluations <= 9 + 4                           # 9 候选 + ≤4 次细化
    assert any("达标" in note for note in plan.notes)

    for spec in specs:
        assert _counts(plan.rows[spec.qnum], spec.options) == list(spec.quota)


def test_choice_for_is_one_based_and_degrades_to_none() -> None:
    """``choice_for`` 与 ``history.answers.submission_index`` 同基（1-based）。

    越界（批次比计划长）与 0 基索引都返回 None：0 这一档**故意不当成第一份** ——
    错一位会让整批的边际分布看起来"莫名就乱了"，比退回随机更难查。
    """
    n = 60
    specs = _flat_specs(n, 4)
    plan = build_plan(specs, n=n, alpha_target=0.75, rng_seed=SEED)
    for index in range(1, n + 1):
        assert plan.choice_for(1, index) in (0, 1, 2, 3, 4)
    assert plan.choice_for(1, 0) is None
    assert plan.choice_for(1, n + 1) is None
    assert plan.choice_for(1, -3) is None
    assert plan.choice_for(999, 1) is None          # 这道题不在计划里
    assert plan.participating() == [1, 2, 3, 4]
    assert plan.quotas[1] == tuple(_spread_quota(n, 5))


def test_alpha_is_measured_with_declared_reverse_items_flipped() -> None:
    """反向题的计划 α 按声明翻转后再算 —— 这才是 P0 实测（``reverse=[...]``）的口径。"""
    n, k = 240, 4
    specs = _flat_specs(n, k, reverse_at=(2,))
    plan = build_plan(specs, n=n, alpha_target=0.8, rng_seed=SEED)

    assert plan.reversed_items == [2]
    # 声明了反向 ⇒ 计划值仍在目标附近（翻转后大家都正向载荷于 θ）
    assert plan.alpha_planned is not None
    assert abs(plan.alpha_planned - 0.8) <= 0.02
    # 配额照样逐题精确，反向不反向都一样兑现
    for spec in specs:
        assert _counts(plan.rows[spec.qnum], spec.options) == list(spec.quota)
    # 但 notes 必须提醒"漏标"这件事（§4：靠题干负面词判反向在真卷上错得很难看）
    assert any("reverse" in note and "漏标" in note for note in plan.notes)


# ============================================================================
#  方向性：目标越高，实测 α 不降
# ============================================================================
def test_measured_alpha_is_monotone_non_decreasing_in_target() -> None:
    """同一组配额 + 同一个 seed，目标 α 拉高 ⇒ 实测 α 单调不降。

    这是"σ_e 这一个自由度真的在控制 α"的直接证据：搜索不是原地打转。
    """
    n = 400
    specs = _flat_specs(n, 5)
    targets = (0.45, 0.60, 0.75, 0.88)
    observed: list[float] = []
    sigmas: list[float] = []
    for target in targets:
        plan = build_plan(specs, n=n, alpha_target=target, rng_seed=SEED)
        measured = cronbach_alpha(_raw_matrix(plan, specs))
        assert measured is not None and plan.sigma_e is not None
        observed.append(measured)
        sigmas.append(plan.sigma_e)

    for earlier, later in zip(observed, observed[1:]):
        assert earlier <= later + 1e-9, f"实测 α 随目标反降：{observed}"
    assert observed[-1] - observed[0] > 0.10          # 差异得是实打实的，不是贴着噪声
    # 目标越高 ⇒ 允许的错误方差越小 ⇒ σ_e 越小（与解析值同向）
    assert sigmas == sorted(sigmas, reverse=True)


def test_undocumented_reverse_item_crashes_alpha() -> None:
    """设计稿 §4 的失效面：**漏标**反向题时 α 显著掉下来。

    同一组配额、同一个 seed，只把第 2 题改成反向：
      * 内核（按声明翻转着算）的计划 α 仍然贴着目标；
      * 而"分析方没翻转"的实测 α（``_raw_matrix`` 那个口径）掉一大截。
    两个数之间的落差就是 §4 想让 P2 报出来的那条提示，也是反向题真实的代价。
    """
    n = 400
    forward = _flat_specs(n, 5)
    with_reverse = _flat_specs(n, 5, reverse_at=(2,))

    plan_fwd = build_plan(forward, n=n, alpha_target=0.8, rng_seed=SEED)
    plan_rev = build_plan(with_reverse, n=n, alpha_target=0.8, rng_seed=SEED)

    alpha_fwd = cronbach_alpha(_raw_matrix(plan_fwd, forward))
    alpha_rev = cronbach_alpha(_raw_matrix(plan_rev, with_reverse))
    assert alpha_fwd is not None and alpha_rev is not None
    assert alpha_fwd == pytest.approx(0.8, abs=0.02)
    assert alpha_rev < alpha_fwd - 0.15               # 漏标 ⇒ 显著下降
    # 内核按声明处理过，所以它自己报的计划 α 不受牵连
    assert plan_rev.alpha_planned == pytest.approx(0.8, abs=0.02)
    assert plan_rev.reversed_items == [2]


# ============================================================================
#  降级与硬约束（设计稿 §6）
# ============================================================================
def test_two_items_dimension_is_not_planned_at_all() -> None:
    """k=2 ⇒ 整个维度不参战：没有 rows、``alpha_planned is None``（不是 0）。"""
    specs = _flat_specs(100, 2)
    plan = build_plan(specs, n=100, alpha_target=0.8, rng_seed=SEED)

    assert plan.alpha_planned is None
    assert plan.sigma_e is None
    assert plan.k == 0 and plan.rows == {}
    assert plan.choice_for(1, 1) is None              # 接线侧据此整批回退加权随机
    joined = " ".join(plan.notes)
    assert "< 3" in joined and "骗人" in joined
    assert {q for q, _reason in plan.excluded} == {1, 2}


def test_quota_sum_mismatch_raises_and_names_the_item() -> None:
    """配额总和 ≠ n：抛，而不是按比例缩放 —— 配额是用户显式配的。"""
    specs = [
        ItemSpec(qnum=1, options=3, quota=[20, 20, 20]),
        ItemSpec(qnum=2, options=3, quota=[30, 10, 5]),   # 和 45 ≠ 60
    ]
    with pytest.raises(ValueError, match="配额总和 45 ≠ 份数 60"):
        build_plan(specs, n=60, alpha_target=0.8, rng_seed=SEED)


def test_single_option_item_is_excluded_but_rest_still_planned() -> None:
    """选项数 < 2 的题：排除、进 excluded、进 notes，其余 3 题照常出计划。"""
    n = 90
    specs = [
        ItemSpec(qnum=1, options=1, quota=[n]),            # 只有一个选项，谈不上相关
        ItemSpec(qnum=2, options=3, quota=_spread_quota(n, 3)),
        ItemSpec(qnum=3, options=3, quota=_spread_quota(n, 3)),
        ItemSpec(qnum=4, options=3, quota=_spread_quota(n, 3)),
    ]
    plan = build_plan(specs, n=n, alpha_target=0.75, rng_seed=SEED)

    assert plan.k == 3 and plan.choice_for(1, 1) is None
    assert [q for q, _r in plan.excluded] == [1]
    assert any("题 1 不参与" in note and "选项数 1" in note for note in plan.notes)
    for spec in specs[1:]:
        assert _counts(plan.rows[spec.qnum], spec.options) == list(spec.quota)


@pytest.mark.parametrize(
    "specs, n, target, needle",
    [
        (_flat_specs(60, 3), 60, 1.0, "开区间"),                     # α 右端退化
        (_flat_specs(60, 3), 60, 0.0, "开区间"),                     # α 左端退化
        (_flat_specs(60, 3), 1, 0.8, "份数 n=1"),                    # 一份没有方差
        (_flat_specs(60, 3), 0, 0.8, "份数 n=0"),
        (
            [
                ItemSpec(1, 3, [20, 20, 20]),
                ItemSpec(1, 3, [10, 20, 30]),
                ItemSpec(2, 3, [20, 20, 20]),
            ],
            60, 0.8, "重复",                                          # 同题号两份需求
        ),
        (
            [
                ItemSpec(1, 3, [20, 20, 20]),
                ItemSpec(2, 4, [20, 20, 20]),                     # 4 个选项写错了长度
                ItemSpec(3, 3, [20, 20, 20]),
            ],
            60, 0.8, "配额长度 3 与选项数 4 不符",
        ),
        (
            [
                ItemSpec(1, 3, [20, -5, 45]),
                ItemSpec(2, 3, [20, 20, 20]),
                ItemSpec(3, 3, [20, 20, 20]),
            ],
            60, 0.8, "负数",
        ),
    ],
    ids=["alpha=1", "alpha=0", "n=1", "n=0", "duplicate_qnum", "quota_length", "negative"],
)
def test_structurally_illegal_input_raises(specs: list[ItemSpec], n: int, target: float, needle: str) -> None:
    with pytest.raises(ValueError) as exc:
        build_plan(specs, n=n, alpha_target=target, rng_seed=SEED)
    assert needle in str(exc.value)


def test_marginal_quotas_survive_a_missed_alpha_target() -> None:
    """α 不达标时**保配额**（设计稿 §6 最后一条，本模块最容易做错的地方）。

    构造一个不可达的目标：三题的选项数分别是 2/3/5，σ_e→0 时各题都只是同一
    个排序的粗化阶梯，题间相关被粒度钉死在 1 以下 ⇒ 目标 0.99 无从达到。
    这时内核只做两件事：notes 里认账 + 给可达区间；配额一格未动。
    """
    n = 150
    specs = [
        ItemSpec(qnum=1, options=2, quota=[60, 90]),
        ItemSpec(qnum=2, options=3, quota=[50, 50, 50]),
        ItemSpec(qnum=3, options=5, quota=[30, 30, 30, 30, 30]),
    ]
    plan = build_plan(specs, n=n, alpha_target=0.99, rng_seed=SEED)

    assert plan.alpha_planned is not None
    assert plan.alpha_planned < 0.99 - 0.02                     # 确实没达标
    joined = " ".join(plan.notes)
    assert "α 未达标" in joined
    assert f"计划 {plan.alpha_planned:.3f}" in joined            # 报的是实测那个数
    assert "一格未动" in joined
    # 配额仍然是构造性精确的 —— 这才是这条硬约束的实际断言
    for spec in specs:
        assert _counts(plan.rows[spec.qnum], spec.options) == list(spec.quota)
    assert cronbach_alpha(_raw_matrix(plan, specs)) == pytest.approx(plan.alpha_planned, abs=1e-12)


def test_small_sample_still_plans_but_says_so() -> None:
    """n<30：设计稿建议只报实测。本内核不拒收（那是接线的开关），但必须留下话。"""
    n = 20
    specs = _flat_specs(n, 4, options=3)
    plan = build_plan(specs, n=n, alpha_target=0.7, rng_seed=SEED)
    assert plan.k == 4
    assert any("拟合噪声" in note for note in plan.notes)
    for spec in specs:
        assert _counts(plan.rows[spec.qnum], spec.options) == list(spec.quota)


# ============================================================================
#  确定性与依赖方向
# ============================================================================
def test_same_seed_reproduces_matrix_cell_by_cell() -> None:
    """同 seed 两次 ``build_plan`` 逐格相同；换 seed 至少要有一格不同。

    后半句不是装饰：它挡住"内核把随机数吞了、恒定输出同一张矩阵"这类
    看似"很可复现"的 bug —— 那样整批答卷会同质到露馅。
    """
    n = 120
    specs = _flat_specs(n, 4)
    first = build_plan(specs, n=n, alpha_target=0.8, rng_seed=SEED)
    second = build_plan(specs, n=n, alpha_target=0.8, rng_seed=SEED)

    assert first.rows == second.rows
    assert first.alpha_planned == second.alpha_planned
    assert first.sigma_e == second.sigma_e

    other = build_plan(specs, n=n, alpha_target=0.8, rng_seed=SEED + 1)
    assert other.rows != first.rows
    assert all(_counts(other.rows[s.qnum], s.options) == list(s.quota) for s in specs)


def test_kernel_does_not_touch_global_random_state() -> None:
    """计划用的是 ``random.Random(seed)`` 实例，不污染进程级随机流。

    接线后 ``generate_answer`` 的回退分支靠的就是全局流，这里被顺手搅动的话
    症状是"回退部分每次跑都不一样"，非常难查。
    """
    import random

    random.seed(SEED)
    expected = [random.random() for _ in range(3)]
    random.seed(SEED)
    build_plan(_flat_specs(60, 3), n=60, alpha_target=0.8, rng_seed=SEED)
    assert [random.random() for _ in range(3)] == expected


def test_alpha_goes_through_reliability_module_not_a_private_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """α 必须复用 P0 那份 ``cronbach_alpha``（设计稿 §5：P1 建立在 P0 之上）。

    自己重写一份的漂移方式很安静：样本方差 vs 母体方差、缺格过滤、None 口径……
    最后"计划 α"和"实测 α"两个数各自都好看、就是对不上。
    """
    calls: list[int] = []
    original = plan_module.cronbach_alpha

    def counting(items: list[list[float]]) -> Optional[float]:
        calls.append(len(items))
        return original(items)

    monkeypatch.setattr(plan_module, "cronbach_alpha", counting)
    n = 80
    specs = _flat_specs(n, 4)
    plan = build_plan(specs, n=n, alpha_target=0.75, rng_seed=SEED)

    assert len(calls) >= 2                                  # 搜索确实调了它若干次
    assert all(k == 4 for k in calls)                       # 每次都是全 k 题
    assert plan.alpha_planned is not None


# ===========================================================================
#  批次状态层（CLI --alpha-target 开起来、生成器逐题来问的那一段）
# ===========================================================================
import pytest

from src import answering_v2, config as cfg_mod, plan as plan_session


def _single(qnum: int, options: int = 3) -> dict:
    return {"q": qnum, "type": "single", "choices": list(range(1, options + 1)),
            "title": f"第{qnum}题"}


def _scale(qnum: int, points: int = 5) -> dict:
    return {"q": qnum, "type": "scale", "scale": points, "scale_min": 1,
            "title": f"第{qnum}题"}


@pytest.fixture()
def session():
    saved = dict(cfg_mod.WEIGHT_CONFIG)
    cfg_mod.WEIGHT_CONFIG.clear()
    plan_session.configure(0.8, 40)
    yield plan_session
    plan_session.end_session()
    cfg_mod.WEIGHT_CONFIG.clear()
    cfg_mod.WEIGHT_CONFIG.update(saved)


def test_quotas_are_exact_and_deterministic() -> None:
    assert sum(plan_session._quotas([1, 1, 1], 10)) == 10
    assert plan_session._quotas([1, 1, 1], 10) == [4, 3, 3]      # 余数落谁可复现
    assert plan_session._quotas([0, 0, 0], 9) == [3, 3, 3]       # 全 0 → 等权
    assert plan_session._quotas([3, 1], 8) == [6, 2]


def test_no_dimension_declaration_refuses_to_build(session) -> None:
    notes = session.ensure_plan([_single(1), _single(2), _single(3)])
    assert session.forced_choice(1) is None
    assert any("dimension" in n for n in notes), notes


def test_plan_is_built_once_and_covers_the_whole_batch(session) -> None:
    cfg_mod.WEIGHT_CONFIG.update({
        1: {"dimension": "满意度"}, 2: {"dimension": "满意度"},
        3: {"dimension": "满意度"},
    })
    qs = [_single(1), _single(2), _single(3)]
    first = session.ensure_plan(qs)
    assert any("计划已建" in n for n in first), first
    assert session.ensure_plan(qs) == [], "第二页再建会改掉已兑现的行"


def test_next_survey_in_a_queue_gets_its_own_plan(session) -> None:
    """换一份问卷就得换一份计划（``--url-file`` 队列）。

    上面那条"只建一次"是给**一份问卷的分页**设的；跨问卷不重建有两种症状，都安静：
    题号撞上时沿用上一份卷的配额，撞不上时既不建也不说为什么不建。
    """
    cfg_mod.WEIGHT_CONFIG.update({
        1: {"dimension": "d"}, 2: {"dimension": "d"}, 3: {"dimension": "d"},
    })
    built = session.ensure_plan([_single(1), _single(2), _single(3)])
    assert any("计划已建" in n for n in built), built
    session.begin_submission(1)
    assert session.forced_choice(1) is not None, "先确认第一份卷真接上了计划"

    session.reset_survey()
    session.begin_submission(1)
    assert session.forced_choice(1) is None, "旧计划的行不许再来答新问卷"

    notes = session.ensure_plan(
        [_single(1, options=8), _single(2, options=8), _single(3, options=8)]
    )
    assert any("计划已建" in n for n in notes), notes
    picks: list[int] = []
    for index in range(1, 41):
        session.begin_submission(index)
        choice = session.forced_choice(1)
        assert choice is not None, index
        picks.append(choice)
    assert sorted(set(picks)) == list(range(8)), (
        f"仍是上一份卷的 3 项配额: {sorted(set(picks))}")


def test_every_submission_gets_a_planned_option_and_marginals_hold(session) -> None:
    cfg_mod.WEIGHT_CONFIG.update({
        1: {"dimension": "d", "weights": [1, 1, 1]},
        2: {"dimension": "d"}, 3: {"dimension": "d", "reverse": True},
    })
    session.ensure_plan([_single(1), _scale(2), _single(3)])
    options = {1: 3, 2: 5, 3: 3}          # Q2 是 5 级量表
    picked: dict[int, list[int]] = {q: [] for q in options}
    for index in range(1, 41):
        session.begin_submission(index)
        for qnum in options:
            choice = session.forced_choice(qnum)
            assert choice is not None and 0 <= choice < options[qnum], (
                index, qnum, choice)
            picked[qnum].append(choice)
    for qnum, got in picked.items():
        assert sorted(set(got)) == list(range(options[qnum])), (
            f"Q{qnum} 每个选项都该被排到: {got}")
        assert sum(1 for x in got if x == got[0]) < len(got), "不该整批压在同一项上"


def test_missing_or_unscoreable_question_refuses_the_whole_plan(session) -> None:
    cfg_mod.WEIGHT_CONFIG.update({
        1: {"dimension": "d"}, 2: {"dimension": "d"}, 9: {"dimension": "d"},
    })
    notes = session.ensure_plan([_single(1), _single(2)])
    assert session.forced_choice(1) is None
    assert any("Q9" in n and "没探测到" in n for n in notes), notes

    cfg_mod.WEIGHT_CONFIG.clear()
    cfg_mod.WEIGHT_CONFIG.update({
        1: {"dimension": "d"}, 2: {"dimension": "d"}, 3: {"dimension": "d"},
    })
    session.configure(0.8, 40)   # 建计划是"一次性的"，换一批声明就得重新 configure
    multi = {"q": 3, "type": "multi", "choices": [1, 2, 3], "title": "多选"}
    notes = session.ensure_plan([_single(1), _single(2), multi])
    assert session.forced_choice(1) is None
    assert any("不能等距计分" in n for n in notes), notes


def test_small_batch_refuses_to_build(session) -> None:
    cfg_mod.WEIGHT_CONFIG.update({
        1: {"dimension": "d"}, 2: {"dimension": "d"}, 3: {"dimension": "d"},
    })
    session.configure(0.8, 12)
    notes = session.ensure_plan([_single(1), _single(2), _single(3)])
    assert session.forced_choice(1) is None
    assert any("12 份" in n for n in notes), notes


def test_forced_choice_needs_a_submission_index(session) -> None:
    cfg_mod.WEIGHT_CONFIG.update({
        1: {"dimension": "d"}, 2: {"dimension": "d"}, 3: {"dimension": "d"},
    })
    session.ensure_plan([_single(1), _single(2), _single(3)])
    session.begin_submission(None)
    assert session.forced_choice(1) is None
    assert session.forced_choice(None) is None


def test_generate_answer_follows_the_plan(session) -> None:
    """计划说选哪项就选哪项 —— 走的是加权采样那条路，不另开出口。"""
    cfg_mod.WEIGHT_CONFIG.update({
        1: {"dimension": "d"}, 2: {"dimension": "d"}, 3: {"dimension": "d"},
    })
    qs = [_single(1), _scale(2), _single(3)]
    session.ensure_plan(qs)
    session.begin_submission(1)
    for q in qs:
        got = answering_v2.generate_answer(q)
        want = session.forced_choice(q["q"])
        selected = got.get("selected") or [got.get("value")]
        picked = selected[0]
        if q["type"] == "single":
            assert picked == q["choices"][want], (q["q"], got, want)
        else:
            assert picked == want + 1, (q["q"], got, want)


def test_plan_covers_every_participating_type(session) -> None:
    """``PARTICIPATING_TYPES`` 点名的每一类，生成答案时都必须真的查表。

    下拉曾漏传 `qnum`：计划把它的配额算进计划 α，投递却照旧随机 —— 症状是
    "计划 α 达标、实测 α 差一截"，而两句告警都不会响。
    """
    qs = [
        {"q": 1, "type": "single", "choices": [1, 2, 3], "title": "第1题"},
        {"q": 2, "type": "scale", "scale": 5, "scale_min": 1, "title": "第2题"},
        {"q": 3, "type": "rating", "scale": 5, "scale_min": 1, "title": "第3题"},
        {"q": 4, "type": "dropdown", "choices": [1, 2, 3], "title": "第4题"},
    ]
    cfg_mod.WEIGHT_CONFIG.update({q["q"]: {"dimension": "d"} for q in qs})
    notes = session.ensure_plan(qs)
    assert any("计划已建" in n for n in notes), notes

    for index in (1, 2, 3):
        session.begin_submission(index)
        for q in qs:
            want = session.forced_choice(q["q"])
            assert want is not None, (index, q)
            got = answering_v2.generate_answer(q)
            sel = got.get("selected") or [got.get("value")]
            if q["type"] in ("single", "dropdown"):
                assert sel[0] == q["choices"][want], (index, q["q"], got, want)
            else:
                assert sel[0] == want + int(q.get("scale_min", 1)), (
                    index, q["q"], got, want)


def test_plan_off_means_generate_answer_untouched() -> None:
    """没开 --alpha-target 时 forced_choice 恒为 None，采样回到 v3.1 的逐位行为。"""
    plan_session.end_session()
    assert plan_session.forced_choice(1) is None
    got = answering_v2.generate_answer(_single(7))
    assert got["selected"][0] in (1, 2, 3)


def test_out_of_range_alpha_target_is_rejected_before_any_work() -> None:
    from src import cli

    with pytest.raises(SystemExit) as boom:
        cli.main(["-u", "https://www.wjx.cn/vj/x.aspx", "-c", "5",
                  "--alpha-target", "0.99"])
    assert boom.value.code == 2
    assert not plan_session.configured()
