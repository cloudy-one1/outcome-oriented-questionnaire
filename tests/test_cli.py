"""CLI 模块测试：命令行参数解析（argparse）。

验证：
  - 无参数时使用 config 默认值
  - --url / -u 可覆盖问卷 URL
  - --count / -n 可覆盖提交份数
  - 非法参数（负份数、非数字）会报错
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import cli
from src import config


class TestCliArgumentParsing(unittest.TestCase):
    """测试 src.cli 中的参数解析函数。"""

    def test_default_values_when_no_args(self) -> None:
        """V2.4：URL 无默认值（合规，不再内置真实线上问卷）→ parsed.url 为 None。"""
        parsed = cli.parse_args([])  # 模拟 python run_cli.py
        self.assertIsNone(parsed.url)
        self.assertEqual(parsed.count, config.DEFAULT_TOTAL_SUBMISSIONS)

    def test_url_long_option(self) -> None:
        """--url 指定 URL。"""
        test_url = "https://v.wjx.cn/vm/ABC123.aspx"
        parsed = cli.parse_args(["--url", test_url])
        self.assertEqual(parsed.url, test_url)
        # 未传 --count，保留默认份数
        self.assertEqual(parsed.count, config.DEFAULT_TOTAL_SUBMISSIONS)

    def test_url_short_option(self) -> None:
        """-u 短选项指定 URL。"""
        test_url = "https://v.wjx.cn/vm/XYZ789.aspx"
        parsed = cli.parse_args(["-u", test_url])
        self.assertEqual(parsed.url, test_url)

    def test_count_long_option(self) -> None:
        """--count 指定提交份数。"""
        parsed = cli.parse_args(["--count", "99"])
        self.assertEqual(parsed.count, 99)
        self.assertIsNone(parsed.url)

    def test_count_short_option(self) -> None:
        """-n 短选项指定提交份数。"""
        parsed = cli.parse_args(["-n", "233"])
        self.assertEqual(parsed.count, 233)

    def test_combined_url_and_count(self) -> None:
        """同时传 -u 和 -n → 都生效。"""
        parsed = cli.parse_args([
            "-u", "https://v.wjx.cn/vm/combined.aspx",
            "-n", "50",
        ])
        self.assertEqual(parsed.url, "https://v.wjx.cn/vm/combined.aspx")
        self.assertEqual(parsed.count, 50)

    def test_count_must_be_positive(self) -> None:
        """提交份数必须 >= 1，否则 SystemExit。"""
        with self.assertRaises(SystemExit):
            cli.parse_args(["-n", "0"])
        with self.assertRaises(SystemExit):
            cli.parse_args(["-n", "-5"])

    def test_count_not_integer(self) -> None:
        """份数不是整数 → SystemExit。"""
        with self.assertRaises(SystemExit):
            cli.parse_args(["-n", "abc"])

    # ==================================================================
    #  浏览器 / UC 模式参数
    # ==================================================================

    def test_default_browser(self) -> None:
        """不传 --browser → 使用 config.DEFAULT_BROWSER（edge）。"""
        parsed = cli.parse_args([])
        self.assertEqual(parsed.browser, config.DEFAULT_BROWSER)
        self.assertIn(parsed.browser, ("edge", "chrome"))
        # --uc 默认关闭
        self.assertEqual(parsed.use_uc, config.DEFAULT_USE_UC)

    def test_browser_edge_long(self) -> None:
        """--browser edge。"""
        parsed = cli.parse_args(["--browser", "edge"])
        self.assertEqual(parsed.browser, "edge")

    def test_browser_chrome_short(self) -> None:
        """-b chrome。"""
        parsed = cli.parse_args(["-b", "chrome"])
        self.assertEqual(parsed.browser, "chrome")

    def test_browser_case_insensitive(self) -> None:
        """浏览器名称大小写不敏感。"""
        for raw in ("Edge", "CHROME", "Edge", "ChRoMe"):
            parsed = cli.parse_args(["-b", raw])
            self.assertEqual(parsed.browser, raw.lower())

    def test_browser_invalid_rejected(self) -> None:
        """不支持的浏览器类型 → SystemExit。"""
        with self.assertRaises(SystemExit):
            cli.parse_args(["-b", "firefox"])
        with self.assertRaises(SystemExit):
            cli.parse_args(["--browser", "safari"])

    def test_use_uc_flag(self) -> None:
        """--uc 标志 → use_uc=True。"""
        parsed = cli.parse_args(["--uc"])
        self.assertTrue(parsed.use_uc)

    def test_use_uc_combined_with_chrome(self) -> None:
        """-b chrome --uc → 都生效。"""
        parsed = cli.parse_args(["-b", "chrome", "--uc"])
        self.assertEqual(parsed.browser, "chrome")
        self.assertTrue(parsed.use_uc)

    def test_full_combination(self) -> None:
        """同时传 -u / -n / -b / --uc → 全部生效。"""
        parsed = cli.parse_args([
            "-u", "https://v.wjx.cn/vm/full.aspx",
            "-n", "77",
            "-b", "chrome",
            "--uc",
        ])
        self.assertEqual(parsed.url, "https://v.wjx.cn/vm/full.aspx")
        self.assertEqual(parsed.count, 77)
        self.assertEqual(parsed.browser, "chrome")
        self.assertTrue(parsed.use_uc)

    # ==================================================================
    #  V2.2 审查整改新增参数：--no-record-text / --target-success / --max-attempts
    # ==================================================================

    def test_no_record_text_default_off(self) -> None:
        """不传 --no-record-text → no_record_text 默认 False（向后兼容）。"""
        parsed = cli.parse_args([])
        self.assertFalse(parsed.no_record_text)

    def test_no_record_text_flag(self) -> None:
        """--no-record-text → no_record_text=True。"""
        parsed = cli.parse_args(["--no-record-text"])
        self.assertTrue(parsed.no_record_text)

    def test_target_success_default_off(self) -> None:
        """不传 --target-success → target_success 默认 False（旧行为：总尝试次数）。"""
        parsed = cli.parse_args([])
        self.assertFalse(parsed.target_success)

    def test_target_success_flag(self) -> None:
        """--target-success → target_success=True。"""
        parsed = cli.parse_args(["--target-success"])
        self.assertTrue(parsed.target_success)

    def test_max_attempts_default_none(self) -> None:
        """不传 --max-attempts → max_attempts=None（run_batch 内部默认 count*2）。"""
        parsed = cli.parse_args([])
        self.assertIsNone(parsed.max_attempts)

    def test_max_attempts_value(self) -> None:
        """--max-attempts 30 → max_attempts=30。"""
        parsed = cli.parse_args(["--max-attempts", "30"])
        self.assertEqual(parsed.max_attempts, 30)

    def test_max_attempts_must_be_positive(self) -> None:
        """--max-attempts 必须 >= 1，否则 SystemExit（复用 _positive_int 校验）。"""
        with self.assertRaises(SystemExit):
            cli.parse_args(["--max-attempts", "0"])
        with self.assertRaises(SystemExit):
            cli.parse_args(["--max-attempts", "-3"])
        with self.assertRaises(SystemExit):
            cli.parse_args(["--max-attempts", "abc"])

    def test_v22_flags_combined(self) -> None:
        """同时传 --no-record-text + --target-success + --max-attempts → 全部生效。"""
        parsed = cli.parse_args([
            "-n", "10",
            "--no-record-text",
            "--target-success",
            "--max-attempts", "25",
        ])
        self.assertTrue(parsed.no_record_text)
        self.assertTrue(parsed.target_success)
        self.assertEqual(parsed.max_attempts, 25)
        self.assertEqual(parsed.count, 10)

    # ==================================================================
    #  V2.4 新增参数：--resume / --log-file
    # ==================================================================

    def test_resume_default_off(self) -> None:
        """不传 --resume → 默认 False（全新批次）。"""
        parsed = cli.parse_args([])
        self.assertFalse(parsed.resume)

    def test_resume_flag(self) -> None:
        """--resume → resume=True。"""
        parsed = cli.parse_args(["--resume"])
        self.assertTrue(parsed.resume)

    def test_log_file_default_none(self) -> None:
        """不传 --log-file → 默认 None（不落盘）。"""
        parsed = cli.parse_args([])
        self.assertIsNone(parsed.log_file)

    def test_log_file_value(self) -> None:
        """--log-file PATH → 生效。"""
        parsed = cli.parse_args(["--log-file", "logs/run.log"])
        self.assertEqual(parsed.log_file, "logs/run.log")


if __name__ == "__main__":
    unittest.main()
