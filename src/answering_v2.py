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

import datetime as _dt
import random
from typing import Any, Optional

from . import anchoring, distribution, plan
from .persona import Persona, active_persona
from .utils import (
    sanitize_weights,
    weighted_sample_no_replace,
    weights_are_usable,
)


# ============================================================================
#  人口学填空：全部从 src.persona 的当前画像取（v3.2）
# ============================================================================
# 这里刻意不再放任何随机池。此前姓名 / 手机 / 邮箱 / 地址是四个各自随机的池子，
# 于是同一份问卷可以同时出现「女名池的张伟 · 深圳地址 · 3 岁 · 乌鲁木齐公司」——
# 每格单看合法，连起来是个不存在的人。



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


def _parse_date_limit(raw: Any) -> _dt.datetime | None:
    """把 laydate 的边界串（或我们刚生成的日期串）解析成 datetime；认不出返回 None。"""
    text = str(raw or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m"):
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _random_date_text(
    kind: str = "date",
    lo: Any = None,
    hi: Any = None,
) -> str:
    """按 laydate 的粒度与区间生成一个日期串（v3.1 日期题）。

    区间必须照守：探测把 ``datelimit`` 一起带回来，就是因为范围外的值会被平台自己的
    校验清掉 —— 辛辛苦苦填一个，交上去那题还是空的。两端都没给时落在 **18~55 年前**：
    真卷上这道题的题干是"请输入您的出生日期"，而 laydate 的默认边界宽到
    1900~2099，随手随机到今天附近会答出一个婴儿的生日。
    """
    fmt_by_kind = {
        "month": "%Y-%m",
        "datetime": "%Y-%m-%d %H:%M",
        "time": "%H:%M",
    }
    now = _dt.datetime.now()
    if kind == "time":
        return (now.replace(hour=random.randint(8, 21), minute=random.randrange(60))
                ).strftime("%H:%M")

    low = _parse_date_limit(lo) or (now - _dt.timedelta(days=365 * 55))
    high = _parse_date_limit(hi) or (now - _dt.timedelta(days=365 * 18))
    if high < low:
        low, high = high, low
    if high <= low:
        picked = low
    else:
        span = int((high - low).total_seconds())
        picked = low + _dt.timedelta(seconds=random.randint(0, max(span, 1)))
    return picked.strftime(fmt_by_kind.get(kind, "%Y-%m-%d"))


_DATE_FMT_BY_KIND = {"month": "%Y-%m", "datetime": "%Y-%m-%d %H:%M"}


def _birth_or_window_date(question: dict, p: Persona) -> str:
    """生日 / 日期类填空优先答画像的出生日期。

    WHY：这一格与年龄、身份证中间那 8 位是**同一个人的同一件事**，各抽一次必然
    对不上（v3.1 之前就是这样：年龄 16~70 随机、日期按 laydate 区间随机）。
    只有当 ``datelimit`` 区间容不下这一天时才退回区间内随机 —— 越界会被平台自己的
    校验清掉，那比不一致更糟（宁可少一致，不能交上去是空的）。
    """
    kind = str(question.get("date_kind") or "date")
    if kind == "time":
        return _random_date_text(kind)
    fmt = _DATE_FMT_BY_KIND.get(kind, "%Y-%m-%d")
    want = p.birth_date_text(fmt)
    got = _parse_date_limit(want)
    low = _parse_date_limit(question.get("date_min"))
    high = _parse_date_limit(question.get("date_max"))
    if got is not None and (low is None or got >= low) and (high is None or got <= high):
        return want
    return _random_date_text(kind, question.get("date_min"), question.get("date_max"))


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
def _drift_weights(
    qnum: Optional[int],
    weights: Optional[list[float]],
    n: int,
) -> Optional[list[float]]:
    """投递分布纠正开着时，把目标权重按已落地的缺口小幅修正。

    关着时原样返回，所以默认路径与 v3.1 逐位一致。配置权重长度与选项数不符时
    退回等权 —— 错位修正比不修正更糟。

    **信度计划优先于一切**：``plan`` 给这一题这一份定了选项，就直接 one-hot。
    复用加权采样这条路而不是另开一条，是因为点击、DOM 回读、落盘、重试全都挂在
    "生成一个答案字典"这个出口上 —— 多开一条路就多一处会忘记校验的地方。
    """
    if qnum is not None:
        forced = plan.forced_choice(qnum)
        if forced is not None and 0 <= forced < n:
            return [1.0 if i == forced else 0.0 for i in range(n)]
    if qnum is None or not distribution.control_enabled():
        return weights
    base = list(weights) if weights and len(weights) == n else [1.0] * n
    return distribution.adjust(qnum, base)


def _weighted_or_equal_choice(
    indices: list[int],
    weights: Optional[list[float]] = None,
    qnum: Optional[int] = None,
) -> int:
    """单个选项：有权重按权重抽，否则均匀。"""
    weights = _drift_weights(qnum, weights, len(indices))
    if weights_are_usable(weights, len(indices)):
        return random.choices(indices, weights=weights, k=1)[0]
    return random.choice(indices)


def _weighted_multi(
    indices: list[int],
    weights: Optional[list[float]],
    k: int,
    qnum: Optional[int] = None,
) -> list[int]:
    """多选：无放回加权抽样。

    实现已上收到 ``utils.weighted_sample_no_replace``（v2.6）——
    此前 answering.py 里有一份算法相同但**守卫不同**的副本，
    那份缺非法权重保护，全 0 权重直接 ZeroDivisionError。
    """
    return weighted_sample_no_replace(
        list(indices), _drift_weights(qnum, weights, len(indices)), k
    )


def _weighted_permutation(
    items: list[Any],
    weights: Optional[list[float]],
    qnum: Optional[int] = None,
) -> list[Any]:
    """按权重无放回地逐个取出 → 得到一个**保持取出顺序**的排列（v3.0 排序题）。

    不能复用 ``utils.weighted_sample_no_replace``：那个函数刻意返回升序结果
    （多选题的选项序号要稳定），而排序题要的正是"谁被先抽中谁排第一"。
    权重全非法（长度不符 / NaN / 总和 0）时退化成均匀洗牌。
    """
    pool = list(items)
    weights = _drift_weights(qnum, weights, len(pool))
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
    # 投递分布纠正按题号记账（见 src/distribution.py）；没有题号时传 None，
    # 采样器会原样用目标权重 —— 宁可少纠正，不可对错账纠正。
    try:
        qnum: int | None = int(question.get("q", 0) or 0) or None
    except (TypeError, ValueError):
        qnum = None

    # --- 1. 选择题 v1 兼容 ---------------------------------------------
    if qtype in ("single", "radio"):
        choices = list(question["choices"])
        indices = list(range(len(choices)))
        picked_idx = _weighted_or_equal_choice(indices, _cfg_weights(question), qnum)
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
        picked_indices = _weighted_multi(indices, _cfg_weights(question), k, qnum)
        return {"type": "multi", "selected": [choices[i] for i in picked_indices]}

    # --- 2. 下拉（等价单选） -------------------------------------------
    if qtype == "dropdown":
        choices = list(question["choices"])
        indices = list(range(len(choices)))
        picked_idx = _weighted_or_equal_choice(indices, _cfg_weights(question), qnum)
        return {"type": "dropdown", "selected": [choices[picked_idx]]}

    # --- 3. 量表打分 ---------------------------------------------------
    if qtype in ("scale", "rating"):
        scale_max = int(question.get("scale", 5))
        scale_min = int(question.get("scale_min", 1))
        indices = list(range(scale_min, scale_max + 1))
        # 权重：1→1分, 2→2分 ... 必须对齐 indices
        w = _cfg_weights(question)
        if not weights_are_usable(w, len(indices)):
            # 默认轻微偏向中上（4/5 概率更高）——符合真实打分习惯
            w = [1, 2, 4, 6, 5][: len(indices)]
            if len(w) < len(indices):
                # 补齐
                w = list(w) + [max(w)] * (len(indices) - len(w))
        value = _weighted_or_equal_choice(indices, list(w or []), qnum)
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

        p = active_persona()
        if field == "name":
            text = p.name
        elif field in ("phone", "mobile", "tel"):
            text = p.phone
        elif field == "email":
            text = p.email
        elif field in ("address", "addr"):
            text = p.address_text
        elif field in ("region", "area", "local"):
            text = p.region()
        elif field in ("age", "number"):
            # 画像的年龄优先；只有它落在题目 min/max 之外才夹回去 —— 夹完仍是个
            # 合法年龄，只是这一格与身份证 / 生日的自洽性弱一档。
            lo, hi = int(question.get("min", 16)), int(question.get("max", 70))
            text = str(min(max(p.age, min(lo, hi)), max(lo, hi)))
        elif field == "date":
            text = _birth_or_window_date(question, p)
        elif field in ("company", "org"):
            text = p.company
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
            idx = _weighted_or_equal_choice(col_indices, list(w) if w else None, qnum)
            result[r] = cols[idx]
        return {"type": "matrix_single", "rows": result}

    # --- 5a2. 矩阵量表（v3.1）-------------------------------------------
    # rows 是**提交槽名**（平台标在 tr[fid] 上，形如 q7_0），cols 是 dval 分值。
    # 与矩阵单选共用"每行一个标量"的形状，但控件完全不同（一排可点的 <a>），
    # 所以单独一型：混进 matrix_single 会让探测与作答各走两套选择器。
    if qtype == "matrix_scale":
        rows = list(question.get("rows", []))
        cols = list(question.get("cols", []))
        col_indices = list(range(len(cols)))
        w = _cfg_weights(question)
        result_scale: dict[Any, Any] = {}
        for r in rows:
            idx = _weighted_or_equal_choice(col_indices, w, qnum)
            result_scale[r] = cols[idx]
        return {"type": "matrix_scale", "rows": result_scale}

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
            picked = _weighted_multi(col_indices, list(w) if w else None, k, qnum)
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
            order = _weighted_permutation(items, _cfg_weights(question), qnum)
        return {"type": "sort", "order": order, "items": items}

    # --- 兜底：当作单选题处理（保守） ----------------------------------
    if "choices" in question:
        choices = list(question["choices"])
        return {"type": "single", "selected": [random.choice(choices)]}

    raise ValueError(f"[answering_v2] 不支持的题型: {qtype!r}，且无 choices 可兜底")
