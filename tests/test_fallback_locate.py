"""降级定位层（v4.4，``interactions/_relocate.py``）的离线契约。

钉三件事：

1. **接线**：主交互回执 False 时才走重定位；开关关 / 主路径成功时一次脚本
   都不多发（fallback 只能在失败路径上花时间）。
2. **回执契约不破**：重定位也失败时照旧回 False —— "找不到就 return false"
   的原子回执是 pipeline 三态判定与作答回执的地基。
3. **分发器透传**：``_answer_one_question`` 把 title / all_choices /
   fallback_locate 真的传进交互层（漏传 = 降级层永远不生效，而测试还全绿）。

relocated 脚本本身在真 DOM 里是否成立，由 E2E（test_e2e_integration.py 的
mock_wjx_shifted.html 用例）与 JS 语法矩阵（test_js_scripts.py）负责 ——
node --check 和离线替身都看不见"拼出来的 JS 在真 DOM 里成立"这件事。
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.interactions.choices import (  # noqa: E402
    js_click_question_options,
    js_fill_option_blank,
)
from src.interactions.text import js_fill_text  # noqa: E402
from src.pipeline_stages.question_stage import _answer_one_question  # noqa: E402


class RoutedDriver:
    """按脚本内容路由返回值的 execute_script 替身。

    ``routes`` 是 (标记子串, 返回值) 列表：脚本含标记 → 返回对应值；
    都不含 → ``default``。同时记录全部脚本，供"多发了一次脚本没有"的断言用。
    """

    def __init__(self, routes: list[tuple[str, Any]], default: Any = False) -> None:
        self.routes = routes
        self.default = default
        self.scripts: list[str] = []

    def execute_script(self, script: str, *_args: Any, **_kw: Any) -> Any:
        self.scripts.append(script)
        for marker, value in self.routes:
            if marker in script:
                return value
        return self.default


# relocated 脚本的唯一标记（finder 函数名只在降级层出现）
_RELOCATE = "__wjxFindQuestionContainer"


class RecordingDriver:
    """只记录被注入的脚本，永远返回"成功"（与 dispatch 测试同款）。"""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def execute_script(self, script: str, *_args: Any, **_kw: Any) -> Any:
        self.scripts.append(script)
        return True


def test_primary_hit_never_invokes_the_fallback() -> None:
    """主选择器命中 → 只发一次脚本；开关开着也不多发（失败路径才付费）。"""
    d = RoutedDriver(routes=[(_RELOCATE, "relocated:attr")], default=True)
    ok = js_click_question_options(
        d, 1, "single", [2],
        all_choices=[1, 2, 3, 4], title="性别", fallback_locate=True,
    )
    assert ok is True
    assert len(d.scripts) == 1, f"主路径成功不该有第二次脚本注入，实际 {len(d.scripts)} 次"


def test_primary_miss_triggers_relocate_and_succeeds() -> None:
    """主回执 False + 开关开 → 重定位脚本被注入，回执如实上抛。"""
    d = RoutedDriver(routes=[(_RELOCATE, "relocated:attr")], default=False)
    ok = js_click_question_options(
        d, 1, "single", [2],
        all_choices=[1, 2, 3, 4], title="性别", fallback_locate=True,
    )
    assert ok is True
    assert len(d.scripts) == 2, "应恰好是 主脚本 + 重定位脚本 两次注入"
    assert _RELOCATE in d.scripts[1]


def test_fallback_off_keeps_the_historical_contract() -> None:
    """开关关（默认）：失配即 False，绝不多发脚本 —— 与 v4.4 之前逐位一致。"""
    d = RoutedDriver(routes=[(_RELOCATE, "relocated:attr")], default=False)
    ok = js_click_question_options(
        d, 1, "single", [2],
        all_choices=[1, 2, 3, 4], title="性别", fallback_locate=False,
    )
    assert ok is False
    assert len(d.scripts) == 1


def test_relocate_failure_still_returns_false() -> None:
    """重定位也找不到 → 照旧回 False，回执契约不因新增路径而变松。"""
    d = RoutedDriver(routes=[(_RELOCATE, False)], default=False)
    ok = js_click_question_options(
        d, 1, "multi", [2, 3],
        all_choices=[1, 2, 3, 4], title="性别", fallback_locate=True,
    )
    assert ok is False
    assert len(d.scripts) == 2


def test_multi_relocate_receives_multi_semantics() -> None:
    """多选的降级点击必须带 is_multi=True（清全部再逐个勾，不是单选语义）。"""
    d = RoutedDriver(routes=[(_RELOCATE, "relocated:title")], default=False)
    ok = js_click_question_options(
        d, 2, "multi", [1, 3],
        all_choices=[1, 2, 3, 4], title="渠道", fallback_locate=True,
    )
    assert ok is True
    assert "var isMulti = true" in d.scripts[1]


def test_fill_text_fallback_wiring() -> None:
    d = RoutedDriver(routes=[(_RELOCATE, "relocated:attr")], default=False)
    ok = js_fill_text(d, 5, "13800000000", title="手机号", fallback_locate=True)
    assert ok is True
    assert len(d.scripts) == 2 and _RELOCATE in d.scripts[1]

    d2 = RoutedDriver(routes=[], default=False)
    assert js_fill_text(d2, 5, "13800000000", title="手机号", fallback_locate=False) is False
    assert len(d2.scripts) == 1


def test_option_blank_fallback_wiring() -> None:
    d = RoutedDriver(routes=[(_RELOCATE, "relocated:attr")], default=False)
    ok = js_fill_option_blank(
        d, 2, 6, "其他原因",
        all_choices=[1, 2, 3, 4, 5, 6], title="渠道", fallback_locate=True,
    )
    assert ok is True
    assert len(d.scripts) == 2 and _RELOCATE in d.scripts[1]


def test_answer_step_threads_title_choices_and_switch(monkeypatch) -> None:
    """分发器必须把 title / all_choices / fallback_locate 传进交互层。

    漏传任何一项，降级层要么永远不生效（开关/题干），要么映射不可靠
    （all_choices）—— 而本文件其余用例全绿，只有这条能抓到。
    """
    seen: list[dict] = []

    def fake_click(_d: Any, _q: int, _t: str, vals: list, **kw: Any) -> bool:
        seen.append({"vals": vals, **kw})
        return False  # 主路径失败，迫使 kwargs 全量走完

    monkeypatch.setattr(
        "src.pipeline_stages.question_stage.js_click_question_options", fake_click
    )
    q = {"q": 1, "type": "single", "choices": [1, 2, 3], "title": "您的性别是？"}
    assert _answer_one_question(RecordingDriver(), q, fallback_locate=True) is False
    assert len(seen) == 1
    # 选中值来自等权随机（1..3 各可能），这里只钉透传契约，不钉生成结果
    assert seen[0]["vals"] and seen[0]["vals"][0] in (1, 2, 3)
    assert seen[0]["all_choices"] == [1, 2, 3]
    assert seen[0]["title"] == "您的性别是？"
    assert seen[0]["fallback_locate"] is True
