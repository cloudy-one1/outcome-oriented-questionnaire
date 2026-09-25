"""两个宿主的权重表对拍：桌面版 ``WeightPanel`` vs webui ``WebService``。

设计稿 ``docs/design/DESIGN_webui.md`` §10 步骤 4。

**这条链为什么先对拍再共用**：解析规则一度有两份实现（``gui/weight_panel`` 与
``webui/weights``）。逐题型对拍抓出三条只有两份才会暴露的问题 —— 锚点整份缺失、
矩阵空行号被接受、结尾多一个 ``|`` 毁掉整份行权重 —— 然后才合并成
``src/weight_text.parse_weight_texts``。先共用再对拍的话这三条一条都看不见。

合并之后本文件换了角色：**比的不再是两份解析，而是两个宿主的接线**。
仍然有意义，因为出错的地方从"规则"挪到了"怎么把文本喂进去、把警告说出来"：

  - 桌面版从 ``tk.StringVar`` 取文本、警告走 ``log(msg, "WARN")``；
  - webui 从 ``session.weight_texts`` 取文本、警告走 ``session.log``。

任一侧接错（拿错键、忘了刷警告、把 str 题号当 int 用），界面就会安静地给出
与另一侧不同的分布 —— 而"同一份权重表在两个界面跑出不同结果"正是本轮要防的事。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui.weight_panel import WeightPanel  # noqa: E402
from webui.service import WebService  # noqa: E402
from webui.session import Availability, RunSession, SessionPaths  # noqa: E402


class _Var:
    """``tk.StringVar`` 的替身：宿主只调 ``.get()``。"""

    def __init__(self, text: str) -> None:
        self._text = text

    def get(self) -> str:
        return self._text


class _PanelHost:
    """``build_weight_config`` 只读这三个名字，所以不必为了跑一段纯逻辑去
    构造真 Tk 面板（那需要根窗口，见 ``conftest.py`` 会话级 tk_root 的说明）。"""

    def __init__(self, questions: list, texts: dict) -> None:
        self.questions = questions
        self.weight_entries = {qi: _Var(t) for qi, t in texts.items()}
        self.logs: list[tuple[str, str]] = []

    def log(self, msg: str, tag: str = "INFO") -> None:
        self.logs.append((tag, msg))


def tk_host(questions: list, texts: dict) -> tuple[dict, list[str]]:
    host = _PanelHost(questions, texts)
    cfg = WeightPanel.build_weight_config(host)
    return cfg, [m for _tag, m in host.logs]


def web_host(questions: list, texts: dict, tmp_path=None) -> tuple[dict, list[str]]:
    session = RunSession(
        paths=SessionPaths(str(tmp_path or ".")),
        availability=Availability(config_io=True, history=False, qr=True,
                                  selenium=True),
    )
    session.set_questions(questions)          # 会顺手预填默认串，下面覆盖掉
    session.set_weight_texts({int(k): v for k, v in texts.items()})
    cfg = WebService(session).build_weight_config()
    return cfg, [row["text"] for row in session.log_lines if row["tag"] == "WARN"]


# --------------------------------------------------------------- 用例语料
#
# 每题都带 title：锚点由题干算出，不带 title 就两边都省 —— 那会把"接没接上锚点"
# 这条真实差异悄悄抹平。不带的情况另有一条覆盖。

CHOICE = {"q": 1, "type": "single", "title": "你最常用的浏览器",
          "choices": ["Edge", "Chrome"]}
MULTI = {"q": 1, "type": "multi", "title": "勾选你用过的",
         "choices": ["a", "b", "c"]}
DROPDOWN = {"q": 1, "type": "dropdown", "title": "选一个城市",
            "choices": ["北京", "上海", "广州"]}
NO_CHOICES = {"q": 1, "type": "single", "title": "没探测到选项的单选题"}
SCALE = {"q": 1, "type": "scale", "title": "满意度", "scale": 5}
SCALE_ZERO = {"q": 1, "type": "scale", "title": "NPS", "scale": 10,
              "scale_min": 0}
RATING = {"q": 1, "type": "rating", "title": "打分", "scale": 5}
TEXT = {"q": 1, "type": "text", "title": "姓名", "field": "name"}
TEXT_PLAIN = {"q": 1, "type": "textarea", "title": "意见"}
MATRIX = {"q": 1, "type": "matrix", "title": "矩阵",
          "rows": ["1", "2"], "cols": ["a", "b", "c"]}
MATRIX_SINGLE = {"q": 1, "type": "matrix_single", "title": "矩阵单选",
                 "rows": ["1", "2"], "cols": ["a", "b", "c"]}
MATRIX_MULTI = {"q": 1, "type": "matrix_multi", "title": "矩阵多选",
                "rows": ["1", "2"], "cols": ["a", "b"]}
SORT = {"q": 1, "type": "sort", "title": "排序", "items": ["3", "1", "2"]}
UNKNOWN = {"q": 1, "type": "slider", "title": "滑条"}

CASES: list[tuple[str, dict, str]] = [
    # ---- 选择类 ----
    ("单选-正常", CHOICE, "0.5,0.5"),
    ("单选-留空", CHOICE, ""),
    ("单选-个数不符", CHOICE, "0.3,0.3,0.4"),
    ("单选-格式错", CHOICE, "abc"),
    ("单选-含负数", CHOICE, "-0.5,1.5"),
    ("单选-含 NaN", CHOICE, "nan,1"),
    ("单选-含 Inf", CHOICE, "inf,1"),
    ("单选-全零", CHOICE, "0,0"),
    ("单选-多余空格", CHOICE, " 0.5 , 0.5 "),
    ("多选-正常", MULTI, "0.2,0.3,0.5"),
    ("多选-个数不符", MULTI, "0.5,0.5"),
    ("下拉-正常", DROPDOWN, "0.2,0.3,0.5"),
    ("无选项的单选-写权重", NO_CHOICES, "0.4,0.6"),
    ("无选项的多选-写权重", {"q": 1, "type": "multi", "title": "没选项的多选"},
     "0.4,0.6"),
    # ---- 量表 ----
    ("量表-留空", SCALE, ""),
    ("量表-单值命中", SCALE, "4"),
    ("量表-单值越界", SCALE, "9"),
    ("量表-单值为 0", SCALE, "0"),
    ("量表-满权重串", SCALE, "0,0,0,0,1"),
    ("量表-长度不足", SCALE, "1,2"),
    ("量表-长度超出", SCALE, "1,1,1,1,1,1,1"),
    ("量表-含负数", SCALE, "1,-1,1,1,1"),
    ("量表-格式错", SCALE, "好,不号"),
    ("量表-0 起评", SCALE_ZERO, "0"),
    ("量表-0 起评写满", SCALE_ZERO, ",".join(["1"] * 11)),
    ("量表-rating 别名", RATING, "1,2,3,4,5"),
    # ---- 填空 ----
    ("填空-留空", TEXT, ""),
    ("填空-候选串", TEXT, "张三,李四,王五"),
    ("填空-无 field", TEXT_PLAIN, "随便写"),
    # ---- 矩阵 ----
    ("矩阵-留空", MATRIX, ""),
    ("矩阵-两行都写", MATRIX, "1:0.1,0.3,0.6 | 2:0.2,0.2,0.6"),
    ("矩阵-某行格式错", MATRIX, "1:0.1,0.3,0.6 | 2:a,b,c"),
    ("矩阵-缺冒号", MATRIX, "1 0.1,0.3,0.6"),
    ("矩阵-行号为空", MATRIX, ":0.1,0.3,0.6"),
    ("矩阵-含负数", MATRIX, "1:-1,0.3,0.6"),
    ("矩阵-多余竖线", MATRIX, "1:0.1,0.3,0.6 |"),
    ("矩阵-存储名别名", MATRIX_SINGLE, "1:0.5,0.2,0.3 | 2:0.1,0.1,0.8"),
    ("矩多-正常", MATRIX_MULTI, "1:0.5,0.5 | 2:0.2,0.8"),
    # ---- 排序 ----
    ("排序-留空", SORT, ""),
    ("排序-正常", SORT, "3,1,2"),
    ("排序-含未知项", SORT, "3,9"),
    ("排序-部分合法", SORT, "3,1"),
    # ---- 未知题型兜底 ----
    ("未知题型-写权重", UNKNOWN, "0.5,0.5"),
    ("未知题型-留空", UNKNOWN, ""),
    ("未知题型-格式错", UNKNOWN, "abc"),
    # ---- 没有题干就不该有锚点 ----
    ("无标题-单选", {"q": 1, "type": "single", "choices": ["a", "b"]}, "0.5,0.5"),
    ("无标题-量表", {"q": 1, "type": "scale", "scale": 5}, "3"),
]


@pytest.mark.parametrize("label,q,text", CASES,
                         ids=[c[0] for c in CASES])
def test_both_hosts_produce_the_same_config_and_the_same_words(
    label, q, text, tmp_path
) -> None:
    """同一题、同一串文本：两边的 cfg 与**说出来的话**都必须一致。

    cfg 不一致 = 答卷分布不一致；警告不一致 = 一个界面告诉用户"这行没生效"、
    另一个不说。后者是更隐蔽的那类，所以两条一起断言。
    """
    questions = [q]
    texts = {1: text}
    tk_cfg, tk_words = tk_host(questions, texts)
    web_cfg, web_words = web_host(questions, texts, tmp_path)
    assert web_cfg == tk_cfg, label
    assert web_words == tk_words, label


def test_a_whole_survey_parses_the_same_way_on_both_sides(tmp_path) -> None:
    """一整套 13 题混排：逐题对拍通过不等于整卷也通过（题号、顺序、锚点都在动）。"""
    questions = [
        {**CHOICE, "q": 1}, {**MULTI, "q": 2}, {**DROPDOWN, "q": 3},
        {**SCALE, "q": 4}, {**TEXT, "q": 5},
        {"q": 6, "type": "text", "title": "手机", "field": "phone"},
        {"q": 7, "type": "text", "title": "邮箱", "field": "email"},
        {**MATRIX, "q": 8}, {**MATRIX_MULTI, "q": 9},
        {**SCALE_ZERO, "q": 10},
        {"q": 11, "type": "text", "title": "公司"},
        {"q": 12, "type": "single", "title": "性别", "choices": ["男", "女"]},
        {**SORT, "q": 13},
    ]
    texts = {1: "0.5,0.5", 2: "0.2,0.3,0.5", 3: "", 4: "5", 5: "张三,李四",
             6: "13800000000", 7: "", 8: "1:0.2,0.3,0.5", 9: "",
             10: ",".join(["1"] * 11), 11: "", 12: "0.9,0.1", 13: "3,1,2"}
    tk_cfg, tk_words = tk_host(questions, texts)
    web_cfg, web_words = web_host(questions, texts, tmp_path)
    assert web_cfg == tk_cfg
    assert web_words == tk_words
    # Q3 留空 → 整题不进 cfg（交给引擎的等权重分支），所以是 12 不是 13
    assert sorted(web_cfg) == [1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
    assert all("anchor" in e for e in web_cfg.values()), "每题都该带锚点"


def test_string_question_keys_survive_the_json_round_trip(tmp_path) -> None:
    """题号从 JSON 配置回来是字符串、权重表内部按 int 存。

    两侧宿主各自的取文本入口都必须认这两种，否则一次 ``--config`` 往返就把整列
    读成"没填"，于是所有题静默退化成等权重。
    """
    questions = [{**CHOICE, "q": 7}]
    tk_cfg, _ = tk_host(questions, {7: "0.5,0.5"})
    web_cfg, _ = web_host(questions, {7: "0.5,0.5"}, tmp_path)
    assert web_cfg == tk_cfg
    assert list(web_cfg) == [7]
