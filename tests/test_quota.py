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
            self.assertEqual(quota.rebuild_consumed(db, rid), 4,
                             "成功的份扣账（单选 2 行 + 多选 2 行）；failed 不占")

            quota.effective_weights(1, [1.0, 1.0], 2)   # 初始化 [5,5] - 历史 2
            self.assertEqual(quota._remaining[1], [3, 5])
            self.assertEqual(quota._multi_pre_consumed.get(2), {0: 2, 1: 2},
                             "多选明细进多选暂记账（len==1 的 q1 行也进，但 q2 "
                             "不会建单选账，不会双计）")

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


class TestQuotaScopeB1(unittest.TestCase):
    """B1 边界（2026-10-29 独立审查 P1/P2）：配额只对单选/下拉建账。"""

    def setUp(self) -> None:
        _reset()

    def tearDown(self) -> None:
        _reset()

    def test_scale_and_matrix_never_get_ledgers(self) -> None:
        """量表与矩阵即使开着配额也不得建账 —— 此前
        _weighted_or_equal_choice 把 quota_eligible 硬编给所有调用方：
        量表分值被配额钉死（与 B1 相反）、矩阵题留幽灵账本刷"未兑完"假报告。
        """
        from src import config as cfg_module
        from src.answering_v2 import generate_answer

        with mock.patch.dict(
            cfg_module.WEIGHT_CONFIG,
            {
                5: {"type": "scale", "scale": 5},
                6: {"type": "matrix_single", "rows": [1], "cols": [1, 2],
                    "row_weights": {1: [1, 1]}},
            },
            clear=True,
        ):
            quota.enable(True)
            quota.start_run(10)
            for _ in range(5):
                generate_answer({"q": 5, "type": "scale", "scale": 5})
                generate_answer({"q": 6, "type": "matrix_single",
                                 "rows": [1], "cols": [1, 2]})
            self.assertNotIn(5, quota._remaining, "量表不得建配额账")
            self.assertNotIn(6, quota._remaining, "矩阵不得建配额账")

    def test_exhausted_ledger_while_batch_continues_warns_once(self) -> None:
        """不变量失守路径（独立审查 P2）：账本提前全满而成功数未达标 →
        大声说明并每批只提醒一次，不静默放过。"""
        import io
        from contextlib import redirect_stdout

        quota.enable(True)
        quota.start_run(10)
        quota.effective_weights(1, [1.0, 1.0], 2)      # 配额 [5, 5]
        quota._remaining[1] = [0, 0]                    # 人为造"提前全满"
        buf = io.StringIO()
        with redirect_stdout(buf):
            quota.effective_weights(1, [1.0, 1.0], 2)
            quota.effective_weights(1, [1.0, 1.0], 2)
        self.assertIn("不再受配额约束", buf.getvalue())
        self.assertEqual(buf.getvalue().count("不再受配额约束"), 1, "每批只提醒一次")

    def test_progress_report_distinguishes_shortfall_causes(self) -> None:
        """独立审查 P2：缺口成因必须区分"达标但未兑现"与"尝试上限先到"。"""
        quota.enable(True)
        quota.start_run(10)
        quota.effective_weights(1, [1.0, 1.0], 2)
        quota._remaining[1] = [0, 2]
        lines = quota.progress_report()
        self.assertTrue(any("尝试上限先到" in line for line in lines), lines)
        quota._success_count = 10
        lines = quota.progress_report()
        self.assertTrue(any("缺席" in line for line in lines), lines)


class TestMultiCalibrationB2(unittest.TestCase):
    """B2（2026-10-30 立项）：多选权重 = 目标入选率，引擎校准逐份勾选。"""

    def setUp(self) -> None:
        _reset()

    def tearDown(self) -> None:
        _reset()

    def test_alloc_multi_water_filling(self) -> None:
        # 8,1,1 选 2、N=2：rate 100/50/50 → [2,1,1]
        self.assertEqual(quota._alloc_multi(2, [8, 1, 1], 3, 2), [2, 1, 1])
        # 等权 4 选项选 2、N=8 → 每项入选 4/8 = 50%
        self.assertEqual(quota._alloc_multi(8, [1, 1, 1, 1], 4, 2), [4, 4, 4, 4])
        # 封顶重分配：[10,1] 选 2、N=10 → 两项都必须在每份里（rate 100%）
        self.assertEqual(quota._alloc_multi(10, [10, 1], 2, 2), [10, 10])
        # 零权重 = 0 配额（0 = 绝不选择，与单选同一条语义）
        self.assertEqual(quota._alloc_multi(10, [8, 1, 0], 3, 2)[2], 0)

    def test_exact_inclusion_rates_through_generate(self) -> None:
        """端到端：8,1,1 选 2、10 份成功 → 选项 1 全中、选项 2/3 各恰 5 份。"""
        from src import config as cfg_module
        from src.answering_v2 import generate_answer

        random.seed(20261030)
        with mock.patch.dict(
            cfg_module.WEIGHT_CONFIG,
            {1: {"type": "multi", "weights": [8, 1, 1], "count_options": [2]}},
            clear=True,
        ):
            quota.enable(True)
            quota.start_run(10)
            question = {"q": 1, "type": "multi", "choices": [1, 2, 3]}
            tally: Counter = Counter()
            for _ in range(10):
                ans = generate_answer(question)
                picks = [int(x) - 1 for x in ans["selected"]]
                quota.buffer_answer(1, picks)
                quota.commit_buffer()
                for p in picks:
                    tally[p] += 1
            self.assertEqual([tally[i] for i in range(3)], [10, 5, 5],
                             f"入选率必须精确 ∝ 权重（100/50/50%），实际 {dict(tally)}")
            self.assertEqual(quota._multi_remaining[1], [0, 0, 0])

    def test_unequal_weights_get_proportional_rates(self) -> None:
        """3,2,1 选 2、N=6：rate ∝ 3:2:1 → 总 12 格 = [6,4,2]（100/66/33%）。"""
        from src import config as cfg_module
        from src.answering_v2 import generate_answer

        random.seed(11)
        with mock.patch.dict(
            cfg_module.WEIGHT_CONFIG,
            {1: {"type": "multi", "weights": [3, 2, 1], "count_options": [2]}},
            clear=True,
        ):
            quota.enable(True)
            quota.start_run(6)
            question = {"q": 1, "type": "multi", "choices": [1, 2, 3]}
            tally: Counter = Counter()
            for _ in range(6):
                ans = generate_answer(question)
                picks = [int(x) - 1 for x in ans["selected"]]
                quota.buffer_answer(1, picks)
                quota.commit_buffer()
                for p in picks:
                    tally[p] += 1
            self.assertEqual([tally[i] for i in range(3)], [6, 4, 2],
                             f"入选率必须逐项精确，实际 {dict(tally)}")

    def test_variable_count_falls_back_to_b1_with_warning(self) -> None:
        """count_options 漂移的题校准不了（入选率没有定义）→ B1 兜底 + 一次告警。"""
        import io
        from contextlib import redirect_stdout

        from src import config as cfg_module
        from src.answering_v2 import generate_answer

        with mock.patch.dict(
            cfg_module.WEIGHT_CONFIG,
            {1: {"type": "multi", "weights": [1, 1, 1, 1],
                 "count_options": [1, 2]}},
            clear=True,
        ):
            quota.enable(True)
            quota.start_run(10)
            question = {"q": 1, "type": "multi", "choices": [1, 2, 3, 4]}
            buf = io.StringIO()
            with redirect_stdout(buf):
                for _ in range(6):
                    generate_answer(question)
            self.assertNotIn(1, quota._multi_remaining, "漂移题不建校准账")
            self.assertEqual(buf.getvalue().count("未进入入选率校准"), 1,
                             "每题只告警一次")

    def test_multi_commit_and_rebuild(self) -> None:
        quota.enable(True)
        quota.start_run(6)
        quota.multi_pick(2, 3, 2, [1.0, 1.0, 1.0])     # 建账 [4, 4, 4]（Σ = k×N = 12）
        quota.buffer_answer(2, [0, 1])
        self.assertEqual(quota.commit_buffer(), 1)
        self.assertEqual(quota._multi_remaining[2], [3, 3, 4])
        quota.buffer_answer(2, [2, 1])
        quota.discard_buffer()                          # 失败不占额
        self.assertEqual(quota._multi_remaining[2], [3, 3, 4])

        # 续传：成功答卷的多选勾选进暂记账，懒初始化时扣减
        with SubmissionHistory(":memory:") as db:
            rid = db.start_run(_URL, 5, "edge", False)
            for sub in (1, 2):
                db.record_answer(rid, sub, 2, "multi", options_selected=[0, 2])
                db.record_submission_result(rid, sub, "success")
            quota.rebuild_consumed(db, rid)
            quota._multi_remaining.clear()
            quota._multi_allocated.clear()
            quota.multi_pick(2, 3, 2, [1.0, 1.0, 1.0])  # [4,4,4] - 历史(0,2 各2)
            self.assertEqual(quota._multi_remaining[2], [2, 4, 2])

    def test_multi_k1_uses_multi_ledger_not_single(self) -> None:
        """k=1 的多选与单选形状相同：靠题号在哪本账区分，不靠 picks 长度。"""
        from src import config as cfg_module
        from src.answering_v2 import generate_answer

        with mock.patch.dict(
            cfg_module.WEIGHT_CONFIG,
            {1: {"type": "multi", "weights": [1, 1], "count_options": [1]}},
            clear=True,
        ):
            quota.enable(True)
            quota.start_run(4)
            question = {"q": 1, "type": "multi", "choices": [1, 2]}
            for _ in range(4):
                ans = generate_answer(question)
                quota.buffer_answer(1, [int(x) - 1 for x in ans["selected"]])
                quota.commit_buffer()
            self.assertEqual(quota._multi_remaining[1], [0, 0])
            self.assertNotIn(1, quota._remaining, "多选题不得落进单选账")


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

    def test_rejects_replay_file(self) -> None:
        """独立审查 P3：回放份不走配额账，混跑的兑现报告说不清缺口。"""
        code, out = self._main("--quota", "--replay-file", "r.csv")
        self.assertEqual(code, 2)
        self.assertIn("--replay-file 互斥", out)

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
