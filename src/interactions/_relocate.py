"""选择器降级定位层（v4.4，借鉴 Scrapling 的"元素自动重定位"思想）。

主交互脚本（_scripts.py）的定位全部押在平台生成的精确 id/name 上
（``#q{N}_{M}`` / ``input[name=q{N}][value=M]`` / ``#q{N}``），单腿无兜底；
而题目容器 id 在真卷上本就有三种模板形态并存（``WJX_QUESTION_ANCHOR_SELECTORS``）
—— 模板改版时主选择器失配，本题静默落空，只能靠提交前自检兜底。

本层是**主路径失败后的第二次尝试**，不破坏"找不到就 return false"的原子回执
契约：主交互返回 False → Python 侧调用本层（一次，不再重试）→ 仍失败照旧回
False。定位走两条腿：

1. **属性锚点**：``div[topic="{q}"]`` —— 探测能认出这道题，说明平台自报的
   topic/type 属性还在；选择器从 ``platforms.WJX.question_marker_selectors``
   注入（平台 DOM 约定集中一张表的原则）。
2. **题干文本锚点**：容器规范化文本包含探测记下的题干（``q["title"]``），
   多命中取"装有输入控件、文本最短"的那个（最内层）；纯题面命中沿祖先链
   有限上爬（≤5 层）。

拿到容器后**不猜**：容器内 radio/checkbox 数必须与探测的 choices 数一致才按
DOM 序建立 value→控件映射，否则放弃。事件链与主脚本共用同一份
（``_scripts.interaction_chain_helper_script``）。

定位成功时 print 一行 pipeline 风格的诊断（``[relocate] Q{q} ...``），说明走的
哪条腿 —— 这是排障时判断"平台是不是改版了"的第一现场。
"""

from __future__ import annotations

import json
from typing import Any

from ..platforms import WJX
from ._scripts import (
    interaction_chain_helper_script,
    option_blank_helper_script,
)


def relocate_finder_helper_script() -> str:
    """JS 函数声明组：两腿定位 + 选项控件枚举（relocated 脚本共用）。

    与主脚本共用 ``interaction_chain_helper_script`` 的理由见其 docstring；
    本组只做"找容器"，不做任何交互。

    脚本回执约定：``__relocateHow`` 记录命中的腿（``attr`` / ``title``），
    各 relocated 脚本把它拼进返回值（``'relocated:attr'``），Python 侧据此打
    诊断行。找不到容器返回 null。
    """
    markers_json = json.dumps(list(WJX.question_marker_selectors), ensure_ascii=False)
    return f"""
        var __relocateHow = '';
        var __relocateMarkers = {markers_json};

        function __wjxNormText(s) {{
            return String(s == null ? '' : s).replace(/\\s+/g, '').trim();
        }}
        function __wjxHasInteractive(el) {{
            return !!el.querySelector('input[type="radio"], input[type="checkbox"], select,'
                + ' textarea, input[type="text"], input[type="tel"], input[type="number"]');
        }}
        function __wjxQuestionInputs(container) {{
            return Array.prototype.slice.call(
                container.querySelectorAll('input[type="radio"], input[type="checkbox"]')
            ).filter(function(el) {{ return !el.disabled; }});
        }}
        function __wjxFindQuestionContainer(q, title) {{
            // ---- 第一腿：平台自报的 topic 属性 ----
            for (var i = 0; i < __relocateMarkers.length; i++) {{
                var nodes;
                try {{ nodes = document.querySelectorAll(__relocateMarkers[i]); }} catch (_) {{ continue; }}
                for (var j = 0; j < nodes.length; j++) {{
                    if (String(nodes[j].getAttribute('topic')) === String(q)) {{
                        __relocateHow = 'attr';
                        return nodes[j];
                    }}
                }}
            }}
            // ---- 第二腿：题干文本锚点（规范化空白后"容器文本包含题干"） ----
            var want = __wjxNormText(title);
            if (!want) return null;
            var pool = [];
            for (var i2 = 0; i2 < __relocateMarkers.length; i2++) {{
                try {{
                    pool = pool.concat(Array.prototype.slice.call(
                        document.querySelectorAll(__relocateMarkers[i2])));
                }} catch (_) {{}}
            }}
            if (!pool.length) {{
                pool = Array.prototype.slice.call(document.querySelectorAll('div, fieldset, li'));
            }}
            var hits = [];
            for (var k = 0; k < pool.length; k++) {{
                var txt = __wjxNormText(pool[k].textContent);
                if (txt && txt.indexOf(want) !== -1) hits.push(pool[k]);
            }}
            if (!hits.length) return null;
            // 优先"装有输入控件的命中"，再取文本最短的（最内层、最贴近题面）
            var withInputs = hits.filter(__wjxHasInteractive);
            var cands = withInputs.length ? withInputs : hits;
            var best = cands[0];
            for (var k2 = 1; k2 < cands.length; k2++) {{
                if (__wjxNormText(cands[k2].textContent).length
                        < __wjxNormText(best.textContent).length) {{
                    best = cands[k2];
                }}
            }}
            // 命中可能是纯题面节点（不含控件）：沿祖先链有限上爬
            if (!__wjxHasInteractive(best)) {{
                for (var up = best.parentElement, hop = 0;
                        up && up !== document.body && hop < 5;
                        up = up.parentElement, hop++) {{
                    if (__wjxHasInteractive(up)) {{
                        __relocateHow = 'title';
                        return up;
                    }}
                }}
                return null;
            }}
            __relocateHow = 'title';
            return best;
        }}
    """


def relocate_click_options_script(
    q: int,
    targets: list,
    all_choices: list,
    title: str | None,
    *,
    is_multi: bool,
) -> str:
    """重定位后的选项点击（单选/多选共用，``is_multi`` 分流）。

    容器内 radio/checkbox 数必须与探测 choices 数一致才建 value→控件映射，
    否则放弃 —— 数对不上说明模板形状不明，按序号猜点会点错选项。
    事件链走 :func:`_scripts.interaction_chain_helper_script`（与主脚本同源）。
    """
    q_json = json.dumps(q)
    targets_json = json.dumps([int(t) for t in targets])
    all_json = json.dumps([int(c) for c in all_choices])
    title_json = json.dumps(str(title or ""))
    multi_json = json.dumps(bool(is_multi))
    return f"""
        {interaction_chain_helper_script()}
        {relocate_finder_helper_script()}
        var q = {q_json};
        var targets = {targets_json};
        var allChoices = {all_json};
        var title = {title_json};
        var isMulti = {multi_json};

        var container = __wjxFindQuestionContainer(q, title);
        if (!container) return false;
        var inputs = __wjxQuestionInputs(container);
        if (inputs.length !== allChoices.length) return false;
        var byValue = {{}};
        for (var i = 0; i < allChoices.length; i++) {{
            byValue[String(allChoices[i])] = inputs[i];
        }}

        if (!isMulti) {{
            var input = byValue[String(targets[0])];
            if (!input) return false;
            __wjxHumanClickFull(input);
            if (input.type === 'radio') {{
                __wjxClearCheckedSiblings('input[type="radio"]', container, input);
            }}
            return 'relocated:' + __relocateHow;
        }}

        __wjxClearCheckedSiblings('input[type="radio"], input[type="checkbox"]', container, null);
        var any_hit = false;
        for (var t = 0; t < targets.length; t++) {{
            var input2 = byValue[String(targets[t])];
            if (!input2) return false;
            __wjxHumanClickSimple(input2);
            any_hit = true;
        }}
        return any_hit ? ('relocated:' + __relocateHow) : false;
    """


def relocate_fill_text_script(q: int, text: str, title: str | None) -> str:
    """重定位后的单框填空。

    只认"容器里恰好一个可填框"：多空题各框有独立提交槽名、框序即语义，
    框数 >1 时按哪个填都是猜 —— 留给主路径的 6 候选与多空脚本。
    写入链走 ``__wjxFillTextLike``（与主脚本同源）。
    """
    q_json = json.dumps(q)
    text_json = json.dumps(str(text))
    title_json = json.dumps(str(title or ""))
    return f"""
        {interaction_chain_helper_script()}
        {relocate_finder_helper_script()}
        var q = {q_json};
        var txt = {text_json};
        var title = {title_json};

        var container = __wjxFindQuestionContainer(q, title);
        if (!container) return false;
        var cand = Array.prototype.slice.call(container.querySelectorAll(
            'textarea, input[type="text"], input[type="tel"], input[type="number"],'
            + ' input:not([type])'
        )).filter(function(el) {{
            if (el.disabled || el.readOnly) return false;
            var t = (el.type || '').toLowerCase();
            return t !== 'radio' && t !== 'checkbox' && t !== 'hidden';
        }});
        if (cand.length !== 1) return false;
        __wjxFillTextLike(cand[0], txt);
        return 'relocated:' + __relocateHow;
    """


def relocate_fill_option_blank_script(
    q: int, choice, all_choices: list, text: str, title: str | None
) -> str:
    """重定位后的"选项自带填空框"写入（"其他____"）。

    框的识别与主脚本共用 ``optionBlankInput``（结构判据，同源约定）；
    写入链共用 ``__wjxWriteOptionBlank``。
    """
    q_json = json.dumps(q)
    choice_json = json.dumps(str(choice))
    all_json = json.dumps([int(c) for c in all_choices])
    text_json = json.dumps(str(text))
    title_json = json.dumps(str(title or ""))
    return f"""
        {option_blank_helper_script()}
        {interaction_chain_helper_script()}
        {relocate_finder_helper_script()}
        var q = {q_json};
        var choice = {choice_json};
        var allChoices = {all_json};
        var txt = {text_json};
        var title = {title_json};

        var container = __wjxFindQuestionContainer(q, title);
        if (!container) return false;
        var inputs = __wjxQuestionInputs(container);
        if (inputs.length !== allChoices.length) return false;
        var byValue = {{}};
        for (var i = 0; i < allChoices.length; i++) {{
            byValue[String(allChoices[i])] = inputs[i];
        }}
        var input = byValue[String(choice)];
        if (!input) return false;
        var blank = optionBlankInput(input);
        if (!blank) return false;
        return __wjxWriteOptionBlank(blank, txt)
            ? ('relocated:' + __relocateHow) : false;
    """


# ============================================================================
#  Python 包装：执行 + 诊断
# ============================================================================

def _report(q: int, how_result: Any, action: str) -> bool:
    """把 relocated 脚本的回执转成 bool，成功时打一行 pipeline 风格诊断。"""
    ok = bool(how_result) and isinstance(how_result, str) and how_result.startswith("relocated:")
    if ok:
        leg = "属性锚点" if "attr" in str(how_result) else "题干锚点"
        print(f"  [relocate] Q{q} 主选择器失配，{leg}重定位成功（{action}）")
    return ok


def relocate_click_options(
    driver: Any,
    q: int,
    targets: list,
    all_choices: list,
    title: str | None,
    *,
    is_multi: bool = False,
) -> bool:
    """主选项点击失败后的重定位重试（一次，不递归）。"""
    return _report(
        q,
        driver.execute_script(
            relocate_click_options_script(q, targets, all_choices, title, is_multi=is_multi)
        ),
        "选项点击",
    )


def relocate_fill_text(driver: Any, q: int, text: str, title: str | None) -> bool:
    """主填空失败后的重定位重试（一次，不递归）。"""
    return _report(
        q,
        driver.execute_script(relocate_fill_text_script(q, text, title)),
        "填空写入",
    )


def relocate_fill_option_blank(
    driver: Any, q: int, choice, all_choices: list, text: str, title: str | None
) -> bool:
    """主"选项自带填空"失败后的重定位重试（一次，不递归）。"""
    return _report(
        q,
        driver.execute_script(
            relocate_fill_option_blank_script(q, choice, all_choices, text, title)
        ),
        "选项自带填空",
    )
