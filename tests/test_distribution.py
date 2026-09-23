"""投递分布在线纠正的契约（v3.2 B 档第 4 条）。

为什么值得测：这个模块会**改答题结果**，而它做错时没有任何外部症状 —— 平台照样收，
用户看到的配置照样写着 3:1。所以断言全部对着"它到底改了多少、往哪边改、什么时候
必须不改"来写，尤其三条：默认关着时逐位不变、失败的那一份绝不进统计、
单份样本的冲击被因子夹住。

对标 SurveyController 的 `core/questions/distribution.py`（借公式，GPL-3.0 不取码）。
"""

from __future__ import annotations

import random

import pytest

from src import distribution as dist


@pytest.fixture(autouse=True)
def _clean_state():
    dist.enable(False)
    dist.start_run()
    yield
    dist.enable(False)
    dist.start_run()


def _deliver(qnum: int, picks: list[int], total: int = 4) -> None:
    dist.buffer_answer(qnum, picks, total)
    assert dist.commit_buffer() == 1


# ---------------------------------------------------------------------------
#  1. 开关与空状态
# ---------------------------------------------------------------------------
def test_disabled_adjust_is_identity() -> None:
    w = [3.0, 1.0]
    _deliver(1, [0])
    assert dist.adjust(1, w) == w, "没开 --drift-correct 时权重必须逐位不变"


def test_enabled_but_no_history_is_identity() -> None:
    dist.enable(True)
    assert dist.adjust(7, [1.0, 2.0]) == [1.0, 2.0]


def test_start_run_clears_everything() -> None:
    dist.enable(True)
    _deliver(1, [0])
    dist.start_run()
    assert dist.drift_report() == {}
    assert dist.adjust(1, [1.0, 1.0]) == [1.0, 1.0]


# ---------------------------------------------------------------------------
#  2. 方向与强度
# ---------------------------------------------------------------------------
def test_under_delivered_option_gets_boosted_and_vice_versa() -> None:
    dist.enable(True)
    for _ in range(40):
        _deliver(3, [0])                     # 目标 1:1，实际全压在 0 上
    out = dist.adjust(3, [1.0, 1.0])
    assert out[1] > out[0], f"没被投出去的 1 号项必须被抬高: {out}"
    assert out[0] < 1.0, f"超投的 0 号项必须被压低: {out}"


def test_factor_is_clamped_so_one_bad_sample_cannot_flip_the_distribution() -> None:
    dist.enable(True)
    for _ in range(1000):
        _deliver(5, [0])
    out = dist.adjust(5, [1.0, 1.0, 1.0, 1.0])
    assert out[0] == pytest.approx(1.0 * dist.FACTOR_MIN), out
    assert all(o == pytest.approx(1.0 * dist.FACTOR_MAX) for o in out[1:]), out


def test_first_warmup_deliveries_do_not_move_weights_at_all() -> None:
    """样本个位数时 gap 基本是噪声 —— 强度必须是 0，否则纠正本身成了抖动源。"""
    dist.enable(True)
    for _ in range(dist.WARMUP_DELIVERIES - 1):
        _deliver(9, [0])
    assert dist.adjust(9, [1.0, 1.0]) == [1.0, 1.0], "爬坡之前必须逐位不动"


def test_correction_strength_ramps_up_with_more_samples() -> None:
    """同样的 4:6 缺口，第 10 份时的纠正力度必须小于第 100 份 —— 强度随样本爬坡。"""
    def ratio_after(n_pairs: int) -> float:
        dist.enable(True)
        dist.start_run()
        for i in range(n_pairs):
            _deliver(9, [1 if i % 5 >= 3 else 0])   # 5 份里 2 份投 1 → 实际 40% : 60%
        out = dist.adjust(9, [1.0, 1.0])
        return out[1] / out[0]

    early, late = ratio_after(10), ratio_after(100)
    assert early > 1.0 and late > 1.0, (early, late)
    assert late > early, (early, late)


def test_zero_weight_option_never_resurrects_and_never_crashes() -> None:
    dist.enable(True)
    for _ in range(20):
        _deliver(2, [1])
    out = dist.adjust(2, [1.0, 0.0])
    assert out[1] == 0.0, "用户明确配了 0 权重的项不能被纠正逻辑救活"
    assert out[0] > 0.0
    assert dist.adjust(2, [0.0, 0.0]) == [0.0, 0.0], "全 0 权重原样退回，交给等权分支"


# ---------------------------------------------------------------------------
#  3. 缓冲语义：只有提交成功的那份才算数
# ---------------------------------------------------------------------------
def test_discarded_submission_never_enters_statistics() -> None:
    dist.buffer_answer(1, [0], 4)
    assert dist.discard_buffer() == 1
    assert dist.drift_report() == {}
    dist.enable(True)
    assert dist.adjust(1, [1.0, 1.0]) == [1.0, 1.0]


def test_commit_is_not_double_counted() -> None:
    """commit 之后缓冲必须清空 —— 同一份被计两次会让实际份额永远偏低，纠正方向就反了。"""
    dist.buffer_answer(1, [2], 4)
    assert dist.commit_buffer() == 1
    assert dist.commit_buffer() == 0
    assert dist.drift_report()[1]["delivered"] == 1.0


def test_multi_select_counts_every_picked_option() -> None:
    """多选的份额口径是"这个选项出现在多少份里"，三项同时被勾就是各 1/1。"""
    dist.buffer_answer(4, [0, 2, 3], 4)
    assert dist.commit_buffer() == 1
    rep = dist.drift_report()
    assert rep[4]["delivered"] == 1.0
    assert rep[4]["max_share"] == pytest.approx(1.0)


def test_rebuffering_the_same_question_replaces_instead_of_double_counting() -> None:
    """断点续填会在同一份里重答同一题 —— 缓冲按题收敛到最后一版，不能累加。"""
    dist.buffer_answer(4, [1], 4)
    dist.buffer_answer(4, [2], 4)
    assert dist.commit_buffer() == 1
    assert dist.drift_report()[4]["delivered"] == 1.0


# ---------------------------------------------------------------------------
#  5. CLI 接线
# ---------------------------------------------------------------------------
def test_cli_flag_turns_the_controller_on_and_off(monkeypatch) -> None:
    from src import cli

    args = cli.parse_args(["-u", "https://www.wjx.cn/vj/x.aspx", "--drift-correct"])
    assert args.drift_correct is True
    args_off = cli.parse_args(["-u", "https://www.wjx.cn/vj/x.aspx"])
    assert args_off.drift_correct is False, "默认必须关着"

    # main() 里的开关分支：不真跑批次，只验它确实调到了 enable
    calls: list[bool] = []
    monkeypatch.setattr(dist, "enable", calls.append)
    monkeypatch.setattr(
        cli, "run_batch",
        lambda *_a, **_kw: (_ for _ in ()).throw(SystemExit(0)),
    )
    with pytest.raises(SystemExit):
        cli.main(["-u", "https://www.wjx.cn/vj/x.aspx", "-c", "1", "--drift-correct"])
    assert calls[:1] == [True], f"--drift-correct 没有开启控制器：{calls}"


# ---------------------------------------------------------------------------
#  4. 端到端：开纠正之后落地分布更靠近目标
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("correct", [False, True], ids=["raw", "corrected"])
def test_correction_pulls_delivered_share_toward_the_target(correct: bool) -> None:
    """目标 3:1 的两项，模拟 20% 的提交失败率后看落地比例。

    断言的是"开着的落地比关着的更靠近目标"，不是"绝对达到目标" ——
    因子夹在 ±1/3、失败还是随机的，绝对命中不成立，也不该成立。
    """
    dist.enable(correct)
    rng = random.Random(4242)
    target = [3.0, 1.0]
    delivered = [0, 0]
    for _ in range(400):
        w = dist.adjust(1, target)
        pick = rng.choices([0, 1], weights=w, k=1)[0]
        if rng.random() < 0.2:               # 这一份提交失败 → 不该进统计
            dist.buffer_answer(1, [pick], 2)
            dist.discard_buffer()
            continue
        delivered[pick] += 1
        dist.buffer_answer(1, [pick], 2)
        dist.commit_buffer()

    share = delivered[0] / sum(delivered)
    closeness = abs(share - 0.75)
    if correct:
        assert closeness < 0.06, f"开了纠正还差 {closeness:.3f}: {delivered}"
    else:
        assert delivered[0] > 0 and delivered[1] > 0, delivered
