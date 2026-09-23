"""等待题目渲染 + 单题答题分发阶段（第一章第 3 条拆分）。

导出：
    - _wait_for_questions       等待题目控件出现在 DOM
    - _answer_one_question      根据 q.type 调用 answering + interaction，并可选写 history
"""

from __future__ import annotations

import time
from typing import Any, Callable


from ..answering import build_answer_strategy
from .. import distribution  # v3.2 投递分布在线纠正的缓冲入口
from .. import reverse_fill  # v3.2 真实答卷回放：逐题问一次要不要覆盖
from ..answering_v2 import generate_answer as generate_answer_v2
from ..answering_v2 import generate_option_blank_text
from ..config import (
    Q_THINK_MU,
    Q_THINK_SIGMA,
)
from ..exceptions import (
    TRANSIENT_DOM_EXCEPTIONS,
    SubmissionAborted,
    format_exc_log,
    raise_non_recoverable,
)
from ..models import normalize_question_type
from ..interactions.choices import (
    js_click_option,
    js_click_question_options,
    js_fill_option_blank,
)
from ..interactions.dropdown import js_select_dropdown
from ..interactions.matrix import (
    js_fill_matrix_multi,
    js_fill_matrix_scale,
    js_fill_matrix_single,
)
from ..interactions.scale import js_set_scale
from ..interactions.sort import js_fill_sort
from ..interactions.text import js_fill_text
from ..utils import human_pause
from .page_loader import QUESTION_CONTROL_SELECTOR


# ============================================================================
#  等待题目控件渲染（覆盖 6 类题型常见输入控件）
# ============================================================================
def _wait_for_questions(
    driver: Any,
    timeout: float,
    hold_lock: Any | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> bool:
    """等待题目输入框出现在 DOM 中（V2 扩展：覆盖 6 类题型的常见输入控件）。

    :param hold_lock: 人工介入锁。传入后，**处于 holding 状态的时间不计入超时预算**。
    :param stop_check: 每个轮询片问一次；返回 True → 抛 ``SubmissionAborted``。
        不传时行为与旧版逐位一致。

    Why 不用 WebDriverWait(driver, timeout).until(...)：
    那条路径把 timeout 交给 WebDriver 的墙钟，而验证码可能在题目等待期间才弹出。
    用户正在拉滑块的十几~几十秒里页面本来就不会出现新控件，于是等待超时 →
    本轮 SUBMIT_FAILED → 人工白忙一场。ManualHoldLock 的文档一直声称
    "pipeline 中的任何超时逻辑看到 is_holding 就不该判失败"，此前却没有任何
    调用点实现它 —— 这里就是那个接线点。
    """
    selector = QUESTION_CONTROL_SELECTOR
    deadline = time.perf_counter() + timeout
    while True:
        if stop_check is not None and stop_check():
            raise SubmissionAborted("用户在等待题目渲染期间请求停止")
        try:
            found = driver.execute_script(
                f"return document.querySelectorAll('{selector}').length > 0"
            )
        except TRANSIENT_DOM_EXCEPTIONS:
            return False
        except Exception as _e:
            raise_non_recoverable(_e)
            return False
        if found:
            return True

        # 人工介入期间暂停计时：把 deadline 往后挪一个轮询片
        if hold_lock is not None and getattr(hold_lock, "is_holding", False):
            deadline += _HOLD_POLL
        elif time.perf_counter() >= deadline:
            return False
        time.sleep(_HOLD_POLL)


_HOLD_POLL = 0.25   # 轮询片长度（秒）；也是 holding 期间时钟暂停的粒度


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
    - v3.0 题型 (matrix_multi/sort)：同上，分发在下面的 ``ans_type`` 分支链。
      链尾的 ``else`` 是"认不出就降级当单选"的兜底 —— 新题型必须显式加分支，
      否则会被静默当成单选题点一下，页面收下的是一个毫无意义的答案。

    :param no_record_text: 审查 P2-3 隐私保护 —— True 时填空题答案
                            不会写入 SQLite 的 text_answer 列（写 NULL 占位），
                            避免明文保存用户自定义的姓名/手机/邮箱等敏感内容。
                            DOM 仍然会填入实际文本（流程需要），只是不持久化。

    :return: 是否答题成功。**v3.3 起外层会收上去**：逐页汇成"我们答过、但回执说没落上"
             的题号集合，点提交之前报一行 ``[作答回执]``，开了 ``--rescue-gaps`` 时连着
             这些一起等人工补。它**仍然不是拦停判据** —— False 有两种形状（控件真没找到、
             作答中途抛异常被降级），后一种下页面可能已经落上了一部分，拦错一单的代价是
             一单本来能交的问卷被判失败（``src/completeness.py::describe_not_written``）。
    """
    qnum = int(q["q"])
    qtype = str(q.get("type", "single")).lower()

    # 用于 history 记录（options_selected / text_answer / elapsed_ms）
    # 排序题会把 item id（字符串）也写进这一列，所以值域比"选项序号"宽
    options_selected: list[Any] | None = None
    text_answer: str | None = None
    t0 = time.perf_counter()
    is_ok = False

    # ---- v3.2 真实答卷回放：这一题被外部答卷表覆盖时用表里的值 ----
    # 覆盖只发生在"生成答案"这一步，点击/校验/落盘全部沿用原路径 —— 回放的答案
    # 同样要经过真 DOM 回读，否则"表里写了但页面没落上"会被记成成功。
    replayed = reverse_fill.answer_for_question(q, submission_index)
    replay_values: list[Any] | None = None
    if replayed is not None and str(replayed.get("type", "")).lower() in (
        "single", "multi",
    ):
        picked = list(replayed.get("selected") or [])
        if picked:
            replay_values = picked

    # ------------------------------------------------------------------
    #  V1 题型：单选 / 多选（完全保留原逻辑，不做任何破坏性改动）
    # ------------------------------------------------------------------
    if qtype in ("single", "multi"):
        answer_values = replay_values if replay_values else build_answer_strategy(q)
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

        # ---- 选项自带填空框（"其他____"）：勾完还要把那一格写上 ----
        # 放在 try/except 之后而不是之内：上面降级成"逐一点击"的路径同样勾中了
        # 那些项，一样需要补文本。少写这一处，症状是"报了成功、平台却整题拒收"。
        selected_values = set(answer_values or [])
        for blank_value in (q.get("blank_options") or []):
            if blank_value not in selected_values:
                continue
            try:
                blank_text = str(replayed.get("option_blank_text") or "") if replayed else ""
                if not js_fill_option_blank(
                    driver, qnum, blank_value, blank_text or generate_option_blank_text()
                ):
                    print(f"  [填空选项] Q{qnum} 第 {blank_value} 项的框没写进去"
                          " → 本题计失败")
                    is_ok = False
            except Exception as _e:
                raise_non_recoverable(_e)
                print("  " + format_exc_log(
                    _e, action="填写选项自带空", question=qnum, qtype=qtype,
                    recovery="本题计失败，跳过继续答下一题",
                ))
                is_ok = False

    # ------------------------------------------------------------------
    #  V2 题型：text / scale / dropdown / matrix_single
    # ------------------------------------------------------------------
    else:
        ans = replayed if replayed is not None else generate_answer_v2(q)  # dict 结构
        ans_type = str(ans.get("type", qtype)).lower()

        try:
            if ans_type == "text":
                text_answer = str(ans.get("text", ""))
                is_ok = js_fill_text(driver, qnum, text_answer)

            elif ans_type == "scale":
                val = int(ans.get("value", 3))
                smax = q.get("scale")
                smin = q.get("scale_min")
                is_ok = js_set_scale(
                    driver, qnum, val, scale_max=smax, scale_min=smin
                )
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

            elif ans_type == "matrix_scale":
                # v3.1：键是提交槽名（tr[fid]）、值是 dval 分值 —— 落库明细与矩阵单选
                # 同为"每行一个标量"，所以摊平方式一致
                row_map = ans.get("rows") or {}
                is_ok = js_fill_matrix_scale(driver, qnum, row_map)
                if isinstance(row_map, dict):
                    options_selected = [
                        v if isinstance(v, int) else int(v)
                        for v in row_map.values()
                        if isinstance(v, int) or (isinstance(v, str) and v.isdigit())
                    ]

            elif ans_type == "matrix_multi":
                # v3.0：每行的值是列表 → 落库时把各行勾中的列值摊平成一维，
                # 与 options_selected 列既有的"JSON 数组"形状保持一致。
                row_map = ans.get("rows") or {}
                is_ok = js_fill_matrix_multi(driver, qnum, row_map)
                if isinstance(row_map, dict):
                    options_selected = [
                        int(v)
                        for vals in row_map.values()
                        for v in (vals if isinstance(vals, (list, tuple)) else [vals])
                        if isinstance(v, int) or (isinstance(v, str) and v.isdigit())
                    ]

            elif ans_type == "sort":
                # v3.0：order 是 item id 序列（探测回来的都是字符串）
                # v3.1：控件形态由探测判定 —— 点击式那套写逗号串会污染选项身份
                order = [str(x) for x in (ans.get("order") or [])]
                is_ok = js_fill_sort(
                    driver, qnum, order, mode=str(q.get("sort_mode") or "value")
                )
                options_selected = [
                    int(v) if v.lstrip("-").isdigit() else v for v in order
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

            # 隐私保护：no_record_text=True 时**用户输入**的文本不落盘。
            #
            # 这里刻意只判 norm_type == "text"。text_answer 这一列有两个写入方：
            #   1. 填空题 —— 存的是生成出来的姓名/手机/邮箱等**用户侧内容**，
            #      这是本开关要保护的对象；
            #   2. 下拉题 —— 存的是**问卷页面自己的 <option> 文案**（站点内容，
            #      见上面 dropdown 分支），它不是任何人的个人信息，
            #      抹掉只会让历史明细失去可读性。
            # 因此 --no-record-text 的契约就是"不记录填空题答案"（README/CLI 帮助
            # 文案一致），不要顺手扩到这里；真要连站点文案一起匿名化的话，
            # 应该另开一个开关，而不是改变本参数的既有语义。
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

    # ---- 投递分布纠正：先攒进缓冲，这一份真的提交成功了才计入 ----
    # 与上面的 history 落盘同址不同命：history 记的是"作答时就写"，这里要的是
    # "**落地**了多少份" —— 失败与 UNKNOWN 的那几份不该进来（见 src/distribution.py）。
    if distribution.control_enabled() and is_ok and options_selected:
        picks = [v for v in options_selected if isinstance(v, int)]
        if picks:
            total = (
                len(q.get("choices") or [])
                or len(q.get("cols") or [])
                or len(q.get("items") or [])
                or len(picks)
            )
            distribution.buffer_answer(qnum, picks, total)

    return bool(is_ok)


__all__ = [
    "_wait_for_questions",
    "_answer_one_question",
]
