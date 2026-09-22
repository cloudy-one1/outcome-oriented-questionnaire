"""v2.0 — 新题型支持（填空/量表/下拉/矩阵）TDD RED 测试。

覆盖 answering_v2.generate_answer 返回统一 dict 结构：
    - {"type": "single",   "selected": [2]}                         # 旧题型兼容
    - {"type": "multi",    "selected": [1, 3, 4]}                  # 旧题型兼容
    - {"type": "text",     "text": "张三", "field": "name"}        # 填空
    - {"type": "scale",    "value": 5}                              # 1-5 量表打分
    - {"type": "dropdown", "selected": [1]}                         # 下拉
    - {"type": "matrix_single", "rows": {1: 2, 2: 4}}              # 矩阵单选（行->列）
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# 手机号 / 邮箱 / 姓名 中文正则（宽松校验）
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_CHINESE_NAME_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")


class TestNewQuestionTypes(unittest.TestCase):
    """v2.0 新题型：RED 阶段。"""

    # 懒加载：测试会保证模块存在
    @classmethod
    def setUpClass(cls) -> None:
        from src import answering_v2  # type: ignore
        cls.mod: Any = answering_v2

    # ------------------------------------------------------------------
    # 兼容性：旧题型 single / multi 的 generate_answer 仍能用
    # ------------------------------------------------------------------
    def test_single_legacy_compatible(self) -> None:
        """单选题：返回 type=single + selected 列表，长度 1。"""
        q = {"q": 1, "type": "single", "choices": [1, 2, 3, 4]}
        ans = self.mod.generate_answer(q)
        self.assertEqual(ans["type"], "single")
        self.assertEqual(len(ans["selected"]), 1)
        self.assertIn(ans["selected"][0], [1, 2, 3, 4])

    def test_multi_legacy_compatible(self) -> None:
        """多选题：不重复、已排序。"""
        q = {"q": 2, "type": "multi", "choices": [1, 2, 3, 4, 5]}
        for _ in range(50):
            ans = self.mod.generate_answer(q)
            self.assertEqual(ans["type"], "multi")
            s = ans["selected"]
            self.assertEqual(s, sorted(s))
            self.assertEqual(len(s), len(set(s)))
            self.assertTrue(1 <= len(s) <= 5)

    # ------------------------------------------------------------------
    # 1. 填空题（text）：多种预设字段类型 + 自由文本
    # ------------------------------------------------------------------
    def test_text_field_name_returns_chinese_name(self) -> None:
        """field=name → 中文姓名，2-4 个汉字。"""
        q = {"q": 3, "type": "text", "field": "name"}
        for _ in range(50):
            ans = self.mod.generate_answer(q)
            self.assertEqual(ans["type"], "text")
            self.assertEqual(ans["field"], "name")
            self.assertTrue(
                _CHINESE_NAME_RE.match(ans["text"]),
                f"非中文姓名: {ans['text']!r}",
            )

    def test_text_field_phone_returns_valid_cn_mobile(self) -> None:
        """field=phone → 11 位中国手机号。"""
        q = {"q": 4, "type": "text", "field": "phone"}
        for _ in range(100):
            ans = self.mod.generate_answer(q)
            self.assertEqual(ans["type"], "text")
            self.assertTrue(
                _PHONE_RE.match(ans["text"]),
                f"非有效手机号: {ans['text']!r}",
            )

    def test_text_field_email_returns_valid_email(self) -> None:
        """field=email → 含 @ 的合法邮箱格式。"""
        q = {"q": 5, "type": "text", "field": "email"}
        for _ in range(50):
            ans = self.mod.generate_answer(q)
            self.assertEqual(ans["type"], "text")
            self.assertTrue(
                _EMAIL_RE.match(ans["text"]),
                f"非邮箱格式: {ans['text']!r}",
            )

    def test_text_field_address_has_city_and_street(self) -> None:
        """field=address → 含"省/市/路/号"等关键词的中文地址。"""
        q = {"q": 6, "type": "text", "field": "address"}
        ans = self.mod.generate_answer(q)
        self.assertEqual(ans["type"], "text")
        # 至少包含 1 个地址相关关键字
        keywords = ("省", "市", "区", "路", "街", "号", "大道", "巷")
        self.assertTrue(
            any(k in ans["text"] for k in keywords),
            f"不像地址: {ans['text']!r}",
        )

    def test_text_no_field_random_sentence(self) -> None:
        """未指定 field → 随机中文短句（≥5字）。"""
        q = {"q": 7, "type": "text"}
        ans = self.mod.generate_answer(q)
        self.assertEqual(ans["type"], "text")
        self.assertGreaterEqual(len(ans["text"].strip()), 5)

    def test_text_custom_options_from_list(self) -> None:
        """options 指定候选列表 → 从候选中随机。"""
        candidates = ["北京大学", "清华大学", "复旦大学", "上海交大"]
        q = {"q": 8, "type": "text", "options": candidates}
        for _ in range(50):
            ans = self.mod.generate_answer(q)
            self.assertIn(ans["text"], candidates)

    # ------------------------------------------------------------------
    # 2. 量表题（scale）：1..N 打分，支持加权偏向高分
    # ------------------------------------------------------------------
    def test_scale_default_5_star_returns_1_to_5(self) -> None:
        """默认 scale=5 → 取值 ∈ {1,2,3,4,5} 整数。"""
        q = {"q": 9, "type": "scale"}
        values = {self.mod.generate_answer(q)["value"] for _ in range(200)}
        self.assertTrue(values.issubset({1, 2, 3, 4, 5}))

    def test_scale_custom_max_10(self) -> None:
        """指定 scale=10 → 取值 ∈ [1,10]。"""
        q = {"q": 10, "type": "scale", "scale": 10}
        for _ in range(200):
            v = self.mod.generate_answer(q)["value"]
            self.assertTrue(1 <= v <= 10, f"超出范围: {v}")
            self.assertIsInstance(v, int)

    def test_scale_bias_high_weight_biased_to_5(self) -> None:
        """用户指定 weights=[0,0,0,0,1] → 永远打 5 分。"""
        q = {"q": 11, "type": "scale", "scale": 5, "weights": [0, 0, 0, 0, 1]}
        for _ in range(50):
            self.assertEqual(self.mod.generate_answer(q)["value"], 5)

    # ------------------------------------------------------------------
    # 3. 下拉选择题（dropdown）：等价于单选
    # ------------------------------------------------------------------
    def test_dropdown_picks_one_option(self) -> None:
        """dropdown 返回 selected 列表长度=1。"""
        q = {"q": 12, "type": "dropdown", "choices": [1, 2, 3, 4, 5]}
        for _ in range(50):
            ans = self.mod.generate_answer(q)
            self.assertEqual(ans["type"], "dropdown")
            self.assertEqual(len(ans["selected"]), 1)
            self.assertIn(ans["selected"][0], [1, 2, 3, 4, 5])

    def test_dropdown_with_weights_obeys(self) -> None:
        """带权重：第3选项权重=0 → 永远不选。"""
        q = {
            "q": 13,
            "type": "dropdown",
            "choices": [1, 2, 3, 4],
            "weights": [1, 1, 0, 1],  # 索引 2 (选项3) 权重 0
        }
        for _ in range(200):
            ans = self.mod.generate_answer(q)
            self.assertNotEqual(ans["selected"][0], 3)

    # ------------------------------------------------------------------
    # 4. 矩阵单选（matrix_single）：每行独立作答（类似一行一道单选题）
    # ------------------------------------------------------------------
    def test_matrix_single_returns_rows_dict(self) -> None:
        """返回 rows: {row_idx: col_idx}，每行一答。"""
        q = {
            "q": 14,
            "type": "matrix_single",
            "rows": [1, 2, 3, 4],        # 矩阵行（子问题）
            "cols": [1, 2, 3, 4, 5],     # 矩阵列（选项）
        }
        ans = self.mod.generate_answer(q)
        self.assertEqual(ans["type"], "matrix_single")
        rows = ans["rows"]
        self.assertEqual(set(rows.keys()), {1, 2, 3, 4})
        for r, c in rows.items():
            self.assertIn(c, [1, 2, 3, 4, 5], f"行{r}选项{c}超范围")

    def test_matrix_single_row_weights_bias(self) -> None:
        """可对某行指定 weights → 该列偏向高分。"""
        q = {
            "q": 15,
            "type": "matrix_single",
            "rows": [1, 2],
            "cols": [1, 2, 3, 4, 5],
            # 行 1 → 永远选 5；行 2 → 永远选 1
            "row_weights": {1: [0, 0, 0, 0, 1], 2: [1, 0, 0, 0, 0]},
        }
        for _ in range(50):
            ans = self.mod.generate_answer(q)
            self.assertEqual(ans["rows"][1], 5)
            self.assertEqual(ans["rows"][2], 1)


class TestWeightsFromConfigApply(unittest.TestCase):
    """v3.0：填空题候选词与矩阵行权重**真的**能从权重配置里生效。

    这两条此前是"文档承诺了但代码从不读"：``generate_answer`` 只看
    ``question["options"]`` / ``question["row_weights"]``，而 ``detect_questions``
    回来的题目永远没有这两个键 —— 于是 README 权重配置表里写的
    ``text.options`` 与 ``matrix_single.row_weights`` 一路静默走内置随机。
    GUI 表格敲的候选词/矩阵行权重同理（探测→表格→另存→运行 整条链路看着都对）。
    """

    @classmethod
    def setUpClass(cls) -> None:
        from src import answering_v2  # type: ignore
        cls.mod: Any = answering_v2

    def setUp(self) -> None:
        from src import config
        self.config = config
        self._saved = dict(config.WEIGHT_CONFIG)
        config.WEIGHT_CONFIG.clear()

    def tearDown(self) -> None:
        self.config.WEIGHT_CONFIG.clear()
        self.config.WEIGHT_CONFIG.update(self._saved)

    # -------------------------------------------------------------- 填空
    def test_text_options_come_from_weight_config(self) -> None:
        self.config.WEIGHT_CONFIG[5] = {
            "type": "text", "field": "name", "options": ["张三", "李四"],
        }
        q = {"q": 5, "type": "text", "field": "name"}
        for _ in range(30):
            self.assertIn(self.mod.generate_answer(q)["text"], {"张三", "李四"})

    def test_text_options_reach_through_an_anchor(self) -> None:
        """候选词 + 锚点：插题之后照样只从这份池子里取。"""
        self.config.WEIGHT_CONFIG[5] = {
            "type": "text", "field": "name", "options": ["张三", "李四"],
            "anchor": {"title": "请填写您的姓名", "signature": "text"},
        }
        q = {"q": 9, "type": "text", "field": "name", "title": "请填写您的姓名"}
        for _ in range(10):
            self.assertIn(self.mod.generate_answer(q)["text"], {"张三", "李四"})

    def test_no_options_keeps_builtin_generation(self) -> None:
        """对照组：没给候选词时仍走内置姓名生成，不能被改成"返回空串"。"""
        self.config.WEIGHT_CONFIG[5] = {"type": "text", "field": "name"}
        text = self.mod.generate_answer({"q": 5, "type": "text", "field": "name"})["text"]
        self.assertRegex(text, r"^[一-龥]{2,4}$")

    # -------------------------------------------------------------- 矩阵
    def test_matrix_row_weights_come_from_weight_config(self) -> None:
        self.config.WEIGHT_CONFIG[10] = {
            "type": "matrix_single", "rows": [1, 2], "cols": [1, 2, 3],
            "row_weights": {1: [1, 0, 0], 2: [0, 0, 1]},
        }
        q = {"q": 10, "type": "matrix_single", "rows": [1, 2], "cols": [1, 2, 3]}
        for _ in range(20):
            rows = self.mod.generate_answer(q)["rows"]
            self.assertEqual(rows[1], 1)
            self.assertEqual(rows[2], 3)

    def test_matrix_row_weights_with_string_keys(self) -> None:
        """GUI 权重表产出的是 ``"1": [...]`` 字符串键 —— 必须照样命中。

        行号键不统一类型的话 ``row_weights.get(1)`` 永远查不到，
        而"配了没生效"在这里同样一声不响。
        """
        self.config.WEIGHT_CONFIG[10] = {
            "type": "matrix_single", "rows": [1, 2], "cols": [1, 2, 3],
            "row_weights": {"1": [1, 0, 0], "2": [0, 0, 1]},
        }
        q = {"q": 10, "type": "matrix_single", "rows": [1, 2], "cols": [1, 2, 3]}
        rows = self.mod.generate_answer(q)["rows"]
        self.assertEqual((rows[1], rows[2]), (1, 3))

    def test_question_level_row_weights_still_win(self) -> None:
        """题目自带的 row_weights 优先于全局配置（v2.0 既有行为不变）。"""
        self.config.WEIGHT_CONFIG[10] = {
            "type": "matrix_single", "row_weights": {1: [1, 0, 0]},
        }
        q = {"q": 10, "type": "matrix_single", "rows": [1], "cols": [1, 2, 3],
             "row_weights": {1: [0, 0, 1]}}
        self.assertEqual(self.mod.generate_answer(q)["rows"][1], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
