"""v3.0 权重锚定（``src/anchoring.py``）的离线契约测试。

要防的失效面：``WEIGHT_CONFIG`` 的键是题号，于是"问卷中间插一道题"会把整份
预设**向后错位一格**，而 ``validate_weight_config`` 只看权重形状 —— 只要选项数
恰好还对得上，错位就一路静默跑完 17 份。v2.8 的"出厂权重污染"是同一个洞的另一半。

这里的用例按 anchoring 模块文档的三条契约组织：
  1. 带 anchor 的条目只按锚点生效，**永不**退回答题号；
  2. 不带 anchor 的条目行为与 v2.8 逐位一致；
  3. 锚点认不到题 → 提示一次并说明"不生效"。
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import anchoring, config  # noqa: E402
from src.answering import build_answer_strategy  # noqa: E402
from src.config_io import (  # noqa: E402
    SCHEMA_VERSION,
    save_weight_config,
    validate_weight_config,
)


@pytest.fixture(autouse=True)
def _isolated_globals():
    """WEIGHT_CONFIG 与"已提示过"集合都是进程级可变状态 —— 逐用例快照/还原。"""
    saved_cfg = dict(config.WEIGHT_CONFIG)
    saved_reported = set(anchoring._REPORTED)
    config.WEIGHT_CONFIG.clear()
    anchoring.reset_reported_anchors()
    yield
    config.WEIGHT_CONFIG.clear()
    config.WEIGHT_CONFIG.update(saved_cfg)
    anchoring._REPORTED.clear()
    anchoring._REPORTED.update(saved_reported)


def _q(n: int, title: str, *, qtype: str = "single", **extra) -> dict:
    base: dict = {"q": n, "type": qtype, "title": title}
    base.update(extra)
    if qtype in ("single", "multi", "radio", "checkbox", "dropdown") and (
        "choices" not in base
    ):
        base["choices"] = [1, 2, 3, 4]
    return base


# ===========================================================================
#  题干归一化
# ===========================================================================
@pytest.mark.parametrize("raw", [
    "1. 您的性别",
    "1、您的性别",
    "第 1 题 您的性别",
    "（1） 您的性别",
    " 1 . 您的性别 ",
    "1.您的性别",
])
def test_numbering_prefix_is_stripped(raw: str) -> None:
    """同一道换了序号写法（问卷星插题后自己会重排序号）必须归一到同一个键。"""
    assert anchoring.normalize_title(raw) == "您的性别"


def test_year_in_the_stem_is_not_mistaken_for_numbering() -> None:
    """"2024年收入" 的 2024 是题干的一部分 —— 剥号规则必须要求分隔符。"""
    assert anchoring.normalize_title("2024年收入") == "2024年收入"


def test_two_layer_numbering_is_stripped() -> None:
    assert anchoring.normalize_title("1、(2) 您对服务满意吗") == "您对服务满意吗"


# ===========================================================================
#  结构签名
# ===========================================================================
@pytest.mark.parametrize("q,expected", [
    (_q(1, "A"), "single:4"),
    (_q(2, "B", qtype="checkbox"), "multi:4"),
    (_q(3, "C", qtype="scale", scale=10, scale_min=2), "scale:2-10"),
    (_q(4, "D", qtype="text", field="name"), "text"),
    (_q(5, "E", qtype="matrix", rows=[1, 2], cols=[1, 2, 3]), "matrix:2x3"),
    (_q(6, "F", qtype="matrix_single", rows=[1, 2], cols=[1, 2, 3]), "matrix:2x3"),
    ({"q": 7, "type": "single"}, "single:0"),
])
def test_question_signature(q: dict, expected: str) -> None:
    """签名走 models.normalize_question_type 的单一真相（checkbox→multi、matrix_single→matrix）。

    矩阵题那条是刻意成对写的：normalize 产出的是**存储名** matrix，
    分支若比 "matrix_single" 就永远走不到，矩阵会被算成 "matrix:0"（无 choices 键）。
    """
    assert anchoring.question_signature(q) == expected


def test_make_anchor_needs_a_title() -> None:
    assert anchoring.make_anchor(_q(1, "您的性别")) == {
        "title": "您的性别", "signature": "single:4",
    }
    assert anchoring.make_anchor({"q": 1, "type": "single", "choices": [1]}) is None
    assert anchoring.make_anchor({"q": 1, "type": "single", "title": "   "}) is None


# ===========================================================================
#  锚点认题（契约 1 的判定面）
# ===========================================================================
def test_exact_title_hit() -> None:
    anchor = {"title": "1. 您的性别", "signature": "single:2"}
    assert anchoring.anchor_matches_question(anchor, _q(9, "您的性别", choices=[1, 2]))


def test_typo_in_the_stem_still_hits() -> None:
    """作者改了题干里一个词：锚点该跟得住，否则稍一编辑就全部脱钩。"""
    anchor = {"title": "您对本次服务的整体满意度", "signature": "single:5"}
    q = _q(3, "您对本次服务的总体满意度", choices=[1, 2, 3, 4, 5])
    assert anchoring.anchor_matches_question(anchor, q)


def test_different_question_never_hits() -> None:
    anchor = {"title": "您的性别", "signature": "single:2"}
    q = _q(1, "您的最高学历", choices=[1, 2, 3, 4])
    assert anchoring.anchor_matches_question(anchor, q) is False


def test_structure_change_breaks_the_match() -> None:
    """题干还在、选项数变了 = 作者动过这道题，权重分布不再对应，宁可脱钩。"""
    anchor = {"title": "您的性别", "signature": "single:2"}
    q = _q(1, "您的性别", choices=[1, 2, 3])
    assert anchoring.anchor_matches_question(anchor, q) is False


@pytest.mark.parametrize("bad", [
    None, "您的性别", 42, [],
    {"signature": "single:4"},          # 没有 title 的锚点无从认题
    {"title": "", "signature": "single:4"},
])
def test_malformed_anchor_never_matches(bad: object) -> None:
    """畸形 anchor 一律不认 —— 认了就是拿一份不知道谁的分布在填这道题。"""
    assert anchoring.anchor_matches_question(bad, _q(1, "性别")) is False


def test_anchor_without_signature_matches_on_title_only() -> None:
    """signature 是可省的：手写 JSON 只给题干也该能用（形状非法由 validate 拒）。"""
    assert anchoring.anchor_matches_question(
        {"title": "您的性别"}, _q(3, "您的性别", choices=[1, 2])
    ) is True


def test_question_without_title_cannot_be_claimed() -> None:
    anchor = {"title": "您的性别", "signature": "single:4"}
    assert anchoring.anchor_matches_question(anchor, {"q": 1, "type": "single"}) is False


# ===========================================================================
#  查表（契约 1 + 2）
# ===========================================================================
def test_anchored_entry_wins_over_the_number_key() -> None:
    """锚点命中的题即使不在它保存时的题号上，也必须用这份权重。"""
    cfg = {2: {"type": "single", "weights": [0.9, 0.1],
               "anchor": {"title": "您的性别", "signature": "single:2"}}}
    q = _q(7, "您的性别", choices=[1, 2])       # 插题后跑到第 7 格
    assert anchoring.lookup_weight_entry(q, cfg) is cfg[2]


def test_stale_number_hit_is_refused() -> None:
    """本题号上坐着的是一条**别人的**锚点条目 → 不能按题号拿它。

    这一条是整个模块的存在理由：错位从来不报错，只是安静地给出错的分布。
    """
    cfg = {7: {"type": "single", "weights": [0.9, 0.1],
               "anchor": {"title": "您的性别", "signature": "single:2"}}}
    q = _q(7, "您的最高学历", choices=[1, 2, 3, 4])
    assert anchoring.lookup_weight_entry(q, cfg) is None


def test_unanchored_entry_still_uses_the_number() -> None:
    """契约 2：老配置（手写 JSON、无 anchor）行为与 v2.8 逐位一致。"""
    cfg = {3: {"type": "single", "weights": [1, 2, 3, 4]}}
    assert anchoring.lookup_weight_entry(_q(3, "任意题干"), cfg) is cfg[3]


def test_string_number_keys_are_accepted() -> None:
    """键可能是 "3"（直接 json.load 没走 load_weight_config 的形态）。"""
    cfg = {"3": {"type": "single", "weights": [1, 1]}}
    assert anchoring.lookup_weight_entry({"q": 3, "type": "single"}, cfg) is cfg["3"]


def test_empty_config_returns_none() -> None:
    assert anchoring.lookup_weight_entry(_q(1, "x"), {}) is None
    config.WEIGHT_CONFIG.clear()
    assert anchoring.lookup_weight_entry(_q(1, "x")) is None


def test_lookup_reads_the_global_by_default() -> None:
    config.WEIGHT_CONFIG[5] = {
        "type": "single", "weights": [1, 1],
        "anchor": {"title": "您的性别", "signature": "single:2"},
    }
    assert anchoring.lookup_weight_entry(_q(11, "您的性别", choices=[1, 2])) is (
        config.WEIGHT_CONFIG[5]
    )


# ===========================================================================
#  与作答链路的接线（"算了却没传"这类缺陷的历史现场）
# ===========================================================================
def test_build_answer_strategy_uses_the_anchored_entry() -> None:
    """加权确实来自锚点条目 —— 接线断了这里就红。"""
    config.WEIGHT_CONFIG[2] = {
        "type": "single", "weights": [1.0, 0.0, 0.0, 0.0],
        "anchor": {"title": "您的性别", "signature": "single:4"},
    }
    picked = {build_answer_strategy(_q(9, "您的性别"))[0] for _ in range(60)}
    assert picked == {1}


def test_build_answer_strategy_falls_back_to_uniform_when_the_stems_differ() -> None:
    """题号撞上但题干不是那道题 → 该题走等权，而不是套用错位的确定性分布。"""
    config.WEIGHT_CONFIG[9] = {
        "type": "single", "weights": [1.0, 0.0, 0.0, 0.0],
        "anchor": {"title": "您的性别", "signature": "single:4"},
    }
    picked = {build_answer_strategy(_q(9, "您的最高学历"))[0] for _ in range(200)}
    assert len(picked) > 1, "错位配置被用上了：只选出了一个选项"


# ===========================================================================
#  未命中报告（契约 3）
# ===========================================================================
def test_unmatched_anchor_is_reported_once_per_process() -> None:
    cfg = {4: {"type": "single", "weights": [1, 1],
               "anchor": {"title": "您对客服的态度满意吗", "signature": "single:5"}}}
    questions = [_q(1, "您的性别", choices=[1, 2])]

    first = anchoring.report_unmatched_anchors(cfg, questions)
    second = anchoring.report_unmatched_anchors(cfg, questions)

    assert len(first) == 1
    assert "不生效" in first[0]
    assert "您对客服的态度满意吗" in first[0]
    assert second == [], "每份问卷都刷一行会把运行信息埋掉"


def test_matched_anchor_is_not_reported() -> None:
    anchor = {"title": "您的性别", "signature": "single:2"}
    cfg = {4: {"type": "single", "weights": [1, 1], "anchor": anchor}}
    assert anchoring.report_unmatched_anchors(cfg, [_q(2, "您的性别", choices=[1, 2])]) == []


def test_unanchored_entries_are_never_reported() -> None:
    """契约 2 的另一面：老配置没有 anchor 就不该被要求认领。"""
    cfg = {4: {"type": "single", "weights": [1, 1]}}
    assert anchoring.report_unmatched_anchors(cfg, [_q(1, "别的题")]) == []


# ===========================================================================
#  配置校验与落盘
# ===========================================================================
@pytest.mark.parametrize("bad_anchor", [
    "您的性别",
    {},
    {"title": "   "},
    {"title": "性别", "signature": 12},
])
def test_validate_weight_config_rejects_malformed_anchor(bad_anchor: object) -> None:
    errs = validate_weight_config(
        {1: {"type": "single", "weights": [1, 1], "anchor": bad_anchor}}
    )
    assert errs, f"畸形 anchor 必须被拒绝，实际放行: {bad_anchor!r}"


def test_validate_weight_config_accepts_a_well_formed_anchor() -> None:
    assert validate_weight_config({
        1: {"type": "single", "weights": [1, 1],
            "anchor": {"title": "您的性别", "signature": "single:2"}},
    }) == []


def test_saved_file_carries_anchors_and_schema_3(tmp_path) -> None:
    """存出去的东西必须带 anchor 与 3.0，否则下次读回来照样按题号错位。"""
    from src.config_io import load_weight_config

    path = str(tmp_path / "cfg.json")
    cfg = {2: {"type": "single", "weights": [0.5, 0.5],
               "anchor": {"title": "您的性别", "signature": "single:2"}}}
    save_weight_config(path, cfg)

    loaded, _meta = load_weight_config(path)
    assert loaded[2]["anchor"]["title"] == "您的性别"
    assert validate_weight_config(loaded) == []
    assert SCHEMA_VERSION == "3.0"


def test_legacy_schema_2_file_still_loads_and_validates(tmp_path) -> None:
    """向后兼容：v2.x 的预设没有 anchor，照旧按题号跑（只是少了错位保护）。"""
    import json

    from src.config_io import load_weight_config

    path = tmp_path / "old.json"
    path.write_text(json.dumps({
        "schema_version": "2.0",
        "config": {"1": {"type": "single", "weights": [0.2, 0.8]}},
    }), encoding="utf-8")

    loaded, _meta = load_weight_config(str(path))
    assert loaded[1]["weights"] == [0.2, 0.8]
    assert "anchor" not in loaded[1]
    assert validate_weight_config(loaded) == []
