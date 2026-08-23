"""第六章整改：_scripts 模块的「最小不变量」单元测试。

设计哲学（参考 Experience 492781）：不对 Selenium 行为做大而全的模拟（那是
test_interaction_submit.py 的 E2E Mock 职责），只对 `_scripts.py` 中每个
脚本生成函数做"结构不变量断言"，保证：
    1. 关键占位符正确替换（q number 出现在 selector 中）
    2. JSON 转义正确（特殊字符不会破坏 JS 语法）
    3. 输出是"形如 JS 语句"的字符串（至少包含 return true/false 关键字）
"""

from __future__ import annotations

import json

import pytest

from src.interactions import _scripts as S


# ---------------------------------------------------------------------------
#  choices.click_option_script 结构不变量
# ---------------------------------------------------------------------------
def test_click_option_contains_q_selector_and_return() -> None:
    out = S.click_option_script(q=3, choice=5)
    assert "#q3_5" in out, f"f-string 替换失败，未找到 #q3_5。片段: {out[:100]!r}"
    assert "input[name='q3']" in out, "同题其他单选选项清空的 selector 未生成"
    assert "return true" in out


def test_click_option_does_not_double_escape_braces() -> None:
    """JS 里的 {} 转义不应该出现在 f-string 已处理后的输出里。"""
    out = S.click_option_script(q=1, choice=1)
    # 转义前是 {{ behavior: ... }}，转义后应只剩一个 {
    assert "{{behavior" not in out and "}}" not in out.replace("}});", ""), (
        "f-string 双大括号转义残留（应只剩单个大括号）"
    )


# ---------------------------------------------------------------------------
#  choices.click_question_options_script
# ---------------------------------------------------------------------------
def test_click_question_options_injects_json_choices() -> None:
    choices = [1, 3, 5]
    out = S.click_question_options_script(q=7, choices=choices)
    # choices 必须作为 JSON 数组内嵌，便于 JS 直接 `var choices = [...]`
    expected_json = json.dumps(choices)
    assert expected_json in out
    assert "return any_hit" in out


# ---------------------------------------------------------------------------
#  text.fill_text_script — JSON/text 转义正确性（第六章重点）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dangerous_text", [
    "</script><script>alert(1)</script>",  # HTML/JS 注入尝试
    "引号\"'特殊\\字符\n换行",
    "C:\\Users\\路径",  # Windows 路径反斜杠
    "中文姓名 😀 emoji",
])
def test_fill_text_json_escapes_injection(dangerous_text: str) -> None:
    """填空题的 text 必须经 json.dumps 转义，避免输出语法错误或注入。"""
    out = S.fill_text_script(q=2, text=dangerous_text)
    # Python 侧重新解析 JSON：应该能完整还原（验证我们用 json.dumps）
    injected_json = json.dumps(dangerous_text)
    assert injected_json in out, "text_json 插入必须使用 json.dumps 序列化"
    # 还原值一致
    assert json.loads(injected_json) == dangerous_text
    # 整体仍然返回布尔
    assert "return true" in out


# ---------------------------------------------------------------------------
#  scale.set_scale_script
# ---------------------------------------------------------------------------
def test_set_scale_handles_scale_max_none_vs_int() -> None:
    out_none = S.set_scale_script(q=4, value=3, scale_max=None)
    out_5 = S.set_scale_script(q=4, value=3, scale_max=5)
    # 原实现保持一致：scale_max=None 时 sm="" 替换后得到 var scaleMax = ;
    # （值为空会被后续 if (!scaleMax) { scaleMax = items.length } 兜底）
    assert "var scaleMax = ;" in out_none
    # 5 → scaleMax = 5（原实现 sm = str(int(5)) → "5" → 直接拼到 JS）
    assert "var scaleMax = 5;" in out_5


# ---------------------------------------------------------------------------
#  dropdown.select_dropdown_script — value(int/str) 两条命中路径
# ---------------------------------------------------------------------------
def test_select_dropdown_int_value_generates_targetInt() -> None:
    out = S.select_dropdown_script(q=6, choice_value=2)
    assert "targetInt" in out, "int 类型 choice_value 必须走 targetInt 分支"
    # targetInt 应为 JSON 字符串 "2"（不是 2）
    assert json.dumps("2") in out or '"2"' in out
    assert "return true" in out


def test_select_dropdown_string_value_only_targetStr() -> None:
    out = S.select_dropdown_script(q=6, choice_value="不是数字-xx")
    # targetInt = null
    assert "targetInt = null" in out
    # targetStr 是 json.dumps("不是数字-xx")
    assert json.dumps("不是数字-xx") in out


# ---------------------------------------------------------------------------
#  matrix.fill_matrix_single_script
# ---------------------------------------------------------------------------
def test_fill_matrix_injects_rowmap_via_json_dumps() -> None:
    row_map = {1: 2, 3: 4, 5: "北京"}
    out = S.fill_matrix_single_script(q=9, row_selections=row_map)
    expected_json = json.dumps(row_map, ensure_ascii=False, default=str)
    assert expected_json in out, "row_map 必须以完整 JSON 注入（含 中文键/值 不转义）"
    # 原实现中 name 是 JS 运行时拼接的 "'q' + q + '_' + rowKey"，所以字面出现的是：
    concat_pattern = "'q' + q + '_' + rowKey"
    assert concat_pattern in out, (
        "name='qN_rowIdx' 的动态拼接约定未保留（原先使用 JS 运行时拼接）"
    )


# ---------------------------------------------------------------------------
#  submit 模块小片段
# ---------------------------------------------------------------------------
def test_submit_selectors_list_contains_known_ids() -> None:
    out = S.submit_button_fallback_script()
    # 问卷星常见 id 至少包含几个（注意引号形式可能为单或双，都合法）
    assert "#divSubmit" in out, "submit fallback JS 缺少 #divSubmit"
    assert "#ctlNext" in out, "submit fallback JS 缺少 #ctlNext"
    # input[type=submit] 可能被 quote 成 'submit' 或 "submit"，都接受
    assert "type='submit'" in out or 'type="submit"' in out, (
        "submit fallback JS 缺少 input[type=submit]/button[type=submit] 的选择器"
    )


def test_submit_success_detect_contains_keywords() -> None:
    out = S.submit_success_detect_script()
    for kw in ['提交成功', '感谢您的参与', '.submit-succ']:
        assert kw in out, f"success detect JS 缺少关键词 {kw}"
