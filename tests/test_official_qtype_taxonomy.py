"""题型范围的对外说法 ↔ 问卷星官方 OpenAPI 清单快照（`scripts/official_qtypes_0_4_5.json`）。

对标第五个源 `wjxcom/wjx-ai-kit`（问卷星官方开源）之后抽出来的两件：

1. **别把两套编号当成一套。** 官方 OpenAPI 的 `q_type` 与页面容器上的 `type` 属性不是同一套码：
   官方 1/2 是**分页栏 / 段落说明**，而 2026-09-22 真卷实测我们页面上的 1/2 是**填空 / 多行文本**；
   下拉、量表、矩阵、排序、多空填空五处的 `q_type` 也各不相同。拿官方码去"修正"
   `src/platforms.py::WJX_TYPE_CODES`，症状是结构对拍在每份问卷上刷假警、甚至把填空题当成分页栏
   跳过 —— 所以这个错误值得由测试挡住，而不是靠注释被读到。
2. **"九类题型全覆盖"这句话要有出处、也不能自我膨胀。** 我们作答的九类全部落在官方清单的
   stable-basic 档（12 种，官方"无需预检即可创建"那一档）；官方清单共 110 种，其中 86 种
   advanced、7 种框架草稿、5 种创建接口直接拒 —— 那 98 种我们不做，也不说成做。
   日期 / 时间在官方映射里**同样没有数字码**，所以"日期题只探测不作答"是被外部限制卡住，不是漏做。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import QuestionType  # noqa: E402
from src.platforms import WJX_TYPE_CODES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = json.loads((ROOT / "scripts" / "official_qtypes_0_4_5.json").read_text(encoding="utf-8"))
README_TEXT = (ROOT / "README.md").read_text(encoding="utf-8")

#: 我们归一化题型 → 官方清单里的中文题型名（一行一个，映射本身就是"我们声称的范围"）
TO_OFFICIAL_NAME: dict[str, list[str]] = {
    QuestionType.SINGLE.value: ["单选"],
    QuestionType.MULTI.value: ["多选"],
    QuestionType.DROPDOWN.value: ["下拉框"],
    QuestionType.SCALE.value: ["量表题", "NPS量表"],
    QuestionType.TEXT.value: ["单项填空", "简答题", "多项填空"],
    QuestionType.MATRIX_SINGLE.value: ["矩阵单选"],
    QuestionType.MATRIX_MULTI.value: ["矩阵多选"],
    QuestionType.MATRIX_SCALE.value: ["矩阵量表"],
    QuestionType.SORT.value: ["排序"],
}

#: 官方 q_type ≠ 页面容器 type 的五个位置（左侧是我们的码）
DIVERGENT = {
    "下拉": ("7", "下拉框", 3),
    "量表": ("5", "量表题", 3),
    "矩阵": ("6", "矩阵单选", 7),
    "排序": ("11", "排序", 4),
    "多空填空": ("9", "多项填空", 6),
}

_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
              "八": 8, "九": 9, "十": 10}


def fixture_type_kinds() -> int:
    """离线 mock 里出现过的**容器题型码**种类数（不是题数）。"""
    html = (ROOT / "tests" / "fixtures" / "mock_wjx.html").read_text(encoding="utf-8")
    return len(set(re.findall(r'type="(\d+)"', html)))


def readme_type_claims() -> list[tuple[int, int, str]]:
    """README 里每一处「N 类题型」断言：(行号, 写的数字, 该行原文)。"""
    out: list[tuple[int, int, str]] = []
    for number, line in enumerate(README_TEXT.splitlines(), start=1):
        for raw in re.findall(r"([一二三四五六七八九十]|\d+)\s*类题型", line):
            out.append((number, int(raw) if raw.isdigit() else _CN_DIGITS[raw], line.strip()))
    return out


def test_snapshot_is_internally_consistent() -> None:
    assert sum(SNAPSHOT["tiers"].values()) == SNAPSHOT["jsonl_qtype_total"]


def test_readme_type_counts_match_what_they_claim_about() -> None:
    """说工具的必须是 `QuestionType` 成员数，说 mock 卷的必须是卷里真有的码种数。

    两种口径混用是这里最容易犯的错：mock 卷只有 13 题、8 种容器码，
    而工具对外说的是九类。
    """
    claims = readme_type_claims()
    assert claims, "README 里找不到题型数量断言 —— 这条测试的靶子没了"
    wrong = [
        (line_no, written, text)
        for line_no, written, text in claims
        if written != (fixture_type_kinds() if "mock" in text.lower() else len(QuestionType))
    ]
    assert not wrong, "README 的题型数量与事实不符（工具看 QuestionType，mock 卷看 fixture）：\n" + "\n".join(
        f"  README.md:{n} 写了 {w}：{t[:70]}" for n, w, t in wrong
    )


def test_every_type_we_answer_exists_in_the_official_stable_tier() -> None:
    stable = set(SNAPSHOT["tier_members"]["stable_basic"])
    missing = [
        f"{ours}→{official}"
        for ours, official in TO_OFFICIAL_NAME.items()
        for name in official if name not in stable
    ]
    assert not missing, f"我们声称覆盖的题型不在官方 stable 档里：{missing}"
    assert set(TO_OFFICIAL_NAME) == {t.value for t in QuestionType}, (
        "QuestionType 加了成员却没在对照表里登记 → 对外那句「全覆盖」会悄悄过头"
    )


def test_we_claim_nothing_outside_the_conservative_tier() -> None:
    """官方另有 86 advanced + 7 草稿 + 5 直接拒 —— 我们九类全在 stable 档内，越界要说清。"""
    stable = set(SNAPSHOT["tier_members"]["stable_basic"])
    claimed = {n for names in TO_OFFICIAL_NAME.values() for n in names}
    assert claimed <= stable


def test_the_two_code_spaces_are_not_one() -> None:
    """拿官方 `q_type` 替换页面容器 `type` 码表，会把填空题当成分页栏 —— 这条专门挡它。"""
    api = SNAPSHOT["api_qtype_codes"]
    by_name = {entry.label: code for code, entry in WJX_TYPE_CODES.items()}   # 码就是字典的键
    for ours_label, (our_code, official_name, official_q_type) in DIVERGENT.items():
        assert our_code in WJX_TYPE_CODES, f"我们的容器码 {our_code}（{ours_label}）不在码表里了"
        assert by_name[ours_label] == our_code
        assert api[official_name][0] != int(our_code), (
            f"{ours_label}：我们的容器码 {our_code} 与官方 q_type 撞上了 —— "
            f"要么两套码真的合流了（需要重新实测真卷），要么码表被照官方改过"
        )


def test_official_page_and_paragraph_codes_are_not_ours() -> None:
    api = SNAPSHOT["api_qtype_codes"]
    assert (api["分页栏"][0], api["段落说明"][0]) == (1, 2)
    assert WJX_TYPE_CODES["1"].label == "填空"
    assert WJX_TYPE_CODES["2"].label == "多行文本"


def test_date_and_time_are_unmapped_on_both_sides() -> None:
    """官方创建/提交侧都没有日期与时间的数字码，我们也没有 —— 这条不是我们漏做。"""
    assert set(SNAPSHOT["api_unmapped_qtypes"]) == {"日期", "时间"}
    names = {code.label for code in WJX_TYPE_CODES.values()}
    assert not ({"日期", "时间"} & names)


def test_codes_we_declined_to_publish_stay_declined() -> None:
    """8 / 10 没有实测依据就不列：拿不准的码报错警，比不报更糟（用户会开始忽略所有对拍提示）。"""
    assert set(WJX_TYPE_CODES) == {"1", "2", "3", "4", "5", "6", "7", "9", "11"}
    assert {int(a) for a in SNAPSHOT["creatable_atypes"]} >= {1, 2, 3, 4, 5, 6, 7, 9, 11}
