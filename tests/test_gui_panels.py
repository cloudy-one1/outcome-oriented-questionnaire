"""gui/ 面板与组件的离线契约测试（v2.6 覆盖率缺口补齐）。

为什么值得测：README「已知缺口」表里 ``gui/`` 只有 13%~35%，而它约占生产代码
的 41%，并且是用户唯一真正摸得到的表面 —— 权重字符串解析错位、历史面板静默
丢行，``src/`` 上跑得再绿的 CI 也看不见。本文件钉住：

    1. gui/theme.py + gui/widgets.py：颜色/渐变纯函数的可观察结果、组件工厂的
       标题/命令/变量联动，以及 ``apply_ttk_style`` 这条从未执行过的 60 行样式
       配置路径；
    2. gui/weight_panel.py：权重字符串 ↔ WEIGHT_CONFIG dict 的双向映射，含
       "格式错 / 数量不符 → 只走 log_fn 记 WARN 不崩"、"留空 → 不落 dict，交给
       src/answering.py 的等权重分支"；
    3. gui/history_panel.py 的 v2.6 两处修复 —— ``_csv_safe`` 的 CSV 公式注入
       前缀（README 已对外承诺，此前零测试）、``get_db()`` 单实例缓存 +
       ``close_db()`` 清缓存（此前每点一次「历史」Tab 就在 Tk 主线程全表扫描）；
    4. gui/log_view.py 的队列 → 终端渲染接缝、gui/qr_utils.py 的失败路径
       （不得弹窗阻塞、必须返回 None）。

Tk 基座：同进程反复建/销 tk.Tk() 会在 Windows 上间歇性抛
"TclError: this probably means that tk wasn't installed properly"（见
tests/test_gui_run_loop.py 的同段说明），所以本模块**只建一个**已 withdraw 的
根窗口，由 module 作用域 fixture 建、其 finalizer 销；全程不 mainloop()。
根窗口建不出来（无显示的 CI runner）→ 整模块 skip，CI 保持绿。

刻意不构造 SurveyGUI：它的 __init__ 会打开真实 data/history.db、启动动画 after
循环并自动载入 configs/default_weight_config.json。所有需要 DB / 配置文件的
地方一律走 pytest 的 tmp_path。
"""

from __future__ import annotations

import csv
import os
import queue
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")
ttk = pytest.importorskip("tkinter.ttk")

from gui import history_panel as hp  # noqa: E402
from gui import qr_utils  # noqa: E402
from gui import theme  # noqa: E402
from gui import widgets  # noqa: E402
from gui.history_panel import HistoryPanel  # noqa: E402
from gui.log_view import _LOG_TAG_PALETTE, LogView  # noqa: E402
from gui.weight_panel import WeightPanel  # noqa: E402
from src.history import SubmissionHistory  # noqa: E402

# gui/app.py 里 _log() 实际会写出的 5 个级别 —— 日志配色必须覆盖它们
APP_LEVELS = ("INFO", "OK", "WARN", "FAIL", "HEADER")

MALICIOUS = "=CMD|' /C calc'!A0"


# ---------------------------------------------------------------------------
#  基座
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def tk_root():
    """整模块唯一的 Tk 根窗口。

    withdraw：不显示窗口，但 widget 仍然真实创建（只有几何尺寸保持 1x1，
    见 _fake_size）。finalizer 先取消尚未兑现的 after 任务再 destroy，避免
    回调在解释器销毁后触发 TclError。
    """
    try:
        root = tk.Tk()
    except Exception as exc:  # 无 DISPLAY / 未装 tk → 整模块 skip
        pytest.skip(f"无法创建 Tk 根窗口: {type(exc).__name__}: {exc}")
    root.withdraw()
    yield root
    # `Tk.after_info()` 是 Python 3.11 才进 tkinter 的，3.10 上取它会 AttributeError
    # （CI 的 3.10 那条腿就是这么红的）。3.10 枚举不出待兑现任务也无妨：本模块从不跑
    # mainloop，root.destroy() 之后解释器自己会把它们连带丢掉，不存在回调打空的问题。
    after_info = getattr(root, "after_info", None)
    if after_info is not None:
        for job in after_info():
            root.after_cancel(job)
    root.destroy()


@pytest.fixture()
def frame(tk_root):
    """每个用例一块干净的宿主 Frame，用完即销（根窗口不动）。"""
    host = tk.Frame(tk_root, bg=theme.COLORS["surface"])
    host.pack(fill=tk.BOTH, expand=True)
    tk_root.update()
    yield host
    host.destroy()


class RecordingRoot:
    """记录 after 排队的回调，由测试自己决定何时兑现。

    面板只用到 root.after()（gui/weight_panel.py 的类型胶囊、
    gui/history_panel.py build 的 300ms 延迟刷新）。直接给真 tk.Tk 的话，
    回调会滞留在事件队列里，根窗口销毁后才触发 → TclError。
    """

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, object]] = []

    def after(self, delay, func=None, *args):
        self.scheduled.append((delay, func))
        return f"after-{len(self.scheduled)}"

    def delays(self) -> list[int]:
        return [d for d, _f in self.scheduled]

    def drain(self) -> int:
        pending, self.scheduled = self.scheduled, []
        for _delay, fn in pending:
            if fn is not None:
                fn()
        return len(pending)


class LogRecorder:
    """面板 log_fn 的真实签名是 ``log(msg, tag)`` —— 直接把 list.append 当
    回调传进去，第一次记日志就会 TypeError。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, msg, tag: str = "INFO") -> None:
        self.records.append((str(msg), tag))

    @property
    def levels(self) -> list[str]:
        return [lvl for _m, lvl in self.records]

    @property
    def last(self) -> tuple[str, str]:
        return self.records[-1]


def _fake_size(widget, width: int, height: int):
    """让 winfo_width/height 报出"已映射"的尺寸。

    WHY：根窗口是 withdraw 的，子 widget 永远 1x1，而 widgets/theme/log_view 里
    每个绘制入口都有 ``if w <= 2: return`` 这类未映射守卫 —— 不跨过守卫就只能
    测到早退分支。这里只在实例上覆写两个方法，既不显示窗口也不碰生产代码。
    """
    widget.winfo_width = lambda: width  # type: ignore[method-assign]
    widget.winfo_height = lambda: height  # type: ignore[method-assign]
    return widget


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _labels_with_text(root_widget, text: str) -> list:
    return [
        w for w in _descendants(root_widget)
        if isinstance(w, tk.Label) and str(w.cget("text")) == text
    ]


# ---------------------------------------------------------------------------
#  1. gui/theme.py — 颜色纯函数
# ---------------------------------------------------------------------------
def test_hex_and_rgb_roundtrip_and_case_handling() -> None:
    assert theme.hex_to_rgb("#0A141E") == (10, 20, 30)
    assert theme.hex_to_rgb("0a141e") == (10, 20, 30), "lstrip('#') → 前缀可选"
    assert theme.rgb_to_hex(10, 20, 30) == "#0a141e", "输出固定小写且带 # 前缀"
    for rgb in [(0, 0, 0), (255, 255, 255), (1, 128, 254)]:
        assert theme.hex_to_rgb(theme.rgb_to_hex(*rgb)) == rgb
    # 下划线别名必须指向同一实现，否则 app.py/widgets.py 的调用点会漂
    assert theme._hex_to_rgb is theme.hex_to_rgb
    assert theme._rgb_to_hex is theme.rgb_to_hex
    assert theme._lerp_color is theme.lerp_color


def test_all_theme_colors_and_gradients_are_valid_hex() -> None:
    """配色表里任何一处手误都会让 widget 在运行时抛 TclError，先静态钉住。"""
    for name, value in theme.COLORS.items():
        assert len(value) == 7, name
        assert theme.hex_to_rgb(value) == theme.hex_to_rgb(value.upper())
    for grad in (theme.GRAD_PRIMARY, theme.GRAD_DANGER,
                 theme.GRAD_SUCCESS, theme.GRAD_HEADER):
        assert len(grad) >= 2
        for c in grad:
            theme.hex_to_rgb(c)


def test_lerp_color_endpoints_and_midpoint() -> None:
    assert theme.lerp_color("#000000", "#ffffff", 0.0) == "#000000"
    assert theme.lerp_color("#000000", "#ffffff", 1.0) == "#ffffff"
    assert theme.lerp_color("#000000", "#ffffff", 0.5) == "#7f7f7f"
    assert theme.lerp_color("#0a141e", "#0a141e", 0.37) == "#0a141e"


# ---------------------------------------------------------------------------
#  2. gui/theme.py — Canvas 渐变
#     注：两个 draw_* 里算好的 kwargs["tags"] 从未传给 create_line
#     （gui/theme.py:145 与 :171），tag 实参目前是空转。因此这里只断言
#     "画了多少条、颜色对不对"，不拿 find_withtag(tag) 断言 —— 那会把缺陷
#     固化成契约，将来修好反而要改测试。
# ---------------------------------------------------------------------------
def test_vertical_gradient_paints_interpolated_lines(tk_root) -> None:
    canvas = _fake_size(tk.Canvas(tk_root), 40, 40)
    before = set(canvas.find_all())
    theme.draw_vertical_gradient(canvas, 0, 0, 10, 20, theme.GRAD_PRIMARY)
    new = [i for i in canvas.find_all() if i not in before]
    assert len(new) == 20, "两段渐变 × 每段 10px"
    assert canvas.itemcget(new[0], "fill") == theme.GRAD_PRIMARY[0]
    assert canvas.itemcget(new[-1], "fill") == theme.GRAD_PRIMARY[-1]


def test_horizontal_gradient_paints_one_line_per_column(tk_root) -> None:
    canvas = _fake_size(tk.Canvas(tk_root), 60, 60)
    before = set(canvas.find_all())
    theme.draw_horizontal_gradient(
        canvas, 0, 0, 30, 10, theme.GRAD_SUCCESS, tag="card_border"
    )
    new = [i for i in canvas.find_all() if i not in before]
    assert len(new) == 30
    assert canvas.itemcget(new[0], "fill") == theme.GRAD_SUCCESS[0]
    assert canvas.itemcget(new[-1], "fill") == theme.GRAD_SUCCESS[-1]


def test_gradient_handles_degenerate_box_without_raising(tk_root) -> None:
    """零宽度 / 零高度是进度条收起时的常态，不能变成 TclError。"""
    canvas = _fake_size(tk.Canvas(tk_root), 20, 20)
    theme.draw_vertical_gradient(canvas, 0, 0, 5, 0, theme.GRAD_DANGER)
    theme.draw_horizontal_gradient(canvas, 0, 0, 0, 5, theme.GRAD_HEADER)
    assert len(canvas.find_all()) >= 2


def test_apply_ttk_style_returns_full_font_table_and_configures_style(tk_root) -> None:
    """整条 60 行的 ttk 配置路径此前从未执行过（gui/theme.py:193-301）。"""
    style, fonts = theme.apply_ttk_style(tk_root)
    assert isinstance(style, ttk.Style)
    assert set(fonts) == set(theme.FONT_PRESETS)
    assert fonts["NORMAL"] == theme.FONT_PRESETS["NORMAL"]
    assert style.theme_use() in style.theme_names()
    assert style.lookup("TFrame", "background") == theme.COLORS["bg"]


def test_apply_ttk_style_accepts_custom_palette_and_presets(tk_root) -> None:
    custom_colors = dict(theme.COLORS, bg="#101010")
    custom_fonts = {"NORMAL": ("Arial", 8), "LARGE": ("Arial", 10, "bold")}
    style, fonts = theme.apply_ttk_style(
        tk_root, colors=custom_colors, font_presets=custom_fonts
    )
    assert fonts == custom_fonts
    assert style.lookup("TFrame", "background") == "#101010"


# ---------------------------------------------------------------------------
#  3. gui/widgets.py — 组件工厂
# ---------------------------------------------------------------------------
def test_resolve_fonts_defaults_to_presets_and_honours_override() -> None:
    default = widgets._resolve_fonts(None)
    assert default == dict(theme.FONT_PRESETS)
    default["NORMAL"] = ("hacked", 1)
    assert theme.FONT_PRESETS["NORMAL"] != ("hacked", 1), "必须返回副本而非原表"

    override = widgets._resolve_fonts({"NORMAL": ("Consolas", 11)})
    assert override == {"NORMAL": ("Consolas", 11)}


def test_make_card_returns_body_carrying_title(frame) -> None:
    body = widgets.make_card(frame, "权重配置", icon="📋", accent=theme.GRAD_SUCCESS)
    assert isinstance(body, tk.Frame)
    outer = body.master.master.master
    assert getattr(outer, "_card_canvas", None) is not None
    assert getattr(outer, "_card_body", None) is not None
    assert _labels_with_text(frame, "权重配置"), "标题文本必须落在卡片上"
    assert _labels_with_text(frame, "📋 "), "图标与标题同处标题栏"


def test_paint_card_border_skips_unmapped_and_is_idempotent_when_sized(frame) -> None:
    """重绘必须真的清干净。

    v2.7 之前 ``draw_*_gradient`` 算出了 ``kwargs["tags"]`` 却没传给
    ``create_line``，于是这里的 ``canvas.delete("card_border")`` 只能删掉两条
    描边矩形，四角渐变的每一段线段全都留在画布上 —— 用户每拖动一次窗口就
    累积一批删不掉的 item。现在 tag 真正落到线上，重绘应当严格幂等。
    """
    canvas = tk.Canvas(frame)
    widgets.paint_card_border(canvas, theme.GRAD_PRIMARY)
    assert canvas.find_withtag("card_border") == (), "未映射时只清不画"

    _fake_size(canvas, 260, 140)
    widgets.paint_card_border(canvas, theme.GRAD_PRIMARY)
    tagged = canvas.find_withtag("card_border")
    rects = [i for i in tagged if canvas.type(i) == "rectangle"]
    lines = [i for i in tagged if canvas.type(i) == "line"]
    assert len(rects) == 2, "两条描边矩形"
    assert lines, "渐变线段也必须带上 tag，否则 delete 删不掉"

    n_tagged, n_total = len(tagged), len(canvas.find_all())
    widgets.paint_card_border(canvas, theme.GRAD_PRIMARY)
    widgets.paint_card_border(canvas, theme.GRAD_PRIMARY)
    assert len(canvas.find_withtag("card_border")) == n_tagged, "重绘后带 tag 的 item 数变了"
    assert len(canvas.find_all()) == n_total, (
        "画布上出现了清不掉的孤儿 item —— 每次 <Configure> 就是一次无界泄漏"
    )


def test_make_icon_button_command_fires_on_click(frame) -> None:
    hits: list[str] = []
    btn = widgets.make_icon_button(frame, "开始", accent="primary",
                                   command=lambda: hits.append("go"))
    btn.pack()
    # invoke() 正是 <ButtonRelease-1> 类绑定实际调用的入口
    assert "<ButtonRelease-1>" in btn.bind_class("Button")
    btn.invoke()
    assert hits == ["go"]
    assert str(btn.cget("text")) == "开始"


def test_make_icon_button_accent_colors_are_distinct(frame) -> None:
    seen = {}
    for accent in ("primary", "success", "danger", "ghost"):
        btn = widgets.make_icon_button(frame, accent, accent=accent)
        seen[accent] = str(btn.cget("bg"))
        btn.destroy()
    assert seen["ghost"] == "#ffffff"
    assert len(set(seen.values())) == 4, f"四种 accent 配色必须互不相同: {seen}"
    # 未知 accent 走 ghost 兜底而不是 KeyError
    unknown = widgets.make_icon_button(frame, "x", accent="bogus")
    assert str(unknown.cget("bg")) == "#ffffff"
    unknown.destroy()


def test_make_toggle_stays_in_sync_with_boolean_var(frame) -> None:
    var = tk.BooleanVar(value=False)
    chk = widgets.make_toggle(frame, "UC 模式", var)
    chk.pack()
    chk.select()
    assert var.get() is True
    chk.deselect()
    assert var.get() is False
    chk.invoke()
    assert var.get() is True, "点击必须翻转变量"
    chk.invoke()
    assert var.get() is False
    var.set(True)
    chk.deselect()
    assert var.get() is False, "变量与勾选框不能被反推歪"
    assert str(chk.cget("state")) == "normal"

    disabled = widgets.make_toggle(frame, "禁用", var, enabled=False)
    assert str(disabled.cget("state")) == "disabled"
    disabled.destroy()


def test_make_spin_button_colors_and_wired_command_moves_var(frame) -> None:
    """make_spin_button 自身不带 command —— 增减语义由调用方接线（见 app）。"""
    plus = widgets.make_spin_button(frame, "+", "plus")
    minus = widgets.make_spin_button(frame, "−", "minus")
    assert str(plus.cget("fg")) == theme.COLORS["primary"]
    assert str(minus.cget("fg")) == theme.COLORS["warning_dim"]

    count = tk.IntVar(value=1)
    plus.configure(command=lambda: count.set(count.get() + 1))
    minus.configure(command=lambda: count.set(max(1, count.get() - 1)))
    plus.invoke()
    assert count.get() == 2
    minus.invoke()
    minus.invoke()
    assert count.get() == 1, "下限由接线方夹住"


def test_count_row_spin_buttons_clamp_between_1_and_9999(frame) -> None:
    """真身是 SurveyGUI._build_count_row —— 用 stub host 承载，不构造 SurveyGUI。

    这是"份数框 +/−"唯一的行为规格：越界必须夹住，否则 0 份或百万份会直接
    灌进 _on_start 的校验分支。
    """
    from gui.app import SurveyGUI

    class _Host:
        FONT_NORMAL = theme.FONT_PRESETS["NORMAL"]
        FONT_SMALL = theme.FONT_PRESETS["SMALL"]

        def __init__(self) -> None:
            self._make_spin_button = types.MethodType(
                SurveyGUI._make_spin_button, self
            )
            self._build_count_row = types.MethodType(
                SurveyGUI._build_count_row, self
            )

    host = _Host()
    host._build_count_row(frame)
    buttons = {
        str(b.cget("text")): b
        for b in _descendants(frame) if isinstance(b, tk.Button)
    }
    assert set(buttons) == {"−", "+"}

    buttons["+"].invoke()
    assert host.count_var.get() == 2
    buttons["−"].invoke()
    buttons["−"].invoke()
    assert host.count_var.get() == 1, "减到 1 不再往下"
    host.count_var.set(9999)
    buttons["+"].invoke()
    assert host.count_var.get() == 9999, "封顶 9999"
    entries = [w for w in _descendants(frame) if isinstance(w, tk.Entry)]
    assert len(entries) == 1 and entries[0].get() == "9999", "份数框与 IntVar 同值"


def test_make_stat_badge_shows_label_and_redraws_glow_on_request(frame) -> None:
    rec = RecordingRoot()
    var = tk.StringVar(value="0")
    wrap = widgets.make_stat_badge(
        frame, "成功", var, theme.GRAD_SUCCESS, "✓", root=rec
    )
    assert _labels_with_text(wrap, "✓")
    assert _labels_with_text(wrap, " 成功")
    canvases = [w for w in _descendants(wrap) if isinstance(w, tk.Canvas)]
    assert canvases, "badge 外层必须是渐变 Canvas"
    # make_stat_badge 只排一次 after(10, _cfg)，所以要先跨过未映射守卫再兑现
    _fake_size(canvases[0], 108, 60)
    rec.drain()
    assert canvases[0].find_withtag("glow"), "兑现 after 后才画发光边框"
    assert rec.scheduled == [], "延迟作业必须被消费掉，不能留在队列里"


# ---------------------------------------------------------------------------
#  4. gui/weight_panel.py — 结构与权重字符串
# ---------------------------------------------------------------------------
_QS = [
    {"q": 1, "type": "single", "choices": [1, 2, 3]},
    {"q": 2, "type": "multi", "choices": [1, 2]},
    {"q": 3, "type": "scale", "scale": 5, "scale_min": 1},
    {"q": 4, "type": "text", "field": "name"},
    {"q": 5, "type": "matrix", "rows": [1, 2], "cols": [1, 2, 3]},
]


def _copy_questions() -> list[dict]:
    return [{k: (list(v) if isinstance(v, list) else v) for k, v in q.items()}
            for q in _QS]


def _weight_panel(frame, *, questions=None, wc=None, logs=None, root=None):
    """构造并 build 一个 WeightPanel。

    weight_config_global 用**私有 dict**，绝不把 src.config.WEIGHT_CONFIG 递进
    去 —— 面板虽然只读它，但共享全局单例会让用例之间互相污染。
    """
    panel = WeightPanel(
        root or RecordingRoot(),
        logs if logs is not None else (lambda *_a: None),
        weight_config_global=wc if wc is not None else {},
    )
    panel.build(frame)
    if questions:
        panel.populate(questions)
    return panel


def test_weight_panel_build_renders_header_and_placeholder(frame) -> None:
    panel = _weight_panel(frame)
    assert panel.table_frame is not None
    for col in ("题号", "类型", "选项/空数", "权重/答案文本（英文逗号分隔）"):
        assert _labels_with_text(panel.table_frame, col), col
    assert _labels_with_text(frame, "权重配置"), "卡片标题"
    assert _labels_with_text(panel.table_frame, "点击「探测题目」自动识别问卷结构")


def test_populate_renders_one_editable_row_per_question(frame) -> None:
    shared_questions: list[dict] = []
    shared_entries: dict[int, tk.StringVar] = {}
    panel = WeightPanel(
        RecordingRoot(), lambda *_a: None, weight_config_global={},
        shared_questions=shared_questions, shared_weight_entries=shared_entries,
    )
    panel.build(frame)
    panel.populate(_copy_questions())

    rows = [w for w in _descendants(panel.table_frame) if isinstance(w, tk.Entry)]
    assert len(rows) == len(_QS), "每题一个输入框 = 一行"
    # 与 SurveyGUI 的双向同步靠的是"同一容器对象 + 原地改写"
    assert panel.questions is shared_questions
    assert panel.weight_entries is shared_entries
    assert [q["q"] for q in shared_questions] == [1, 2, 3, 4, 5]
    assert set(shared_entries) == {1, 2, 3, 4, 5}
    assert _labels_with_text(panel.table_frame, "Q3")
    assert _labels_with_text(panel.table_frame, "1~5"), "量表行显示 起点~级数"
    assert _labels_with_text(panel.table_frame, "3"), "Q1 的选项数"

    panel.populate([{"q": 9, "type": "single", "choices": [1, 2]}])
    assert len(panel.weight_entries) == 1
    assert len([w for w in _descendants(panel.table_frame)
                if isinstance(w, tk.Entry)]) == 1, "重填不得残留旧行"


def test_populate_seeds_equal_weight_defaults(frame) -> None:
    """留空 = 等权重，所以默认串就是均分值。"""
    panel = _weight_panel(frame, questions=_copy_questions())
    assert panel.weight_entries[1].get() == "0.3333,0.3333,0.3333"
    assert panel.weight_entries[2].get() == "0.5000,0.5000"
    assert panel.weight_entries[3].get() == "1,1,1,1,1", "量表默认每级 1"
    assert panel.weight_entries[4].get() == "", "填空题没有默认候选"
    assert panel.weight_entries[5].get() == "", "矩阵默认留空"


def test_populate_prefills_from_weight_config_global(frame) -> None:
    """已有 WEIGHT_CONFIG 记录要覆盖默认串，否则"载入配置"在表格里看不见。"""
    seeded = {
        1: {"type": "single", "weights": [0.3, 0.5, 0.2]},
        4: {"type": "text", "options": ["张三", "李四"]},
        5: {"type": "matrix", "row_weights": {"1": [1, 2, 3]}},
    }
    holder = tk.Frame(frame)
    holder.pack()
    panel = _weight_panel(holder, questions=_copy_questions(), wc=seeded)
    assert panel.weight_entries[1].get() == "0.3000,0.5000,0.2000"
    assert panel.weight_entries[4].get() == "张三,李四"
    assert panel.weight_entries[5].get() == "1:1,2,3"


def test_populate_badge_after_callback_draws_without_error(frame) -> None:
    """胶囊标签由 root.after(10, _draw_badge) 触发 —— 兑现它才算走到绘制分支。"""
    rec = RecordingRoot()
    panel = _weight_panel(frame, questions=_copy_questions(), root=rec)
    assert rec.delays() == [10] * len(_QS)
    rec.drain()
    canvases = [w for w in _descendants(panel.table_frame)
                if isinstance(w, tk.Canvas)]
    assert len(canvases) == len(_QS)
    assert all(len(c.find_all()) >= 4 for c in canvases)


# ---------------------------------------------------------------------------
#  5. gui/weight_panel.py — build_weight_config（字符串 → dict）
# ---------------------------------------------------------------------------
def test_build_weight_config_parses_comma_separated_weights(frame) -> None:
    logs = LogRecorder()
    panel = _weight_panel(frame, questions=_copy_questions(), logs=logs)
    panel.weight_entries[1].set("0.3,0.5,0.2")
    panel.weight_entries[2].set(" 0.4 , 0.6 ")
    cfg = panel.build_weight_config()
    assert cfg[1] == {"type": "single", "weights": [0.3, 0.5, 0.2]}
    assert cfg[2] == {"type": "multi", "weights": [0.4, 0.6]}, "空格要被剥掉"
    assert logs.records == []


def test_empty_weight_field_is_omitted_so_equal_weights_apply(frame) -> None:
    """留空 → 该题**不进** dict，由 src/answering.py「无配置 → 等权重」接管。"""
    logs = LogRecorder()
    panel = _weight_panel(frame, questions=_copy_questions(), logs=logs)
    panel.weight_entries[1].set("")
    cfg = panel.build_weight_config()
    assert 1 not in cfg
    assert logs.records == [], "留空是合法输入，不该记 WARN"


@pytest.mark.parametrize(
    "raw, expected_fragment",
    [
        ("abc,def,ghi", "权重格式错误"),
        ("0.5,0.5", "权重数(2) != 选项数(3)"),
        ("0.5,0.5,0.5,0.5", "权重数(4) != 选项数(3)"),
        ("0.5,,0.5", "权重数(2) != 选项数(3)"),
    ],
)
def test_malformed_weight_input_routes_warning_not_crash(
    frame, raw, expected_fragment
) -> None:
    logs = LogRecorder()
    panel = _weight_panel(frame, questions=_copy_questions(), logs=logs)
    panel.weight_entries[1].set(raw)
    cfg = panel.build_weight_config()
    assert 1 not in cfg, f"非法输入必须整题跳过，实际 {cfg}"
    assert logs.levels == ["WARN"]
    assert expected_fragment in logs.records[0][0]


@pytest.mark.parametrize("raw, fragment", [
    ("-1,2,2", "负数"),
    ("nan,2,2", "NaN"),
    ("inf,2,2", "NaN"),
])
def test_illegal_weights_are_rejected_at_the_panel(frame, raw, fragment) -> None:
    """负数与非有限值在输入框就该被拦下。

    v2.7 之前面板只校验"能不能转 float + 数量对不对"，于是 ``-1,2,2`` 静默通过，
    到运行期才被 ``utils.weights_are_usable`` 按"正权重之和 > 0"当合法值用
    （等于把 -1 当成极低权重，与用户直觉相反），或在极端情况整体降级等权重 ——
    无论哪种，用户在界面上都看不到任何提示。
    """
    logs = LogRecorder()
    panel = _weight_panel(frame, questions=_copy_questions(), logs=logs)
    panel.weight_entries[1].set(raw)
    cfg = panel.build_weight_config()
    assert 1 not in cfg, f"非法权重必须整题跳过，实际 {cfg}"
    assert logs.levels == ["WARN"]
    assert fragment in logs.records[0][0]


def test_zero_weight_is_accepted_as_low_probability(frame) -> None:
    """0 是合法权重（"基本不选"），不能跟负数一起被误拒。"""
    logs = LogRecorder()
    panel = _weight_panel(frame, questions=_copy_questions(), logs=logs)
    panel.weight_entries[1].set("0,2,2")
    cfg = panel.build_weight_config()
    assert cfg[1]["weights"] == [0.0, 2.0, 2.0]
    assert logs.records == []


def test_illegal_scale_weights_drop_only_the_weights_not_the_structure(frame) -> None:
    """量表拿到负权重时只弃用权重，scale / scale_min 这些结构信息必须留下。

    整条 continue 掉会让这道题退化成"连级数都不知道"，比只丢权重糟得多。
    """
    logs = LogRecorder()
    panel = _weight_panel(frame, questions=_copy_questions(), logs=logs)
    panel.weight_entries[3].set("-1,1,1,1,1")
    cfg = panel.build_weight_config()
    assert cfg[3] == {"type": "scale", "scale": 5, "scale_min": 1}
    assert "weights" not in cfg[3]
    assert logs.levels == ["WARN"] and "量表权重含负数" in logs.last[0]


def test_illegal_matrix_row_weights_are_discarded_wholesale(frame) -> None:
    logs = LogRecorder()
    panel = _weight_panel(frame, questions=_copy_questions(), logs=logs)
    panel.weight_entries[5].set("1:-0.5,0.5,0.5 | 2:1,1,1")
    cfg = panel.build_weight_config()
    assert "row_weights" not in cfg, "含负数的行权重必须整组弃用"
    assert logs.levels == ["WARN"] and "非法值" in logs.last[0]


def test_scale_weights_digit_shortcut_and_length_warnings(frame) -> None:
    logs = LogRecorder()
    panel = _weight_panel(frame, questions=_copy_questions(), logs=logs)
    panel.weight_entries[3].set("4")
    cfg = panel.build_weight_config()
    assert cfg[3] == {"type": "scale", "scale": 5, "scale_min": 1,
                      "weights": [0.0, 0.0, 0.0, 1.0, 0.0]}
    assert logs.records == [], "纯数字是合法的量表快捷写法"

    panel.weight_entries[3].set("1,2")
    assert panel.build_weight_config()[3]["weights"] == [1.0, 2.0, 0.0, 0.0, 0.0]
    assert "级数(5)" in logs.last[0] and logs.levels[-1] == "WARN"

    panel.weight_entries[3].set("1,2,3,4,5,6,7")
    assert panel.build_weight_config()[3]["weights"] == [1.0, 2.0, 3.0, 4.0, 5.0]

    panel.weight_entries[3].set("x,y,z,4,5")
    assert 3 not in panel.build_weight_config()
    assert "量表权重格式错误" in logs.last[0]

    panel.weight_entries[3].set("")
    assert "weights" not in panel.build_weight_config()[3], "留空 → 不落权重"


def test_text_and_matrix_branches(frame) -> None:
    logs = LogRecorder()
    panel = _weight_panel(frame, questions=_copy_questions(), logs=logs)
    panel.weight_entries[4].set("张三, 李四 ,,王五")
    cfg = panel.build_weight_config()
    assert cfg[4] == {"type": "text", "field": "name",
                      "options": ["张三", "李四", "王五"]}

    panel.weight_entries[5].set("1:0.5,0.5,0.5 | 2:1,1,1")
    cfg = panel.build_weight_config()
    assert cfg[5]["rows"] == [1, 2] and cfg[5]["cols"] == [1, 2, 3]
    assert cfg[5]["row_weights"] == {"1": [0.5, 0.5, 0.5], "2": [1.0, 1.0, 1.0]}

    panel.weight_entries[5].set("1:0.5,0.5,0.5 oops")
    cfg = panel.build_weight_config()
    assert "row_weights" not in cfg[5], "行权重解析失败要弃用编辑值而非崩"
    assert "矩阵行权重格式错误" in logs.last[0] and logs.levels[-1] == "WARN"


def test_unknown_question_type_falls_back_to_plain_weights(frame) -> None:
    logs = LogRecorder()
    panel = _weight_panel(frame, questions=[{"q": 7, "type": "mystery"}],
                          logs=logs)
    panel.weight_entries[7].set("1,2")
    assert panel.build_weight_config() == {
        7: {"type": "mystery", "weights": [1.0, 2.0]}
    }
    panel.weight_entries[7].set("nope")
    assert panel.build_weight_config() == {}
    assert logs.levels[-1] == "WARN"


# ---------------------------------------------------------------------------
#  6. gui/weight_panel.py — restore_from_config（dict → 表格）
# ---------------------------------------------------------------------------
_RESTORED = {
    1: {"type": "single", "weights": [0.2, 0.3, 0.5]},
    2: {"type": "scale", "weights": [1, 2, 3, 4]},
    3: {"type": "text", "field": "phone", "options": ["13800000000"]},
    4: {"type": "matrix", "row_weights": {"1": [1, 2], "2": [3, 4]}},
    5: {"type": "weird"},
}


def test_restore_from_config_reconstructs_rows_without_detection(frame) -> None:
    """未探测过时按 cfg 反构造题目结构（生产路径：先 apply 再 restore）。"""
    panel = _weight_panel(frame, wc=dict(_RESTORED))
    panel.restore_from_config(_RESTORED)
    assert list(panel.weight_entries) == [1, 2, 3, 4, 5]
    assert panel.weight_entries[1].get() == "0.2000,0.3000,0.5000"
    assert panel.weight_entries[2].get() == "1.0000,2.0000,3.0000,4.0000", (
        "scale 没有 scale 键时按权重长度反推级数，再把权重原样回填"
    )
    assert panel.weight_entries[3].get() == "13800000000"
    assert panel.weight_entries[4].get() == "1:1,2 | 2:3,4"
    by_q = {q["q"]: q for q in panel.questions}
    assert len(by_q[1]["choices"]) == 3
    assert by_q[3]["field"] == "phone"
    assert by_q[4]["rows"] == [1, 2] and by_q[4]["cols"] == [1, 2]
    assert by_q[5]["choices"] == [1, 2], "未知类型给两个占位选项"


def test_restore_from_config_is_noop_or_refresh_only(frame) -> None:
    panel = _weight_panel(frame)
    panel.restore_from_config({})
    assert panel.questions == [] and panel.weight_entries == {}, "空配置直接返回"

    panel.restore_from_config({7: "not-a-dict", 8: {"type": "single",
                                                   "weights": [1.0, 2.0]}})
    assert list(panel.weight_entries) == [8], "非 dict 的题号被忽略"

    questions_before = list(panel.questions)
    panel.restore_from_config({99: {"type": "single", "weights": [0.5, 0.5]}})
    assert panel.questions == questions_before, "已探测过时以探测结构为准，只重刷表格"


# ---------------------------------------------------------------------------
#  7. gui/history_panel.py — _csv_safe（v2.6 CSV 注入防线）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dangerous", [
    MALICIOUS, "+1-1", "-2+3", "@SUM(A1)", "\t=tabbed", "\r=carriage",
])
def test_csv_safe_prefixes_formula_leading_chars(dangerous: str) -> None:
    out = hp._csv_safe(dangerous)
    assert out == "'" + dangerous
    assert out[0] == "'", "首字符必须被单引号顶掉，Excel 才不会当公式求值"


@pytest.mark.parametrize("harmless", [
    "普通文本", "https://www.wjx.cn/vm/abc.aspx", "13800000000",
    "张", "[0, 1]", "", None, 42, 0.5,
])
def test_csv_safe_leaves_ordinary_values_alone(harmless) -> None:
    expected = "" if harmless is None else str(harmless)
    assert hp._csv_safe(harmless) == expected


# ---------------------------------------------------------------------------
#  8. gui/history_panel.py — get_db 缓存 / close_db（v2.6 修复）
# ---------------------------------------------------------------------------
def _history_panel(tmp_path, *, has_history=True, cls=SubmissionHistory,
                   logs=None):
    panel = HistoryPanel(
        RecordingRoot(),
        logs if logs is not None else (lambda *_a: None),
        has_history=has_history,
        submission_history_cls=cls,
        history_db_path=str(tmp_path / "nested" / "history.db"),
    )
    return panel, panel.root


def test_get_db_creates_missing_directory_and_returns_same_instance(tmp_path) -> None:
    panel, _rec = _history_panel(tmp_path)
    db_path = panel._db_path
    assert not os.path.exists(os.path.dirname(db_path)), "前置：目录还不存在"
    first = panel.get_db()
    assert first is not None and os.path.exists(db_path), "懒打开时顺手建目录"
    assert panel.get_db() is first, "v2.6：必须复用同一连接，不得每次重跑迁移"
    assert panel.get_db() is first
    panel.close_db()


def test_close_db_clears_the_cache_and_is_safe_to_repeat(tmp_path) -> None:
    panel, _rec = _history_panel(tmp_path)
    first = panel.get_db()
    assert panel._db_cached is first
    panel.close_db()
    assert panel._db_cached is None, "close_db 必须清缓存，否则拿到已关闭的连接"
    panel.close_db()  # 幂等：没有缓存也不炸

    second = panel.get_db()
    assert second is not None and second is not first
    panel.close_db()


def test_get_db_logs_warning_when_opening_fails(tmp_path) -> None:
    class _Boom:
        def __init__(self, *_a, **_kw) -> None:
            raise RuntimeError("disk on fire")

    logs = LogRecorder()
    panel, _rec = _history_panel(tmp_path, cls=_Boom, logs=logs)
    assert panel.get_db() is None
    assert logs.levels == ["WARN"], "打不开库不能抛，只能留一条 WARN"
    assert "disk on fire" in logs.last[0]


def test_get_db_returns_none_when_history_module_missing(tmp_path) -> None:
    panel, _rec = _history_panel(tmp_path, has_history=False, cls=None)
    assert panel.get_db() is None
    assert not os.path.exists(os.path.dirname(panel._db_path)), "未启用时不该建目录"


def test_close_db_swallows_close_error_with_warning(tmp_path) -> None:
    class _BadClose:
        def close(self) -> None:
            raise OSError("locked")

    logs = LogRecorder()
    panel, _rec = _history_panel(tmp_path, logs=logs)
    panel._db_cached = _BadClose()
    panel.close_db()
    assert panel._db_cached is None, "关闭失败也要清缓存"
    assert logs.levels == ["WARN"] and "locked" in logs.last[0]


# ---------------------------------------------------------------------------
#  9. gui/history_panel.py — build / refresh / select_run
# ---------------------------------------------------------------------------
def _seed(tmp_path):
    """建面板 + 懒开库 + 写两条批次，返回 (panel, recorder, db, run_ids)。"""
    panel, rec = _history_panel(tmp_path)
    db = panel.get_db()
    assert db is not None
    r1 = db.start_run("https://www.wjx.cn/vm/ok.aspx", 3, "edge", False)
    db.record_answer(r1, 1, 1, "single", [0, 1], None, elapsed_ms=120)
    db.record_answer(r1, 1, 2, "text", None, MALICIOUS, elapsed_ms=300)
    db.record_answer(r1, 1, 3, "text", None, "李四", elapsed_ms=80)
    db.finish_run(r1, success_count=2, fail_count=1,
                  total_elapsed_seconds=12.5, status="finished")
    r2 = db.start_run('=HYPERLINK("evil")', 1, "chrome", True)
    db.finish_run(r2, success_count=0, fail_count=1,
                  total_elapsed_seconds=1.0, status="failed",
                  error_message="SUBMIT_FAILED")
    return panel, rec, db, (r1, r2)


class _BadDB:
    """查询即抛 —— 历史面板必须把异常收敛成一条日志。"""

    def query_runs(self, *_a, **_kw):
        raise RuntimeError("table runs missing")

    def query_answers(self, *_a, **_kw):
        raise RuntimeError("table answers missing")


def test_build_defers_one_refresh_and_renders_seeded_runs(tmp_path, frame) -> None:
    panel, rec, _db, (r1, r2) = _seed(tmp_path)
    panel.build(frame)
    assert rec.delays() == [300], "build 只允许排一次 300ms 延迟刷新"
    assert panel.runs_tree.get_children() == (), "回调没兑现 → 表还是空的"
    rec.drain()

    rows = panel.runs_tree.get_children()
    assert set(rows) == {str(r1), str(r2)}, "两条批次都要落进树里"
    values = panel.runs_tree.item(str(r2), "values")
    assert str(values[0]) == str(r2) and str(values[3]) == "failed"
    assert "status_fail" in panel.runs_tree.item(str(r2), "tags")
    assert "status_ok" in panel.runs_tree.item(str(r1), "tags")
    summary = panel.summary_var.get()
    assert "共 2 次运行" in summary and "累计提交 4" in summary
    assert "✓ 2" in summary and "✕ 2" in summary
    panel.close_db()


def test_refresh_is_harmless_before_build(tmp_path) -> None:
    """两道早退守卫：树还没建 / 库拿不到，都不能抛异常。"""
    panel, _rec = _history_panel(tmp_path)
    panel.refresh()
    assert panel._db_cached is not None, "刷新仍会懒开库，但渲染要等 build"
    panel.close_db()

    panel2, _rec2 = _history_panel(tmp_path / "b", has_history=False, cls=None)
    panel2.refresh()
    assert panel2.get_db() is None


def test_select_run_loads_answer_details(tmp_path, frame) -> None:
    panel, rec, _db, (r1, _r2) = _seed(tmp_path)
    panel.build(frame)
    rec.drain()
    panel.runs_tree.selection_set(str(r1))
    panel.select_run()
    rows = panel.ans_tree.get_children()
    assert len(rows) == 3, "三行答案明细"
    values = [panel.ans_tree.item(iid, "values") for iid in rows]
    assert {str(v[3]) for v in values if str(v[1]) == "text"} == {MALICIOUS, "李四"}, (
        "树里存原文，注入前缀只在导出时加"
    )
    assert str(values[0][2]) == "[0, 1]", "options_selected 以 JSON 串呈现"
    assert panel.answer_head_var.get() == f"Run #{r1} · 答题数 3"

    panel.runs_tree.selection_remove(panel.runs_tree.selection())
    panel.select_run()
    assert len(panel.ans_tree.get_children()) == 3, "无选中时保持原状"
    panel.close_db()


def test_refresh_and_select_run_degrade_to_logs(tmp_path, frame) -> None:
    logs = LogRecorder()
    panel, _rec = _history_panel(tmp_path, logs=logs)
    panel._db_cached = _BadDB()
    panel.build(frame)
    panel.refresh()
    assert "刷新历史记录失败" in logs.last[0] and logs.levels[-1] == "WARN"

    panel.runs_tree.insert("", tk.END, iid="4242", values=("4242",))
    panel.runs_tree.selection_set("4242")
    panel.select_run()
    assert "读取答题明细失败" in logs.last[0] and logs.levels[-1] == "WARN"
    assert panel.ans_tree.get_children() == (), "失败时不该留下半截明细"
    panel.close_db()


# ---------------------------------------------------------------------------
#  10. gui/history_panel.py — export_csv / purge_old
# ---------------------------------------------------------------------------
def _read_csv(path) -> list[list[str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def test_export_csv_applies_injection_prefix_to_seeded_answer(
    tmp_path, frame, monkeypatch
) -> None:
    panel, rec, _db, (r1, r2) = _seed(tmp_path)
    panel.build(frame)
    rec.drain()
    out_dir = tmp_path / "exported"
    out_dir.mkdir()
    out = out_dir / "history_runs.csv"
    monkeypatch.setattr(hp.filedialog, "asksaveasfilename",
                        lambda **_kw: str(out))
    logs = LogRecorder()
    panel.log = logs

    panel.export_csv()

    assert out.exists(), "runs CSV 必须真的落盘（写进 tmp_path，不碰 data/）"
    runs_rows = _read_csv(out)
    assert runs_rows[0][:4] == ["id", "started_at", "finished_at", "status"]
    by_id = {r[0]: r for r in runs_rows[1:]}
    assert set(by_id) == {str(r1), str(r2)}, "一行都不能少"
    assert by_id[str(r2)][4].startswith("'="), "survey_url 也在注入面之内"
    assert by_id[str(r2)][8] == "SUBMIT_FAILED", "普通报错文本不该被改写"

    ans_rows = _read_csv(out_dir / "history_runs_answers.csv")
    assert ans_rows[0][-1] == "created_at"
    cells = {r[2]: r[5] for r in ans_rows[1:]}
    assert cells["2"] == "'" + MALICIOUS, "注入前缀必须真的落到文件里"
    assert cells["3"] == "李四"
    assert cells["1"] == "", "选择题 text_answer 为 NULL → 空串"
    assert logs.levels == ["OK", "OK"]
    panel.close_db()


def test_export_csv_without_db_logs_warning(tmp_path, frame) -> None:
    panel, _rec = _history_panel(tmp_path, has_history=False, cls=None)
    panel.build(frame)
    logs = LogRecorder()
    panel.log = logs
    panel.export_csv()
    assert logs.records == [("未加载 src.history，无法导出", "WARN")]


def test_export_csv_cancelled_dialog_writes_nothing(tmp_path, frame,
                                                   monkeypatch) -> None:
    panel, rec, _db, _ids = _seed(tmp_path)
    panel.build(frame)
    rec.drain()
    monkeypatch.setattr(hp.filedialog, "asksaveasfilename", lambda **_kw: "")
    logs = LogRecorder()
    panel.log = logs
    panel.export_csv()
    assert logs.records == [], "取消保存对话框后不该有任何后续动作"
    panel.close_db()


def test_purge_old_reports_count_and_refreshes(tmp_path, frame) -> None:
    panel, rec, db, (r1, r2) = _seed(tmp_path)
    panel.build(frame)
    rec.drain()
    logs = LogRecorder()
    panel.log = logs

    panel.purge_old()
    assert "已清理 0 条" in logs.last[0] and logs.levels[-1] == "OK"
    assert len(panel.runs_tree.get_children()) == 2, "刚写的批次不该被清掉"

    db._conn.execute(
        "UPDATE runs SET started_at = datetime('now','-30 days') WHERE id = ?", (r1,)
    )
    db._conn.commit()
    panel.purge_old()
    assert "已清理 1 条" in logs.last[0]
    assert list(panel.runs_tree.get_children()) == [str(r2)], (
        "清理后必须刷新表格，否则用户看到的是已删除的行"
    )
    panel.close_db()


def test_purge_old_failure_and_missing_db_log_levels(tmp_path) -> None:
    class _BadPurge:
        def purge_old(self, *_a, **_kw):
            raise RuntimeError("database is locked")

    logs = LogRecorder()
    panel, _rec = _history_panel(tmp_path, logs=logs)
    panel._db_cached = _BadPurge()
    panel.purge_old()
    assert logs.levels == ["FAIL"] and "database is locked" in logs.last[0]

    panel2, _rec2 = _history_panel(tmp_path / "c", has_history=False, cls=None)
    logs2 = LogRecorder()
    panel2.log = logs2
    panel2.purge_old()
    assert logs2.records == [("未加载 src.history，无法清理", "WARN")]


# ---------------------------------------------------------------------------
#  11. gui/log_view.py — 队列 → 终端
# ---------------------------------------------------------------------------
@pytest.fixture()
def log_view(tk_root, frame):
    q: queue.Queue = queue.Queue()
    view = LogView(tk_root, log_queue=q, make_card_fn=widgets.make_card)
    view.build(frame)
    return view, q


def test_log_card_is_titled_and_tags_every_emitted_level(frame, log_view) -> None:
    view, _q = log_view
    assert _labels_with_text(frame, "运行日志"), "卡片标题"
    for level in APP_LEVELS:
        assert level in view.log_text.tag_names(), f"{level} 没有配色标签"
        assert view.log_text.tag_cget(level, "foreground") == \
            _LOG_TAG_PALETTE[level][0]


def test_queue_drain_appends_lines_and_bumps_counter(log_view) -> None:
    view, q = log_view
    for i, level in enumerate(APP_LEVELS):
        q.put((f"第 {i} 条 {level} 日志", level))
    assert view.log_text.get("1.0", tk.END).strip() == "", "没 drain 前不该有内容"
    view.drain_queue()
    text = view.log_text.get("1.0", tk.END)
    for i, level in enumerate(APP_LEVELS):
        assert f"第 {i} 条 {level} 日志" in text
    assert view._lineno_count == len(APP_LEVELS)
    assert view.log_lines_var.get() == f"{len(APP_LEVELS)} lines"
    assert view.log_text.tag_nextrange("HEADER", "1.0", tk.END) != ()
    assert "❯" in text and q.empty(), "drain 要把队列清空"
    assert view.log_lineno.get("1.0", tk.END).split() == ["1", "2", "3", "4", "5"]

    q.put(("再来一条", "OK"))
    view.drain_queue()
    assert view._lineno_count == len(APP_LEVELS) + 1
    assert view.log_lines_var.get() == "6 lines"


def test_unknown_tag_degrades_to_info_without_raising(log_view) -> None:
    view, q = log_view
    q.put(("级别写错了", "TRACE"))
    view.drain_queue()
    assert view.log_text.tag_nextrange("TRACE", "1.0", tk.END) == ()
    assert view.log_text.tag_nextrange("INFO", "1.0", tk.END) != ()


def test_scanline_redraw_is_safe_before_build_and_paints_when_sized(
    tk_root, log_view
) -> None:
    view, _q = log_view
    unbuilt = LogView(tk_root, log_queue=queue.Queue(),
                      make_card_fn=widgets.make_card)
    unbuilt.redraw_scanline()  # 还没 build → 走 AttributeError 守卫
    unbuilt.refresh_cursor_tag()
    assert not hasattr(unbuilt, "log_text")

    view.scan_phase = 0.5
    view.redraw_scanline()
    assert view.scan_canvas.find_all() == (), "未映射时只清不画"
    _fake_size(view.scan_canvas, 400, 2)
    view.redraw_scanline()
    assert len(view.scan_canvas.find_all()) > 1, "跨过守卫后要真画出扫描线"


def test_cursor_blink_tag_follows_phase(log_view) -> None:
    view, _q = log_view
    view.cursor_blink = True
    view.refresh_cursor_tag()
    assert view.log_text.tag_cget("CURSOR_BLINK", "background") == \
        theme.COLORS["success"]
    view.cursor_blink = False
    view.refresh_cursor_tag()
    assert view.log_text.tag_cget("CURSOR_BLINK", "background") in ("", "none")


def test_sync_scroll_moves_line_number_column(log_view) -> None:
    view, q = log_view
    for i in range(40):
        q.put((f" filler line {i}", "INFO"))
    view.drain_queue()
    view.log_text.update_idletasks()
    view._sync_scroll("0.5", "0.6")
    assert 0.0 < float(view.log_lineno.yview()[0]) <= 1.0


# ---------------------------------------------------------------------------
#  12. gui/qr_utils.py — 失败路径不得阻塞
# ---------------------------------------------------------------------------
def test_has_cv2_returns_a_bool() -> None:
    assert isinstance(qr_utils.has_cv2(), bool)


def _record_dialogs(monkeypatch) -> list[tuple[str, str]]:
    dialogs: list[tuple[str, str]] = []
    for fn in ("showerror", "showwarning", "showinfo"):
        monkeypatch.setattr(
            qr_utils.messagebox, fn,
            lambda title, msg, _fn=fn: dialogs.append((_fn, msg)),
        )
    return dialogs


def test_decode_nonexistent_image_returns_none_with_one_dialog(
    monkeypatch
) -> None:
    dialogs = _record_dialogs(monkeypatch)
    assert qr_utils.decode_qr_from_image("Z:/definitely/not/here.png") is None
    assert len(dialogs) == 1, f"只该提示一次，实际 {dialogs}"
    expected = "showerror" if qr_utils.has_cv2() else "showwarning"
    assert dialogs[0][0] == expected


def test_decode_image_without_qr_returns_none(monkeypatch, tmp_path) -> None:
    if not qr_utils.has_cv2():
        pytest.skip("本机未安装 opencv，跳过真图片分支")
    import cv2
    import numpy as np

    ok, buf = cv2.imencode(".png", np.zeros((24, 24, 3), dtype=np.uint8))
    assert ok
    png = tmp_path / "blank.png"
    buf.tofile(str(png))

    dialogs = _record_dialogs(monkeypatch)
    assert qr_utils.decode_qr_from_image(str(png)) is None
    assert [d[0] for d in dialogs] == ["showinfo"], "无二维码 → 未识别提示"


def test_decode_non_image_bytes_returns_none(monkeypatch, tmp_path) -> None:
    if not qr_utils.has_cv2():
        pytest.skip("本机未安装 opencv，跳过坏字节分支")
    bogus = tmp_path / "not-an-image.png"
    bogus.write_bytes(b"\x00\x01\x02not a png at all")
    dialogs = _record_dialogs(monkeypatch)
    assert qr_utils.decode_qr_from_image(str(bogus)) is None
    assert dialogs and dialogs[0][0] == "showerror"
    assert "无法解析图片" in dialogs[0][1]
