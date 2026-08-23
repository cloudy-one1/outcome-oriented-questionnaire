"""权重配置表面板组件（第一章第 1 条拆分 · 7D 子步骤）。

封装：
    - "权重配置"卡片：探测题目占位符 → 按题型渲染行
    - 行结构：Q 号 · 类型胶囊 Badge · 选项/规模列 · 权重编辑列（单行 hover 高亮）
    - `build_weight_config()` ：导出 V2 cfg dict → config_io / 运行前构建
    - `restore_from_config(restored_w)` ：从 JSON 配置反向回填 GUI 表格

公共可变对象引用：
    - `self.questions`（list[dict]）—— 与 SurveyGUI.questions 为同一引用对象同步
    - `self.weight_entries`（dict[int, StringVar]）—— 与 SurveyGUI.weight_entries 共享
    设计原则：两个容器都使用原地修改（.clear() / .update() / 直接 append），
    因此 SurveyGUI 侧在初始化时把 panel.questions/panel.weight_entries 的
    引用直接赋给 self.questions/self.weight_entries 一次即可永久同步。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from .theme import COLORS, FONT_PRESETS, GRAD_SUCCESS, _lerp_color
from .widgets import _make_card as _default_make_card

_QTYPE_BADGE_MAP: dict[str, tuple[str, str]] = {
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

_FIELD_LABEL_MAP: dict[str, str] = {
    "name": "姓名", "phone": "手机", "email": "邮箱",
    "address": "地址", "age": "年龄", "company": "公司",
    "mobile": "手机", "tel": "手机", "addr": "地址", "org": "公司",
}


class WeightPanel:
    """「权重配置」卡片面板（题号 + 类型 badge + 选项数 + 可编辑权重）。"""

    # 公共引用（与 SurveyGUI 双向同步，见文档注释）
    questions: list[dict]
    weight_entries: dict[int, tk.StringVar]

    def __init__(
        self,
        root: tk.Tk,
        log_fn: Callable[[str, str], None],
        *,
        weight_config_global: dict,
        make_card_fn: Any = None,
        fonts: Any = None,
        # 同步容器：若 SurveyGUI 希望外部传入同一对象，可在此注入
        shared_questions: list[dict] | None = None,
        shared_weight_entries: dict[int, tk.StringVar] | None = None,
    ) -> None:
        self.root = root
        self.log = log_fn
        self._wc_global = weight_config_global
        self._make_card = make_card_fn or _default_make_card
        self._fonts = dict(fonts) if fonts is not None else dict(FONT_PRESETS)

        # 容器使用调用方传入的共享对象（若有）；否则新建，便于外部再赋回
        self.questions = shared_questions if shared_questions is not None else []
        self.weight_entries = (
            shared_weight_entries
            if shared_weight_entries is not None
            else {}
        )

        # ---- 内部 UI 引用（build 之后才设置） ----
        self.table_frame: tk.Frame | None = None

    # ==================================================================
    #  build + header + placeholder（原 _build_weight_table_card /
    #  _draw_table_header / _show_table_placeholder）
    # ==================================================================

    def build(self, parent: tk.Frame) -> None:
        body = self._make_card(parent, "权重配置", icon="📋", accent=GRAD_SUCCESS)
        body.pack(fill=tk.BOTH, expand=True)
        body.grid_rowconfigure(1, weight=1)
        body.grid_columnconfigure(0, weight=1)

        tip = tk.Frame(body, bg=COLORS["surface"])
        tip.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tk.Label(tip, text="💡", font=self._fonts["ICON"],
                 bg=COLORS["surface"], fg=COLORS["warning"]).pack(side=tk.LEFT)
        tk.Label(tip,
                 text="每题一行，权重用英文逗号分隔，例如  0.3 , 0.5 , 0.2   "
                      "（留空则使用等权重随机）",
                 font=self._fonts["SMALL"], fg=COLORS["text_dim"],
                 bg=COLORS["surface"], justify=tk.LEFT, wraplength=500
                 ).pack(side=tk.LEFT, padx=(4, 0))

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
        table_win = canvas.create_window((0, 0), window=self.table_frame, anchor="nw")

        def _resize(_e):
            canvas.itemconfigure(table_win, width=_e.width)

        canvas.bind("<Configure>", _resize)
        canvas.configure(yscrollcommand=scrollbar.set)

        def _wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        self.weight_canvas = canvas  # 保留给外部需要访问滚动容器的代码
        self.draw_header()

    def draw_header(self) -> None:
        assert self.table_frame is not None
        for w in self.table_frame.winfo_children():
            w.destroy()
        cols = [("题号", 1), ("类型", 2), ("选项/空数", 1), ("权重/答案文本（英文逗号分隔）", 5)]
        for i in range(4):
            self.table_frame.grid_columnconfigure(i, weight=cols[i][1])
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
        self.show_placeholder()

    def show_placeholder(self) -> None:
        assert self.table_frame is not None
        ph = tk.Frame(self.table_frame, bg=COLORS["surface"])
        ph.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=36)
        tk.Label(ph, text="📡", font=("Segoe UI Emoji", 36),
                 bg=COLORS["surface"], fg=COLORS["text_muted"]).pack()
        tk.Label(ph,
                 text="点击「探测题目」自动识别问卷结构",
                 font=self._fonts["NORMAL"],
                 bg=COLORS["surface"], fg=COLORS["text_dim"]).pack(pady=(6, 2))
        tk.Label(ph,
                 text="连接浏览器 → 分析单选/多选 → 生成权重编辑表格",
                 font=self._fonts["SMALL"],
                 bg=COLORS["surface"], fg=COLORS["text_muted"]).pack()

    # ==================================================================
    #  填充题目表格（原 _populate_weight_table，最大的一个方法 ~250 行）
    # ==================================================================

    def populate(self, questions: list[dict]) -> None:
        assert self.table_frame is not None
        for w in self.table_frame.winfo_children():
            w.destroy()
        self.weight_entries.clear()
        # 注意：这里直接改 self.questions 引用的 LIST 对象（共享引用）
        self.questions[:] = questions

        cols = [("题号", 1), ("类型", 2), ("选项/空数", 1), ("权重/答案文本（英文逗号分隔）", 5)]
        for i in range(4):
            self.table_frame.grid_columnconfigure(i, weight=cols[i][1])

        header_wrapper = tk.Frame(self.table_frame, bg=COLORS["surface"])
        header_wrapper.grid(row=0, column=0, columnspan=4, sticky="ew")
        for i, (text, _w) in enumerate(cols):
            header_wrapper.grid_columnconfigure(i, weight=_w)
            cell = tk.Frame(header_wrapper, bg=COLORS["table_head"], height=32)
            cell.grid(row=0, column=i, sticky="nsew", padx=(0, 1), pady=(0, 1))
            cell.pack_propagate(False)
            tk.Label(cell, text=text,
                     font=("Microsoft YaHei UI", 9, "bold"),
                     bg=COLORS["table_head"], fg="#004099",
                     anchor=tk.CENTER).pack(expand=True)

        for i, q in enumerate(questions):
            row = i + 1
            qi = q["q"]
            qtype = q["type"]
            stripe_bg = (
                COLORS["table_row_a"] if i % 2 == 0 else COLORS["table_row_b"]
            )

            # --- 默认值 / 规模描述：基于题型分支 ---
            if qtype in ("single", "multi", "radio", "checkbox", "dropdown"):
                n_opts = len(q.get("choices", []))
                default_weights = (
                    ",".join([f"{1.0 / n_opts:.4f}" for _ in range(n_opts)])
                    if n_opts > 0 else ""
                )
                n_label = f"{n_opts}"
            elif qtype in ("scale", "rating"):
                n_opts = int(q.get("scale", 5))
                smin = int(q.get("scale_min", 1))
                default_weights = ",".join(["1"] * n_opts)
                n_label = f"{smin}~{n_opts}"
            elif qtype in ("text", "input", "textarea", "fillblank"):
                opts_hint = q.get("options") or []
                default_weights = (
                    ",".join([str(x) for x in opts_hint]) if opts_hint else ""
                )
                fld = q.get("field")
                n_label = {
                    "name": "姓名字段", "phone": "手机字段", "mobile": "手机字段",
                    "tel": "手机字段", "email": "邮箱字段",
                    "address": "地址字段", "addr": "地址字段", "age": "年龄字段",
                    "company": "公司字段", "org": "公司字段",
                }.get(fld, "自由文本")
            elif qtype in ("matrix_single", "matrix"):
                rs = q.get("rows", [])
                cs = q.get("cols", [])
                default_weights = ""
                n_label = f"{len(rs)}行 × {len(cs)}列"
            else:
                n_opts = len(q.get("choices", []))
                default_weights = ",".join(["1"] * n_opts) if n_opts > 0 else ""
                n_label = f"{n_opts}"

            # 如果有全局配置 WEIGHT_CONFIG 中的记录，覆盖默认值
            existing = self._wc_global.get(qi)
            if existing:
                if "weights" in existing:
                    default_weights = ",".join(
                        [f"{w:.4f}" if isinstance(w, (int, float)) else str(w)
                         for w in existing["weights"]]
                    )
                elif "options" in existing and isinstance(existing["options"], list):
                    default_weights = ",".join([str(x) for x in existing["options"]])
                if "row_weights" in existing and isinstance(existing["row_weights"], dict):
                    lines = []
                    for rk in sorted(existing["row_weights"].keys(),
                                     key=lambda x: int(x) if str(x).isdigit() else str(x)):
                        wlst = existing["row_weights"][rk]
                        lines.append(f"{rk}:{','.join(str(x) for x in wlst)}")
                    if lines:
                        default_weights = " | ".join(lines)

            # --- 题号 ---
            qid_cell = tk.Frame(self.table_frame, bg=stripe_bg, height=34)
            qid_cell.grid(row=row, column=0, sticky="nsew", padx=(0, 1), pady=(0, 1))
            qid_cell.pack_propagate(False)
            tk.Label(qid_cell, text=f"Q{qi}",
                     font=("Cascadia Code", 10, "bold"),
                     bg=stripe_bg, fg=COLORS["primary_2"],
                     anchor=tk.CENTER).pack(expand=True)

            # --- 类型 Badge（胶囊） ---
            type_cell = tk.Frame(self.table_frame, bg=stripe_bg, height=34)
            type_cell.grid(row=row, column=1, sticky="nsew", padx=(0, 1), pady=(0, 1))
            type_cell.pack_propagate(False)

            badge_c, badge_text = _QTYPE_BADGE_MAP.get(
                qtype, (COLORS["primary"], qtype[:4].upper())
            )
            badge_fg = "white"
            badge_canvas = tk.Canvas(type_cell, height=22, width=78,
                                     bg=stripe_bg, highlightthickness=0, bd=0)
            badge_canvas.pack(expand=True, padx=(0, 2))

            def _draw_badge(_e=None, bc=badge_canvas, col=badge_c, fgcol=badge_fg,
                            txt=badge_text, bg_stripe=stripe_bg):
                bc.delete("all")
                w, h = bc.winfo_width(), bc.winfo_height()
                if w <= 1:
                    w, h = 78, 22
                r = h // 2 - 1
                for ii in range(3, 0, -1):
                    t = ii / 3
                    gcol = _lerp_color(col, bg_stripe, 0.5 + t * 0.4)
                    bc.create_rectangle(r - ii, 1 - ii, w - r + ii, h - 1 + ii,
                                        outline=gcol, width=1)
                bc.create_rectangle(r, 1, w - r, h - 1, fill=col, outline="")
                bc.create_oval(0, 1, 2 * r, h - 1, fill=col, outline="")
                bc.create_oval(w - 2 * r, 1, w, h - 1, fill=col, outline="")
                bc.create_text(w / 2, h / 2 + 1, text=txt,
                               font=("Microsoft YaHei UI", 8, "bold"), fill=fgcol)

            badge_canvas.bind("<Configure>", _draw_badge)
            self.root.after(10, _draw_badge)

            if qtype in ("text", "input", "textarea", "fillblank"):
                fld = q.get("field")
                if fld:
                    field_label = _FIELD_LABEL_MAP.get(fld, str(fld).upper()[:4])
                    mini = tk.Label(
                        type_cell, text=field_label,
                        font=("Microsoft YaHei UI", 7, "bold"),
                        bg="#e8ecf3", fg="#334155",
                        padx=5, pady=1,
                    )
                    mini.pack(side=tk.RIGHT, padx=(0, 4))

            # --- 选项数 / 规模描述列 ---
            ncell = tk.Frame(self.table_frame, bg=stripe_bg, height=34)
            ncell.grid(row=row, column=2, sticky="nsew", padx=(0, 1), pady=(0, 1))
            ncell.pack_propagate(False)
            tk.Label(ncell, text=n_label,
                     font=("Cascadia Code", 10),
                     bg=stripe_bg, fg=COLORS["text_soft"],
                     anchor=tk.CENTER).pack(expand=True)

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
                for cell in cells:
                    for child in cell.winfo_children():
                        try: child.configure(bg=COLORS["table_hover"])
                        except Exception: pass

            def _leave(_e, cells=all_cells, orig=stripe_bg):
                for c in cells:
                    try: c.configure(bg=orig)
                    except Exception: pass
                for cell in cells:
                    for child in cell.winfo_children():
                        try: child.configure(bg=orig)
                        except Exception: pass

            for c in all_cells:
                c.bind("<Enter>", _enter)
                c.bind("<Leave>", _leave)
                for child in c.winfo_children():
                    child.bind("<Enter>", _enter)
                    child.bind("<Leave>", _leave)

    # ==================================================================
    #  导出为 config（原 _build_weight_config）
    # ==================================================================

    def build_weight_config(self) -> dict:
        config: dict = {}
        for q in self.questions:
            qi = q["q"]
            qtype = str(q.get("type", "single")).lower()
            entry_var = self.weight_entries.get(qi)
            raw = entry_var.get().strip() if entry_var else ""

            # single / multi / radio / checkbox / dropdown
            if qtype in ("single", "radio", "multi", "checkbox", "dropdown"):
                if not raw:
                    continue
                try:
                    weights = [float(x.strip()) for x in raw.split(",") if x.strip()]
                except ValueError:
                    self.log(f"Q{qi} 权重格式错误，已跳过：{raw}", "WARN")
                    continue
                expected = len(q.get("choices", []))
                if qtype in ("single", "radio", "dropdown") and expected == 0:
                    pass
                elif expected > 0 and len(weights) != expected:
                    self.log(
                        f"Q{qi} 权重数({len(weights)}) != 选项数({expected}), 已跳过",
                        "WARN",
                    )
                    continue
                config[qi] = {"type": qtype, "weights": weights}
                continue

            # scale / rating
            if qtype in ("scale", "rating"):
                scale_max = int(q.get("scale", 5))
                weights = None
                if raw:
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
                            self.log(f"Q{qi} 量表权重格式错误，已跳过：{raw}", "WARN")
                            continue
                        if len(weights) != scale_max:
                            self.log(
                                f"Q{qi} 量表权重数({len(weights)}) != 级数({scale_max}), "
                                "已按现有长度裁剪/补 0",
                                "WARN",
                            )
                            if len(weights) < scale_max:
                                weights += [0.0] * (scale_max - len(weights))
                            else:
                                weights = weights[:scale_max]
                cfg = {"type": qtype, "scale": scale_max}
                if weights is not None:
                    cfg["weights"] = weights
                smin = q.get("scale_min")
                if smin:
                    cfg["scale_min"] = int(smin)
                config[qi] = cfg
                continue

            # text / input / textarea / fillblank
            if qtype in ("text", "input", "textarea", "fillblank"):
                cfg = {"type": qtype}
                fld = q.get("field")
                if fld:
                    cfg["field"] = fld
                if raw:
                    options = [s.strip() for s in raw.split(",") if s.strip()]
                    if options:
                        cfg["options"] = options
                config[qi] = cfg
                continue

            # matrix / matrix_single
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
                        self.log(
                            f"Q{qi} 矩阵行权重格式错误，跳过使用。"
                            "正确格式: 1:w1,w2,w3 | 2:w1,w2,w3",
                            "WARN",
                        )
                    elif row_weights:
                        cfg["row_weights"] = row_weights
                config[qi] = cfg
                continue

            # 未知类型兜底
            if raw:
                try:
                    weights = [float(x.strip()) for x in raw.split(",") if x.strip()]
                except ValueError:
                    self.log(f"Q{qi} 权重格式错误，已跳过：{raw}", "WARN")
                    continue
                config[qi] = {"type": qtype, "weights": weights}
        return config

    # ==================================================================
    #  从配置反向回填（原 _restore_weight_table_from_config）
    # ==================================================================

    def restore_from_config(self, restored_w: dict[int, dict]) -> None:
        """从持久化的 weight_config 重建 GUI 表格显示。

        V2.1 续传语义：
          - 若已探测过（self.questions 非空）：直接重新 populate 以同步结构
          - 否则按 cfg 反向构造最小化 questions，用于显示题号/类型/默认值

        反向构造规则：
          - single/multi/dropdown：choices 占位长度 = weights 长度
          - scale：优先 cfg.scale，否则取 weights 长度或默认 5
          - text：保留 field；options 作为候选预填
          - matrix_single/matrix：rows/cols/row_weights 原样保留
        """
        if not restored_w:
            return

        if self.questions:
            self.populate(list(self.questions))
            return

        reconstructed: list[dict] = []
        for qi in sorted(restored_w.keys()):
            cfg = restored_w[qi]
            if not isinstance(cfg, dict):
                continue
            qtype = cfg.get("type", "single")
            q: dict = {"q": qi, "type": qtype}

            if qtype in ("single", "radio", "multi", "checkbox", "dropdown"):
                weights = cfg.get("weights") or []
                q["choices"] = (
                    list(range(1, len(weights) + 1)) if weights else [1, 2]
                )
            elif qtype in ("scale", "rating"):
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
                row_weights = cfg.get("row_weights") or {}
                if row_weights:
                    rows = sorted(int(k) for k in row_weights.keys())
                    if not cols and row_weights:
                        first_rw = next(iter(row_weights.values()))
                        cols = (
                            list(range(1, len(first_rw) + 1))
                            if first_rw else [1, 2]
                        )
                q["rows"] = rows
                q["cols"] = cols
                q["choices"] = cols
            else:
                q["choices"] = cfg.get("choices") or [1, 2]

            reconstructed.append(q)

        if reconstructed:
            self.populate(reconstructed)
