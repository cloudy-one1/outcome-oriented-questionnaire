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
import math
import random
import threading
import time
from typing import Any, Callable, Optional, TypeVar

# numpy 可选：装了就用其高效的无放回加权抽样，没装走纯 Python A-Res 算法
# numpy 是可选依赖。显式把 np 声明成 Any，避免 pyright 把它推成
# "Module | None" 后，对 np.random.choice 报 reportOptionalMemberAccess。
np: Any
try:
    import numpy as np
    _HAS_NUMPY: bool = True
except Exception:  # pragma: no cover - 环境缺 numpy 时走纯 Python 路径
    np = None
    _HAS_NUMPY = False


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
                    *, _rng: Any | None = None) -> float:
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
                abort_check: Callable[[], bool] | None = None,
                _rng: Any | None = None,
                _sleep_fn: Callable[[float], Any] | None = None) -> float:
    """模拟人类行为的「停顿」，真实 sleep 一段时间后返回实际 sleep 的秒数。

    逻辑：
      1. 先掷骰子：若 long_pause_prob > 0 且命中，则在 [long_lo, long_hi] 中均匀
         抽取一段长停顿（模拟"思考""看手机""喝水"这类离散事件）
      2. 否则走标准正态分布截断采样
      3. 调用 _sleep_fn 或 time.sleep 睡眠
      4. 返回实际 sleep 秒数（供日志/统计）

    :param abort_check: 每 ~0.2s 询问一次；返回 True 就**提前结束停顿**并立即返回
        已 sleep 的秒数。给 GUI「停止」按钮用：轮间停顿最长可达 20s，
        此前一次性 sleep 打不断，用户点停止要等整段停顿跑完才见效。
        CLI 不传，行为与旧版完全一致。
    """
    rng = _rng or random
    if long_pause_prob > 0 and rng.random() < long_pause_prob:
        seconds = rng.uniform(long_lo, long_hi)
    else:
        seconds = gaussian_seconds(mu, sigma, lo, hi, _rng=rng)

    if abort_check is None:
        sleep_fn = _sleep_fn or time.sleep
        sleep_fn(seconds)
        return seconds

    # 分片睡眠：把一整段停顿切成 <=0.2s 的小片，每片之间问一次要不要停
    sleep_fn = _sleep_fn or time.sleep
    _ABORT_POLL_INTERVAL = 0.2
    remaining = seconds
    elapsed = 0.0
    while remaining > 0:
        if abort_check():
            break
        chunk = min(_ABORT_POLL_INTERVAL, remaining)
        sleep_fn(chunk)
        elapsed += chunk
        remaining -= chunk
    return elapsed


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


# ============================================================================
#  权重合法性清洗 —— v1(answering) 与 v2(answering_v2) 共用同一份判定
# ============================================================================

def weights_are_usable(weights: Any, n: int) -> bool:
    """判断一组权重能否安全喂给 ``random.choices`` / ``numpy`` 归一化。

    不合法的情形（任一命中即 False）：
      - 长度与选项数不符
      - 含 NaN / Inf（``random.choices`` 会抛 ValueError）
      - 含非数值项
      - 正权重总和为 0（全 0 或全负，同样抛 ValueError / 除零）
    """
    if not isinstance(weights, (list, tuple)) or len(weights) != n:
        return False
    positive_total = 0.0
    for w in weights:
        if isinstance(w, bool) or not isinstance(w, (int, float)):
            return False
        if math.isnan(w) or math.isinf(w):
            return False
        if w > 0:
            positive_total += w
    return positive_total > 0


def sanitize_weights(
    weights: Any,
    n: int,
    *,
    question: int | None = None,
    label: str = "权重",
    warn: Callable[[str], None] | None = None,
) -> Optional[list[float]]:
    """返回可直接用于加权采样的权重列表；不合法时返回 ``None`` 让调用方降级。

    Why：权重非法抛出的 ``ValueError`` 不在 ``TRANSIENT_DOM_EXCEPTIONS`` 内，
    会让**整批任务**在第一道题就终止 —— 代价与「这一题按等权重随机」完全不成
    比例。CLI 已在加载期硬校验，本函数是 GUI（校验仅告警）与手写 WEIGHT_CONFIG
    的运行时兜底。
    """
    if weights_are_usable(weights, n):
        return [float(w) for w in weights]
    msg = f"  WARNING: Q{question} {label}非法（长度/NaN/总和为 0），已按等权重处理" \
        if question is not None \
        else f"  WARNING: {label}非法（长度/NaN/总和为 0），已按等权重处理"
    (warn or print)(msg)
    return None


# ============================================================================
#  加权无放回抽样 —— v1(answering) 与 v2(answering_v2) 共用这一份实现
# ============================================================================

def weighted_sample_no_replace(
    pool: list,
    weights: Any,
    k: int,
) -> list:
    """从 ``pool`` 里按 ``weights`` 无放回抽 k 个，返回**升序**结果。

    单一实现的原因：这段逻辑此前在 answering.py 与 answering_v2.py 各有一份
    （都是 A-Res / numpy 概率法），而两副本的行为并不相同 ——
    answering.py 的 numpy 分支缺非法权重守卫，全 0 权重直接 ZeroDivisionError，
    answering_v2 那份早就修好了。副本只要存在，就会出现"修了一边忘了另一边"。

    非法权重（长度不符 / NaN / 总和为 0）一律降级为等概率抽样，
    判定与 :func:`weights_are_usable` 共用同一把尺子。
    """
    n = len(pool)
    if n == 0:
        return []
    k = max(1, min(int(k), n))

    usable = weights_are_usable(weights, n)
    # numpy 的 p= 抽样要求"非零概率的条目数 >= k"，否则抛
    # ValueError: Fewer non-zero entries in p than size。
    # 权重形如 [0, 0, 1] 而 k=2 时就会撞上（多选题里很自然：用户只想让一个选项出现，
    # 但仍要求填满足少选个数）。此时走 A-Res —— 它给零权重一个 1e-12 的兜底。
    positive_cnt = (
        sum(1 for w in weights if w > 0) if usable else 0
    )

    if _HAS_NUMPY and (not usable or positive_cnt >= k):
        if usable:
            total = sum(weights)
            probs = [w / total for w in weights]
            picked = np.random.choice(n, size=k, replace=False, p=probs)
        else:
            picked = np.random.choice(n, size=k, replace=False)
        return sorted(pool[int(i)] for i in picked)

    # A-Res（Efraimidis & Spirakis）：key = log(u)/w，取 top-k。
    # 零/负权重不能直接除，给一个 1e-12 的下限 —— 于是它"几乎不会被选中"，
    # 但在 k 大于正权重个数时仍能凑够 k 个不同选项（v1 原本就是这个行为）。
    _W_FLOOR = 1e-12
    pairs: list[tuple[float, Any]] = []
    for i in range(n):
        if usable:
            w = float(weights[i])
        else:
            w = 1.0
        pairs.append((math.log(random.random()) / max(w, _W_FLOOR), pool[i]))
    pairs.sort(reverse=True)
    return sorted(item for _, item in pairs[:k])


def equal_sample_no_replace(pool: list, k: int) -> list:
    """无放回等概率抽样，返回升序结果。"""
    n = len(pool)
    if n == 0:
        return []
    k = max(1, min(int(k), n))
    if _HAS_NUMPY:
        return sorted(pool[int(i)] for i in np.random.choice(n, size=k, replace=False))
    return sorted(random.sample(pool, k=k))
