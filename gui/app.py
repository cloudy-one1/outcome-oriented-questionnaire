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

import os
import queue
import random
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
from src import config as _cfg_module  # noqa: E402
from src.browser import create_driver  # noqa: E402
from src.config import (  # noqa: E402
    BROWSER_OPTIONS,
    DEFAULT_BROWSER,
    DEFAULT_USE_UC,
    RESTART_BROWSER_EVERY,
    ROUND_INTERVAL_MAX,
    ROUND_INTERVAL_MIN,
    WEIGHT_CONFIG,
)
from src.detection import detect_questions  # noqa: E402
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
APP_VERSION = "2.0.0"


# ============================================================================
#  配色方案 — 高对比度·黑白极简主题（+ 蓝色点缀）
#  所有文字对比度满足 WCAG AA（≥ 4.5:1），确保 UC 复选框等元素清晰可见
# ============================================================================

COLORS = {
    # ---- 背景层（白→浅灰） ----
    "bg_deep":     "#ffffff",      # 最外层纯白
    "bg":          "#f5f6f8",      # 主背景（浅灰）
    "bg_mid":      "#ffffff",      # 中间层（输入框等）
    "surface":     "#ffffff",      # 卡片背景（纯白）
    "surface_2":   "#eef1f6",      # 悬浮/凹陷背景
    "border_dim":  "#d5d9e0",      # 默认边框
    "border_hi":   "#0066ff",      # 聚焦/高亮边框

    # ---- 主色：单一品牌蓝 ----
    "primary":        "#0066ff",     # 主蓝（按钮/链接）
    "primary_2":      "#0066ff",     # 同主蓝，保证 UI 不花哨
    "primary_hover":  "#1a75ff",     # 悬停蓝
    "primary_active": "#0052cc",     # 按下蓝
    "primary_glow":   "#a8c5ff",     # 发光晕（淡蓝）

    # ---- 功能色（低饱和、可辨） ----
    "success":      "#16a34a",     # 标准绿
    "success_dim":  "#15803d",
    "danger":       "#dc2626",     # 标准红
    "danger_dim":   "#b91c1c",
    "warning":      "#d97706",     # 标准橙
    "warning_dim":  "#b45309",
    "info":         "#0284c7",

    # ---- 文字（纯黑主色，100% 对比度） ----
    "text":         "#111111",     # 主文字（近黑）
    "text_soft":    "#333333",     # 次文字（深灰）
    "text_dim":     "#666666",     # 弱文字
    "text_muted":   "#999999",     # 最弱/禁用文字

    # ---- 表格 ----
    "table_head":   "#f0f2f5",     # 表头（浅灰）
    "table_row_a":  "#ffffff",     # 奇数行（白）
    "table_row_b":  "#f8f9fb",     # 偶数行（浅灰）
    "table_hover":  "#eaf1ff",     # 悬停（淡蓝底）

    # ---- 日志（终端）—— 黑底白字经典 ----
    "term_bg":      "#111111",
    "term_fg":      "#f0f0f0",
}

# 渐变色定义（克制使用，只在进度条/装饰条上）
GRAD_PRIMARY = ("#0052cc", "#0066ff", "#3385ff")   # 深→浅蓝
GRAD_DANGER  = ("#b91c1c", "#dc2626", "#ef4444")   # 红渐变
GRAD_SUCCESS = ("#15803d", "#16a34a", "#22c55e")   # 绿渐变
GRAD_HEADER  = ("#111111", "#222222", "#2a2a2a")   # 标题栏：纯黑渐变灰


# ============================================================================
#  工具函数
# ============================================================================

def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


def _lerp_color(c1: str, c2: str, t: float) -> str:
    """在两个 hex 颜色间线性插值。"""
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    return _rgb_to_hex(
        r1 + (r2 - r1) * t,
        g1 + (g2 - g1) * t,
        b1 + (b2 - b1) * t,
    )


def _draw_vertical_gradient(
    canvas: tk.Canvas,
    x1: int, y1: int, x2: int, y2: int,
    colors: tuple[str, ...],
    tag: str | None = None,
) -> None:
    """在 Canvas 上绘制垂直多色渐变（通过密集线条模拟）。"""
    n = max(1, y2 - y1)
    segments = len(colors) - 1
    steps_per_seg = max(1, n // segments)
    for si in range(segments):
        c_start, c_end = colors[si], colors[si + 1]
        y_start = y1 + si * steps_per_seg
        y_end = y1 + (si + 1) * steps_per_seg if si < segments - 1 else y2
        for i in range(y_end - y_start):
            t = i / max(1, (y_end - y_start) - 1)
            c = _lerp_color(c_start, c_end, t)
            kwargs = {}
            if tag is not None and si == 0 and i == 0:
                kwargs["tags"] = tag
            canvas.create_line(x1, y_start + i, x2, y_start + i, fill=c, width=1)


def _draw_horizontal_gradient(
    canvas: tk.Canvas,
    x1: int, y1: int, x2: int, y2: int,
    colors: tuple[str, ...],
    tag: str | None = None,
) -> None:
    n = max(1, x2 - x1)
    segments = len(colors) - 1
    steps_per_seg = max(1, n // segments)
    for si in range(segments):
        c_start, c_end = colors[si], colors[si + 1]
        x_start = x1 + si * steps_per_seg
        x_end = x1 + (si + 1) * steps_per_seg if si < segments - 1 else x2
        for i in range(x_end - x_start):
            t = i / max(1, (x_end - x_start) - 1)
            c = _lerp_color(c_start, c_end, t)
            kwargs = {}
            if tag is not None and si == 0 and i == 0:
                kwargs["tags"] = tag
            canvas.create_line(x_start + i, y1, x_start + i, y2, fill=c, width=1)


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

        # 统计
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

        self._setup_theme()
        self._build_ui()
        self._start_log_poller()
        self._start_animations()
        self._auto_load_default_config()

    def _auto_load_default_config(self) -> None:
        """启动时如果存在 default_weight_config.json，自动加载。"""
        if not (os.path.exists(DEFAULT_WEIGHT_CONFIG_PATH) and _HAS_CONFIG_IO):
            return
        try:
            self._on_load_config(path=DEFAULT_WEIGHT_CONFIG_PATH)
        except Exception as e:
            self._log(f"[启动] 自动载入默认配置跳过: {type(e).__name__}: {e}", "WARN")

    # ==================================================================
    #  主题系统
    # ==================================================================

    def _setup_theme(self) -> None:
        """配置全局 ttk 样式（黑白极简 + 蓝色点缀）。"""
        style = ttk.Style()
        available = style.theme_names()
        base_theme = "vista" if "vista" in available else ("clam" if "clam" in available else available[0])
        style.theme_use(base_theme)

        # ---- 字体 ----
        self.FONT_HUGE   = ("Microsoft YaHei UI", 20, "bold")
        self.FONT_TITLE  = ("Microsoft YaHei UI", 14, "bold")
        self.FONT_LARGE  = ("Microsoft YaHei UI", 12, "bold")
        self.FONT_NORMAL = ("Microsoft YaHei UI", 10)
        self.FONT_SMALL  = ("Microsoft YaHei UI", 9)
        self.FONT_MONO   = ("Cascadia Code", 9)
        self.FONT_MONO_L = ("Cascadia Code", 10)
        self.FONT_ICON   = ("Segoe UI Emoji", 11)

        # ---- 通用 Frame ----
        style.configure("TFrame",       background=COLORS["bg"])
        style.configure("Deep.TFrame",  background=COLORS["bg_deep"])
        style.configure("Mid.TFrame",   background=COLORS["bg_mid"])
        style.configure("Surface.TFrame", background=COLORS["surface"])

        style.configure("TLabelframe", background=COLORS["bg"])
        style.configure(
            "TLabelframe.Label",
            background=COLORS["bg"],
            foreground=COLORS["primary"],
            font=self.FONT_LARGE,
        )

        # ---- 按钮（ttk 兜底） ----
        style.configure(
            "Ghost.TButton",
            font=self.FONT_NORMAL,
            padding=(14, 5),
            borderwidth=0,
            focuscolor=COLORS["primary"],
            background=COLORS["surface"],
            foreground=COLORS["text"],
        )
        style.map(
            "Ghost.TButton",
            background=[("active", COLORS["surface_2"])],
            foreground=[("active", COLORS["primary"])],
        )

        # ---- Combobox（白底黑字 + 聚焦蓝边） ----
        style.configure(
            "TCombobox",
            fieldbackground="#ffffff",
            background="#ffffff",
            foreground=COLORS["text"],
            arrowcolor=COLORS["primary"],
            bordercolor=COLORS["border_dim"],
            lightcolor=COLORS["border_dim"],
            darkcolor=COLORS["border_dim"],
            padding=(10, 4),
            font=self.FONT_NORMAL,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#ffffff"), ("focus", "#ffffff")],
            foreground=[("readonly", COLORS["text"]), ("disabled", COLORS["text_muted"])],
            selectbackground=[("readonly", COLORS["primary"])],
            selectforeground=[("readonly", "white")],
            bordercolor=[("focus", COLORS["primary"])],
        )

        # ---- 滚动条（灰底 + 蓝滑条） ----
        style.configure(
            "Vertical.TScrollbar",
            background="#cccccc",
            troughcolor=COLORS["surface_2"],
            bordercolor=COLORS["surface_2"],
            arrowcolor=COLORS["primary"],
            gripcount=0,
            relief="flat",
            width=10,
        )
        style.map(
            "Vertical.TScrollbar",
            background=[("active", COLORS["primary"])],
        )
        style.configure(
            "Horizontal.TScrollbar",
            background="#cccccc",
            troughcolor=COLORS["surface_2"],
            bordercolor=COLORS["surface_2"],
            arrowcolor=COLORS["primary"],
            gripcount=0,
            relief="flat",
        )
        style.map(
            "Horizontal.TScrollbar",
            background=[("active", COLORS["primary"])],
        )

        # ---- 进度条 ----
        style.configure(
            "Aurora.Horizontal.TProgressbar",
            thickness=14,
            troughcolor=COLORS["surface_2"],
            background=COLORS["primary"],
            bordercolor=COLORS["surface_2"],
            lightcolor=COLORS["primary"],
            darkcolor=COLORS["primary"],
            relief="flat",
        )

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
        try:
            self._redraw_scanline()
        except Exception:
            pass
        self.root.after(40, self._tick_scanline)

    def _tick_cursor(self) -> None:
        """终端光标闪烁。"""
        self._cursor_blink = not self._cursor_blink
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
    #  通用卡片包装器：细蓝边 + 白底高对比
    # ==================================================================

    def _make_card(self, parent: tk.Misc, title: str, icon: str = "◆",
                   accent: tuple[str, ...] = GRAD_PRIMARY) -> tk.Frame:
        """创建带细边框和标题条的卡片容器（白底高对比），返回内部 body Frame。"""
        outer = tk.Frame(parent, bg=COLORS["bg"])
        outer.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        outer.pack_propagate(True)

        # Canvas 绘制 1.5px 细边（克制的点缀）
        card_c = tk.Canvas(outer, bg=COLORS["bg"], highlightthickness=0, bd=0)
        card_c.pack(fill=tk.BOTH, expand=True)
        card_c.bind("<Configure>", lambda e, cc=card_c, ac=accent:
                    self._on_card_resize(cc, ac))

        body = tk.Frame(card_c, bg=COLORS["surface"])
        body_win = card_c.create_window((1, 1), window=body, anchor="nw")

        def _body_config(_e, cc=card_c, bw=body_win, bd=body):
            w = cc.winfo_width() - 2
            h = cc.winfo_height() - 2
            if w > 0 and h > 0:
                cc.itemconfigure(bw, width=w, height=h)

        card_c.bind("<Configure>", _body_config, add="+")

        # 标题栏
        header = tk.Frame(body, bg=COLORS["surface"], height=38)
        header.pack(fill=tk.X, padx=16, pady=(12, 0))
        header.pack_propagate(False)

        # 4px 彩色竖条（极简装饰）
        title_canvas = tk.Canvas(header, height=22, width=4, bg=COLORS["surface"],
                                 highlightthickness=0, bd=0)
        title_canvas.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        title_canvas.bind("<Configure>",
                          lambda e, c=title_canvas, a=accent: (
                              c.delete("t"),
                              _draw_vertical_gradient(c, 0, 0, 4, 22, a, tag="t"),
                          ))

        tk.Label(
            header, text=f"{icon} ", font=self.FONT_ICON,
            fg=accent[0], bg=COLORS["surface"],
        ).pack(side=tk.LEFT)
        tk.Label(
            header, text=title, font=self.FONT_TITLE,
            fg=COLORS["text"], bg=COLORS["surface"],
        ).pack(side=tk.LEFT)
        # 细灰分隔点
        tk.Label(header, text="···", font=("Consolas", 9),
                 fg=COLORS["text_muted"], bg=COLORS["surface"]).pack(side=tk.LEFT, padx=12)

        inner_body = tk.Frame(body, bg=COLORS["surface"])
        inner_body.pack(fill=tk.BOTH, expand=True, padx=16, pady=(6, 14))
        outer._card_canvas = card_c
        outer._card_body = body
        return inner_body

    def _on_card_resize(self, canvas: tk.Canvas, accent: tuple[str, ...]) -> None:
        canvas.delete("card_border")
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w <= 2 or h <= 2:
            return
        # 极简：灰色描边 + 左上角 15px 蓝色装饰段（克制特效）
        # 灰边
        canvas.create_rectangle(0, 0, w - 1, h - 1,
                                outline=COLORS["border_dim"], width=1, tags="card_border")
        # 左上角蓝段：顶部横段 + 左边竖段
        seg = min(36, max(18, w // 14))
        _draw_horizontal_gradient(
            canvas, 0, 0, seg, 2, accent, tag="card_border",
        )
        _draw_vertical_gradient(
            canvas, 0, 0, 2, seg, accent, tag="card_border",
        )
        # 右下角蓝段
        _draw_horizontal_gradient(
            canvas, w - seg, h - 2, w, h,
            (accent[-1], accent[0]), tag="card_border",
        )
        _draw_vertical_gradient(
            canvas, w - 2, h - seg, w, h,
            (accent[-1], accent[0]), tag="card_border",
        )
        # 外发光：一圈淡蓝（只 1px，克制）
        canvas.create_rectangle(1, 1, w - 2, h - 2,
                                outline=COLORS["primary_glow"], width=1,
                                tags="card_border")

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

    # ----- 设置区：自定义按钮 -----

    def _make_icon_button(self, parent, text: str, accent: str = "primary",
                          command=None) -> tk.Button:
        """创建高对比度按钮（白底黑字 + 功能色背景）。"""
        if accent == "primary":
            bg = COLORS["primary"]
            fg = "white"
            hover_bg = COLORS["primary_hover"]
            active_bg = COLORS["primary_active"]
            hl = COLORS["primary_glow"]
        elif accent == "success":
            bg = COLORS["success_dim"]
            fg = "white"
            hover_bg = COLORS["success"]
            active_bg = COLORS["success_dim"]
            hl = COLORS["success"]
        elif accent == "danger":
            bg = COLORS["danger_dim"]
            fg = "white"
            hover_bg = COLORS["danger"]
            active_bg = COLORS["danger_dim"]
            hl = COLORS["danger"]
        else:  # ghost：白底黑字蓝边
            bg = "#ffffff"
            fg = COLORS["text"]
            hover_bg = COLORS["surface_2"]
            active_bg = "#e3e8f0"
            hl = COLORS["primary_glow"]

        btn = tk.Button(
            parent,
            text=text,
            font=self.FONT_NORMAL,
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground="white",
            relief=tk.FLAT,
            bd=0,
            padx=14,
            pady=7,
            cursor="hand2",
            command=command,
            highlightthickness=2,
            highlightbackground=bg,
            highlightcolor=hl,
            borderwidth=0,
        )

        def _on_enter(_e, b=btn, hb=hover_bg, hlc=hl):
            try:
                b.configure(bg=hb, highlightbackground=hlc)
            except Exception:
                pass

        def _on_leave(_e, b=btn, ob=bg):
            try:
                b.configure(bg=ob, highlightbackground=ob)
            except Exception:
                pass

        btn.bind("<Enter>", _on_enter)
        btn.bind("<Leave>", _on_leave)
        return btn

    def _make_spin_button(self, parent, text: str, kind: str) -> tk.Button:
        bg = COLORS["bg_mid"]
        fg = COLORS["primary"] if kind == "plus" else COLORS["warning_dim"]
        hover = COLORS["primary"] if kind == "plus" else COLORS["warning_dim"]
        btn = tk.Button(
            parent, text=text,
            font=("Cascadia Code", 14, "bold"),
            width=2,
            bg=bg, fg=fg,
            activebackground=hover, activeforeground="white",
            relief=tk.FLAT, bd=0, cursor="hand2",
            highlightthickness=2,
            highlightbackground=COLORS["border_dim"],
            highlightcolor=hover,
        )

        def _ent(_e, b=btn, h=hover):
            try: b.configure(bg=h, fg="white")
            except Exception: pass

        def _lv(_e, b=btn, ob=bg, of=fg):
            try: b.configure(bg=ob, fg=of)
            except Exception: pass

        btn.bind("<Enter>", _ent)
        btn.bind("<Leave>", _lv)
        return btn

    def _make_toggle(self, parent, text: str, var: tk.BooleanVar,
                     enabled: bool = True) -> tk.Checkbutton:
        """**高对比复选框**：白底黑字，确保 UC 模式等文字清晰可见。"""
        chk = tk.Checkbutton(
            parent,
            text=text,
            variable=var,
            onvalue=True,
            offvalue=False,
            bg="#ffffff",                          # 纯白背景（绝对清晰）
            fg="#111111",                          # 纯黑文字（最大对比度）
            selectcolor="#eaf1ff",                 # 勾选后淡蓝（仍可看清黑字）
            activebackground="#ffffff",            # 点击时背景保持白
            activeforeground=COLORS["primary"],    # 点击时文字变蓝
            font=self.FONT_NORMAL,                 # 正号字 (10pt)，不再用小号
            bd=0,
            cursor="hand2",
            state=("normal" if enabled else "disabled"),
            disabledforeground="#888888",          # 禁用文字浅灰
            anchor="w",
            padx=6,
            pady=4,
        )
        return chk

    # ==================================================================
    #  历史记录卡片（V2 新增）
    # ==================================================================

    def _history_get_db(self) -> "SubmissionHistory | None":
        """懒构造 SubmissionHistory。避免没选 SQLite 驱动时崩溃。"""
        if not _HAS_HISTORY or SubmissionHistory is None:
            return None
        try:
            if not os.path.exists(os.path.dirname(DEFAULT_HISTORY_DB_PATH)):
                os.makedirs(os.path.dirname(DEFAULT_HISTORY_DB_PATH),
                            exist_ok=True)
            return SubmissionHistory(DEFAULT_HISTORY_DB_PATH)
        except Exception as e:
            self._log(f"历史记录数据库打开失败: {type(e).__name__}: {e}", "WARN")
            return None

    def _build_history_card(self, parent: tk.Frame) -> None:
        """V2：显示 runs + answers 双表 + 工具栏。"""
        body = self._make_card(parent, "历史记录", icon="📜", accent=GRAD_PRIMARY)
        body.pack(fill=tk.BOTH, expand=True)
        body.grid_rowconfigure(1, weight=1)
        body.grid_rowconfigure(3, weight=1)
        body.grid_columnconfigure(0, weight=1)

        # ---- 顶部工具栏 ----
        tb = tk.Frame(body, bg=COLORS["surface"])
        tb.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.history_summary_var = tk.StringVar(value="—")
        tk.Label(tb, textvariable=self.history_summary_var,
                 font=self.FONT_SMALL, fg=COLORS["primary_2"],
                 bg=COLORS["surface"]).pack(side=tk.LEFT, padx=(10, 10))

        if not _HAS_HISTORY:
            tk.Label(tb, text="⚠ src.history 未加载，历史记录不可用",
                     font=self.FONT_SMALL, fg=COLORS["warning"],
                     bg=COLORS["surface"]).pack(side=tk.RIGHT, padx=10)

        btns = tk.Frame(tb, bg=COLORS["surface"])
        btns.pack(side=tk.RIGHT, padx=(0, 6))
        btn_refresh = self._make_icon_button(
            btns, "🔄 刷新", accent="ghost",
            command=self._history_refresh,
        )
        btn_export = self._make_icon_button(
            btns, "📤 导出 CSV", accent="ghost",
            command=self._history_export_csv,
        )
        btn_purge = self._make_icon_button(
            btns, "🗑 清理 7 天前", accent="danger",
            command=self._history_purge_old,
        )
        btn_refresh.pack(side=tk.LEFT, padx=(0, 6))
        btn_export.pack(side=tk.LEFT, padx=(0, 6))
        btn_purge.pack(side=tk.LEFT)

        # ---- runs 表（上） ----
        runs_frame = tk.Frame(body, bg=COLORS["bg_mid"],
                              highlightthickness=1,
                              highlightbackground=COLORS["border_dim"])
        runs_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        runs_frame.grid_rowconfigure(0, weight=1)
        runs_frame.grid_columnconfigure(0, weight=1)

        run_cols = ("id", "started", "finished", "status",
                    "total", "ok", "fail", "url")
        self.history_runs_tree = ttk.Treeview(
            runs_frame, columns=run_cols, show="headings", height=6,
        )
        head = {"id": ("ID", 55), "started": ("开始", 150),
                "finished": ("结束", 150), "status": ("状态", 62),
                "total": ("总", 42), "ok": ("✓", 42),
                "fail": ("✕", 42), "url": ("URL", 280)}
        for col, (txt, w) in head.items():
            self.history_runs_tree.heading(col, text=txt)
            self.history_runs_tree.column(col, width=w, anchor="w",
                                          stretch=(col == "url"))
        run_scroll = ttk.Scrollbar(runs_frame, orient="vertical",
                                   command=self.history_runs_tree.yview)
        self.history_runs_tree.configure(yscrollcommand=run_scroll.set)
        self.history_runs_tree.grid(row=0, column=0, sticky="nsew")
        run_scroll.grid(row=0, column=1, sticky="ns")
        self.history_runs_tree.bind("<<TreeviewSelect>>",
                                    self._history_select_run)

        # ---- 标签：选中 run 的 answers 表 ----
        ans_header = tk.Label(body, text="🔍  答题明细 （点击上方 Run 查看）",
                              font=("Microsoft YaHei UI", 10, "bold"),
                              fg=COLORS["text_soft"],
                              bg=COLORS["surface"], anchor="w",
                              padx=10, pady=4)
        ans_header.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        self.history_answer_head_var = tk.StringVar(value="（未选中运行记录）")
        tk.Label(body, textvariable=self.history_answer_head_var,
                 font=self.FONT_SMALL, fg=COLORS["text_dim"],
                 bg=COLORS["surface"], anchor="e", padx=10
                 ).grid(row=2, column=0, sticky="e")

        ans_frame = tk.Frame(body, bg=COLORS["bg_mid"],
                             highlightthickness=1,
                             highlightbackground=COLORS["border_dim"])
        ans_frame.grid(row=3, column=0, sticky="nsew")
        ans_frame.grid_rowconfigure(0, weight=1)
        ans_frame.grid_columnconfigure(0, weight=1)

        ans_cols = ("q", "type", "selected", "text_ans", "elapsed", "time")
        self.history_ans_tree = ttk.Treeview(
            ans_frame, columns=ans_cols, show="headings", height=10,
        )
        an_head = {"q": ("Q", 50), "type": ("题型", 75),
                   "selected": ("选项", 260), "text_ans": ("文本答案", 260),
                   "elapsed": ("耗时ms", 85), "time": ("记录时间", 145)}
        for col, (txt, w) in an_head.items():
            self.history_ans_tree.heading(col, text=txt)
            self.history_ans_tree.column(col, width=w, anchor="w",
                                         stretch=(col in ("selected",
                                                          "text_ans")))
        ans_scroll = ttk.Scrollbar(ans_frame, orient="vertical",
                                   command=self.history_ans_tree.yview)
        self.history_ans_tree.configure(yscrollcommand=ans_scroll.set)
        self.history_ans_tree.grid(row=0, column=0, sticky="nsew")
        ans_scroll.grid(row=0, column=1, sticky="ns")

        # 启动后 300ms 刷新一次（避免日志区还没 ready）
        self.root.after(300, self._history_refresh)

    def _history_refresh(self) -> None:
        db = self._history_get_db()
        tree = getattr(self, "history_runs_tree", None)
        if db is None or tree is None:
            return
        try:
            runs = db.query_runs(limit=200)
            for iid in tree.get_children():
                tree.delete(iid)
            ok_cnt = 0
            fail_cnt = 0
            total_cnt = 0
            for r in runs:
                rid = r["id"]
                status = str(r.get("status") or "-")
                started = str(r.get("started_at") or "")[:19]
                finished = str(r.get("finished_at") or "")[:19]
                tot = r.get("total_submissions", 0) or 0
                ok = r.get("ok_count", 0) or 0
                fail = r.get("fail_count", 0) or 0
                url = str(r.get("survey_url") or "")[:180]
                total_cnt += tot
                ok_cnt += ok
                fail_cnt += fail
                tag = ("status_ok",) if status in ("done", "success",
                                                   "finished") else \
                      (("status_fail",) if status in ("fail", "error",
                                                       "stopped") else ())
                tree.insert("", tk.END, iid=str(rid),
                            values=(rid, started, finished, status,
                                    tot, ok, fail, url), tags=tag)
            # 颜色样式
            tree.tag_configure("status_ok",
                               background="#eaffef", foreground="#0b5c1e")
            tree.tag_configure("status_fail",
                               background="#fff1f0", foreground="#8a1e1e")
            n_runs = len(runs)
            self.history_summary_var.set(
                f"共 {n_runs} 次运行 · 累计提交 {total_cnt} "
                f"(✓ {ok_cnt} / ✕ {fail_cnt})"
            )
        except Exception as e:
            self._log(f"刷新历史记录失败: {type(e).__name__}: {e}", "WARN")

    def _history_select_run(self, _e=None) -> None:
        db = self._history_get_db()
        tree_runs = getattr(self, "history_runs_tree", None)
        tree_ans = getattr(self, "history_ans_tree", None)
        if db is None or tree_runs is None or tree_ans is None:
            return
        sel = tree_runs.selection()
        if not sel:
            return
        run_id = int(sel[0])
        for iid in tree_ans.get_children():
            tree_ans.delete(iid)
        try:
            answers = db.query_answers(run_id=run_id)
            for a in answers:
                q = a.get("q_number") or "-"
                qt = a.get("q_type") or "-"
                sel_raw = a.get("options_selected")
                if isinstance(sel_raw, str) and sel_raw:
                    sel_s = sel_raw
                elif isinstance(sel_raw, (list, tuple)):
                    sel_s = ",".join(str(x) for x in sel_raw)
                else:
                    sel_s = ""
                ta = a.get("text_answer") or ""
                ems = a.get("elapsed_ms") or ""
                rec_at = str(a.get("recorded_at") or "")[:19]
                tree_ans.insert("", tk.END,
                                values=(q, qt, sel_s, ta, ems, rec_at))
            self.history_answer_head_var.set(
                f"Run #{run_id} · 答题数 {len(answers)}"
            )
        except Exception as e:
            self._log(f"读取答题明细失败: {type(e).__name__}: {e}", "WARN")

    def _history_export_csv(self) -> None:
        db = self._history_get_db()
        if db is None:
            self._log("未加载 src.history，无法导出", "WARN")
            return
        path = filedialog.asksaveasfilename(
            title="导出历史记录 CSV",
            defaultextension=".csv",
            initialfile="history_runs.csv",
            filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            runs = db.query_runs(limit=10000)
            all_answers: list = []
            for r in runs:
                rid = int(r["id"])
                try:
                    all_answers.extend(db.query_answers(run_id=rid))
                except Exception:
                    pass
            # runs sheet 写第一个表，answers 写第二个？CSV 无 sheet，分两个文件。
            # 简单起见：写 runs；单独写 *_answers.csv
            runs_path = path
            base, ext = os.path.splitext(path)
            ans_path = f"{base}_answers{ext}"
            import csv
            with open(runs_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["id", "started_at", "finished_at", "status",
                            "survey_url", "total", "ok", "fail", "note"])
                for r in runs:
                    w.writerow([r.get("id"), r.get("started_at"),
                                r.get("finished_at"), r.get("status"),
                                r.get("survey_url"),
                                r.get("total_submissions"),
                                r.get("ok_count"), r.get("fail_count"),
                                r.get("note")])
            with open(ans_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["run_id", "submission_index",
                            "q_number", "q_type",
                            "options_selected", "text_answer",
                            "elapsed_ms", "recorded_at"])
                for a in all_answers:
                    sel_raw = a.get("options_selected")
                    if isinstance(sel_raw, (list, tuple)):
                        sel_s = ",".join(str(x) for x in sel_raw)
                    else:
                        sel_s = str(sel_raw or "")
                    w.writerow([a.get("run_id"),
                                a.get("submission_index"),
                                a.get("q_number"), a.get("q_type"),
                                sel_s, a.get("text_answer") or "",
                                a.get("elapsed_ms"),
                                a.get("recorded_at")])
            self._log(f"✓ 已导出 runs → {os.path.basename(runs_path)}", "OK")
            self._log(f"✓ 已导出 answers → {os.path.basename(ans_path)}", "OK")
        except Exception as e:
            self._log(f"导出 CSV 失败: {type(e).__name__}: {e}", "FAIL")

    def _history_purge_old(self) -> None:
        db = self._history_get_db()
        if db is None:
            self._log("未加载 src.history，无法清理", "WARN")
            return
        try:
            removed = db.purge_old(days=7)
            self._log(f"已清理 {removed} 条 7 天前的运行记录", "OK")
            self._history_refresh()
        except Exception as e:
            self._log(f"清理旧历史失败: {type(e).__name__}: {e}", "FAIL")

    # ==================================================================
    #  权重表格卡片
    # ==================================================================

    def _build_weight_table_card(self, parent: tk.Frame) -> None:
        body = self._make_card(parent, "权重配置", icon="📋", accent=GRAD_SUCCESS)
        body.pack(fill=tk.BOTH, expand=True)
        body.grid_rowconfigure(1, weight=1)
        body.grid_columnconfigure(0, weight=1)

        # 提示行
        tip = tk.Frame(body, bg=COLORS["surface"])
        tip.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tk.Label(tip, text="💡", font=self.FONT_ICON,
                 bg=COLORS["surface"], fg=COLORS["warning"]).pack(side=tk.LEFT)
        tk.Label(tip,
                 text="每题一行，权重用英文逗号分隔，例如  0.3 , 0.5 , 0.2   （留空则使用等权重随机）",
                 font=self.FONT_SMALL, fg=COLORS["text_dim"],
                 bg=COLORS["surface"], justify=tk.LEFT, wraplength=500).pack(side=tk.LEFT, padx=(4, 0))

        # 表格容器 + 滚动
        canvas = tk.Canvas(body, bg=COLORS["surface"],
                           highlightthickness=0, relief=tk.FLAT)
        canvas.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=canvas.yview,
                                  style="Vertical.TScrollbar")
        scrollbar.grid(row=1, column=1, sticky="ns")

        self.table_frame = tk.Frame(canvas, bg=COLORS["surface"])
        self.table_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        self.table_win = canvas.create_window((0, 0), window=self.table_frame, anchor="nw")

        def _resize(_e):
            canvas.itemconfigure(self.table_win, width=_e.width)

        canvas.bind("<Configure>", _resize)
        canvas.configure(yscrollcommand=scrollbar.set)

        def _wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        self.weight_canvas = canvas
        self._draw_table_header()

    def _draw_table_header(self) -> None:
        for w in self.table_frame.winfo_children():
            w.destroy()

        cols = [("题号", 1), ("类型", 2), ("选项/空数", 1), ("权重/答案文本（英文逗号分隔）", 5)]
        self.table_frame.grid_columnconfigure(0, weight=1)
        self.table_frame.grid_columnconfigure(1, weight=2)
        self.table_frame.grid_columnconfigure(2, weight=1)
        self.table_frame.grid_columnconfigure(3, weight=5)

        header_wrapper = tk.Frame(self.table_frame, bg=COLORS["surface"])
        header_wrapper.grid(row=0, column=0, columnspan=4, sticky="ew")
        for i, (text, _w) in enumerate(cols):
            header_wrapper.grid_columnconfigure(i, weight=_w)
            cell = tk.Frame(header_wrapper, bg=COLORS["table_head"], height=32)
            cell.grid(row=0, column=i, sticky="nsew", padx=(0, 1), pady=(0, 1))
            cell.pack_propagate(False)
            tk.Label(
                cell, text=text,
                font=("Microsoft YaHei UI", 9, "bold"),
                bg=COLORS["table_head"],
                fg="#004099",   # 深蓝，浅灰表头上 CR > 5.5:1，满足 WCAG AA
                anchor=tk.CENTER,
            ).pack(expand=True)

        self._show_table_placeholder()

    def _show_table_placeholder(self) -> None:
        ph = tk.Frame(self.table_frame, bg=COLORS["surface"])
        ph.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=36)

        # 大号图标 + 提示
        tk.Label(ph, text="📡", font=("Segoe UI Emoji", 36),
                 bg=COLORS["surface"], fg=COLORS["text_muted"]).pack()
        tk.Label(ph,
                 text="点击「探测题目」自动识别问卷结构",
                 font=self.FONT_NORMAL,
                 bg=COLORS["surface"], fg=COLORS["text_dim"]).pack(pady=(6, 2))
        tk.Label(ph,
                 text="连接浏览器 → 分析单选/多选 → 生成权重编辑表格",
                 font=self.FONT_SMALL,
                 bg=COLORS["surface"], fg=COLORS["text_muted"]).pack()

    def _populate_weight_table(self, questions: list[dict]) -> None:
        for w in self.table_frame.winfo_children():
            w.destroy()
        self.weight_entries.clear()
        self.questions = questions

        cols = [("题号", 1), ("类型", 2), ("选项/空数", 1), ("权重/答案文本（英文逗号分隔）", 5)]
        self.table_frame.grid_columnconfigure(0, weight=1)
        self.table_frame.grid_columnconfigure(1, weight=2)
        self.table_frame.grid_columnconfigure(2, weight=1)
        self.table_frame.grid_columnconfigure(3, weight=5)

        header_wrapper = tk.Frame(self.table_frame, bg=COLORS["surface"])
        header_wrapper.grid(row=0, column=0, columnspan=4, sticky="ew")
        for i, (text, _w) in enumerate(cols):
            header_wrapper.grid_columnconfigure(i, weight=_w)
            cell = tk.Frame(header_wrapper, bg=COLORS["table_head"], height=32)
            cell.grid(row=0, column=i, sticky="nsew", padx=(0, 1), pady=(0, 1))
            cell.pack_propagate(False)
            tk.Label(
                cell, text=text,
                font=("Microsoft YaHei UI", 9, "bold"),
                bg=COLORS["table_head"],
                fg="#004099",   # 深蓝，浅灰表头上 CR > 5.5:1，满足 WCAG AA
                anchor=tk.CENTER,
            ).pack(expand=True)

        for i, q in enumerate(questions):
            row = i + 1
            qi = q["q"]
            qtype = q["type"]
            stripe_bg = COLORS["table_row_a"] if i % 2 == 0 else COLORS["table_row_b"]

            n_opts = len(q["choices"])
            default_weights = ",".join([f"{1.0 / n_opts:.4f}" for _ in range(n_opts)])
            existing = WEIGHT_CONFIG.get(qi)
            if existing and "weights" in existing:
                default_weights = ",".join([f"{w:.4f}" for w in existing["weights"]])

            # --- 题号 ---
            qid_cell = tk.Frame(self.table_frame, bg=stripe_bg, height=34)
            qid_cell.grid(row=row, column=0, sticky="nsew", padx=(0, 1), pady=(0, 1))
            qid_cell.pack_propagate(False)
            tk.Label(
                qid_cell,
                text=f"Q{qi}",
                font=("Cascadia Code", 10, "bold"),
                bg=stripe_bg, fg=COLORS["primary_2"],
                anchor=tk.CENTER,
            ).pack(expand=True)

            # --- 类型 Badge（胶囊） ---
            type_cell = tk.Frame(self.table_frame, bg=stripe_bg, height=34)
            type_cell.grid(row=row, column=1, sticky="nsew", padx=(0, 1), pady=(0, 1))
            type_cell.pack_propagate(False)

            qtype_badge_map = {
                "single":       (COLORS["primary"],             "单选"),
                "radio":        (COLORS["primary"],             "单选"),
                "multi":        (COLORS["success_dim"],         "多选"),
                "checkbox":     (COLORS["success_dim"],         "多选"),
                "dropdown":     (COLORS["warning_dim"],         "下拉"),
                "scale":        (COLORS["primary_2"],           "量表"),
                "rating":       (COLORS["primary_2"],           "量表"),
                "text":         ("#64748b",                     "填空"),
                "input":        ("#64748b",                     "填空"),
                "textarea":     ("#64748b",                     "填空"),
                "fillblank":    ("#64748b",                     "填空"),
                "matrix":       (COLORS["danger_dim"],          "矩阵"),
                "matrix_single":(COLORS["danger_dim"],          "矩阵"),
            }
            badge_c, badge_text = qtype_badge_map.get(
                qtype, (COLORS["primary"], qtype[:4].upper())
            )
            badge_fg = "white"

            badge_canvas = tk.Canvas(type_cell, height=22, width=78,
                                     bg=stripe_bg, highlightthickness=0, bd=0)
            badge_canvas.pack(expand=True, padx=(0, 2))
            # 画圆角胶囊
            def _draw_badge(_e=None, bc=badge_canvas, col=badge_c, fgcol=badge_fg, txt=badge_text):
                bc.delete("all")
                w, h = bc.winfo_width(), bc.winfo_height()
                if w <= 1:
                    w, h = 78, 22
                r = h // 2 - 1
                # 发光
                for ii in range(3, 0, -1):
                    t = ii / 3
                    gcol = _lerp_color(col, stripe_bg, 0.5 + t * 0.4)
                    bc.create_rectangle(r - ii, 1 - ii, w - r + ii, h - 1 + ii,
                                        outline=gcol, width=1)
                bc.create_rectangle(r, 1, w - r, h - 1, fill=col, outline="")
                bc.create_oval(0, 1, 2 * r, h - 1, fill=col, outline="")
                bc.create_oval(w - 2 * r, 1, w, h - 1, fill=col, outline="")
                bc.create_text(w / 2, h / 2 + 1, text=txt,
                               font=("Microsoft YaHei UI", 8, "bold"), fill=fgcol)
            badge_canvas.bind("<Configure>", _draw_badge)
            self.root.after(10, _draw_badge)

            # --- 填空：附加字段名小 badge（name/phone/email 等）---
            if qtype in ("text", "input", "textarea", "fillblank"):
                fld = q.get("field")
                if fld:
                    field_map = {
                        "name": "姓名", "phone": "手机", "email": "邮箱",
                        "address": "地址", "age": "年龄", "company": "公司",
                    }
                    field_label = field_map.get(fld, str(fld).upper()[:4])
                    mini = tk.Label(
                        type_cell, text=field_label,
                        font=("Microsoft YaHei UI", 7, "bold"),
                        bg="#e8ecf3", fg="#334155",
                        padx=5, pady=1,
                    )
                    mini.pack(side=tk.RIGHT, padx=(0, 4))

            # --- 选项 / 规模描述 ---
            if qtype in ("single", "multi", "radio", "checkbox", "dropdown"):
                n_opts = len(q.get("choices", []))
                default_weights = ",".join(
                    [f"{1.0 / n_opts:.4f}" for _ in range(n_opts)]
                ) if n_opts > 0 else ""
                n_label = f"{n_opts}"
            elif qtype in ("scale", "rating"):
                n_opts = int(q.get("scale", 5))
                smin = int(q.get("scale_min", 1))
                default_weights = ",".join(
                    ["1"] * n_opts  # 用户可自己调节；初始等权重
                )
                n_label = f"{smin}~{n_opts}"
            elif qtype in ("text", "input", "textarea", "fillblank"):
                # 填空：如果检测阶段给了 options 就用 options，否则留空（让用户写候选文本）
                opts_hint = q.get("options") or []
                if opts_hint:
                    default_weights = ",".join([str(x) for x in opts_hint])
                else:
                    default_weights = ""  # 空白：用户可自定义候选，逗号分隔
                fld = q.get("field")
                if fld == "name":
                    n_label = "姓名字段"
                elif fld in ("phone", "mobile", "tel"):
                    n_label = "手机字段"
                elif fld == "email":
                    n_label = "邮箱字段"
                elif fld in ("address", "addr"):
                    n_label = "地址字段"
                elif fld == "age":
                    n_label = "年龄字段"
                elif fld in ("company", "org"):
                    n_label = "公司字段"
                else:
                    n_label = "自由文本"
            elif qtype in ("matrix_single", "matrix"):
                rs = q.get("rows", [])
                cs = q.get("cols", [])
                default_weights = ""  # 矩阵留空，用户按 "row:w1,w2,w3..." 多行格式写
                n_label = f"{len(rs)}行 × {len(cs)}列"
            else:
                n_opts = len(q.get("choices", []))
                default_weights = ",".join(["1"] * n_opts) if n_opts > 0 else ""
                n_label = f"{n_opts}"

            # 如果有全局历史配置，覆盖默认值（V1 逻辑保留）
            existing = WEIGHT_CONFIG.get(qi)
            if existing:
                if "weights" in existing:
                    default_weights = ",".join(
                        [f"{w:.4f}" if isinstance(w, (int, float)) else str(w)
                         for w in existing["weights"]]
                    )
                elif "options" in existing and isinstance(existing["options"], list):
                    default_weights = ",".join([str(x) for x in existing["options"]])
                # 矩阵特殊：row_weights 也转成可编辑字符串
                if "row_weights" in existing and isinstance(existing["row_weights"], dict):
                    lines = []
                    for rk in sorted(existing["row_weights"].keys(),
                                     key=lambda x: int(x) if str(x).isdigit() else str(x)):
                        wlst = existing["row_weights"][rk]
                        lines.append(f"{rk}:{','.join(str(x) for x in wlst)}")
                    if lines:
                        default_weights = " | ".join(lines)

            # --- 选项数 / 规模描述列 ---
            ncell = tk.Frame(self.table_frame, bg=stripe_bg, height=34)
            ncell.grid(row=row, column=2, sticky="nsew", padx=(0, 1), pady=(0, 1))
            ncell.pack_propagate(False)
            tk.Label(
                ncell, text=n_label,
                font=("Cascadia Code", 10),
                bg=stripe_bg, fg=COLORS["text_soft"],
                anchor=tk.CENTER,
            ).pack(expand=True)

            # --- 权重输入 ---
            ecell = tk.Frame(self.table_frame, bg=stripe_bg, height=34)
            ecell.grid(row=row, column=3, sticky="nsew", padx=(0, 1), pady=(0, 1))
            ecell.pack_propagate(False)
            entry_var = tk.StringVar(value=default_weights)
            entry = tk.Entry(
                ecell,
                textvariable=entry_var,
                font=("Cascadia Code", 9),
                bg=COLORS["bg_mid"],
                fg=COLORS["text"],
                insertbackground=COLORS["primary_2"],
                relief=tk.FLAT,
                bd=0,
                highlightthickness=1,
                highlightbackground=COLORS["border_dim"],
                highlightcolor=COLORS["primary"],
            )
            entry.pack(fill=tk.X, padx=6, pady=4, ipady=3)
            self.weight_entries[qi] = entry_var

            # --- 行悬停效果 ---
            all_cells = [qid_cell, type_cell, ncell, ecell]

            def _enter(_e, cells=all_cells, orig=stripe_bg):
                for c in cells:
                    try: c.configure(bg=COLORS["table_hover"])
                    except Exception: pass
                # 子组件也要跟着改 bg
                for cell in cells:
                    for child in cell.winfo_children():
                        try:
                            if not isinstance(child, (tk.Canvas,)):
                                child.configure(bg=COLORS["table_hover"])
                            else:
                                child.configure(bg=COLORS["table_hover"])
                        except Exception:
                            pass

            def _leave(_e, cells=all_cells, orig=stripe_bg):
                for c in cells:
                    try: c.configure(bg=orig)
                    except Exception: pass
                for cell in cells:
                    for child in cell.winfo_children():
                        try:
                            if not isinstance(child, tk.Canvas):
                                child.configure(bg=orig)
                            else:
                                child.configure(bg=orig)
                        except Exception:
                            pass

            for c in all_cells:
                c.bind("<Enter>", _enter)
                c.bind("<Leave>", _leave)
                for child in c.winfo_children():
                    child.bind("<Enter>", _enter)
                    child.bind("<Leave>", _leave)

    # ==================================================================
    #  日志卡片（赛博朋克终端）
    # ==================================================================

    def _build_log_area_card(self, parent: tk.Frame) -> None:
        body = self._make_card(parent, "运行日志", icon="📟", accent=GRAD_DANGER)
        body.pack(fill=tk.BOTH, expand=True)
        body.grid_rowconfigure(1, weight=1)
        body.grid_columnconfigure(0, weight=1)

        # 顶部工具栏：终端标题 + 控制按钮
        tb = tk.Frame(body, bg=COLORS["surface"])
        tb.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        # 终端装饰：3 个小圆点（Mac 风格）
        dots = tk.Frame(tb, bg=COLORS["surface"])
        dots.pack(side=tk.LEFT)
        for col in [COLORS["danger"], COLORS["warning"], COLORS["success"]]:
            d = tk.Canvas(dots, width=12, height=12, bg=COLORS["surface"],
                          highlightthickness=0, bd=0)
            d.pack(side=tk.LEFT, padx=(0, 5))
            d.bind("<Configure>",
                   lambda e, c=d, col=col: (c.delete("all"),
                                            c.create_oval(1, 1, 11, 11, fill=col, outline=""),
                                            c.create_oval(3, 3, 5, 5, fill="white", outline="",
                                                          stipple="gray25")))

        tk.Label(tb, text="  terminal@aurora  ~  zsh",
                 font=("Cascadia Code", 9),
                 bg=COLORS["surface"], fg=COLORS["text_dim"]).pack(side=tk.LEFT, padx=(6, 0))

        # 日志行数统计
        self.log_lines_var = tk.StringVar(value="0 lines")
        tk.Label(tb, textvariable=self.log_lines_var,
                 font=("Cascadia Code", 8),
                 bg=COLORS["surface"], fg=COLORS["primary_2"]).pack(side=tk.RIGHT)

        tk.Label(tb, text="●", font=("Cascadia Code", 6),
                 bg=COLORS["surface"], fg=COLORS["primary"]).pack(side=tk.RIGHT, padx=(0, 8))

        # 扫描线 + 日志容器
        log_container = tk.Frame(body, bg=COLORS["term_bg"],
                                 highlightthickness=2,
                                 highlightbackground=COLORS["border_dim"])
        log_container.grid(row=1, column=0, sticky="nsew")
        log_container.grid_rowconfigure(1, weight=1)
        log_container.grid_columnconfigure(1, weight=1)

        # 顶部扫描线 Canvas
        self.scan_canvas = tk.Canvas(log_container, height=2, bg=COLORS["term_bg"],
                                     highlightthickness=0, bd=0)
        self.scan_canvas.grid(row=0, column=0, columnspan=2, sticky="ew")

        # 行号列
        self.log_lineno = tk.Text(
            log_container,
            width=5,
            font=("Cascadia Code", 9),
            bg=COLORS["term_bg"],
            fg=COLORS["text_muted"],
            state=tk.DISABLED,
            relief=tk.FLAT,
            bd=0,
            padx=8,
            pady=8,
            takefocus=0,
            highlightthickness=0,
            selectbackground=COLORS["term_bg"],
        )
        self.log_lineno.grid(row=1, column=0, sticky="ns")

        # 日志主体
        self.log_text = scrolledtext.ScrolledText(
            log_container,
            wrap=tk.WORD,
            font=("Cascadia Code", 9),
            state=tk.DISABLED,
            bg=COLORS["term_bg"],
            fg=COLORS["term_fg"],
            insertbackground=COLORS["success"],
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=8,
            highlightthickness=0,
            yscrollcommand=self._sync_scroll,
        )
        self.log_text.grid(row=1, column=1, sticky="nsew")

        # 同步滚动
        def _on_yview(*args):
            self.log_text.yview_moveto(args[0])
            self.log_lineno.yview_moveto(args[0])

        self.log_text.config(yscrollcommand=_on_yview)
        self.log_text["yscrollcommand"] = _on_yview  # 再次同步

        # 标签配色（Tokyo Night 风）
        self.log_tags = {
            "INFO":    ("#9aa5ce",),
            "OK":      ("#9ece6a",),
            "FAIL":    ("#f7768e",),
            "WARN":    ("#e0af68",),
            "HEADER":  ("#7aa2f7",),
            "TIME":    ("#565f89",),
            "CURSOR":  ("#00ffa3",),
            "PROMPT":  ("#bb9af7",),
        }
        for tag, (fg,) in self.log_tags.items():
            self.log_text.tag_config(tag, foreground=fg)
        self.log_text.tag_config("scan_hl", background="#151a2e")
        self.log_lineno_count = 0

        # 光标闪烁
        self._cursor_tag_configured = False

    def _sync_scroll(self, *args):
        """同步行号和日志的滚动。"""
        self.log_lineno.yview_moveto(args[0])

    def _redraw_scanline(self) -> None:
        """重绘扫描线（顶部 2px Canvas）。"""
        if not hasattr(self, "scan_canvas"):
            return
        c = self.scan_canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1 or h <= 1:
            return
        # 主扫描线（渐变）
        t = self._scan_phase
        line_x = int(w * t)
        # 整条基线
        _draw_horizontal_gradient(c, 0, 0, w, h,
                                  (COLORS["term_bg"], COLORS["primary"], COLORS["term_bg"]))
        # 扫描光点
        for i in range(20, 0, -1):
            col = _lerp_color(COLORS["primary_2"], COLORS["term_bg"], i / 20)
            x1 = max(0, line_x - i * 4)
            x2 = min(w, line_x + i * 4)
            c.create_line(x1, 0, x2, 0, fill=col, width=2)

    def _refresh_cursor_tag(self) -> None:
        if not hasattr(self, "log_text"):
            return
        # 最后一行加闪烁光标提示
        try:
            self.log_text.tag_configure(
                "CURSOR_BLINK",
                background=COLORS["success"] if self._cursor_blink else "",
            )
        except Exception:
            pass

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
        """渐变发光数字统计 Badge。"""
        wrap = tk.Frame(parent, bg=COLORS["surface"])

        # 外层 Canvas 做渐变边框发光
        card_c = tk.Canvas(wrap, width=108, height=60,
                           bg=COLORS["surface"], highlightthickness=0, bd=0)
        card_c.pack()

        body = tk.Frame(card_c, bg=COLORS["surface_2"])
        win_id = card_c.create_window((3, 3), window=body, anchor="nw")

        def _cfg(_e=None, c=card_c, wid=win_id, bd=body, g=grad, ic=icon, lbl=label):
            c.delete("glow")
            w, h = c.winfo_width(), c.winfo_height()
            if w <= 6 or h <= 6:
                return
            c.itemconfigure(wid, width=w - 6, height=h - 6)
            # 边框渐变
            _draw_horizontal_gradient(c, 1, 1, w - 1, 2, g, tag="glow")
            _draw_horizontal_gradient(c, 1, h - 2, w - 1, h - 1,
                                      (g[-1], g[0]), tag="glow")
            _draw_vertical_gradient(c, 1, 1, 2, h - 1,
                                    (g[0], g[-1]), tag="glow")
            _draw_vertical_gradient(c, w - 2, 1, w - 1, h - 1,
                                    (g[0], g[-1]), tag="glow")
            # 光晕
            for i in range(3, 0, -1):
                col = _lerp_color(g[1], COLORS["surface"], 0.55 + i * 0.12)
                c.create_rectangle(1 - i, 1 - i, w - 1 + i, h - 1 + i,
                                   outline=col, width=1, tags="glow")

        card_c.bind("<Configure>", _cfg)
        self.root.after(10, _cfg)

        # 内部布局
        body.grid_rowconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)
        body.grid_columnconfigure(0, weight=1)

        head = tk.Frame(body, bg=COLORS["surface_2"])
        head.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 0))
        tk.Label(head, text=icon,
                 font=("Segoe UI Emoji", 9, "bold"),
                 bg=COLORS["surface_2"], fg=grad[0]).pack(side=tk.LEFT)
        tk.Label(head, text=f" {label}",
                 font=self.FONT_SMALL,
                 bg=COLORS["surface_2"], fg=COLORS["text_soft"]).pack(side=tk.LEFT)

        num_wrap = tk.Frame(body, bg=COLORS["surface_2"])
        num_wrap.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6))
        tk.Label(num_wrap, textvariable=var,
                 font=("Cascadia Code", 18, "bold"),
                 bg=COLORS["surface_2"], fg=grad[1]).pack(side=tk.LEFT)
        return wrap

    # ==================================================================
    #  日志系统
    # ==================================================================

    def _log(self, message: str, tag: str = "INFO") -> None:
        self.log_queue.put((message, tag))

    def _start_log_poller(self) -> None:
        self._drain_log_queue()
        self.root.after(80, self._start_log_poller)

    def _drain_log_queue(self) -> None:
        while True:
            try:
                message, tag = self.log_queue.get_nowait()
                self._append_log(message, tag)
            except queue.Empty:
                break

    def _append_log(self, message: str, tag: str) -> None:
        # 确保 tag 有效
        if tag not in self.log_tags:
            tag = "INFO"
        self.log_text.configure(state=tk.NORMAL)
        self.log_lineno.configure(state=tk.NORMAL)

        ts = time.strftime("%H:%M:%S")

        # 写行号
        self.log_lineno_count += 1
        self.log_lineno.insert(tk.END, f"{self.log_lineno_count:>4}\n")

        # 写日志：[prompt] [time] [tag-colored message]
        self.log_text.insert(tk.END, "❯ ", "PROMPT")
        self.log_text.insert(tk.END, f"{ts}  ", "TIME")
        self.log_text.insert(tk.END, f"{message}\n", tag)

        # 同步滚动
        self.log_text.see(tk.END)
        self.log_lineno.see(tk.END)

        self.log_text.configure(state=tk.DISABLED)
        self.log_lineno.configure(state=tk.DISABLED)
        self.log_lines_var.set(f"{self.log_lineno_count} lines")

    # ==================================================================
    #  权重配置导出
    # ==================================================================

    def _build_weight_config(self) -> dict:
        """将 GUI 表格中用户编辑的内容导出为 V2 cfg dict（键为 int 题号）。

        结构与 src/config_io.save_weight_config 要求的 cfg 完全一致。
        """
        config: dict = {}
        for q in self.questions:
            qi = q["q"]
            qtype = str(q.get("type", "single")).lower()
            entry_var = self.weight_entries.get(qi)
            raw = entry_var.get().strip() if entry_var else ""

            # single / multi / radio / checkbox / dropdown：权重用浮点数组
            if qtype in ("single", "radio", "multi", "checkbox", "dropdown"):
                if not raw:
                    continue
                try:
                    weights = [float(x.strip()) for x in raw.split(",") if x.strip()]
                except ValueError:
                    self._log(f"Q{qi} 权重格式错误，已跳过：{raw}", "WARN")
                    continue
                expected = len(q.get("choices", []))
                if qtype in ("single", "radio", "dropdown") and expected == 0:
                    # 下拉选项数可能未知时，接受任意长度
                    pass
                elif expected > 0 and len(weights) != expected:
                    self._log(
                        f"Q{qi} 权重数({len(weights)}) != 选项数({expected}), 已跳过",
                        "WARN",
                    )
                    continue
                cfg: dict = {"type": qtype, "weights": weights}
                config[qi] = cfg
                continue

            # scale / rating：权重按分值 (1..N)
            if qtype in ("scale", "rating"):
                scale_max = int(q.get("scale", 5))
                if raw:
                    # 允许权重：也允许直接写一个默认分值（纯整数）
                    if raw.isdigit():
                        v = int(raw)
                        weights = [0.0] * scale_max
                        if 1 <= v <= scale_max:
                            weights[v - 1] = 1.0
                    else:
                        try:
                            weights = [float(x.strip()) for x in raw.split(",")
                                       if x.strip()]
                        except ValueError:
                            self._log(f"Q{qi} 量表权重格式错误，已跳过：{raw}", "WARN")
                            continue
                        if len(weights) != scale_max:
                            self._log(
                                f"Q{qi} 量表权重数({len(weights)}) != 级数({scale_max}), "
                                "已按现有长度裁剪/补 0",
                                "WARN",
                            )
                            if len(weights) < scale_max:
                                weights += [0.0] * (scale_max - len(weights))
                            else:
                                weights = weights[:scale_max]
                else:
                    weights = None
                cfg = {"type": qtype, "scale": scale_max}
                if weights is not None:
                    cfg["weights"] = weights
                smin = q.get("scale_min")
                if smin:
                    cfg["scale_min"] = int(smin)
                config[qi] = cfg
                continue

            # text / input / textarea / fillblank：选项是候选文本（或留空走自动生成）
            if qtype in ("text", "input", "textarea", "fillblank"):
                cfg = {"type": qtype}
                fld = q.get("field")
                if fld:
                    cfg["field"] = fld
                if raw:
                    # 候选文本：逗号分隔；为防用户要写英文逗号，用最后一次非空 split
                    options = [s.strip() for s in raw.split(",") if s.strip()]
                    if options:
                        # 尝试解析：如果是纯数字也可能是用户想写权重？统一当成候选文本
                        cfg["options"] = options
                config[qi] = cfg
                continue

            # matrix / matrix_single：格式 "1:w1,w2,w3 | 2:w1,w2,w3 ..."
            if qtype in ("matrix_single", "matrix"):
                cfg = {"type": qtype}
                rows = q.get("rows", [])
                cols = q.get("cols", [])
                if rows:
                    cfg["rows"] = list(rows)
                if cols:
                    cfg["cols"] = list(cols)
                if raw:
                    row_weights: dict = {}
                    ok_rows = True
                    for seg in raw.split("|"):
                        seg = seg.strip()
                        if not seg:
                            continue
                        if ":" not in seg:
                            ok_rows = False
                            break
                        rk, rhs = seg.split(":", 1)
                        rk = rk.strip()
                        try:
                            wlst = [float(x.strip()) for x in rhs.split(",")
                                    if x.strip()]
                        except ValueError:
                            ok_rows = False
                            break
                        if not rk:
                            ok_rows = False
                            break
                        row_weights[rk] = wlst
                    if not ok_rows:
                        self._log(
                            f"Q{qi} 矩阵行权重格式错误，跳过使用。"
                            "正确格式: 1:w1,w2,w3 | 2:w1,w2,w3",
                            "WARN",
                        )
                    elif row_weights:
                        cfg["row_weights"] = row_weights
                config[qi] = cfg
                continue

            # 未知类型：按旧逻辑兜底（浮点权重）
            if raw:
                try:
                    weights = [float(x.strip()) for x in raw.split(",") if x.strip()]
                except ValueError:
                    self._log(f"Q{qi} 权重格式错误，已跳过：{raw}", "WARN")
                    continue
                config[qi] = {"type": qtype, "weights": weights}
        return config

    # ==================================================================
    #  配置 IO 按钮处理（V2）
    # ==================================================================

    def _on_save_config(self) -> None:
        """把 GUI 表格的当前编辑内容导出为 JSON 配置文件。"""
        if not _HAS_CONFIG_IO or save_weight_config is None:
            self._log("未加载 src/config_io，无法导出配置", "FAIL")
            return
        cfg = self._build_weight_config()
        if not cfg:
            self._log("当前没有可导出的权重配置（请先探测题目）", "WARN")
            return
        filepath = filedialog.asksaveasfilename(
            title="导出权重配置",
            defaultextension=".json",
            initialfile="weight_config.json",
            filetypes=[("JSON 配置", "*.json"), ("所有文件", "*.*")],
        )
        if not filepath:
            return
        try:
            meta = {
                "name": os.path.splitext(os.path.basename(filepath))[0],
                "description": f"GUI 导出 · 共 {len(cfg)} 道题",
                "survey_url": self.url_var.get().strip()[:200],
            }
            if validate_weight_config:
                warnings = validate_weight_config(cfg)
                if warnings:
                    self._log(
                        f"导出前校验发现 {len(warnings)} 条警告，首条：{warnings[0]}",
                        "WARN",
                    )
            save_weight_config(filepath, cfg, meta=meta)
            self._log(f"✓ 已导出配置 → {os.path.relpath(filepath, _PROJECT_ROOT)}",
                      "OK")
        except Exception as e:
            self._log(f"导出配置失败: {type(e).__name__}: {e}", "FAIL")

    def _on_save_default_config(self) -> None:
        """把当前 GUI 表格保存为默认配置（启动时自动加载）。"""
        if not _HAS_CONFIG_IO or save_weight_config is None:
            self._log("未加载 src/config_io，无法另存默认配置", "FAIL")
            return
        cfg = self._build_weight_config()
        if not cfg:
            self._log("当前没有可保存的权重配置", "WARN")
            return
        try:
            if not os.path.exists(_DEFAULT_CONFIG_DIR):
                os.makedirs(_DEFAULT_CONFIG_DIR, exist_ok=True)
            meta = {
                "name": "default_weight_config",
                "description": f"GUI 另存默认 · 共 {len(cfg)} 道题",
                "survey_url": self.url_var.get().strip()[:200],
            }
            save_weight_config(DEFAULT_WEIGHT_CONFIG_PATH, cfg, meta=meta)
            self._log(
                f"✓ 已另存默认配置 → {os.path.relpath(DEFAULT_WEIGHT_CONFIG_PATH, _PROJECT_ROOT)}",
                "OK",
            )
        except Exception as e:
            self._log(f"另存默认配置失败: {type(e).__name__}: {e}", "FAIL")

    def _on_load_config(self, path: str | None = None) -> None:
        """从 JSON 配置文件导入权重；更新全局 WEIGHT_CONFIG + GUI 表格。

        :param path: 若为 None，弹文件选择框。
        """
        if not _HAS_CONFIG_IO or load_weight_config is None:
            self._log("未加载 src/config_io，无法导入配置", "FAIL")
            return
        if path is None:
            path = filedialog.askopenfilename(
                title="导入权重配置",
                filetypes=[("JSON 配置", "*.json"), ("所有文件", "*.*")],
                initialdir=_DEFAULT_CONFIG_DIR if os.path.exists(_DEFAULT_CONFIG_DIR)
                else _PROJECT_ROOT,
            )
            if not path:
                return
        if not os.path.exists(path):
            self._log(f"配置文件不存在: {path}", "FAIL")
            return
        try:
            cfg, meta = load_weight_config(path)
        except Exception as e:
            self._log(f"读取配置失败: {type(e).__name__}: {e}", "FAIL")
            return

        if validate_weight_config:
            warnings = validate_weight_config(cfg)
            for w in warnings:
                self._log(f"[校验警告] {w}", "WARN")
            self._log(f"配置载入 · 校验警告 {len(warnings)} 条", "INFO")

        # 1. 合并到全局 WEIGHT_CONFIG
        _cfg_module.WEIGHT_CONFIG.update(cfg)

        # 2. 如果已经探测过题目，刷新 GUI 表格中对应题的显示
        applied_cnt = 0
        for qi, qcfg in cfg.items():
            entry_var = self.weight_entries.get(qi)
            if entry_var is None:
                continue  # 该题号不在当前探测结果里，仅更新全局即可
            weights = qcfg.get("weights")
            options = qcfg.get("options")
            row_weights = qcfg.get("row_weights")
            if weights:
                entry_var.set(",".join(
                    [f"{w:.4f}" if isinstance(w, (int, float)) else str(w)
                     for w in weights]
                ))
            elif options:
                entry_var.set(",".join(str(x) for x in options))
            elif row_weights and isinstance(row_weights, dict):
                lines = []
                for rk in sorted(row_weights.keys(),
                                 key=lambda x: int(x) if str(x).isdigit() else str(x)):
                    wlst = row_weights[rk]
                    lines.append(f"{rk}:{','.join(str(x) for x in wlst)}")
                if lines:
                    entry_var.set(" | ".join(lines))
            applied_cnt += 1

        meta_name = meta.get("name") or os.path.basename(path)
        self._log(
            f"✓ 已载入「{meta_name}」· 配置 {len(cfg)} 道 · "
            f"同步到表格 {applied_cnt} 道",
            "OK",
        )

    # ==================================================================
    #  探测题目
    # ==================================================================

    def _on_import_qr(self) -> None:
        filepath = filedialog.askopenfilename(
            title="选择二维码图片",
            filetypes=[
                ("图片文件", "*.png *.jpg *.jpeg *.bmp *.gif"),
                ("所有文件", "*.*"),
            ],
        )
        if not filepath:
            return
        self._set_status("正在解析二维码...", COLORS["warning"])
        self._log(f"正在解析二维码: {os.path.basename(filepath)}", "INFO")
        result = decode_qr_from_image(filepath)
        if result:
            self.url_var.set(result)
            self._log(f"二维码解析成功 ✓: {result}", "OK")
            self._set_status("就绪", COLORS["text_dim"])
        else:
            self._log("未识别到二维码内容 ✗", "FAIL")
            self._set_status("就绪", COLORS["text_dim"])

    def _on_detect_questions(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请先填写问卷 URL")
            return
        self.detect_btn.configure(state=tk.DISABLED)
        self.qr_btn.configure(state=tk.DISABLED)
        self._set_status("正在探测题目...", COLORS["warning"])
        self._log("正在连接问卷页面，探测题目结构...", "HEADER")

        def _worker():
            driver = None
            try:
                driver = create_driver(
                    self.browser_var.get(), use_uc=self.use_uc_var.get(),
                )
                driver.get(url)
                try:
                    WebDriverWait(driver, 25).until(
                        lambda d: d.execute_script("return document.readyState") == "complete"
                    )
                except Exception:
                    pass
                time.sleep(2)

                if is_smart_verification_showing(driver):
                    self._log("检测到验证码，请在浏览器中手动完成...", "WARN")
                    if not wait_for_manual_verification(driver):
                        self._log("验证超时，探测失败", "FAIL")
                        return

                # V2：判定"问卷已加载"的元素条件扩展到 6 类题型的常见 DOM
                has_any_q = driver.execute_script(
                    "return ("
                    "document.querySelectorAll('input[type=\"radio\"], input[type=\"checkbox\"]').length"
                    " + document.querySelectorAll('input[type=\"text\"], textarea').length"
                    " + document.querySelectorAll('select').length"
                    " + document.querySelectorAll('div.ui-slider, div.star, div.question-rating, div.scale-span').length"
                    " + document.querySelectorAll('div.field div.label').length"
                    ");"
                )
                if not has_any_q:
                    iframes = driver.execute_script(
                        "return document.querySelectorAll('iframe').length"
                    )
                    found_frame = False
                    for i in range(iframes):
                        driver.switch_to.frame(i)
                        in_frame = driver.execute_script(
                            "return ("
                            "document.querySelectorAll('input[type=\"radio\"], input[type=\"checkbox\"]').length"
                            " + document.querySelectorAll('input[type=\"text\"], textarea').length"
                            " + document.querySelectorAll('select').length"
                            " + document.querySelectorAll('div.ui-slider, div.star, div.scale-span').length"
                            " + document.querySelectorAll('div.field div.label').length"
                            ");"
                        )
                        if in_frame:
                            found_frame = True
                            break
                        driver.switch_to.default_content()
                    if not found_frame:
                        driver.switch_to.default_content()
                        self._log("未能在页面中找到题目元素", "FAIL")
                        return

                try:
                    WebDriverWait(driver, 15).until(
                        lambda d: d.execute_script(
                            "return ("
                            "document.querySelectorAll('input[type=\"radio\"], input[type=\"checkbox\"]').length"
                            " + document.querySelectorAll('input[type=\"text\"], textarea').length"
                            " + document.querySelectorAll('select').length"
                            " + document.querySelectorAll('div.ui-slider, div.star, div.scale-span').length"
                            " + document.querySelectorAll('div.field div.label').length"
                            ") > 0;"
                        )
                    )
                except Exception:
                    pass

                questions = detect_questions(driver)
                driver.switch_to.default_content()
                if not questions:
                    self._log("未探测到任何题目", "FAIL")
                    return
                self.root.after(0, lambda: self._on_questions_detected(questions))
            except Exception as e:
                self._log(f"探测失败: {type(e).__name__}: {e}", "FAIL")
            finally:
                if driver:
                    try: driver.quit()
                    except Exception: pass
                self.root.after(0, lambda: self.detect_btn.configure(state=tk.NORMAL))
                self.root.after(0, lambda: self.qr_btn.configure(state=tk.NORMAL))
                self.root.after(0, lambda: self._set_status("就绪", COLORS["text_dim"]))

        threading.Thread(target=_worker, daemon=True).start()

    def _restore_weight_table_from_config(self, restored_w: dict[int, dict]) -> None:
        """V2.1 续传：从持久化的 weight_config 重建 GUI 表格显示。

        当用户跨进程续传时，``self.questions`` 是空的（页面还没探测），
        无法直接调 ``_populate_weight_table`` —— 这里从 restored_w 反向构造
        一个最小化的 questions list，让表格能正确显示题号 + 类型 Badge +
        填入用户上次的权重字符串。

        反向构造规则：
          - single/multi/dropdown：choices 长度 = weights 长度
          - scale：scale 字段或 weights 长度
          - text：无 choices；若 options 字段存在则填入候选
          - matrix_single：rows/cols 字段；若 row_weights 存在则填入
        """
        if not restored_w:
            return

        # 检查 self.questions 是否已有数据（用户已探测过 + 已填表）
        # 若有，直接走 _populate_weight_table 复用现有 self.questions
        # 没有 → 反向构造
        if self.questions:
            # 已有 questions，直接刷新表格（_populate_weight_table 会从
            # _cfg_module.WEIGHT_CONFIG 读取已恢复的配置）
            self._populate_weight_table(self.questions)
            return

        # 反向构造最小化 questions
        reconstructed: list[dict] = []
        for qi in sorted(restored_w.keys()):
            cfg = restored_w[qi]
            if not isinstance(cfg, dict):
                continue
            qtype = cfg.get("type", "single")
            q: dict = {"q": qi, "type": qtype}

            if qtype in ("single", "radio", "multi", "checkbox", "dropdown"):
                weights = cfg.get("weights") or []
                # choices 用占位索引（仅用于显示"选项数"列）
                q["choices"] = list(range(1, len(weights) + 1)) if weights else [1, 2]
            elif qtype in ("scale", "rating"):
                # 优先用 scale 字段，否则用 weights 长度
                scale = cfg.get("scale")
                if scale is None:
                    weights = cfg.get("weights") or []
                    scale = len(weights) if weights else 5
                q["scale"] = int(scale)
                q["scale_min"] = 1
                q["choices"] = list(range(1, int(scale) + 1))
            elif qtype in ("text", "input", "textarea", "fillblank"):
                q["field"] = cfg.get("field")
                q["choices"] = []
            elif qtype in ("matrix_single", "matrix"):
                rows = cfg.get("rows") or [1, 2]
                cols = cfg.get("cols") or [1, 2]
                # 如果有 row_weights，从 row_weights 推断 rows
                row_weights = cfg.get("row_weights") or {}
                if row_weights:
                    rows = sorted(int(k) for k in row_weights.keys())
                    if not cols and row_weights:
                        first_rw = next(iter(row_weights.values()))
                        cols = list(range(1, len(first_rw) + 1)) if first_rw else [1, 2]
                q["rows"] = rows
                q["cols"] = cols
                q["choices"] = cols  # 给 _populate_weight_table 用作"选项数"列
            else:
                q["choices"] = cfg.get("choices") or [1, 2]

            reconstructed.append(q)

        if reconstructed:
            self._populate_weight_table(reconstructed)

    def _on_questions_detected(self, questions: list[dict]) -> None:
        self._populate_weight_table(questions)
        single_n = sum(1 for q in questions if q.get("type")
                       in ("single", "radio"))
        multi_n = sum(1 for q in questions if q.get("type")
                      in ("multi", "checkbox"))
        scale_n = sum(1 for q in questions if q.get("type")
                      in ("scale", "rating"))
        drop_n = sum(1 for q in questions if q.get("type") == "dropdown")
        text_n = sum(1 for q in questions if q.get("type")
                     in ("text", "input", "textarea", "fillblank"))
        mat_n = sum(1 for q in questions if q.get("type")
                    in ("matrix_single", "matrix"))
        parts = []
        if single_n: parts.append(f"{single_n} 单选")
        if multi_n:  parts.append(f"{multi_n} 多选")
        if drop_n:   parts.append(f"{drop_n} 下拉")
        if scale_n:  parts.append(f"{scale_n} 量表")
        if text_n:   parts.append(f"{text_n} 填空")
        if mat_n:    parts.append(f"{mat_n} 矩阵")
        summary = "、".join(parts) if parts else "未识别题型"
        self._log(
            f"探测完成，共 {len(questions)} 题（{summary}）",
            "OK",
        )
        self._log("请在表格中调整权重后点击「开始运行」", "INFO")

    # ==================================================================
    #  运行控制
    # ==================================================================

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

        if self.questions:
            _cfg_module.WEIGHT_CONFIG.clear()
            _cfg_module.WEIGHT_CONFIG.update(self._build_weight_config())
            self._log(f"已加载 {len(_cfg_module.WEIGHT_CONFIG)} 道题的自定义权重", "INFO")
        else:
            _cfg_module.WEIGHT_CONFIG.clear()
            self._log("未配置权重表格，所有题目使用等权重随机", "WARN")

        # ---- V2 断点续传：检查是否有可恢复的上次批次 ----
        start_idx = 1
        resume_run_id: int | None = None
        weights_restored = False
        db_for_resume = self._history_get_db()
        if db_for_resume is not None:
            try:
                prev = db_for_resume.find_resumable_run(url[:500])
                if prev is not None:
                    done = int(prev["success_count"])
                    planned = int(prev["total_submissions"])
                    # 只有"已成功 ≥ 1 且 < 计划总数"时才提示恢复
                    if 0 < done < planned:
                        # V2.1：从 row 反序列化上次的权重配置，自动恢复
                        try:
                            restored_w = type(db_for_resume).deserialize_weight_config(prev)
                        except Exception:
                            restored_w = {}
                        if restored_w:
                            _cfg_module.WEIGHT_CONFIG.clear()
                            _cfg_module.WEIGHT_CONFIG.update(restored_w)
                            weights_restored = True
                            self._log(
                                f"[续传] 已自动恢复上次权重配置："
                                f"{len(restored_w)} 道题",
                                "OK",
                            )
                            # 把权重数据重新刷到 GUI 表格显示，让用户可见可改
                            self._restore_weight_table_from_config(restored_w)

                        msg = (
                            f"检测到上次未完成的批次：\n\n"
                            f"  Run #{prev['id']} · 状态 = {prev['status']}\n"
                            f"  已成功 {done} / {planned} 份\n"
                            f"  开始时间 {str(prev['started_at'])[:19]}\n\n"
                        )
                        if weights_restored:
                            msg += (
                                f"✅ 上次权重已自动恢复到表格（{len(restored_w)} 道题），\n"
                                f"    可在配置 Tab 检查 / 修改后再启动。\n\n"
                            )
                        msg += (
                            f"是否从第 {done + 1} 份继续？"
                            f"（取消则从第 1 份重新开始，但权重恢复仍生效）"
                        )
                        yes = messagebox.askyesno(
                            "断点续传", msg, icon=messagebox.QUESTION,
                        )
                        if yes:
                            start_idx = done + 1
                            resume_run_id = int(prev["id"])
                            self._log(
                                f"[续传] 恢复 Run #{resume_run_id}："
                                f"从第 {start_idx} 份继续（共 {planned} 份）",
                                "OK",
                            )
                        else:
                            self._log(
                                f"[续传] 已忽略上次中断批次，从第 1 份重新开始"
                                f"（权重恢复仍生效）",
                                "INFO",
                            )
            except Exception as e:
                self._log(
                    f"[续传] 检查可恢复批次失败（不影响运行）: "
                    f"{type(e).__name__}: {e}",
                    "WARN",
                )

        self.running = True
        self.stop_flag = False
        # 续传：已成功份数初始化为 start_idx - 1（这样进度条立刻反映真实状态）
        self.success_count = start_idx - 1
        self.fail_count = 0
        self.current_round = start_idx
        self.total_rounds = total

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
        if start_idx > 1:
            self._log(
                f"▶ 断点续传启动：从第 {start_idx} 份 → 第 {total} 份（共 {total - start_idx + 1} 份待跑）",
                "HEADER",
            )
        else:
            self._log(f"▶ 开始执行，目标 {total} 份", "HEADER")
        self._log("═" * 40, "HEADER")

        threading.Thread(
            target=self._run_loop,
            args=(url, total, start_idx, resume_run_id),
            daemon=True,
        ).start()

    def _on_stop(self) -> None:
        if not self.running:
            return
        self.stop_flag = True
        self.stop_btn.configure(state=tk.DISABLED)
        self._set_status("正在停止...", COLORS["warning"])
        self._log("用户请求停止，等待当前轮次完成...", "WARN")

    def _run_loop(
        self,
        url: str,
        total: int,
        start_idx: int = 1,
        resume_run_id: int | None = None,
    ) -> None:
        driver = None
        history_db: "SubmissionHistory | None" = None
        run_id: int | None = resume_run_id  # 断点续传：复用上次 run_id
        final_status = "failed"
        t0 = time.time()
        try:
            # ---- V2：若历史模块可用，开启本次运行记录 ----
            db = self._history_get_db()
            if db is not None:
                try:
                    if run_id is None:
                        # 全新批次：start_run + 持久化当前权重配置快照
                        # （V2.1：用 dict() 深拷贝，避免后续 WEIGHT_CONFIG 变动影响快照）
                        wc_snapshot = {
                            int(k): dict(v) if isinstance(v, dict) else v
                            for k, v in _cfg_module.WEIGHT_CONFIG.items()
                        }
                        run_id = db.start_run(
                            survey_url=url[:500],
                            total_submissions=total,
                            browser=self.browser_var.get(),
                            use_uc=self.use_uc_var.get(),
                            weight_config=wc_snapshot if wc_snapshot else None,
                        )
                        self._log(
                            f"[历史] Run #{run_id} 已记录起点"
                            + (f" · 权重快照 {len(wc_snapshot)} 道题" if wc_snapshot else " · 等权重"),
                            "INFO",
                        )
                    else:
                        # 断点续传：复用上次 run_id，不重新建表
                        # （上次写入的 weight_config_json 仍是当时的快照，不需要覆盖）
                        self._log(
                            f"[历史] 续传模式 · 复用 Run #{run_id}（不重置计数，权重沿用上次）",
                            "INFO",
                        )
                    history_db = db
                except Exception as e:
                    self._log(f"[历史] start_run 失败（不影响答题）: "
                              f"{type(e).__name__}: {e}", "WARN")
                    history_db = None
                    run_id = None

            driver = create_driver(
                self.browser_var.get(), use_uc=self.use_uc_var.get(),
            )
            # 断点续传：循环从 start_idx 起步（已成功的 start_idx-1 份数不计入本轮）
            for idx in range(start_idx, total + 1):
                if self.stop_flag:
                    final_status = "interrupted"  # 用户主动停止 → 可恢复
                    self._log("已停止运行（已成功份数可下次恢复）", "WARN")
                    break

                self.current_round = idx
                self.root.after(0, self._update_progress)

                self._log(f"[{idx}/{total}] 提交中...", "INFO")

                try:
                    # 审查 P1-1：run_one_submission 返回 "success" / "failed" / "unknown" 三态
                    outcome = run_one_submission(
                        driver, url,
                        history_db=history_db,
                        run_id=run_id,
                        submission_index=idx,
                    )
                except InvalidSessionIdException:
                    self._log("浏览器断开，正在重建...", "WARN")
                    try: driver.quit()
                    except Exception: pass
                    driver = create_driver(
                        self.browser_var.get(), use_uc=self.use_uc_var.get(),
                    )
                    self.fail_count += 1
                    self.root.after(0, self._update_progress)
                    continue

                # 审查 P1-1：只有 "success" 才计成功；"failed" / "unknown" 都计失败
                if outcome == "success":
                    self.success_count += 1
                    self._log("✓ 提交成功", "OK")
                elif outcome == "unknown":
                    # 按钮已点击但未观察到成功信号 → 保守计失败，但日志区分便于复盘
                    self.fail_count += 1
                    self._log("⚠ 提交状态未知（按钮已点击但效果超时）", "FAIL")
                else:
                    self.fail_count += 1
                    self._log("✕ 提交失败", "FAIL")

                self.root.after(0, self._update_progress)

                try:
                    driver.delete_all_cookies()
                    driver.execute_script("window.localStorage.clear();")
                    driver.execute_script("window.sessionStorage.clear();")
                except Exception:
                    pass

                if idx % RESTART_BROWSER_EVERY == 0:
                    self._log("重启浏览器释放内存", "INFO")
                    try: driver.quit()
                    except Exception: pass
                    driver = create_driver(
                        self.browser_var.get(), use_uc=self.use_uc_var.get(),
                    )

                time.sleep(random.uniform(ROUND_INTERVAL_MIN, ROUND_INTERVAL_MAX))
            else:
                # for 正常跑完（没 break）
                final_status = "finished"

        except Exception as e:
            final_status = "failed"
            self._log(f"运行异常: {type(e).__name__}: {e}", "FAIL")
        finally:
            # ---- V2：写入结束状态 ----
            if history_db is not None and run_id is not None:
                try:
                    note = (
                        f"GUI · browser={self.browser_var.get()} "
                        f"uc={self.use_uc_var.get()}"
                    )
                    elapsed = time.time() - t0
                    if final_status == "interrupted":
                        # 中断：用 mark_interrupted 别名，语义清晰
                        history_db.mark_interrupted(
                            run_id,
                            success_count=self.success_count,
                            fail_count=self.fail_count,
                            total_elapsed_seconds=elapsed,
                            error_message=note,
                        )
                    else:
                        history_db.finish_run(
                            run_id,
                            success_count=self.success_count,
                            fail_count=self.fail_count,
                            total_elapsed_seconds=elapsed,
                            status=final_status,
                            error_message=note if final_status != "finished" else None,
                        )
                    self._log(
                        f"[历史] Run #{run_id} 已闭合: {final_status} "
                        f"(✓ {self.success_count} / ✕ {self.fail_count})",
                        "INFO",
                    )
                except Exception as he:
                    self._log(f"[历史] finish_run 失败: {type(he).__name__}: {he}",
                              "WARN")
            if driver:
                try: driver.quit()
                except Exception: pass
            self.root.after(0, self._on_run_finished)

    def _on_run_finished(self) -> None:
        self.running = False
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
        # V2：刷新历史记录 Tab
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
