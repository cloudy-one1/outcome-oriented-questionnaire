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


def detect_questions(driver: Any) -> list[dict]:
    """扫描当前页面，自动提取所有题目的结构信息（v2.0 全题型版）。

    实现方式：
      单次 execute_script 注入 → 串行识别 6 类题型 → 按题号合并去重
      → 升序排序后返回。相比 Selenium 原生查找元素速度快一个数量级，
      且不会被问卷星的动态渲染干扰。

    参数：
      driver : Selenium WebDriver 实例

    返回：
      list[dict]，每个元素至少包含 ``q`` 和 ``type``；各题型的额外字段如下：

      **V1 兼容（单选 / 多选）** ::

          {"q": 1, "type": "single", "choices": [1, 2, 3, 4]}
          {"q": 2, "type": "multi",  "choices": [1, 2, 3]}

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
    raw = driver.execute_script(r"""
(function() {
    var result = [];
    var map = {};   // qnum(int) -> question dict

    // ---------- 工具：向 map 合并 / 获取一题槽位 ----------
    function slot(q) {
        var qi = parseInt(q);
        if (!map[qi]) map[qi] = { q: qi };
        return map[qi];
    }

    // ---------- 1. 单选 / 多选（v1 逻辑，保持完全一致） ----------
    (function() {
        var inputs = document.querySelectorAll(
            'input[type="radio"], input[type="checkbox"]'
        );
        inputs.forEach(function(el) {
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
            }
        });
    })();

    // ---------- 2. 下拉（<select name=qN>） ----------
    (function() {
        var selects = document.querySelectorAll('select');
        selects.forEach(function(sel) {
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
                // 数一下带 value 的隐藏 radio 数量
                var hiddenRadios = document.querySelectorAll(
                    'input[type="radio"][name="q' + qid + '"]'
                );
                levelCount = hiddenRadios.length;
            }
            if (levelCount >= 2 && levelCount <= 12) {
                var s = slot(qid);
                s.type = 'scale';
                s.scale = levelCount;
                s.scale_min = 1;
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

    // ---------- 5. 矩阵单选（每行一组 radio，同题不同行 name 可能是 qN_1, qN_2） ----------
    (function() {
        // 找所有 name 形如 qN_R 的 radio → N 是题号，R 是行号
        var allRadios = document.querySelectorAll('input[type="radio"]');
        var matrixMap = {};  // qN -> { rows: Set(R), cols: Set(values) }
        allRadios.forEach(function(r) {
            var m = (r.name || '').match(/^q(\d+)_(\d+)$/);
            if (!m) return;
            var qN = parseInt(m[1]);
            var rowN = parseInt(m[2]);
            if (!matrixMap[qN]) matrixMap[qN] = { rows: new Set(), cols: new Set() };
            matrixMap[qN].rows.add(rowN);
            var v = parseInt(r.value);
            if (!isNaN(v)) matrixMap[qN].cols.add(v);
        });

        Object.keys(matrixMap).forEach(function(qN) {
            var qi = parseInt(qN);
            var s = slot(qi);
            // 如果该题已被识别为 single/multi，且 matrix 的 row 只有 1 条 → 保留 single
            if (s.type === 'single' || s.type === 'multi') {
                if (matrixMap[qN].rows.size <= 1) return;
            }
            var rowsArr = Array.from(matrixMap[qN].rows).sort(function(a, b) { return a - b; });
            var colsArr = Array.from(matrixMap[qN].cols).sort(function(a, b) { return a - b; });
            if (rowsArr.length >= 2 && colsArr.length >= 2) {
                s.type = 'matrix_single';
                s.rows = rowsArr;
                s.cols = colsArr;
            }
        });
    })();

    // ---------- 后处理：对 single/multi 的 choices 排序；按题号升序输出 ----------
    Object.keys(map).forEach(function(q) {
        var it = map[q];
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
