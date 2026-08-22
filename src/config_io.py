"""v2.0 — 权重配置 JSON 导入/导出模块。

功能：
    save_weight_config(path, cfg, meta)
        保存一份 JSON 配置文件；自动写入 saved_at 时间戳、schema_version。

    load_weight_config(path)
        读取 JSON 文件 → 校验结构 → 转回 int 题号键 → 返回 (cfg_dict, meta_dict)

    apply_weight_config(cfg_dict)
        **热更新** 到 ``src.config.WEIGHT_CONFIG``，GUI 改完立即生效。

    validate_weight_config(cfg) -> list[str]
        结构合法性校验，返回发现的错误/警告列表（空列表 = 完全合法）。

JSON 文件格式::

    {
      "schema_version": "2.0",
      "saved_at": "2026-07-04T20:50:00",
      "meta": {
        "name": "预设名称",
        "description": "描述",
        "author": "张三",
        "survey_url": "..."
      },
      "config": {
        "1":  {"type": "single", "weights": [0.2, 0.5, 0.3]},
        "7":  {"type": "multi",  "weights": [0.1, 0.2, ...], "count_options": [2,3]},
        "10": {"type": "scale",  "scale": 5, "weights": [0,0,0,0,1]},
        "12": {"type": "text",   "field": "name"},
        "20": {"type": "matrix_single", "row_weights": {"1": [1,2,3,2,1]}}
      }
    }
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
from typing import Any

from . import config as _config_module


SCHEMA_VERSION = "2.0"

# v2.0 支持的合法题型（与 answering_v2 对齐）
_VALID_TYPES = {
    "single", "radio",
    "multi", "checkbox",
    "dropdown",
    "scale", "rating",
    "text", "input", "textarea", "fillblank",
    "matrix_single", "matrix",
}


# ============================================================================
#  Public API
# ============================================================================
def save_weight_config(
    path: str,
    cfg: dict[int, dict],
    meta: dict | None = None,
    pretty: bool = True,
) -> str:
    """保存权重配置 + 元信息到 JSON。

    :return: 实际写入的 JSON 文件绝对路径。
    """
    # 1. 确保父目录存在
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    # 2. 把 int 键的 cfg 转成 str 键（JSON 只支持字符串键）
    serializable_cfg: dict[str, Any] = {}
    for qnum, qcfg in cfg.items():
        qnum_int = int(qnum)
        if qnum_int <= 0:
            raise ValueError(f"题号必须为正整数，收到: {qnum!r}")
        if not isinstance(qcfg, dict):
            raise ValueError(f"Q{qnum_int} 配置必须是 dict，收到 {type(qcfg).__name__}")
        serializable_cfg[str(qnum_int)] = qcfg

    # 3. 组装顶层结构
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "meta": dict(meta or {}),
        "config": serializable_cfg,
    }

    # 4. 写入（UTF-8 不带 BOM，标准 JSON 工具都兼容）
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            sort_keys=True,
        )
    return os.path.abspath(path)


def load_weight_config(path: str) -> tuple[dict[int, dict], dict]:
    """读取并校验 JSON 配置文件。

    :return: ``(cfg_dict, meta_dict)`` — cfg 的键已转回 ``int``。
    :raises FileNotFoundError: ``path`` 不存在。
    :raises ValueError: JSON 结构非法。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with open(path, "r", encoding="utf-8-sig") as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 解析失败: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("JSON 顶层必须是 object（dict）。")

    cfg_section = raw.get("config", {})
    meta = raw.get("meta", {}) or {}
    if not isinstance(cfg_section, dict):
        raise ValueError("'config' 字段必须是 object（dict）。")
    if not isinstance(meta, dict):
        raise ValueError("'meta' 字段必须是 object（dict）。")

    # 转回 int 键 + 基础结构预检（含 matrix 题 row_weights 的内层 int 键）
    cfg: dict[int, dict] = {}
    for qnum_str, qcfg in cfg_section.items():
        try:
            qnum_int = int(qnum_str)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"题号键 {qnum_str!r} 不能转成正整数"
            ) from exc
        if qnum_int <= 0:
            raise ValueError(f"题号必须为正整数，收到: {qnum_int}")
        if not isinstance(qcfg, dict):
            raise ValueError(f"Q{qnum_int} 配置必须是 dict，实际 {type(qcfg).__name__}")

        # matrix_single 的 row_weights：JSON 里 key 是 str，转回 int
        fixed_cfg: dict = dict(qcfg)
        if fixed_cfg.get("type") in {"matrix_single", "matrix"} and "row_weights" in fixed_cfg:
            rw = fixed_cfg["row_weights"]
            if isinstance(rw, dict):
                fixed_rw: dict = {}
                for rk, rv in rw.items():
                    try:
                        rk_int = int(rk)
                    except (TypeError, ValueError):
                        rk_int = rk  # type: ignore[assignment]
                    fixed_rw[rk_int] = rv
                fixed_cfg["row_weights"] = fixed_rw

        cfg[qnum_int] = fixed_cfg

    return cfg, meta


def apply_weight_config(cfg: dict[int, dict]) -> None:
    """热更新：把 ``cfg`` 里的每一项写入 ``src.config.WEIGHT_CONFIG``。

    - 已有的题号 → 覆盖
    - 没有的题号 → 新增
    - 原来存在但 cfg 中没有的 → 保留不删（避免误删 GUI 里没动过的）
    """
    for qnum, qcfg in cfg.items():
        qnum_int = int(qnum)
        _config_module.WEIGHT_CONFIG[qnum_int] = dict(qcfg)


def validate_weight_config(
    cfg: dict,
) -> list[str]:
    """结构合法性校验；返回错误/警告字符串列表，空列表表示合法。

    审查 P2-1 增强校验项：
      - 权重总和必须 > 0（全 0 权重会让等权随机采样退化或抛异常）
      - NaN / Inf 权重值（会让 random.choices 抛 ValueError，但应在配置期就拦截）
      - weights 长度是否匹配 choices 数量（题号配置可能附 choices 元信息）
      - multi 题 count_options 与 count_weights 长度是否一致
      - scale 题 weights 长度是否匹配 scale 范围
      - matrix_single 题 row_weights 每行长度是否匹配 cols 数量
      - count_weights 长度是否匹配 count_options 长度
    """
    errors: list[str] = []

    if not isinstance(cfg, dict):
        errors.append("配置必须是 dict（题号->题配置映射）")
        return errors

    for qnum, qcfg in cfg.items():
        # 1. 题号必须能转成正整数
        try:
            qi = int(qnum)
        except (TypeError, ValueError):
            errors.append(f"题号键 {qnum!r} 非法：必须是正整数")
            continue
        if qi <= 0:
            errors.append(f"Q{qnum!r} 必须为正整数")
            continue

        # 2. 值必须是 dict
        if not isinstance(qcfg, dict):
            errors.append(f"Q{qi} 配置必须是 dict，实际为 {type(qcfg).__name__}")
            continue

        # 3. 必须有 type
        qtype = qcfg.get("type")
        if not qtype or not isinstance(qtype, str):
            errors.append(f"Q{qi} 缺少必填字段 'type'（str）")
            continue

        qtype_lc = qtype.lower()
        if qtype_lc not in _VALID_TYPES:
            errors.append(
                f"Q{qi} 的题型 {qtype!r} 未在 v2.0 支持列表中"
                f"（支持：{', '.join(sorted(_VALID_TYPES))}）"
            )

        # 4. 特定题型的 weights 检查
        needs_weights = {"single", "radio", "multi", "checkbox", "dropdown", "scale", "rating"}
        if qtype_lc in needs_weights and "weights" in qcfg:
            errors.extend(_validate_weights_array(qi, qcfg, qtype_lc))

        # 5. multi 题：count_options / count_weights 校验
        if qtype_lc in {"multi", "checkbox"}:
            errors.extend(_validate_count_options(qi, qcfg))

        # 6. scale 题：weights 长度必须匹配 scale 范围
        if qtype_lc in {"scale", "rating"} and "weights" in qcfg:
            errors.extend(_validate_scale_length(qi, qcfg))

        # 7. matrix：row_weights 必须是 dict 且每行长度匹配 cols 数量
        if qtype_lc in {"matrix_single", "matrix"}:
            errors.extend(_validate_matrix_row_weights(qi, qcfg))

        # 8. text：field 必须是字符串（如果给的话）
        if qtype_lc in {"text", "input", "textarea", "fillblank"}:
            field = qcfg.get("field")
            if field is not None and not isinstance(field, str):
                errors.append(f"Q{qi} text 型的 field 必须是 str 或 None")

    return errors


# ============================================================================
#  内部校验子函数（审查 P2-1 拆分，便于单测覆盖各分项）
# ============================================================================
def _is_finite_number(v: Any) -> tuple[bool, float]:
    """尝试把 v 转 float；返回 (是否有限数值, 转换后的值)。

    NaN / Inf / 不能转的 → (False, 0.0)
    """
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return False, 0.0
    if math.isnan(fv) or math.isinf(fv):
        return False, fv
    return True, fv


def _validate_weights_array(qi: int, qcfg: dict, qtype_lc: str) -> list[str]:
    """校验 weights 字段：类型、NaN/Inf、非负、总和 > 0、长度匹配 choices（如有）。"""
    errs: list[str] = []
    w = qcfg["weights"]
    if not isinstance(w, (list, tuple)):
        errs.append(f"Q{qi} 的 'weights' 必须是 list/tuple，实际 {type(w).__name__}")
        return errs

    # 逐项数值 + NaN/Inf + 非负
    finite_vals: list[float] = []
    for i, v in enumerate(w):
        ok, fv = _is_finite_number(v)
        if not ok:
            # 区分 NaN/Inf 和"根本不能转"
            try:
                _ = float(v)
                # 能转但 NaN/Inf
                errs.append(f"Q{qi} 的 weights[{i}]={v!r} 是 NaN 或 Inf，必须为有限数值")
            except (TypeError, ValueError):
                errs.append(f"Q{qi} 的 weights[{i}]={v!r} 不能转成数值")
            continue
        if fv < 0:
            errs.append(f"Q{qi} 的 weights[{i}]={fv} < 0，不能为负数")
        finite_vals.append(fv)

    # 总和必须 > 0（全 0 / 全 NaN 时等权随机会抛 ValueError 或退化为非法分布）
    positive_sum = sum(x for x in finite_vals if x > 0)
    if len(finite_vals) == len(w) and positive_sum <= 0:
        errs.append(
            f"Q{qi} 的 weights 总和 = {positive_sum}，必须 > 0"
            f"（全 0 会让加权采样退化；请至少给一项非零权重，或删除 weights 走等权随机）"
        )

    # 长度匹配 choices（如果配置里附带了 choices 元信息）
    choices = qcfg.get("choices")
    if isinstance(choices, (list, tuple)) and len(choices) > 0:
        if len(w) != len(choices):
            errs.append(
                f"Q{qi} 的 weights 长度 {len(w)} 与 choices 长度 {len(choices)} 不匹配"
                f"（choices 来自探测后的题目结构元信息；权重必须逐项对应每个选项）"
            )

    # multi / dropdown 也应至少有 1 项；scale 跳过长度匹配（在 _validate_scale_length 单独校验）
    return errs


def _validate_count_options(qi: int, qcfg: dict) -> list[str]:
    """multi 题：count_options / count_weights 长度一致性 + 值合法性。"""
    errs: list[str] = []
    co = qcfg.get("count_options")
    cw = qcfg.get("count_weights")

    if co is None and cw is None:
        return errs  # 都没配 → 走默认均匀分布，合法

    if co is not None and not isinstance(co, (list, tuple)):
        errs.append(f"Q{qi} multi 的 'count_options' 必须是 list/tuple")
        return errs
    if cw is not None and not isinstance(cw, (list, tuple)):
        errs.append(f"Q{qi} multi 的 'count_weights' 必须是 list/tuple")
        return errs

    # count_options 必须都是 >= 1 的正整数
    if co is not None:
        for i, v in enumerate(co):
            ok, fv = _is_finite_number(v)
            if not ok or fv < 1 or int(fv) != fv:
                errs.append(
                    f"Q{qi} multi 的 count_options[{i}]={v!r} 必须是 >= 1 的正整数"
                )

    # count_weights 必须非负且有限
    if cw is not None:
        for i, v in enumerate(cw):
            ok, fv = _is_finite_number(v)
            if not ok:
                errs.append(
                    f"Q{qi} multi 的 count_weights[{i}]={v!r} 是 NaN/Inf 或非数值"
                )
                continue
            if fv < 0:
                errs.append(
                    f"Q{qi} multi 的 count_weights[{i}]={fv} < 0，不能为负数"
                )

    # 长度必须一致（如果两者都给了）
    if co is not None and cw is not None and len(co) != len(cw):
        errs.append(
            f"Q{qi} multi 的 count_options 长度 {len(co)} 与 count_weights 长度 {len(cw)} 不一致"
            f"（每个选项数对应一个权重，必须一一对应）"
        )
    elif co is not None and cw is not None:
        # 总和 > 0
        positive_sum = sum(float(v) for v in cw if _is_finite_number(v)[0] and float(v) > 0)
        if positive_sum <= 0:
            errs.append(
                f"Q{qi} multi 的 count_weights 总和 = {positive_sum}，必须 > 0"
            )

    return errs


def _validate_scale_length(qi: int, qcfg: dict) -> list[str]:
    """scale 题：weights 长度必须匹配 scale 范围（1..scale_max）。"""
    errs: list[str] = []
    w = qcfg.get("weights")
    if not isinstance(w, (list, tuple)):
        return errs  # 类型错误已在 _validate_weights_array 报过

    scale_max = qcfg.get("scale")
    if scale_max is None:
        return errs  # 没声明 scale → 跳过长度匹配检查（answering_v2 会用默认 5）

    try:
        smax = int(scale_max)
    except (TypeError, ValueError):
        errs.append(f"Q{qi} scale 的 'scale' 字段必须是整数，实际 {scale_max!r}")
        return errs

    scale_min = int(qcfg.get("scale_min", 1))
    expected_len = smax - scale_min + 1
    if expected_len <= 0:
        errs.append(
            f"Q{qi} scale 范围非法：scale_min={scale_min} > scale_max={smax}"
        )
        return errs

    if len(w) != expected_len:
        errs.append(
            f"Q{qi} scale 的 weights 长度 {len(w)} 与量表范围 {expected_len}"
            f"（scale_min={scale_min}..scale_max={smax}）不匹配"
        )

    return errs


def _validate_matrix_row_weights(qi: int, qcfg: dict) -> list[str]:
    """matrix_single 题：row_weights 是 dict 且每行长度匹配 cols 数量。"""
    errs: list[str] = []
    rw = qcfg.get("row_weights")
    if rw is None:
        return errs  # 未配 row_weights → 走等权随机，合法

    if not isinstance(rw, dict):
        errs.append(f"Q{qi} matrix 型的 row_weights 必须是 dict（行号→权重列表）")
        return errs

    cols = qcfg.get("cols")
    expected_col_len: int | None = None
    if isinstance(cols, (list, tuple)) and len(cols) > 0:
        expected_col_len = len(cols)
    elif isinstance(cols, int) and cols > 0:
        expected_col_len = cols

    for rk, rv in rw.items():
        # 行号键应能转成 int（load_weight_config 已转，但直接 dict 可能还是 str）
        try:
            _ = int(rk)
        except (TypeError, ValueError):
            errs.append(f"Q{qi} matrix row_weights 的行号键 {rk!r} 必须能转成整数")

        if not isinstance(rv, (list, tuple)):
            errs.append(
                f"Q{qi} matrix row_weights 行 {rk!r} 的值必须是 list/tuple"
                f"，实际 {type(rv).__name__}"
            )
            continue

        # 逐项 NaN/Inf/非负
        finite_vals: list[float] = []
        for i, v in enumerate(rv):
            ok, fv = _is_finite_number(v)
            if not ok:
                errs.append(
                    f"Q{qi} matrix row_weights 行 {rk!r} 的 weights[{i}]={v!r}"
                    f" 是 NaN/Inf 或非数值"
                )
                continue
            if fv < 0:
                errs.append(
                    f"Q{qi} matrix row_weights 行 {rk!r} 的 weights[{i}]={fv} < 0"
                )
            finite_vals.append(fv)

        # 长度匹配 cols
        if expected_col_len is not None and len(rv) != expected_col_len:
            errs.append(
                f"Q{qi} matrix row_weights 行 {rk!r} 的权重长度 {len(rv)}"
                f" 与 cols 长度 {expected_col_len} 不匹配"
            )

        # 总和 > 0
        positive_sum = sum(x for x in finite_vals if x > 0)
        if len(finite_vals) == len(rv) and positive_sum <= 0:
            errs.append(
                f"Q{qi} matrix row_weights 行 {rk!r} 的权重总和 = {positive_sum}，必须 > 0"
            )

    return errs
