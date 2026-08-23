"""GUI 主题系统：颜色常量 + 颜色/渐变纯函数工具（第一章第 1 条拆分，7A 子步骤）。

设计目标：
    1. 把 COLORS / GRAD_* 等纯主题常量从 app.py 中剥离，使 2900 行的主类瘦身；
    2. 颜色转换与 Canvas 渐变绘制是"无状态纯函数"，完全不依赖 SurveyGUI 实例，
       可单独检视、替换、扩展（如新增「暗夜模式」时只改此文件）。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Mapping

# ============================================================================
#  配色方案 — 高对比度·黑白极简主题（+ 蓝色点缀）
#  所有文字对比度满足 WCAG AA（≥ 4.5:1），确保 UC 复选框等元素清晰可见
# ============================================================================

COLORS: dict[str, str] = {
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
GRAD_PRIMARY: tuple[str, ...] = ("#0052cc", "#0066ff", "#3385ff")   # 深→浅蓝
GRAD_DANGER:  tuple[str, ...] = ("#b91c1c", "#dc2626", "#ef4444")   # 红渐变
GRAD_SUCCESS: tuple[str, ...] = ("#15803d", "#16a34a", "#22c55e")   # 绿渐变
GRAD_HEADER:  tuple[str, ...] = ("#111111", "#222222", "#2a2a2a")   # 标题栏：纯黑渐变灰


# ============================================================================
#  字体常量（统一由 theme 模块暴露，避免 SurveyGUI 散设字体）
# ============================================================================

FONT_PRESETS: dict[str, tuple[str, int, str]] = {
    "HUGE":   ("Microsoft YaHei UI", 20, "bold"),
    "TITLE":  ("Microsoft YaHei UI", 14, "bold"),
    "LARGE":  ("Microsoft YaHei UI", 12, "bold"),
    "NORMAL": ("Microsoft YaHei UI", 10),
    "SMALL":  ("Microsoft YaHei UI", 9),
    "MONO":   ("Cascadia Code", 9),
    "MONO_L": ("Cascadia Code", 10),
    "ICON":   ("Segoe UI Emoji", 11),
}


# ============================================================================
#  颜色纯函数工具
# ============================================================================

def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """#RRGGBB → (R, G, B) 0-255。别名兼容：_hex_to_rgb。"""
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


# 兼容历史下划线别名（app.py / widgets.py 中仍以 `_hex_to_rgb` 调用）
_hex_to_rgb = hex_to_rgb


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """(R, G, B) 0-255 → #RRGGBB。"""
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


_rgb_to_hex = rgb_to_hex


def lerp_color(c1: str, c2: str, t: float) -> str:
    """在两个 hex 颜色间线性插值（0 ≤ t ≤ 1）。"""
    r1, g1, b1 = hex_to_rgb(c1)
    r2, g2, b2 = hex_to_rgb(c2)
    return rgb_to_hex(
        r1 + (r2 - r1) * t,
        g1 + (g2 - g1) * t,
        b1 + (b2 - b1) * t,
    )


_lerp_color = lerp_color


# ============================================================================
#  Canvas 渐变绘制（纯绘制操作，不持有任何 SurveyGUI 状态）
# ============================================================================

def draw_vertical_gradient(
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
            c = lerp_color(c_start, c_end, t)
            kwargs = {}
            if tag is not None and si == 0 and i == 0:
                kwargs["tags"] = tag
            canvas.create_line(x1, y_start + i, x2, y_start + i, fill=c, width=1)


_draw_vertical_gradient = draw_vertical_gradient


def draw_horizontal_gradient(
    canvas: tk.Canvas,
    x1: int, y1: int, x2: int, y2: int,
    colors: tuple[str, ...],
    tag: str | None = None,
) -> None:
    """在 Canvas 上绘制水平多色渐变（通过密集线条模拟）。"""
    n = max(1, x2 - x1)
    segments = len(colors) - 1
    steps_per_seg = max(1, n // segments)
    for si in range(segments):
        c_start, c_end = colors[si], colors[si + 1]
        x_start = x1 + si * steps_per_seg
        x_end = x1 + (si + 1) * steps_per_seg if si < segments - 1 else x2
        for i in range(x_end - x_start):
            t = i / max(1, (x_end - x_start) - 1)
            c = lerp_color(c_start, c_end, t)
            kwargs = {}
            if tag is not None and si == 0 and i == 0:
                kwargs["tags"] = tag
            canvas.create_line(x_start + i, y1, x_start + i, y2, fill=c, width=1)


_draw_horizontal_gradient = draw_horizontal_gradient


# ============================================================================
#  ttk Style 配置（从 SurveyGUI._setup_theme 抽取 —— 仍允许用户从外部重写）
# ============================================================================

def apply_ttk_style(
    root: tk.Tk,
    colors: Mapping[str, str] | None = None,
    font_presets: Mapping[str, tuple] | None = None,
) -> tuple[tk.ttk.Style, dict[str, tuple]]:
    """配置全局 ttk 样式 + 返回字体字典。

    返回：
        (style, fonts_dict)
            fonts_dict 形如:
            {"HUGE": (...), "TITLE": (...), ..., "ICON": (...)}
    """
    from tkinter import ttk  # local import, ttk 必须随 Tk() 之后创建

    colors = colors if colors is not None else COLORS
    font_presets = font_presets if font_presets is not None else FONT_PRESETS

    style = ttk.Style(root)
    available = style.theme_names()
    base_theme = (
        "vista" if "vista" in available
        else ("clam" if "clam" in available else available[0])
    )
    style.theme_use(base_theme)

    fonts = dict(font_presets)

    # ---- 通用 Frame ----
    style.configure("TFrame",       background=colors["bg"])
    style.configure("Deep.TFrame",  background=colors["bg_deep"])
    style.configure("Mid.TFrame",   background=colors["bg_mid"])
    style.configure("Surface.TFrame", background=colors["surface"])

    style.configure("TLabelframe", background=colors["bg"])
    style.configure(
        "TLabelframe.Label",
        background=colors["bg"],
        foreground=colors["primary"],
        font=fonts["LARGE"],
    )

    # ---- 按钮（ttk 兜底） ----
    style.configure(
        "Ghost.TButton",
        font=fonts["NORMAL"],
        padding=(14, 5),
        borderwidth=0,
        focuscolor=colors["primary"],
        background=colors["surface"],
        foreground=colors["text"],
    )
    style.map(
        "Ghost.TButton",
        background=[("active", colors["surface_2"])],
        foreground=[("active", colors["primary"])],
    )

    # ---- Combobox（白底黑字 + 聚焦蓝边） ----
    style.configure(
        "TCombobox",
        fieldbackground="#ffffff",
        background="#ffffff",
        foreground=colors["text"],
        arrowcolor=colors["primary"],
        bordercolor=colors["border_dim"],
        lightcolor=colors["border_dim"],
        darkcolor=colors["border_dim"],
        padding=(10, 4),
        font=fonts["NORMAL"],
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", "#ffffff"), ("focus", "#ffffff")],
        foreground=[("readonly", colors["text"]), ("disabled", colors["text_muted"])],
        selectbackground=[("readonly", colors["primary"])],
        selectforeground=[("readonly", "white")],
        bordercolor=[("focus", colors["primary"])],
    )

    # ---- 滚动条（灰底 + 蓝滑条） ----
    style.configure(
        "Vertical.TScrollbar",
        background="#cccccc",
        troughcolor=colors["surface_2"],
        bordercolor=colors["surface_2"],
        arrowcolor=colors["primary"],
        gripcount=0,
        relief="flat",
        width=10,
    )
    style.map(
        "Vertical.TScrollbar",
        background=[("active", colors["primary"])],
    )
    style.configure(
        "Horizontal.TScrollbar",
        background="#cccccc",
        troughcolor=colors["surface_2"],
        bordercolor=colors["surface_2"],
        arrowcolor=colors["primary"],
        gripcount=0,
        relief="flat",
    )
    style.map(
        "Horizontal.TScrollbar",
        background=[("active", colors["primary"])],
    )

    # ---- 进度条 ----
    style.configure(
        "Aurora.Horizontal.TProgressbar",
        thickness=14,
        troughcolor=colors["surface_2"],
        background=colors["primary"],
        bordercolor=colors["surface_2"],
        lightcolor=colors["primary"],
        darkcolor=colors["primary"],
        relief="flat",
    )

    return style, fonts
