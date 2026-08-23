"""问卷数据模型（v2.3 引入）。

为题目、答案、提交结果和权重配置定义明确的数据模型,
替代之前在模块间传递的未约束 dict。

设计原则（对照「代码可读性改进建议」第三章「类型和数据模型」）:
  - 与现有 dict 接口双向兼容:``from_dict()`` / ``as_dict()`` 互逆,
    保证旧代码继续传 dict 也能跑,新代码可以选择用 dataclass。
  - 类型注解完整,IDE 友好,避免 ``dict[str, Any]`` 在模块间裸传。
  - 题型用枚举替代散落在多处的 ``"single"`` / ``"multi"`` / ``"text"`` 字符串字面量。
  - 提交结果用 ``SubmitResult`` dataclass 承载三态 + 上下文信息,
    避免单一布尔值或纯字符串承载多个状态。
  - 不破坏 V2.2 已通过的 130/130 测试。

迁移策略:
    第 1 步（本模块）:定义模型 + 提供双向转换 + 单元测试。
    第 2 步:detection.py / answering_v2.py / pipeline.py 内部改用 dataclass。
    第 3 步:config_io.py 用 ``WeightConfigEntry`` 替代 ad-hoc dict 校验。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional

from .interaction import SubmitOutcome, SUBMIT_SUCCESS, SUBMIT_FAILED, SUBMIT_UNKNOWN


# ============================================================================
# 题型枚举
# ============================================================================
class QuestionType(str, enum.Enum):
    """问卷题型枚举。

    继承 ``str`` 使其可直接 JSON 序列化、与旧字符串字面量 ``==`` 比较,
    例如 ``QuestionType.SINGLE == "single"`` 返回 ``True``。

    别名兼容（``from_str`` 接受）:
        - ``radio`` → ``SINGLE``
        - ``checkbox`` → ``MULTI``
        - ``rating`` → ``SCALE``
        - ``textarea`` / ``input`` / ``fillblank`` → ``TEXT``
        - ``matrix`` → ``MATRIX_SINGLE``
    """

    SINGLE = "single"                # 单选
    MULTI = "multi"                  # 多选
    DROPDOWN = "dropdown"           # 下拉
    SCALE = "scale"                  # 量表/评分
    TEXT = "text"                    # 填空/textarea
    MATRIX_SINGLE = "matrix_single"  # 矩阵单选

    @classmethod
    def from_str(cls, raw: str | None) -> "QuestionType":
        """从字符串构造 ``QuestionType``,兼容别名与大小写差异。

        :param raw: 题型字符串（如 ``"single"``、``"radio"``、``"SCALE"``）
        :return: 对应枚举成员
        :raises ValueError: 字符串无法识别时
        """
        if raw is None:
            raise ValueError("题型字符串为 None")
        s = str(raw).lower()
        alias = {
            "radio": cls.SINGLE,
            "checkbox": cls.MULTI,
            "rating": cls.SCALE,
            "textarea": cls.TEXT,
            "input": cls.TEXT,
            "fillblank": cls.TEXT,
            "matrix": cls.MATRIX_SINGLE,
        }
        if s in alias:
            return alias[s]
        try:
            return cls(s)
        except ValueError:
            raise ValueError(f"未知题型: {raw!r}") from None

    def __str__(self) -> str:  # 类型:ignore[override]
        """直接拿 .value,避免 ``str(QuestionType.SINGLE)`` 返回 ``"QuestionType.SINGLE"``。"""
        return self.value


# ============================================================================
# 题目数据模型
# ============================================================================
@dataclass(slots=True)
class QuestionData:
    """单道题目的结构信息（detection.detect_questions 返回项的类型化版本）。

    各题型字段说明:
        - SINGLE / MULTI / DROPDOWN: ``choices`` 必填,元素为选项值（int 或 str）
        - SCALE: ``scale`` 必填（量表最大值,如 5 或 10）, ``scale_min`` 默认 1
        - TEXT: ``field`` 可选（name/phone/email/address/age/company 或 None）
        - MATRIX_SINGLE: ``rows`` 与 ``cols`` 必填
    """

    q: int                                    # 题号
    type: QuestionType                        # 题型
    choices: Optional[list[Any]] = None       # 选项列表(SINGLE/MULTI/DROPDOWN)
    scale: Optional[int] = None               # 量表最大值(SCALE)
    scale_min: int = 1                        # 量表最小值,默认 1(SCALE)
    field: Optional[str] = None               # 填空字段类型(TEXT)
    rows: Optional[list[Any]] = None          # 矩阵行标签(MATRIX)
    cols: Optional[list[Any]] = None          # 矩阵列标签(MATRIX)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QuestionData":
        """从 detection 返回的原始 dict 构造 ``QuestionData``。

        容错策略:缺字段一律视为 None,不抛异常（与现有 dict 行为一致）。
        题型字符串不可识别时,落回 ``QuestionType.SINGLE``（与 answering_v2 兜底一致）。
        """
        try:
            qtype = QuestionType.from_str(d.get("type"))
        except ValueError:
            qtype = QuestionType.SINGLE
        return cls(
            q=int(d.get("q", 0)),
            type=qtype,
            choices=list(d["choices"]) if d.get("choices") is not None else None,
            scale=int(d["scale"]) if d.get("scale") is not None else None,
            scale_min=int(d.get("scale_min", 1)),
            field=d.get("field"),
            rows=list(d["rows"]) if d.get("rows") is not None else None,
            cols=list(d["cols"]) if d.get("cols") is not None else None,
        )

    def as_dict(self) -> dict[str, Any]:
        """转回 detection 原始 dict 格式（None 字段省略,JSON 友好）。

        保证 ``QuestionData.from_dict(q.as_dict()) == q``。
        """
        out: dict[str, Any] = {"q": self.q, "type": str(self.type)}
        if self.choices is not None:
            out["choices"] = self.choices
        if self.scale is not None:
            # QuestionData 总是同时输出 scale + scale_min,
            # 对齐 detection.detect_questions 的 JS 实际输出（即使 scale_min=1 也保留）
            out["scale"] = self.scale
            out["scale_min"] = self.scale_min
        if self.field is not None:
            out["field"] = self.field
        if self.rows is not None:
            out["rows"] = self.rows
        if self.cols is not None:
            out["cols"] = self.cols
        return out


# ============================================================================
# 答案数据模型
# ============================================================================
@dataclass(slots=True)
class AnswerData:
    """单道题目的答案（answering_v2.generate_answer 返回项的类型化版本）。

    各题型字段说明:
        - SINGLE / MULTI / DROPDOWN: ``selected`` 必填（list[int] 或 list[str]）
        - SCALE: ``value`` 必填（1..N 整数）
        - TEXT: ``text`` 必填, ``field`` 可选
        - MATRIX_SINGLE: ``rows`` 必填（{row_idx: col_idx} 映射）
    """

    type: QuestionType                            # 题型
    selected: Optional[list[Any]] = None          # 选中项索引/值(SINGLE/MULTI/DROPDOWN)
    value: Optional[int] = None                   # 量表分值(SCALE)
    text: Optional[str] = None                   # 文本答案(TEXT)
    field: Optional[str] = None                  # 字段类型(TEXT)
    rows: Optional[dict[Any, Any]] = None         # 矩阵选择(MATRIX)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AnswerData":
        """从 answering_v2 返回的原始 dict 构造 ``AnswerData``。"""
        try:
            qtype = QuestionType.from_str(d.get("type"))
        except ValueError:
            qtype = QuestionType.SINGLE
        sel = d.get("selected")
        rows = d.get("rows")
        return cls(
            type=qtype,
            selected=list(sel) if sel is not None else None,
            value=int(d["value"]) if d.get("value") is not None else None,
            text=str(d["text"]) if d.get("text") is not None else None,
            field=d.get("field"),
            rows=dict(rows) if rows is not None else None,
        )

    def as_dict(self) -> dict[str, Any]:
        """转回 answering_v2 原始 dict 格式（None 字段省略）。"""
        out: dict[str, Any] = {"type": str(self.type)}
        if self.selected is not None:
            out["selected"] = self.selected
        if self.value is not None:
            out["value"] = self.value
        if self.text is not None:
            out["text"] = self.text
        if self.field is not None:
            out["field"] = self.field
        if self.rows is not None:
            out["rows"] = self.rows
        return out


# ============================================================================
# 提交结果数据模型
# ============================================================================
@dataclass(slots=True)
class SubmitResult:
    """单次提交的结果对象（增强 ``SubmitOutcome`` 字符串三态）。

    设计动机（对照「代码可读性改进建议」第三章 3.3）:
        旧版用 ``str`` 或 ``bool`` 承载提交结果,无法携带上下文信息
        （例如超时时是哪一步超时、按钮定位失败时是哪些选择器都失败）。
        ``SubmitResult`` 把结果状态 + 失败原因 + 调试上下文集中到一个对象,
        方便日志记录、统计分项、回溯问题。

    状态语义:
        - ``SUCCESS``: URL 变化或出现"提交成功"关键词 → 已确认成功
        - ``FAILED``:  按钮定位失败 / 验证码未通过 / iframe 失败 → 已确认失败
        - ``UNKNOWN``: 按钮已点击但效果超时 → 保守计为失败,保留分项统计
    """

    outcome: SubmitOutcome                       # 结果状态(success/failed/unknown)
    reason: str = ""                             # 失败/未知原因的简短描述
    elapsed_ms: int = 0                          # 提交耗时(从点击按钮到判定完成)
    detail: dict[str, Any] = field(default_factory=dict)  # 调试用上下文(选择器列表等)

    @property
    def is_success(self) -> bool:
        """是否已确认成功。"""
        return self.outcome == SUBMIT_SUCCESS

    @property
    def is_failed(self) -> bool:
        """是否已确认失败。"""
        return self.outcome == SUBMIT_FAILED

    @property
    def is_unknown(self) -> bool:
        """是否结果未知（超时,保守计为失败）。"""
        return self.outcome == SUBMIT_UNKNOWN

    @classmethod
    def success(cls, *, elapsed_ms: int = 0, **detail: Any) -> "SubmitResult":
        """构造成功结果。"""
        return cls(outcome=SUBMIT_SUCCESS, elapsed_ms=elapsed_ms, detail=dict(detail))

    @classmethod
    def failed(cls, reason: str = "", *, elapsed_ms: int = 0, **detail: Any) -> "SubmitResult":
        """构造失败结果。"""
        return cls(outcome=SUBMIT_FAILED, reason=reason, elapsed_ms=elapsed_ms, detail=dict(detail))

    @classmethod
    def unknown(cls, reason: str = "", *, elapsed_ms: int = 0, **detail: Any) -> "SubmitResult":
        """构造未知结果（超时）。"""
        return cls(outcome=SUBMIT_UNKNOWN, reason=reason, elapsed_ms=elapsed_ms, detail=dict(detail))


# ============================================================================
# 权重配置项数据模型
# ============================================================================
@dataclass(slots=True)
class WeightConfigEntry:
    """单道题的权重配置项（``WEIGHT_CONFIG`` 每项的类型化版本）。

    各题型字段语义（与 ``src/config_io.py`` 校验逻辑一致）:
        - SINGLE / MULTI / DROPDOWN: ``weights`` 必填,长度 = 选项数
        - MULTI: ``count_options`` + ``count_weights`` 控制选中个数分布
        - SCALE: ``scale`` 必填, ``weights`` 长度 = scale - scale_min + 1
        - TEXT: ``field`` 可选, ``options`` 可选（显式候选文本池）
        - MATRIX_SINGLE: ``rows`` / ``cols`` + ``row_weights``（每行独立权重）
    """

    type: QuestionType
    weights: Optional[list[float]] = None
    count_options: Optional[list[int]] = None
    count_weights: Optional[list[float]] = None
    scale: Optional[int] = None
    scale_min: int = 1
    field: Optional[str] = None
    options: Optional[list[str]] = None
    rows: Optional[list[Any]] = None
    cols: Optional[list[Any]] = None
    row_weights: Optional[dict[Any, list[float]]] = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WeightConfigEntry":
        """从 ``WEIGHT_CONFIG[q]`` 的 dict 构造 ``WeightConfigEntry``。"""
        try:
            qtype = QuestionType.from_str(d.get("type"))
        except ValueError:
            qtype = QuestionType.SINGLE
        cw = d.get("count_weights")
        rw = d.get("row_weights")
        return cls(
            type=qtype,
            weights=list(d["weights"]) if d.get("weights") is not None else None,
            count_options=list(d["count_options"]) if d.get("count_options") is not None else None,
            count_weights=list(cw) if cw is not None else None,
            scale=int(d["scale"]) if d.get("scale") is not None else None,
            scale_min=int(d.get("scale_min", 1)),
            field=d.get("field"),
            options=list(d["options"]) if d.get("options") is not None else None,
            rows=list(d["rows"]) if d.get("rows") is not None else None,
            cols=list(d["cols"]) if d.get("cols") is not None else None,
            row_weights={k: list(v) for k, v in rw.items()} if isinstance(rw, dict) else None,
        )

    def as_dict(self) -> dict[str, Any]:
        """转回 ``WEIGHT_CONFIG[q]`` 的 dict 格式（None 字段省略）。"""
        out: dict[str, Any] = {"type": str(self.type)}
        if self.weights is not None:
            out["weights"] = self.weights
        if self.count_options is not None:
            out["count_options"] = self.count_options
        if self.count_weights is not None:
            out["count_weights"] = self.count_weights
        if self.scale is not None:
            out["scale"] = self.scale
            # 仅当 scale_min 非 1（默认值）时输出，保持 from_dict→as_dict 等价
            if self.scale_min != 1:
                out["scale_min"] = self.scale_min
        if self.field is not None:
            out["field"] = self.field
        if self.options is not None:
            out["options"] = self.options
        if self.rows is not None:
            out["rows"] = self.rows
        if self.cols is not None:
            out["cols"] = self.cols
        if self.row_weights is not None:
            out["row_weights"] = self.row_weights
        return out


# ============================================================================
# 运行状态对象
# ============================================================================
@dataclass(slots=True)
class RunState:
    """运行批次的状态对象（对照「代码可读性改进建议」第四章「状态和全局变量」）。

    设计动机:
        旧版 ``run_batch`` / ``GUI._run_loop`` 分别维护 ``success`` /
        ``fail`` / ``unknown_count`` / ``is_interrupted`` / ``run_id`` 等
        一组散落的局部变量,容易在不同分支忘记同步更新（例如 Ctrl+C
        后忘记把 status 写成 ``interrupted``,导致审查 P1-3）。
        ``RunState`` 把这些状态集中到一个对象,统一管理状态转换,
        CLI 和 GUI 共用同一份状态语义。

    状态转换规则（对照建议第四章 4.3）::

        running   ──success/fail/unknown──>  running
        running   ──Ctrl+C────────────────>  interrupted
        running   ──目标达成/用尽尝试──────>  finished
        interrupted ──(下次启动同 URL)─────>  running  （通过 find_resumable_run 恢复）

    使用约定:
        - CLI ``run_batch`` 内部用 ``RunState`` 替代散落的局部计数器
        - GUI ``_run_loop`` 通过 ``RunState`` 管理 counters / resume /
          stop_flag / final_status / weight_config snapshot / browser 选择,
          避免 ``self.success_count`` / ``self.fail_count`` /
          ``self.current_round`` / ``self.stop_flag`` 等实例变量散落。
        - ``history_db.finish_run`` 直接读 ``state.success_count`` /
          ``state.fail_count`` 等字段
    """

    # ------------ 共用字段（CLI/GUI 都用） ------------
    run_id: Optional[int] = None              # SQLite runs 表的批次 id
    success_count: int = 0                   # 已确认成功份数
    fail_count: int = 0                      # 已确认失败份数(含 unknown)
    unknown_count: int = 0                   # 提交结果未知分项(已计入 fail_count)
    is_interrupted: bool = False              # 是否被用户中断(Ctrl+C / GUI 停止)
    total_elapsed_start: float = 0.0          # 批次起始 perf_counter 时间戳
    attempts_cap: int = 0                     # 最大尝试次数(target_success / GUI 总计划)
    current_attempt: int = 0                  # 当前已尝试次数(基础计数;CLI 用)

    # ------------ GUI 字段（断点续传 / UI 状态） ------------
    resume_start_idx: int = 1                 # 续传起点（= 已成功 + 1）
    total_target: int = 0                     # 计划总份数（GUI total_rounds）
    browser: str = "edge"                     # 浏览器名称（edge/chrome 等）
    use_uc: bool = False                      # 是否使用 undetected-chromedriver
    weight_config_snapshot: Any = None        # 启动时 WEIGHT_CONFIG 快照（dict|None）
    stop_flag: bool = False                   # GUI 用户点击"停止"的标志位
    survey_url: str = ""                      # 问卷 URL（续传 find_resumable_run 已用）

    # ------------ 只读派生属性 ------------
    @property
    def total_count(self) -> int:
        """总尝试次数 = 成功 + 失败(含未知)。"""
        return self.success_count + self.fail_count

    @property
    def has_any_submission(self) -> bool:
        """是否跑过至少一次提交(用于 history 状态判定)。"""
        return self.total_count > 0

    @property
    def displayed_round(self) -> int:
        """GUI 进度条展示的「当前第几份」。

        对全新批次：等于 current_attempt（range 1..attempts_cap）
        对续传批次：等于 resume_start_idx + current_attempt - 1
        即 GUI 原 self.current_round 的语义。
        """
        return max(1, self.resume_start_idx + self.current_attempt - 1)

    @property
    def remaining(self) -> int:
        """剩余尝试次数。"""
        return max(0, self.attempts_cap - self.current_attempt)

    # ------------------------------------------------------------------
    #  状态变更方法(集中更新,避免散落分支不同步)
    # ------------------------------------------------------------------
    def mark_success(self) -> None:
        """记录一次已确认成功的提交。"""
        self.success_count += 1

    def mark_failure(self) -> None:
        """记录一次已确认失败的提交(不含未知)。"""
        self.fail_count += 1

    def mark_unknown(self) -> None:
        """记录一次结果未知(按钮已点击但效果超时)。

        按审查 P1-1 决策:保守计为失败(计入 fail_count),
        但单独累加 unknown_count 便于事后复盘。
        """
        self.unknown_count += 1
        self.fail_count += 1

    def mark_interrupted(self) -> None:
        """标记批次被用户中断(Ctrl+C / GUI 停止按钮)。"""
        self.is_interrupted = True
        self.stop_flag = True

    def request_stop(self) -> None:
        """GUI 点击停止：先写 stop_flag；最终结束时再 mark_interrupted。

        与 CLI Ctrl+C 的差别：CLI 立即抛 KeyboardInterrupt 走 mark_interrupted；
        GUI 需要等待当前单轮结果返回，所以用"软 flag + 延迟 mark"的语义。
        """
        self.stop_flag = True

    def advance_attempt(self) -> int:
        """前进一次尝试计数,返回新的当前尝试序号(从 1 开始)。"""
        self.current_attempt += 1
        return self.current_attempt

    # ------------------------------------------------------------------
    #  GUI 便利：构造快照 dict 深拷贝 WEIGHT_CONFIG（V2 续传用）
    # ------------------------------------------------------------------
    @staticmethod
    def snapshot_weight_config(wc: dict) -> dict | None:
        if not wc:
            return None
        return {
            int(k): dict(v) if isinstance(v, dict) else v
            for k, v in wc.items()
        }

    # ------------------------------------------------------------------
    #  history 表状态字段导出
    # ------------------------------------------------------------------
    def history_status(self) -> str:
        """根据当前状态返回 SQLite runs 表的 ``status`` 字段值。

        - 中断(主动 stop 或 Ctrl+C) → ``"interrupted"``
        - 连一次都没跑就退出 → ``"interrupted"``(极端情况,可恢复)
        - 否则 → ``"finished"``
        """
        if self.is_interrupted or self.stop_flag:
            return "interrupted"
        if not self.has_any_submission:
            return "interrupted"
        return "finished"

    def history_error_message(self, suffix: str = "") -> Optional[str]:
        """返回 SQLite runs 表的 ``error_message`` 字段值。

        :param suffix: 可选追加备注，例如 GUI 浏览器/模式信息。
        """
        base: Optional[str]
        if self.is_interrupted or self.stop_flag:
            base = "Ctrl+C 用户中断"
        elif not self.has_any_submission:
            base = "未执行任何提交即退出"
        else:
            base = None
        if base and suffix:
            return f"{base} · {suffix}"
        return base or (suffix if suffix else None)
