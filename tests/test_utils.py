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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import utils  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
