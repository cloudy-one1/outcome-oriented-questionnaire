"""第六章整改：交互模块内嵌长 JS 脚本集中收纳（interactions/_scripts.py）。

设计原则（第六章）：
    1. 各题型 DOM 交互的「注入 JS 骨架」与 Python 控制流解耦。
    2. JS 字符串统一收口，便于单独检视/搜索/替换（比如批量修改 scrollIntoView 参数）。
    3. 本模块只负责「返回 JS 字符串」——不调用 Selenium，不改变行为，
       保证可以通过单测直接对比输出与原 f-string 结果完全一致。

使用方式：
    # 原先：
    driver.execute_script(f\""" ... {q} ... {choice} ... \"\"")
    # 现在：
    driver.execute_script(_scripts.click_option_script(q, choice))
"""

from __future__ import annotations

import json


# ============================================================================
#  choices 模块：单选 / 多选题
# ============================================================================

def click_option_script(q: int, choice: int) -> str:
    """js_click_option 的 JS：人类行为增强的"某题某个选项"点击。"""
    return f"""
        var input = document.querySelector('#q{q}_{choice}') ||
                    document.querySelector("input[name='q{q}'][value='{choice}']");
        if (!input) return false;

        // ---- 点击前：把选项滚动到可视位置 ----
        var target = input.closest('.field, .div_question, .q-item, li, label');
        if (!target) target = input.closest('div, span') || input;
        target.scrollIntoView({{behavior: 'instant', block: 'center'}});

        // ---- 点击前：模拟鼠标移动到元素上 ----
        try {{
            var r = target.getBoundingClientRect();
            var cx = Math.round(r.left + r.width / 2);
            var cy = Math.round(r.top + r.height / 2);
            ['mousemove','mouseover','mouseenter'].forEach(function(evtName){{
                var ev = new MouseEvent(evtName, {{
                    bubbles: true, cancelable: true, view: window,
                    clientX: cx, clientY: cy,
                    relatedTarget: document.body
                }});
                target.dispatchEvent(ev);
            }});
        }} catch(_) {{}}

        // ---- 点击：mousedown → change/input → checked=true → mouseup → click ----
        try {{
            var ev2 = new MouseEvent('mousedown', {{ bubbles:true, button:0, cancelable:true }});
            target.dispatchEvent(ev2);
        }} catch(_) {{}}

        input.checked = true;
        input.dispatchEvent(new Event('input', {{bubbles: true}}));
        input.dispatchEvent(new Event('change', {{bubbles: true}}));

        try {{ target.dispatchEvent(new MouseEvent('mouseup', {{bubbles:true, button:0}})); }} catch(_) {{}}
        try {{ target.dispatchEvent(new MouseEvent('click', {{bubbles:true, button:0}})); }} catch(_) {{}}

        // ---- 问卷星特有的样式：给装饰 <a> 加 jqchecked ----
        var wrapper = input.closest('.jqradiowrapper, .jqcheckboxwrapper, label, span');
        if (wrapper) {{
            var a = wrapper.querySelector('a.jqradio, a.jqcheck, a');
            if (a) {{
                a.classList.add('jqchecked');
            }}
        }}

        // ---- 单选：清掉同题其他选项 ----
        if (input.type === 'radio') {{
            document.querySelectorAll("input[name='q{q}']").forEach(function(sib) {{
                if (sib !== input) {{
                    sib.checked = false;
                    sib.dispatchEvent(new Event('change', {{bubbles: true}}));
                    var w = sib.closest('.jqradiowrapper, label, span');
                    if (w) {{
                        var aa = w.querySelector('a.jqradio, a.jqcheck, a');
                        if (aa) aa.classList.remove('jqchecked');
                    }}
                }}
            }});
        }}
        return true;
    """


def click_question_options_script(q: int, choices: list[int]) -> str:
    """js_click_question_options 的多选题批量点击 JS（清旧值 → 逐个选中 → 加样式）。"""
    choices_json = json.dumps(choices)
    return f"""
        var choices = {choices_json};
        var any_hit = false;

        // 先把该题所有原有选中态清空（防止页面已有默认值）
        document.querySelectorAll("input[name='q{q}']").forEach(function(sib) {{
            sib.checked = false;
            sib.dispatchEvent(new Event('change', {{bubbles: true}}));
            var w = sib.closest('.jqcheckboxwrapper, .jqradiowrapper, label, span');
            if (w) {{
                var aa = w.querySelector('a.jqcheck, a.jqradio, a');
                if (aa) aa.classList.remove('jqchecked');
            }}
        }});

        choices.forEach(function(ch) {{
            var input = document.querySelector('#q{q}_' + ch) ||
                        document.querySelector("input[name='q{q}'][value='" + ch + "']");
            if (!input) return;
            any_hit = true;

            var target = input.closest('.field, .div_question, .q-item, li, label');
            if (!target) target = input;
            target.scrollIntoView({{behavior: 'instant', block: 'center'}});

            input.checked = true;
            input.dispatchEvent(new Event('input', {{bubbles: true}}));
            input.dispatchEvent(new Event('change', {{bubbles: true}}));

            var wrapper = input.closest('.jqcheckboxwrapper, label, span');
            if (wrapper) {{
                var a = wrapper.querySelector('a.jqcheck, a.jqradio, a');
                if (a) a.classList.add('jqchecked');
            }}
        }});

        return any_hit;
    """


# ============================================================================
#  text 模块：填空题
# ============================================================================

def fill_text_script(q: int, text: str) -> str:
    """js_fill_text 的 JS：scroll → hover → focus → 赋值 → 假键盘 → input/change → blur。"""
    text_json = json.dumps(str(text))
    return f"""
        // 依次尝试多种 id/name 组合（问卷星 + 其他常见模板）
        var candidates = [
            document.getElementById('q{q}'),
            document.getElementById('answerq{q}'),
            document.querySelector("textarea[name='q{q}']"),
            document.querySelector("input[name='q{q}']"),
            document.querySelector("input[id*='q{q}'][type='text']"),
            document.querySelector("textarea[id*='q{q}']"),
        ];
        var el = null;
        for (var i = 0; i < candidates.length; i++) {{
            if (candidates[i]) {{ el = candidates[i]; break; }}
        }}
        if (!el) return false;

        // 1. 滚动到视野中央
        var wrap = el.closest('.field, .div_question, .q-item, li, label, div');
        if (!wrap) wrap = el;
        wrap.scrollIntoView({{behavior: 'instant', block: 'center'}});

        // 2. fake hover + focus（假装点进来）
        try {{
            var r = wrap.getBoundingClientRect();
            var cx = Math.round(r.left + r.width / 2);
            var cy = Math.round(r.top + r.height / 2);
            ['mousemove','mouseover','mouseenter'].forEach(function(n){{
                wrap.dispatchEvent(new MouseEvent(n, {{ bubbles:true, clientX:cx, clientY:cy }}));
            }});
        }} catch(_) {{}}

        try {{ el.dispatchEvent(new MouseEvent('mousedown', {{ bubbles:true, button:0 }})); }} catch(_) {{}}
        try {{ el.focus(); }} catch(_) {{}}
        try {{ el.dispatchEvent(new Event('focus', {{ bubbles:true }})); }} catch(_) {{}}

        // 3. 清原有值 → 赋值（如果是 contentEditable 走 innerText）
        var txt = {text_json};
        if (el.isContentEditable) {{
            el.innerText = txt;
        }} else {{
            el.value = txt;
        }}

        // 4. keydown 假逐字（问卷星可能监控键盘触发节奏）
        try {{
            for (var k = 0; k < Math.min(txt.length, 6); k++) {{
                var ev = new Event('keydown', {{ bubbles:true }});
                el.dispatchEvent(ev);
            }}
        }} catch(_) {{}}

        // 5. 核心触发：input + change（让前端框架感知数据变化）
        try {{ el.dispatchEvent(new Event('input',  {{ bubbles:true }})); }} catch(_) {{}}
        try {{ el.dispatchEvent(new Event('change', {{ bubbles:true }})); }} catch(_) {{}}

        // 6. blur 失焦（表示用户写完了）
        try {{ el.dispatchEvent(new Event('blur', {{ bubbles:true }})); }} catch(_) {{}}
        try {{ el.blur(); }} catch(_) {{}}

        return true;
    """


# ============================================================================
#  scale 模块：量表打分
# ============================================================================

def set_scale_script(q: int, value: int, scale_max: int | None) -> str:
    """js_set_scale 的 JS：优先 radio 命中；否则 area 第 value 个子项点击。"""
    v_int = int(value)
    sm = "" if scale_max is None else str(int(scale_max))
    return rf"""
        var q = {q};
        var val = {v_int};
        var scaleMax = {sm};

        // ---- 策略 A：找到带 name="qN" 的隐藏 radio（最常见） ----
        var radios = document.querySelectorAll('input[type="radio"][name="q' + q + '"]');
        for (var i = 0; i < radios.length; i++) {{
            var r = radios[i];
            var rv = parseInt(r.value);
            if (!isNaN(rv) && rv === val) {{
                var host = r.closest('.rate, .star, .level, .score, .rating, label, span, li');
                if (!host) host = r;
                host.scrollIntoView({{behavior:'instant', block:'center'}});
                try {{ r.dispatchEvent(new MouseEvent('mousedown',{{bubbles:true,button:0}})); }} catch(_) {{}}
                r.checked = true;
                try {{ r.dispatchEvent(new Event('input', {{bubbles:true}})); }} catch(_) {{}}
                try {{ r.dispatchEvent(new Event('change', {{bubbles:true}})); }} catch(_) {{}}
                try {{ r.dispatchEvent(new MouseEvent('mouseup',{{bubbles:true,button:0}})); }} catch(_) {{}}
                try {{ r.dispatchEvent(new MouseEvent('click',  {{bubbles:true,button:0}})); }} catch(_) {{}}
                // 问卷星 star 特有的 class 高亮
                var a = (host.querySelector ? host.querySelector('a, span.star') : null);
                if (a) a.classList.add('jqchecked', 'checked', 'on');
                return true;
            }}
        }}

        // ---- 策略 B：容器内第 value 个 clickable 子项 ----
        var sel = ['.rate','.star','.level','.score','.rating',
                   '#div' + q, '#q' + q + '_area'].join(',');
        var areas = document.querySelectorAll(sel);
        for (var j = 0; j < areas.length; j++) {{
            var area = areas[j];
            area.scrollIntoView({{behavior:'instant', block:'center'}});
            // 找所有 clickable 的子项
            var kids = area.querySelectorAll('li, a, span, i');
            var items = [];
            kids.forEach(function(k) {{
                var cls = (k.className || '').toString();
                if (/item|star|level|score|point|right|ok|full/.test(cls)) items.push(k);
                else if (/^\d+$/.test((k.textContent || '').trim())) items.push(k);
            }});
            if (!scaleMax) {{ scaleMax = items.length; }}
            var idx = val - 1;
            if (idx < 0) idx = 0;
            if (idx >= items.length) idx = items.length - 1;
            var tgt = items[idx];
            if (tgt) {{
                try {{ tgt.dispatchEvent(new MouseEvent('mousedown',{{bubbles:true,button:0}})); }} catch(_) {{}}
                try {{ tgt.dispatchEvent(new MouseEvent('click',  {{bubbles:true,button:0}})); }} catch(_) {{}}
                try {{ tgt.dispatchEvent(new Event('change', {{bubbles:true}})); }} catch(_) {{}}
                return true;
            }}
        }}
        return false;
    """


# ============================================================================
#  dropdown 模块：下拉选择
# ============================================================================

def select_dropdown_script(q: int, choice_value) -> str:
    """js_select_dropdown 的 JS：value(int/str) 精确匹配 → textContent 兜底匹配。"""
    val_json = json.dumps(str(choice_value))
    try:
        val_int = str(int(choice_value))
    except (TypeError, ValueError):
        val_int = None
    int_json = "null" if val_int is None else json.dumps(val_int)
    return f"""
        var q = {q};
        var targetStr = {val_json};
        var targetInt = {int_json};

        // 找 <select>
        var sel = null;
        var candidates = [
            document.querySelector("select[name='q" + q + "']"),
            document.getElementById('selectq' + q),
            document.getElementById('q' + q),
            document.querySelector("select[id*='q" + q + "']"),
        ];
        for (var i = 0; i < candidates.length; i++) {{
            if (candidates[i]) {{ sel = candidates[i]; break; }}
        }}
        if (!sel) return false;

        sel.scrollIntoView({{behavior:'instant', block:'center'}});
        var opts = sel.querySelectorAll('option');
        var hitIdx = -1;

        // 1) value 精确匹配（int 优先）
        if (targetInt !== null) {{
            for (var k = 0; k < opts.length; k++) {{
                if (String(opts[k].value) === targetInt) {{ hitIdx = k; break; }}
            }}
        }}
        // 2) value == targetStr
        if (hitIdx === -1) {{
            for (var k2 = 0; k2 < opts.length; k2++) {{
                if (String(opts[k2].value) === targetStr) {{ hitIdx = k2; break; }}
            }}
        }}
        // 3) textContent 匹配（用户只给选项文本时）
        if (hitIdx === -1) {{
            for (var k3 = 0; k3 < opts.length; k3++) {{
                var t = (opts[k3].textContent || '').trim();
                if (t && t === targetStr) {{ hitIdx = k3; break; }}
            }}
        }}

        if (hitIdx === -1) return false;
        sel.selectedIndex = hitIdx;

        // 触发 change
        try {{ sel.dispatchEvent(new Event('input',  {{ bubbles:true }})); }} catch(_) {{}}
        try {{ sel.dispatchEvent(new Event('change', {{ bubbles:true }})); }} catch(_) {{}}
        return true;
    """


# ============================================================================
#  matrix 模块：矩阵单选
# ============================================================================

def fill_matrix_single_script(q: int, row_selections: dict) -> str:
    """js_fill_matrix_single 的 JS：name="qN_rowIdx" 约定 → 整题区域行定位兜底。"""
    mapping_json = json.dumps(row_selections, ensure_ascii=False, default=str)
    return f"""
        var q = {q};
        var rowMap = {mapping_json};
        var anyHit = false;

        Object.keys(rowMap).forEach(function(rowKey) {{
            var col = rowMap[rowKey];
            // 先假设 name 约定是 "qN_rowIdx"
            var name = 'q' + q + '_' + rowKey;
            var radios = document.querySelectorAll('input[type="radio"][name="' + name + '"]');

            // 兜底：整题区域内按行定位
            if (radios.length === 0) {{
                var host = document.getElementById('div' + q) ||
                           document.getElementById('q' + q + '_table') ||
                           document.querySelector('.matrix, .mulitytitle, [data-q="' + q + '"]');
                if (host) {{
                    var rows = host.querySelectorAll('tr, .matrix-row, [class*="row"]');
                    var rowIdx = parseInt(rowKey);
                    if (!isNaN(rowIdx) && rows.length >= rowIdx) {{
                        var tgtRow = rows[rowIdx - 1];
                        if (tgtRow) radios = tgtRow.querySelectorAll('input[type="radio"]');
                    }}
                }}
            }}

            for (var i = 0; i < radios.length; i++) {{
                var r = radios[i];
                var rv = parseInt(r.value);
                var colStr = String(col);
                if (
                    (!isNaN(rv) && rv === col) ||
                    String(r.value) === colStr ||
                    (r.getAttribute('data-value') || '') === colStr
                ) {{
                    var wrap = r.closest('label, td, span, li, div') || r;
                    wrap.scrollIntoView({{behavior:'instant', block:'center'}});
                    try {{ r.dispatchEvent(new MouseEvent('mousedown',{{bubbles:true,button:0}})); }} catch(_) {{}}
                    r.checked = true;
                    try {{ r.dispatchEvent(new Event('input', {{bubbles:true}})); }} catch(_) {{}}
                    try {{ r.dispatchEvent(new Event('change', {{bubbles:true}})); }} catch(_) {{}}
                    try {{ r.dispatchEvent(new MouseEvent('click', {{bubbles:true,button:0}})); }} catch(_) {{}}
                    // 装饰样式
                    var a = wrap.querySelector ? wrap.querySelector('a, .jqradio, .radio-label') : null;
                    if (a) {{
                        a.classList.add('jqchecked', 'checked', 'active');
                        // 兄弟移除选中态
                        var sibs = wrap.parentNode ? wrap.parentNode.querySelectorAll('a,.jqradio,.radio-label') : [];
                        sibs.forEach(function(s){{ if(s !== a) s.classList.remove('jqchecked','checked','active'); }});
                    }}
                    anyHit = true;
                    break;
                }}
            }}
        }});

        return anyHit;
    """


# ============================================================================
#  submit 模块（较小 JS 片段，也集中便于维护）
# ============================================================================

SUBMIT_SELECTORS_ARRAY_LITERAL: str = """["#divSubmit","#submit_button","#ctlNext",
    "button[type='submit']","input[type='submit']",
    ".submitbtn","#submitBtn","#submitDiv",".btn-submit",
    ".submitbtn.clickable","#ctl00_ContentPlaceHolder1_ctlSubmit"]"""


def submit_button_fallback_script() -> str:
    """find_and_click_submit 的 JS 兜底：遍历提交选择器逐个点击。"""
    return f"""
        var sels = {SUBMIT_SELECTORS_ARRAY_LITERAL};
        for (var i=0; i<sels.length; i++) {{
            var el = document.querySelector(sels[i]);
            if (el) {{
                el.scrollIntoView({{behavior:'instant',block:'center'}});
                try {{ el.dispatchEvent(new MouseEvent('mousedown',{{bubbles:true,button:0}})); }} catch(_) {{}}
                el.click();
                return true;
            }}
        }}
        return false;
    """


def submit_success_detect_script() -> str:
    """_wait_until_submit_effect 的 success text/selector 命中检测。"""
    return """
        var txt = (document.body && document.body.innerText) || '';
        return txt.indexOf('提交成功') !== -1 ||
               txt.indexOf('感谢您的参与') !== -1 ||
               txt.indexOf('感谢您的认真填写') !== -1 ||
               txt.indexOf('已完成') !== -1 ||
               !!document.querySelector('.submit-succ, .success-tip, #success-tip, .success');
    """
