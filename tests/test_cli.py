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
        """不传任何参数 → 返回 config 中的默认 URL 和默认份数。"""
        parsed = cli.parse_args([])  # 模拟 python run_cli.py
        self.assertEqual(parsed.url, config.DEFAULT_SURVEY_URL)
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
        self.assertEqual(parsed.url, config.DEFAULT_SURVEY_URL)

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


if __name__ == "__main__":
    unittest.main()
