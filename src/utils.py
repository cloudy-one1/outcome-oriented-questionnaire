"""通用工具函数集合。

本模块是纯逻辑实现（不依赖 Selenium/浏览器），可被单元测试完整覆盖。
包含：
    - 指数退避重试装饰器
    - 人类行为模拟的正态分布等待
    - User-Agent 多样性池 + 随机挑选
    - 人工介入锁（ManualHoldLock）：防止页面触发人工验证时流程误判超时
"""

from __future__ import annotations

import functools
import random
import threading
import time
from typing import Any, Callable, TypeVar


_Fn = TypeVar("_Fn", bound=Callable[..., Any])


# ============================================================================
#  User-Agent 多样性池（Edge for Windows / Chrome for Windows；每次启动浏览器随机挑一个）
#  真实 UA 会随着浏览器更新迭代，这里用"常见版本 + 微小变体"避免单一指纹。
# ============================================================================

USER_AGENT_POOL_EDGE: list[str] = [
    # Edge 131 (主流)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    # Edge 130
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    # Edge 129
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    # Edge 131 + WOW64 (旧版 Win10 32-bit-on-64-bit)
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]

USER_AGENT_POOL_CHROME: list[str] = [
    # Chrome 131 (主流)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 130
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Chrome 129
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    # Chrome 131 + WOW64
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# 兼容别名：老代码引用 USER_AGENT_POOL 时默认给 Edge
USER_AGENT_POOL: list[str] = USER_AGENT_POOL_EDGE

# 支持的浏览器类型
BROWSER_TYPES = ("edge", "chrome")


def pick_user_agent(
    browser: str = "edge",
    _seed: int | None = None,
) -> str:
    """从对应浏览器的 UA 池中随机选择一个 UA 字符串。

    参数：
      browser : "edge" | "chrome"（大小写不敏感），默认 edge
      _seed   : 仅单元测试用，固定随机种子以获得可重复结果
    """
    b = (browser or "edge").lower()
    if b == "chrome":
        pool = USER_AGENT_POOL_CHROME
    else:
        pool = USER_AGENT_POOL_EDGE
    rnd = random.Random(_seed) if _seed is not None else random
    return rnd.choice(pool)


# ============================================================================
#  人类行为计时 — 正态分布截断采样（比 uniform 更真实）
# ============================================================================

def gaussian_seconds(mu: float, sigma: float, lo: float, hi: float,
                    *, _rng: random.Random | None = None) -> float:
    """返回一个落在 [lo, hi] 区间内、近似 N(mu, sigma^2) 的随机秒数（float）。

    采样策略：用 Box-Muller 高斯采样；若超出范围就重新采样，最多 50 次；
    仍落不到区间内就退化为边界 clamp。该方法可以保证每次返回的均值≈mu，
    且绝对不会越界。
    """
    rng = _rng or random
    for _ in range(50):
        sample = rng.gauss(mu, sigma)
        if lo <= sample <= hi:
            return sample
    # 极端退化为边界（很少发生）
    return max(lo, min(hi, mu))


def human_pause(mu: float, sigma: float, lo: float, hi: float,
                *,
                long_pause_prob: float = 0.0,
                long_lo: float = 2.0, long_hi: float = 5.0,
                _rng: random.Random | None = None,
                _sleep_fn: Callable[[float], Any] | None = None) -> float:
    """模拟人类行为的「停顿」，真实 sleep 一段时间后返回实际 sleep 的秒数。

    逻辑：
      1. 先掷骰子：若 long_pause_prob > 0 且命中，则在 [long_lo, long_hi] 中均匀
         抽取一段长停顿（模拟"思考""看手机""喝水"这类离散事件）
      2. 否则走标准正态分布截断采样
      3. 调用 _sleep_fn 或 time.sleep 睡眠
      4. 返回实际 sleep 秒数（供日志/统计）
    """
    rng = _rng or random
    if long_pause_prob > 0 and rng.random() < long_pause_prob:
        seconds = rng.uniform(long_lo, long_hi)
    else:
        seconds = gaussian_seconds(mu, sigma, lo, hi, _rng=rng)
    sleep_fn = _sleep_fn or time.sleep
    sleep_fn(seconds)
    return seconds


# ============================================================================
#  指数退避重试装饰器
# ============================================================================

def retry_with_backoff(
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    retry_on: tuple[type[BaseException], ...] | type[BaseException] = Exception,
    *,
    _sleep_fn: Callable[[float], Any] | None = None,
) -> Callable[[_Fn], _Fn]:
    """指数退避重试装饰器。

    典型延迟序列（initial_delay=1.0, backoff_factor=2, jitter=False）：
      第 1 次失败后等待 1.0s → 第 2 次失败后等待 2.0s → …
    jitter=True 时：每次延迟 d 会被扰动为 [0.5d, 1.5d] 内的随机值，
    避免多个客户端在失败重试时产生"惊群效应"。

    参数：
      max_attempts   : 最大尝试次数（>=1），达到上限后抛出最后一次异常
      initial_delay  : 第 1 次失败后等待时长（秒）
      backoff_factor : 每次延迟的倍率（通常为 2.0，指数增长）
      jitter         : 是否给延迟增加 ±50% 随机抖动
      retry_on       : 指定哪些异常会触发重试（默认所有 Exception）
      _sleep_fn      : 仅测试用，替换 time.sleep
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    sleep_fn = _sleep_fn or time.sleep

    def decorator(func: _Fn) -> _Fn:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retry_on as exc:  # 只有命中的异常才重试
                    last_exc = exc
                    # 最后一次尝试不再等待，直接在下一行外抛
                    if attempt >= max_attempts:
                        break
                    # 计算本次等待
                    delay = initial_delay * (backoff_factor ** (attempt - 1))
                    if jitter:
                        delay = delay * random.uniform(0.5, 1.5)
                    sleep_fn(delay)
            # 到达 max_attempts，抛出最后一次异常
            assert last_exc is not None
            raise last_exc

        return wrapper  # type: ignore[return-value]

    return decorator


# ============================================================================
#  人工介入锁 — 用于验证码检测后，避免误判为超时失败
# ============================================================================

class ManualHoldLock:
    """人工介入锁。

    使用场景：
      1. 检测到验证码后，调用 lock.acquire() 进入"人工介入"状态
      2. pipeline 中的任何超时/中断逻辑在看到 lock.is_holding=True 时
         都不得直接返回 False，必须调用 wait_until_released 阻塞等待
      3. 用户完成验证后（验证码消失或页面跳转）调用 lock.release()
      4. wait_until_released 成功返回 True 后恢复自动化流程

    线程安全：可在后台执行线程与 UI 回调线程之间共享。
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._event.set()  # 默认 set 状态 → **非** holding（释放态）

    @property
    def is_holding(self) -> bool:
        """当前是否处于"人工介入 holding"状态。"""
        return not self._event.is_set()

    def acquire(self) -> None:
        """进入人工介入锁状态。重复 acquire 无副作用。"""
        self._event.clear()

    def release(self) -> None:
        """释放人工介入锁，允许流程继续。重复 release 无副作用。"""
        self._event.set()

    def wait_until_released(self, timeout: float = 120.0,
                            check_interval: float = 0.1) -> bool:
        """阻塞等待直到被 release 或 timeout。

        参数：
          timeout        : 最长等待秒数
          check_interval : 间隔多少秒检查一次（越小响应越快）

        返回：
          True  : 在超时前被释放
          False : 到达 timeout 仍未释放（通常意味着要刷新重试）
        """
        if not self.is_holding:
            return True  # 已经释放，立刻返回
        # Event.wait() 天然支持 timeout；封装成独立 check 以便后续扩展回调
        return self._event.wait(timeout=timeout)

    # 上下文管理器支持：with lock: ... 会自动 acquire/release
    def __enter__(self) -> "ManualHoldLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False  # 不吞异常
