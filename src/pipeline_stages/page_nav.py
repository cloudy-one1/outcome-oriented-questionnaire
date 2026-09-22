"""分页问卷的翻页阶段（v3.0）。

问卷星的分页把每一页渲染成同一棵 DOM 树里的 ``div.page``，只有当前页可见，
翻页由页尾的"下一页"按钮驱动 —— 它和提交按钮是两个控件，但 **id 会撞**：
``interactions._scripts.SUBMIT_SELECTORS`` 里排着 ``#ctlNext``，那既是某些模板的
"下一页"，也是某些模板的提交键。所以翻页必须在提交之前跑完：先按分页控件语义
找"下一页"，只有确认没有下一页了，才把页面交给 ``find_and_click_submit``。

三种出口：
    ``no_more``  页面上没有可用的下一页 → 已经在最后一页（单页问卷永远走这里）
    ``advanced`` 点完按钮后，可见题号集合变了
    ``failed``   点了按钮但页面没变（JS 卡住 / 平台又弹了验证 / 结构不认识）

``failed`` 的处理由调用方决定，而且**必须**是"整份判失败、不点提交"：
只交了第一页的问卷在服务端仍可能算一份有效回收，那是最坏的一种静默失败。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..config import PAGE_NAV_TIMEOUT
from ..detection import detect_questions
from ..exceptions import raise_non_recoverable
from ..platforms import WJX

logger = logging.getLogger(__name__)

# 刻意**不含** ``#ctlNext`` 与 ``.next``：前者在 SUBMIT_SELECTORS 里是提交键，
# 后者会命中无数装饰元素。误点提交键等于"只交了第一页"，比翻不了页糟得多。
NEXT_PAGE_SELECTORS: list[str] = list(WJX.next_page_selectors)

# 分页结构探测：至少要看到 2 个分页容器才承认"这是分页问卷"
_PAGE_WRAPPER_SEL = WJX.page_wrapper_selector
_DETECT_PAGES_JS = f"""
var nodes = document.querySelectorAll('{_PAGE_WRAPPER_SEL}');
var pages = 0, shown = 0;
for (var i = 0; i < nodes.length; i++) {{
    var n = nodes[i];
    // 只把"确实装着题目控件"的容器当一页，避免把分页导航条本身数成一页
    if (!n.querySelector || !n.querySelector('input, select, textarea')) continue;
    pages += 1;
    var hidden = n.hidden || (n.style && n.style.display === 'none');
    if (!hidden && window.getComputedStyle
            && window.getComputedStyle(n).display === 'none') hidden = true;
    if (!hidden) shown += 1;
}}
return JSON.stringify({{pages: pages, shown: shown}});
"""


def page_counts(driver: Any) -> tuple[int, int]:
    """``(分页容器数, 其中可见数)``。单页问卷是 ``(0, 0)`` 或 ``(1, 1)``。

    探测本身出问题（脚本被页面改写、会话已失效）时一律按"不是分页问卷"处理 ——
    这样最坏结果只是"不翻页"，而反过来（猜成多页）会在单页问卷上白点一次下一页键。
    """
    try:
        raw = driver.execute_script(_DETECT_PAGES_JS)
    except Exception as _e:  # noqa: BLE001 - 旁路探测，任何失败都只降级为"单页"
        raise_non_recoverable(_e)
        logger.debug("分页探测失败，按单页问卷处理: %s", _e)
        return 0, 0
    if not isinstance(raw, str):
        return 0, 0
    try:
        data = json.loads(raw)
        return int(data.get("pages", 0)), int(data.get("shown", 0))
    except (ValueError, TypeError, AttributeError):
        return 0, 0

_CLICK_NEXT_JS_TEMPLATE = """
var sels = %s;
function visible(el) {
    if (!el || el.disabled) return false;
    var st = el.style || {};
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    if (window.getComputedStyle) {
        var cs = window.getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        if (cs.opacity === '0') return false;
    }
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
}
for (var i = 0; i < sels.length; i++) {
    var hits = [];
    try { hits = document.querySelectorAll(sels[i]); } catch (_) { continue; }
    for (var j = 0; j < hits.length; j++) {
        if (!visible(hits[j])) continue;
        try { hits[j].scrollIntoView({behavior: 'instant', block: 'center'}); } catch (_) {}
        hits[j].click();
        return 'clicked:' + sels[i];
    }
}
// 兜底：模板千变万化，认文案比再堆选择器稳
var all = document.querySelectorAll('a, button, input[type="button"], input[type="submit"], span, div');
for (var k = 0; k < all.length; k++) {
    var el = all[k];
    // 只点叶子节点：容器 div/span 的 textContent 也会等于"下一页"（文字在它孩子里），
    // 点那种包裹层等于点到一个没有任何翻页语义的 div —— 页面不动，8s 后判失败。
    if (el.children && el.children.length > 0) continue;
    var txt = ((el.value || el.textContent || '') + '').replace(/\\s+/g, '').trim();
    if (txt !== '下一页' && txt !== '下页' && txt !== 'Next') continue;
    if (!visible(el)) continue;
    el.click();
    return 'clicked:text';
}
return 'no_button';
"""


def _visible_question_numbers(driver: Any) -> set[int]:
    """当前可见页的题号集合（``detect_questions`` 已过滤掉隐藏分页的控件）。"""
    return {int(q["q"]) for q in detect_questions(driver) if isinstance(q.get("q"), int)}


def advance_to_next_page(
    driver: Any,
    current_qnums: set[int],
    *,
    timeout: float = PAGE_NAV_TIMEOUT,
    _sleep: Any = time.sleep,
) -> tuple[str, str]:
    """尝试翻到下一页。

    :param current_qnums: 翻页**前**本页的题号集合，用来判断页面是否真的换了
    :return: ``(verdict, detail)`` —— verdict 见模块文档；detail 给人看
    """
    # 先确认"这真是一份分页问卷"：单页问卷没有下一页按钮，但它的提交键可能长得像，
    # 误点就等于只交第一页 —— 所以没有 ≥2 个分页容器时直接判定"没有下一页"。
    pages, _shown = page_counts(driver)
    if pages < 2:
        return "no_more", f"分页容器 {pages} 个，按单页问卷处理"

    js = _CLICK_NEXT_JS_TEMPLATE % json.dumps(NEXT_PAGE_SELECTORS)
    raw = driver.execute_script(js)
    if isinstance(raw, str) and raw.startswith("no_button"):
        return "no_more", "未发现可用的下一页按钮"
    if not isinstance(raw, str) or not raw.startswith("clicked:"):
        # 注入返回了意外形态（被页面脚本改写 / 返回 None）：不猜，交回上层判失败
        return "failed", f"翻页脚本返回意外值: {raw!r}"

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        _sleep(0.3)
        nums = _visible_question_numbers(driver)
        if nums and nums != current_qnums:
            return "advanced", f"下一页题号 {sorted(nums)}"
    return "failed", f"点击下一页后 {timeout}s 内可见题号没变化"


__all__ = ["NEXT_PAGE_SELECTORS", "advance_to_next_page", "page_counts"]
