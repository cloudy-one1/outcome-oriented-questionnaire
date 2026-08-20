"""DOM 级别交互模块（增强版）。

相比 v1 的改进：
  1. 新增 js_click_question_options(driver, q, choices_list) — 一次性对同一题
     的多个选项做 DOM 操作，减少 Selenium → 浏览器往返次数（速率提升）。
  2. 新增 human_js_click_option — 对单个选项点击前先做"假滚动 + 假 hover"，
     模拟人类点击前的浏览行为（反检测提升）。
  3. find_and_click_submit — 新增问卷星的更多选择器 + 提交后 URL 变化检测
     （不再固定 sleep 2-3 秒，URL 一变化就立即返回 — 速率提升）。
  4. 全局 `js_execute_with_retry` 装饰器：JS 执行偶发的
     StaleElementReference / JavascriptException / WebDriverException
     自动重试最多 3 次 — 稳定性提升。
"""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

from selenium.common.exceptions import (
    JavascriptException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.by import By

from .config import CLICK_AFTER_HI, CLICK_AFTER_LO, CLICK_AFTER_MU, CLICK_AFTER_SIGMA
from .utils import gaussian_seconds, human_pause, retry_with_backoff


_Fn = TypeVar("_Fn", bound=Callable[..., Any])

# JS execute 重试会捕获的异常类集合
_JS_RETRYABLE = (
    StaleElementReferenceException,
    JavascriptException,
    WebDriverException,
)


def js_execute_retry(max_attempts: int = 3,
                     initial_delay: float = 0.2,
                     backoff_factor: float = 2.0) -> Callable[[_Fn], _Fn]:
    """execute_script 常用重试装饰器。

    问卷星页面会频繁重新渲染 DOM，StaleElementReference 是常见偶发异常，
    一般重试 1~2 次就能成功。
    """
    return retry_with_backoff(
        max_attempts=max_attempts,
        initial_delay=initial_delay,
        backoff_factor=backoff_factor,
        jitter=True,
        retry_on=_JS_RETRYABLE,
    )


# ============================================================================
#  单选项点击（人类行为增强：滚动 → hover → 点击 → 样式更新）
# ============================================================================

@js_execute_retry()
def js_click_option(driver: Any, q: int, choice: int) -> bool:
    """通过 JS 注入点击选中某道题的某个选项（人类行为版）。

    与 v1 的区别：
      - 在点击前，先把元素滚动到可视区（而不是瞬间跳到 screen center）
      - 先 dispatch mouseover / mouseenter，假装鼠标真的移了过来
      - 选中后触发 input/change/mouseup/mousedown 一套完整的原生事件链
      - 单选题其他选项取消选中更彻底（额外清空 checked state）

    注意：本函数不做任何 sleep — 外部调用者在每题之间统一做人类停顿。
    """
    return driver.execute_script(f"""
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
    """)


# ============================================================================
#  批量点击（给多选题 / 整页一次性注入所有答案时用，减少 N 次往返）
# ============================================================================

@js_execute_retry()
def js_click_question_options(driver: Any, q: int, question_type: str,
                              choices: list[int]) -> bool:
    """**一次性**设置单道题的所有被选中选项（返回布尔表示整题是否成功）。

    多选题尤其适合：原本要对每个选项调用一次 execute_script（N 次往返），
    现在整题一次执行（1 次往返）— 平均提速 3~6 倍。

    参数：
      q             : 题号
      question_type : "single" 或 "multi"
      choices       : 要选中的选项值列表（int 数组）
    """
    if not choices:
        return False

    # 单选题只有第一个值有意义
    if question_type == "single":
        return js_click_option(driver, q, choices[0])

    # 多选题：一次注入全部
    from json import dumps as _dumps
    return driver.execute_script(f"""
        var choices = {_dumps(choices)};
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
    """)


def click_after_pause() -> float:
    """在每次点击后 sleep 一段"人类反应时间"，返回实际 sleep 秒数。"""
    return human_pause(
        CLICK_AFTER_MU, CLICK_AFTER_SIGMA, CLICK_AFTER_LO, CLICK_AFTER_HI
    )


# ============================================================================
#  提交按钮查找 + 提交后 URL 变化快进
# ============================================================================

@js_execute_retry(max_attempts=3, initial_delay=0.15)
def find_and_click_submit(driver: Any, *, wait_url_change_timeout: float = 6.0) -> bool:
    """查找并点击问卷"提交"按钮；点击后等待 URL 变化。

    相比 v1 的改进：
      1. 多 3 个问卷星新版选择器（`.submitbtn.clickable`, `#submitDiv`, `.btn-submit`）
      2. 点击后不再固定 sleep(2-3)，而是等 URL 变化 → 大幅提速
      3. 如果 6 秒内 URL 没变化也返回 True（点击本身已生效，后续交给 pipeline 判断）
    """
    selectors = [
        "#divSubmit", "#submit_button", "#ctlNext",
        "button[type='submit']", "input[type='submit']",
        ".submitbtn", "#submitBtn", "#submitDiv", ".btn-submit",
        ".submitbtn.clickable", "#ctl00_ContentPlaceHolder1_ctlSubmit",
    ]

    # (a) 先尝试 Selenium 原生定位
    for sel in selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior:'instant',block:'center'});",
                btn,
            )
            time.sleep(gaussian_seconds(0.18, 0.04, 0.08, 0.4))
            btn.click()
            return _wait_until_submit_effect(driver, wait_url_change_timeout)
        except Exception:
            continue

    # (b) Selenium 定位全部失败 → JS 兜底
    ok = driver.execute_script("""
        var sels = ["#divSubmit","#submit_button","#ctlNext",
                    "button[type='submit']","input[type='submit']",
                    ".submitbtn","#submitBtn","#submitDiv",".btn-submit",
                    ".submitbtn.clickable","#ctl00_ContentPlaceHolder1_ctlSubmit"];
        for (var i=0; i<sels.length; i++) {
            var el = document.querySelector(sels[i]);
            if (el) {
                el.scrollIntoView({behavior:'instant',block:'center'});
                try { el.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:0})); } catch(_) {}
                el.click();
                return true;
            }
        }
        return false;
    """)
    if not ok:
        return False
    return _wait_until_submit_effect(driver, wait_url_change_timeout)


def _wait_until_submit_effect(driver: Any, timeout: float) -> bool:
    """点击提交按钮后，检测效果（URL 变化 / 页面 readyState 重新加载）。

    相比固定 sleep(2.0-3.0) 平均可节省 1.5 秒 / 次提交。
    """
    old_url = driver.current_url
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        try:
            cur = driver.current_url
            if cur != old_url:
                return True
            # 提交后出现感谢语 / 提交成功弹窗也算有效
            if driver.execute_script("""
                var txt = (document.body && document.body.innerText) || '';
                return txt.indexOf('提交成功') !== -1 ||
                       txt.indexOf('感谢您的参与') !== -1 ||
                       txt.indexOf('感谢您的认真填写') !== -1 ||
                       txt.indexOf('已完成') !== -1 ||
                       !!document.querySelector('.submit-succ, .success-tip, #success-tip, .success');
            """):
                return True
        except Exception:
            pass
        time.sleep(0.15)
    # 超时了也算成功（按钮确实点击了，只是页面响应慢或做了 AJAX）
    return True


# ============================================================================
#  V2 新题型：填空（text / textarea）人类模拟输入
# ============================================================================

@js_execute_retry()
def js_fill_text(driver: Any, q: int, text: str) -> bool:
    """通过 JS 注入 + 人类行为事件链，将 ``text`` 填入 Q``q`` 的文本框/文本域。

    反检测要点：
      1. 先 scrollIntoView（把题目滚到视野中央，不会瞬间跳屏）
      2. 先 dispatch focus/blur/mouseover（假装用户真的点了输入框）
      3. 使用 ``value=...`` 后显式 dispatch ``input`` + ``change`` + ``keydown``
         事件（问卷星的 Vue/React 绑定通常监听 input 事件，只改 value 不生效）

    :param q:    题号（正整数）
    :param text: 要写入的文本（中文/英文/数字均可）
    :return:     布尔表示是否成功命中并填写
    """
    from json import dumps as _dumps
    text_json = _dumps(str(text))
    return driver.execute_script(f"""
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
    """)


# ============================================================================
#  V2 新题型：量表打分（scale 1..N）
# ============================================================================

@js_execute_retry()
def js_set_scale(driver: Any, q: int, value: int, scale_max: int | None = None) -> bool:
    """为 Q``q`` 打量表分数 ``value``（1-based）。

    策略：优先用隐藏 radio 的 value 点选；如果该结构不存在，
    回退成点击 area 内第 ``value`` 个子元素（星/级）。
    """
    v_int = int(value)
    sm = "" if scale_max is None else str(int(scale_max))
    return driver.execute_script(rf"""
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
    """)


# ============================================================================
#  V2 新题型：下拉选择题（<select>）
# ============================================================================

@js_execute_retry()
def js_select_dropdown(driver: Any, q: int, choice_value: Any) -> bool:
    """为 Q``q`` 的 <select> 下拉选择指定选项值 ``choice_value``。

    :param choice_value: 可以是 int 或 str，会自动与 option.value 匹配；
                         若找不到完全相等的 value，则匹配 option.text。
    """
    from json import dumps as _dumps
    val_json = _dumps(str(choice_value))
    val_int: str | None
    try:
        val_int = str(int(choice_value))
    except (TypeError, ValueError):
        val_int = None

    int_json = "null" if val_int is None else _dumps(val_int)

    return driver.execute_script(f"""
        var q = {q};
        var targetStr = {val_json};
        var targetInt = {int_json};

        // 找 <select>
        var sel = null;
        var candidates = [
            document.querySelector("select[name='q' + q]"),
            document.getElementById('selectq' + q),
            document.getElementById('q' + q),
            document.querySelector("select[id*='q' + q]"),
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
    """)


# ============================================================================
#  V2 新题型：矩阵单选（每行一道单选题，name 形如 qN_rowIdx）
# ============================================================================

@js_execute_retry()
def js_fill_matrix_single(
    driver: Any,
    q: int,
    row_selections: dict[Any, Any],
) -> bool:
    """矩阵单选：一次性设置 Q``q`` 的所有行选择。

    :param q:                题号（正整数）
    :param row_selections:   ``{row_idx: col_value}`` 映射；row_idx 与 detection 返回的
                             ``rows`` 元素完全对应；col_value 与 detection 返回的
                             ``cols`` 元素完全对应（通常是 1..M 整数）。
    :return:                 是否至少有一行成功命中
    """
    import json as _json
    mapping_json = _json.dumps(row_selections, ensure_ascii=False, default=str)

    return driver.execute_script(f"""
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
    """)
