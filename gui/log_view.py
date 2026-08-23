"""日志终端面板（第一章第 1 条拆分 · 7E 子步骤）。

封装：
    - "运行日志"卡片：Mac 风格装饰点 + "terminal@aurora" 标题
    - 扫描线动画（顶部 2px Canvas · 40ms 周期）
    - 终端光标闪烁（~530ms 交替）
    - 行号 + 日志文本 + 同步滚动（东京夜配色）
    - 多生产者 / 单消费者：Queue.Queue → 80ms 轮询 drain

设计要点：
    * ``log_queue`` 外部注入，保持 SurveyGUI 可以直接 ``.put()``。
    * 动画 tick 由调用方 ``LogView.tick_*()`` 显式触发，避免 view 层自己
      创建 after 作业；这样 root.after() 的生命周期都在编排层统一管理。
    * ``_draw_horizontal_gradient`` 仅 theme.py 里的同签名函数引用，以
      避免重复实现。
"""

from __future__ import annotations

import queue
import time
import tkinter as tk
from tkinter import scrolledtext
from typing import Callable

from .theme import COLORS, _draw_horizontal_gradient, _lerp_color, GRAD_DANGER

_LOG_TAG_PALETTE: dict[str, tuple[str,]] = {
    "INFO":    ("#9aa5ce",),
    "OK":      ("#9ece6a",),
    "FAIL":    ("#f7768e",),
    "WARN":    ("#e0af68",),
    "HEADER":  ("#7aa2f7",),
    "TIME":    ("#565f89",),
    "CURSOR":  ("#00ffa3",),
    "PROMPT":  ("#bb9af7",),
}


class LogView:
    """赛博朋克终端风格的日志面板。"""

    # 动画相位：调用方每 tick 自增这些变量，再调用 redraw 方法。
    scan_phase: float = 0.0
    cursor_blink: bool = True

    # 对主 gui 的兼容导出：log_lines_var 统计行号显示（外部可能读取）
    log_lines_var: tk.StringVar

    def __init__(
        self,
        root: tk.Tk,
        log_queue: "queue.Queue[tuple[str, str]]",
        *,
        make_card_fn: Callable,
    ) -> None:
        self.root = root
        self.log_queue = log_queue
        self._make_card = make_card_fn

        self._lineno_count: int = 0
        self._tags: dict[str, tuple[str,]] = dict(_LOG_TAG_PALETTE)

    # ==================================================================
    #  build
    # ==================================================================

    def build(self, parent: tk.Frame) -> None:
        body = self._make_card(parent, "运行日志", icon="📟", accent=GRAD_DANGER)
        body.pack(fill=tk.BOTH, expand=True)
        body.grid_rowconfigure(1, weight=1)
        body.grid_columnconfigure(0, weight=1)

        tb = tk.Frame(body, bg=COLORS["surface"])
        tb.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        dots = tk.Frame(tb, bg=COLORS["surface"])
        dots.pack(side=tk.LEFT)
        for col in [COLORS["danger"], COLORS["warning"], COLORS["success"]]:
            d = tk.Canvas(dots, width=12, height=12, bg=COLORS["surface"],
                          highlightthickness=0, bd=0)
            d.pack(side=tk.LEFT, padx=(0, 5))
            d.bind(
                "<Configure>",
                lambda e, c=d, col=col: (
                    c.delete("all"),
                    c.create_oval(1, 1, 11, 11, fill=col, outline=""),
                    c.create_oval(3, 3, 5, 5, fill="white", outline="",
                                  stipple="gray25"),
                ),
            )

        tk.Label(tb, text="  terminal@aurora  ~  zsh",
                 font=("Cascadia Code", 9),
                 bg=COLORS["surface"], fg=COLORS["text_dim"]
                 ).pack(side=tk.LEFT, padx=(6, 0))

        self.log_lines_var = tk.StringVar(value="0 lines")
        tk.Label(tb, textvariable=self.log_lines_var,
                 font=("Cascadia Code", 8),
                 bg=COLORS["surface"], fg=COLORS["primary_2"]).pack(side=tk.RIGHT)
        tk.Label(tb, text="●", font=("Cascadia Code", 6),
                 bg=COLORS["surface"], fg=COLORS["primary"]).pack(side=tk.RIGHT,
                                                                      padx=(0, 8))

        log_container = tk.Frame(body, bg=COLORS["term_bg"],
                                 highlightthickness=2,
                                 highlightbackground=COLORS["border_dim"])
        log_container.grid(row=1, column=0, sticky="nsew")
        log_container.grid_rowconfigure(1, weight=1)
        log_container.grid_columnconfigure(1, weight=1)

        self.scan_canvas = tk.Canvas(log_container, height=2, bg=COLORS["term_bg"],
                                     highlightthickness=0, bd=0)
        self.scan_canvas.grid(row=0, column=0, columnspan=2, sticky="ew")

        self.log_lineno = tk.Text(
            log_container, width=5, font=("Cascadia Code", 9),
            bg=COLORS["term_bg"], fg=COLORS["text_muted"],
            state=tk.DISABLED, relief=tk.FLAT, bd=0, padx=8, pady=8, takefocus=0,
            highlightthickness=0, selectbackground=COLORS["term_bg"],
        )
        self.log_lineno.grid(row=1, column=0, sticky="ns")

        self.log_text = scrolledtext.ScrolledText(
            log_container, wrap=tk.WORD, font=("Cascadia Code", 9),
            state=tk.DISABLED, bg=COLORS["term_bg"], fg=COLORS["term_fg"],
            insertbackground=COLORS["success"], relief=tk.FLAT, bd=0,
            padx=10, pady=8, highlightthickness=0,
            yscrollcommand=self._sync_scroll,
        )
        self.log_text.grid(row=1, column=1, sticky="nsew")

        def _on_yview(*args):
            self.log_text.yview_moveto(args[0])
            self.log_lineno.yview_moveto(args[0])

        self.log_text.config(yscrollcommand=_on_yview)
        self.log_text["yscrollcommand"] = _on_yview

        for tag, (fg,) in self._tags.items():
            self.log_text.tag_config(tag, foreground=fg)
        self.log_text.tag_config("scan_hl", background="#151a2e")

    # ==================================================================
    #  滚动同步 + 动画重绘
    # ==================================================================

    def _sync_scroll(self, *args):
        self.log_lineno.yview_moveto(args[0])

    def redraw_scanline(self) -> None:
        try: c = self.scan_canvas
        except AttributeError: return
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1 or h <= 1:
            return
        _draw_horizontal_gradient(
            c, 0, 0, w, h,
            (COLORS["term_bg"], COLORS["primary"], COLORS["term_bg"]),
        )
        line_x = int(w * self.scan_phase)
        for i in range(20, 0, -1):
            col = _lerp_color(COLORS["primary_2"], COLORS["term_bg"], i / 20)
            x1 = max(0, line_x - i * 4)
            x2 = min(w, line_x + i * 4)
            c.create_line(x1, 0, x2, 0, fill=col, width=2)

    def refresh_cursor_tag(self) -> None:
        try:
            self.log_text.tag_configure(
                "CURSOR_BLINK",
                background=COLORS["success"] if self.cursor_blink else "",
            )
        except Exception:
            pass

    # ==================================================================
    #  日志 drain 与 append
    # ==================================================================

    def drain_queue(self) -> None:
        while True:
            try:
                message, tag = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append(message, tag)

    def _append(self, message: str, tag: str) -> None:
        if tag not in self._tags:
            tag = "INFO"
        self.log_text.configure(state=tk.NORMAL)
        self.log_lineno.configure(state=tk.NORMAL)

        ts = time.strftime("%H:%M:%S")
        self._lineno_count += 1
        self.log_lineno.insert(tk.END, f"{self._lineno_count:>4}\n")

        self.log_text.insert(tk.END, "❯ ", "PROMPT")
        self.log_text.insert(tk.END, f"{ts}  ", "TIME")
        self.log_text.insert(tk.END, f"{message}\n", tag)

        self.log_text.see(tk.END)
        self.log_lineno.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)
        self.log_lineno.configure(state=tk.DISABLED)
        self.log_lines_var.set(f"{self._lineno_count} lines")
