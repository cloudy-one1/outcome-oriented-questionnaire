"""答案生成策略模块。

根据题目结构 + 用户权重配置（WEIGHT_CONFIG），生成该题的随机答案。

决策逻辑（按优先级）：
  1. 如果 WEIGHT_CONFIG 中有该题的自定义权重 → 使用加权随机
     - 单选题：按 weights 加权采样，选中 1 个选项
     - 多选题：先从 count_options 中加权采样出"选几个"（k），
               再用 numpy 无放回加权抽样选出 k 个不同选项
  2. 如果没有配置 → 等权重随机
     - 单选题：random.choice（均匀分布）
     - 多选题：随机选 k 个（k 在 1 ~ 选项总数之间均匀随机）
"""

from __future__ import annotations

import random

import numpy as np

from .config import WEIGHT_CONFIG


def build_answer_strategy(question: dict) -> list[int]:
    """根据题目结构 + 用户权重配置，生成该题的随机答案。

    为什么多选题用 numpy.random.choice(replace=False)？
      因为 Python 标准库的 random.choices 只支持有放回抽样，
      直接使用会导致多选题选中重复选项 —— 这是问卷不允许的。

    参数：
      question : dict，detect_questions() 返回的单题结构

    返回：
      list[int]，被选中的选项值列表
        - 单选题（单选）返回 [单个值]，如 [3]
        - 多选题（多选）返回排序后的列表，如 [1, 3, 5]
    """
    qi = question["q"]  # 题号

    cfg = WEIGHT_CONFIG.get(qi)  # 该题的权重配置，可能为 None

    # ------------------------------------------------------------------
    #  情况 A：用户配置了权重 → 加权随机
    # ------------------------------------------------------------------
    if cfg and "weights" in cfg:
        weights = cfg["weights"]  # 用户指定的各选项权重
        n = len(question["choices"])  # 页面实际选项数

        # 安全检查：权重长度必须与选项数一致
        if len(weights) != n:
            print(f"  WARNING: Q{qi} 权重长度 {len(weights)} != 选项数 {n}，使用等权重")
            weights = [1] * n  # 降级为等权重

        if question["type"] == "single":
            # 单选题：加权有放回采样 1 个选项（k=1）
            return [random.choices(question["choices"], weights=weights, k=1)[0]]
        else:
            # 多选题：先决定"选几个"，再按权重无放回抽具体选项
            # 默认：从 1 到 n 中均匀随机选择 k
            count_opts = cfg.get("count_options", list(range(1, n + 1)))
            count_wts = cfg.get("count_weights", [1] * len(count_opts))

            # 加权采样出"选几个"
            k = random.choices(count_opts, weights=count_wts, k=1)[0]

            # 归一化权重 → 概率分布（numpy 要求 p 之和为 1）
            total_w = sum(weights)
            probs = [w / total_w for w in weights]

            # 无放回加权抽样 k 个选项，返回排序后的列表
            return sorted(
                np.random.choice(question["choices"], size=k, replace=False, p=probs).tolist()
            )

    # ------------------------------------------------------------------
    #  情况 B：无配置 → 等权重随机
    # ------------------------------------------------------------------
    else:
        c = question["choices"]
        if question["type"] == "single":
            # 单选题：等概率随机选 1 个
            return [random.choice(c)]
        else:
            # 多选题：随机决定选几个（1 ~ 选项总数），然后无放回等概率抽样
            k = random.randint(1, len(c))
            return sorted(np.random.choice(c, size=k, replace=False).tolist())
