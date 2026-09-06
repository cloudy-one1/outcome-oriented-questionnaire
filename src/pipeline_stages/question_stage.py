"""等待题目渲染 + 单题答题分发阶段（第一章第 3 条拆分）。

导出：
    - _wait_for_questions       等待题目控件出现在 DOM
    - _answer_one_question      根据 q.type 调用 answering + interaction，并可选写 history
"""

from __future__ import annotations

import time
from typing import Any

from selenium.webdriver.support.ui import WebDriverWait

from ..answering import build_answer_strategy
from ..answering_v2 import generate_answer as generate_answer_v2
from ..config import (
    Q_LONG_PAUSE_HI,
    Q_LONG_PAUSE_LO,
    Q_LONG_PAUSE_PROB,
    Q_THINK_HI,
    Q_THINK_LO,
    Q_THINK_MU,
    Q_THINK_SIGMA,
)
from ..exceptions import TRANSIENT_DOM_EXCEPTIONS, format_exc_log, raise_non_recoverable
from ..models import normalize_question_type
from ..interactions.choices import js_click_option, js_click_question_options
from ..interactions.dropdown import js_select_dropdown
from ..interactions.matrix import js_fill_matrix_single
from ..interactions.scale import js_set_scale
from ..interactions.text import js_fill_text
from ..utils import human_pause
from .page_loader import QUESTION_CONTROL_SELECTOR


# ============================================================================
#  等待题目控件渲染（覆盖 6 类题型常见输入控件）
# ============================================================================
def _wait_for_questions(driver: Any, timeout: float) -> bool:
    """等待题目输入框出现在 DOM 中（V2 扩展：覆盖 6 类题型的常见输入控件）。"""
    selector = QUESTION_CONTROL_SELECTOR
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script(
                f"return document.querySelectorAll('{selector}').length > 0"
            )
        )
        return True
    except Exception:
        return False


# ============================================================================
#  单题答题分发器 + 逐题 history 答案明细落盘
# ============================================================================
def _answer_one_question(
    driver: Any,
    q: dict,
    history_db: Any | None = None,
    run_id: int | None = None,
    submission_index: int | None = None,
    *,
    no_record_text: bool = False,
) -> bool:
    """为单道题生成答案并写入 DOM，可选地落盘 history.answers。

    - V1 题型 (single/multi)：沿用 ``build_answer_strategy`` +
      ``js_click_question_options``，保证 100% 行为不变。
    - V2 题型 (text/scale/dropdown/matrix_single)：使用
      ``answering_v2.generate_answer``（统一 dict）+ 对应 interaction 新函数。

    :param no_record_text: 审查 P2-3 隐私保护 —— True 时填空题答案
                            不会写入 SQLite 的 text_answer 列（写 NULL 占位），
                            避免明文保存用户自定义的姓名/手机/邮箱等敏感内容。
                            DOM 仍然会填入实际文本（流程需要），只是不持久化。

    :return: 是否答题成功（不影响外层统计 —— 失败通常只是识别不到 DOM，
             整次提交会在提交后统一判断）。
    """
    qnum = int(q["q"])
    qtype = str(q.get("type", "single")).lower()

    # 用于 history 记录（options_selected / text_answer / elapsed_ms）
    options_selected: list[int] | None = None
    text_answer: str | None = None
    t0 = time.perf_counter()
    is_ok = False

    # ------------------------------------------------------------------
    #  V1 题型：单选 / 多选（完全保留原逻辑，不做任何破坏性改动）
    # ------------------------------------------------------------------
    if qtype in ("single", "multi"):
        answer_values = build_answer_strategy(q)  # list[int]
        try:
            is_ok = js_click_question_options(driver, qnum, qtype, answer_values)
        except TRANSIENT_DOM_EXCEPTIONS as _e:
            print("  " + format_exc_log(
                _e, action="批量点击选项", question=qnum, qtype=qtype,
                recovery="降级为逐一点击",
            ))
            is_ok = True
            for c in answer_values:
                if not js_click_option(driver, qnum, c):
                    is_ok = False
                    break
                human_pause(
                    Q_THINK_MU * 0.3, Q_THINK_SIGMA * 0.3,
                    0.04, 0.15,
                )
        except Exception as _e:
            raise_non_recoverable(_e)
            print("  " + format_exc_log(
                _e, action="V1 答题交互", question=qnum, qtype=qtype,
                recovery="本题计失败，跳过继续答下一题",
            ))
            is_ok = False
        # history 记录：选项值列表
        options_selected = list(answer_values) if answer_values else None

    # ------------------------------------------------------------------
    #  V2 题型：text / scale / dropdown / matrix_single
    # ------------------------------------------------------------------
    else:
        ans = generate_answer_v2(q)  # dict 结构
        ans_type = str(ans.get("type", qtype)).lower()

        try:
            if ans_type == "text":
                text_answer = str(ans.get("text", ""))
                is_ok = js_fill_text(driver, qnum, text_answer)

            elif ans_type == "scale":
                val = int(ans.get("value", 3))
                smax = q.get("scale")
                is_ok = js_set_scale(driver, qnum, val, scale_max=smax)
                options_selected = [val]

            elif ans_type == "dropdown":
                sel_list = ans.get("selected") or []
                if sel_list:
                    is_ok = js_select_dropdown(driver, qnum, sel_list[0])
                    options_selected = [sel_list[0]] if isinstance(sel_list[0], int) else None
                    # 文本型下拉值 → 写 text_answer 备查
                    if options_selected is None and sel_list:
                        text_answer = str(sel_list[0])

            elif ans_type in ("matrix_single", "matrix"):
                row_map = ans.get("rows") or {}  # {row_idx: col_idx/val}
                is_ok = js_fill_matrix_single(driver, qnum, row_map)
                if isinstance(row_map, dict):
                    options_selected = [
                        v if isinstance(v, int) else int(v)
                        for v in row_map.values()
                        if isinstance(v, int) or (isinstance(v, str) and v.isdigit())
                    ]

            else:
                # 兜底：如果有 choices，降级成单选（与 answering_v2 的兜底一致）
                if q.get("choices"):
                    answer_values = build_answer_strategy(q)
                    is_ok = js_click_question_options(driver, qnum, "single", answer_values)
                    options_selected = list(answer_values)
                else:
                    is_ok = False
        except TRANSIENT_DOM_EXCEPTIONS as _e:
            print("  " + format_exc_log(
                _e, action="V2 答题交互", question=qnum, qtype=qtype,
                recovery="本题计失败，跳过继续答下一题",
            ))
            is_ok = False
        except Exception as _e:
            raise_non_recoverable(_e)
            print("  " + format_exc_log(
                _e, action="V2 答题交互", question=qnum, qtype=qtype,
                recovery="本题计失败，跳过继续答下一题",
            ))
            is_ok = False

    # ------------------------------------------------------------------
    #  逐题答案明细落盘（history DB）
    # ------------------------------------------------------------------
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if (
        history_db is not None
        and run_id is not None
        and submission_index is not None
    ):
        try:
            # V2.4 整改：题型别名归一化收敛到 models.normalize_question_type（单一真相）
            norm_type = normalize_question_type(qtype)

            # 隐私保护：no_record_text=True 时填空题文本不落盘
            persisted_text: str | None = text_answer
            if no_record_text and norm_type == "text":
                persisted_text = None  # 写 NULL 占位，不存敏感内容

            history_db.record_answer(
                run_id=run_id,
                submission_index=submission_index,
                question_number=qnum,
                question_type=norm_type,
                options_selected=options_selected,
                text_answer=persisted_text,
                elapsed_ms=elapsed_ms,
            )
        except Exception as _he:
            # history 写失败只打印提示，不影响主流程
            print(f"  [history] record_answer(Q{qnum}) 失败: {type(_he).__name__}")

    return bool(is_ok)


__all__ = [
    "_wait_for_questions",
    "_answer_one_question",
]
