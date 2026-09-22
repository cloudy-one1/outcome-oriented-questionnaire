"""问卷结构自动探测模块（v2.0 增强版）。

扫描问卷星页面，通过 JS 注入直接操作 DOM，自动提取所有题目的结构信息
（题号、类型、选项值列表 / 文本字段 / 量表级数 / 矩阵行列）。

相比 v1 的改进：
    1. 新增 4 类题型识别：text（填空）、scale（量表）、dropdown（下拉）、matrix_single（矩阵单选）
    2. 所有题型一次性通过单次 execute_script 完成 → 零额外 Selenium 往返
    3. 与 v1 100% 兼容：single/multi 结构完全不变（保持 choices: list[int]），旧调用方无需改动

JS 识别逻辑简述 ::

    ┌────────────────────┬─────────────────────────────────────────────────┐
    │ 题型               │ DOM 特征（问卷星通用结构）                       │
    ├────────────────────┼─────────────────────────────────────────────────┤
    │ single (radio)     │ input[type=radio][name~=qN]                     │
    │ multi (checkbox)   │ input[type=checkbox][name~=qN]                  │
    │ dropdown           │ select[name~=qN] 或 select#selectqN              │
    │ scale              │ div[class~=rate] 或 ul[class~=star] + 子项计数   │
    │ text / textarea    │ input[type=text]#qN 或 textarea#qN              │
    │ matrix_single      │ table/div 矩阵容器：每行内有一组同 name 的 radio │
    └────────────────────┴─────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from typing import Any

from .interactions._scripts import option_blank_helper_script
from .platforms import SurveyPlatform


# 两处 execute_script 脚本体共用的 JS 函数声明（见 option_blank_helper_script）：
# 探测时用它认"这一项自带填空框"，作答时用它定位要写入的框，已答扫描时用它判断
# "框填上了没有"。三段判定必须同源，否则会出现"探测说有框、作答找不到框"这种
# 报成功但页面仍是空的分裂。
_OPTION_BLANK_JS: str = option_blank_helper_script()


# ``detect_questions`` 与 ``detect_platform_questions`` **共用**的分页过滤：
# 问卷星把每一页渲染成同一棵 DOM 树里的 div.page，只有当前页可见。
# 不做这层过滤，探测会把后面几页的题也算成"本页题"—— 于是在第 1 页就去点
# 第 3 页的选项，平台只收当前页的输入，结果是"整卷答完"仍被判未答。
# 刻意只认分页容器这一层：量表那些 display:none 的隐藏 radio 不在其列，
# 那是它们的正常形态（v2.6 靠那些 radio 读量表边界）。
#
# 为什么两处必须同源：对拍是拿"本页探测结果"比"本页平台题号"。两边分页口径
# 不一致时，每份分页问卷都会刷一堆"平台有 Q9 我们没探测到"的假警 —— 而假警的
# 代价是用户开始忽略所有提示，真错位也就跟着没人看了。
_PAGE_HIDDEN_JS: str = r"""
function pageHidden(el) {
    var n = el;
    while (n && n !== document.body) {
        var cl = (n.className || '').toString();
        var isPage = /(^|\s)(page|paging|pDiv|question-page|ui-page)(\s|$)/.test(cl)
                  || (n.dataset && n.dataset.page !== undefined);
        if (isPage) {
            if (n.hidden) return true;
            if (n.style && n.style.display === 'none') return true;
            if (window.getComputedStyle
                    && window.getComputedStyle(n).display === 'none') return true;
        }
        n = n.parentElement;
    }
    return false;
}
"""


# 平台自报结构（题号 + 题型码 + 必填标记）的读取脚本。参数依次是候选选择器、
# 题号属性名、题型码属性名、是否只看当前可见页。
# 选择器按候选顺序试到**第一个非空**为止就停：`#fieldset1 > div[topic]` 是问卷星的
# 标准投放，`div[topic][type]` 兜那些换了外层容器的模板。
#
# 两种"看不见"要分清，它们的意思正好相反：
#   * ``skip``  —— 被**跳题 / 互斥**逻辑藏起来的题（不在分页容器里却整块 display:none）。
#     这种题平台本来就不要求答，算进必答题会把一份能交的问卷白拦下来。
#   * 分页隐藏 —— 别的页的题（``pageHidden``）。对拍时不算（两边都只看本页），
#     提交前完整度自检时要算（那时整卷都走完了）。
_PLATFORM_QUESTIONS_JS: str = r"""
var selectors = arguments[0] || [];
var numAttr = arguments[1];
var typeAttr = arguments[2];
var visibleOnly = arguments[3];
var nodes = [];
for (var i = 0; i < selectors.length && nodes.length === 0; i++) {
    var found = document.querySelectorAll(selectors[i]);
    if (found.length) nodes = Array.prototype.slice.call(found);
}
function displayHidden(n) {
    if (n.hidden) return true;
    if (n.style && n.style.display === 'none') return true;
    return !!(window.getComputedStyle && window.getComputedStyle(n).display === 'none');
}
var out = [];
nodes.forEach(function(n) {
    var num = parseInt(n.getAttribute(numAttr), 10);
    var code = n.getAttribute(typeAttr);
    if (isNaN(num) || code === null || String(code).trim() === '') return;
    var paged = pageHidden(n);
    if (displayHidden(n) && !paged) return;          // 跳题藏起来的题：不参与任何判定
    if (visibleOnly && paged) return;                // 对拍：只要本页的题
    var reqAttr = (n.getAttribute('req') || '').trim();
    out.push({
        q: num,
        code: String(code).trim(),
        // 空值、"0"、没有这个属性都算不必答：缺信号一律朝"不拦"的方向降级
        required: reqAttr !== '' && reqAttr !== '0'
    });
});
return JSON.stringify(out);
"""


def detect_questions(driver: Any) -> list[dict]:
    """扫描当前页面，自动提取所有题目的结构信息（v2.0 全题型版）。

    实现方式：
      单次 execute_script 注入 → 串行识别 6 类题型 → 按题号合并去重
      → 升序排序后返回。相比 Selenium 原生查找元素速度快一个数量级，
      且不会被问卷星的动态渲染干扰。

    参数：
      driver : Selenium WebDriver 实例

    返回：
      list[dict]，每个元素至少包含 ``q`` 和 ``type``；识别到题面元素时附带
      ``title``（题干文本，去掉多余空白、最长 120 字，v3.0 权重锚定用）。
      各题型的额外字段如下：

      **V1 兼容（单选 / 多选）** ::

          {"q": 1, "type": "single", "choices": [1, 2, 3, 4]}
          {"q": 2, "type": "multi",  "choices": [1, 2, 3]}

      单选/多选的某一项自带填空框（"其他____"）时多一个键，值是**那些选项的
      option value**（与 ``choices`` 同域，不是下标）::

          {"q": 2, "type": "multi", "choices": [1, 2, 3], "blank_options": [3]}

      **V2 新增题型** ::

          # 下拉（等价单选，choices 是选项文本或数值）
          {"q": 3, "type": "dropdown", "choices": ["A", "B", "C", "D"]}

          # 量表打分：scale=N 表示 1..N 分
          {"q": 4, "type": "scale", "scale": 5}

          # 填空题：field 表示字段类型（name/phone/email/address/None），供 answering_v2 匹配
          {"q": 5, "type": "text",  "field": "name"}

          # 矩阵单选：rows 为行标签数组 / 索引，cols 为列标签数组 / 索引
          {"q": 6, "type": "matrix_single",
           "rows": [1, 2, 3], "cols": [1, 2, 3, 4, 5]}
    """
    raw = driver.execute_script(_OPTION_BLANK_JS + _PAGE_HIDDEN_JS + r"""
return (function() {
    var result = [];
    var map = {};   // qnum(int) -> question dict

    // ---------- 工具：向 map 合并 / 获取一题槽位 ----------
    function slot(q) {
        var qi = parseInt(q);
        if (!map[qi]) map[qi] = { q: qi };
        return map[qi];
    }

    // ---------- 工具：跳过"被分页容器隐藏"的控件（v3.0 多分页问卷） ----------
    // pageHidden 本体在 _PAGE_HIDDEN_JS（与平台侧对拍共用，见那边注释）。

    // ---------- 1. 单选 / 多选（v1 逻辑，保持完全一致） ----------
    (function() {
        var inputs = document.querySelectorAll(
            'input[type="radio"], input[type="checkbox"]'
        );
        inputs.forEach(function(el) {
            if (pageHidden(el)) return;
            var m = (el.name || '').match(/q(\d+)/);
            if (!m) m = (el.id || '').match(/q(\d+)/);
            if (!m) return;
            var q = parseInt(m[1]);
            var s = slot(q);
            if (!s.choices) s.choices = [];
            s.type = (el.type === 'radio') ? 'single' : 'multi';

            var v = parseInt(el.value);
            if (!isNaN(v) && s.choices.indexOf(v) === -1) {
                s.choices.push(v);
                // 这一项自带填空框（"其他____"）。勾了它却不写文本，平台会按
                // "该项内容未填写"拦下整题 —— 在本工具侧的样子是提交返回
                // unknown，完全看不出是这一格空着。
                if (optionBlankInput(el)) {
                    if (!s.blank_options) s.blank_options = [];
                    s.blank_options.push(v);
                }
            }
        });
    })();

    // ---------- 2. 下拉（<select name=qN>） ----------
    (function() {
        var selects = document.querySelectorAll('select');
        selects.forEach(function(sel) {
            if (pageHidden(sel)) return;
            var nm = sel.name || '';
            var id = sel.id || '';
            var m = nm.match(/q(\d+)/) || id.match(/q(\d+)/) || id.match(/selectq(\d+)/);
            if (!m) return;
            var s = slot(m[1]);
            s.type = 'dropdown';

            var choices = [];
            var opts = sel.querySelectorAll('option');
            opts.forEach(function(opt) {
                var val = opt.value;
                if (val === '' || val === null || val === undefined) return;  // 跳过"请选择"空项
                var num = parseInt(val);
                choices.push(isNaN(num) ? val : num);
            });
            if (choices.length > 0) s.choices = choices;
        });
    })();

    // ---------- 3. 量表 / 评分（问卷星常用 .rate-star / .level / .score-area） ----------
    (function() {
        // 特征：带 rate / star / level / score 的容器，内部子项数 = 分值级数
        var sel = [
            '[class*="rate"]', '[class*="star"]', '[class*="level"]',
            '[class*="score"]', '[class*="rating"]',
            'ul.rate', 'div.rate', 'ul.stars',
        ].join(',');
        var areas = document.querySelectorAll(sel);
        var seen = new Set();
        areas.forEach(function(area) {
            if (pageHidden(area)) return;
            // 找最近的题目容器，取其题号
            var host = area.closest ? area.closest(
                '.field,.div_question,.q-item,li,.question,div[id^="div"]'
            ) : null;
            var qid = null;
            if (host) {
                var idm = (host.id || '').match(/div(\d+)/) || (host.id || '').match(/q(\d+)/);
                if (idm) qid = parseInt(idm[1]);
            }
            // 兜底：area 自身 id / data-q
            if (!qid) {
                var dm = (area.id || '').match(/q(\d+)/) ||
                         (area.dataset && area.dataset.q && parseInt(area.dataset.q));
                if (dm) qid = (typeof dm === 'number') ? dm : parseInt(dm[1]);
            }
            if (!qid || seen.has(qid)) return;
            seen.add(qid);

            // ---- 量表边界：优先读隐藏 radio 的真实 value 区间 ----
            // 此前这里写死 s.scale_min = 1 且 s.scale = 选项个数，
            // 于是 2~10 分的量表被识别成 scale=9 / scale_min=1：
            //   作答时永远点不到 10，还会去点根本不存在的 1。
            // radio 的 value 才是权威边界；只有拿不到 radio 时才退回"数格子"。
            var hiddenRadios = document.querySelectorAll(
                'input[type="radio"][name="q' + qid + '"]'
            );
            var scaleVals = [];
            hiddenRadios.forEach(function(r) {
                var v = parseInt(r.value);
                if (!isNaN(v)) scaleVals.push(v);
            });

            var levelMin, levelMax;
            if (scaleVals.length >= 2) {
                levelMin = Math.min.apply(null, scaleVals);
                levelMax = Math.max.apply(null, scaleVals);
            } else {
                // 数内部"可选项"数量（li / a / span 带分数语义）
                var kids = area.querySelectorAll('li, a, span, i');
                var levelCount = 0;
                kids.forEach(function(k) {
                    var cls = (k.className || '').toString();
                    if (/item|star|level|score|point|right|ok|full/.test(cls) ||
                        /^\d+$/.test((k.textContent || '').trim())) {
                        levelCount++;
                    }
                });
                // 常见 5 / 10 级兜底
                if (levelCount < 2 || levelCount > 12) {
                    levelCount = hiddenRadios.length;
                }
                levelMin = 1;
                levelMax = levelCount;
            }

            var levels = levelMax - levelMin + 1;
            if (levels >= 2 && levels <= 12) {
                var s = slot(qid);
                s.type = 'scale';
                s.scale = levelMax;
                s.scale_min = levelMin;
            }
        });
    })();

    // ---------- 4. 填空（text / textarea / input[type=text]#qN） ----------
    (function() {
        var fillables = document.querySelectorAll(
            'input[type="text"], input[type="tel"], input[type="number"], input:not([type]), textarea'
        );
        fillables.forEach(function(el) {
            if (el.disabled || el.readOnly) return;
            if (pageHidden(el)) return;
            var id = el.id || '';
            var name = el.name || '';
            var m = id.match(/^q(\d+)$/) || id.match(/^answerq(\d+)$/) ||
                    name.match(/^q(\d+)$/) || id.match(/q(\d+)/);
            if (!m) {
                // 再找父级
                var parent = el.closest ? el.closest('[id*="div"]') : null;
                if (parent) {
                    var pm = (parent.id || '').match(/div(\d+)/);
                    if (pm) m = pm;
                }
            }
            if (!m) return;
            var qi = parseInt(m[1]);
            // 如果该题已被判为 single/multi（即已有 radio/checkbox 同名），不覆盖
            var existing = map[qi];
            if (existing && existing.type !== undefined && existing.type !== 'text') return;

            var s = slot(qi);
            s.type = 'text';

            // 猜字段类型：从 placeholder / 标签文字 / 前导 label
            var txt = (el.placeholder || '') + '|' +
                      (el.getAttribute('aria-label') || '') + '|';
            // 往上找题目文字
            var wrap = el.closest ? el.closest('.field,.div_question,.q-item,li,.question') : null;
            if (wrap) txt += (wrap.textContent || '').substring(0, 80);
            txt = txt.toLowerCase();

            var field = null;
            if (/姓名|名字|name|您的称呼|称呼/.test(txt)) field = 'name';
            else if (/手机|电话|mobile|phone|tel|联系方式/.test(txt)) field = 'phone';
            else if (/邮箱|e-mail|email|mail/.test(txt)) field = 'email';
            else if (/地址|住址|地址|addr|address|所在地区/.test(txt)) field = 'address';
            else if (/年龄|岁数|age/.test(txt)) field = 'age';
            else if (/公司|单位|学校|工作|org|company/.test(txt)) field = 'company';
            s.field = field;
        });
    })();

    // ---------- 5. 矩阵（每行一组控件，同题不同行 name 是 qN_1 / qN_2 ...） ----------
    (function() {
        // radio → matrix_single，checkbox → matrix_multi。v3.0 之前这里只看 radio，
        // 于是矩阵多选题被整题识别成"没有题目"或降级成单选，答出来的形状还不对。
        var allBoxes = document.querySelectorAll(
            'input[type="radio"], input[type="checkbox"]'
        );
        var matrixMap = {};  // qN -> { rows: Set(R), cols: Set(values), kind: 'radio'|'checkbox' }
        allBoxes.forEach(function(r) {
            if (pageHidden(r)) return;
            var m = (r.name || '').match(/^q(\d+)_(\d+)$/);
            if (!m) return;
            var qN = parseInt(m[1]);
            var rowN = parseInt(m[2]);
            if (!matrixMap[qN]) {
                matrixMap[qN] = { rows: new Set(), cols: new Set(), kinds: new Set() };
            }
            matrixMap[qN].rows.add(rowN);
            matrixMap[qN].kinds.add(r.type);
            var v = parseInt(r.value);
            if (!isNaN(v)) matrixMap[qN].cols.add(v);
        });

        Object.keys(matrixMap).forEach(function(qN) {
            var qi = parseInt(qN);
            var s = slot(qi);
            var entry = matrixMap[qN];
            // 如果该题已被识别为 single/multi，且 matrix 的 row 只有 1 条 → 保留 single
            if (s.type === 'single' || s.type === 'multi') {
                if (entry.rows.size <= 1) return;
            }
            var rowsArr = Array.from(entry.rows).sort(function(a, b) { return a - b; });
            var colsArr = Array.from(entry.cols).sort(function(a, b) { return a - b; });
            if (rowsArr.length < 2 || colsArr.length < 2) return;
            // 混合类型（既有 radio 又有 checkbox）按 radio 处理：行内单选是更强的约束
            var isMulti = entry.kinds.has('checkbox') && !entry.kinds.has('radio');
            s.type = isMulti ? 'matrix_multi' : 'matrix_single';
            s.rows = rowsArr;
            s.cols = colsArr;
        });
    })();

    // ---------- 5b. 排序题（v3.0：ul/ol 带 sort 类名，li 逐个可拖） ----------
    // 结构前提：容器 class 含 "sort"（问卷星实际是 ul.lisort），且里面 ≥2 个 li；
    // 题号按 隐藏 input 的 name → 容器自身 id → 分页/题目容器 id 的顺序找。
    // 三条都找不到就不认这题 —— 认错了比认不到更糟（会去重排别人的列表）。
    (function() {
        var lists = document.querySelectorAll('ul, ol');
        lists.forEach(function(ul) {
            if (pageHidden(ul)) return;
            var cls = (ul.className || '').toString();
            if (!/sort/i.test(cls)) return;
            var lis = [];
            for (var i = 0; i < ul.children.length; i++) {
                if (ul.children[i].tagName === 'LI') lis.push(ul.children[i]);
            }
            if (lis.length < 2) return;

            var items = lis.map(function(li, idx) {
                var v = li.getAttribute('value')
                     || li.getAttribute('data-value')
                     || li.getAttribute('data-id');
                return v !== null && v !== '' ? v : String(idx + 1);
            });

            var q = null;
            // ① 容器自身 id：问卷星常用 q13_list / sortq13 这类带题号前缀的 id
            var um = (ul.id || '').match(/^q(\d+)(?:$|[^0-9])/);
            if (um) q = parseInt(um[1]);
            // ② 同域的隐藏 input（提交值就装在这里）—— 刻意不用 closest('[id]')，
            //    那会先匹配到 ul 自己，把 scope 缩成一个查不到任何东西的节点
            var scope = (ul.closest && ul.closest('.field, .div_question, .question'))
                     || ul.parentNode;
            var hidden = scope ? scope.querySelectorAll('input[type="hidden"]') : [];
            for (var h = 0; h < hidden.length && q === null; h++) {
                var hm = (hidden[h].name || hidden[h].id || '').match(/^q(\d+)$/);
                if (hm) q = parseInt(hm[1]);
            }
            // ③ 题目容器 id（div13 / divquestion13）
            if (q === null && scope && scope.id) {
                var cm = scope.id.match(/(?:div|q)(\d+)$/);
                if (cm) q = parseInt(cm[1]);
            }
            if (q === null) return;

            var s = slot(q);
            if (s.type && s.type !== 'sort') return;   // 已被别的题型占用 → 不覆盖
            s.type = 'sort';
            s.items = items;
        });
    })();

    // ---------- 6. 题干文本（v3.0 权重锚定用） ----------
    // 只取题面元素自己的文字，绝不退回到"整个容器 textContent" ——
    // 那样会把选项文案一起吸进来，作者改一个选项就换一道题，锚点比题号更脆。
    (function() {
        var TITLE_SEL = '.topichtml, h2.t, .field-label, .quetitle,'
                      + ' .question-title, .field-label-text, .qtitle,'
                      + ' [class*="topichtml"]';
        Object.keys(map).forEach(function(q) {
            var s = map[q];
            if (s.title) return;
            var host = document.getElementById('divquestion' + q)
                    || document.getElementById('div_question_' + q);
            if (!host) {
                // 矩阵题的控件 name 是 qN_R（N 题号、R 行号），只查 [name="qN"] 会漏整道题
                host = document.querySelector(
                    '[id="q' + q + '"], [name="q' + q + '"],'
                    + ' [id^="q' + q + '_"], [name^="q' + q + '_"]'
                );
            }
            if (!host) return;
            // 从命中的控件**往上爬**，逐层找题面：closest('.field') 只会停在
            // 控件包装层，而题面是它的兄弟节点（真页面与 mock 都是这个结构）。
            var text = '';
            for (var node = host; node && node !== document.body; node = node.parentElement) {
                if (!node.querySelector) break;
                var t = node.querySelector(TITLE_SEL);
                if (t) { text = t.textContent || ''; break; }
            }
            text = text.replace(/\s+/g, ' ').trim();
            if (!text) return;
            s.title = text.substring(0, 120);
        });
    })();

    // ---------- 后处理：single/multi 的 choices 排序；清理 scale/text/matrix 的顶层脏字段；按题号升序输出 ----------
    Object.keys(map).forEach(function(q) {
        var it = map[q];
        if (it.type === 'scale' || it.type === 'text'
                || it.type === 'matrix_single' || it.type === 'matrix_multi') {
            delete it.choices;
            // 矩阵/量表的控件 name 也带 qN，第 1 步会把它们先记成选择题；
            // 题型改判后这些"哪一项要填空"的结论不再适用于本体的定位方式。
            delete it.blank_options;
        }
        if (it.choices && typeof it.choices.sort === 'function') {
            it.choices.sort(function(a, b) {
                if (typeof a === 'number' && typeof b === 'number') return a - b;
                return String(a).localeCompare(String(b));
            });
        }
    });
    Object.keys(map)
        .map(function(k) { return parseInt(k); })
        .sort(function(a, b) { return a - b; })
        .forEach(function(q) {
            result.push(map[q]);
        });

    return JSON.stringify(result);
})();
    """)
    return json.loads(raw)


def detect_platform_questions(
    driver: Any,
    platform: SurveyPlatform,
    *,
    visible_only: bool = True,
) -> list[dict]:
    """读平台自己标在题目容器上的结构：``[{"q": 7, "code": "6", "required": True}, ...]``。

    与 :func:`detect_questions` 的区别在于**信息来源**：那边是我们从控件形状反推
    出来的结构，这边是平台明写的结构。两者对不上就说明其中一边错了，而"我们探测
    出来的题型"是没法自己发现自己是错的 —— ``question_signature`` 算签名时用的
    就是这个探测结果，探测判错时签名跟着错，锚点比对也就一路放行。

    这是**只读旁路**，不参与作答，也不改变任何作答行为：拿不到信号（模板不带这些
    属性、候选选择器全部落空、脚本被页面改写）就返回空列表，调用方据此**静默跳过
    对拍**。平台没自报结构不等于我们探测错了，把它报成警报只会训练用户忽略提示。

    分页问卷只返回**当前可见页**的题 —— 与 ``detect_questions`` 共用
    ``_PAGE_HIDDEN_JS``，两边口径必须一致，理由见那段的注释。
    ``visible_only=False`` 时返回**整卷**的题（含其它页上那些），给提交前的完整度自检用；
    两种情况下被跳题 / 互斥逻辑藏起来的题都不返回 —— 那种题平台本来就不要求答。

    每项的 ``required`` 取自容器上的 ``req`` 属性（真卷实测 ``req="1"``）。
    **没有这个属性、或值是 ``0``，一律算不必答**：这条判据只在"确定会被平台拦下"时
    才拦停，宁可少拦也不能拦错 —— 拦错一次就是一单本来能交的问卷被判失败。
    """
    if not platform.question_marker_selectors:
        return []
    raw = driver.execute_script(
        _PAGE_HIDDEN_JS + _PLATFORM_QUESTIONS_JS,
        list(platform.question_marker_selectors),
        platform.question_number_attr,
        platform.question_type_attr,
        bool(visible_only),
    )
    if not isinstance(raw, str):
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []

    out: list[dict] = []
    seen: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            qi = int(item.get("q"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        code = str(item.get("code") or "").strip()
        # 同一题号出现两次（嵌套容器 / 模板异常）时保留第一个：对拍只要有个读数，
        # 拿两个互相矛盾的读数去比我们的探测，等于自己造警。
        if not code or qi in seen:
            continue
        seen.add(qi)
        out.append({"q": qi, "code": code,
                    "required": bool(item.get("required"))})
    return sorted(out, key=lambda d: int(d["q"]))


def detect_answered_questions(driver: Any) -> set[int]:
    """扫描当前页面，返回「已经填好的题号集合」（用于断点续填）。

    单次 JS 注入，覆盖 V2 全部 6 类题型的"已答"判定规则：

    ================================ ===========================================
    题型                              判定"已填"的 DOM 条件
    ================================ ===========================================
    single / radio                  ``input[name=qN]:checked`` 存在
    multi / checkbox                ``input[name=qN]:checked`` 数量 > 0
    dropdown                        ``select[name=qN].selectedIndex > 0``
                                     （0 = "请选择"占位项，视为未填）
    scale / rating                  同 single（隐藏 radio 命中 :checked）
                                     兜底：容器内有 ``.jqchecked/.checked/.on`` 子项
    text / textarea                  ``#qN.value`` 或 ``textarea[name=qN].value``
                                     非空字符串；contentEditable 走 ``innerText``
    matrix_single                    每行 ``input[name=qN_R]:checked`` 都存在
                                     （任一行未选 → 整题视为未填，跳过不填）
    ================================ ===========================================

    :param driver: Selenium WebDriver
    :return:       set[int]，元素是已答的题号；探测失败/页面无题 → 空集合
    """
    raw = driver.execute_script(_OPTION_BLANK_JS + r"""
return (function() {
    var answered = {};
    var blankPending = {};   // 勾了"其他____"但那一格还没写字的题号

    // ---------- 1. 单选 / 多选 ----------
    document.querySelectorAll('input[type="radio"], input[type="checkbox"]').forEach(function(el) {
        var m = (el.name || '').match(/q(\d+)/) || (el.id || '').match(/q(\d+)/);
        if (!m) return;
        var q = parseInt(m[1]);
        if (el.checked) {
            var blank = optionBlankInput(el);
            if (blank && !(blank.value || '').trim()) {
                blankPending[q] = true;
            } else {
                answered[q] = true;
            }
        }
    });

    // ---------- 2. 下拉 ----------
    document.querySelectorAll('select').forEach(function(sel) {
        var nm = sel.name || '', id = sel.id || '';
        var m = nm.match(/q(\d+)/) || id.match(/q(\d+)/) || id.match(/selectq(\d+)/);
        if (!m) return;
        var q = parseInt(m[1]);
        // selectedIndex === 0 通常是"请选择"占位项 → 不算已填
        // 但如果第一个 option 就是真实选项（无占位），selectedIndex > 0 才算
        // 这里采用：selectedIndex > 0 OR (selectedIndex === 0 且第一个 option.value 非空)
        if (sel.selectedIndex > 0) {
            answered[q] = true;
        } else if (sel.selectedIndex === 0 && sel.options.length > 0) {
            var firstOpt = sel.options[0];
            var firstVal = firstOpt ? (firstOpt.value || '') : '';
            var firstTxt = firstOpt ? ((firstOpt.textContent || '').trim()) : '';
            // 第一个 option 有 value 且不是 "请选择" 类提示 → 视为已选第一项
            if (firstVal && !/请选择|选择|---|^\s*$/.test(firstTxt)) {
                answered[q] = true;
            }
        }
    });

    // ---------- 3. 量表 / 评分（兜底：:checked 已在 1. 中命中；这里补 jqchecked 类检测） ----------
    var scaleSel = '[class*="rate"],[class*="star"],[class*="level"],[class*="score"],[class*="rating"]';
    document.querySelectorAll(scaleSel).forEach(function(area) {
        var host = area.closest ? area.closest('.field,.div_question,.q-item,li,.question,div[id^="div"]') : null;
        if (!host) return;
        var idm = (host.id || '').match(/div(\d+)/) || (host.id || '').match(/q(\d+)/);
        if (!idm) return;
        var q = parseInt(idm[1]);
        if (answered[q]) return;  // 已被 :checked 命中
        // 兜底：容器内有 .jqchecked / .checked / .on / .active 类的子项
        var hit = area.querySelector('.jqchecked, .checked, .on, .active');
        if (hit) {
            answered[q] = true;
        }
    });

    // ---------- 4. 填空 ----------
    var fillables = document.querySelectorAll(
        'input[type="text"], input[type="tel"], input[type="number"], input:not([type]), textarea'
    );
    fillables.forEach(function(el) {
        if (el.disabled || el.readOnly) return;
        // 选项自带的填空框（"其他____"）不是一道填空题。它的 id 形如 q2_6_text，
        // 下面那条宽松的 /q(\d+)/ 兜底匹配会把它归给第 2 题，于是"框里有字"
        // 就把整题报成已答 —— 而那一格属于哪个选项、有没有被勾中，全没人看。
        if (isOptionLevelBlank(el)) return;
        var id = el.id || '', name = el.name || '';
        var m = id.match(/^q(\d+)$/) || id.match(/^answerq(\d+)$/) ||
                name.match(/^q(\d+)$/) || id.match(/q(\d+)/);
        if (!m) {
            var parent = el.closest ? el.closest('[id*="div"]') : null;
            if (parent) {
                var pm = (parent.id || '').match(/div(\d+)/);
                if (pm) m = pm;
            }
        }
        if (!m) return;
        var q = parseInt(m[1]);

        var val = '';
        if (el.isContentEditable) {
            val = (el.innerText || '').trim();
        } else {
            val = (el.value || '').toString().trim();
        }
        if (val) {
            answered[q] = true;
        }
    });

    // ---------- 5. 矩阵（单选/多选）：每行都有 :checked 才算整题已答 ----------
    var matrixMap = {};  // qN -> { total: Set(rows), answered: Set(rows) }
    document.querySelectorAll('input[type="radio"], input[type="checkbox"]').forEach(function(r) {
        var m = (r.name || '').match(/^q(\d+)_(\d+)$/);
        if (!m) return;
        var qN = parseInt(m[1]);
        var rowN = parseInt(m[2]);
        if (!matrixMap[qN]) matrixMap[qN] = { total: {}, answered: {} };
        matrixMap[qN].total[rowN] = true;
        if (r.checked) {
            matrixMap[qN].answered[rowN] = true;
        }
    });
    Object.keys(matrixMap).forEach(function(qN) {
        var qi = parseInt(qN);
        var info = matrixMap[qN];
        var totalRows = Object.keys(info.total);
        var answeredRows = Object.keys(info.answered);
        // 矩阵的"已答"判定：所有行都有 :checked
        if (totalRows.length > 0 && answeredRows.length === totalRows.length) {
            // 不直接标记为已答，因为矩阵如果某行被检测出来但实际只有部分行选中，
            // 应当只跳过已选的行——简化：整题全选才算跳过；否则整题重填
            answered[qi] = true;
        } else if (answeredRows.length > 0) {
            // 部分行已答：从 answered 中移除，让 pipeline 整题重填
            // （pipeline 的 _answer_one_question 不支持部分行续填，简单起见整题重填）
            delete answered[qi];
        }
    });

    // ---------- 6. "其他____"没写字的题不算已答 ----------
    // 放在所有判定之后统一收口：量表兜底（第 3 步）也可能把同一题标成已答，
    // 而"勾了带填空的项却没写字"是更强的未答证据。跳过它去提交，换回来的是
    // 一个看不出原因的 unknown。
    Object.keys(blankPending).forEach(function(q) { delete answered[parseInt(q)]; });

    return JSON.stringify(Object.keys(answered).map(function(k) { return parseInt(k); }));
})();
    """)
    try:
        return set(int(x) for x in json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return set()
