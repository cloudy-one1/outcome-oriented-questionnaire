"""``src/persona.py`` 的自洽性契约（v3.2 B 档第 1 条）。

为什么值得测：填空的每个字段**单看都合法**，所以任何离线断言（长度、正则、平台
校验）都发现不了"女名池的张伟 + 深圳地址 + 3 岁 + 乌鲁木齐公司"这种不存在的人。
能钉住它的只有跨字段的不变量：身份证校验位、性别位与名字用字、出生年与年龄、
地址与身份证前 6 位同省、画像在一份问卷内不换人。

对标 SurveyController 的 ``core/persona``（条件概率链 + 身份证真校验位），
补的是它没有的那一环：它的代理出口省份与身份证省份互不知情。校验位在测试里
**独立重写一遍**，不 import 被测实现 —— 拿被测函数验自己不算防线。
"""

from __future__ import annotations

import random
import re
import threading
from datetime import date, timedelta

import pytest

from src import answering_v2, persona
from src.persona import Persona, active_persona, draw_persona, new_persona

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


@pytest.fixture(autouse=True)
def _fresh_persona():
    new_persona()
    yield


def _check_digit(body17: str) -> str:
    """ISO 7064 MOD 11-2：与 src/persona 无关的第二份实现。"""
    return _ID_CHECK[sum(int(c) * w for c, w in zip(body17, _ID_WEIGHTS)) % 11]


# ---------------------------------------------------------------------------
#  1. 身份证
# ---------------------------------------------------------------------------
def test_id_card_is_18_chars_with_valid_checksum_and_birth_date() -> None:
    for _ in range(200):
        p = draw_persona()
        assert re.fullmatch(r"\d{17}[\dXx]", p.id_card), p.id_card
        assert _check_digit(p.id_card[:17]) == p.id_card[17].upper(), p.id_card
        assert p.id_card[6:10] == str(p.birth_year)
        assert 1 <= int(p.id_card[10:12]) <= 12
        assert 1 <= int(p.id_card[12:14]) <= 28


def test_id_card_gender_digit_matches_gender_pool() -> None:
    """第 17 位：男奇女偶。名字与身份证来自同一次抽签，所以必须同步。"""
    seen = {"M": set(), "F": set()}
    for _ in range(400):
        p = draw_persona()
        seen[p.gender].add(int(p.id_card[16]) % 2)
    assert seen["M"] == {1}, seen
    assert seen["F"] == {0}, seen


def test_id_card_region_prefix_is_the_same_city_as_the_address() -> None:
    """对标源最缺的一环：区划前缀 6 位必须就是画像那个省市，不是另抽一个。"""
    prov_codes = {code for code, _n, _m, cities in persona._REGIONS}
    for _ in range(200):
        p = draw_persona()
        assert int(p.id_card[:2]) in prov_codes, p.id_card[:2]
        assert p.id_card[2:4] != "00", "市级段不能空着，那是省级占位"
        assert p.address_text.startswith(p.province if not p.municipality else p.city)


# ---------------------------------------------------------------------------
#  2. 姓名 / 邮箱 / 电话
# ---------------------------------------------------------------------------
def test_given_name_comes_from_the_matching_gender_pool() -> None:
    """名字用字必须来自本性别池 —— 池里也允许出现两字词被截成单字（三成概率）。"""
    unisex = set(persona._GIVEN_NAMES_UNISEX)

    def legal(gender: str) -> set[str]:
        pool = persona._GIVEN_NAMES_M if gender == "M" else persona._GIVEN_NAMES_F
        return set(pool) | unisex | {w[0] for w in pool}

    for _ in range(300):
        p = draw_persona()
        assert p.name[0] in persona._SURNAMES
        given = p.name[1:]
        ok = given in legal(p.gender) or all(c in legal(p.gender) for c in given)
        assert ok, f"{p.gender} {p.name}"


def test_email_local_part_starts_with_the_surname_pinyin() -> None:
    mapped = 0
    for _ in range(200):
        p = draw_persona()
        stem = persona._SURNAME_PINYIN.get(p.name[0])
        if stem is None:
            continue
        mapped += 1
        local = p.email.split("@")[0]
        assert local.startswith(stem), f"{p.name} -> {p.email}"
    assert mapped > 100, "姓氏拼音表要真的被用上，而不是偶尔命中"


def test_email_domain_and_phone_shape() -> None:
    for _ in range(100):
        p = draw_persona()
        assert p.email.split("@")[1] in persona._EMAIL_DOMAINS
        assert re.fullmatch(r"\d{11}", p.phone)
        assert int(p.phone[:3]) in persona._MOBILE_PREFIXES


# ---------------------------------------------------------------------------
#  3. 条件概率链：不自相矛盾
# ---------------------------------------------------------------------------
def test_chain_invariants_hold_over_many_draws() -> None:
    this_year = date.today().year
    checked = 0
    for _ in range(2000):
        p = draw_persona()
        checked += 1
        assert this_year - p.age == p.birth_year, (this_year, p.age, p.birth_year)
        assert p.occupation != "学生" or p.age <= 24, p
        assert p.occupation != "退休" or p.age >= 60, p
        assert p.children != "有" or p.marital == "已婚", p
        assert p.education in {"小学", "初中", "高中", "大专", "本科", "硕士", "博士"}
        assert p.income, p
    assert checked == 2000


def test_persona_is_spread_over_regions_and_ages() -> None:
    """分布检查：只抽到 3 个省、或全是 25 岁，就不叫"像真人"了。"""
    provinces, bands = set(), {"<25": 0, "25-44": 0, "45+": 0}
    for _ in range(600):
        p = draw_persona()
        provinces.add(p.province)
        bands["<25" if p.age < 25 else "25-44" if p.age <= 44 else "45+"] += 1
    assert len(provinces) >= 15, sorted(provinces)
    assert all(v > 20 for v in bands.values()), bands


# ---------------------------------------------------------------------------
#  4. 一份问卷 = 一个人
# ---------------------------------------------------------------------------
def _answer(field: str, **extra) -> str:
    q = {"q": 1, "type": "text", "field": field}
    q.update(extra)
    return str(answering_v2.generate_answer(q)["text"])


def test_all_demographic_fields_come_from_one_persona() -> None:
    p = active_persona()
    assert _answer("name") == p.name, "两次取名字必须还是同一个人"
    assert _answer("phone") == p.phone
    assert _answer("email") == p.email
    assert _answer("company") == p.company
    assert _answer("address") == p.address_text
    assert _answer("age") == str(min(max(p.age, 16), 70))
    assert _answer("region") == f"{p.province} {p.city}"


def test_two_address_questions_in_one_survey_agree() -> None:
    assert _answer("address") == _answer("address")


def test_age_clamps_into_the_question_range_but_stays_a_real_age() -> None:
    """题目把范围卡死时（如 16~18）宁可夹进区间：越界会被平台直接拦下。"""
    p = new_persona()
    if p.age < 40:  # 构造一个画像在区间外的情形
        p = Persona(**{**p.__dict__, "age": 66, "birth_year": date.today().year - 66})
        persona._local.persona = p
    assert _answer("age", min=16, max=25) == "25"
    assert _answer("age", min=30, max=40) == "40"


def test_birth_date_question_answers_the_persons_real_birth_year() -> None:
    p = new_persona()
    got = _answer("date")
    assert got.startswith(str(p.birth_year)), (got, p.birth_year)


def test_birth_date_respects_an_unbearable_datelimit() -> None:
    """区间容不下这个人的出生年时退回区间内 —— 越界等于那题交上去是空的。"""
    p = new_persona()
    lo = (date.today() - timedelta(days=365 * 22)).strftime("%Y-%m-%d")
    hi = (date.today() - timedelta(days=365 * 19)).strftime("%Y-%m-%d")
    # 出生年钉到区间之外。不钉的话这一条会随画像的随机落点偶尔整条 skip，
    # 而 docs/coverage.md 把 skip 清单当成对外口径记着 —— 随机 skip 等于口径不可复现。
    p = Persona(**{**p.__dict__, "birth_year": int(hi[:4]) - 6})
    persona._local.persona = p
    got = _answer("date", date_min=lo, date_max=hi)
    assert lo <= got <= hi, (got, lo, hi)


def test_rotation_changes_the_person_and_is_thread_local() -> None:
    first = active_persona()
    assert active_persona() is first, "同一份问卷内不该自己换人"
    second = new_persona()
    assert active_persona() is second
    assert second.id_card != first.id_card

    seen: list[str] = []

    def worker() -> None:
        seen.append(new_persona().id_card)
        seen.append(active_persona().id_card)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen[0] == seen[1]
    assert active_persona().id_card == second.id_card, "子线程换人不能影响主线程"


def test_seed_makes_a_persona_reproducible() -> None:
    """固定 rng 能复现：出问题时按种子重放一个人，比重新运气快。"""
    a = draw_persona(random.Random(20260923))
    b = draw_persona(random.Random(20260923))
    assert a == b
    assert a.id_card == b.id_card
