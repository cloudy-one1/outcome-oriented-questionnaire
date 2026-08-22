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
    """结构合法性校验；返回错误/警告字符串列表，空列表表示合法。"""
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
            w = qcfg["weights"]
            if not isinstance(w, (list, tuple)):
                errors.append(f"Q{qi} 的 'weights' 必须是 list/tuple，实际 {type(w).__name__}")
            else:
                for i, v in enumerate(w):
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        errors.append(f"Q{qi} 的 weights[{i}]={v!r} 不能转成数值")
                        continue
                    if fv < 0:
                        errors.append(f"Q{qi} 的 weights[{i}]={fv} < 0，不能为负数")

        # 5. matrix：row_weights 必须是 dict
        if qtype_lc in {"matrix_single", "matrix"}:
            rw = qcfg.get("row_weights")
            if rw is not None and not isinstance(rw, dict):
                errors.append(f"Q{qi} matrix 型的 row_weights 必须是 dict（行号→权重列表）")

        # 6. text：field 必须是字符串（如果给的话）
        if qtype_lc in {"text", "input", "textarea", "fillblank"}:
            field = qcfg.get("field")
            if field is not None and not isinstance(field, str):
                errors.append(f"Q{qi} text 型的 field 必须是 str 或 None")

    return errors
