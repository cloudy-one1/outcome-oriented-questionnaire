"""历史记录面板组件（第一章第 1 条拆分 · 7C 子步骤）。

封装：
    - runs + answers 双表 Treeview 布局
    - 顶部工具栏（刷新 / 导出 CSV / 清理 7 天前）
    - 懒打开 SubmissionHistory 数据库
    - 刷新 / 选 run 看明细 / 导出 CSV / 清理旧数据 四种动作

设计要点：
    1. 与 SurveyGUI 的唯一耦合点是构造时注入的回调（log_fn / make_card_fn /
       make_icon_fn）与属性。所有后续业务方法与 SurveyGUI 自身已完全解耦，
       可以单独起一个根窗口把 HistoryPanel 嵌进去做 widget 级 smoke 测试。
    2. 暴露的属性名（summary_var / runs_tree / ans_tree /
       answer_head_var）与原先挂在 SurveyGUI 上的属性名保持一一对应，
       便于在 app.py 里做「属性别名转发」，避免其它代码路径硬改名。
"""

from __future__ import annotations

import csv
import os
import tkinter as tk
from tkinter import filedialog, ttk
from typing import TYPE_CHECKING, Any, Callable

from collections.abc import Mapping as _FONTS_KIND  # 局部别名：字体参数字典类型

from .theme import COLORS, FONT_PRESETS, GRAD_PRIMARY
from .widgets import _make_card as _default_make_card
from .widgets import _make_icon_button as _default_make_icon_btn

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型注解，避免循环 import
    from src.history import SubmissionHistory  # type: ignore


class HistoryPanel:
    """「历史记录」卡片面板：runs 表 + answers 表 + 工具栏。"""

    # ---------- 公开属性（替代原先挂在 SurveyGUI 上的 self.history_*） ----------
    summary_var: tk.StringVar
    answer_head_var: tk.StringVar
    runs_tree: ttk.Treeview
    ans_tree: ttk.Treeview

    def __init__(
        self,
        root: tk.Tk,
        log_fn: Callable[[str, str], None],
        *,
        has_history: bool,
        submission_history_cls: "type[SubmissionHistory] | None",
        history_db_path: str,
        make_card_fn: Any = None,
        make_icon_btn_fn: Any = None,
        fonts: _FONTS_KIND | None = None,
    ) -> None:
        self.root = root
        self.log = log_fn
        self._has_history = has_history
        self._sh_cls = submission_history_cls
        self._db_path = history_db_path
        self._make_card = make_card_fn or _default_make_card
        self._make_btn = make_icon_btn_fn or _default_make_icon_btn
        self._fonts = dict(fonts) if fonts is not None else dict(FONT_PRESETS)

    # ==================================================================
    #  历史数据库懒加载（原 _history_get_db）
    # ==================================================================

    def get_db(self) -> "SubmissionHistory | None":
        """懒构造 SubmissionHistory。避免没选 SQLite 驱动时崩溃。"""
        if not self._has_history or self._sh_cls is None:
            return None
        try:
            dirname = os.path.dirname(self._db_path)
            if dirname and not os.path.exists(dirname):
                os.makedirs(dirname, exist_ok=True)
            return self._sh_cls(self._db_path)
        except Exception as e:
            self.log(
                f"历史记录数据库打开失败: {type(e).__name__}: {e}", "WARN"
            )
            return None

    # ==================================================================
    #  构建 UI（原 _build_history_card）
    # ==================================================================

    def build(self, parent: tk.Frame) -> None:
        """把 HistoryPanel 挂到 parent（内部会自行调用 _make_card）。"""
        body = self._make_card(parent, "历史记录", icon="📜", accent=GRAD_PRIMARY)
        body.pack(fill=tk.BOTH, expand=True)
        body.grid_rowconfigure(1, weight=1)
        body.grid_rowconfigure(3, weight=1)
        body.grid_columnconfigure(0, weight=1)

        # ---- 顶部工具栏 ----
        tb = tk.Frame(body, bg=COLORS["surface"])
        tb.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.summary_var = tk.StringVar(value="—")
        tk.Label(tb, textvariable=self.summary_var,
                 font=self._fonts["SMALL"], fg=COLORS["primary_2"],
                 bg=COLORS["surface"]).pack(side=tk.LEFT, padx=(10, 10))

        if not self._has_history:
            tk.Label(tb, text="⚠ src.history 未加载，历史记录不可用",
                     font=self._fonts["SMALL"], fg=COLORS["warning"],
                     bg=COLORS["surface"]).pack(side=tk.RIGHT, padx=10)

        btns = tk.Frame(tb, bg=COLORS["surface"])
        btns.pack(side=tk.RIGHT, padx=(0, 6))
        btn_refresh = self._make_btn(
            btns, "🔄 刷新", accent="ghost",
            command=self.refresh,
        )
        btn_export = self._make_btn(
            btns, "📤 导出 CSV", accent="ghost",
            command=self.export_csv,
        )
        btn_purge = self._make_btn(
            btns, "🗑 清理 7 天前", accent="danger",
            command=self.purge_old,
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
        self.runs_tree = ttk.Treeview(
            runs_frame, columns=run_cols, show="headings", height=6,
        )
        head = {"id": ("ID", 55), "started": ("开始", 150),
                "finished": ("结束", 150), "status": ("状态", 62),
                "total": ("总", 42), "ok": ("✓", 42),
                "fail": ("✕", 42), "url": ("URL", 280)}
        for col, (txt, w) in head.items():
            self.runs_tree.heading(col, text=txt)
            self.runs_tree.column(col, width=w, anchor="w",
                                  stretch=(col == "url"))
        run_scroll = ttk.Scrollbar(runs_frame, orient="vertical",
                                   command=self.runs_tree.yview)
        self.runs_tree.configure(yscrollcommand=run_scroll.set)
        self.runs_tree.grid(row=0, column=0, sticky="nsew")
        run_scroll.grid(row=0, column=1, sticky="ns")
        self.runs_tree.bind("<<TreeviewSelect>>", self.select_run)

        # ---- 标签：选中 run 的 answers 表 ----
        ans_header = tk.Label(body, text="🔍  答题明细 （点击上方 Run 查看）",
                              font=("Microsoft YaHei UI", 10, "bold"),
                              fg=COLORS["text_soft"],
                              bg=COLORS["surface"], anchor="w",
                              padx=10, pady=4)
        ans_header.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        self.answer_head_var = tk.StringVar(value="（未选中运行记录）")
        tk.Label(body, textvariable=self.answer_head_var,
                 font=self._fonts["SMALL"], fg=COLORS["text_dim"],
                 bg=COLORS["surface"], anchor="e", padx=10
                 ).grid(row=2, column=0, sticky="e")

        ans_frame = tk.Frame(body, bg=COLORS["bg_mid"],
                             highlightthickness=1,
                             highlightbackground=COLORS["border_dim"])
        ans_frame.grid(row=3, column=0, sticky="nsew")
        ans_frame.grid_rowconfigure(0, weight=1)
        ans_frame.grid_columnconfigure(0, weight=1)

        ans_cols = ("q", "type", "selected", "text_ans", "elapsed", "time")
        self.ans_tree = ttk.Treeview(
            ans_frame, columns=ans_cols, show="headings", height=10,
        )
        an_head = {"q": ("Q", 50), "type": ("题型", 75),
                   "selected": ("选项", 260), "text_ans": ("文本答案", 260),
                   "elapsed": ("耗时ms", 85), "time": ("记录时间", 145)}
        for col, (txt, w) in an_head.items():
            self.ans_tree.heading(col, text=txt)
            self.ans_tree.column(col, width=w, anchor="w",
                                 stretch=(col in ("selected", "text_ans")))
        ans_scroll = ttk.Scrollbar(ans_frame, orient="vertical",
                                   command=self.ans_tree.yview)
        self.ans_tree.configure(yscrollcommand=ans_scroll.set)
        self.ans_tree.grid(row=0, column=0, sticky="nsew")
        ans_scroll.grid(row=0, column=1, sticky="ns")

        # 启动后 300ms 刷新一次（避免日志区还没 ready）
        self.root.after(300, self.refresh)

    # ==================================================================
    #  刷新 runs 表（原 _history_refresh）
    # ==================================================================

    def refresh(self) -> None:
        db = self.get_db()
        tree = getattr(self, "runs_tree", None)
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
                if status in ("done", "success", "finished"):
                    tag: tuple = ("status_ok",)
                elif status in ("fail", "error", "stopped"):
                    tag = ("status_fail",)
                else:
                    tag = ()
                tree.insert("", tk.END, iid=str(rid),
                            values=(rid, started, finished, status,
                                    tot, ok, fail, url), tags=tag)
            # 颜色样式
            tree.tag_configure("status_ok",
                               background="#eaffef", foreground="#0b5c1e")
            tree.tag_configure("status_fail",
                               background="#fff1f0", foreground="#8a1e1e")
            n_runs = len(runs)
            self.summary_var.set(
                f"共 {n_runs} 次运行 · 累计提交 {total_cnt} "
                f"(✓ {ok_cnt} / ✕ {fail_cnt})"
            )
        except Exception as e:
            self.log(f"刷新历史记录失败: {type(e).__name__}: {e}", "WARN")

    # ==================================================================
    #  点击 run → 答题明细（原 _history_select_run）
    # ==================================================================

    def select_run(self, _e=None) -> None:
        db = self.get_db()
        tree_runs = getattr(self, "runs_tree", None)
        tree_ans = getattr(self, "ans_tree", None)
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
            self.answer_head_var.set(
                f"Run #{run_id} · 答题数 {len(answers)}"
            )
        except Exception as e:
            self.log(f"读取答题明细失败: {type(e).__name__}: {e}", "WARN")

    # ==================================================================
    #  导出 CSV（原 _history_export_csv）
    # ==================================================================

    def export_csv(self) -> None:
        db = self.get_db()
        if db is None:
            self.log("未加载 src.history，无法导出", "WARN")
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
            runs_path = path
            base, ext = os.path.splitext(path)
            ans_path = f"{base}_answers{ext}"
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
            self.log(f"✓ 已导出 runs → {os.path.basename(runs_path)}", "OK")
            self.log(f"✓ 已导出 answers → {os.path.basename(ans_path)}", "OK")
        except Exception as e:
            self.log(f"导出 CSV 失败: {type(e).__name__}: {e}", "FAIL")

    # ==================================================================
    #  清理 7 天前（原 _history_purge_old）
    # ==================================================================

    def purge_old(self) -> None:
        db = self.get_db()
        if db is None:
            self.log("未加载 src.history，无法清理", "WARN")
            return
        try:
            removed = db.purge_old(days=7)
            self.log(f"已清理 {removed} 条 7 天前的运行记录", "OK")
            self.refresh()
        except Exception as e:
            self.log(f"清理旧历史失败: {type(e).__name__}: {e}", "FAIL")
