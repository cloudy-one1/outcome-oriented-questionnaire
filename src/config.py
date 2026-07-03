"""全局配置模块。

包含：
    - WEIGHT_CONFIG : 每道题的选项权重分布（可按需修改）
    - 运行时常量（默认超时、轮询间隔等）
"""

from __future__ import annotations

# ============================================================================
#  权重配置 WEIGHT_CONFIG（可按需修改）
# ============================================================================
#
#  配置格式说明：
#    key        —— 整数题号，对应页面上的 q1, q2, q3 ... 的数字部分
#    type       —— "single" 表示单选题（radio），"multi" 表示多选题（checkbox）
#    weights    —— 各选项被选中的概率权重，数组长度必须等于该题的选项总数
#                  （所有权重之和会自动归一化，不需要保证和为 1）
#    count_options —— [仅多选题] 可选，控制"选几个"的分布，默认从 1 到选项总数
#    count_weights —— [仅多选题] 可选，与 count_options 对应的概率权重
#
#  示例：
#    1: {"type": "single", "weights": [0.24, 0.4, 0.26, 0.1]}
#    表示第 1 题是单选题，有 4 个选项，
#    选项 A 被选概率 24%，B 40%，C 26%，D 10%
#
#    7: {"type": "multi", "weights": [0.05, 0.15, 0.3, 0.35, 0.15]}
#    表示第 7 题是多选题，有 5 个选项，
#    选中数量随机（1~5 均匀分布），然后按 5%/15%/30%/35%/15% 加权无放回抽取
# ============================================================================

WEIGHT_CONFIG: dict = {
    # ... existing config entries remain unchanged ...
    # 单选题 (single)：最终只选一个选项
    1:  {"type": "single", "weights": [0.24, 0.4, 0.26, 0.1]},
    2:  {"type": "single", "weights": [0.55, 0.45, 0.0]},       # 第 3 个选项权重为 0，永远不会被选中
    3:  {"type": "single", "weights": [0.1, 0.37, 0.33, 0.2]},
    4:  {"type": "single", "weights": [0.02, 0.08, 0.2, 0.45, 0.25]},
    5:  {"type": "single", "weights": [0.01, 0.05, 0.1, 0.64, 0.2]},
    6:  {"type": "single", "weights": [0.01, 0.1, 0.15, 0.6, 0.14]},

    # 多选题 (multi)：可选中多个选项
    7:  {"type": "multi",  "weights": [0.05, 0.15, 0.3, 0.35, 0.15]},
    8:  {"type": "multi",  "weights": [0.04, 0.16, 0.2, 0.45, 0.15]},
    9:  {"type": "multi",  "weights": [0.05, 0.1, 0.35, 0.4, 0.1]},
    10: {"type": "multi",  "weights": [0.09, 0.21, 0.35, 0.25, 0.1]},
    11: {"type": "multi",  "weights": [0.08, 0.2, 0.35, 0.27, 0.1]},
    12: {"type": "multi",  "weights": [0.02, 0.08, 0.22, 0.43, 0.25]},
    13: {"type": "multi",  "weights": [0.01, 0.04, 0.15, 0.5, 0.3]},
    14: {"type": "multi",  "weights": [0.02, 0.03, 0.15, 0.6, 0.2]},
    15: {"type": "multi",  "weights": [0.01, 0.06, 0.18, 0.4, 0.35]},
    16: {"type": "multi",  "weights": [0.03, 0.12, 0.25, 0.42, 0.18]},
    17: {"type": "multi",  "weights": [0.04, 0.11, 0.2, 0.38, 0.27]},
    18: {"type": "multi",  "weights": [0.08, 0.17, 0.25, 0.33, 0.17]},
    19: {"type": "multi",  "weights": [0.03, 0.15, 0.27, 0.37, 0.18]},
    20: {"type": "multi",  "weights": [0.05, 0.15, 0.25, 0.35, 0.2]},
    21: {"type": "multi",  "weights": [0.04, 0.16, 0.3, 0.3, 0.2]},
    22: {"type": "multi",  "weights": [0.03, 0.1, 0.25, 0.44, 0.18]},
}


# ============================================================================
#  运行时常量（一般无需修改）
# ============================================================================

# 默认问卷 URL（命令行入口使用）
DEFAULT_SURVEY_URL: str = "https://v.wjx.cn/vm/eo733a1.aspx#"

# 默认批量提交份数
DEFAULT_TOTAL_SUBMISSIONS: int = 17

# 页面加载超时（秒）
PAGE_LOAD_TIMEOUT: int = 30

# 题目探测等待超时（秒）
QUESTION_DETECT_TIMEOUT: int = 15

# 验证码等待超时（秒）
VERIFICATION_TIMEOUT: int = 120

# 每多少轮主动重启浏览器（释放内存）
RESTART_BROWSER_EVERY: int = 30

# 轮次间随机等待区间（秒）
ROUND_INTERVAL_MIN: float = 4.0
ROUND_INTERVAL_MAX: float = 7.0


# ============================================================================
#  反检测 / 人类行为模拟参数
# ============================================================================
#
# 核心原则：行为参数采用「正态分布 + 区间截断」，
#         让每次点击/等待的时间呈现高斯分布（模拟真实人类），
#         而不是可疑的 uniform(min, max) 均匀分布。

# 单个选项点击后的「人类反应 + 点击事件生效」等待
CLICK_AFTER_MU: float = 0.22        # 均值（秒）
CLICK_AFTER_SIGMA: float = 0.06     # 标准差
CLICK_AFTER_LO: float = 0.1         # 下限（防止过快）
CLICK_AFTER_HI: float = 0.5         # 上限（防止过慢）

# 每道题之间的「思考时间」
Q_THINK_MU: float = 0.5
Q_THINK_SIGMA: float = 0.2
Q_THINK_LO: float = 0.2
Q_THINK_HI: float = 1.2

# 答题中途模拟"真的停下来思考"的长停顿概率 + 区间
Q_LONG_PAUSE_PROB: float = 0.03     # 每题 3% 概率
Q_LONG_PAUSE_LO: float = 2.0
Q_LONG_PAUSE_HI: float = 4.5

# 轮次间等待（替换原均匀分布）
ROUND_WAIT_MU: float = 5.5
ROUND_WAIT_SIGMA: float = 1.3
ROUND_WAIT_LO: float = 3.0
ROUND_WAIT_HI: float = 9.0
ROUND_LONG_PAUSE_PROB: float = 0.05  # 每轮 5% 概率真的去"看手机/喝水"
ROUND_LONG_PAUSE_LO: float = 10.0
ROUND_LONG_PAUSE_HI: float = 20.0


# ============================================================================
#  指数退避重试参数（用于页面加载/答题/提交偶发失败）
# ============================================================================

SUBMISSION_MAX_ATTEMPTS: int = 2          # 单次提交最多重试 2 次（总尝试 <= 2）
SUBMISSION_INITIAL_DELAY: float = 1.2     # 首次重试等待秒
SUBMISSION_BACKOFF: float = 2.0           # 等待倍率
RETRY_JITTER: bool = True                 # 给等待时间加随机抖动（±50%）

# 页面 GET 失败重试
PAGE_LOAD_MAX_ATTEMPTS: int = 2
PAGE_LOAD_INITIAL_DELAY: float = 1.0

# 每 N 题主动检查一次验证码（原 3 → 提升为 2，更频繁）
VERIFY_EVERY_N_QUESTIONS: int = 2


# ============================================================================
#  浏览器指纹多样性（每次启动浏览器随机挑一套）
# ============================================================================

# 常见的中国用户桌面分辨率集合（非全 HD，更真实）
SCREEN_PRESETS: list[tuple[int, int, int]] = [
    # (width, height, availTop — 任务栏高度)
    (1920, 1080, 40),
    (1920, 1080, 48),
    (1600, 900, 40),
    (1440, 900, 40),
    (1366, 768, 40),
    (1536, 864, 40),
    (1280, 720, 40),
]

# 常见的 deviceMemory / hardwareConcurrency 组合（中国办公电脑主流配置）
HW_PRESETS: list[tuple[int, int]] = [
    (8, 8), (8, 12), (16, 12), (16, 16), (8, 6), (4, 4),
]

# Windows 时区（中国标准时间，UTC+8，分钟为单位的负值）
TIMEZONE_OFFSET_MIN: int = -480  # 中国大陆统一 UTC+8


# ============================================================================
#  浏览器选择相关默认值
# ============================================================================

# 默认浏览器（edge 或 chrome）。注意：这里不改历史默认值，
# 以便老用户的行为保持一致；用户可通过 CLI --browser / GUI 下拉框切到 Chrome
DEFAULT_BROWSER: str = "edge"

# Chrome 默认是否优先使用 undetected-chromedriver
# False = 使用 Selenium 原生 Chrome + Stealth CDP（零额外依赖，默认）
# True  = 先尝试 undetected-chromedriver，失败再回退 Selenium Chrome
DEFAULT_USE_UC: bool = False

# 允许的浏览器类型列表（供 CLI/GUI 校验入参）
BROWSER_OPTIONS: tuple[str, ...] = ("edge", "chrome")
