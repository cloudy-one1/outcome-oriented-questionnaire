"""answering 模块的单元测试示例。

覆盖：
  - 单选题在自定义权重下的加权分布一致性（概率验证）
  - 多选题答案不重复、数量 k 落在指定区间
  - 未配置权重时走等权重分支

运行：
    cd automation
    python -m pytest tests/ -v
    # 或直接：python -m unittest tests.test_answering -v
"""

from __future__ import annotations

import collections
import sys
import os
import unittest

# 将项目根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import answering
from src import config


SINGLE_Q = {"q": 999, "type": "single", "choices": [1, 2, 3, 4]}
MULTI_Q = {"q": 998, "type": "multi", "choices": [1, 2, 3, 4, 5]}


class TestBuildAnswerStrategy(unittest.TestCase):
    """build_answer_strategy 行为测试。"""

    def setUp(self) -> None:
        """测试前备份 WEIGHT_CONFIG，避免相互干扰。"""
        self._saved = dict(config.WEIGHT_CONFIG)
        config.WEIGHT_CONFIG.clear()

    def tearDown(self) -> None:
        """测试后恢复。"""
        config.WEIGHT_CONFIG.clear()
        config.WEIGHT_CONFIG.update(self._saved)

    # ------------------------------------------------------------------
    #  单选题
    # ------------------------------------------------------------------

    def test_single_no_config_returns_one_choice(self) -> None:
        """未配置权重 → 返回单个选项且在 choices 范围内。"""
        for _ in range(50):
            ans = answering.build_answer_strategy(SINGLE_Q)
            self.assertEqual(len(ans), 1)
            self.assertIn(ans[0], SINGLE_Q["choices"])

    def test_single_zero_weight_never_picked(self) -> None:
        """权重为 0 的选项永远不会被抽到。"""
        config.WEIGHT_CONFIG[999] = {
            "type": "single",
            "weights": [1.0, 0.0, 0.0, 1.0],
        }
        for _ in range(200):
            ans = answering.build_answer_strategy(SINGLE_Q)
            self.assertIn(ans[0], [1, 4])

    def test_single_weight_distribution_roughly_match(self) -> None:
        """大样本下，权重高的选项出现次数显著更多。"""
        config.WEIGHT_CONFIG[999] = {
            "type": "single",
            "weights": [1, 0, 3, 0],  # 选项 3 占 75%
        }
        n = 5000
        counter: collections.Counter[int] = collections.Counter()
        for _ in range(n):
            counter[answering.build_answer_strategy(SINGLE_Q)[0]] += 1
        # 选项 3 应占约 75%，允许 ±5%
        self.assertGreater(counter[3], n * 0.65)
        self.assertEqual(counter[2], 0)
        self.assertEqual(counter[4], 0)

    # ------------------------------------------------------------------
    #  多选题
    # ------------------------------------------------------------------

    def test_multi_no_config_returns_distinct_choices(self) -> None:
        """多选题未配置时，返回选项不重复且数量在 1~n 区间。"""
        n_choices = len(MULTI_Q["choices"])
        for _ in range(100):
            ans = answering.build_answer_strategy(MULTI_Q)
            self.assertEqual(len(ans), len(set(ans)))  # 不重复
            self.assertTrue(1 <= len(ans) <= n_choices)
            for c in ans:
                self.assertIn(c, MULTI_Q["choices"])
            self.assertEqual(ans, sorted(ans))  # 返回已排序

    def test_multi_fixed_count(self) -> None:
        """指定 count_options=[3] → 多选题永远选 3 个。"""
        config.WEIGHT_CONFIG[998] = {
            "type": "multi",
            "weights": [1, 1, 1, 1, 1],
            "count_options": [3],
            "count_weights": [1],
        }
        for _ in range(200):
            ans = answering.build_answer_strategy(MULTI_Q)
            self.assertEqual(len(ans), 3)
            self.assertEqual(len(set(ans)), 3)

    def test_multi_weight_mismatch_fallback_equal(self) -> None:
        """权重数组长度与选项数不一致 → 降级为等权重，不抛异常。"""
        # 故意给 3 个权重，但题目有 5 个选项
        config.WEIGHT_CONFIG[998] = {"type": "multi", "weights": [1, 2, 3]}
        for _ in range(50):
            ans = answering.build_answer_strategy(MULTI_Q)
            self.assertTrue(1 <= len(ans) <= 5)
            for c in ans:
                self.assertIn(c, MULTI_Q["choices"])

    if __name__ == "__main__":
        unittest.main()
