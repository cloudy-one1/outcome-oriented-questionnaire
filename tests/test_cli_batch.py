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
from src.exceptions import SubmissionAborted
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

    def test_run_batch_clears_stale_distribution_statistics(self) -> None:
        """每份问卷各自清零分布统计，且失败的一份绝不进统计（v3.2 在线纠正）。

        假驱动找不到题目 → 这一份判 FAIL：`run_batch` 开头的 start_run 要把上一份
        问卷留下的账清掉，`run_one_submission` 的失败分支要把这一份的缓冲丢掉。
        两条都失守的症状是同一种：纠正对着一个不存在的目标收敛。
        """
        from src import distribution

        with SubmissionHistory(":memory:") as db:
            distribution.enable(True)
            try:
                distribution.buffer_answer(1, [0], 2)
                distribution.commit_buffer()
                self.assertTrue(distribution.drift_report(), "先造一份脏统计")
                with mock.patch("src.browser.create_driver",
                                side_effect=_fake_driver_factory), \
                     mock.patch("src.utils.human_pause", return_value=0.0):
                    cli.run_batch(SURVEY_URL, 1, history_db=db)
                self.assertEqual(distribution.drift_report(), {}, "批次开始时没清零")
                self.assertEqual(distribution._buffer, {}, "失败的一份的缓冲没丢掉")
            finally:
                distribution.enable(False)
                distribution.start_run()

    def test_run_batch_resets_the_alpha_plan_for_the_next_survey(self) -> None:
        """换一份问卷时清掉信度计划矩阵（v3.2 ``--url-file`` 队列接线）。

        ``ensure_plan`` 的 built 门是为**一份问卷的分页**设的，跨问卷不清的症状是
        第二份卷沿用上一份的配额（题号撞上时）或静默不建（撞不上时）—— 两种都没有
        一句提示。这里只认"跑完一批之后旧计划答不了新题"这个事实。
        """
        from src import config as cfg_mod
        from src import plan

        saved = dict(cfg_mod.WEIGHT_CONFIG)
        cfg_mod.WEIGHT_CONFIG.clear()
        cfg_mod.WEIGHT_CONFIG.update(
            {q: {"dimension": "d"} for q in (1, 2, 3)}
        )
        plan.configure(0.8, 40)
        try:
            qs = [{"q": q, "type": "single", "choices": [1, 2, 3], "title": f"第{q}题"}
                  for q in (1, 2, 3)]
            assert any("计划已建" in n for n in plan.ensure_plan(qs))
            plan.begin_submission(1)
            assert plan.forced_choice(1) is not None, "先造一份接上的计划"

            with SubmissionHistory(":memory:") as db:
                with mock.patch("src.browser.create_driver",
                                side_effect=_fake_driver_factory), \
                     mock.patch("src.utils.human_pause", return_value=0.0):
                    cli.run_batch(SURVEY_URL, 1, history_db=db)

            plan.begin_submission(1)
            assert plan.forced_choice(1) is None, "run_batch 没清掉上一份问卷的计划"
        finally:
            plan.end_session()
            cfg_mod.WEIGHT_CONFIG.clear()
            cfg_mod.WEIGHT_CONFIG.update(saved)

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
        # 审查 P3-4 口径：上限是**尝试次数**，上次已消耗 done+fail = 3+1 = 4 次
        # → attempts_cap = 5 - 4 = 1，一轮失败 → 失败 1(旧) + 1(新) = 2
        self.assertEqual((success, fail), (3, 2))
        self.assertEqual(len(rows), 1, "续传不得新建 runs 行")
        self.assertEqual(row["id"], rid)
        self.assertEqual(row["status"], "finished")
        self.assertEqual(row["success_count"], 3)
        self.assertEqual(row["fail_count"], 2)

    def test_resume_cap_subtracts_consumed_attempts(self) -> None:
        """target_success 模式的续传上限同样按「已尝试」而非「已成功」扣减。

        旧实现只减 resume_done，于是续传后总尝试数会凭空多出 resume_fail 次：
        上限 6、上次成功 2 失败 1 → 旧算法给 4 次（合计 7 次尝试），新算法给 3 次。
        """
        with mock.patch("src.browser.create_driver",
                        side_effect=_fake_driver_factory), \
             mock.patch("src.utils.human_pause", return_value=0.0):
            success, fail = cli.run_batch(
                SURVEY_URL,
                5,
                target_success=True,
                max_attempts=6,
                resume_done=2,
                resume_fail=1,
            )
        self.assertEqual((success, fail), (0, 3), "本轮应只再尝试 6-(2+1)=3 次")


    def test_webdriver_exception_in_one_round_does_not_abort_batch(self) -> None:
        """V2.5 回归：单轮 WebDriver 异常只计该份失败，剩余份数继续跑。

        此前只接 InvalidSessionIdException，一次 TimeoutException 就冒泡到
        兜底 except → mark_crashed → 整批夭折且批次记 failed（不可续传），
        前面已成功的份数被静默丢弃。
        """
        from selenium.common.exceptions import TimeoutException

        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutException("page load timeout")
            return "success"

        with SubmissionHistory(":memory:") as db:
            with mock.patch("src.browser.create_driver",
                            side_effect=_fake_driver_factory), \
                 mock.patch("src.utils.human_pause", return_value=0.0), \
                 mock.patch("src.pipeline.run_one_submission", side_effect=flaky):
                success, fail = cli.run_batch(SURVEY_URL, 3, history_db=db)
            row = db._query_one("SELECT * FROM runs")

        self.assertEqual(calls["n"], 3, "第 1 轮异常后必须继续跑第 2、3 轮")
        self.assertEqual((success, fail), (2, 1))
        self.assertEqual(
            row["status"], "finished",
            "单轮异常不能被当成整批崩溃（failed 会让 find_resumable_run 拒绝续传）",
        )

    def test_dead_window_rebuilds_driver_and_continues(self) -> None:
        """NoSuchWindowException（用户手关窗口）应重建浏览器后继续，而非终止批次。"""
        from selenium.common.exceptions import NoSuchWindowException

        created = {"n": 0}
        calls = {"n": 0}

        def factory(browser="edge", **kwargs):
            created["n"] += 1
            return FakeDriver()

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise NoSuchWindowException("window closed")
            return "success"

        with mock.patch("src.browser.create_driver", side_effect=factory), \
             mock.patch("src.utils.human_pause", return_value=0.0), \
             mock.patch("src.pipeline.run_one_submission", side_effect=flaky):
            success, fail = cli.run_batch(SURVEY_URL, 2)

        self.assertEqual(created["n"], 2, "关窗后应重建一次浏览器")
        self.assertEqual((success, fail), (1, 1))


class TestRunBatchStopWithinRound(unittest.TestCase):
    """v3.0：run_batch 如何接住「轮内停止」（此前 stop_check 只在轮间生效）。"""

    def test_abort_marks_interrupted_without_counting_the_round(self) -> None:
        """轮内停止 → 该份不提交、不计成功也不计失败，批次 closed 为 interrupted。

        计失败会把成功率人为压低；记成 failed（崩溃态）则 find_resumable_run
        拒绝续传，前面已成功的份数被静默丢弃 —— 正是 v2.5 为 KeyboardInterrupt
        修过的那类后果，只是这次发生在停止路径上。
        """
        calls = {"n": 0}

        def _abort(*_a, **_kw):
            calls["n"] += 1
            raise SubmissionAborted("逐题作答期间收到停止请求", question=3)

        rounds: list[cli.RoundOutcome] = []
        with SubmissionHistory(":memory:") as db:
            with mock.patch("src.browser.create_driver", side_effect=_fake_driver_factory),                  mock.patch("src.utils.human_pause", return_value=0.0),                  mock.patch("src.pipeline.run_one_submission", side_effect=_abort):
                success, fail = cli.run_batch(
                    SURVEY_URL, 3, history_db=db, on_round=rounds.append,
                )
            row = db._query_one("SELECT * FROM runs")

        self.assertEqual((success, fail), (0, 0))
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual([r.outcome for r in rounds], ["aborted"])
        self.assertEqual(calls["n"], 1, "停止后不能再开下一份")

    def test_stop_check_is_forwarded_into_the_round(self) -> None:
        """接线契约：轮内停止依赖 stop_check 真的传进 run_one_submission。

        v2.4 的 --resume、v2.3 的 lock 都是同一个形状的缺陷 —— 算好了却没传。
        """
        captured: dict = {}

        def _spy(driver, url, lock, **kw):
            captured.update(kw)
            return "failed"

        def chk() -> bool:
            return False

        with mock.patch("src.browser.create_driver", side_effect=_fake_driver_factory),              mock.patch("src.utils.human_pause", return_value=0.0),              mock.patch("src.pipeline.run_one_submission", side_effect=_spy):
            cli.run_batch(SURVEY_URL, 1, stop_check=chk)

        self.assertIs(captured["stop_check"], chk)


if __name__ == "__main__":
    unittest.main(verbosity=2)
