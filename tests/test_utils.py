"""纯逻辑工具函数单元测试。

覆盖：
  1. retry_with_backoff 指数退避重试（成功/失败边界）
  2. gaussian_sleep 正态分布等待（返回值范围、平均值偏差）
  3. human_pause 人类"偶尔停顿"行为（短停顿为主 + 偶发长停顿）
  4. pick_user_agent UA 池随机选择
  5. ManualHoldLock 人工介入锁（等待不超时、release 正确释放）
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import utils  # noqa: E402
from src.utils import sanitize_weights, weights_are_usable  # noqa: E402


class TestRetryWithBackoff(unittest.TestCase):
    """指数退避重试装饰器。"""

    def test_succeeds_on_first_try_returns_value(self) -> None:
        """首次就成功 → 直接返回值，等待 0 次。"""
        call_count = 0

        @utils.retry_with_backoff(max_attempts=3, initial_delay=0.01, backoff_factor=2, jitter=False)
        def f():
            nonlocal call_count
            call_count += 1
            return 42

        self.assertEqual(f(), 42)
        self.assertEqual(call_count, 1)

    def test_succeeds_after_retries(self) -> None:
        """前两次抛异常，第三次成功 → 共 3 次调用，最终返回。"""
        call_count = 0

        @utils.retry_with_backoff(max_attempts=3, initial_delay=0.005, backoff_factor=2, jitter=False)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError(f"boom {call_count}")
            return "ok"

        self.assertEqual(flaky(), "ok")
        self.assertEqual(call_count, 3)

    def test_exhausts_retries_reraises_last_error(self) -> None:
        """全部失败 → 抛出最后一次异常，调用次数 = max_attempts。"""
        call_count = 0

        @utils.retry_with_backoff(max_attempts=2, initial_delay=0.001, backoff_factor=2, jitter=False)
        def bad():
            nonlocal call_count
            call_count += 1
            raise ValueError(f"err{call_count}")

        with self.assertRaisesRegex(ValueError, "err2"):
            bad()
        self.assertEqual(call_count, 2)

    def test_delay_sequence_is_exponential(self) -> None:
        """检查每次实际等待时长 ≈ initial_delay * (backoff_factor ** i)。"""
        delays: list[float] = []
        call_count = 0

        def fake_sleep(secs: float) -> None:
            delays.append(secs)

        @utils.retry_with_backoff(
            max_attempts=4, initial_delay=0.01, backoff_factor=2, jitter=False, _sleep_fn=fake_sleep
        )
        def f():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            f()
        # 4 次调用 → 3 次等待 (attempt 1→2, 2→3, 3→4)
        self.assertEqual(len(delays), 3)
        expected = [0.01, 0.02, 0.04]
        for got, exp in zip(delays, expected):
            self.assertAlmostEqual(got, exp, delta=1e-6)

    def test_jitter_keeps_delay_within_range(self) -> None:
        """开 jitter → 每次等待在 [0.5*d, 1.5*d] 区间。"""
        delays: list[float] = []

        def fake_sleep(secs: float) -> None:
            delays.append(secs)

        @utils.retry_with_backoff(
            max_attempts=4, initial_delay=0.08, backoff_factor=2, jitter=True, _sleep_fn=fake_sleep
        )
        def f():
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            f()
        base = [0.08, 0.16, 0.32]
        for got, b in zip(delays, base):
            self.assertGreaterEqual(got, 0.5 * b)
            self.assertLessEqual(got, 1.5 * b)


class TestGaussianHumanTiming(unittest.TestCase):
    """正态分布/人类行为计时函数。"""

    def test_gaussian_clip_in_range(self) -> None:
        """gaussian_seconds 返回值必须落在 [lo, hi] 内。"""
        for _ in range(200):
            t = utils.gaussian_seconds(0.2, 0.06, 0.05, 0.5)
            self.assertGreaterEqual(t, 0.05)
            self.assertLessEqual(t, 0.5)
            self.assertIsInstance(t, float)

    def test_gaussian_mean_close_to_mu(self) -> None:
        """大样本下均值 ≈ mu（允许 ±10% 误差）。"""
        samples = [utils.gaussian_seconds(0.3, 0.05, 0.05, 0.6) for _ in range(5000)]
        mean = sum(samples) / len(samples)
        self.assertAlmostEqual(mean, 0.3, delta=0.03)

    def test_human_pause_returns_none_and_respects_wall(self) -> None:
        """human_pause 实际 sleep 的时长不超过上限。"""
        wall_start = time.perf_counter()
        utils.human_pause(mu=0.01, sigma=0.003, lo=0.002, hi=0.03, long_pause_prob=0.0, long_lo=0.1, long_hi=0.2)
        elapsed = time.perf_counter() - wall_start
        self.assertLess(elapsed, 0.15)  # 远短于长停顿

    def test_human_pause_long_pause_kicks_in_when_forced(self) -> None:
        """长停顿概率=1 时，sleep 时长必落在长区间。"""
        for _ in range(5):
            wall = time.perf_counter()
            utils.human_pause(mu=0.01, sigma=0.003, lo=0.0, hi=1,
                              long_pause_prob=1.0, long_lo=0.04, long_hi=0.06)
            elapsed = time.perf_counter() - wall
            self.assertGreaterEqual(elapsed, 0.03)


class TestUserAgentPool(unittest.TestCase):
    """UA 池选择。"""

    def test_pick_returns_from_pool(self) -> None:
        pool = utils.USER_AGENT_POOL
        self.assertGreaterEqual(len(pool), 3)
        for _ in range(20):
            self.assertIn(utils.pick_user_agent(), pool)

    def test_pick_with_seed_reproducible(self) -> None:
        """相同 seed 下 pick_user_agent 返回相同 UA。"""
        a = utils.pick_user_agent(_seed=42)
        b = utils.pick_user_agent(_seed=42)
        self.assertEqual(a, b)


class TestManualHoldLock(unittest.TestCase):
    """人工介入锁。"""

    def test_init_state_not_holding(self) -> None:
        lock = utils.ManualHoldLock()
        self.assertFalse(lock.is_holding)

    def test_acquire_release_cycle(self) -> None:
        lock = utils.ManualHoldLock()
        lock.acquire()
        self.assertTrue(lock.is_holding)
        lock.release()
        self.assertFalse(lock.is_holding)

    def test_wait_until_released_returns_true_when_released(self) -> None:
        """release 在另一个线程触发 → wait_until_released 返回 True。"""
        lock = utils.ManualHoldLock()
        lock.acquire()

        def releaser():
            time.sleep(0.05)
            lock.release()

        threading.Thread(target=releaser, daemon=True).start()
        t0 = time.perf_counter()
        result = lock.wait_until_released(timeout=2.0, check_interval=0.01)
        elapsed = time.perf_counter() - t0
        self.assertTrue(result)
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertFalse(lock.is_holding)

    def test_wait_until_released_timeout_returns_false(self) -> None:
        """一直不 release → 达到 timeout 后返回 False，is_holding 仍为 True。"""
        lock = utils.ManualHoldLock()
        lock.acquire()
        t0 = time.perf_counter()
        result = lock.wait_until_released(timeout=0.08, check_interval=0.01)
        elapsed = time.perf_counter() - t0
        self.assertFalse(result)
        self.assertTrue(lock.is_holding)
        self.assertGreaterEqual(elapsed, 0.07)
        lock.release()


# ============================================================================
#  权重清洗（v2.5：v1/v2 采样器共用同一份判定）
# ============================================================================
class TestWeightsUsable(unittest.TestCase):
    """weights_are_usable / sanitize_weights。

    回归背景：全 0 权重在 numpy 分支除零、NaN 权重让 random.choices 抛
    ValueError，而这类异常不在 TRANSIENT_DOM_EXCEPTIONS 里 → 整批任务在
    第一道题就终止。现已统一降级为等权重。
    """

    def test_rejects_all_zero_and_nan_and_inf_and_length(self) -> None:
        for bad in ([0, 0, 0], [0.0, 0.0], [float("nan"), 1.0, 1.0],
                    [float("inf"), 1.0], [1.0, 2.0], None, "abc", [True, False]):
            self.assertFalse(
                weights_are_usable(bad, 3),
                f"{bad!r} 应判为不可用",
            )

    def test_accepts_normal_weights(self) -> None:
        self.assertTrue(weights_are_usable([1, 2, 3], 3))
        self.assertTrue(weights_are_usable([0.0, 0.0, 5.0], 3))

    def test_sanitize_returns_none_so_caller_can_degrade(self) -> None:
        warned: list = []
        self.assertIsNone(
            sanitize_weights([0, 0], 2, question=1, warn=warned.append)
        )
        self.assertEqual(len(warned), 1, "降级必须留一行可见日志，不能静默")
        self.assertEqual(sanitize_weights([1, 3], 2), [1.0, 3.0])

    def test_generator_no_longer_crashes_on_bad_weights(self) -> None:
        """端到端：坏权重只降级，不抛。"""
        from src import answering, answering_v2

        cfgs = {
            1: {"type": "multi", "weights": [0, 0, 0],
                "count_options": [2], "count_weights": [0]},
            2: {"type": "scale", "scale": 5,
                "weights": [float("nan")] * 5},
        }
        with mock.patch.dict("src.config.WEIGHT_CONFIG", cfgs, clear=True):
            out = answering.build_answer_strategy(
                {"q": 1, "type": "multi", "choices": [1, 2, 3]}
            )
            self.assertEqual(len(out), 2)
            scale = answering_v2.generate_answer(
                {"q": 2, "type": "scale", "scale": 5}
            )
            self.assertIn(scale["value"], (1, 2, 3, 4, 5))
            multi = answering_v2.generate_answer(
                {"q": 1, "type": "multi", "choices": ["a", "b", "c", "d"]}
            )
            self.assertTrue(multi["selected"])


# ============================================================================
#  human_pause 的可中断性（v2.6：GUI 停止按钮响应延迟）
# ============================================================================
class TestHumanPauseAbortCheck(unittest.TestCase):
    """abort_check 生效时不必等满整段高斯停顿。

    此前轮间停顿是一次性 sleep，最长 20s 且打不断 —— 用户点「停止」后
    还要等这一整段跑完才见效。
    """

    def test_no_abort_check_keeps_original_single_sleep(self) -> None:
        slept: list = []
        got = utils.human_pause(
            1.0, 0.1, 0.5, 2.0, _sleep_fn=slept.append,
        )
        self.assertEqual(len(slept), 1, "不传 abort_check 时必须仍是一次整段 sleep")
        self.assertAlmostEqual(got, slept[0], places=6)

    def test_abort_check_shortens_a_long_pause(self) -> None:
        slept: list = []
        calls = {"n": 0}

        def abort_after_two():
            calls["n"] += 1
            return calls["n"] > 2

        got = utils.human_pause(
            10.0, 0.0, 10.0, 10.0,          # 恒定 10s，好验证被截断
            _sleep_fn=slept.append,
            abort_check=abort_after_two,
        )
        self.assertLess(got, 1.0, f"应在 2 个轮询片后就停，实际睡了 {got}s")
        self.assertEqual(len(slept), 2)

    def test_completes_normally_when_never_aborted(self) -> None:
        slept: list = []
        got = utils.human_pause(
            1.0, 0.0, 1.0, 1.0,
            _sleep_fn=lambda s: slept.append(s),
            abort_check=lambda: False,
        )
        self.assertAlmostEqual(got, 1.0, places=2)
        self.assertAlmostEqual(sum(slept), 1.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
