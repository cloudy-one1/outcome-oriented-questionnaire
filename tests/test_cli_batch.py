"""run_batch 批处理主链路冒烟测试（V2.4 新增）。

背景：v2.3 重构后 ``run_one_submission`` 新增必传位置参数 ``lock``，
但 ``run_batch`` / GUI ``_run_loop`` 的调用点漏传 → CLI 首次提交即 TypeError。
此前 194 个测试全部在 run_batch 之外 mock，从未真实调用批处理主链路，
所以 194 全绿也测不出该回归。本文件用假驱动打通主链路：

    - ``FakeDriver.execute_script`` 按脚本内容返回（body/readyState 特判，
      其余返回 0）→ 流程在「页面/iframe 中找不到题目」处返回 SUBMIT_FAILED，
      速度极快、无需网络、无需真实浏览器
    - mock ``src.browser.create_driver``（run_batch 延迟导入，调用时读取模块属性）
    - mock ``src.utils.human_pause``（跳过轮间高斯 sleep）

覆盖：
    1. run_batch 完整跑完（lock 传参回归保护）+ history 落盘状态正确
    2. KeyboardInterrupt → status='interrupted'（可续传语义）
    3. --resume 语义：resume_run_id 复用旧 runs 行、计数绝对累计、不新建 run
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import cli
from src.history import SubmissionHistory

SURVEY_URL = "https://example.com/survey"


class FakeSwitchTo:
    def default_content(self) -> None:
        pass

    def frame(self, index) -> None:
        pass


class FakeDriver:
    """假 WebDriver：足以驱动 pipeline 走到 SUBMIT_FAILED 的最小实现。"""

    def __init__(self) -> None:
        self.current_url = SURVEY_URL
        self.quit_called = False
        self.switch_to = FakeSwitchTo()

    def get(self, url) -> None:
        pass

    def quit(self) -> None:
        self.quit_called = True

    def delete_all_cookies(self) -> None:
        pass

    def execute_script(self, script, *args, **kwargs):
        # 注意：必须精确匹配 pipeline 的 body 存在性探测脚本——
        # 验证码检测 JS 里同样含 "document.body" 字样，宽松匹配会被误判为
        # "验证码出现"而进入 120s 人工等待（等于测试挂起）
        if "document.body != null" in script:
            return True          # body 存在性探测 → 立即通过
        if "readyState" in script:
            return "complete"    # ready-state 等待 → 立即通过
        return 0                 # 题目/iframe/验证码探测 → falsy


def _fake_driver_factory(browser="edge", **kwargs) -> FakeDriver:
    return FakeDriver()


class TestRunBatchSmoke(unittest.TestCase):
    """批处理主链路冒烟（防 lock 断链类回归）。"""

    def test_run_batch_completes_and_records_history(self) -> None:
        """run_batch 真实调用 run_one_submission（含 lock 必传参数）并完整落盘。"""
        with SubmissionHistory(":memory:") as db:
            with mock.patch("src.browser.create_driver",
                            side_effect=_fake_driver_factory), \
                 mock.patch("src.utils.human_pause", return_value=0.0):
                success, fail = cli.run_batch(
                    SURVEY_URL,
                    2,
                    history_db=db,
                    weight_config={1: {"type": "single", "weights": [1, 1]}},
                )
            row = db._query_one("SELECT * FROM runs")
        # 假驱动找不到题目 → 全部 FAIL；重点是链路不再抛 TypeError（V2.3 回归）
        self.assertEqual((success, fail), (0, 2))
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "finished")
        self.assertEqual(row["success_count"], 0)
        self.assertEqual(row["fail_count"], 2)

    def test_keyboard_interrupt_marks_interrupted(self) -> None:
        """Ctrl+C → status='interrupted'（find_resumable_run 可恢复）。"""
        with SubmissionHistory(":memory:") as db:
            with mock.patch("src.browser.create_driver",
                            side_effect=_fake_driver_factory), \
                 mock.patch("src.utils.human_pause", return_value=0.0), \
                 mock.patch("src.pipeline.run_one_submission",
                            side_effect=KeyboardInterrupt):
                success, fail = cli.run_batch(SURVEY_URL, 3, history_db=db)
            row = db._query_one("SELECT * FROM runs")
        self.assertEqual((success, fail), (0, 0))
        self.assertEqual(row["status"], "interrupted")

    def test_resume_reuses_run_and_accumulates_counts(self) -> None:
        """续传：复用旧 runs 行（不新建）、失败计数绝对累计、结束状态 finished。"""
        with SubmissionHistory(":memory:") as db:
            rid = db.start_run(SURVEY_URL, 5, "edge", False)
            db.finish_run(rid, success_count=3, fail_count=1,
                          total_elapsed_seconds=1.0, status="interrupted")
            with mock.patch("src.browser.create_driver",
                            side_effect=_fake_driver_factory), \
                 mock.patch("src.utils.human_pause", return_value=0.0):
                success, fail = cli.run_batch(
                    SURVEY_URL,
                    5,
                    history_db=db,
                    resume_run_id=rid,
                    resume_done=3,
                    resume_fail=1,
                )
            rows = db._query("SELECT * FROM runs ORDER BY id")
            row = rows[0]
        # attempts_cap = 5 - 3 = 2，两轮都失败 → 失败 1(旧) + 2(新) = 3
        self.assertEqual((success, fail), (3, 3))
        self.assertEqual(len(rows), 1, "续传不得新建 runs 行")
        self.assertEqual(row["id"], rid)
        self.assertEqual(row["status"], "finished")
        self.assertEqual(row["success_count"], 3)
        self.assertEqual(row["fail_count"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
