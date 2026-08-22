"""v2.0 — 新题型答案生成策略。

相比 v1 answering 模块的改进：
    1. 返回统一 dict 结构，兼容 single/multi 并新增 text/scale/dropdown/matrix_single
    2. 填空题内置中文数据池（姓名/手机/邮箱/地址），零外部依赖（不需要 Faker 包）
    3. 每题可独立配置权重，不再依赖全局 WEIGHT_CONFIG（也可以接 WEIGHT_CONFIG 过渡）

统一返回格式 ::

    # 选择题
    {"type": "single",       "selected": [idx]}
    {"type": "multi",        "selected": [idx1, idx2, ...]}
    {"type": "dropdown",     "selected": [idx]}

    # 量表 / 填空
    {"type": "scale",        "value": int_1_to_N}
    {"type": "text",         "text": "具体内容", "field": "name"|"phone"|...|None}

    # 矩阵
    {"type": "matrix_single","rows": {row_idx: col_idx, ...}}

设计原则：
    - 零外部依赖：中文数据池内置，不需要安装 Faker
    - 幂等友好：所有随机数来源均为 random/numpy，已由上层播种（如需可重复）
"""

from __future__ import annotations

import math
import random
from typing import Any, Optional

# numpy 为可选依赖：有则用其高效无放回加权抽样；没有则用纯 Python A-Res 算法
try:
    import numpy as np  # type: ignore
    _HAS_NUMPY: bool = True
except Exception:  # pragma: no cover - 环境缺 numpy 时走纯 Python 路径
    np = None  # type: ignore
    _HAS_NUMPY = False

from .config import WEIGHT_CONFIG


# ============================================================================
#  中文数据池（零依赖）：姓名、手机号前缀、邮箱域名、地址部件
# ============================================================================
_SURNAMES = (
    "王李张刘陈杨赵黄周吴徐孙胡朱高林何郭马罗梁宋郑谢韩唐冯于董萧程曹袁邓许傅沈曾彭吕"
    "苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚谭廖邹熊金陆郝孔白崔康毛邱秦"
    "江史顾侯邵孟万段雷钱汤尹黎易常武乔贺赖龚文"
)
_GIVEN_NAMES_M = (
    "伟 强 磊 军 洋 勇 艳 杰 涛 明 超 秀兰 霞 平 刚 桂英 建国 建华 志强 晓明 志伟 "
    "浩宇 浩然 宇航 子轩 文博 梓涵 思远 俊杰 睿 皓 博文 子豪 天佑 宇航 润泽 承恩".split()
)
_GIVEN_NAMES_F = (
    "芳 娜 敏 静 丽 强 磊 军 洋 艳 杰 娟 涛 明 超 秀兰 霞 平 桂英 玉兰 秀英 建华 "
    "思琪 梓萱 雨桐 梦瑶 佳怡 雨萱 欣怡 梓涵 诗涵 语桐 若曦 紫涵 思远 静怡 晨曦 悦".split()
)
_MOBILE_PREFIXES = (
    130, 131, 132, 133, 134, 135, 136, 137, 138, 139,
    150, 151, 152, 153, 155, 156, 157, 158, 159,
    176, 177, 178, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189,
    198, 199,
)
_EMAIL_DOMAINS = (
    "qq.com", "163.com", "126.com", "gmail.com", "outlook.com",
    "sina.com", "foxmail.com", "hotmail.com", "icloud.com",
)
_EMAIL_USER_PREFIXES = (
    "zhang wang li zhao liu chen yang huang zhou wu xu sun zhu mao tan "
    "alex bob charlie david emma frank grace henry iris jack kate leo "
    "marry nancy oliver peter quincy rose sam tom uma vivian wendy".split()
    + ["love2008", "happy_dog", "sky_blue", "sunshine", "cool_boy", "nice_girl",
       "dreamer", "winner", "genius", "superman", "angel007", "hero888"]
)

# 地址部件：省份+城市、后缀
_CITIES = (
    "北京市朝阳区", "上海市浦东新区", "广州市天河区", "深圳市南山区",
    "杭州市西湖区", "成都市武侯区", "武汉市洪山区", "西安市雁塔区",
    "南京市鼓楼区", "重庆市渝中区", "苏州市工业园区", "天津市和平区",
    "郑州市金水区", "青岛市市南区", "长沙市岳麓区", "厦门市思明区",
    "济南市历下区", "合肥市蜀山区", "佛山市南海区", "东莞市长安镇",
)
_STREET_PREFIXES = "人民 中山 解放 建设 文化 科技 和平 朝阳 胜利 创新 长江 黄河 海滨 大学 体育".split()
_STREET_TYPES = "路 街 大道 巷 胡同".split()


# ============================================================================
#  内部工具：中文数据生成
# ============================================================================
def _chinese_name() -> str:
    """随机生成 2~4 字中文姓名。"""
    s = random.choice(_SURNAMES)
    # 20% 2字名 + 80% 1字名 = 2字或3字姓名
    if random.random() < 0.25:
        g = random.choice(_GIVEN_NAMES_M + _GIVEN_NAMES_F)
    else:
        # 单字名：只取第一个字（避免"建国"这种两字作为名）
        pool = _GIVEN_NAMES_M + _GIVEN_NAMES_F
        single_chars = [c for w in pool for c in w if "\u4e00" <= c <= "\u9fff"]
        g = random.choice(single_chars)
    return s + g


def _chinese_phone() -> str:
    """生成 11 位中国手机号（合规前缀 + 8 位随机）。"""
    prefix = random.choice(_MOBILE_PREFIXES)
    suffix = random.randint(0, 99_999_999)
    return f"{prefix}{suffix:08d}"


def _email() -> str:
    """生成像真的邮箱地址。"""
    user = random.choice(_EMAIL_USER_PREFIXES)
    # 30% 概率加数字后缀，避免生成重复
    if random.random() < 0.5:
        user += str(random.randint(1, 9999))
    domain = random.choice(_EMAIL_DOMAINS)
    return f"{user}@{domain}"


def _chinese_address() -> str:
    """生成像真的中文地址：城市 + 路名 + 号。"""
    city = random.choice(_CITIES)
    street_prefix = random.choice(_STREET_PREFIXES)
    street_type = random.choice(_STREET_TYPES)
    number = random.randint(1, 999)
    # 30% 概率加小区/大厦名
    extra = ""
    if random.random() < 0.4:
        estates = [
            f"{random.choice(['阳光', '金色', '翠湖', '御景', '龙湖', '保利', '万科', '万达'])}"
            f"{random.choice(['花园', '小区', '家园', '公寓', '大厦', '广场'])}",
        ]
        building = random.randint(1, 30)
        room = random.randint(101, 3209)
        extra = f"{random.choice(estates)}{building}栋{room}室 "
    return f"{city}{street_prefix}{street_type}{number}号 {extra}".strip()


# 常用随机中文短句（自由填空）
_SHORT_SENTENCES = (
    "整体体验非常不错，希望下次还能再来",
    "服务态度很好，环境也比较满意",
    "性价比高，值得推荐给朋友",
    "一般般吧，没有想象的那么好",
    "感觉还可以，基本符合预期",
    "客服很耐心，问题都解决了",
    "配送速度快，包装完整",
    "功能齐全，操作方便简单",
    "学习到了很多新知识，收获满满",
    "老师讲课生动有趣，氛围很好",
    "交通便利，周边配套设施完善",
    "会继续支持，期待更多活动",
    "产品设计合理，使用起来很顺手",
    "质量过硬，用了一段时间都没问题",
    "价格合理，比预期的要便宜",
)


def _random_sentence(min_len: int = 5) -> str:
    """随机中文短句。"""
    s = random.choice(_SHORT_SENTENCES)
    if len(s) >= min_len:
        return s
    return s + "，" + random.choice(_SHORT_SENTENCES)


# ============================================================================
#  内部工具：通用加权/等权重抽样
# ============================================================================
def _weighted_or_equal_choice(
    indices: list[int],
    weights: Optional[list[float]] = None,
) -> int:
    """单个选项：有权重按权重抽，否则均匀。"""
    if weights and len(weights) == len(indices) and sum(w for w in weights if w > 0) > 0:
        return random.choices(indices, weights=weights, k=1)[0]
    return random.choice(indices)


def _weighted_multi(
    indices: list[int],
    weights: Optional[list[float]],
    k: int,
) -> list[int]:
    """多选：无放回加权抽样。numpy 优先，否则纯 Python A-Res 算法。"""
    n = len(indices)
    k = max(1, min(k, n))

    # 分支 A：有 numpy → 经典概率法
    if _HAS_NUMPY:
        if weights and len(weights) == n and sum(w for w in weights if w > 0) > 0:
            total = sum(weights)
            probs = [w / total for w in weights]
            return sorted(
                np.random.choice(indices, size=k, replace=False, p=probs).tolist()
            )
        return sorted(np.random.choice(indices, size=k, replace=False).tolist())

    # 分支 B：纯 Python —— A-Res 算法（Efraimidis & Spirakis, WRes 无放回加权抽样）
    # key = u^(1/w)，取 top-k，保证概率正比于权重
    safe_ws = list(weights) if weights and len(weights) == n else [1.0] * n
    keys: list[tuple[float, int]] = []
    for w, idx in zip(safe_ws, indices):
        w_pos = max(w if w > 0 else 1e-12, 1e-12)
        u = random.random()            # (0, 1) 均匀
        # math.log 防下溢：key = log(u) / w → 排序等价于 u^(1/w)
        keys.append((math.log(u) / w_pos, idx))
    keys.sort(reverse=True)  # top-k 在最前
    return sorted(idx for _, idx in keys[:k])


def _cfg_weights(q: dict) -> Optional[list[float]]:
    """优先从 q['weights'] 取，其次从全局 WEIGHT_CONFIG 取。"""
    if "weights" in q and q["weights"]:
        return list(q["weights"])
    qi = q.get("q")
    if qi is not None and isinstance(qi, int):
        cfg = WEIGHT_CONFIG.get(qi)
        if cfg and "weights" in cfg:
            return list(cfg["weights"])
    return None


# ============================================================================
#  Public API
# ============================================================================
def generate_answer(question: dict) -> dict[str, Any]:
    """根据题目结构生成对应题型的随机答案。

    :param question: 结构至少包含 ``type``；各题型的额外键见上方 docstring。
    :return: 统一 dict 结构（见模块文档）。
    """
    qtype = str(question.get("type", "single")).lower()

    # --- 1. 选择题 v1 兼容 ---------------------------------------------
    if qtype in ("single", "radio"):
        choices = list(question["choices"])
        indices = list(range(len(choices)))
        picked_idx = _weighted_or_equal_choice(indices, _cfg_weights(question))
        return {"type": "single", "selected": [choices[picked_idx]]}

    if qtype in ("multi", "checkbox"):
        choices = list(question["choices"])
        indices = list(range(len(choices)))
        # 决定选几个
        qi = question.get("q")
        cfg = WEIGHT_CONFIG.get(qi) if isinstance(qi, int) else None
        n = len(indices)
        if cfg and "count_options" in cfg:
            count_opts = list(cfg["count_options"])
            count_wts = list(cfg.get("count_weights", [1] * len(count_opts)))
            k = random.choices(count_opts, weights=count_wts, k=1)[0]
        else:
            k = random.randint(1, max(1, n))
        picked_indices = _weighted_multi(indices, _cfg_weights(question), k)
        return {"type": "multi", "selected": [choices[i] for i in picked_indices]}

    # --- 2. 下拉（等价单选） -------------------------------------------
    if qtype == "dropdown":
        choices = list(question["choices"])
        indices = list(range(len(choices)))
        picked_idx = _weighted_or_equal_choice(indices, _cfg_weights(question))
        return {"type": "dropdown", "selected": [choices[picked_idx]]}

    # --- 3. 量表打分 ---------------------------------------------------
    if qtype in ("scale", "rating"):
        scale_max = int(question.get("scale", 5))
        scale_min = int(question.get("scale_min", 1))
        indices = list(range(scale_min, scale_max + 1))
        # 权重：1→1分, 2→2分 ... 必须对齐 indices
        w = _cfg_weights(question)
        if w and len(w) == len(indices):
            value = random.choices(indices, weights=w, k=1)[0]
        else:
            # 默认轻微偏向中上（4/5 概率更高）——符合真实打分习惯
            default_w = [1, 2, 4, 6, 5][: len(indices)]
            if len(default_w) < len(indices):
                # 补齐
                default_w = default_w + [max(default_w)] * (len(indices) - len(default_w))
            value = random.choices(indices, weights=default_w[: len(indices)], k=1)[0]
        return {"type": "scale", "value": int(value)}

    # --- 4. 填空 -------------------------------------------------------
    if qtype in ("text", "input", "textarea", "fillblank"):
        field = str(question.get("field", "")).lower() or None

        # 如果给出显式选项池 → 从池中随机
        explicit_options = question.get("options")
        if isinstance(explicit_options, (list, tuple)) and len(explicit_options) > 0:
            picked = random.choice(list(explicit_options))
            return {"type": "text", "text": str(picked), "field": field}

        if field == "name":
            text = _chinese_name()
        elif field in ("phone", "mobile", "tel"):
            text = _chinese_phone()
        elif field == "email":
            text = _email()
        elif field in ("address", "addr"):
            text = _chinese_address()
        elif field in ("age", "number"):
            # 年龄数字范围：16~70
            text = str(random.randint(int(question.get("min", 16)), int(question.get("max", 70))))
        elif field in ("company", "org"):
            suffixes = "科技有限公司 信息咨询有限公司 贸易有限公司 文化传播有限公司 网络服务公司".split()
            prefixes = "中瑞 华盛 远景 星辰 蓝海 卓越 飞扬 鼎盛 合众 博远 天启 晨光".split()
            text = random.choice(prefixes) + random.choice(suffixes)
        else:
            text = _random_sentence()

        return {"type": "text", "text": text, "field": field}

    # --- 5. 矩阵单选 ---------------------------------------------------
    if qtype in ("matrix_single", "matrix"):
        rows = list(question.get("rows", []))
        cols = list(question.get("cols", []))
        col_indices = list(range(len(cols)))
        row_weights = question.get("row_weights") or {}
        result: dict[int, Any] = {}
        for r in rows:
            w = row_weights.get(r) if isinstance(row_weights, dict) else None
            idx = _weighted_or_equal_choice(col_indices, list(w) if w else None)
            result[r] = cols[idx]
        return {"type": "matrix_single", "rows": result}

    # --- 兜底：当作单选题处理（保守） ----------------------------------
    if "choices" in question:
        choices = list(question["choices"])
        return {"type": "single", "selected": [random.choice(choices)]}

    raise ValueError(f"[answering_v2] 不支持的题型: {qtype!r}，且无 choices 可兜底")
