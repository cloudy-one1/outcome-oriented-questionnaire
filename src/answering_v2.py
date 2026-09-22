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

import random
from typing import Any, Optional

from . import anchoring
from .utils import (
    sanitize_weights,
    weighted_sample_no_replace,
    weights_are_usable,
)


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


# 选项自带填空框（"其他____"）时填的短文本。
# 不复用 _SHORT_SENTENCES：那是 15~20 字的整句，而这一格通常紧贴在选项文字后面，
# 宽几十像素、还常带 maxlength。填整句既不像这项的答案（读起来像评论），
# 也容易被浏览器的长度上限截掉。
_OPTION_BLANK_TEXTS = (
    "其他", "其他原因", "个人原因", "其他方面", "暂时没有", "不太清楚", "以上都不是",
)


def generate_option_blank_text() -> str:
    """为"其他____"这类**选项自带**的填空框生成短文本（v3.0）。

    公开给 ``pipeline_stages.question_stage`` 用：多选题勾中带填空的项时，
    必须同时往那一格写字，否则平台按"该项未填写"拦下整题。
    """
    return random.choice(_OPTION_BLANK_TEXTS)


# ============================================================================
#  内部工具：通用加权/等权重抽样
# ============================================================================
def _weighted_or_equal_choice(
    indices: list[int],
    weights: Optional[list[float]] = None,
) -> int:
    """单个选项：有权重按权重抽，否则均匀。"""
    if weights_are_usable(weights, len(indices)):
        return random.choices(indices, weights=weights, k=1)[0]
    return random.choice(indices)


def _weighted_multi(
    indices: list[int],
    weights: Optional[list[float]],
    k: int,
) -> list[int]:
    """多选：无放回加权抽样。

    实现已上收到 ``utils.weighted_sample_no_replace``（v2.6）——
    此前 answering.py 里有一份算法相同但**守卫不同**的副本，
    那份缺非法权重保护，全 0 权重直接 ZeroDivisionError。
    """
    return weighted_sample_no_replace(list(indices), weights, k)


def _weighted_permutation(
    items: list[Any],
    weights: Optional[list[float]],
) -> list[Any]:
    """按权重无放回地逐个取出 → 得到一个**保持取出顺序**的排列（v3.0 排序题）。

    不能复用 ``utils.weighted_sample_no_replace``：那个函数刻意返回升序结果
    （多选题的选项序号要稳定），而排序题要的正是"谁被先抽中谁排第一"。
    权重全非法（长度不符 / NaN / 总和 0）时退化成均匀洗牌。
    """
    pool = list(items)
    if weights is None or not weights_are_usable(weights, len(pool)):
        random.shuffle(pool)
        return pool

    w = [float(x) for x in weights]
    out: list[Any] = []
    while pool:
        if sum(v for v in w if v > 0) <= 0:
            idx = random.randrange(len(pool))
        else:
            idx = random.choices(range(len(pool)), weights=w, k=1)[0]
        out.append(pool.pop(idx))
        w.pop(idx)
    return out


def _cfg_entry(q: dict) -> dict:
    """该题生效的配置条目（v3.0：锚点优先、题号兜底；没配置则空 dict）。"""
    return anchoring.lookup_weight_entry(q) or {}


def _cfg_weights(q: dict) -> Optional[list[float]]:
    """优先从 q['weights'] 取，其次按「锚点 → 题号」查全局配置（v3.0 见 anchoring）。"""
    if "weights" in q and q["weights"]:
        return list(q["weights"])
    cfg = _cfg_entry(q)
    if cfg and "weights" in cfg:
        return list(cfg["weights"])
    return None


def _row_weight_map(raw: Any) -> dict:
    """把 row_weights 的行号键统一成 int。

    两条来源的键类型天然不同：GUI 权重表里敲出来的是 ``"1"``，
    而 ``load_weight_config`` 会转成 ``1``，探测到的 rows 又一定是 int。
    不统一的话 ``row_weights.get(1)`` 在 GUI 那条路上永远查不到 → 静默等权。
    """
    out: dict = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                out[int(k)] = v
            except (TypeError, ValueError):
                out[k] = v
    return out


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
        cfg = _cfg_entry(question)
        n = len(indices)
        if cfg and "count_options" in cfg:
            count_opts = list(cfg["count_options"])
            count_wts = sanitize_weights(
                list(cfg.get("count_weights", [1] * len(count_opts))),
                len(count_opts),
                question=qi if isinstance(qi, int) else None,
                label="选中个数权重",
            )
            k = random.choices(count_opts, weights=count_wts, k=1)[0] \
                if count_wts else random.choice(count_opts)
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
        if weights_are_usable(w, len(indices)):
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

        # 如果给出显式选项池 → 从池中随机。
        # v3.0：候选词也能只写在权重配置里（GUI 表格 / JSON 的 "options"）——
        # 此前只看 question["options"]，而探测回来的题目**永远没有**这个键，
        # 于是用户填的候选词全部静默失效、每次都走内置随机生成。
        explicit_options = question.get("options") or _cfg_entry(question).get("options")
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
        # v3.0：行权重同样要能从权重配置里取到 —— 探测回来的题目没有 row_weights，
        # 此前矩阵题的权重只能显示、不能生效（README 的 matrix 配置项形同虚设）。
        row_weights = _row_weight_map(
            question.get("row_weights") or _cfg_entry(question).get("row_weights")
        )
        result: dict[int, Any] = {}
        for r in rows:
            w = row_weights.get(r) if isinstance(row_weights, dict) else None
            idx = _weighted_or_equal_choice(col_indices, list(w) if w else None)
            result[r] = cols[idx]
        return {"type": "matrix_single", "rows": result}

    # --- 5b. 矩阵多选（v3.0）--------------------------------------------
    if qtype == "matrix_multi":
        rows = list(question.get("rows", []))
        cols = list(question.get("cols", []))
        entry = _cfg_entry(question)
        row_weights = _row_weight_map(
            question.get("row_weights") or entry.get("row_weights")
        )
        # 每行勾几个：默认 1。平台的矩阵多选常带"每行至多选 N 个"，而 1 永远同时
        # 满足"至少一个"和"至多 N 个"；要多勾就配 pick_options / pick_weights
        # （与多选的 count_options 同形）。k 超过列数时由抽样函数夹到 len(cols)。
        pick_opts = question.get("pick_options") or entry.get("pick_options") or [1]
        pick_opts = [int(x) for x in pick_opts]
        pick_wts = question.get("pick_weights") or entry.get("pick_weights")
        col_indices = list(range(len(cols)))
        result_multi: dict[int, list] = {}
        for r in rows:
            w = row_weights.get(r)
            k = (
                random.choices(pick_opts, weights=pick_wts, k=1)[0]
                if weights_are_usable(pick_wts, len(pick_opts))
                else random.choice(pick_opts)
            )
            picked = _weighted_multi(col_indices, list(w) if w else None, k)
            result_multi[r] = [cols[i] for i in picked]
        return {"type": "matrix_multi", "rows": result_multi}

    # --- 6. 排序题（v3.0）-----------------------------------------------
    if qtype == "sort":
        items = [str(x) for x in (question.get("items") or [])]
        entry = _cfg_entry(question)
        fixed = question.get("order") or entry.get("order")
        if fixed:
            head = [str(x) for x in fixed if str(x) in items]
            # 用户只写了前几项 → 其余随机补在后面，而不是丢掉：
            # 排序题要求每一项都有一个位置，漏一项就是整题无效。
            rest = _weighted_permutation(
                [x for x in items if x not in set(head)], None
            )
            order = head + rest
        else:
            order = _weighted_permutation(items, _cfg_weights(question))
        return {"type": "sort", "order": order, "items": items}

    # --- 兜底：当作单选题处理（保守） ----------------------------------
    if "choices" in question:
        choices = list(question["choices"])
        return {"type": "single", "selected": [random.choice(choices)]}

    raise ValueError(f"[answering_v2] 不支持的题型: {qtype!r}，且无 choices 可兜底")
