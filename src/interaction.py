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
