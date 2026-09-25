"""权重表第 4 列的字符串 → ``WEIGHT_CONFIG`` dict —— webui 自己的一份实现。

设计稿：``docs/design/DESIGN_webui.md`` §5 末与 §10 步骤 4。

**为什么是复制而不是共用**：本轮 Tk 是参照实现。先各写一份、用对拍测试逐题型比过，
才知道两边是不是真的在做同一件事；一上来就抽成共用函数，对拍退化成"自己等于自己"，
parity 就没有证据了。等对拍绿了再把两份合成一份（步骤 4 的 4b）。

规则以 ``gui/weight_panel.py`` 为准，逐条对应：

  - 单选/多选/下拉：逗号分 float；格式错、个数不符、负数/NaN/Inf → 记 WARN 并**跳过该题**；
    留空 → 整条不进 cfg，运行时退化成等权随机。
  - 量表：写一个整数 = 强制打那一级（越界会算出全 0 权重，因此补一条 WARN）；
    逗号列表长度不符 → 补 0 / 截断并 WARN（**不**丢题）。
  - 填空：逗号分成候选文本；留空表示按字段类型自动生成。
  - 矩阵：``1:w1,w2 | 2:w1,w2``；任何一行写坏 → 整份 row_weights 作废（附示例），
    rows/cols 结构仍然保留。
  - 排序：逗号分 item id；出现没探测到的 id → 整个顺序作废（不部分接受）。
"""

from __future__ import annotations

import math
from typing import Any

from src.models import normalize_question_type

_CHOICE_TYPES = ("single", "radio", "multi", "checkbox", "dropdown")
_SCALE_TYPES = ("scale", "rating", "nps")
_TEXT_TYPES = ("text", "input", "textarea", "fillblank")
_MATRIX_TYPES = ("matrix_single", "matrix", "matrix_multi")
_SORT_TYPES = ("sort", "ordering", "rank")


def _bad_number(x: float) -> bool:
    return x < 0 or math.isnan(x) or math.isinf(x)


def _floats(raw: str) -> list[float] | None:
    try:
        return [float(p) for p in [s.strip() for s in raw.split(",") if s.strip()]]
    except ValueError:
        return None


def _choice_entry(q: dict, raw: str, warn) -> dict | None:
    n_opts = len(q.get("choices", []))
    if not raw:
        return None                                   # 留空 = 等权重，不落条目
    weights = _floats(raw)
    if weights is None:
        warn(f"Q{q['q']} 权重格式错误，已跳过：{raw}")
        return None
    if len(weights) != n_opts and n_opts > 0:
        warn(f"Q{q['q']} 权重个数 {len(weights)} 与选项数 {n_opts} 不符，已跳过")
        return None
    # 探测没给出选项的 single/dropdown 放行 —— 与 Tk 宿主一致
    if any(_bad_number(w) for w in weights):
        warn(f"Q{q['q']} 权重含负数或非有限值，已跳过")
        return None
    return {"type": q["type"], "weights": weights}


def _scale_entry(q: dict, raw: str, warn) -> dict:
    n = int(q.get("scale", 5))
    smin = int(q.get("scale_min", 1))
    entry: dict[str, Any] = {"type": q["type"], "scale": n, "scale_min": smin}
    if not raw:
        return entry
    stripped = raw.strip()
    if stripped.isdigit():
        pick = int(stripped)
        weights = [0.0] * n
        if 1 <= pick <= n:
            weights[pick - 1] = 1.0
        else:
            warn(f"Q{q['q']} 指定的分值 {pick} 超出量表范围 {smin}~{n}，"
                 "该题将没有可选中的分值")
        entry["weights"] = weights
        return entry
    weights = _floats(stripped)
    if weights is None:
        warn(f"Q{q['q']} 量表权重格式错误，已只保留量表结构：{raw}")
        return entry
    if len(weights) != n:
        warn(f"Q{q['q']} 量表权重个数 {len(weights)} 与级数 {n} 不符，已自动补齐/截断")
        weights = (weights + [0.0] * n)[:n]
    if any(_bad_number(w) for w in weights):
        warn(f"Q{q['q']} 量表权重含负数或非有限值，已只保留量表结构")
        return entry
    entry["weights"] = weights
    return entry


def _text_entry(q: dict, raw: str, warn) -> dict:
    entry: dict[str, Any] = {"type": q["type"]}
    if q.get("field"):
        entry["field"] = q["field"]
    if raw.strip():
        entry["options"] = [p.strip() for p in raw.split(",") if p.strip()]
    return entry


def _matrix_entry(q: dict, raw: str, warn) -> dict:
    entry: dict[str, Any] = {
        "type": q["type"],
        "rows": list(q.get("rows", [])),
        "cols": list(q.get("cols", [])),
    }
    if not raw.strip():
        return entry
    example = "1:0,0,0.1,0.4,0.5 | 2:0.1,0.2,0.3,0.3,0.1"
    row_weights: dict[str, list[float]] = {}
    for segment in raw.split("|"):
        segment = segment.strip()
        if not segment or ":" not in segment:
            warn(f"Q{q['q']} 矩阵格式缺少行号分隔符 ':'，整份行权重作废。"
                 f"正确格式示例：{example}")
            return entry
        key, _, values = segment.partition(":")
        floats = _floats(values)
        if floats is None or any(_bad_number(w) for w in floats):
            warn(f"Q{q['q']} 矩阵第 {key.strip()} 行权重非法，整份行权重作废。"
                 f"正确格式示例：{example}")
            return entry
        row_weights[key.strip()] = floats
    entry["row_weights"] = row_weights
    return entry


def _sort_entry(q: dict, raw: str, warn) -> dict:
    entry: dict[str, Any] = {"type": q["type"]}
    items = [str(i) for i in q.get("items", [])]
    order = [p.strip() for p in raw.split(",") if p.strip()]
    if not order:
        return entry                                   # 留空 = 整题随机排序
    unknown = [o for o in order if o not in items]
    if unknown:
        warn(f"Q{q['q']} 排序含未探测到的项 {unknown}，整个顺序作废（改为随机排序）")
        return entry
    entry["order"] = order
    return entry


def _text_for(texts: dict, qi: Any) -> str:
    """题号在界面里是 int、从 JSON 配置回来时是 str —— 两种都要认。"""
    try:
        key = int(qi)
    except (TypeError, ValueError):
        return ""
    if key in texts:
        return str(texts[key] or "")
    return str(texts.get(str(key), "") or "")


# 存储名 → 展示标签。与 gui/weight_panel 的胶囊文案对齐，但**这是 webui 自己的一份**
# （理由同本模块开头：本轮 Tk 是参照实现，共用会让对拍失去独立性）。
TYPE_LABELS: dict[str, str] = {
    "single": "单选",
    "multi": "多选",
    "dropdown": "下拉",
    "scale": "量表",
    "text": "填空",
    "matrix": "矩阵",
    "matrix_multi": "矩多",
    "sort": "排序",
}

_FIELD_LABELS = {
    "name": "姓名字段", "phone": "手机字段", "mobile": "手机字段",
    "tel": "手机字段", "email": "邮箱字段",
    "address": "地址字段", "addr": "地址字段", "age": "年龄字段",
    "company": "公司字段", "org": "公司字段",
}


def type_label(qtype: Any) -> str:
    return TYPE_LABELS.get(normalize_question_type(str(qtype or "")), "其它")


def default_text_for(q: dict) -> str:
    """探测完给每行预填的默认串 —— 留空即"等权重随机"，所以矩阵/排序不预填。"""
    storage = normalize_question_type(str(q.get("type", "single")))
    if storage in _CHOICE_TYPES:
        n = len(q.get("choices", []))
        if not n:
            return ""
        return ",".join(f"{1.0 / n:.4f}" for _ in range(n))
    if storage in _SCALE_TYPES:
        return ",".join(["1"] * int(q.get("scale", 5) or 0))
    if storage in _TEXT_TYPES:
        return ",".join(str(x) for x in (q.get("options") or []))
    return ""


def scale_label(q: dict) -> str:
    """第 3 列"选项/空数"该写什么。"""
    storage = normalize_question_type(str(q.get("type", "single")))
    if storage in _CHOICE_TYPES:
        return str(len(q.get("choices", [])))
    if storage in _SCALE_TYPES:
        n = int(q.get("scale", 5) or 0)
        return f"{int(q.get('scale_min', 1) or 1)}~{n}"
    if storage in _TEXT_TYPES:
        return _FIELD_LABELS.get(str(q.get("field") or ""), "自由文本")
    if storage in _MATRIX_TYPES:
        return f"{len(q.get('rows', []))}行 × {len(q.get('cols', []))}列"
    if storage in _SORT_TYPES:
        return f"{len(q.get('items', []))} 项可排"
    return str(len(q.get("choices", [])))


def parse_weights(questions: list[dict], texts: dict[int, str]
                  ) -> tuple[dict, list[str]]:
    """返回 ``(cfg, warnings)``。warnings 是要往日志里刷的人话，不阻断。"""
    warnings: list[str] = []
    warn = warnings.append
    cfg: dict[Any, dict] = {}

    for q in questions:
        qi = q.get("q")
        if qi is None:
            continue
        qtype = str(q.get("type", "single")).lower()
        raw = _text_for(texts, qi).strip()
        storage = normalize_question_type(qtype)

        if storage in _CHOICE_TYPES:
            entry = _choice_entry({**q, "type": storage}, raw, warn)
        elif storage in _SCALE_TYPES:
            entry = _scale_entry({**q, "type": storage}, raw, warn)
        elif storage in _TEXT_TYPES:
            entry = _text_entry({**q, "type": storage}, raw, warn)
        elif storage in _MATRIX_TYPES:
            entry = _matrix_entry({**q, "type": storage}, raw, warn)
        elif storage in _SORT_TYPES:
            entry = _sort_entry({**q, "type": storage}, raw, warn)
        elif raw:
            floats = _floats(raw)
            if floats is None:
                warn(f"Q{qi} 未知题型 {qtype} 且权重格式错误，已跳过：{raw}")
                continue
            entry = {"type": q.get("type"), "weights": floats}
        else:
            continue

        if entry is None:
            continue            # 留空/格式错：这道题不进 cfg，交给引擎的等权重分支

        anchor = q.get("anchor")
        if anchor:
            entry["anchor"] = anchor
        cfg[int(qi)] = entry

    return cfg, warnings


__all__ = ["parse_weights", "default_text_for", "scale_label", "type_label",
           "TYPE_LABELS"]
