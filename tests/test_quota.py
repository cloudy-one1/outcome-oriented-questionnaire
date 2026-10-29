"""配额调度（v4.2 ``--quota``，设计稿 docs/design/DESIGN_quota_scheduling.md A1+C1）。

核心承诺：整批**明确成功**的 N 份在单选/下拉上**恰好**按配置比例兑现 ——
不是概率收敛，是记账精确。failed / UNKNOWN 不占配额（C1），续传按历史库
逐份成败重建账本（与 resume_progress 同一单一真相）。
"""

from __future__ import annotations

import os
import random
import sys
import unittest
from collections import Counter
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import quota  # noqa: E402
from src.history import SubmissionHistory  # noqa: E402

_URL = "https://www.wjx.cn/vm/offline-only.aspx"


def _reset(total: int = 0) -> None:
    quota.enable(False)
    quota.start_run(total)


class TestAlloc(unittest.TestCase):
    """最大余数法：整数配额总和必须恒等于 N。"""

    def setUp(self) -> None:
        _reset()

    def tearDown(self) -> None:
        _reset()

    def test_exact_split_when_divisible(self) -> None:
        self.assertEqual(quota._alloc(100, [10, 30, 50, 10]), [10, 30, 50, 10])
        self.assertEqual(quota._alloc(20, [3, 1]), [15, 5])

    def test_largest_remainder_when_indivisible(self) -> None:
        # 100/3 = 33.33… ×3：余数全相等 → 稳定序给最前面的选项补 1
        self.assertEqual(quota._alloc(100, [1, 1, 1]), [34, 33, 33])
        # 7/3 = 2.33…：[2.33, 2.33, 2.33] → floors [2,2,2] + 1 给第一个
        self.assertEqual(quota._alloc(7, [1, 1, 1]), [3, 2, 2])

    def test_zero_total_weight_defensively_allocates_nothing(self) -> None:
        self.assertEqual(quota._alloc(10, [0, 0, 0]), [0, 0, 0])


class TestEffectiveWeights(unittest.TestCase):
    """懒初始化账本 + 满额屏蔽；未开启时一个字节都不动。"""

    def setUp(self) -> None:
        _reset(10)
        quota.enable(True)

    def tearDown(self) -> None:
        _reset()

    def test_disabled_is_a_noop(self) -> None:
        _reset()
        w = [3.0, 1.0]
        self.assertIs(quota.effective_weights(1, w, 2), w)

    def test_exhausted_option_is_masked_out(self) -> None:
        quota.effective_weights(1, [1.0, 1.0, 1.0], 3)   # 初始化：[4, 3, 3]
        quota._remaining[1] = [0, 3, 3]
        out = quota.effective_weights(1, [1.0, 1.0, 1.0], 3)
        self.assertEqual(out, [0.0, 1.0, 1.0], "满额项屏蔽，未满额项保持相对频率")

    def test_all_exhausted_defensively_returns_original(self) -> None:
        """全部满额还来要权重：只可能是"成功已达标、批次收尾中"的窗口，
        原样返回（这份不会再被 commit）。"""
        quota.effective_weights(1, [1.0, 1.0], 2)        # 初始化：[5, 5]
        quota._remaining[1] = [0, 0]
        self.assertEqual(quota.effective_weights(1, [1.0, 1.0], 2), [1.0, 1.0])

    def test_relative_frequency_kept_among_unfilled(self) -> None:
        quota.effective_weights(1, [3.0, 1.0, 1.0], 3)  # 配额 [6, 2, 2]
        quota._remaining[1] = [6, 0, 0]
        # 选项 2/3 满额 → 屏蔽；未满额项之间的相对频率不变
        self.assertEqual(quota.effective_weights(1, [3.0, 1.0, 1.0], 3),
                         [3.0, 0.0, 0.0])
        quota._remaining[1] = [3, 1, 1]
        self.assertEqual(quota.effective_weights(1, [3.0, 1.0, 1.0], 3),
                         [3.0, 1.0, 1.0])


class TestBufferLifecycle(unittest.TestCase):
    """成功 commit 扣账、失败 discard 不扣 —— C1 语义的记账面。"""

    def setUp(self) -> None:
        _reset(10)
        quota.enable(True)
        quota.effective_weights(1, [1.0, 1.0], 2)      # 配额 [5, 5]

    def tearDown(self) -> None:
        _reset()

    def test_commit_consumes_and_discard_does_not(self) -> None:
        quota.buffer_answer(1, [0])
        self.assertEqual(quota.commit_buffer(), 1)
        self.assertEqual(quota._remaining[1], [4, 5])

        quota.buffer_answer(1, [1])
        self.assertEqual(quota.discard_buffer(), 1)
        self.assertEqual(quota._remaining[1], [4, 5], "失败不占配额")
        self.assertEqual(quota._remaining[1], quota._remaining[1])

    def test_multi_shaped_picks_are_ignored(self) -> None:
        quota.buffer_answer(1, [0, 1])
        self.assertEqual(quota._buffer, {}, "多选不是单选形，不入配额账")

    def test_question_without_ledger_is_ignored_at_commit(self) -> None:
        quota.buffer_answer(99, [0])                   # 量表/未配置题
        self.assertEqual(quota.commit_buffer(), 0)

    def test_overfill_is_never_negative(self) -> None:
        quota._remaining[1] = [0, 5]
        quota.buffer_answer(1, [0])
        quota.commit_buffer()
        self.assertEqual(quota._remaining[1], [0, 5], "满额项防御性不扣成负数")


class TestEndToEndExactTally(unittest.TestCase):
    """验收标准 1：N=100、注入 25% 失败 → 明确成功的分布**逐项精确**等于配额。"""

    def setUp(self) -> None:
        _reset()

    def tearDown(self) -> None:
        _reset()

    def test_exact_tally_after_failures(self) -> None:
        from src import config as cfg_module
        from src.answering_v2 import generate_answer

        random.seed(20261029)
        with mock.patch.dict(cfg_module.WEIGHT_CONFIG,
                             {1: {"type": "single", "weights": [10, 30, 50, 10]}},
                             clear=True):
            quota.enable(True)
            quota.start_run(100)
            question = {"q": 1, "type": "single", "choices": [1, 2, 3, 4]}
            tally: Counter = Counter()
            attempts = 0
            while sum(tally.values()) < 100:
                attempts += 1
                self.assertLess(attempts, 2000, "配额卡死：不变量被破坏")
                ans = generate_answer(question)
                pick = int(ans["selected"][0]) - 1
                quota.buffer_answer(1, [pick])
                if random.random() < 0.75:      # 25% 的尝试失败
                    quota.commit_buffer()
                    tally[pick] += 1
                else:
                    quota.discard_buffer()

            self.assertEqual([tally[i] for i in range(4)], [10, 30, 50, 10],
                             "总量配额必须逐项精确兑现，失败不打歪分布")
            self.assertEqual(quota._remaining[1], [0, 0, 0, 0])

    def test_invariant_remaining_equals_remaining_successes(self) -> None:
        """Σ剩余配额 = 剩余成功份数 —— 这是"永不卡死"的结构保证。"""
        from src import config as cfg_module
        from src.answering_v2 import generate_answer

        random.seed(7)
        with mock.patch.dict(cfg_module.WEIGHT_CONFIG,
                             {1: {"type": "single", "weights": [1, 2, 3]}},
                             clear=True):
            quota.enable(True)
            quota.start_run(40)
            question = {"q": 1, "type": "single", "choices": [1, 2, 3]}
            done = 0
            while done < 40:
                pick = int(generate_answer(question)["selected"][0]) - 1
                quota.buffer_answer(1, [pick])
                if random.random() < 0.6:
                    quota.commit_buffer()
                    done += 1
                else:
                    quota.discard_buffer()
                self.assertEqual(sum(quota._remaining[1]), 40 - done)


class TestResumeRebuild(unittest.TestCase):
    """验收标准 2：续传按 submissions.final_status='success' + answers 明细重建。"""

    def setUp(self) -> None:
        _reset(10)

    def tearDown(self) -> None:
        _reset()

    def test_rebuild_counts_only_successful_submissions(self) -> None:
        with SubmissionHistory(":memory:") as db:
            rid = db.start_run(_URL, 5, "edge", False)
            for sub, status in ((1, "success"), (2, "failed"), (3, "success")):
                db.record_answer(rid, sub, 1, "single", options_selected=[0])
                db.record_answer(rid, sub, 2, "multi", options_selected=[0, 1])
                db.record_submission_result(rid, sub, status)

            quota.enable(True)
            quota.start_run(10)
            self.assertEqual(quota.rebuild_consumed(db, rid), 2,
                             "只有成功的份扣账；多选明细（len≠1）不进单选形账")

            quota.effective_weights(1, [1.0, 1.0], 2)   # 初始化 [5,5] - 历史 2
            self.assertEqual(quota._remaining[1], [3, 5])

    def test_pre_consumed_beyond_quota_clamps_with_warning(self) -> None:
        """改过权重后续传：历史成功数超过当前配额 → 夹 0 并大声说明。

        构造：历史 3 份成功都选了选项 2（下标 1），续传时把权重改成 [1,0] ——
        选项 2 的配额本来就是 0，3 笔历史消耗无处可扣。
        """
        import io
        from contextlib import redirect_stdout

        with SubmissionHistory(":memory:") as db:
            rid = db.start_run(_URL, 5, "edge", False)
            for sub in (1, 2, 3):
                db.record_answer(rid, sub, 1, "single", options_selected=[1])
                db.record_submission_result(rid, sub, "success")

            quota.enable(True)
            quota.start_run(10)
            quota.rebuild_consumed(db, rid)
            buf = io.StringIO()
            with redirect_stdout(buf):
                quota.effective_weights(1, [1.0, 0.0], 2)   # 配额 [10, 0]，历史在 idx1
            self.assertEqual(quota._remaining[1], [10, 0], "夹到 0，不扣成负数")
            self.assertIn("夹到 0", buf.getvalue())


class TestProgressReport(unittest.TestCase):
    def setUp(self) -> None:
        _reset()

    def tearDown(self) -> None:
        _reset()

    def test_unfilled_quota_is_reported(self) -> None:
        quota.enable(True)
        quota.start_run(10)
        quota.effective_weights(1, [1.0, 1.0], 2)
        quota._remaining[1] = [0, 2]
        lines = quota.progress_report()
        self.assertTrue(any("Q1" in line and "差 2 份" in line for line in lines),
                        lines)
        self.assertTrue(any("成功 0/10" in line for line in lines), lines)

    def test_silent_when_disabled_or_empty(self) -> None:
        self.assertEqual(quota.progress_report(), [])
        quota.enable(True)
        self.assertEqual(quota.progress_report(), [])


class TestCliMutualExclusion(unittest.TestCase):
    """验收标准 3：--quota 与 --drift-correct / --alpha-target 同给退出码 2。"""

    def _main(self, *extra: str) -> tuple[int, str]:
        import contextlib
        import io

        from src import cli

        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                cli.main(["-u", _URL, "-n", "1", *extra])
            except SystemExit as e:
                code = int(e.code or 0)
        return code, buf.getvalue()

    def setUp(self) -> None:
        _reset()

    def tearDown(self) -> None:
        _reset()

    def test_rejects_drift_correct(self) -> None:
        code, out = self._main("--quota", "--drift-correct")
        self.assertEqual(code, 2)
        self.assertIn("--drift-correct 互斥", out)

    def test_rejects_alpha_target(self) -> None:
        code, out = self._main("--quota", "--alpha-target", "0.8")
        self.assertEqual(code, 2)
        self.assertIn("--alpha-target 互斥", out)

    def test_quota_alone_enables_and_implies_target_success(self) -> None:
        """不带冲突开关时 --quota 通过校验：banner 出现，且以 target-success
        语义与 quota_mode 进入批次引擎（引擎本体被替身拦下，不真跑）。"""
        import contextlib
        import io

        from src import cli

        args = cli.parse_args(["-u", _URL, "-n", "1", "--quota"])
        self.assertTrue(args.quota)

        captured: dict = {}

        def fake_run_batch(*a, **kw):
            captured["target_success"] = kw.get("target_success")
            captured["quota_mode"] = kw.get("quota_mode")
            captured["quota_enabled"] = quota.enabled()
            raise SystemExit(0)            # 语义截获后立刻收，不碰浏览器

        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf), \
                mock.patch.object(cli, "run_batch", side_effect=fake_run_batch):
            try:
                cli.main(["-u", _URL, "-n", "1", "--quota"])
            except SystemExit as e:
                code = int(e.code or 0)
        self.assertEqual(code, 0)
        self.assertIn("配额调度已开启", buf.getvalue())
        self.assertIn("目标成功份数", buf.getvalue())
        self.assertTrue(captured["target_success"], "--quota 必须隐含 target-success")
        self.assertTrue(captured["quota_mode"])
        self.assertTrue(captured["quota_enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
