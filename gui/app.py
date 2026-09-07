"""问卷自动填写工具 — GUI 主类（赛博朋克·极光主题版）。

视觉特性：
  - 深色极光渐变背景 + 动态星空粒子
  - 毛玻璃 (Glassmorphism) 卡片 + 霓虹发光边框
  - 呼吸灯状态指示 + 按钮悬停辉光动画
  - 赛博朋克终端风日志（行号 + 扫描线）
  - 渐变进度条 + 发光统计 Badge
  - 权重表格行悬停高亮 + 胶囊类型标签
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

# 将项目根目录加入 sys.path，使得启动脚本放在任意位置都能 import src
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from selenium.common.exceptions import InvalidSessionIdException  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402

from gui.qr_utils import decode_qr_from_image  # noqa: E402
from gui.theme import (  # noqa: E402  7A: 主题常量/工具已从本文件剥离
    COLORS,
    FONT_PRESETS,
    GRAD_DANGER,
    GRAD_HEADER,
    GRAD_PRIMARY,
    GRAD_SUCCESS,
    _draw_horizontal_gradient,
    _draw_vertical_gradient,
    _hex_to_rgb,
    _lerp_color,
    _rgb_to_hex,
    apply_ttk_style,
)
from gui.widgets import (  # noqa: E402  7B: 通用 UI 工厂方法已剥离
    _make_card as _widgets_make_card,
    _make_icon_button as _widgets_make_icon_button,
    _make_spin_button as _widgets_make_spin_button,
    _make_stat_badge as _widgets_make_stat_badge,
    _make_toggle as _widgets_make_toggle,
    paint_card_border as _widgets_paint_card_border,
)
from gui.history_panel import HistoryPanel  # noqa: E402  7C: 历史记录面板剥离
from gui.weight_panel import WeightPanel  # noqa: E402  7D: 权重表面板剥离
from gui.log_view import LogView  # noqa: E402  7E: 日志终端面板剥离
from gui.controller import GuiController  # noqa: E402  7F: 命令处理器剥离
from src import __version__ as APP_VERSION  # noqa: E402
from src import config as _cfg_module  # noqa: E402
from src.browser import cleanup_browser_state, create_driver  # noqa: E402
from src.config import (  # noqa: E402
    BROWSER_OPTIONS,
    DEFAULT_BROWSER,
    DEFAULT_USE_UC,
    RESTART_BROWSER_EVERY,
    ROUND_LONG_PAUSE_HI,
    ROUND_LONG_PAUSE_LO,
    ROUND_LONG_PAUSE_PROB,
    ROUND_WAIT_HI,
    ROUND_WAIT_LO,
    ROUND_WAIT_MU,
    ROUND_WAIT_SIGMA,
    WEIGHT_CONFIG,
)
from src.utils import ManualHoldLock, human_pause  # noqa: E402
from src.detection import detect_questions  # noqa: E402
from src.models import RunState  # noqa: E402  Step 9: 共用状态对象
from src.pipeline import run_one_submission  # noqa: E402
from src.verification import (  # noqa: E402
    is_smart_verification_showing,
    wait_for_manual_verification,
)
# V2 新增：配置文件 IO / 历史记录
try:
    from src.config_io import (  # noqa: E402
        load_weight_config,
        save_weight_config,
        validate_weight_config,
    )
    _HAS_CONFIG_IO: bool = True
except Exception:  # pragma: no cover
    load_weight_config = None  # type: ignore
    save_weight_config = None  # type: ignore
    validate_weight_config = None  # type: ignore
    _HAS_CONFIG_IO = False
try:
    from src.history import SubmissionHistory  # noqa: E402
    _HAS_HISTORY: bool = True
except Exception:  # pragma: no cover
    SubmissionHistory = None  # type: ignore
    _HAS_HISTORY = False

# V2.4：静默降级路径（except: pass）统一走 logger.debug 留痕（详见 src/logging_setup.py）
logger = logging.getLogger("wjx.gui.app")


# ============================================================================
#  全局路径 & 版本（V2）
# ============================================================================
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
_DEFAULT_CONFIG_DIR = os.path.join(_PROJECT_ROOT, "configs")
DEFAULT_WEIGHT_CONFIG_PATH = os.path.join(
    _DEFAULT_CONFIG_DIR, "default_weight_config.json"
)
DEFAULT_HISTORY_DB_PATH = os.path.join(
    _PROJECT_ROOT, "data", "history.db"
)
# V2.4：版本号单一真相来自 src.__version__（模块顶部 import 处已 as APP_VERSION）


# ============================================================================
#  主题常量 + 颜色/渐变工具（7A 拆分 → gui.theme）
#  兼容层：已通过模块顶部 from gui.theme import COLORS, GRAD_*, _hex_to_rgb,
#  _rgb_to_hex, _lerp_color, _draw_vertical_gradient, _draw_horizontal_gradient,
#  apply_ttk_style, FONT_PRESETS 引入。若需修改主题，改 gui/theme.py。
# ============================================================================
# （本文件不再重复内联定义，避免双份定义漂移）


# ============================================================================
#  主 GUI 类
# ============================================================================

class SurveyGUI:
    """问卷自动填写工具 — 赛博朋克·极光主题。"""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(f"问卷星自动填写工具 v{APP_VERSION}")
        self.root.geometry("1120x760")
        self.root.minsize(980, 620)
        self.root.configure(bg=COLORS["bg"])

        # 线程安全日志队列
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()

        # 运行状态
        self.driver = None
        self.questions: list[dict] = []
        self.stop_flag = False
        self.running = False
        self.weight_entries: dict[int, tk.StringVar] = {}

        # Step 9 共用状态对象：CLI 同款 RunState 集中管理计数器/续传/停止语义
        # 以下 5 个统计字段保留为"GUI 只读影子镜像"，由 _sync_ui_mirrors 从 state 同步
        self._state: RunState | None = None
        self.success_count = 0
        self.fail_count = 0
        self.current_round = 0
        self.total_rounds = 0

        # 浏览器选择
        self.browser_var = tk.StringVar(value=DEFAULT_BROWSER)
        self.use_uc_var = tk.BooleanVar(value=DEFAULT_USE_UC)

        # 动画状态
        self._breath_phase: float = 0.0
        self._scan_phase: float = 0.0
        self._cursor_blink: bool = True

        # 星空粒子
        self._stars: list[tuple[int, int, float, str]] = []

        # （7C/7D/7E）子面板引用，由 build_* 方法实例化
        self._history_panel: HistoryPanel | None = None
        self._weight_panel: WeightPanel | None = None
        self._log_view: LogView | None = None
        # （7F）命令处理器，封装 config IO / 二维码 / 探测题目
        self._controller: GuiController | None = None

        self._setup_theme()
        self._build_ui()
        self._controller = GuiController(
            host=self,
            project_root=_PROJECT_ROOT,
            has_config_io=_HAS_CONFIG_IO,
            save_weight_config=save_weight_config,
            load_weight_config=load_weight_config,
            validate_weight_config=validate_weight_config,
            default_config_dir=_DEFAULT_CONFIG_DIR,
            default_weight_config_path=DEFAULT_WEIGHT_CONFIG_PATH,
        )
        self._start_log_poller()
        self._start_animations()
        self._auto_load_default_config()

    def _auto_load_default_config(self) -> None:
        if self._controller is None:
            return
        self._controller.auto_load_default_config()

    # ==================================================================
    #  主题系统（7A: 纯逻辑已抽至 gui.theme.apply_ttk_style）
    # ==================================================================

    def _setup_theme(self) -> None:
        """配置全局 ttk 样式（实际实现：gui.theme.apply_ttk_style）。"""
        style, fonts = apply_ttk_style(self.root)
        # 保持向后兼容：把 FONT_PRESETS 中的键映射为 self.FONT_* 实例属性，
        # 这样后续 2000+ 行调用 self.FONT_HUGE 等完全无需改动。
        for key, value in fonts.items():
            setattr(self, f"FONT_{key}", value)
        # FONT_PRESETS 里的 key 与历史命名一致：HUGE/TITLE/LARGE/NORMAL/SMALL/MONO/MONO_L/ICON
        # 若存在额外键，也一并安全设置（未使用也不报错）。
        self._style = style

    # ==================================================================
    #  动画系统
    # ==================================================================

    def _start_animations(self) -> None:
        """启动所有循环动画：呼吸灯、扫描线、光标闪烁。"""
        self._tick_breath()
        self._tick_scanline()
        self._tick_cursor()

    def _tick_breath(self) -> None:
        """呼吸灯动画周期：~2.4s。"""
        self._breath_phase = (self._breath_phase + 0.06) % (2 * 3.14159)
        try:
            self._update_status_glow()
        except Exception:
            pass
        self.root.after(80, self._tick_breath)

    def _tick_scanline(self) -> None:
        """日志区域扫描线动画。"""
        self._scan_phase = (self._scan_phase + 0.012) % 1.0
        if self._log_view is not None:
            self._log_view.scan_phase = self._scan_phase
        try:
            self._redraw_scanline()
        except Exception:
            pass
        self.root.after(40, self._tick_scanline)

    def _tick_cursor(self) -> None:
        """终端光标闪烁。"""
        self._cursor_blink = not self._cursor_blink
        if self._log_view is not None:
            self._log_view.cursor_blink = self._cursor_blink
        try:
            self._refresh_cursor_tag()
        except Exception:
            pass
        self.root.after(530, self._tick_cursor)

    # ==================================================================
    #  UI 构建
    # ==================================================================

    def _build_ui(self) -> None:
        self.root.grid_rowconfigure(0, weight=0)   # 标题栏
        self.root.grid_rowconfigure(1, weight=1)   # 主内容
        self.root.grid_rowconfigure(2, weight=0)   # 底部状态栏
        self.root.grid_columnconfigure(0, weight=1)

        # 极简背景 Canvas（只画一条蓝色装饰线）
        self._bg_canvas = tk.Canvas(
            self.root,
            bg=COLORS["bg"],
            highlightthickness=0,
            bd=0,
        )
        self._bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._bg_canvas.bind("<Configure>", self._on_bg_resize)

        self._build_header()
        self._build_main_content()
        self._build_status_bar()

    # ----------- 极简背景 -----------

    def _on_bg_resize(self, event) -> None:
        self._redraw_bg()

    def _redraw_bg(self) -> None:
        c = self._bg_canvas
        c.delete("bg")
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            return
        # 背景色已由 Canvas bg 提供，这里只画顶部极细蓝色装饰线（克制的特效）
        _draw_horizontal_gradient(
            c, 0, 0, w, 2,
            (COLORS["bg"], COLORS["primary"], COLORS["primary_glow"], COLORS["bg"]),
            tag="bg",
        )
        # 底部一条淡蓝线
        _draw_horizontal_gradient(
            c, 0, h - 2, w, h,
            (COLORS["bg"], "#c8d6ff", COLORS["bg"]),
            tag="bg",
        )

    # ==================================================================
    #  标题栏
    # ==================================================================

    def _build_header(self) -> None:
        """顶部深色标题栏（黑灰渐变）+ Logo + 呼吸灯状态，文字白字高对比。"""
        h = 64
        self.header_canvas = tk.Canvas(
            self.root, height=h, bg="#111111",
            highlightthickness=0, bd=0,
        )
        self.header_canvas.grid(row=0, column=0, sticky="ew")
        self.header_canvas.grid_propagate(False)
        self.header_canvas.bind("<Configure>", self._on_header_resize)

        HDR_BG = "#111111"
        HDR_FG = "#ffffff"
        HDR_SUB = "#bbbbbb"
        self._hdr_bg = HDR_BG
        self._hdr_fg = HDR_FG
        self._hdr_sub = HDR_SUB

        self.header_inner = tk.Frame(self.header_canvas, bg=HDR_BG)
        self.header_inner.pack(fill=tk.BOTH, expand=True, padx=20, pady=8)

        # 左侧：Logo 图标 + 标题
        left = tk.Frame(self.header_inner, bg=HDR_BG)
        left.pack(side=tk.LEFT, fill=tk.Y)

        self.logo_canvas = tk.Canvas(
            left, width=44, height=44, bg=HDR_BG,
            highlightthickness=0, bd=0,
        )
        self.logo_canvas.pack(side=tk.LEFT, padx=(0, 12), pady=2)
        self.logo_canvas.bind("<Configure>", self._on_logo_resize)

        title_box = tk.Frame(left, bg=HDR_BG)
        title_box.pack(side=tk.LEFT, fill=tk.Y)
        self.title_label = tk.Label(
            title_box,
            text="问卷星自动填写工具",
            font=self.FONT_HUGE,
            bg=HDR_BG,
            fg=HDR_FG,
        )
        self.title_label.pack(anchor="w")
        self.subtitle_label = tk.Label(
            title_box,
            text=f"Automation Suite  v{APP_VERSION}  ·  Stealth Engine Ready",
            font=("Cascadia Code", 8),
            bg=HDR_BG,
            fg=HDR_SUB,
        )
        self.subtitle_label.pack(anchor="w", pady=(1, 0))

        # 右侧：状态指示
        right = tk.Frame(self.header_inner, bg=HDR_BG)
        right.pack(side=tk.RIGHT, fill=tk.Y)

        self.status_canvas = tk.Canvas(
            right, width=220, height=44, bg=HDR_BG,
            highlightthickness=0, bd=0,
        )
        self.status_canvas.pack(side=tk.RIGHT)
        self.status_canvas.bind("<Configure>", self._on_status_resize)
        self._status_text = "就绪"
        self._status_color = COLORS["primary"]

    def _on_header_resize(self, event) -> None:
        c = self.header_canvas
        c.delete("hdr")
        w, h = event.width, event.height
        # 黑→深灰水平渐变
        _draw_horizontal_gradient(c, 0, 0, w, h, GRAD_HEADER, tag="hdr")
        # 底部一条细蓝线（克制的点缀）
        _draw_horizontal_gradient(
            c, 0, h - 2, w, h,
            (COLORS["primary"], COLORS["primary_2"]),
            tag="hdr",
        )

    def _on_logo_resize(self, event) -> None:
        c = self.logo_canvas
        c.delete("all")
        w, h = event.width, event.height
        pad = 2
        # 外圈：蓝色极简描边 + 细发光
        for i in range(4, 0, -1):
            t = i / 4
            col = _lerp_color(COLORS["primary_glow"], "#111111", t)
            c.create_rectangle(pad - i, pad - i, w - pad + i, h - pad + i,
                               outline=col, width=1)
        # 内部：纯蓝渐变
        _draw_vertical_gradient(c, pad, pad, w - pad, h - pad, GRAD_PRIMARY)
        # 文字：WJ 白字
        c.create_text(w / 2, h / 2 + 1, text="WJ",
                      font=("Microsoft YaHei UI", 14, "bold"),
                      fill="white")

    def _on_status_resize(self, event) -> None:
        self._update_status_glow()

    def _update_status_glow(self) -> None:
        """根据呼吸相位重绘状态指示（发光圆圈 + 白字）。"""
        if not hasattr(self, "status_canvas"):
            return
        c = self.status_canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1 or h <= 1:
            return

        import math
        breath = 0.55 + 0.45 * math.sin(self._breath_phase)
        base_col = self._status_color

        cx, cy = 14, h / 2
        max_radius = 11
        # 多层发光晕
        for i in range(5, 0, -1):
            t = i / 5
            r = max_radius + i * 3 * breath
            alpha_t = 0.1 + 0.25 * breath * (1 - t)
            col = _lerp_color(base_col, "#111111", 1 - alpha_t)
            c.create_oval(cx - r, cy - r, cx + r, cy + r,
                          outline=col, width=1)
        # 核心圆
        c.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, fill=base_col, outline="")
        c.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill="white", outline="")

        # 文字：白字高对比
        c.create_text(34, cy, text="状态",
                      font=self.FONT_SMALL, fill="#aaaaaa", anchor="w")
        c.create_text(70, cy, text=self._status_text,
                      font=("Microsoft YaHei UI", 10, "bold"),
                      fill="white", anchor="w")

    def _set_status(self, text: str, color: str) -> None:
        self._status_text = text
        self._status_color = color
        self._update_status_glow()

    # ==================================================================
    #  主内容：左设置+表格 / 右日志
    # ==================================================================

    def _build_main_content(self) -> None:
        container = tk.Frame(self.root, bg=COLORS["bg"])
        container.grid(row=1, column=0, sticky="nsew", padx=16, pady=(12, 8))
        container.grid_columnconfigure(0, weight=55)
        container.grid_columnconfigure(1, weight=45)
        container.grid_rowconfigure(0, weight=1)

        # ---- V2 左侧 Notebook（配置 Tab / 历史 Tab） ----
        style = ttk.Style()
        style.layout("Aurora.TNotebook",
                     [("Notebook.client", {"sticky": "nswe"})])
        style.configure("Aurora.TNotebook",
                        background=COLORS["bg"], borderwidth=0)
        style.element_create("aurora_tab", "from", "clam")
        style.layout("Aurora.TNotebook.Tab", [
            ("aurora_tab.tab",
             {"side": "top", "sticky": "nswe", "children": [
                 ("aurora_tab.padding",
                  {"side": "top", "sticky": "nswe", "children": [
                      ("aurora_tab.label", {"sticky": "nswe"})
                  ]})
             ]})
        ])
        style.configure(
            "Aurora.TNotebook.Tab",
            font=("Microsoft YaHei UI", 10, "bold"),
            background=COLORS["surface"],
            foreground=COLORS["text_soft"],
            padding=(22, 9),
            borderwidth=0,
        )
        style.map("Aurora.TNotebook.Tab",
                  background=[("selected", COLORS["primary"])],
                  foreground=[("selected", "white")],
                  )

        notebook = ttk.Notebook(container, style="Aurora.TNotebook")
        notebook.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        # ---------- Tab 1：配置 ----------
        tab_cfg = tk.Frame(notebook, bg=COLORS["bg"])
        notebook.add(tab_cfg, text="  📋  配  置  ")
        tab_cfg.grid_rowconfigure(1, weight=1)
        tab_cfg.grid_columnconfigure(0, weight=1)

        self._build_settings_card(tab_cfg)
        self._build_weight_table_card(tab_cfg)

        # ---------- Tab 2：历史记录 ----------
        tab_history = tk.Frame(notebook, bg=COLORS["bg"])
        notebook.add(tab_history, text="  📜  历史记录  ")
        tab_history.grid_rowconfigure(0, weight=1)
        tab_history.grid_columnconfigure(0, weight=1)
        self._build_history_card(tab_history)  # V2 新增

        # 右侧列
        right = tk.Frame(container, bg=COLORS["bg"])
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)
        self._build_log_area_card(right)

    # ==================================================================
    #  通用卡片包装器：细蓝边 + 白底高对比（7B 代理 → gui.widgets）
    # ==================================================================

    def _make_card(self, parent: tk.Misc, title: str, icon: str = "◆",
                   accent: tuple[str, ...] = GRAD_PRIMARY) -> tk.Frame:
        """创建卡片容器 → 代理到 widgets.make_card。"""
        return _widgets_make_card(parent, title=title, icon=icon, accent=accent)

    def _on_card_resize(self, canvas: tk.Canvas, accent: tuple[str, ...]) -> None:
        """重绘卡片边框 → 代理到 widgets.paint_card_border。"""
        _widgets_paint_card_border(canvas, accent)

    # ==================================================================
    #  设置卡片
    # ==================================================================

    def _build_settings_card(self, parent: tk.Frame) -> None:
        body = self._make_card(parent, "基本设置", icon="⚙", accent=GRAD_PRIMARY)
        body.pack(fill=tk.X, expand=False)
        self._build_url_row(body)
        self._build_count_row(body)
        self._build_browser_row(body)
        self._build_config_io_row(body)

    def _build_url_row(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=COLORS["surface"])
        row.pack(fill=tk.X, pady=(0, 10))

        tk.Label(row, text="🔗  问卷 URL",
                 font=self.FONT_NORMAL, fg=COLORS["text_soft"],
                 bg=COLORS["surface"], width=13, anchor="w").pack(side=tk.LEFT)

        self.url_var = tk.StringVar()
        url_entry_wrapper = tk.Frame(row, bg=COLORS["surface"])
        url_entry_wrapper.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        url_entry = tk.Entry(
            url_entry_wrapper,
            textvariable=self.url_var,
            font=("Cascadia Code", 10),
            bg=COLORS["bg_mid"],
            fg=COLORS["text"],
            insertbackground=COLORS["primary_2"],
            relief=tk.FLAT,
            bd=0,
            highlightthickness=2,
            highlightbackground=COLORS["border_dim"],
            highlightcolor=COLORS["primary"],
        )
        url_entry.pack(fill=tk.X, ipady=6, padx=1, pady=1)

        # 按钮组
        btn_group = tk.Frame(row, bg=COLORS["surface"])
        btn_group.pack(side=tk.RIGHT)

        self.qr_btn = self._make_icon_button(
            btn_group, "📷 扫码", accent="ghost",
            command=self._on_import_qr,
        )
        self.qr_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.detect_btn = self._make_icon_button(
            btn_group, "🔍 探测题目", accent="primary",
            command=self._on_detect_questions,
        )
        self.detect_btn.pack(side=tk.LEFT)

    def _build_count_row(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=COLORS["surface"])
        row.pack(fill=tk.X, pady=(0, 10))

        tk.Label(row, text="📊  提交份数",
                 font=self.FONT_NORMAL, fg=COLORS["text_soft"],
                 bg=COLORS["surface"], width=13, anchor="w").pack(side=tk.LEFT)

        self.count_var = tk.IntVar(value=1)

        sp = tk.Frame(row, bg=COLORS["surface"])
        sp.pack(side=tk.LEFT)

        # - 按钮
        dec_btn = self._make_spin_button(sp, "−", "minus")
        dec_btn.pack(side=tk.LEFT)

        # 中间数字输入
        cnt_entry = tk.Entry(
            sp,
            textvariable=self.count_var,
            font=("Cascadia Code", 12, "bold"),
            width=7,
            justify=tk.CENTER,
            bg=COLORS["bg_mid"],
            fg=COLORS["primary_2"],
            insertbackground=COLORS["primary_2"],
            relief=tk.FLAT,
            bd=0,
            highlightthickness=2,
            highlightbackground=COLORS["border_dim"],
            highlightcolor=COLORS["primary"],
        )
        cnt_entry.pack(side=tk.LEFT, padx=2, ipady=4)

        inc_btn = self._make_spin_button(sp, "+", "plus")
        inc_btn.pack(side=tk.LEFT)

        tk.Label(row, text=" 份  ( 1 ~ 9999 )",
                 font=self.FONT_SMALL, fg=COLORS["text_dim"],
                 bg=COLORS["surface"]).pack(side=tk.LEFT, padx=(10, 0))

        def _up():
            self.count_var.set(min(9999, self.count_var.get() + 1))

        def _down():
            self.count_var.set(max(1, self.count_var.get() - 1))

        inc_btn.configure(command=_up)
        dec_btn.configure(command=_down)

    def _build_browser_row(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=COLORS["surface"])
        row.pack(fill=tk.X)

        tk.Label(row, text="🌐  浏览器",
                 font=self.FONT_NORMAL, fg=COLORS["text_soft"],
                 bg=COLORS["surface"], width=13, anchor="w").pack(side=tk.LEFT)

        combo_wrapper = tk.Frame(row, bg=COLORS["bg_mid"],
                                 highlightthickness=2,
                                 highlightbackground=COLORS["border_dim"])
        combo_wrapper.pack(side=tk.LEFT, padx=(0, 20))

        browser_combo = ttk.Combobox(
            combo_wrapper,
            textvariable=self.browser_var,
            values=[opt.capitalize() for opt in BROWSER_OPTIONS],
            state="readonly",
            width=10,
            style="TCombobox",
        )

        def _on_browser_change(_e=None):
            v = self.browser_var.get()
            if v.lower() in BROWSER_OPTIONS:
                self.browser_var.set(v.lower())
            self.use_uc_chk.configure(
                state=("normal" if self.browser_var.get() == "chrome" else "disabled")
            )

        browser_combo.bind("<<ComboboxSelected>>", _on_browser_change)
        browser_combo.set(self.browser_var.get().capitalize())
        browser_combo.pack(padx=6, pady=4)

        # UC 复选框（发光 Badge 风格）
        self.use_uc_chk = self._make_toggle(
            row,
            text="UC 模式  (更强反爬，需 undetected-chromedriver)",
            var=self.use_uc_var,
            enabled=self.browser_var.get() == "chrome",
        )
        self.use_uc_chk.pack(side=tk.LEFT)

    def _build_config_io_row(self, parent: tk.Frame) -> None:
        """配置导入 / 导出 / 另存默认。V2 新增。"""
        row = tk.Frame(parent, bg=COLORS["surface"])
        row.pack(fill=tk.X, pady=(4, 0))

        tk.Label(row, text="📦  配置文件",
                 font=self.FONT_NORMAL, fg=COLORS["text_soft"],
                 bg=COLORS["surface"], width=13, anchor="w").pack(side=tk.LEFT)

        btns = tk.Frame(row, bg=COLORS["surface"])
        btns.pack(side=tk.LEFT, fill=tk.X, expand=True)

        btn_load = self._make_icon_button(
            btns, "📂 导入配置", accent="ghost",
            command=self._on_load_config,
        )
        btn_save = self._make_icon_button(
            btns, "💾 导出配置", accent="success",
            command=self._on_save_config,
        )
        btn_default = self._make_icon_button(
            btns, "⭐ 另存默认", accent="primary",
            command=self._on_save_default_config,
        )
        btn_load.pack(side=tk.LEFT, padx=(0, 6))
        btn_save.pack(side=tk.LEFT, padx=(0, 6))
        btn_default.pack(side=tk.LEFT)

        if not _HAS_CONFIG_IO:
            # 模块缺失时禁用按钮并给出提示
            for b in (btn_load, btn_save, btn_default):
                b.configure(state=tk.DISABLED)
            tk.Label(row,
                     text="⚠ src/config_io 未加载，功能不可用",
                     font=self.FONT_SMALL,
                     fg=COLORS["warning"],
                     bg=COLORS["surface"]).pack(side=tk.RIGHT)

    # ----- 设置区：自定义按钮（7B 代理 → gui.widgets） -----

    def _make_icon_button(self, parent, text: str, accent: str = "primary",
                          command=None) -> tk.Button:
        """高对比按钮 → 代理到 widgets.make_icon_button。"""
        return _widgets_make_icon_button(parent, text=text, accent=accent,
                                         command=command)

    def _make_spin_button(self, parent, text: str, kind: str) -> tk.Button:
        """数字增减按钮 → 代理到 widgets.make_spin_button。"""
        return _widgets_make_spin_button(parent, text=text, kind=kind)

    def _make_toggle(self, parent, text: str, var: tk.BooleanVar,
                     enabled: bool = True) -> tk.Checkbutton:
        """高对比复选框 → 代理到 widgets.make_toggle。"""
        return _widgets_make_toggle(parent, text=text, var=var, enabled=enabled)

    # ==================================================================
    #  历史记录卡片（V2 新增）
    # ==================================================================

    # ------------ 7C: 历史记录代理到 gui.history_panel.HistoryPanel ------------

    def _history_ensure_panel(self) -> HistoryPanel | None:
        """懒构造 HistoryPanel（保证在 GUI 尚未完全构造前调用不崩溃）。"""
        if self._history_panel is None:
            if not hasattr(self, "root"):
                return None
            self._history_panel = HistoryPanel(
                root=self.root,
                log_fn=self._log,
                has_history=_HAS_HISTORY,
                submission_history_cls=SubmissionHistory,  # 未加载时为 None
                history_db_path=DEFAULT_HISTORY_DB_PATH,
                make_card_fn=self._make_card,
                make_icon_btn_fn=self._make_icon_button,
                fonts={
                    k.removeprefix("FONT_"): v
                    for k, v in vars(self).items()
                    if k.startswith("FONT_")
                } if hasattr(self, "FONT_NORMAL") else None,
            )
        return self._history_panel

    def _history_get_db(self):
        """懒构造 SubmissionHistory → 代理。"""
        panel = self._history_ensure_panel()
        if panel is None:
            return None
        return panel.get_db()

    def _build_history_card(self, parent: tk.Frame) -> None:
        """V2：显示 runs + answers 双表 + 工具栏 → 代理到 HistoryPanel。"""
        panel = self._history_ensure_panel()
        assert panel is not None, "GUI root 尚未就绪，无法构建历史面板"
        panel.build(parent)
        # 兼容属性：把 panel 的公共变量挂回 self，供后续代码读取
        self.history_summary_var = panel.summary_var
        self.history_answer_head_var = panel.answer_head_var
        self.history_runs_tree = panel.runs_tree
        self.history_ans_tree = panel.ans_tree

    def _history_refresh(self) -> None:  self._history_ensure_panel() and self._history_panel.refresh()
    def _history_select_run(self, _e=None) -> None:  self._history_ensure_panel() and self._history_panel.select_run(_e)
    def _history_export_csv(self) -> None:  self._history_ensure_panel() and self._history_panel.export_csv()
    def _history_purge_old(self) -> None:  self._history_ensure_panel() and self._history_panel.purge_old()

    # ==================================================================
    #  权重表格卡片
    # ==================================================================

    # ------------ 7D: 权重表面板代理到 gui.weight_panel.WeightPanel ------------

    def _ensure_weight_panel(self) -> WeightPanel:
        if self._weight_panel is None:
            self._weight_panel = WeightPanel(
                root=self.root,
                log_fn=self._log,
                weight_config_global=WEIGHT_CONFIG,
                make_card_fn=self._make_card,
                fonts={
                    k.removeprefix("FONT_"): v
                    for k, v in vars(self).items()
                    if k.startswith("FONT_")
                } if hasattr(self, "FONT_NORMAL") else None,
                shared_questions=self.questions,
                shared_weight_entries=self.weight_entries,
            )
        return self._weight_panel

    def _build_weight_table_card(self, parent: tk.Frame) -> None:
        panel = self._ensure_weight_panel()
        panel.build(parent)
        # 兼容属性：table_frame / weight_canvas 外部（_on_card_resize 等）可能使用
        self.table_frame = panel.table_frame
        if hasattr(panel, "weight_canvas"):
            self.weight_canvas = panel.weight_canvas

    def _draw_table_header(self) -> None:  self._ensure_weight_panel().draw_header()
    def _show_table_placeholder(self) -> None:  self._ensure_weight_panel().show_placeholder()

    def _populate_weight_table(self, questions: list[dict]) -> None:
        self._ensure_weight_panel().populate(questions)
        # populate 已经通过共享引用更新了 self.questions[:] / self.weight_entries
        # 如果调用者期望 self.questions 是新 LIST 对象：重新赋回同一引用保证一致
        # （共享方式：panel.questions 与 self.questions 是同一个 list 对象）
        self.table_frame = self._weight_panel.table_frame

    # ==================================================================
    #  日志卡片（赛博朋克终端）
    # ==================================================================

    # ------------ 7E: 日志终端代理到 gui.log_view.LogView ------------

    def _ensure_log_view(self) -> LogView:
        if self._log_view is None:
            self._log_view = LogView(
                root=self.root,
                log_queue=self.log_queue,
                make_card_fn=self._make_card,
            )
            # 同步初始动画相位
            self._log_view.scan_phase = self._scan_phase
            self._log_view.cursor_blink = self._cursor_blink
        return self._log_view

    def _build_log_area_card(self, parent: tk.Frame) -> None:
        panel = self._ensure_log_view()
        panel.build(parent)
        # 兼容属性（外部代码 / Status bar 可能引用）
        self.log_text = panel.log_text
        self.log_lineno = panel.log_lineno
        self.log_tags = panel._tags
        self.scan_canvas = panel.scan_canvas
        self.log_lineno_count = panel._lineno_count
        self.log_lines_var = panel.log_lines_var
        self._cursor_tag_configured = False

    def _sync_scroll(self, *args):
        if self._log_view is not None:
            self._log_view._sync_scroll(*args)

    def _redraw_scanline(self) -> None:
        if self._log_view is not None:
            self._log_view.redraw_scanline()

    def _refresh_cursor_tag(self) -> None:
        if self._log_view is not None:
            self._log_view.refresh_cursor_tag()

    # ==================================================================
    #  底部状态栏（玻璃态控制栏）
    # ==================================================================

    def _build_status_bar(self) -> None:
        # 最外层：白底蓝边容器
        outer = tk.Frame(self.root, bg=COLORS["bg"])
        outer.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 14))

        self.status_canvas_bar = tk.Canvas(outer, bg=COLORS["bg"],
                                           highlightthickness=0, bd=0, height=92)
        self.status_canvas_bar.pack(fill=tk.X)
        self.status_canvas_bar.bind("<Configure>", self._on_status_bar_resize)

        body = tk.Frame(self.status_canvas_bar, bg=COLORS["surface"])
        self._status_bar_win = self.status_canvas_bar.create_window(
            (3, 3), window=body, anchor="nw"
        )

        def _bc(_e, cc=self.status_canvas_bar, bw=self._status_bar_win, bd=body):
            w = cc.winfo_width() - 6
            h = cc.winfo_height() - 6
            if w > 0 and h > 0:
                cc.itemconfigure(bw, width=w, height=h)
        self.status_canvas_bar.bind("<Configure>", _bc, add="+")

        # 布局：左（开始/停止按钮）| 中（进度）| 右（统计）
        body.grid_columnconfigure(2, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # ---------- 左：控制按钮组 ----------
        ctrl = tk.Frame(body, bg=COLORS["surface"])
        ctrl.grid(row=0, column=0, sticky="ns", padx=(16, 8), pady=14)

        self.start_btn = self._make_icon_button(
            ctrl, "▶  开始运行", accent="success",
            command=self._on_start,
        )
        self.start_btn.configure(font=("Microsoft YaHei UI", 11, "bold"),
                                 padx=22, pady=10)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.stop_btn = self._make_icon_button(
            ctrl, "■  停止", accent="danger",
            command=self._on_stop,
        )
        self.stop_btn.configure(font=("Microsoft YaHei UI", 11, "bold"),
                                padx=18, pady=10, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)

        # ---------- 中：进度条 + 进度文字 ----------
        prog_wrap = tk.Frame(body, bg=COLORS["surface"])
        prog_wrap.grid(row=0, column=2, sticky="nsew", padx=14, pady=14)
        prog_wrap.grid_rowconfigure(1, weight=1)
        prog_wrap.grid_columnconfigure(0, weight=1)

        # 进度标题行
        prog_header = tk.Frame(prog_wrap, bg=COLORS["surface"])
        prog_header.pack(fill=tk.X)
        tk.Label(prog_header, text="⚡ 执行进度",
                 font=("Microsoft YaHei UI", 9, "bold"),
                 bg=COLORS["surface"], fg=COLORS["primary_2"]).pack(side=tk.LEFT)
        self.progress_text_var = tk.StringVar(value="0 / 0")
        tk.Label(prog_header, textvariable=self.progress_text_var,
                 font=("Cascadia Code", 10, "bold"),
                 bg=COLORS["surface"], fg=COLORS["text"]).pack(side=tk.RIGHT)
        self.progress_pct_var = tk.StringVar(value="0.0%")
        tk.Label(prog_header, textvariable=self.progress_pct_var,
                 font=("Cascadia Code", 10),
                 bg=COLORS["surface"], fg=COLORS["primary_2"]).pack(side=tk.RIGHT, padx=(0, 10))

        # 进度条：用 Canvas 绘制渐变进度
        self.progress_canvas = tk.Canvas(prog_wrap, height=18,
                                         bg=COLORS["bg_mid"],
                                         highlightthickness=0, bd=0)
        self.progress_canvas.pack(fill=tk.X, pady=(6, 0))
        self.progress_canvas.bind("<Configure>", self._on_progress_resize)
        self._progress_value = 0.0  # 0~1

        # ---------- 右：统计 Badge ----------
        stats = tk.Frame(body, bg=COLORS["surface"])
        stats.grid(row=0, column=3, sticky="ns", padx=(0, 16), pady=14)

        self.success_var = tk.StringVar(value="0")
        self.fail_var = tk.StringVar(value="0")

        self._make_stat_badge(stats, "成功", self.success_var,
                              GRAD_SUCCESS, "✓").pack(side=tk.LEFT, padx=(0, 10))
        self._make_stat_badge(stats, "失败", self.fail_var,
                              GRAD_DANGER, "✕").pack(side=tk.LEFT)

    def _on_status_bar_resize(self, event) -> None:
        self._on_card_resize(self.status_canvas_bar, GRAD_PRIMARY)

    def _on_progress_resize(self, event) -> None:
        self._redraw_progress()

    def _redraw_progress(self) -> None:
        if not hasattr(self, "progress_canvas"):
            return
        c = self.progress_canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1 or h <= 1:
            return
        # 背景轨道
        c.create_rectangle(0, 0, w, h, fill=COLORS["bg_mid"], outline="")
        # 轨道发光边
        c.create_rectangle(0, 0, w, h, outline=COLORS["border_dim"], width=1)

        # 进度渐变
        fill_w = int(w * self._progress_value)
        if fill_w > 0:
            # 多圈发光
            for i in range(4, 0, -1):
                col = _lerp_color(COLORS["primary_glow"], COLORS["bg_mid"], i / 5)
                c.create_rectangle(0, 0, fill_w + i, h, outline=col, width=1)
            # 主体渐变
            ph = max(1, h - 2)
            _draw_horizontal_gradient(
                c, 1, 1, fill_w, 1 + ph, GRAD_PRIMARY
            )
            # 进度头高光
            if fill_w > 8:
                c.create_rectangle(fill_w - 4, 1, fill_w, h - 1,
                                   fill=COLORS["primary_2"], outline="")
                # 光点
                c.create_oval(fill_w - 3, h / 2 - 2, fill_w - 1, h / 2 + 2,
                              fill="white", outline="")

    def _make_stat_badge(self, parent, label: str, var: tk.StringVar,
                         grad: tuple[str, ...], icon: str) -> tk.Frame:
        """渐变发光数字统计 Badge → 代理到 widgets.make_stat_badge。"""
        return _widgets_make_stat_badge(
            parent, label=label, var=var, grad=grad, icon=icon,
            root=self.root,
        )

    # ==================================================================
    #  日志系统（代理到 LogView）
    # ==================================================================

    def _log(self, message: str, tag: str = "INFO") -> None:
        self.log_queue.put((message, tag))

    def _start_log_poller(self) -> None:
        self._drain_log_queue()
        self.root.after(80, self._start_log_poller)

    def _drain_log_queue(self) -> None:
        if self._log_view is not None:
            self._log_view.drain_queue()
            # 兼容：同步 _lineno_count 回到 app，以便旧代码读取 log_lineno_count
            self.log_lineno_count = self._log_view._lineno_count

    def _append_log(self, message: str, tag: str) -> None:
        if self._log_view is None:
            _ = self._ensure_log_view()
        self._log_view._append(message, tag)
        self.log_lineno_count = self._log_view._lineno_count

    # ==================================================================
    #  权重配置导出
    # ==================================================================

    def _build_weight_config(self) -> dict:
        return self._ensure_weight_panel().build_weight_config()

    # ==================================================================
    #  配置 IO 按钮处理（V2）—— 代理到 GuiController
    # ==================================================================

    def _on_save_config(self) -> None:
        if self._controller is None: return
        self._controller.on_save_config()

    def _on_save_default_config(self) -> None:
        if self._controller is None: return
        self._controller.on_save_default_config()

    def _on_load_config(self, path: str | None = None) -> None:
        if self._controller is None: return
        self._controller.on_load_config(path)

    # ==================================================================
    #  探测题目 —— 代理到 GuiController
    # ==================================================================

    def _on_import_qr(self) -> None:
        if self._controller is None: return
        self._controller.on_import_qr()

    def _on_detect_questions(self) -> None:
        if self._controller is None: return
        self._controller.on_detect_questions()

    def _restore_weight_table_from_config(self, restored_w: dict[int, dict]) -> None:
        self._ensure_weight_panel().restore_from_config(restored_w)
        if self._weight_panel is not None:
            self.table_frame = self._weight_panel.table_frame

    def _on_questions_detected(self, questions: list[dict]) -> None:
        if self._controller is None:
            self._populate_weight_table(questions)
            return
        self._controller.on_questions_detected(questions)

    # ==================================================================
    #  运行控制
    # ==================================================================

    # ==================================================================
    #  运行控制（Step 9：共用 CLI 同款 RunState 状态对象）
    # ==================================================================

    def _sync_ui_mirrors_from_state(self) -> None:
        """把 RunState 的计数器同步到 GUI 线程会读取的 self.success_count 等字段。

        GUI `_update_progress` / 日志输出 / 统计 Badge 都在主线程读这些实例
        字段；保持一份"影子镜像"让 UI 代码无需每次加 None 检查，也不需要
        跨线程 state 访问。
        """
        state = self._state
        if state is None:
            return
        self.success_count = state.success_count
        self.fail_count = state.fail_count
        # displayed_round = resume_start_idx + current_attempt - 1
        if state.current_attempt <= 0:
            # 还没跑第 1 次：显示起点
            self.current_round = max(1, state.resume_start_idx)
        else:
            self.current_round = state.displayed_round
        self.total_rounds = state.total_target

    def _on_start(self) -> None:
        if self.running:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请填写问卷 URL")
            return
        total = self.count_var.get()
        if total < 1:
            messagebox.showwarning("提示", "提交份数至少为 1")
            return

        # ---- V2 权重配置 ----
        if self.questions:
            _cfg_module.WEIGHT_CONFIG.clear()
            _cfg_module.WEIGHT_CONFIG.update(self._build_weight_config())
            self._log(f"已加载 {len(_cfg_module.WEIGHT_CONFIG)} 道题的自定义权重", "INFO")
        else:
            _cfg_module.WEIGHT_CONFIG.clear()
            self._log("未配置权重表格，所有题目使用等权重随机", "WARN")

        # ---- Step 9: 构造 RunState（全新批次），续传时更新 resume_start_idx / run_id ----
        browser_name = self.browser_var.get()
        use_uc_flag = bool(self.use_uc_var.get())
        state = RunState(
            attempts_cap=int(total),          # GUI 总是"计划总数 = 最多尝试 N 次"
            total_target=int(total),
            resume_start_idx=1,                # 默认从第 1 份开始
            browser=browser_name,
            use_uc=use_uc_flag,
            survey_url=url[:500],
            weight_config_snapshot=RunState.snapshot_weight_config(
                dict(_cfg_module.WEIGHT_CONFIG)
            ),
        )

        # ---- V2 断点续传：检查可恢复的上次批次 ----
        db_for_resume = self._history_get_db()
        if db_for_resume is not None:
            try:
                prev = db_for_resume.find_resumable_run(url[:500])
                if prev is not None:
                    done = int(prev["success_count"])
                    planned = int(prev["total_submissions"])
                    if 0 < done < planned:
                        try:
                            restored_w = type(db_for_resume).deserialize_weight_config(prev)
                        except Exception:
                            restored_w = {}
                        if restored_w:
                            _cfg_module.WEIGHT_CONFIG.clear()
                            _cfg_module.WEIGHT_CONFIG.update(restored_w)
                            state.weight_config_snapshot = (
                                RunState.snapshot_weight_config(restored_w)
                            )
                            self._log(
                                f"[续传] 已自动恢复上次权重配置：{len(restored_w)} 道题",
                                "OK",
                            )
                            self._restore_weight_table_from_config(restored_w)

                        msg = (
                            f"检测到上次未完成的批次：\n\n"
                            f"  Run #{prev['id']} · 状态 = {prev['status']}\n"
                            f"  已成功 {done} / {planned} 份\n"
                            f"  开始时间 {str(prev['started_at'])[:19]}\n\n"
                        )
                        if state.weight_config_snapshot:
                            msg += (
                                "✅ 上次权重已自动恢复到表格，\n"
                                "    可在配置 Tab 检查 / 修改后再启动。\n\n"
                            )
                        msg += (
                            f"是否从第 {done + 1} 份继续？"
                            "（取消则从第 1 份重新开始，但权重恢复仍生效）"
                        )
                        yes = messagebox.askyesno(
                            "断点续传", msg, icon=messagebox.QUESTION,
                        )
                        if yes:
                            state.resume_start_idx = done + 1
                            state.run_id = int(prev["id"])
                            state.success_count = done  # 历史成功已计入，用于进度显示
                            state.total_target = planned
                            state.attempts_cap = planned - done  # 还要跑多少份
                            self._log(
                                f"[续传] 恢复 Run #{state.run_id}："
                                f"从第 {state.resume_start_idx} 份继续（共 {planned} 份，"
                                f"剩余 {state.attempts_cap} 份待跑）",
                                "OK",
                            )
                        else:
                            self._log(
                                "[续传] 已忽略上次中断批次，从第 1 份重新开始"
                                "（权重恢复仍生效）",
                                "INFO",
                            )
            except Exception as e:
                self._log(
                    f"[续传] 检查可恢复批次失败（不影响运行）: "
                    f"{type(e).__name__}: {e}",
                    "WARN",
                )

        # ---- 绑定 state + 初始化 UI 影子镜像 ----
        self._state = state
        self.running = True
        self.stop_flag = False
        self._sync_ui_mirrors_from_state()

        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.detect_btn.configure(state=tk.DISABLED)
        self.qr_btn.configure(state=tk.DISABLED)
        self._set_status("运行中...", COLORS["primary"])

        self._progress_value = (
            (self.success_count + self.fail_count) / self.total_rounds
            if self.total_rounds else 0.0
        )
        self._redraw_progress()
        self._update_progress()

        self._log("═" * 40, "HEADER")
        if state.resume_start_idx > 1:
            self._log(
                f"▶ 断点续传启动：从第 {state.resume_start_idx} 份 → 第 {state.total_target} 份"
                f"（共 {state.attempts_cap} 份待跑）",
                "HEADER",
            )
        else:
            self._log(f"▶ 开始执行，目标 {state.total_target} 份", "HEADER")
        self._log("═" * 40, "HEADER")

        threading.Thread(
            target=self._run_loop,
            args=(state,),
            daemon=True,
        ).start()

    def _on_stop(self) -> None:
        if not self.running:
            return
        if self._state is not None:
            self._state.request_stop()
        self.stop_flag = True
        self.stop_btn.configure(state=tk.DISABLED)
        self._set_status("正在停止...", COLORS["warning"])
        self._log("用户请求停止，等待当前轮次完成...", "WARN")

    def _run_loop(
        self,
        state: RunState,
    ) -> None:
        """批量提交主循环（Step 9：完全走 RunState，计数器统一到 state.mark_* 分支）。

        签名简化：旧版 ``_run_loop(url,total,start_idx,resume_run_id)`` → 新版只传 state，
        所有配置/续传/计数器都在 state 里。
        """
        driver = None
        history_db: "SubmissionHistory | None" = None
        url = state.survey_url
        t0 = time.perf_counter()
        state.total_elapsed_start = t0
        try:
            # ---- 历史落盘 ----
            db = self._history_get_db()
            if db is not None:
                try:
                    if state.run_id is None:
                        state.run_id = db.start_run(
                            survey_url=url[:500],
                            total_submissions=state.total_target,
                            browser=state.browser,
                            use_uc=state.use_uc,
                            weight_config=state.weight_config_snapshot,
                        )
                        self._log(
                            f"[历史] Run #{state.run_id} 已记录起点"
                            + (f" · 权重快照 {len(state.weight_config_snapshot)} 道题"
                               if state.weight_config_snapshot else " · 等权重"),
                            "INFO",
                        )
                    else:
                        self._log(
                            f"[历史] 续传模式 · 复用 Run #{state.run_id}"
                            "（不重置计数，权重沿用上次）",
                            "INFO",
                        )
                    history_db = db
                except Exception as e:
                    self._log(f"[历史] start_run 失败（不影响答题）: "
                              f"{type(e).__name__}: {e}", "WARN")
                    history_db = None
                    state.run_id = None

            # 启动浏览器
            driver = create_driver(state.browser, use_uc=state.use_uc)

            # V2.4 修复：人工介入锁贯穿整轮批次（与 CLI 同款）。
            # 此前 v2.3 重构后本循环漏传 lock → run_one_submission 抛 TypeError
            # 被兜底 except 吞成"运行异常"日志（P0 级回归）。
            lock = ManualHoldLock()

            # 循环：attempts_cap 为"还需跑多少份"（续传时 = planned - done）
            while state.current_attempt < state.attempts_cap:
                if state.stop_flag:
                    # 用户点了"停止"→ CLI 同款 interrupted 语义
                    state.mark_interrupted()
                    self._log("已停止运行（已成功份数可下次恢复）", "WARN")
                    break

                state.advance_attempt()  # 当前尝试序号：1..attempts_cap
                displayed_idx = state.displayed_round
                self._sync_ui_mirrors_from_state()
                self.root.after(0, self._update_progress)
                self._log(f"[{displayed_idx}/{state.total_target}] 提交中...", "INFO")

                try:
                    # V2.4 修复：lock 必传（与 CLI 同一处回归）
                    outcome = run_one_submission(
                        driver, url,
                        lock,
                        history_db=history_db,
                        run_id=state.run_id,
                        submission_index=displayed_idx,
                    )
                except InvalidSessionIdException:
                    self._log("浏览器断开，正在重建...", "WARN")
                    try: driver.quit()
                    except Exception:
                        logger.debug("重建前 driver.quit() 失败（忽略）", exc_info=True)
                    driver = create_driver(state.browser, use_uc=state.use_uc)
                    state.mark_failure()
                    self._sync_ui_mirrors_from_state()
                    self.root.after(0, self._update_progress)
                    continue

                # ---- 三态统计（走 state 集中方法，避免散落分支漏同步） ----
                if outcome == "success":
                    state.mark_success()
                    self._log("✓ 提交成功", "OK")
                elif outcome == "unknown":
                    state.mark_unknown()
                    self._log("⚠ 提交状态未知（按钮已点击但效果超时）", "FAIL")
                else:
                    state.mark_failure()
                    self._log("✕ 提交失败", "FAIL")
                self._sync_ui_mirrors_from_state()
                self.root.after(0, self._update_progress)

                # V2.4 整改：浏览器状态清理收敛到 src.browser.cleanup_browser_state（CLI 共用）
                cleanup_browser_state(driver)

                if state.current_attempt % RESTART_BROWSER_EVERY == 0:
                    self._log("重启浏览器释放内存", "INFO")
                    try: driver.quit()
                    except Exception:
                        logger.debug("重启前 driver.quit() 失败（忽略）", exc_info=True)
                    driver = create_driver(state.browser, use_uc=state.use_uc)

                # V2.4 整改：轮间停顿统一为高斯分布（与 CLI 一致）。
                # 旧版 uniform(min, max) 均匀分布是可疑的机器人特征，
                # 与 config.py "正态分布 + 区间截断" 的反检测原则矛盾。
                human_pause(
                    ROUND_WAIT_MU, ROUND_WAIT_SIGMA,
                    ROUND_WAIT_LO, ROUND_WAIT_HI,
                    long_pause_prob=ROUND_LONG_PAUSE_PROB,
                    long_lo=ROUND_LONG_PAUSE_LO,
                    long_hi=ROUND_LONG_PAUSE_HI,
                )

        except Exception as e:
            # V2.4 修复：未捕获异常 → 批次记 failed。旧版注释声称"status=failed"
            # 但从未实现，崩溃批次被 history_status() 误标 finished 污染成功率，
            # 且状态为 finished 后 find_resumable_run 也不会再恢复它。
            state.mark_crashed(f"{type(e).__name__}: {e}")
            self._log(f"运行异常: {type(e).__name__}: {e}", "FAIL")
        finally:
            # ---- V2：写入结束状态（走 RunState.history_status / history_error_message） ----
            if history_db is not None and state.run_id is not None:
                try:
                    note = f"GUI · browser={state.browser} uc={state.use_uc}"
                    elapsed = time.perf_counter() - state.total_elapsed_start
                    # 兼容旧版：如果 history 有 mark_interrupted 方法就用（GUI 续传友好）
                    if (state.is_interrupted or state.stop_flag) and hasattr(
                        history_db, "mark_interrupted"
                    ):
                        history_db.mark_interrupted(
                            state.run_id,
                            success_count=state.success_count,
                            fail_count=state.fail_count,
                            total_elapsed_seconds=max(0.0, elapsed),
                            error_message=note,
                        )
                    else:
                        history_db.finish_run(
                            state.run_id,
                            success_count=state.success_count,
                            fail_count=state.fail_count,
                            total_elapsed_seconds=max(0.0, elapsed),
                            status=state.history_status(),
                            error_message=state.history_error_message(suffix=note),
                        )
                    self._log(
                        f"[历史] Run #{state.run_id} 已闭合: "
                        f"{state.history_status()} "
                        f"(✓ {state.success_count} / ✕ {state.fail_count})",
                        "INFO",
                    )
                except Exception as he:
                    self._log(
                        f"[历史] finish_run 失败: {type(he).__name__}: {he}",
                        "WARN",
                    )
            if driver:
                try: driver.quit()
                except Exception:
                    logger.debug("收尾 driver.quit() 失败（忽略）", exc_info=True)
            self._sync_ui_mirrors_from_state()
            self.root.after(0, self._on_run_finished)

    def _on_run_finished(self) -> None:
        # 结束状态：Step 9 已把计数器同步到 ui 镜像（success_count / fail_count）
        self.running = False
        if self._state is not None:
            # 如果从未被用户主动 request_stop 也未 interrupted，清理遗留 stop_flag
            if not self._state.is_interrupted and not self._state.stop_flag:
                self.stop_flag = False
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.detect_btn.configure(state=tk.NORMAL)
        self.qr_btn.configure(state=tk.NORMAL)
        self._set_status("就绪", COLORS["text_dim"])
        self._log("═" * 40, "HEADER")
        self._log(
            f"执行结束 — 成功 {self.success_count}  ·  失败 {self.fail_count}",
            "HEADER",
        )
        self._log("═" * 40, "HEADER")
        # 未知分项单独统计，方便复盘（与 CLI 同款 summary 格式对齐）
        if self._state is not None and self._state.unknown_count > 0:
            self._log(
                f"[统计] 其中 {self._state.unknown_count} 次提交结果未知"
                "（按钮已点击但未观察到成功信号），已保守计入失败数。",
                "WARN",
            )
        try:
            self._history_refresh()
        except Exception:
            pass

    def _update_progress(self) -> None:
        self.success_var.set(str(self.success_count))
        self.fail_var.set(str(self.fail_count))
        self.progress_text_var.set(f"{self.current_round} / {self.total_rounds}")
        pct = (self.current_round / self.total_rounds * 100) if self.total_rounds else 0.0
        self.progress_pct_var.set(f"{pct:>5.1f}%")
        self._progress_value = (self.current_round / self.total_rounds) if self.total_rounds else 0.0
        self._redraw_progress()

    # ==================================================================
    #  启动
    # ==================================================================

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    SurveyGUI().run()
