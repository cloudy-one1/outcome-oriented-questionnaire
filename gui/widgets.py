"""通用 GUI 组件工厂（第一章第 1 条拆分 · 7B 子步骤）。

把 SurveyGUI 中的"通用 UI 工厂方法"剥离为纯函数：
    - make_card           : 渐变边框 + 标题栏卡片容器（细蓝边，极简）
    - paint_card_border   : Canvas 尺寸变化时重绘卡片装饰
    - make_icon_button    : 主功能按钮（primary/success/danger/ghost 四色）
    - make_spin_button    : 增减数字按钮（+/−）
    - make_toggle         : 高对比复选框（白底黑字，确保 UC 提示可见）
    - make_stat_badge     : 渐变发光统计 Badge（成功/失败计数）

所有函数只依赖 gui.theme 中的常量/工具（COLORS / FONT_PRESETS / draw_*），
不持有 SurveyGUI 状态，便于单独替换主题或写 widget 级微测试。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Mapping
from typing import Any

from .theme import (
    COLORS,
    FONT_PRESETS,
    GRAD_PRIMARY,
    _draw_horizontal_gradient,
    _draw_vertical_gradient,
    _lerp_color,
)

_FONTS_KIND = Mapping[str, tuple]


def _resolve_fonts(fonts: _FONTS_KIND | None) -> dict[str, tuple]:
    """默认字体：FONT_PRESETS。允许调用方覆盖（方便以后自定义字体）。"""
    if fonts is None:
        return dict(FONT_PRESETS)
    return dict(fonts)


# ============================================================================
#  make_card + paint_card_border（原 SurveyGUI._make_card / _on_card_resize）
# ============================================================================

def make_card(
    parent: tk.Misc,
    title: str,
    icon: str = "◆",
    accent: tuple[str, ...] = GRAD_PRIMARY,
    fonts: _FONTS_KIND | None = None,
) -> tk.Frame:
    """创建带细边框和标题条的卡片容器（白底高对比），返回内部 body Frame。

    返回值是 inner_body（供调用方 .pack / .grid 放置内容）。outer Frame
    上附加 `_card_canvas` 与 `_card_body` 属性以便后续事件使用。
    """
    fonts_dict = _resolve_fonts(fonts)
    font_icon = fonts_dict["ICON"]
    font_title = fonts_dict["TITLE"]

    outer = tk.Frame(parent, bg=COLORS["bg"])
    outer.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
    outer.pack_propagate(True)

    # Canvas 绘制 1.5px 细边（克制的点缀）
    card_c = tk.Canvas(outer, bg=COLORS["bg"], highlightthickness=0, bd=0)
    card_c.pack(fill=tk.BOTH, expand=True)
    card_c.bind("<Configure>", lambda e, cc=card_c, ac=accent:
                paint_card_border(cc, ac))

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
        header, text=f"{icon} ", font=font_icon,
        fg=accent[0], bg=COLORS["surface"],
    ).pack(side=tk.LEFT)
    tk.Label(
        header, text=title, font=font_title,
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


# 向后兼容：SurveyGUI 原有下划线方法名
_make_card = make_card


def paint_card_border(
    canvas: tk.Canvas,
    accent: tuple[str, ...],
) -> None:
    """响应 Canvas 尺寸变化，重绘卡片灰边 + 四角渐变色段。

    原 SurveyGUI._on_card_resize 方法剥离，无 self 依赖。
    """
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


# 下划线别名兼容
_on_card_resize = paint_card_border


# ============================================================================
#  make_icon_button（原 _make_icon_button）
# ============================================================================

def make_icon_button(
    parent: tk.Misc,
    text: str,
    accent: str = "primary",
    command: Any = None,
    fonts: _FONTS_KIND | None = None,
) -> tk.Button:
    """创建高对比度按钮（白底黑字 / 功能色背景）。

    accent ∈ {"primary", "success", "danger", "ghost"}。
    """
    fonts_dict = _resolve_fonts(fonts)
    font_normal = fonts_dict["NORMAL"]

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
        font=font_normal,
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


_make_icon_button = make_icon_button


# ============================================================================
#  make_spin_button（原 _make_spin_button — +/− 按钮）
# ============================================================================

def make_spin_button(
    parent: tk.Misc,
    text: str,
    kind: str,
    fonts: _FONTS_KIND | None = None,
) -> tk.Button:
    """kind="plus" 为蓝色，kind="minus" 为橙色（低饱和可辨）。"""
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


_make_spin_button = make_spin_button


# ============================================================================
#  make_toggle（原 _make_toggle — 高对比复选框）
# ============================================================================

def make_toggle(
    parent: tk.Misc,
    text: str,
    var: tk.BooleanVar,
    enabled: bool = True,
    fonts: _FONTS_KIND | None = None,
) -> tk.Checkbutton:
    """**高对比复选框**：白底黑字，确保 UC 模式等文字清晰可见。"""
    fonts_dict = _resolve_fonts(fonts)
    font_normal = fonts_dict["NORMAL"]
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
        font=font_normal,                      # 正号字 (10pt)，不再用小号
        bd=0,
        cursor="hand2",
        state=("normal" if enabled else "disabled"),
        disabledforeground="#888888",          # 禁用文字浅灰
        anchor="w",
        padx=6,
        pady=4,
    )
    return chk


_make_toggle = make_toggle


# ============================================================================
#  make_stat_badge（原 _make_stat_badge — 渐变发光数字统计 Badge）
# ============================================================================

def make_stat_badge(
    parent: tk.Misc,
    label: str,
    var: tk.StringVar,
    grad: tuple[str, ...],
    icon: str,
    fonts: _FONTS_KIND | None = None,
    root: tk.Tk | None = None,
) -> tk.Frame:
    """渐变发光数字统计 Badge。

    说明：原 _make_stat_badge 通过 `self.root.after(10, _cfg)` 实现延迟触发
    首次布局，这里改为可选 `root` 参数（传则保持原语义；不传也能在后续
    <Configure> 事件中正常触发）。
    """
    fonts_dict = _resolve_fonts(fonts)
    font_small = fonts_dict["SMALL"]

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
    if root is not None:
        root.after(10, _cfg)

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
             font=font_small,
             bg=COLORS["surface_2"], fg=COLORS["text_soft"]).pack(side=tk.LEFT)

    num_wrap = tk.Frame(body, bg=COLORS["surface_2"])
    num_wrap.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6))
    tk.Label(num_wrap, textvariable=var,
             font=("Cascadia Code", 18, "bold"),
             bg=COLORS["surface_2"], fg=grad[1]).pack(side=tk.LEFT)
    return wrap


_make_stat_badge = make_stat_badge
