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

v2.3 曾引入 4 个题型 dataclass（QuestionData / AnswerData / SubmitResult /
WeightConfigEntry）打算替换 dict 链路；v2.6 评估后判定不采纳（生产链路全程
``dict[str, Any]``、探测结构由注入 JS 直接产出，dataclass 只会在 JS ↔ Python
边界多一次构造却不消除缺陷），此后它们以"契约文档"名义空挂了 9 个版本、
零生产调用 —— v4.5 随死代码清理删除，取舍记录见 CHANGELOG。本模块留下的
三样东西都在生产链路上：QuestionType 枚举、SubmitOutcome 三态、RunState 状态对象。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Literal, Optional


# ============================================================================
# 提交结果三态（本模块定义，interactions.submit 反向引用）
# ============================================================================
# 放在这里而不是 interactions/submit.py：SubmitResult 的 ``outcome`` 字段要用它，
# 而 models 是「纯数据层」。此前方向是 models → interaction → selenium，
# 导致 ``import src.models`` 会连带加载 100+ 个 selenium.* 模块，
# 也让离线单测无法在不装浏览器依赖的情况下构造 SubmitResult。
SubmitOutcome = Literal["success", "failed", "unknown"]
SUBMIT_SUCCESS: SubmitOutcome = "success"
SUBMIT_FAILED: SubmitOutcome = "failed"
SUBMIT_UNKNOWN: SubmitOutcome = "unknown"


# ============================================================================
# 题型枚举
# ============================================================================
class QuestionType(str, enum.Enum):
    """问卷题型枚举。

    继承 ``str`` 使其可直接 JSON 序列化、与旧字符串字面量 ``==`` 比较,
    例如 ``QuestionType.SINGLE == "single"`` 返回 ``True``。

    成员的 value 是**规范化题型名**（detection / JSON 配置用的那套），
    ``.storage_name`` 是 history.answers.question_type 列的落库名。
    两者只在矩阵题上不同：``matrix_single`` ↔ ``matrix``。
    """

    SINGLE = "single"                # 单选
    MULTI = "multi"                  # 多选
    DROPDOWN = "dropdown"           # 下拉
    SCALE = "scale"                  # 量表/评分
    TEXT = "text"                    # 填空/textarea
    MATRIX_SINGLE = "matrix_single"  # 矩阵单选
    MATRIX_MULTI = "matrix_multi"    # 矩阵多选（v3.0）
    SORT = "sort"                    # 排序题（v3.0）
    MATRIX_SCALE = "matrix_scale"    # 矩阵量表（v3.1：格子里是一排 <a dval>，不是 radio）

    @property
    def storage_name(self) -> str:
        """落库用的短名（目前只有矩阵题与 value 不同）。"""
        return _STORAGE_NAMES[self]

    @property
    def aliases(self) -> tuple[str, ...]:
        """本题型接受的其他写法。"""
        return _ALIASES[self]

    def __str__(self) -> str:  # 类型:ignore[override]
        """直接拿 .value,避免 ``str(QuestionType.SINGLE)`` 返回 ``"QuestionType.SINGLE"``。"""
        return self.value

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
        try:
            return _LOOKUP[s]
        except KeyError:
            # 保持 v2.3 以来的契约：调用方（answering_v2 / weight_panel）
            # 统一 except ValueError 兜底成 SINGLE，不能换成 KeyError
            raise ValueError(f"未知题型: {raw!r}") from None


# 每个题型的"落库短名"。与下面的 _ALIASES 一起构成**唯一**的题型命名表。
_STORAGE_NAMES: dict[QuestionType, str] = {
    QuestionType.SINGLE: "single",
    QuestionType.MULTI: "multi",
    QuestionType.DROPDOWN: "dropdown",
    QuestionType.SCALE: "scale",
    QuestionType.TEXT: "text",
    QuestionType.MATRIX_SINGLE: "matrix",
    # 矩阵多选必须与 matrix 区分：两者 answers 表里 options_selected 的形状不同
    # （标量 vs 每行一个列表），共用短名会让历史明细无法判别是哪种。
    QuestionType.MATRIX_MULTI: "matrix_multi",
    QuestionType.SORT: "sort",
    # 矩阵量表与矩阵选择的答案形状不同（每行一个标量分值 vs 每行一个列表），
    # 落库短名必须区分开，否则历史明细分不出来。
    QuestionType.MATRIX_SCALE: "matrix_scale",
}

# 每个题型接受的别名（不含自己的 value，value 由 _build_tables 自动登记）
_ALIASES: dict[QuestionType, tuple[str, ...]] = {
    QuestionType.SINGLE: ("radio",),
    QuestionType.MULTI: ("checkbox",),
    QuestionType.DROPDOWN: (),
    # nps = 0~10 十一级量表，DOM 与作答方式完全同 scale，只是题面语义不同；
    # 单列一个题型会把 scale 的分支复制一遍，收益只有"配置里写得好看"。
    QuestionType.SCALE: ("rating", "nps"),
    QuestionType.TEXT: ("textarea", "input", "fillblank"),
    QuestionType.MATRIX_SINGLE: ("matrix",),
    QuestionType.MATRIX_MULTI: ("matrix_checkbox", "multi_matrix"),
    QuestionType.SORT: ("ordering", "rank"),
    # 平台把这种题叫"矩阵量表"，页面上的容器 class 是 matrix-rating / scaletablewrap
    QuestionType.MATRIX_SCALE: ("matrix_rating", "scale_matrix"),
}


def _build_tables() -> tuple[dict[str, str], dict[str, QuestionType]]:
    """由 _STORAGE_NAMES + _ALIASES 生成对外的两张视图。"""
    aliases: dict[str, str] = {}
    lookup: dict[str, QuestionType] = {}
    for qtype, storage in _STORAGE_NAMES.items():
        for name in (qtype.value, *qtype.aliases):
            aliases[name] = storage
            lookup[name] = qtype
        aliases[storage] = storage      # 落库短名自身也能反查（如 "matrix"）
        lookup.setdefault(storage, qtype)
    return aliases, lookup


QUESTION_TYPE_ALIASES, _LOOKUP = _build_tables()


# ============================================================================
# 题型别名 → 存储名（由上面的单一真相表派生）
# ============================================================================
# 历史背景：这份别名知识曾散落在 question_stage / weight_panel / controller /
# config_io 四处，v2.4 收进 QUESTION_TYPE_ALIASES 手写表；但 QuestionType.from_str
# 里还留着**另一份**手写别名映射，两张表各自演化迟早会漂（矩阵题的
# "matrix_single vs matrix" 就是这类漂移的高发点）。
# v2.6 起QUESTION_TYPE_ALIASES 与 from_str 共用 _STORAGE_NAMES/_ALIASES 这一份声明。
def normalize_question_type(raw: str | None) -> str:
    """把任意题型字符串归一化为 history 存储名（6 类之一）。

    未识别的字符串原样返回（与旧 ``dict.get(qtype, qtype)`` 行为一致），
    ``None`` 返回空字符串。
    """
    if raw is None:
        return ""
    s = str(raw).lower()
    return QUESTION_TYPE_ALIASES.get(s, s)


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
        running   ──未捕获异常────────────>  failed   （mark_crashed，历史库可区分崩溃批次）
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
    crash_message: Optional[str] = None       # 未捕获异常说明（非空 = 批次崩溃 → history 记 failed）
    no_record_text: bool = False              # 隐私保护：填空题答案不写入 SQLite（CLI 同名参数）
    manual_submit: bool = False               # 人工提交：答完停在提交按钮前由人自己点
                                              # （v4.1 Web 控制台接线；Web 宿主无无头形态，
                                              #  CLI 那条与 --headless 的互斥在这里结构性成立）

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

    def mark_crashed(self, reason: str) -> None:
        """标记批次因未捕获异常终止。

        与 mark_interrupted 的区别：崩溃不可恢复（页面/驱动状态未知），
        history_status() 会返回 ``"failed"`` 而非 ``"interrupted"``，
        避免崩溃批次被误标 finished 污染成功率、或被 find_resumable_run 误恢复。
        """
        self.crash_message = reason or "unknown error"

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

        - 崩溃(未捕获异常, mark_crashed) → ``"failed"``
        - 中断(主动 stop 或 Ctrl+C) → ``"interrupted"``
        - 连一次都没跑就退出 → ``"interrupted"``(极端情况,可恢复)
        - 否则 → ``"finished"``
        """
        if self.crash_message:
            return "failed"
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
        if self.crash_message:
            base = f"运行异常: {self.crash_message}"
        elif self.is_interrupted or self.stop_flag:
            base = "Ctrl+C 用户中断"
        elif not self.has_any_submission:
            base = "未执行任何提交即退出"
        else:
            base = None
        if base and suffix:
            return f"{base} · {suffix}"
        return base or (suffix if suffix else None)
