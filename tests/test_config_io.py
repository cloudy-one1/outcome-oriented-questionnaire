"""v2.0 — 权重配置 JSON 导入/导出 TDD RED 测试。

模块名: src.config_io
功能:
    save_weight_config(path, cfg, meta)   → 保存 JSON 配置文件（含元信息）
    load_weight_config(path)              → 读取并校验 JSON，返回 (cfg_dict, meta_dict)
    apply_weight_config(cfg_dict)         → 将配置写入 src.config.WEIGHT_CONFIG（热更新）
    validate_weight_config(cfg, questions_schema=None) → 校验配置结构合法性
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


SAMPLE_CFG: dict = {
    1: {"type": "single", "weights": [0.2, 0.5, 0.3]},
    7: {
        "type": "multi",
        "weights": [0.1, 0.2, 0.3, 0.2, 0.2],
        "count_options": [2, 3],
        "count_weights": [0.6, 0.4],
    },
    10: {"type": "scale", "scale": 5, "weights": [0, 0, 0, 0, 1]},
    12: {"type": "text", "field": "name"},
    15: {"type": "dropdown", "weights": [1, 0, 0, 0]},
    20: {
        "type": "matrix_single",
        "row_weights": {
            1: [1, 2, 3, 2, 1],
            2: [0, 0, 1, 0, 0],
        },
    },
}

SAMPLE_META: dict = {
    "name": "学生调研预设 v1",
    "description": "适用于大学生问卷，高分成倾向",
    "author": "张三",
    "survey_url": "https://v.wjx.cn/vm/test.aspx",
    "schema_version": "2.0",
}


class TestConfigIO(unittest.TestCase):
    """config_io 模块 TDD 测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        from src import config_io  # type: ignore
        cls.mod: Any = config_io

    def setUp(self) -> None:
        self._tmpfd, self._tmppath = tempfile.mkstemp(suffix=".json")
        os.close(self._tmpfd)
        os.unlink(self._tmppath)  # 让 save 创建文件

    def tearDown(self) -> None:
        try:
            if os.path.exists(self._tmppath):
                os.unlink(self._tmppath)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # save → load 往返一致
    # ------------------------------------------------------------------
    def test_save_and_load_roundtrip(self) -> None:
        """save → load 后，cfg 和 meta 必须与原始数据一致。"""
        self.mod.save_weight_config(self._tmppath, SAMPLE_CFG, SAMPLE_META)
        self.assertTrue(os.path.exists(self._tmppath))

        cfg, meta = self.mod.load_weight_config(self._tmppath)
        # int 键 JSON 会变成字符串，load 时必须转回来
        self.assertEqual(cfg[1]["type"], "single")
        self.assertEqual(cfg[1]["weights"], [0.2, 0.5, 0.3])
        self.assertEqual(cfg[7]["count_options"], [2, 3])
        self.assertEqual(cfg[10]["weights"], [0, 0, 0, 0, 1])
        self.assertEqual(cfg[12]["field"], "name")
        self.assertEqual(cfg[15]["weights"], [1, 0, 0, 0])
        self.assertEqual(cfg[20]["row_weights"][2], [0, 0, 1, 0, 0])

        # meta 字段
        for k, v in SAMPLE_META.items():
            self.assertEqual(meta.get(k), v, f"meta[{k}] 不匹配")

    def test_save_writes_valid_json(self) -> None:
        """保存的文件必须是合法 JSON（可用 json 模块打开）。"""
        self.mod.save_weight_config(self._tmppath, SAMPLE_CFG, SAMPLE_META)
        with open(self._tmppath, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("config", data)
        self.assertIn("meta", data)
        self.assertIn("saved_at", data)  # 自动加的时间戳

    def test_load_without_meta_returns_empty_meta(self) -> None:
        """旧版本 JSON 无 meta 字段时，meta 返回 {}，不抛异常。"""
        raw = {"config": {"1": {"type": "single", "weights": [1, 0]}}}
        with open(self._tmppath, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        cfg, meta = self.mod.load_weight_config(self._tmppath)
        self.assertEqual(cfg[1]["weights"], [1, 0])
        self.assertEqual(meta, {})

    # ------------------------------------------------------------------
    # validate_weight_config 结构校验
    # ------------------------------------------------------------------
    def test_validate_accepts_valid_cfg(self) -> None:
        """合法配置 → validate 返回 True 或空列表错误。"""
        errs = self.mod.validate_weight_config(SAMPLE_CFG)
        self.assertEqual(errs, [], f"不应有错误，实际: {errs}")

    def test_validate_rejects_bad_key(self) -> None:
        """题号不是正整数 → 报错。"""
        bad = {"abc": {"type": "single", "weights": [1]}}
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("题号" in e or "key" in e.lower() for e in errs), errs)

    def test_validate_rejects_missing_type(self) -> None:
        """缺 type 字段 → 报错。"""
        bad = {1: {"weights": [0.5, 0.5]}}  # 无 type
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("type" in e.lower() for e in errs), errs)

    def test_validate_rejects_weights_not_list(self) -> None:
        """weights 不是列表 → 报错。"""
        bad = {1: {"type": "single", "weights": "1,2,3"}}
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("weights" in e.lower() for e in errs), errs)

    def test_validate_unknown_type_returns_warning(self) -> None:
        """未知题型 → 至少给出 warning。"""
        bad = {1: {"type": "mystery"}}
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(len(errs) >= 1, f"未知题型应报错，实际错误: {errs}")

    # ------------------------------------------------------------------
    # apply_weight_config 热更新到 WEIGHT_CONFIG
    # ------------------------------------------------------------------
    def test_apply_writes_to_weight_config(self) -> None:
        """apply 后 config.WEIGHT_CONFIG 中出现 Q1 新配置。"""
        from src import config

        saved_backup = dict(config.WEIGHT_CONFIG)
        try:
            fresh_cfg = {
                1000: {"type": "single", "weights": [0.1, 0.9]},
                1001: {"type": "multi", "weights": [1, 2, 3, 4]},
            }
            self.mod.apply_weight_config(fresh_cfg)
            self.assertEqual(config.WEIGHT_CONFIG[1000]["weights"], [0.1, 0.9])
            self.assertEqual(config.WEIGHT_CONFIG[1001]["type"], "multi")
        finally:
            config.WEIGHT_CONFIG.clear()
            config.WEIGHT_CONFIG.update(saved_backup)

    def test_load_missing_file_raises_file_not_found(self) -> None:
        """加载不存在的路径 → 抛 FileNotFoundError。"""
        with self.assertRaises(FileNotFoundError):
            self.mod.load_weight_config(r"Z:\no\such\path\abc999.json")

    # ==================================================================
    #  V2.2 审查 P2-1 增强校验：权重和 > 0 / NaN / Inf / 长度匹配
    # ==================================================================
    def test_validate_rejects_all_zero_weights(self) -> None:
        """权重全 0 → 总和 <= 0 → 报错（不应让加权采样退化）。"""
        bad = {1: {"type": "single", "weights": [0, 0, 0]}}
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("总和" in e for e in errs), f"应报总和>0 错误，实际: {errs}")

    def test_validate_accepts_zero_with_positive_weight(self) -> None:
        """权重中有 0 但至少一项 > 0 → 合法（0 表示该选项永不被选中）。"""
        good = {1: {"type": "single", "weights": [0, 1, 0]}}
        errs = self.mod.validate_weight_config(good)
        self.assertEqual(errs, [], f"应合法，实际: {errs}")

    def test_validate_rejects_nan_weights(self) -> None:
        """权重含 NaN → 报错（不应让 random.choices 抛 ValueError）。"""
        bad = {1: {"type": "single", "weights": [float("nan"), 1, 1]}}
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("NaN" in e or "Inf" in e for e in errs), f"应报 NaN 错误，实际: {errs}")

    def test_validate_rejects_inf_weights(self) -> None:
        """权重含 Inf → 报错。"""
        bad = {1: {"type": "single", "weights": [float("inf"), 1, 1]}}
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("NaN" in e or "Inf" in e for e in errs), f"应报 Inf 错误，实际: {errs}")

    def test_validate_rejects_weights_choices_length_mismatch(self) -> None:
        """weights 长度与 choices 长度不匹配 → 报错（避免采样 IndexError）。"""
        bad = {1: {"type": "single", "choices": ["a", "b", "c"],
                   "weights": [0.5, 0.5]}}  # 3 选项 2 权重
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("choices 长度" in e for e in errs), f"应报长度不匹配，实际: {errs}")

    def test_validate_accepts_weights_choices_same_length(self) -> None:
        """weights 长度 == choices 长度 → 合法。"""
        good = {1: {"type": "single", "choices": ["a", "b", "c"],
                     "weights": [0.5, 0.3, 0.2]}}
        errs = self.mod.validate_weight_config(good)
        self.assertEqual(errs, [], f"应合法，实际: {errs}")

    def test_validate_rejects_count_options_count_weights_length_mismatch(self) -> None:
        """multi 题 count_options 与 count_weights 长度不一致 → 报错。"""
        bad = {1: {"type": "multi", "weights": [0.1, 0.2, 0.3, 0.4],
                    "count_options": [2, 3, 4],
                    "count_weights": [0.4, 0.6]}}  # 3 vs 2
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("count_options 长度" in e for e in errs),
                        f"应报 count_options 长度不一致，实际: {errs}")

    def test_validate_accepts_count_options_count_weights_same_length(self) -> None:
        """multi 题 count_options 与 count_weights 长度一致 + 总和 > 0 → 合法。"""
        good = {1: {"type": "multi", "weights": [0.1, 0.2, 0.3, 0.4],
                     "count_options": [2, 3, 4],
                     "count_weights": [0.2, 0.3, 0.5]}}
        errs = self.mod.validate_weight_config(good)
        self.assertEqual(errs, [], f"应合法，实际: {errs}")

    def test_validate_rejects_count_weights_all_zero(self) -> None:
        """multi 题 count_weights 全 0 → 总和 <= 0 → 报错。"""
        bad = {1: {"type": "multi", "weights": [0.1, 0.2, 0.3, 0.4],
                    "count_options": [2, 3],
                    "count_weights": [0, 0]}}
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("count_weights 总和" in e for e in errs),
                        f"应报 count_weights 总和>0，实际: {errs}")

    def test_validate_rejects_count_options_not_positive_int(self) -> None:
        """multi 题 count_options 含 0 / 负数 / 非整数 → 报错。"""
        bad = {1: {"type": "multi", "weights": [0.1, 0.2, 0.3],
                    "count_options": [0, 2],   # 0 不合法
                    "count_weights": [0.5, 0.5]}}
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("count_options" in e and "正整数" in e for e in errs),
                        f"应报 count_options 必须 >= 1，实际: {errs}")

    def test_validate_rejects_scale_weights_length_mismatch(self) -> None:
        """scale 题 weights 长度不匹配 scale 范围 → 报错。"""
        bad = {1: {"type": "scale", "scale": 5,
                   "weights": [0, 0, 1]}}  # 5 分量表只给 3 权重
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("量表范围" in e for e in errs),
                        f"应报 scale 长度不匹配，实际: {errs}")

    def test_validate_accepts_scale_weights_matching_range(self) -> None:
        """scale 题 weights 长度 == scale 范围 → 合法。"""
        good = {1: {"type": "scale", "scale": 5, "weights": [0, 0, 0.1, 0.4, 0.5]}}
        errs = self.mod.validate_weight_config(good)
        self.assertEqual(errs, [], f"应合法，实际: {errs}")

    def test_validate_rejects_scale_negative_range(self) -> None:
        """scale_min > scale_max → 范围非法 → 报错。"""
        bad = {1: {"type": "scale", "scale": 3, "scale_min": 5,
                   "weights": [0, 0, 0, 0, 0, 0, 0]}}
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("范围非法" in e for e in errs),
                        f"应报范围非法，实际: {errs}")

    def test_validate_rejects_matrix_row_weights_length_mismatch(self) -> None:
        """matrix 题 row_weights 每行长度不匹配 cols → 报错。"""
        bad = {1: {"type": "matrix_single",
                   "cols": [1, 2, 3, 4, 5],
                   "row_weights": {1: [0, 1, 0],   # 3 个，应 5
                                   2: [0, 0, 0, 0, 1]}}}
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("cols 长度" in e for e in errs),
                        f"应报 row_weights 行长度不匹配 cols，实际: {errs}")

    def test_validate_accepts_matrix_row_weights_matching_cols(self) -> None:
        """matrix 题 row_weights 每行长度 == cols 长度 → 合法。"""
        good = {1: {"type": "matrix_single",
                     "cols": [1, 2, 3, 4, 5],
                     "row_weights": {1: [0, 0, 0.1, 0.4, 0.5],
                                     2: [0.1, 0.2, 0.3, 0.3, 0.1]}}}
        errs = self.mod.validate_weight_config(good)
        self.assertEqual(errs, [], f"应合法，实际: {errs}")

    def test_validate_rejects_matrix_row_weights_all_zero(self) -> None:
        """matrix 题 row_weights 某行全 0 → 总和 <= 0 → 报错。"""
        bad = {1: {"type": "matrix_single",
                   "cols": [1, 2, 3],
                   "row_weights": {1: [0, 1, 0],
                                   2: [0, 0, 0]}}}  # 全 0
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("总和" in e and "2" in e for e in errs),
                        f"应报 row_weights 行 2 总和>0，实际: {errs}")

    def test_validate_rejects_matrix_row_weights_nan(self) -> None:
        """matrix 题 row_weights 含 NaN → 报错。"""
        bad = {1: {"type": "matrix_single",
                   "cols": [1, 2, 3],
                   "row_weights": {1: [0, float("nan"), 1]}}}
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("NaN" in e or "Inf" in e for e in errs),
                        f"应报 matrix row_weights NaN，实际: {errs}")

    def test_validate_rejects_matrix_row_weights_not_list(self) -> None:
        """matrix 题 row_weights 某行不是 list → 报错。"""
        bad = {1: {"type": "matrix_single",
                   "cols": [1, 2, 3],
                   "row_weights": {1: "not_a_list"}}}
        errs = self.mod.validate_weight_config(bad)
        self.assertTrue(any("必须是 list/tuple" in e for e in errs),
                        f"应报 row_weights 行值不是 list，实际: {errs}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
