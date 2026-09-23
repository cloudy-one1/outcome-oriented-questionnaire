"""真实答卷回放（reverse fill）：拿一份**已经收集到的真实答卷表**逐题覆盖生成器。

要解决的问题：加权随机只能保证"这批大致的分布"，保证不了"第 7 份的第 3 题答什么"。
有些场景要的就是后者 —— 补录线下问卷、复现某份被平台判异常的答卷、按真实样本做
小批量投放。本模块把收集表读进来，认列到题号，作答时**外部数据强制覆盖**生成器；
认不到、解析不出的那些格子再逐题交回现有的加权随机。

对标来源：SurveyController 的 ``software/core/reverse_fill/``（GPL-3.0，
**只借思路不取码**）。借过来的三件事：
  1. 表头形如 ``序号 | 1、单选题 | 3、姓名 | 6、城市`` —— 前导序号 + 题干；
  2. 一格三种编码（选项序号 / 选项分值 / 纯文本）自动嗅探，不要求导出方改格式；
  3. **成功提交才推进队列**，失败的那一份重排队时仍拿到同一行（重复提交防线）。

没借的一条：**模糊匹配**。它按相似度认列，我们只认"归一化后相等或互相包含"。
理由是代价不对称 —— 权重配置认错题只是一份分布不准，回放认错列是**把别人问卷的
答案写进这道题**，静默错答比整题退回随机糟得多。所以认不到就明说（``blocked``），
绝不按位置猜。

三种列状态（``ColumnBinding.status``）：
  - ``reverse``  —— 可靠对上题号、题型本版本支持、且能把格子解析成该题的合法答案；
  - ``fallback`` —— 对得上题号但这一格解析不出合法值 → 该份该题交回现有加权随机；
  - ``blocked``  —— 整列认不到题号 / 该题本版本不支持 → 明确报出来，不参与回放。

**本版本的边界：多选题与排序题不回放**（对标源 V1 同样不支持）。判定与理由都收在
``unsupported_reason`` 一处 —— 列计划、逐格编码、启动前告警三条路径共用它，
将来支持了只改那一个函数。原因：它们一格里要放的是"多个值 / 整个次序"，而导出表
那一列的写法在各家模板之间并不统一，猜一个分隔符规则去解析就是拿静默错答赌运气。

答案字典的形状与 ``answering_v2.generate_answer`` **逐键一致**（``type`` +
``selected`` / ``value`` / ``text`` + ``field`` / ``rows``），所以接线时可以原样
替换生成器的返回值。唯一多出来的键是 ``option_blank_text``：
"其他____"这类**选项自带填空框**的项，勾了还必须往那格写字（见
``pipeline_stages/question_stage.py`` 的 ``js_fill_option_blank`` 分支），
回放时这一格的文字也得来自外部数据，不能用内置的"其他原因"池。

使用示例::

    table = load_table("答卷.csv")
    plan = resolve_question_columns(headers_of(table), questions, sample_rows=table)
    for line in preflight(table, questions, target_submissions=20):
        print(line)
    queue = ReplayQueue(table)
    ...
    ans = override_answer(q, queue.peek(submission_index), plan)
    if ans is None:
        ans = generate_answer(q)          # fallback / blocked 都走这里
"""

from __future__ import annotations

import csv
import datetime as _dt
import importlib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Sequence

from .anchoring import normalize_title
from .models import normalize_question_type

__all__ = [
    "STATUS_BLOCKED",
    "STATUS_FALLBACK",
    "STATUS_REVERSE",
    "ColumnBinding",
    "ColumnPlan",
    "ReplayQueue",
    "encode_cell",
    "has_openpyxl",
    "headers_of",
    "load_table",
    "override_answer",
    "preflight",
    "resolve_question_columns",
    "unsupported_reason",
]

# openpyxl —— 只有 .xlsx 答卷表需要它（**可选依赖**，刻意不进 requirements.txt：
# CSV 是必选路径、功能完全等价，为一个 Excel 导出格式给 CLI 用户加必装依赖不值）。
# 用 importlib 而不是 "try: import openpyxl / except ImportError + # type: ignore"：
# 后者在装了的环境里被 pyright 判成冗余 ignore（reportUnnecessaryTypeIgnoreComment
# 是 warning），没装的环境里又报模块解析不了 —— 两侧各留一条，数量随环境 ±1。
# 同一个套路在 gui/qr_utils.py:14-24 已经是仓库范例。注解写成 Any 而不是模块类型，
# 于是 ``openpyxl.load_workbook`` 在本文件里是 Any，不会牵出一条 union 展开。
openpyxl: Any = None
try:
    openpyxl = importlib.import_module("openpyxl")
except ImportError:
    pass


def has_openpyxl() -> bool:
    """是否装了 openpyxl（只影响 .xlsx 一条路径，CSV 不需要它）。"""
    return openpyxl is not None


# ============================================================================
#  列状态常量
# ============================================================================
STATUS_REVERSE = "reverse"
STATUS_FALLBACK = "fallback"
STATUS_BLOCKED = "blocked"

#: 本版本能回放的题型（``models.normalize_question_type`` 的存储名）。
#: 白名单而不是黑名单：探测以后新增题型时，默认**不回放**才是安全的一侧。
_SUPPORTED_TYPES = frozenset({"single", "dropdown", "scale", "text", "matrix", "matrix_scale"})

_TYPE_LABELS = {
    "multi": "多选题",
    "sort": "排序题",
    "matrix_multi": "矩阵多选题",
}


# ============================================================================
#  文本归一化（表头 / 选项 / 格子共用）
# ============================================================================
_WS_RE = re.compile(r"[\s　]+")
# 表头前导序号：``3、`` ``3.`` ``3．`` ``3)`` ``第3题`` ``（3）``（全半角都要）。
# 先过 NFKC，全角数字与括号已经变成半角，这里只需认半角形式。
_HDR_INDEX = re.compile(r"^\s*[({]?\s*第?\s*(\d+)\s*[、.．)）}题]\s*")
_EXPLICIT_Q = re.compile(r"^q[-_. ]*(\d+)$", re.IGNORECASE)
# 一格"选项 + 自填文字"的分隔符：``其他____内容`` / ``其他：内容`` / ``其他——内容``。
_BLANK_SPLIT = re.compile(r"[_＿—–\-]{2,}|[：:]")
# 矩阵格的 ``行!列`` 分隔符与多行分隔符（全角 ! 已由 NFKC 变成半角）。
_PAIR_SEP = re.compile(r"[;|]+")
_ROW_COL = "!"
_SCALE_SUFFIX = re.compile(r"\s*(?:分|档|级|星)\s*$")
_INT_RE = re.compile(r"^-?\d+$")


def _as_int(value: Any) -> int | None:
    """能干净转成整数才转（``"4.0"`` / ``""`` / ``"男"`` 一律 None）。

    刻意不用 ``str.isdigit()`` 前置判断：``int()`` 对 ``"４"`` 这类全角数字本来就是
    对的，而我们比较前统一走过 NFKC，这里只要挡住小数与空串就够了。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value if value is not None else "").strip()
    if not _INT_RE.match(text):
        return None
    try:
        return int(text)
    except ValueError:                      # pragma: no cover - 正则已挡住，留一手
        return None


def _norm(value: Any) -> str:
    """比对用归一化：NFKC（全半角统一）→ 去空白 → 小写。"""
    text = unicodedata.normalize("NFKC", str(value if value is not None else ""))
    return _WS_RE.sub("", text).lower()


def _norm_option(value: Any) -> str:
    """选项文本归一化：在 ``_norm`` 之上再砍掉填空下划线及其后内容。

    题面上的 ``其他____`` 与表里的 ``其他`` / ``其他：原因`` 必须能对上，
    而 ``normalize_title`` 会把标点整串删掉 —— 那对题干是对的，对选项会把
    ``A-`` 和 ``A`` 也判成同一个，所以这里只认填空标记那一种截断。
    """
    text = _norm(value)
    cut = _BLANK_SPLIT.search(text)
    return text[: cut.start()] if cut else text


def _title_part(header: str) -> str:
    """表头去掉前导序号与空白后的题干（复用 anchoring 的归一化，单一真相）。"""
    return normalize_title(header)


def _title_hit(a: str, b: str) -> bool:
    """题干是否算"同一道题"。

    三档，从严到宽：归一化后相等；两边都 ≥4 字且一方完整包含另一方（截断的长题干）；
    短题干只认**前缀** —— 导出表头常写成 ``姓名（必填）``，而 ``姓名`` 两字的题也真实
    存在，放宽到"任意位置包含"就会让 ``城市`` 这种两字词到处乱认。短词出现在中间
    不算命中。

    不含 difflib 比例匹配 —— 见模块文档里"没借的一条"。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 2 and long.startswith(short)


# ============================================================================
#  读表
# ============================================================================
def load_table(path: str) -> list[dict[str, str]]:
    """读取真实答卷表，返回"每行一个 dict（表头 → 格子文本）"。

    ``.csv`` 走 stdlib ``csv``（必选路径，零依赖）；``.xlsx`` 走 openpyxl（可选依赖）。

    :raises FileNotFoundError: 文件不存在。
    :raises ValueError: 扩展名不是 .csv / .xlsx。
    :raises RuntimeError: 要读 .xlsx 但没装 openpyxl（**不静默降级** ——
        静默返回空表的样子是"回放开了却没生效"，正是要防的那种无声失效）。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"答卷表文件不存在: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xlsx":
        headers, rows = _read_xlsx(path)
    elif ext in (".csv", ".txt", ""):
        headers, rows = _read_csv(path)
    else:
        raise ValueError(
            f"不支持的答卷表格式 {ext!r}（只支持 .csv 与 .xlsx）。"
            "问卷星后台「下载答卷」两种都能出，导出成 CSV 即可，无需额外依赖。"
        )
    return _assemble(headers, rows)


def headers_of(table: list[dict[str, str]]) -> list[str]:
    """从 ``load_table`` 的结果取列顺序（dict 保序，第一行的键序就是列序）。"""
    for row in table:
        if row:
            return list(row.keys())
    return []


def _read_csv(path: str) -> tuple[list[str], list[list[str]]]:
    """stdlib csv → (表头, 数据行)。编码先 utf-8-sig（吃 Excel 那层 BOM），
    解不动再退 GB18030：问卷星后台直接导出的 CSV 常见是 GBK 系，
    而"另存为 CSV"经 Excel 又常变 UTF-8 —— 只认一种的话症状是"读不出中文"，
    跟表本身对不对没关系。"""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            rows = [[_cell_text(c) for c in raw] for raw in csv.reader(f)]
    except UnicodeDecodeError:
        with open(path, "r", encoding="gb18030", newline="") as f:
            rows = [[_cell_text(c) for c in raw] for raw in csv.reader(f)]
    return _split_header(rows)


def _read_xlsx(path: str) -> tuple[list[str], list[list[str]]]:
    if openpyxl is None:
        raise RuntimeError(
            "读取 .xlsx 答卷表需要 openpyxl，本仓库刻意没把它列进必装依赖。"
            "两条出路任选：pip install openpyxl，或在问卷星后台把答卷导出为 CSV"
            "（CSV 是必选路径，功能完全等价）。"
        )
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book.active
        rows = [[_cell_text(c) for c in row] for row in sheet.iter_rows(values_only=True)]
    finally:
        book.close()                        # read_only 的工作簿握着 zip 句柄，不关会一直占着文件
    return _split_header(rows)


def _cell_text(value: Any) -> str:
    """单元格 → 文本。

    数字必须特殊处理：openpyxl 给的是 ``3.0`` 这类 float，``str()`` 出来的
    ``"3.0"`` 会让"选项序号 3"变成解析不出（``_as_int`` 只认整数串），
    于是整列被误判成 fallback。真值是整数就写成整数。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value).strip()


def _split_header(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    for i, row in enumerate(rows):                     # 跳过导出文件顶部那几行空行
        if any(str(c).strip() for c in row):
            return [_cell_text(c) for c in row], rows[i + 1:]
    return [], []


def _assemble(headers: list[str], rows: list[list[str]]) -> list[dict[str, str]]:
    """按列名把行拼成 dict；列名空或重名时补齐成可用的键。

    重名是真事（一份问卷里两个"其他"、两份导出并排粘），而 dict 只能留一个键 ——
    后一列写成 ``其他#7`` 而不是丢掉：位置信息一旦丢了，那一列就永远认不回来。
    键规则只有一份（``_assemble_keys``）：列计划拿的是同一份键，两处不一致的话
    查表就会拿到别人的格子。
    """
    keys = _assemble_keys(headers)
    out: list[dict[str, str]] = []
    for row in rows:
        if not any(str(c).strip() for c in row):       # 表尾空行
            continue
        item: dict[str, str] = {}
        for i, key in enumerate(keys):
            item[key] = _cell_text(row[i]) if i < len(row) else ""
        out.append(item)
    return out


# ============================================================================
#  列计划
# ============================================================================
@dataclass
class ColumnBinding:
    """一列与一道题的绑定结果。"""

    key: str                       # 行 dict 里的键（重名列会带 ``#列号`` 后缀）
    header: str                    # 表头原文
    column: int                    # 0-based 列序
    status: str                    # STATUS_REVERSE / _FALLBACK / _BLOCKED
    qnum: int | None = None
    reason: str = ""               # 为什么是这个状态（人读，preflight 直接拼句）

    @property
    def replayable(self) -> bool:
        """这一列的值要不要覆盖生成器（只有 reverse 才覆盖）。"""
        return self.status == STATUS_REVERSE


@dataclass
class ColumnPlan:
    """整张表的列 → 题号绑定结果。"""

    bindings: list[ColumnBinding] = field(default_factory=list)

    def by_status(self, status: str) -> list[ColumnBinding]:
        return [b for b in self.bindings if b.status == status]

    @property
    def reverse(self) -> list[ColumnBinding]:
        return self.by_status(STATUS_REVERSE)

    @property
    def fallback(self) -> list[ColumnBinding]:
        return self.by_status(STATUS_FALLBACK)

    @property
    def blocked(self) -> list[ColumnBinding]:
        return self.by_status(STATUS_BLOCKED)

    def binding_for(self, qnum: int) -> ColumnBinding | None:
        """这道题由哪一列负责（blocked 的列不算负责）。"""
        want = int(qnum)
        for b in self.bindings:
            if b.qnum == want and b.status != STATUS_BLOCKED:
                return b
        return None


def resolve_question_columns(
    headers: Sequence[str],
    detected_questions: Sequence[dict[str, Any]],
    sample_rows: Sequence[dict[str, str]] | None = None,
) -> ColumnPlan:
    """把表头认到题号上。

    :param headers: 表头顺序（``headers_of(table)`` 或直接给列名列表）
    :param detected_questions: ``detection.detect_questions`` 的返回
    :param sample_rows: 可选，给了样本行才能做**嗅探降级** —— 一列结构上认得出题号、
        但样本里的格子一个都解析不出合法答案时，整列判 ``fallback``，
        省得每份每题都白解析一遍（``override_answer`` 仍逐格再判，两者不冲突）
    """
    by_qnum: dict[int, dict[str, Any]] = {}
    for q in detected_questions:
        qi = _as_int(q.get("q"))
        if qi is not None:
            by_qnum[qi] = dict(q)

    keys = _assemble_keys(list(headers))
    claimed: dict[int, str] = {}
    bindings: list[ColumnBinding] = []
    for i, raw_header in enumerate(headers):
        header = _cell_text(raw_header)
        binding = _resolve_one(header, keys[i], i, by_qnum, claimed)
        if binding.status == STATUS_REVERSE and binding.qnum is not None:
            claimed[binding.qnum] = header
            if sample_rows is not None:
                question = by_qnum.get(binding.qnum)
                if question is not None:
                    _sniff_samples(binding, question, sample_rows)
        bindings.append(binding)
    return ColumnPlan(bindings)


def _assemble_keys(headers: list[str]) -> list[str]:
    """与 ``_assemble`` 同一套键规则 —— 两处不一致的话查表就会拿到别人的格子。"""
    keys: list[str] = []
    seen: set[str] = set()
    for i, header in enumerate(headers):
        base = _cell_text(header) or f"第{i + 1}列"
        key = base if base not in seen else f"{base}#{i + 1}"
        seen.add(key)
        keys.append(key)
    return keys


def _resolve_one(
    header: str,
    key: str,
    column: int,
    by_qnum: dict[int, dict[str, Any]],
    claimed: dict[int, str],
) -> ColumnBinding:
    def blocked(reason: str, qnum: int | None = None) -> ColumnBinding:
        return ColumnBinding(key=key, header=header, column=column,
                             status=STATUS_BLOCKED, qnum=qnum, reason=reason)

    explicit = _explicit_qnum(header)
    idx = explicit if explicit is not None else _header_index(header)
    title = _title_part(header) if explicit is None else ""
    hits = [qi for qi, q in by_qnum.items() if _title_hit(title, _norm(q.get("title")))]
    hits = sorted(hits)

    if idx is None and not hits:
        return blocked("表头既没有可解析的题号，题干也没匹配到本卷任何一道题")
    if len(hits) > 1:
        return blocked(
            f"题干同时匹配到 Q{'、Q'.join(str(h) for h in hits)}，位置不能猜",
            qnum=hits[0],
        )
    qnum = idx if idx is not None else hits[0]
    target = by_qnum.get(qnum)

    if hits and idx is not None and hits[0] != idx:
        return blocked(
            f"表头序号指向 Q{idx}，题干却匹配到 Q{hits[0]} —— 两道题之间没得选，"
            "很可能是这份表对的不是当前这张问卷"
        )
    if target is None:
        return blocked(f"本卷探测结果里没有 Q{qnum}（题号超出范围或该题未被探测到）")
    if idx is not None and not hits and title and _norm(target.get("title")):
        return blocked(
            f"表头序号指向 Q{qnum}，但那道题的题干「{str(target.get('title')).strip()[:20]}」"
            f"与表头「{title[:20]}」对不上"
        )

    why = unsupported_reason(target)
    if why:
        return blocked(why, qnum=qnum)
    gap = _structure_gap(target)
    if gap:
        return ColumnBinding(key=key, header=header, column=column,
                             status=STATUS_FALLBACK, qnum=qnum, reason=gap)
    owner = claimed.get(qnum)
    if owner is not None:
        return blocked(f"Q{qnum} 已被「{owner}」列认领，同一道题不重复制定", qnum=qnum)
    return ColumnBinding(key=key, header=header, column=column,
                         status=STATUS_REVERSE, qnum=qnum)


def _explicit_qnum(header: str) -> int | None:
    """显式列名 ``q7`` / ``Q7`` / ``q_7``。"""
    m = _EXPLICIT_Q.match(_norm(header))
    return int(m.group(1)) if m else None


def _header_index(header: str) -> int | None:
    """表头的前导序号（``3、`` / ``3.`` / ``第3题`` / ``（3）``，全半角均可）。"""
    text = unicodedata.normalize("NFKC", str(header or ""))
    m = _HDR_INDEX.match(text)
    return _as_int(m.group(1)) if m else None


def _structure_gap(question: dict[str, Any]) -> str | None:
    """题型支持、但探测结构不足以把格子折成答案 → 整列 fallback 的理由。"""
    qtype = normalize_question_type(str(question.get("type", "single"))) or "single"
    if qtype in ("single", "dropdown"):
        if not (question.get("choices") or []):
            return "该题的探测结果里没有选项列表（choices 为空），无法把格子折成选项"
    if qtype in ("matrix", "matrix_scale"):
        if not (question.get("rows") or []) or not (question.get("cols") or []):
            return "该题的探测结果里没有矩阵的行/列结构，无法定位到具体某一行"
    return None


def _sniff_samples(
    binding: ColumnBinding,
    question: dict[str, Any],
    sample_rows: Sequence[dict[str, str]],
) -> None:
    """有样本就把整列嗅探一遍：一个合法值都解析不出 → 降级成 fallback。"""
    nonempty = 0
    for row in sample_rows:
        cell = row.get(binding.key)
        if not str(cell or "").strip():
            continue
        nonempty += 1
        if encode_cell(str(cell), question) is not None:
            return
    if nonempty:
        binding.status = STATUS_FALLBACK
        binding.reason = "这一列的样本值一个都解析不出合法答案（选项文本在题面上找不到）"


# ============================================================================
#  逐格编码（三种编码嗅探）
# ============================================================================
def unsupported_reason(question: dict[str, Any]) -> str | None:
    """这道题本版本能不能回放；能则 None，不能则给出人读理由。

    **本版本不支持多选与排序（以及矩阵多选）的判定就收在这一处**：
    ``resolve_question_columns``（整列 blocked）、``encode_cell``（逐格拒编）、
    ``preflight``（启动前告知）三条路径都读它，将来放开只改这里。
    """
    qtype = normalize_question_type(str(question.get("type", "single"))) or "single"
    label = _TYPE_LABELS.get(qtype)
    if label is not None:
        return (
            f"{label}本版本不回放：一格要放的是多个值/整个次序，"
            "而导出表那一列的写法在各家模板之间不统一（对标源 V1 同样不支持）"
        )
    if qtype not in _SUPPORTED_TYPES:
        return f"题型 {qtype!r} 不在本版本的回放题型里（支持：{', '.join(sorted(_SUPPORTED_TYPES))}）"
    return None


def encode_cell(cell_text: str, question: dict[str, Any]) -> dict[str, Any] | None:
    """把一格外部数据编码成该题的答案字典；解析不出返回 None（→ 该题交回随机）。

    返回形状与 ``answering_v2.generate_answer`` 逐键一致，可直接替换其返回值。
    嗅探顺序（对标源的三种编码，按"信文本还是信数字"的代价排）：

      1. **选项全文** —— 与选项文本归一化比对；``其他____xxx`` 这类带填空的项
         同时产出 ``selected`` 与 ``option_blank_text``。放最前是因为页面上写着的
         字比一个孤立数字更不容易撞别人。
      2. **纯数字** —— 先按选项 value/分值命中（导出表常直接写 value），
         再退到 1-based 选项序号折成 0-based 下标。两者在 value 就是 1..N 的
         常见导出上重合，所以顺序不会改变结果；不重合时信 value 更贴导出原意。
      3. **纯文本** —— 填空题（含多空填空的一格）原样作答。

    多选 / 排序 / 矩阵多选不在 ``_SUPPORTED_TYPES`` 里：这里返回 None 只是防御性
    兜底，调用方**必须**先经 ``resolve_question_columns``（那些列会是 blocked，
    带上 ``unsupported_reason`` 的说明），不能把"不支持"误当成"这格解析不出"。
    """
    raw = str(cell_text if cell_text is not None else "").strip()
    if not raw:
        return None
    cell = unicodedata.normalize("NFKC", raw)
    qtype = normalize_question_type(str(question.get("type", "single"))) or "single"
    if unsupported_reason(question) is not None:
        return None

    if qtype == "text":
        return _encode_text(raw, question)
    if qtype in ("single", "dropdown"):
        return _encode_choice(cell, question, qtype)
    if qtype == "scale":
        return _encode_scale(cell, question)
    if qtype == "matrix":
        return _encode_matrix(cell, question, "matrix_single")
    if qtype == "matrix_scale":
        return _encode_matrix(cell, question, "matrix_scale")
    return None                                 # pragma: no cover - 白名单已挡在前面


def _encode_text(raw: str, question: dict[str, Any]) -> dict[str, Any] | None:
    """填空题：纯文本原样答。只校验平台侧真有硬校验的两类（数字框、日期框）——
    越界的日期会被平台自己的 laydate 校验清掉（见 detection 的 date_min/date_max），
    那比退回随机生成更糟。"""
    field_name = str(question.get("field", "") or "").lower() or None
    if field_name in ("age", "number") and not _valid_age(raw, question):
        return None
    if field_name == "date" and not _valid_date(raw, question):
        return None
    return {"type": "text", "text": raw, "field": field_name}


def _valid_age(raw: str, question: dict[str, Any]) -> bool:
    n = _as_int(raw)
    if n is None:
        return False
    lo = _as_int(question.get("min")) if question.get("min") is not None else None
    hi = _as_int(question.get("max")) if question.get("max") is not None else None
    return (lo is None or n >= lo) and (hi is None or n <= hi)


_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m",
)


def _valid_date(raw: str, question: dict[str, Any]) -> bool:
    text = unicodedata.normalize("NFKC", raw).strip().replace("T", " ")
    if str(question.get("date_kind") or "") == "time":
        return bool(re.match(r"^\d{1,2}:\d{2}$", text))
    parsed = _parse_date(text)
    if parsed is None:
        return False
    low = _parse_date(question.get("date_min"))
    high = _parse_date(question.get("date_max"))
    if low is not None and parsed < low:
        return False
    return not (high is not None and parsed > high)


def _parse_date(raw: Any) -> "_dt.datetime | None":
    """按几种常见导出格式试解析；全不对返回 None。"""
    text = str(raw if raw is not None else "").strip().replace("T", " ")
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _scale_bounds(question: dict[str, Any]) -> tuple[int, int] | None:
    """与 answering_v2 同一套默认值（scale=5、scale_min=1），否则回放与生成的边界会漂。"""
    smax = _as_int(question.get("scale")) if question.get("scale") is not None else 5
    smin = _as_int(question.get("scale_min")) if question.get("scale_min") is not None else 1
    if smax is None or smin is None or smax < smin:
        return None
    return smin, smax


def _encode_scale(cell: str, question: dict[str, Any]) -> dict[str, Any] | None:
    bounds = _scale_bounds(question)
    if bounds is None:
        return None
    lo, hi = bounds
    n = _as_int(_SCALE_SUFFIX.sub("", cell))
    if n is None or n < lo or n > hi:
        return None
    return {"type": "scale", "value": n}


def _option_pairs(question: dict[str, Any]) -> list[tuple[Any, str]]:
    """``(选项 value, 选项文本)`` 对；value 进答案，文本只用来认格子。"""
    choices = list(question.get("choices") or [])
    texts = _option_texts(question, choices)
    return list(zip(choices, texts))


def _option_texts(question: dict[str, Any], choices: list[Any]) -> list[str]:
    """与 ``choices`` 一一对齐的选项文本。

    探测回来的 ``choices`` 是**选项 value**（radio/checkbox 的 DOM value，通常是
    1..N 的整数），不是页面上那行字 —— 所以"选项全文匹配"要么题面本身就把值写成
    了文本（下拉的 ``choices`` 常是 "A"/"北京"），要么调用方补一份映射。
    ``option_texts`` 就是这个补口：list（DOM 顺序）或 dict（value → 文本）。
    没补时退化成 ``str(value)``，纯数字那一列照旧能回放。
    """
    raw = question.get("option_texts")
    out: list[str] = [str(c) for c in choices]
    if isinstance(raw, dict):
        for i, c in enumerate(choices):
            hit = raw.get(c, raw.get(str(c)))
            if hit is not None:
                out[i] = str(hit)
    elif isinstance(raw, (list, tuple)):
        for i, t in enumerate(raw):
            if i < len(out):
                out[i] = str(t)
    return out


def _encode_choice(cell: str, question: dict[str, Any], out_type: str) -> dict[str, Any] | None:
    pairs = _option_pairs(question)
    if not pairs:
        return None
    blanks = {str(x) for x in (question.get("blank_options") or [])}
    texts = [t for _v, t in pairs]
    values = [v for v, _t in pairs]

    parts = _BLANK_SPLIT.split(cell, maxsplit=1)
    left = parts[0].strip()
    right = parts[1].strip() if len(parts) > 1 else ""

    idx = _text_index(left, texts)
    if idx is None and right:
        idx = _text_index(cell, texts)          # 选项文本里本来就带冒号
    if idx is None:
        n = _as_int(cell)
        if n is not None:
            idx = _value_index(n, values)
            if idx is None and 1 <= n <= len(pairs):
                idx = n - 1                          # 序号编码：1-based → 0-based 下标
    if idx is None:
        return None
    value = values[idx]
    ans: dict[str, Any] = {"type": out_type, "selected": [value]}
    if right and str(value) in blanks:
        ans["option_blank_text"] = right
    return ans


def _text_index(token: str, texts: list[str]) -> int | None:
    want = _norm_option(token)
    if not want:
        return None
    for i, text in enumerate(texts):
        if _norm_option(text) == want:
            return i
    return None


def _value_index(n: int, choices: list[Any]) -> int | None:
    """把纯数字先当成"选项值/分值"命中一次（导出表里那一列经常就是 value）。"""
    for i, c in enumerate(choices):
        if _as_int(c) == n:
            return i
    return None


def _encode_matrix(cell: str, question: dict[str, Any], out_type: str) -> dict[str, Any] | None:
    """矩阵题：``行!列``（多行用 ``;`` 或 ``|`` 分隔），行/列都可用序号或原文。

    整格只有一个值时按"每行同一个列"处理（真实答卷里"全部打 4 分"就是这么写的）。
    行 token 匹配的是探测回来的 ``rows``：矩阵单选是行号、矩阵量表是提交槽名
    （``q7_0``），所以纸面上的行标签要靠序号位对上。
    """
    rows = list(question.get("rows") or [])
    cols = list(question.get("cols") or [])
    if not rows or not cols:
        return None
    picked: dict[Any, Any] = {}
    for token in _PAIR_SEP.split(cell):
        text = token.strip()
        if not text:
            continue
        row_part, sep, col_part = text.partition(_ROW_COL)
        if not sep:
            ci = _col_index(text, cols)
            if ci is None:
                return None
            for r in rows:
                picked[r] = cols[ci]
            continue
        ri = _row_index(row_part, rows)
        ci = _col_index(col_part, cols)
        if ri is None or ci is None:
            return None
        picked[rows[ri]] = cols[ci]
    if len(picked) != len(rows):
        # 只答了半张矩阵 = 剩下那些行整题空着，平台按"未答完整"拦下整题。
        # 与其交一份必挂的，不如这一份这一题退回随机生成（它保证每行都有值）。
        return None
    return {"type": out_type, "rows": picked}


def _row_index(token: str, rows: list[Any]) -> int | None:
    idx = _text_index(token, [str(r) for r in rows])
    if idx is not None:
        return idx
    n = _as_int(token)
    if n is not None and 1 <= n <= len(rows):
        return n - 1
    return None


def _col_index(token: str, cols: list[Any]) -> int | None:
    idx = _text_index(token, [str(c) for c in cols])
    if idx is not None:
        return idx
    n = _as_int(token)
    if n is None:
        return None
    idx = _value_index(n, cols)
    if idx is not None:
        return idx
    return n - 1 if 1 <= n <= len(cols) else None


# ============================================================================
#  回放队列
# ============================================================================
class ReplayQueue:
    """按 ``submission_index`` 供行的队列：成功提交才推进，失败重排队拿到同一行。

    幂等是"重复提交"的防线：一份答卷失败后重投，如果第二次拿到了**另一行**，
    症状是同一批里出现两份一模一样的真实答卷内容（平台按内容查重时全挂），
    而且第 N+1 行被跳过、后面的行整体错位一位。所以 ``peek`` 一旦分过行就记住。

    行用尽后 ``peek`` 返回 None —— 调用方回退正常生成，而不是抛异常：
    表只有 12 份却要投 20 份是正常用法，不是错误。
    """

    def __init__(self, table: Sequence[dict[str, str]], *, skip: int = 0) -> None:
        self._rows: list[dict[str, str]] = [dict(r) for r in table]
        self._next: int = max(int(skip), 0)
        self._assigned: dict[int, int] = {}
        self._consumed: set[int] = set()

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def remaining(self) -> int:
        """还没发出去的行数（不是"还没消费的行数"，发出去没提交完也算在内）。"""
        return max(len(self._rows) - self._next, 0)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    @property
    def consumed_indices(self) -> set[int]:
        return set(self._consumed)

    def peek(self, index: int) -> dict[str, str] | None:
        """取第 ``index`` 份（1-based 的 submission_index）该用的行；行用尽返回 None。"""
        key = int(index)
        if key in self._assigned:
            pos = self._assigned[key]
            return dict(self._rows[pos]) if pos < len(self._rows) else None
        if self._next >= len(self._rows):
            return None
        pos = self._next
        self._next += 1
        self._assigned[key] = pos
        return dict(self._rows[pos])

    def mark_consumed(self, index: int) -> None:
        """**只在提交成功后调用**：这份答卷真的落库了，行才算用掉。

        没 ``peek`` 过就直接标记时补一次分配，把那行发出去 —— 否则下一份会拿到
        同一行，等于把"成功提交才推进"漏成了一个静默的重号。
        """
        key = int(index)
        if key not in self._assigned:
            self.peek(key)
        self._consumed.add(key)


# ============================================================================
#  作答期的接缝
# ============================================================================
def override_answer(
    question: dict[str, Any],
    row: dict[str, str] | None,
    plan: ColumnPlan,
) -> dict[str, Any] | None:
    """用回放表的这一格**强制覆盖**这道题的答案；None = 交回现有加权随机。

    返回 None 的四种情况都是"降级"而不是"错误"：没有行（表用尽）、这道题没有对应列、
    列是 blocked/fallback、以及该格解析不出合法值（逐格的 ``fallback``）。
    """
    if row is None:
        return None
    qnum = _as_int(question.get("q"))
    if qnum is None:
        return None
    binding = plan.binding_for(qnum)
    if binding is None or not binding.replayable:
        return None
    cell = row.get(binding.key)
    if cell is None or not str(cell).strip():
        return None
    return encode_cell(str(cell), question)


# ============================================================================
#  启动前告警
# ============================================================================
def preflight(
    table: Sequence[dict[str, str]],
    detected_questions: Sequence[dict[str, Any]],
    target_submissions: int | None = None,
) -> list[str]:
    """跑之前的告警清单（人读中文句子）。**这里不抛异常** —— 拦截与降级
    的区别很重要：列对不上题只是"这一题照旧随机"，不是"这次运行不能跑"。
    """
    rows: list[dict[str, str]] = [dict(r) for r in table]
    headers = headers_of(rows)
    plan = resolve_question_columns(headers, list(detected_questions), sample_rows=rows)
    out: list[str] = []

    if not rows:
        out.append("[回放] 答卷表是空的 → 本次运行与不启用回放完全一致（逐题走加权随机）")

    for b in plan.blocked:
        out.append(f"[回放] 列「{b.header}」{b.reason} → 这一列不回放")
    for b in plan.fallback:
        out.append(
            f"[回放] 列「{b.header}」（Q{b.qnum}）{b.reason} → 这些份的该题走加权随机"
        )

    claimed = {b.qnum for b in plan.bindings if b.qnum is not None}
    missing = [
        int(q["q"]) for q in detected_questions
        if _as_int(q.get("q")) is not None and int(q["q"]) not in claimed
    ]
    if missing:
        out.append(
            "[回放] 探测到的 "
            + "、".join(f"Q{n}" for n in sorted(missing))
            + " 在表里没有对应列 → 这些题照旧随机生成"
        )

    questions = list(detected_questions)
    if questions and not any(str(q.get("title") or "").strip() for q in questions):
        out.append(
            "[回放] 本次探测没带回题干，只能按表头的前导序号认题 —— "
            "请核对这份答卷表与当前问卷是同一版本（题序变了会整排错位）"
        )

    target = _as_int(target_submissions)
    if target is not None and target > len(rows):
        out.append(
            f"[回放] 答卷表只有 {len(rows)} 行，本次要提交 {target} 份 → "
            f"第 {len(rows) + 1} 份起队列耗尽，回退正常生成"
        )
    return out


# ============================================================================
#  本次批次的回放状态：CLI ``--replay-file`` 开起来，作答链逐题来问
# ============================================================================
@dataclass
class _Session:
    path: str
    table: list[dict[str, str]]
    queue: ReplayQueue
    headers: list[str]
    _plans: dict[int, ColumnPlan] = field(default_factory=dict)
    _noted: set[int] = field(default_factory=set)


_session: _Session | None = None


def begin_replay(path: str, target_submissions: int | None = None) -> list[str]:
    """装载答卷表并开启回放，返回启动时该说给用户的告警（不抛）。

    ``target_submissions`` 只用来多算一条"表比份数短"的提示 —— 这句话必须
    在**开始之前**说，等队列耗尽才发现，用户看到的就是一串"莫名变回随机"。
    """
    global _session
    table = load_table(path)
    _session = _Session(
        path=path, table=table, queue=ReplayQueue(table), headers=headers_of(table)
    )
    return preflight(table, [], target_submissions=target_submissions)


def end_replay() -> None:
    global _session
    _session = None


def replaying() -> bool:
    return _session is not None


def replay_path() -> str | None:
    return None if _session is None else _session.path


def remaining_rows() -> int:
    return 0 if _session is None else _session.queue.remaining


def _plan_for(session: _Session, question: dict[str, Any]) -> ColumnPlan:
    """按题缓存绑定：整表重认一次只要几十次字符串比对，但每题只需认一次。"""
    qnum = _as_int(question.get("q"))
    assert qnum is not None
    plan = session._plans.get(qnum)
    if plan is None:
        plan = resolve_question_columns(session.headers, [question])
        session._plans[qnum] = plan
    return plan


def _note(session: _Session, qnum: int, text: str) -> None:
    """同一道题只说一次：逐份提交都印一行会淹掉日志。"""
    if qnum in session._noted:
        return
    session._noted.add(qnum)
    print(f"  [回放] {text}")


def answer_for_question(
    question: dict[str, Any], submission_index: int | None
) -> dict[str, Any] | None:
    """这一题这一份要不要用答卷表里的值覆盖；``None`` = 照旧随机生成。

    降级而不是报错的三种情形都属正常：没开回放、表用尽、这道题没有可用列。
    """
    session = _session
    if session is None:
        return None
    qnum = _as_int(question.get("q"))
    if qnum is None or submission_index is None:
        return None
    row = session.queue.peek(int(submission_index))
    if row is None:
        _note(session, qnum,
              f"答卷表已用尽（{len(session.table)} 行）→ Q{qnum} 起回退随机生成")
        return None
    reason = unsupported_reason(question)
    if reason is not None:
        _note(session, qnum, f"Q{qnum} {reason} → 本版本不回放这一题")
        return None
    binding = _plan_for(session, question).binding_for(qnum)
    if binding is None or not binding.replayable:
        status = "没有对应列" if binding is None else f"这一列判为 {binding.status}"
        _note(session, qnum, f"Q{qnum}（{str(question.get('title') or '')[:20]}）"
                             f"{status} → 照旧随机生成")
        return None
    answer = encode_cell(str(row.get(binding.key) or ""), question)
    if answer is None:
        _note(session, qnum, f"Q{qnum} 第 {submission_index} 行的值解析不出合法答案"
                             " → 该题照旧随机生成")
    return answer


def mark_consumed(submission_index: int | None) -> int:
    """整份提交成功后推进队列；返回推进后的剩余行数（没开回放返回 -1）。"""
    session = _session
    if session is None or submission_index is None:
        return -1
    session.queue.mark_consumed(int(submission_index))
    return session.queue.remaining


def reset_for_survey() -> None:
    """换一份问卷：清掉按题号缓存的列绑定与"只说一次"的提示记录。

    绑定缓存的键是题号，而题号在不同问卷之间是重复使用的 —— 不清的话第二份问卷
    会拿第一份的表头绑定去认题，症状是"答案看着来自表，其实来自上一份问卷的列"。
    """
    if _session is None:
        return
    _session._plans.clear()
    _session._noted.clear()
