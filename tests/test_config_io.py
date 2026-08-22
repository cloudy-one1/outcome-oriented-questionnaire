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


if __name__ == "__main__":
    unittest.main(verbosity=2)
