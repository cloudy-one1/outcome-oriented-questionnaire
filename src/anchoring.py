"""权重锚定（v3.0）：让一份权重配置跟着**题干**走，而不是只跟着题号走。

为什么要有这个模块：``WEIGHT_CONFIG`` 的键一直是题号。于是"问卷中间插一道题"
或"探测顺序变了"会把整份预设**向后错位一格** —— 而且静默：只要选项数恰好还对得上，
``validate_weight_config`` 一律放行，跑完 17 份才发现分布全落在别人的题上。
v2.8 修掉的"出厂权重污染"是同一个失效面的另一半。

GUI「探测题目 → 另存」现在会把题干文本与结构签名写进每条配置的 ``anchor``，
读取时按锚点认领题目。三条契约：

  1. **带 anchor 的条目只按锚点生效，永不退回答题号。** 认不到题就这一题走等权随机，
     并提示一次 —— 宁可少一份定制分布，也不能拿别人的分布去填。
  2. **不带 anchor 的条目**（手写 JSON、v2.x 老配置、GUI 之外构造的 dict）
     行为与 v2.8 逐位一致。
  3. 锚点条目与题号条目同时指向一题时，**锚点赢**（题号只是"保存时在哪儿"的遗迹）。

结构与 ``models.normalize_question_type`` 共用题型单一真相；题干归一化会去掉
``1.`` / ``第2题`` / ``（3）`` 这类序号前缀，再压缩空白与标点。
"""

from __future__ import annotations

import difflib
import re
import threading
from typing import Any

from . import config as _config_module
from .models import normalize_question_type

__all__ = [
    "anchor_matches_question",
    "lookup_weight_entry",
    "make_anchor",
    "normalize_title",
    "question_signature",
    "report_unmatched_anchors",
    "reset_reported_anchors",
    "unmatched_anchors",
    "validate_anchor",
]

# 相似度阈值：0.78 大致对应"改一两个词"仍能命中，而"换了一道题"必然掉到线下。
_TITLE_THRESHOLD = 0.78

# 题号前缀：必须带分隔符才剥，否则 "2024年收入" 会被剥成 "年收入"。
_LEADING_INDEX = re.compile(
    r"^\s*(?:第\s*\d+\s*[题小]?|\d+\s*[.、)）．]|[.、]\s*\d+|[（(]\s*\d+\s*[)）])\s*"
)
_WS = re.compile(r"[\s　]+")
_PUNCT = re.compile(r"[，。、！？：；・,.\-_/\\|~`'\"“”‘’()（）\[\]【】<>《》*#]+")


# ============================================================================
#  题干与结构签名
# ============================================================================
def normalize_title(text: Any) -> str:
    """题干归一化：剥序号 → 去标点空白 → 小写。"""
    t = str(text or "")
    for _ in range(3):                      # "1、(2) 您的性别" 这类双层序号
        stripped = _LEADING_INDEX.sub("", t)
        if stripped == t:
            break
        t = stripped
    t = _WS.sub("", t)
    t = _PUNCT.sub("", t)
    return t.lower()


def question_signature(q: dict) -> str:
    """题目的结构签名：``题型:规模``。题干相同而结构变了 = 作者动过这道题。"""
    # 注意：normalize_question_type 产出的是 **存储名**（matrix_single → matrix），
    # 所以这里的分支比较必须用存储名，写成 "matrix_single" 会永远走不到。
    qtype = normalize_question_type(str(q.get("type", "single"))) or "single"
    if qtype == "scale":
        smin = q.get("scale_min", 1)
        smax = q.get("scale", 5)
        return f"scale:{smin}-{smax}"
    if qtype in ("matrix", "matrix_multi"):
        n_rows = len(q.get("rows") or [])
        n_cols = len(q.get("cols") or [])
        return f"{qtype}:{n_rows}x{n_cols}"
    if qtype == "text":
        return "text"
    if qtype == "sort":
        return f"sort:{len(q.get('items') or [])}"
    n_choices = len(q.get("choices") or [])
    return f"{qtype}:{n_choices}"


def make_anchor(q: dict) -> dict[str, Any] | None:
    """由探测到的题目构造锚点；没有题干就没有锚点（返回 None）。"""
    title = str(q.get("title") or "").strip()
    if not title:
        return None
    return {"title": title, "signature": question_signature(q)}


def _title_hit(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= _TITLE_THRESHOLD


def anchor_matches_question(anchor: Any, q: dict) -> bool:
    """这条锚点是否认领 ``q``。要求题干命中**且**结构签名一致（若锚点带了签名）。"""
    if not isinstance(anchor, dict):
        return False
    a_title = normalize_title(anchor.get("title"))
    q_title = normalize_title(q.get("title"))
    if not a_title or not q_title:
        return False
    if not _title_hit(a_title, q_title):
        return False
    sig = anchor.get("signature")
    if sig and q.get("title"):
        return str(sig) == question_signature(q)
    return True


# ============================================================================
#  查表
# ============================================================================
def _entry_at_number(cfg: dict, qi: Any) -> dict | None:
    """按题号取条目；键可能是 int（apply/GUI）或 str（手写 JSON）。"""
    entry = cfg.get(qi)
    if entry is None and qi is not None:
        entry = cfg.get(str(qi))
    if entry is None:
        try:
            entry = cfg.get(int(qi))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            entry = None
    return entry if isinstance(entry, dict) else None


def lookup_weight_entry(q: dict, cfg: dict | None = None) -> dict | None:
    """取这道题生效的权重配置：锚点优先，题号兜底（兜底**跳过**带锚点的条目）。

    :param q:   ``detect_questions`` 返回的单题结构
    :param cfg: 被查的配置；默认全局 ``src.config.WEIGHT_CONFIG``
    """
    source = _config_module.WEIGHT_CONFIG if cfg is None else cfg
    if not source:
        return None

    for entry in source.values():
        if not isinstance(entry, dict):
            continue
        anchor = entry.get("anchor")
        if anchor and anchor_matches_question(anchor, q):
            return entry

    by_number = _entry_at_number(source, q.get("q"))
    if by_number is not None and not by_number.get("anchor"):
        return by_number
    return None


# ============================================================================
#  未命中报告（每进程每条锚点最多提示一次）
# ============================================================================
_REPORTED: set[str] = set()
_REPORTED_LOCK = threading.Lock()


def unmatched_anchors(cfg: dict, questions: list[dict]) -> list[str]:
    """列出"带了 anchor 却在本次探测里认不到题"的条目（一行一条可读提示）。"""
    if not cfg or not questions:
        return []
    lines: list[str] = []
    for key, entry in cfg.items():
        if not isinstance(entry, dict):
            continue
        anchor = entry.get("anchor")
        if not anchor:
            continue
        if any(anchor_matches_question(anchor, q) for q in questions):
            continue
        title = str(anchor.get("title") or "").strip() or "（题干为空）"
        sig = anchor.get("signature")
        lines.append(
            f"[锚定] 保存于 Q{key} 的权重认不到题（题干「{title[:40]}」"
            + (f"、结构 {sig}" if sig else "")
            + "）→ 该权重本次**不生效**，不退回答题号"
        )
    return lines


def report_unmatched_anchors(cfg: dict, questions: list[dict]) -> list[str]:
    """``unmatched_anchors`` + 去重打印。返回本次**新**提示的行。

    探测每份问卷都会跑一次，同一份预设的未命中提示一次就够（17 份刷 17 行会把
    真正的运行信息埋掉）。集合是进程级状态，与 ``logging_setup._CONFIGURED``
    同类，测试需快照/还原。
    """
    fresh: list[str] = []
    with _REPORTED_LOCK:
        for line in unmatched_anchors(cfg, questions):
            if line in _REPORTED:
                continue
            _REPORTED.add(line)
            fresh.append(line)
    return fresh


def reset_reported_anchors() -> None:
    """清空去重集合（仅测试用）。"""
    with _REPORTED_LOCK:
        _REPORTED.clear()


# ============================================================================
#  配置校验（config_io 调用）
# ============================================================================
def validate_anchor(anchor: Any, where: str) -> list[str]:
    """校验 ``anchor`` 字段形态；返回错误列表（空 = 合法）。"""
    if not isinstance(anchor, dict):
        return [f"{where} 的 'anchor' 必须是 dict（title/signature），"
                f"实际 {type(anchor).__name__}"]
    errs: list[str] = []
    title = anchor.get("title")
    if not isinstance(title, str) or not title.strip():
        errs.append(f"{where} 的 anchor.title 必须是非空字符串")
    sig = anchor.get("signature")
    if sig is not None and (not isinstance(sig, str) or not sig.strip()):
        errs.append(f"{where} 的 anchor.signature 必须是非空字符串或省略")
    return errs
