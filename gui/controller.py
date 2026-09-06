"""GUI 命令控制器（第一章第 1 条拆分 · 7F 子步骤 · 第 1 批）。

封装「非批量运行」用户命令处理器：
    - save_config / save_default_config ：V2 config_io 导出
    - load_config ：载入 JSON、校验、写入 WEIGHT_CONFIG、同步 GUI 表格
    - auto_load_default_config ：启动时若存在默认文件则静默导入
    - import_qr ：本地二维码图片解析 → 填入 URL 输入框
    - on_questions_detected ：探测成功回调 → 重绘表格 + 汇总日志

设计契约（Host Facade）：SurveyGUI 需要提供以下属性以支持注入：
    - host.root / host.log(msg, tag) / host._set_status(text, color)
    - host.url_var / host.detect_btn / host.qr_btn
    - host.weight_entries / host.weight_panel / host.build_weight_config()
    - host.restore_weight_table_from_config(restored_w)
以及可选：
    - host._cfg_module（用于把 cfg 合并进全局 WEIGHT_CONFIG）

*运行循环(_run_loop / _on_start / _single_submission_worker)暂未迁移*
——因为第 9 步（第 8 章第 2 条）明确要求该段代码改用 RunState 抽象，
先在 controller 层提供命令处理器与线程骨架，后续再合并。
"""

from __future__ import annotations

import logging
import os
import threading
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING, Any, Callable

from selenium.webdriver.support.ui import WebDriverWait

# V2.4 修复：原先从 src.interactions 导入 detect_questions 等三个名字
# （它们实际在 src.detection / src.verification），且 src.utils.qr 路径不存在
# （二维码实现在 gui.qr_utils）——整个 try 块必然失败并被吞掉，
# 导致"探测题目 / 扫码导入"按钮静默失效。
try:
    from src.browser import create_driver
    from src.detection import detect_questions
    from src.verification import (
        is_smart_verification_showing,
        wait_for_manual_verification,
    )
    from .qr_utils import decode_qr_from_image
except Exception:  # pragma: no cover - 导入失败在方法内部会告警
    create_driver = None  # type: ignore[assignment]
    detect_questions = None  # type: ignore[assignment]
    is_smart_verification_showing = None  # type: ignore[assignment]
    wait_for_manual_verification = None  # type: ignore[assignment]
    decode_qr_from_image = None  # type: ignore[assignment]

from src.models import normalize_question_type  # noqa: E402

# 探测用"页面题目控件计数"JS（原先在 _worker 内重复 3 份，V2.4 收敛为一份）
_QUESTION_COUNT_JS: str = (
    "return ("
    "document.querySelectorAll('input[type=\"radio\"], input[type=\"checkbox\"]').length"
    " + document.querySelectorAll('input[type=\"text\"], textarea').length"
    " + document.querySelectorAll('select').length"
    " + document.querySelectorAll('div.ui-slider, div.star, div.question-rating, div.scale-span').length"
    " + document.querySelectorAll('div.field div.label').length"
    ");"
)

# 存储名 → 展示标签（配合 models.normalize_question_type 使用）
_TYPE_LABELS: dict[str, str] = {
    "single": "单选",
    "multi": "多选",
    "dropdown": "下拉",
    "scale": "量表",
    "text": "填空",
    "matrix": "矩阵",
}


if TYPE_CHECKING:
    import tkinter as tk

# V2.4：静默降级路径统一走 logger.debug 留痕（详见 src/logging_setup.py）
logger = logging.getLogger("wjx.gui.controller")


class GuiController:
    """非运行循环命令处理器集合；通过 host facade 反向操作 GUI。"""

    def __init__(
        self,
        host: Any,
        *,
        project_root: str,
        has_config_io: bool,
        save_weight_config: Callable | None,
        load_weight_config: Callable | None,
        validate_weight_config: Callable | None,
        default_config_dir: str,
        default_weight_config_path: str,
    ) -> None:
        self.host = host
        self._project_root = project_root
        self._has_config_io = has_config_io
        self._save_wc = save_weight_config
        self._load_wc = load_weight_config
        self._validate_wc = validate_weight_config
        self._default_cfg_dir = default_config_dir
        self._default_cfg_path = default_weight_config_path

    # ==================================================================
    #  内部便捷方法
    # ==================================================================

    def _log(self, msg: str, tag: str = "INFO") -> None: self.host._log(msg, tag)

    def _set_status(self, text: str, color: str) -> None:
        self.host._set_status(text, color)

    def _format_weights_for_entry(self, qcfg: dict) -> str:
        weights = qcfg.get("weights")
        options = qcfg.get("options")
        row_weights = qcfg.get("row_weights")
        if weights:
            return ",".join(
                [f"{w:.4f}" if isinstance(w, (int, float)) else str(w)
                 for w in weights]
            )
        if options:
            return ",".join(str(x) for x in options)
        if row_weights and isinstance(row_weights, dict):
            lines = []
            for rk in sorted(row_weights.keys(),
                             key=lambda x: int(x) if str(x).isdigit() else str(x)):
                wlst = row_weights[rk]
                lines.append(f"{rk}:{','.join(str(x) for x in wlst)}")
            return " | ".join(lines)
        return ""

    # ==================================================================
    #  配置 IO 导出 / 导入 / 默认载入
    # ==================================================================

    def on_save_config(self) -> None:
        if not self._has_config_io or self._save_wc is None:
            self._log("未加载 src/config_io，无法导出配置", "FAIL")
            return
        cfg = self.host._build_weight_config()
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
                "survey_url": self.host.url_var.get().strip()[:200],
            }
            if self._validate_wc is not None:
                warnings = self._validate_wc(cfg)
                if warnings:
                    self._log(
                        f"导出前校验发现 {len(warnings)} 条警告，首条：{warnings[0]}",
                        "WARN",
                    )
            self._save_wc(filepath, cfg, meta=meta)
            rel = os.path.relpath(filepath, self._project_root)
            self._log(f"✓ 已导出配置 → {rel}", "OK")
        except Exception as e:
            self._log(f"导出配置失败: {type(e).__name__}: {e}", "FAIL")

    def on_save_default_config(self) -> None:
        if not self._has_config_io or self._save_wc is None:
            self._log("未加载 src/config_io，无法另存默认配置", "FAIL")
            return
        cfg = self.host._build_weight_config()
        if not cfg:
            self._log("当前没有可保存的权重配置", "WARN")
            return
        try:
            if not os.path.exists(self._default_cfg_dir):
                os.makedirs(self._default_cfg_dir, exist_ok=True)
            meta = {
                "name": "default_weight_config",
                "description": f"GUI 另存默认 · 共 {len(cfg)} 道题",
                "survey_url": self.host.url_var.get().strip()[:200],
            }
            self._save_wc(self._default_cfg_path, cfg, meta=meta)
            rel = os.path.relpath(self._default_cfg_path, self._project_root)
            self._log(f"✓ 已另存默认配置 → {rel}", "OK")
        except Exception as e:
            self._log(f"另存默认配置失败: {type(e).__name__}: {e}", "FAIL")

    def on_load_config(self, path: str | None = None) -> None:
        if not self._has_config_io or self._load_wc is None:
            self._log("未加载 src/config_io，无法导入配置", "FAIL")
            return
        if path is None:
            path = filedialog.askopenfilename(
                title="导入权重配置",
                filetypes=[("JSON 配置", "*.json"), ("所有文件", "*.*")],
                initialdir=(
                    self._default_cfg_dir
                    if os.path.exists(self._default_cfg_dir)
                    else self._project_root
                ),
            )
            if not path:
                return
        if not os.path.exists(path):
            self._log(f"配置文件不存在: {path}", "FAIL")
            return
        try:
            cfg, meta = self._load_wc(path)
        except Exception as e:
            self._log(f"读取配置失败: {type(e).__name__}: {e}", "FAIL")
            return

        if self._validate_wc is not None:
            warnings = self._validate_wc(cfg)
            for w in warnings:
                self._log(f"[校验警告] {w}", "WARN")
            self._log(f"配置载入 · 校验警告 {len(warnings)} 条", "INFO")

        # 1. 合并到全局 WEIGHT_CONFIG
        cfg_module = getattr(self.host, "_cfg_module", None)
        if cfg_module is not None:
            cfg_module.WEIGHT_CONFIG.update(cfg)

        # 2. 同步 GUI 表格（仅刷新当前已探测到的题号对应的 entry_var）
        applied_cnt = 0
        for qi, qcfg in cfg.items():
            entry_var = self.host.weight_entries.get(qi)
            if entry_var is None:
                continue
            rendered = self._format_weights_for_entry(qcfg)
            if rendered:
                entry_var.set(rendered)
            applied_cnt += 1

        meta_name = meta.get("name") or os.path.basename(path)
        self._log(
            f"✓ 已载入「{meta_name}」· 配置 {len(cfg)} 道 · "
            f"同步到表格 {applied_cnt} 道",
            "OK",
        )

    def auto_load_default_config(self) -> None:
        if not (os.path.exists(self._default_cfg_path) and self._has_config_io):
            return
        try:
            self.on_load_config(path=self._default_cfg_path)
        except Exception as e:
            self._log(
                f"[启动] 自动载入默认配置跳过: {type(e).__name__}: {e}",
                "WARN",
            )

    # ==================================================================
    #  二维码
    # ==================================================================

    def on_import_qr(self) -> None:
        if decode_qr_from_image is None:
            self._log("二维码模块未加载，无法导入", "FAIL")
            return
        filepath = filedialog.askopenfilename(
            title="选择二维码图片",
            filetypes=[
                ("图片文件", "*.png *.jpg *.jpeg *.bmp *.gif"),
                ("所有文件", "*.*"),
            ],
        )
        if not filepath:
            return
        self._set_status("正在解析二维码...", "#b38700")  # =COLORS.warning
        self._log(f"正在解析二维码: {os.path.basename(filepath)}", "INFO")
        result = decode_qr_from_image(filepath)
        if result:
            self.host.url_var.set(result)
            self._log(f"二维码解析成功 ✓: {result}", "OK")
            self._set_status("就绪", "#5a6b85")  # =COLORS.text_dim
        else:
            self._log("未识别到二维码内容 ✗", "FAIL")
            self._set_status("就绪", "#5a6b85")

    # ==================================================================
    #  探测题目（worker 骨架，主线程回调给 host.on_questions_detected）
    # ==================================================================

    def on_detect_questions(self) -> None:
        if create_driver is None or detect_questions is None:
            self._log("探测模块未加载，请检查 src 导入", "FAIL")
            return
        import time  # localize
        url = self.host.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请先填写问卷 URL")
            return
        try: self.host.detect_btn.configure(state="disabled")
        except Exception:
            logger.debug("detect_btn 置灰失败（忽略）", exc_info=True)
        try: self.host.qr_btn.configure(state="disabled")
        except Exception:
            logger.debug("qr_btn 置灰失败（忽略）", exc_info=True)
        self._set_status("正在探测题目...", "#b38700")  # =COLORS.warning
        self._log("正在连接问卷页面，探测题目结构...", "HEADER")

        def _worker():
            import time as _t
            driver = None
            try:
                driver = create_driver(
                    self.host.browser_var.get(),
                    use_uc=bool(getattr(self.host, "use_uc_var", None)
                                and self.host.use_uc_var.get()),
                )
                driver.get(url)
                try:
                    WebDriverWait(driver, 25).until(
                        lambda d: d.execute_script(
                            "return document.readyState"
                        ) == "complete"
                    )
                except Exception:
                    logger.debug("探测页 readyState 等待未完成（继续探测）", exc_info=True)
                _t.sleep(2)

                if (is_smart_verification_showing is not None
                        and is_smart_verification_showing(driver)):
                    self._log("检测到验证码，请在浏览器中手动完成...", "WARN")
                    if wait_for_manual_verification is not None:
                        if not wait_for_manual_verification(driver):
                            self._log("验证超时，探测失败", "FAIL")
                            return

                has_any_q = driver.execute_script(_QUESTION_COUNT_JS)
                if not has_any_q:
                    iframes = driver.execute_script(
                        "return document.querySelectorAll('iframe').length"
                    )
                    found_frame = False
                    for i in range(iframes):
                        driver.switch_to.frame(i)
                        in_frame = driver.execute_script(_QUESTION_COUNT_JS)
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
                        lambda d: d.execute_script(_QUESTION_COUNT_JS) > 0
                    )
                except Exception:
                    logger.debug("等待题目控件渲染超时（继续探测）", exc_info=True)

                questions = detect_questions(driver)
                driver.switch_to.default_content()
                if not questions:
                    self._log("未探测到任何题目", "FAIL")
                    return
                self.host.root.after(
                    0, lambda: self.host._on_questions_detected(questions)
                )
            except Exception as e:
                self._log(f"探测失败: {type(e).__name__}: {e}", "FAIL")
            finally:
                if driver:
                    try: driver.quit()
                    except Exception:
                        logger.debug("探测收尾 driver.quit() 失败（忽略）", exc_info=True)
                self.host.root.after(
                    0, lambda: self._safe_conf(self.host, "detect_btn", "normal")
                )
                self.host.root.after(
                    0, lambda: self._safe_conf(self.host, "qr_btn", "normal")
                )
                # text_dim fallback: call host._set_status directly
                self.host.root.after(
                    0, lambda: self._set_status("就绪", "#5a6b85")
                )

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def _safe_conf(host: Any, attr: str, state: str) -> None:
        try:
            w = getattr(host, attr, None)
            if w is not None:
                w.configure(state=state)
        except Exception:
            pass

    # ==================================================================
    #  探测题目 → 汇总日志 + 刷新 weight_entries
    # ==================================================================

    def on_questions_detected(self, questions: list[dict]) -> None:
        self.host._populate_weight_table(questions)
        counts: dict[str, int] = {}
        for q in questions:
            # V2.4 整改：别名归一化统一走 models.normalize_question_type（单一真相）
            bucket = _TYPE_LABELS.get(normalize_question_type(q.get("type")))
            if bucket:
                counts[bucket] = counts.get(bucket, 0) + 1
        parts = [f"{v} {k}" for k, v in counts.items()]
        summary = "、".join(parts) if parts else "未识别题型"
        self._log(
            f"探测完成，共 {len(questions)} 题（{summary}）",
            "OK",
        )
        self._log("请在表格中调整权重后点击「开始运行」", "INFO")
