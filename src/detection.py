"""问卷结构自动探测模块。

扫描问卷星页面，通过 JS 注入直接操作 DOM，自动提取所有题目的结构信息
（题号、类型、选项值列表）。相比 Selenium 原生查找元素更快，且不会被
问卷星的动态渲染干扰。
"""

from __future__ import annotations

import json
from typing import Any


def detect_questions(driver: Any) -> list[dict]:
    """扫描当前页面，自动提取所有题目的结构信息。

    实现方式：
      通过 JS 注入直接操作 DOM，无需 Selenium 逐个查找元素，
      速度快一个数量级，且不会被问卷星的动态渲染干扰。

    JS 逻辑：
      1. 找到页面所有的 <input type="radio"> 和 <input type="checkbox">
      2. 从 name/id 属性中提取题号（正则匹配 q + 数字）
      3. 按题号分组，收集每道题的类型（单选/多选）和选项值列表
      4. 合并后按题号升序排序后返回

    参数：
      driver : Selenium WebDriver 实例

    返回：
      list[dict]，每个元素格式：
        {"q": 题号(int), "type": "single"|"multi", "choices": [选项值列表(int)]}

      示例：
        [{"q": 1, "type": "single", "choices": [1, 2, 3, 4]},
         {"q": 2, "type": "multi",  "choices": [1, 2, 3]}]
    """
    raw = driver.execute_script("""
        var result = [];

        // ---- 单选/复选 ----
        var inputs = document.querySelectorAll('input[type="radio"], input[type="checkbox"]');
        var map = {};

        inputs.forEach(function(el) {
            var m = (el.name || '').match(/q(\\d+)/);
            if (!m) m = (el.id || '').match(/q(\\d+)/);
            if (!m) return;

            var q = parseInt(m[1]);
            if (!map[q])
                map[q] = {type: el.type, choices: []};

            var v = parseInt(el.value);
            if (!isNaN(v) && map[q].choices.indexOf(v) === -1)
                map[q].choices.push(v);
        });

        Object.keys(map)
            .sort(function(a, b) { return parseInt(a) - parseInt(b); })
            .forEach(function(q) {
                var c = map[q].choices.sort(function(a, b) { return a - b; });
                result.push({
                    q: parseInt(q),
                    type: map[q].type === 'radio' ? 'single' : 'multi',
                    choices: c
                });
            });

        return JSON.stringify(result);
    """)
    return json.loads(raw)
