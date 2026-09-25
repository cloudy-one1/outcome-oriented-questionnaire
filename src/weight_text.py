"""权重表第 4 列的文本 → ``WEIGHT_CONFIG`` dict —— **两个宿主共用的唯一一份**。

设计稿：``docs/design/DESIGN_webui.md`` §5 末与 §10 步骤 4。

这条链是"先各写一份、对拍、再共用"走出来的，不是直接抽出来的：
``webui/weights.py`` 曾独立实现过一遍，与桌面版 ``gui/weight_panel.py`` 逐题型对拍
（``tests/test_weight_parser_parity.py``）之后才合成这一份。对拍抓到三条只有两份
实现才会暴露的问题 —— 锚点整份缺失、矩阵空行号被接受、结尾多一个 ``|`` 毁掉整份
行权重。先共用再对拍的话，这三条一条都看不见（自己等于自己）。

语义（``gui/weight_panel`` 是参照，三条刻意与它不同，理由见下）：

  - 单选/多选/下拉：逗号分 float；格式错、个数不符、负数/NaN/Inf → 记 WARN 并**跳过该题**；
    留空 → 整条不进 cfg，运行时退化成等权随机。
  - 量表：写一个整数 = 强制打那一级（越界会算出全 0 权重，因此补一条 WARN —— 桌面版
    原来一句都不说）；逗号列表长度不符 → 补 0 / 截断并 WARN；**格式错也只保留量表结构**。
    桌面版原来在格式错时 ``continue`` 掉整题，与它自己"含负数只弃权重、保留结构"那条
    分支的说明相互矛盾，这里统一到后者。
  - ``scale_min`` **恒写**：桌面版原来是 ``if smin:``，于是 0~10 与 2~10 这类量表的下限
    被丢掉，而 ``config_io`` 的校验按缺省 1 反推长度 —— 一份完全正确的配置会被报成
    "weights 长度与量表范围不匹配"。
  - ``type`` 写 ``models.normalize_question_type`` 的**存储名**（单一真相）：探测输出
    ``matrix_single`` 时，这里落 ``matrix``。
  - 填空：逗号分成候选文本；留空表示按字段类型自动生成。
  - 矩阵：``1:w1,w2 | 2:w1,w2``；任何一行写坏（含缺行号）→ 整份 row_weights 作废并附示例，
    rows/cols 结构仍然保留；空段（结尾多一个 ``|``）跳过而不是作废。
  - 排序：逗号分 item id；出现没探测到的 id → 整个顺序作废（不部分接受）。
  - 每条配置附题干锚点（``anchoring.make_anchor``）：题号只是"保存时这道题在第几格"
    的遗迹，问卷中间插一题会让整份预设错位，而错位是静默的（选项数恰好还来得及）。

**能力边界**（不是缺陷，但值得写在这里）：探测会输出的 ``matrix_scale``（量表式矩阵）
**不支持逐行权重** —— 它的 rows 是提交槽名 fid 而不是行号，而
``src/answering_v2`` 那条分支读的是档位 ``weights``、不看 ``row_weights``。
所以本模块刻意不把它并进 ``MATRIX_TYPES``（并进去只会得到"界面填得进、分布不动"的
静默无效）；它走未知题型兜底，用户写 ``0.2,0.3,0.3,0.1,0.1`` 这样的档位串是**通的**、
真的影响分布。写 ``1:... | 2:...`` 会被拒 —— 那条警告的措辞因此值得留意。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Callable

from .anchoring import make_anchor
from .models import normalize_question_type

CHOICE_TYPES = ("single", "radio", "multi", "checkbox", "dropdown")
SCALE_TYPES = ("scale", "rating", "nps")
TEXT_TYPES = ("text", "input", "textarea", "fillblank")
MATRIX_TYPES = ("matrix_single", "matrix", "matrix_multi")
SORT_TYPES = ("sort", "ordering", "rank")

# 界面上按"几行 × 几列"显示的题型。**故意不等于 MATRIX_TYPES**：
# 探测还会输出 matrix_scale（量表式矩阵），它的 rows 是提交槽名 fid 而不是行号，
# 而 src/answering_v2 的那条分支读的是档位 weights、不看 row_weights ——
# 把它并进 MATRIX_TYPES 只会得到"界面填得进行权重、分布一动不动"的静默无效。
# 所以它只在显示上同类，解析上仍走未知题型兜底（用户写逗号串 = 档位权重，那条是通的）。
MATRIX_LIKE_TYPES = MATRIX_TYPES + ("matrix_scale",)

_MatrixRowWeights = dict[str, list[float]]


def _bad_number(x: float) -> bool:
    return x < 0 or math.isnan(x) or math.isinf(x)


def _floats(raw: str) -> list[float] | None:
    try:
        return [float(p) for p in [s.strip() for s in raw.split(",") if s.strip()]]
    except ValueError:
        return None


def _choice_entry(q: dict, raw: str, warn: Callable[[str], None]) -> dict | None:
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
    # 探测没给出选项的 single/dropdown 放行 —— 与桌面版一致
    if any(_bad_number(w) for w in weights):
        warn(f"Q{q['q']} 权重含负数或非有限值，已跳过")
        return None
    return {"type": q["type"], "weights": weights}


def scale_levels(q: dict) -> int:
    """这题有几个可选的分值。

    ``scale`` 存的是**最大分值**而不是个数，所以 0~10 是 11 格、2~10 是 9 格。
    按 ``scale`` 当个数算会让权重数组短一截，而 ``config_io`` 的长度校验
    按 ``scale_max - scale_min + 1`` 反推 —— 于是界面自己产出的配置被自己的校验拒收。
    """
    # 不做 try：`_scale_entry` 外面同样要 int(scale)，这里吞掉只会让两处行为不一致
    # —— 手写配置里塞个 "scale": "abc" 本来就该大声失败。
    return max(int(q.get("scale", 5)) - int(q.get("scale_min", 1)) + 1, 0)


def _scale_entry(q: dict, raw: str, warn: Callable[[str], None]) -> dict:
    n = int(q.get("scale", 5))
    smin = int(q.get("scale_min", 1))
    levels = scale_levels(q)
    entry: dict[str, Any] = {"type": q["type"], "scale": n, "scale_min": smin}
    if not raw:
        return entry
    stripped = raw.strip()
    if stripped.isdigit():
        pick = int(stripped)
        weights = [0.0] * levels
        if smin <= pick <= n:
            weights[pick - smin] = 1.0
        else:
            warn(f"Q{q['q']} 指定的分值 {pick} 超出量表范围 {smin}~{n}，"
                 "该题将没有可选中的分值")
        entry["weights"] = weights
        return entry
    weights = _floats(stripped)
    if weights is None:
        warn(f"Q{q['q']} 量表权重格式错误，已只保留量表结构：{raw}")
        return entry
    if len(weights) != levels:
        warn(f"Q{q['q']} 量表权重个数 {len(weights)} 与级数 {levels} 不符，"
             "已自动补齐/截断")
        weights = (weights + [0.0] * levels)[:levels]
    if any(_bad_number(w) for w in weights):
        warn(f"Q{q['q']} 量表权重含负数或非有限值，已只保留量表结构")
        return entry
    entry["weights"] = weights
    return entry


def _text_entry(q: dict, raw: str,
                warn: Callable[[str], None]) -> dict:           # noqa: ARG001
    entry: dict[str, Any] = {"type": q["type"]}
    if q.get("field"):
        entry["field"] = q["field"]
    if raw.strip():
        entry["options"] = [p.strip() for p in raw.split(",") if p.strip()]
    return entry


def _matrix_entry(q: dict, raw: str, warn: Callable[[str], None]) -> dict:
    entry: dict[str, Any] = {
        "type": q["type"],
        "rows": list(q.get("rows", [])),
        "cols": list(q.get("cols", [])),
    }
    if not raw.strip():
        return entry
    example = "1:0,0,0.1,0.4,0.5 | 2:0.1,0.2,0.3,0.3,0.1"
    row_weights: _MatrixRowWeights = {}
    for segment in raw.split("|"):
        segment = segment.strip()
        if not segment:
            continue            # 结尾多一个 '|' 是最自然的笔误，不该毁掉整份行权重
        if ":" not in segment:
            warn(f"Q{q['q']} 矩阵格式缺少行号分隔符 ':'，整份行权重作废。"
                 f"正确格式示例：{example}")
            return entry
        key, _, values = segment.partition(":")
        key = key.strip()
        if not key:
            warn(f"Q{q['q']} 矩阵有一行没写行号，整份行权重作废。"
                 f"正确格式示例：{example}")
            return entry
        floats = _floats(values)
        if floats is None or any(_bad_number(w) for w in floats):
            warn(f"Q{q['q']} 矩阵第 {key} 行权重非法，整份行权重作废。"
                 f"正确格式示例：{example}")
            return entry
        row_weights[key] = floats
    entry["row_weights"] = row_weights
    return entry


def _sort_entry(q: dict, raw: str, warn: Callable[[str], None]) -> dict:
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


_ENTRY_BUILDERS: tuple[tuple[tuple[str, ...], Callable[..., dict | None]], ...] = (
    (CHOICE_TYPES, _choice_entry),
    (SCALE_TYPES, _scale_entry),
    (TEXT_TYPES, _text_entry),
    (MATRIX_TYPES, _matrix_entry),
    (SORT_TYPES, _sort_entry),
)


def weight_text_for(texts: Mapping[Any, Any], qi: Any) -> str:
    """取某一行的原始文本。

    题号在界面里是 ``int``、从 JSON 配置回来时是 ``str`` —— 两种都要认，
    否则一次 ``--config`` 往返就把整列读成"没填"，于是所有题静默退化成等权重。
    """
    try:
        key = int(qi)
    except (TypeError, ValueError):
        return ""
    if key in texts:
        return str(texts[key] or "")
    return str(texts.get(str(key), "") or "")


def parse_weight_texts(questions: list[dict], texts: Mapping[Any, Any]
                       ) -> tuple[dict, list[str]]:
    """探测到的题目 + 每行的文本 → ``(cfg, warnings)``。

    ``warnings`` 是要往日志里刷的人话，不阻断：被弃用的那部分已经不进 cfg，
    引擎会按等权重作答，所以调用方必须把它们说出来，否则"我设了权重"与
    "实际拿到的分布"就对不上。
    """
    warnings: list[str] = []
    warn = warnings.append
    cfg: dict[Any, dict] = {}

    for q in questions:
        qi = q.get("q")
        if qi is None:
            continue
        qtype = str(q.get("type", "single")).lower()
        raw = weight_text_for(texts, qi).strip()
        storage = normalize_question_type(qtype)

        entry: dict | None = None
        for types, build in _ENTRY_BUILDERS:
            if storage in types:
                entry = build({**q, "type": storage}, raw, warn)
                break
        else:
            # 未知题型兜底：只认逗号分 float，认不出来就整题跳过
            if not raw:
                continue
            floats = _floats(raw)
            if floats is None:
                warn(f"Q{qi} 未知题型 {qtype} 且权重格式错误，已跳过：{raw}")
                continue
            entry = {"type": q.get("type"), "weights": floats}

        if entry is None:
            continue            # 留空/格式错：这道题不进 cfg，交给引擎的等权重分支

        anchor = make_anchor(q)
        if anchor:
            entry["anchor"] = anchor
        cfg[int(qi)] = entry

    return cfg, warnings


def reconstruct_questions(restored: Mapping[Any, Any]) -> list[dict[str, Any]]:
    """从一份持久化 ``weight_config`` 反构出**最小题目表**，给界面显示与编辑用。

    生产路径只有一条：断点续传时把上次那批的权重恢复回表格。两个宿主原本各自实现
    （桌面版在 ``gui/weight_panel.restore_from_config``，webui 干脆没做 —— 它只写
    ``weight_texts``，而表格是按 ``questions`` 渲染的，于是那句"已自动恢复到表格，
    可检查/修改"在没探测过的会话里是假的：表上一行都没有）。

    规则逐字照桌面版，**包括两条已知的粗糙**，第 7 步退役 Tk 时不该由一次搬家顺带改掉：
      - 单选/多选/下拉：``choices`` 用占位长度 = ``weights`` 长度（没有 weights 给 2 个）
      - 量表：优先 ``cfg.scale``，否则按 weights 长度，再退到 5；``scale_min`` 恒为 1
      - 填空：保留 ``field``；``options`` 作为候选文本预填
      - 矩阵一族：``rows``/``cols``/``row_weights`` 原样保留
      - **认不出的题型（含 ``sort``）给两个占位选项** —— 排序题因此显示成"2 选项"，
        行数与 weights 也不对应。这是现状，不是这次引入的。
    """
    out: list[dict[str, Any]] = []
    for qi in sorted(restored.keys()):        # 与桌面版一致：键序即题号序
        cfg = restored[qi]
        if not isinstance(cfg, dict):
            continue
        qtype = str(cfg.get("type", "single"))
        q: dict[str, Any] = {"q": qi, "type": qtype}

        if qtype in CHOICE_TYPES:
            weights = cfg.get("weights") or []
            q["choices"] = list(range(1, len(weights) + 1)) if weights else [1, 2]
        elif qtype in SCALE_TYPES:
            scale = cfg.get("scale")
            if scale is None:
                weights = cfg.get("weights") or []
                scale = len(weights) if weights else 5
            q["scale"] = int(scale)
            q["scale_min"] = 1
            q["choices"] = list(range(1, int(scale) + 1))
        elif qtype in TEXT_TYPES:
            q["field"] = cfg.get("field")
            q["choices"] = []
        elif qtype in MATRIX_TYPES:
            rows = cfg.get("rows") or [1, 2]
            cols = cfg.get("cols") or [1, 2]
            row_weights = cfg.get("row_weights") or {}
            if row_weights:
                rows = sorted(int(k) for k in row_weights.keys())
                if not cols:
                    first_rw = next(iter(row_weights.values()))
                    cols = list(range(1, len(first_rw) + 1)) if first_rw else [1, 2]
            q["rows"] = rows
            q["cols"] = cols
            q["choices"] = cols
        else:
            q["choices"] = cfg.get("choices") or [1, 2]

        out.append(q)
    return out


__all__ = ["parse_weight_texts", "weight_text_for", "scale_levels",
           "reconstruct_questions",
           "CHOICE_TYPES", "SCALE_TYPES", "TEXT_TYPES",
           "MATRIX_TYPES", "MATRIX_LIKE_TYPES", "SORT_TYPES"]
