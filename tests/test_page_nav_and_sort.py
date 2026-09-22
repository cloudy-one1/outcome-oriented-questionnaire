"""分页翻页（``page_nav``）与排序题作答的离线契约（v3.0）。

翻页那条为什么必须离线也测一遍：E2E 只走"两页都正常"这一条 happy path，
而真正的风险是**误判**——把单页问卷当成还有下一页去点、或者点了却没换页还照交。
这两种误判在真页面上分别对应"提前提交/白等 8 秒后仍提交"，都要靠出口枚举钉住。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import anchoring, config  # noqa: E402
from src.answering_v2 import generate_answer  # noqa: E402
from src.config_io import validate_weight_config  # noqa: E402
from src.pipeline_stages import page_nav  # noqa: E402


class FakePageDriver:
    """按脚本内容回分：分页探测 / 翻页点击 / 题目探测三类。

    ``numbers`` 可以在"翻页点击"时被改写，用来模拟"点了按钮页面真的换了"
    与"点了却没反应"两种结局。
    """

    def __init__(
        self,
        *,
        pages: int = 2,
        numbers_per_call: list[list[int]] | None = None,
        click_reply: str = "clicked:#btnNext",
    ) -> None:
        self.pages = pages
        self.queue = list(numbers_per_call or [[1, 2], [3]])
        self.click_reply = click_reply
        self.clicks = 0
        self.probes = 0

    def execute_script(self, script: str, *_a: object, **_k: object) -> object:
        # 三段脚本按各自的开头区分：分页探测 / 翻页点击 / 题目探测
        if script.lstrip().startswith("var nodes ="):
            return json.dumps({"pages": self.pages, "shown": 1})
        if script.lstrip().startswith("var sels ="):
            self.clicks += 1
            return self.click_reply
        if "return JSON.stringify(result)" in script:
            nums = self.queue[min(self.probes, len(self.queue) - 1)]
            self.probes += 1
            return json.dumps([{"q": n, "type": "single", "choices": [1, 2]}
                               for n in nums])
        return None


def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture()
def driver_factory():
    return FakePageDriver


# ===========================================================================
#  page_counts
# ===========================================================================
def test_page_counts_parses_the_wrapper_probe() -> None:
    d = FakePageDriver(pages=3)
    assert page_nav.page_counts(d) == (3, 1)


def test_page_counts_degrades_to_single_page_on_garbage() -> None:
    """探测本身不可信时按"单页"处理：最坏只是不翻页，而不是去点像下一页的键。"""
    class Garbage:
        def execute_script(self, *_a, **_k):
            return {"pages": 2}          # 不是 JSON 字符串

    assert page_nav.page_counts(Garbage()) == (0, 0)

    class Boom:
        def execute_script(self, *_a, **_k):
            raise RuntimeError("session gone")

    assert page_nav.page_counts(Boom()) == (0, 0)


# ===========================================================================
#  advance_to_next_page 的四个出口
# ===========================================================================
def test_single_page_survey_is_never_asked_for_a_next_button() -> None:
    """pages < 2 直接判"没有下一页"，且**一次点击都不做**。

    点错提交键的代价比少翻一页大得多：SUBMIT_SELECTORS 里就有 #ctlNext 这种
    既是翻页键又是提交键的 id。
    """
    d = FakePageDriver(pages=1)
    verdict, detail = page_nav.advance_to_next_page(d, {1, 2}, _sleep=_no_sleep)
    assert verdict == "no_more"
    assert d.clicks == 0
    assert "单页" in detail


def test_no_next_button_means_last_page() -> None:
    d = FakePageDriver(pages=2, click_reply="no_button")
    verdict, _ = page_nav.advance_to_next_page(d, {1, 2}, _sleep=_no_sleep)
    assert verdict == "no_more"
    assert d.clicks == 1


def test_advanced_when_visible_question_numbers_change() -> None:
    d = FakePageDriver(pages=2, numbers_per_call=[[1, 2], [3, 4]])
    verdict, detail = page_nav.advance_to_next_page(d, {1, 2}, _sleep=_no_sleep)
    assert verdict == "advanced"
    assert "3" in detail


def test_click_that_changes_nothing_is_reported_failed() -> None:
    """点了按钮但可见题号没变 → failed。调用方必须因此**不提交**。

    只交了第一页的问卷在服务端仍可能算一份完整回收，那是最坏的静默失败。
    """
    d = FakePageDriver(pages=2, numbers_per_call=[[1, 2], [1, 2]])
    verdict, detail = page_nav.advance_to_next_page(
        d, {1, 2}, timeout=0.5, _sleep=_no_sleep
    )
    assert verdict == "failed"
    assert "没变化" in detail


def test_unexpected_script_reply_is_failed_not_guessed() -> None:
    d = FakePageDriver(pages=2, click_reply="weird")
    verdict, detail = page_nav.advance_to_next_page(d, {1, 2}, _sleep=_no_sleep)
    assert verdict == "failed"
    assert "意外值" in detail


# ===========================================================================
#  排序题答案
# ===========================================================================
_SORT_Q = {"q": 13, "type": "sort", "items": ["1", "2", "3", "4"]}


@pytest.fixture(autouse=True)
def _clean_weight_config():
    saved = dict(config.WEIGHT_CONFIG)
    config.WEIGHT_CONFIG.clear()
    yield
    config.WEIGHT_CONFIG.clear()
    config.WEIGHT_CONFIG.update(saved)


def test_sort_answer_is_a_permutation_of_every_item() -> None:
    """每一项都必须有一个位置 —— 漏一项整题无效。"""
    for _ in range(50):
        ans = generate_answer(dict(_SORT_Q))
        assert ans["type"] == "sort"
        assert sorted(ans["order"]) == ["1", "2", "3", "4"]


def test_sort_weights_bias_the_first_position() -> None:
    config.WEIGHT_CONFIG[13] = {
        "type": "sort", "weights": [10, 0, 0, 0],
    }
    firsts = {generate_answer(dict(_SORT_Q))["order"][0] for _ in range(40)}
    assert firsts == {"1"}, f"权重全压在 1 上却排出了别的首位: {firsts}"


def test_sort_fixed_order_keeps_the_prefix_and_fills_the_rest() -> None:
    """用户只钉了前两名 → 其余随机补在后面，而不是被丢掉。"""
    config.WEIGHT_CONFIG[13] = {"type": "sort", "order": ["4", "2"]}
    for _ in range(30):
        order = generate_answer(dict(_SORT_Q))["order"]
        assert order[:2] == ["4", "2"]
        assert sorted(order) == ["1", "2", "3", "4"]


def test_sort_signature_and_anchor() -> None:
    assert anchoring.question_signature(dict(_SORT_Q)) == "sort:4"
    # 没题干就没有锚点（题干是锚定的唯一入口）
    assert anchoring.make_anchor(dict(_SORT_Q)) is None
    with_title = dict(_SORT_Q, title="请把下列渠道排序")
    anchor2 = anchoring.make_anchor(with_title)
    assert anchor2 is not None and anchor2["signature"] == "sort:4"
    assert anchoring.anchor_matches_question(anchor2, with_title)
    # 选项数变了就是另一道题
    assert not anchoring.anchor_matches_question(
        anchor2, dict(with_title, items=["1", "2", "3"])
    )


# ===========================================================================
#  排序题配置校验
# ===========================================================================
@pytest.mark.parametrize("bad", [
    {"type": "sort", "order": "1,2,3"},
    {"type": "sort", "order": []},
    {"type": "sort", "order": [1, 1, 2]},
    {"type": "sort", "weights": "1,2"},
])
def test_validate_rejects_malformed_sort_config(bad: dict) -> None:
    assert validate_weight_config({13: bad}), f"应拒绝 {bad}"


def test_validate_accepts_a_sane_sort_config() -> None:
    assert validate_weight_config({
        13: {"type": "sort", "order": ["3", "1"], "weights": [1, 2, 3, 4]},
    }) == []


# ===========================================================================
#  v3.0 平台层（src/platforms.py）
# ===========================================================================
def test_url_host_routing_and_notice() -> None:
    from src import platforms

    assert platforms.platform_for_url("https://v.wjx.cn/vm/abc.aspx") is platforms.WJX
    assert platforms.platform_for_url("https://example.com/form") is None
    # 子串匹配不能误伤：evilwjx.cn 不是 wjx.cn
    assert platforms.platform_for_url("https://evilwjx.cn/x") is None
    notice = platforms.unsupported_url_notice("https://someothersite.com/q")
    assert notice and "探测不到题目" in notice
    assert platforms.unsupported_url_notice("https://www.wjx.cn/vm/x.aspx") is None


@pytest.mark.parametrize("url, key", [
    # 同一份问卷的多种投放形态 + 渠道参数 → 同一个键
    ("https://www.wjx.cn/jq/77295530.aspx", "wjx.cn:77295530"),
    ("https://www.wjx.cn/m/77295530.aspx", "wjx.cn:77295530"),
    ("https://www.wjx.cn/vm/77295530.aspx?kd=abc&source=wx", "wjx.cn:77295530"),
    ("https://www.wjx.cn/hj/77295530.aspx#wechat", "wjx.cn:77295530"),
    ("https://www.wjx.cn/vj/Pq8k2A.aspx", "wjx.cn:Pq8k2A"),
    ("https://www.wjx.cn/VJ/Pq8k2A.aspx", "wjx.cn:Pq8k2A"),   # host 归一，短码原样
    # 结束页：标识在 query 上（同类项目的历史 issue 里参数名换过一次）
    ("https://www.wjx.cn/wjx/join/complete.aspx?activityid=77295530", "wjx.cn:77295530"),
    ("https://www.wjx.cn/wjx/join/complete.aspx?q=77295530", "wjx.cn:77295530"),
    # 刻意不合并的两类：跨 host、短码大小写
    ("https://v.wjx.cn/vj/Pq8k2A.aspx", "v.wjx.cn:Pq8k2A"),
    ("https://www.wjx.cn/vj/pq8k2a.aspx", "wjx.cn:pq8k2a"),
    # 非问卷星 URL / 空值只要不炸就行，匹配交由 history
    ("https://example.com/form/42", "example.com:42"),
    ("", ":"),
])
def test_canonical_survey_key_collapses_url_forms(url: str, key: str) -> None:
    from src import platforms

    assert platforms.canonical_survey_key(url) == key


def test_call_sites_read_selectors_from_the_platform_table() -> None:
    """常量搬进平台层后，三个调用点必须是**派生视图**而不是各自再抄一份。

    这一步断的是"搬家没搬漏"：只要还有一处留着字面量，换平台就会只换一半，
    比全没换更糟（看起来像支持了）。
    """
    from src.interactions import _scripts
    from src.pipeline_stages import page_loader, page_nav

    assert _scripts.SUBMIT_SELECTORS == list(page_loader.WJX.submit_selectors)
    assert page_nav.NEXT_PAGE_SELECTORS == list(page_loader.WJX.next_page_selectors)
    assert page_nav._PAGE_WRAPPER_SEL == page_loader.WJX.page_wrapper_selector
    assert page_loader.QUESTION_CONTROL_SELECTOR == page_loader.WJX.question_control_selector
    # 翻页键里绝不能有提交键
    assert "#ctlNext" not in page_nav.NEXT_PAGE_SELECTORS
    assert "#ctlNext" in _scripts.SUBMIT_SELECTORS
