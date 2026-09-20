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
import os
import shutil
import subprocess
import tempfile

import pytest

from src.interactions import _scripts as S


# ---------------------------------------------------------------------------
#  choices.click_option_script 结构不变量
# ---------------------------------------------------------------------------
def test_click_option_contains_q_selector_and_return() -> None:
    out = S.click_option_script(q=3, choice=5)
    # v2.6：q / choice 改为 json.dumps 成 JS 字面量，selector 在 JS 运行时拼接，
    # 因此不再出现字面 '#q3_5'，而是拼接表达式 + 两个安全字面量。
    assert "var q = 3;" in out, "题号必须以 JS 字面量注入"
    assert "var choice = 5;" in out, "选项值必须以 JS 字面量注入"
    assert "'#q' + q + '_' + choice" in out, "id selector 的拼接约定未保留"
    # 同题其他选项清理用的 selector；拼接必须发生在 JS 字符串字面量**之外**
    assert """input[name=\"q' + q + '\"]""" in out, (
        "同题其他单选选项清空的 selector 未生成"
    )
    # 反例守卫：写成 document.querySelector("input[name='q' + q + '']") 是**合法 JS**
    # （node --check 过得去），但 '+ q +' 落进了双引号字面量内部，
    # 浏览器会报 "is not a valid selector"。这类语义错误只有真浏览器 E2E 能兜住，
    # —— v2.6 开发过程中正是 E2E 抓到了这里的一次回归。
    assert '"input[name=' not in out, (
        "selector 拼接被写进了双引号字面量内部，运行时是非法 CSS selector"
    )
    assert "return true" in out


def test_click_option_choice_cannot_break_out_of_js_literal() -> None:
    """页面可控的值不得成为 JS 代码 —— 这是本用例存在的唯一理由。

    旧实现把 choice 裸插进 ``value='{choice}'``，带单引号的值即可闭合字符串
    并续接任意表达式。现在走 json.dumps，值只能待在字面量里。
    """
    evil = "1'; alert(1); var x='"
    out = S.click_option_script(q=1, choice=evil)
    assert json.dumps(evil) in out, "choice 必须经 json.dumps 序列化"
    assert "alert(1); var x='''" not in out
    # 未被转义的裸单引号注入点不该再存在（形如 value='...'; alert）
    assert "']; alert(" not in out
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
    # scale_max=None 必须落成 JS 字面量 null。
    # 旧实现落空串得到 `var scaleMax = ;` —— 语法错误，execute_script 直接抛
    # JavascriptException，重试 3 次后该题记为失败。下方 JS 的
    # `if (!scaleMax) scaleMax = items.length` 兜底本来就期望收到 null。
    assert "var scaleMax = null;" in out_none
    assert "var scaleMax = ;" not in out_none
    # 5 → scaleMax = 5（sm = str(int(5)) → "5" → 直接拼到 JS）
    assert "var scaleMax = 5;" in out_5


def test_set_scale_uses_scale_min_for_index() -> None:
    """v2.6：分值→子项下标必须减 scale_min，而不是恒减 1。

    2~10 分的量表里 value=10 是第 9 个子项（下标 8）。旧 JS 写死
    ``var idx = val - 1`` 会点错一格；越界时被 clamp 到最后一格，
    于是"打了 10 分"实际提交的是别的值，且毫无报错。
    """
    out_min2 = S.set_scale_script(q=4, value=10, scale_max=10, scale_min=2)
    assert "var scaleMin = 2;" in out_min2
    assert "var idx = val - scaleMin;" in out_min2
    assert "var idx = val - 1;" not in out_min2
    # scale_min 省略时行为与旧版一致（起点 1）
    out_default = S.set_scale_script(q=4, value=3, scale_max=5)
    assert "var scaleMin = 1;" in out_default
    # scale_min=None 同样落 1，且不能产出 "var scaleMin = ;"
    out_none = S.set_scale_script(q=4, value=3, scale_max=5, scale_min=None)
    assert "var scaleMin = 1;" in out_none


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


# ---------------------------------------------------------------------------
#  生成的 JS 必须是**语法合法**的 JS（结构性回归防线）
#
#  为什么需要这一组：本文件其余断言全是「输出字符串包含某个子串」，
#  子串断言无法发现 JS 语法被破坏——因为 fake driver 从不抛
#  JavascriptException，离线套件结构上看不见坏 JS。set_scale_script 的
#  `var scaleMax = ;` 就是这么溜过去并被锁进断言的。
# ---------------------------------------------------------------------------

def _script_cases() -> dict[str, str]:
    """每个脚本生成器在**边界输入**下的输出。

    边界输入是重点：None / 空列表 / 带引号的文本，正是 Python 侧插值
    最容易产出非法 JS 的地方。
    """
    return {
        "click_option_script__basic": S.click_option_script(q=3, choice=5),
        "click_option_script__zero_ids": S.click_option_script(q=0, choice=0),
        "click_option_script__quote_in_choice": S.click_option_script(
            q=1, choice="a'; alert(1); var x='"
        ),
        "click_question_options_script__basic": S.click_question_options_script(
            q=7, choices=[1, 3, 5]
        ),
        "click_question_options_script__empty_choices": S.click_question_options_script(
            q=7, choices=[]
        ),
        "fill_text_script__quotes": S.fill_text_script(q=2, text='he said "hi"\n\t</script>'),
        "fill_text_script__empty": S.fill_text_script(q=2, text=""),
        "set_scale_script__none_max": S.set_scale_script(q=4, value=3, scale_max=None),
        "set_scale_script__int_max": S.set_scale_script(q=4, value=3, scale_max=5),
        "set_scale_script__min_2": S.set_scale_script(
            q=4, value=10, scale_max=10, scale_min=2
        ),
        "set_scale_script__min_none": S.set_scale_script(
            q=4, value=3, scale_max=5, scale_min=None
        ),
        "select_dropdown_script__int": S.select_dropdown_script(q=6, choice_value=2),
        "select_dropdown_script__str": S.select_dropdown_script(q=6, choice_value='a"b'),
        "fill_matrix_single_script__basic": S.fill_matrix_single_script(
            q=9, row_selections={1: 2, 2: "北京"}
        ),
        "submit_button_fallback_script__noargs": S.submit_button_fallback_script(),
        "submit_success_detect_script__noargs": S.submit_success_detect_script(),
    }


_SCRIPT_CASES = _script_cases()

# Python 侧把空串/None 插值进 JS 表达式槽位时产生的退化 token 序列。
# 这些片段在合法 JS 里不可能出现，因此无需 node 也能挡住最常见的一类回归。
_DEGENERATE_PATTERNS = (
    "= ;", "=  ;", "= ,", "= )", "= ]", "( ,", ", ,", ", )", "&& )", "|| )",
)


@pytest.mark.parametrize("name", sorted(_SCRIPT_CASES))
def test_script_has_no_empty_interpolation(name: str) -> None:
    """不依赖 node：插值槽位绝不能留空（``var x = ;`` 这一类）。"""
    body = _SCRIPT_CASES[name]
    for pat in _DEGENERATE_PATTERNS:
        assert pat not in body, f"{name} 含退化片段 {pat!r}，疑似 Python 插值留空"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node，跳过 JS 语法解析")
@pytest.mark.parametrize("name", sorted(_SCRIPT_CASES))
def test_script_is_syntactically_valid_js(name: str) -> None:
    """用 ``node --check`` 真正解析生成的 JS（脚本体包进函数作用域）。"""
    body = _SCRIPT_CASES[name]
    # 这些片段是 execute_script 的**函数体**（含 return / arguments），
    # 必须包进 function 才能作为独立程序解析。
    wrapped = "function __probe() {" + body + "\n}\n"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(wrapped)
        path = fh.name
    try:
        proc = subprocess.run(
            ["node", "--check", path], capture_output=True, text=True, timeout=30
        )
    finally:
        os.unlink(path)
    assert proc.returncode == 0, (
        f"{name} 生成的 JS 语法非法：\n{proc.stderr.strip()[:400]}\n"
        f"--- 前 200 字符 ---\n{body[:200]}"
    )


def test_all_script_builders_are_covered_by_syntax_cases() -> None:
    """防止新增脚本生成器却忘了加进语法防线。

    ``_SCRIPT_CASES`` 的键约定为 ``"<builder 名>__<场景>"``，
    因此「builder 名是某个键的前缀」即视为已覆盖。
    """
    builders = sorted(
        n for n, o in vars(S).items()
        if n.endswith("_script") and callable(o) and not n.startswith("_")
    )
    assert builders, "未从 _scripts 发现任何 *_script 生成器，导入面可能变了"
    keys = list(_SCRIPT_CASES)
    missing = [b for b in builders if not any(k == b or k.startswith(b + "_") for k in keys)]
    assert not missing, f"以下脚本生成器未纳入 JS 语法测试：{missing}"
