"""v2.8 审查 P2-1 整改：出厂默认权重必须为空。

为什么单独开一个文件、且用子进程断言：
  ``src.config.WEIGHT_CONFIG`` 是全局可变 dict，其它测试套件会在用例里
  注入 / 清空它（``tests/test_answering.py`` 的 setUp 就无条件 clear）。
  于是"出厂那一刻它到底是什么"在本进程内是不可知的 —— 只能新起解释器，
  在干净 import 之后读它。这正是 v2.7 之前 493 个测试全绿却漏掉该缺陷的
  原因：所有等权重分支的测试都跑在一个人造的空配置上。

覆盖三件事：
  1. 出厂 ``WEIGHT_CONFIG`` 为空 dict（不传 --config 时不会套用别人的分布）；
  2. 空配置下 ``build_answer_strategy`` 真的走等权（README「方式一」的承诺）；
  3. ``examples/weight_config.example.json`` 能被 loader 读、能通过校验
     —— 迁移出去的示例若漂成非法结构，README 里的引用就变成假链接。
"""

from __future__ import annotations

import collections
import json
import os
import random
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import answering
from src.config_io import load_weight_config, validate_weight_config

EXAMPLE_PATH = os.path.join(ROOT, "examples", "weight_config.example.json")


def _shipped_weight_config_keys() -> list[int]:
    """在新解释器里读出厂键，避开本进程内其它用例对全局 dict 的改写。"""
    out = subprocess.run(
        [sys.executable, "-c",
         "import json, src.config as c; print(json.dumps(sorted(c.WEIGHT_CONFIG)))"],
        cwd=ROOT, capture_output=True, text=True, timeout=60, check=True,
    )
    return list(json.loads(out.stdout.strip()))


class TestShippedDefaults(unittest.TestCase):
    def setUp(self) -> None:
        # 本文件的用例必须在**未被改写的**全局配置上跑，先断言再测行为。
        self._saved = dict(answering.WEIGHT_CONFIG)

    def tearDown(self) -> None:
        answering.WEIGHT_CONFIG.clear()
        answering.WEIGHT_CONFIG.update(self._saved)

    # ------------------------------------------------------------------
    #  1. 出厂默认为空
    # ------------------------------------------------------------------
    def test_shipped_weight_config_is_empty(self) -> None:
        self.assertEqual(
            _shipped_weight_config_keys(), [],
            "src/config.py 的出厂 WEIGHT_CONFIG 必须为空：CLI 不传 --config 时"
            "不会调用 apply_weight_config(replace=True)，任何残留条目都会静默"
            "套到用户自己的问卷上（审查 P2-1）",
        )

    def test_no_legacy_placeholder_comment_in_config(self) -> None:
        """代码生成工具的占位注释不该入库（P2-1 附带项）。"""
        with open(
            os.path.join(ROOT, "src", "config.py"), encoding="utf-8"
        ) as f:
            self.assertNotIn("existing config entries remain unchanged", f.read())

    # ------------------------------------------------------------------
    #  2. 空配置 → 等权（README「方式一」的承诺）
    # ------------------------------------------------------------------
    def test_single_choice_without_config_is_uniform(self) -> None:
        answering.WEIGHT_CONFIG.clear()
        random.seed(20260921)
        q = {"q": 1, "type": "single", "choices": [1, 2, 3, 4]}
        n = 20000
        picked = collections.Counter(
            answering.build_answer_strategy(q)[0] for _ in range(n)
        )
        self.assertEqual(set(picked), {1, 2, 3, 4})
        for opt in (1, 2, 3, 4):
            share = picked[opt] / n
            # 期望 0.25，单次抽样标准差约 0.0031，这里放到 ±0.01（约 3σ）
            self.assertAlmostEqual(share, 0.25, delta=0.01, msg=f"选项 {opt} 偏离等权")

    def test_first_22_questions_do_not_inherit_shipped_skew(self) -> None:
        """回归哨兵：Q1–Q22 正是残留权重表的键域，必须与"未配置"同分布。

        旧实现里 Q5 的实测分布是 0.01/0.05/0.10/0.64/0.20 —— 单看某一道题
        很容易当成"随机数本来就这样"，所以这里按题号逐个卡住。
        """
        answering.WEIGHT_CONFIG.clear()
        random.seed(20260922)
        for qi in (1, 5, 14, 22):
            n_opts = {1: 4, 5: 5, 14: 5, 22: 5}[qi]
            q = {
                "q": qi,
                "type": "single" if qi in (1, 5) else "multi",
                "choices": list(range(1, n_opts + 1)),
            }
            n = 12000
            hits = collections.Counter()
            for _ in range(n):
                hits.update(answering.build_answer_strategy(q))
            total = sum(hits.values())
            for opt in range(1, n_opts + 1):
                self.assertAlmostEqual(
                    hits[opt] / total, 1 / n_opts, delta=0.02,
                    msg=f"Q{qi} 选项 {opt} 没有拿到等权份额",
                )

    # ------------------------------------------------------------------
    #  3. 迁出的示例文件仍然可用
    # ------------------------------------------------------------------
    def test_example_config_loads_and_validates(self) -> None:
        self.assertTrue(os.path.isfile(EXAMPLE_PATH), "示例配置文件缺失")
        cfg, meta = load_weight_config(EXAMPLE_PATH)
        self.assertEqual(validate_weight_config(cfg), [], "示例配置结构非法")
        self.assertEqual(sorted(cfg), list(range(1, 23)))
        self.assertEqual(cfg[1]["weights"], [0.24, 0.4, 0.26, 0.1])
        self.assertTrue(meta.get("name"), "示例缺 meta.name")

    def test_example_is_not_the_default(self) -> None:
        """示例里的偏斜值绝不能反过来变成出厂默认（否则迁移等于没做）。"""
        with open(EXAMPLE_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        shipped = _shipped_weight_config_keys()
        for qi in raw["config"]:
            self.assertNotIn(int(qi), shipped)


if __name__ == "__main__":
    unittest.main()
