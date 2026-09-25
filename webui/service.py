"""``webui`` 的命令层 —— 由 ``gui/controller.py`` 的 7 个命令改写而来。

设计稿：``docs/design/DESIGN_webui.md`` §3。

与 Tk 宿主的三处结构性差异，都是"宿主不同"而非"行为不同"：

  - ``host.root.after(0, fn)`` → 直接 ``session.emit(...)``。webui 没有"主线程亲和性"
    这回事，worker 写完队列就走，所以 Tk 里那串"控件已销毁就静默吞掉"的
    ``except Exception: pass`` 在这里根本不会出现。
  - 按钮 ``configure(state="disabled")`` → ``session.begin_command/end_command`` 的 busy 集合。
  - ``popup_warning("请先填写问卷 URL")`` → 抛 ``ValidationError``，由 api 层翻成 400。
    弹窗是模态阻塞，HTTP 里没有对应物，也不该有。

外部依赖全部从构造函数注入（``create_driver`` / ``detect_questions`` / ``decode_qr`` /
``spawn`` / ``build_config`` …），默认值才是真实现 —— Tk 那份是模块级 try 导入 +
运行时判空，测的时候只能 monkeypatch 全局，这里不用。

**``_QUESTION_COUNT_JS`` 与 ``_TYPE_LABELS`` 刻意复制一份而不是 import gui 那份**：
本轮 Tk 是参照实现，共用常量就等于让对拍提前失去独立性（设计稿 §10 步骤 4 的教训）。
两份不一致会被步骤 8 的对拍测试抓出来。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from src.config_io import apply_weight_config
from src.models import normalize_question_type
from webui.session import RunSession, ValidationError

logger = logging.getLogger("wjx.webui.service")

try:  # selenium 是可选的：缺了只是探测/扫码不可用，配置与历史照常
    from selenium.webdriver.support.ui import WebDriverWait

    from src.browser import create_driver as _create_driver
    from src.detection import detect_questions as _detect_questions
    from src.verification import (
        is_smart_verification_showing as _is_smart_verification_showing,
        wait_for_manual_verification as _wait_for_manual_verification,
    )

    SELENIUM_AVAILABLE = True
except Exception:  # pragma: no cover - 无 selenium 的环境
    WebDriverWait = None  # type: ignore[assignment]
    _create_driver = None  # type: ignore[assignment]
    _detect_questions = None  # type: ignore[assignment]
    _is_smart_verification_showing = None  # type: ignore[assignment]
    _wait_for_manual_verification = None  # type: ignore[assignment]
    SELENIUM_AVAILABLE = False

try:
    from gui.qr_utils import decode_qr_from_image as _decode_qr
except Exception:  # pragma: no cover
    _decode_qr = None  # type: ignore[assignment]

_QUESTION_COUNT_JS: str = (
    "return ("
    "document.querySelectorAll('input[type=\"radio\"], input[type=\"checkbox\"]').length"
    " + document.querySelectorAll('input[type=\"text\"], textarea').length"
    " + document.querySelectorAll('select').length"
    " + document.querySelectorAll('div.ui-slider, div.star, div.question-rating, div.scale-span').length"
    " + document.querySelectorAll('div.field div.label').length"
    ");"
)

_TYPE_LABELS: dict[str, str] = {
    "single": "单选",
    "multi": "多选",
    "dropdown": "下拉",
    "scale": "量表",
    "text": "填空",
    "matrix": "矩阵",
    "matrix_multi": "矩多",
    "sort": "排序",
}

READY_STATUS = "就绪"
DETECT_STATUS = "正在探测题目..."
QR_STATUS = "正在解析二维码..."


def _spawn_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True).start()


def _relative_to(path: str, root: str) -> str:
    """日志里报相对路径；跨盘符时 ``relpath`` 会抛 ValueError，退回绝对路径。

    Tk 那份没有这层保护：导出**已经成功**之后因为一句日志把整条命令报成 FAIL。
    """
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


class WebService:
    """探测 / 扫码 / 配置三件套 / 探测回调。运行循环在步骤 3 之后另说。"""

    def __init__(
        self,
        session: RunSession,
        *,
        save_weight_config: Callable[..., None] | None = None,
        load_weight_config: Callable[..., Any] | None = None,
        validate_weight_config: Callable[[dict], list[str]] | None = None,
        build_config: Callable[[], dict] | None = None,
        create_driver: Callable[..., Any] | None = _create_driver,
        detect_questions: Callable[[Any], list[dict]] | None = _detect_questions,
        is_smart_verification_showing: Callable[[Any], bool] | None = (
            _is_smart_verification_showing
        ),
        wait_for_manual_verification: Callable[[Any], bool] | None = (
            _wait_for_manual_verification
        ),
        decode_qr: Callable[[str], str | None] | None = _decode_qr,
        spawn: Callable[[Callable[[], None]], None] = _spawn_thread,
        page_ready_timeout: int = 25,
        question_ready_timeout: int = 15,
        settle_seconds: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session
        self._save_wc = save_weight_config
        self._load_wc = load_weight_config
        self._validate_wc = validate_weight_config
        self._build_cfg = build_config
        self._create_driver = create_driver
        self._detect_questions = detect_questions
        self._verification_showing = is_smart_verification_showing
        self._wait_verification = wait_for_manual_verification
        self._decode_qr = decode_qr
        self._spawn = spawn
        self._page_ready_timeout = page_ready_timeout
        self._question_ready_timeout = question_ready_timeout
        self._settle_seconds = settle_seconds
        self._sleep = sleeper

    # ------------------------------------------------------------ 内部

    def _log(self, msg: str, tag: str = "INFO") -> None:
        self.session.log(msg, tag)

    def _save_ready(self, action: str) -> bool:
        if not self.session.availability.config_io or self._save_wc is None:
            self._log(f"未加载 src/config_io，无法{action}配置", "FAIL")
            return False
        return True

    def current_config_for_export(self) -> dict | None:
        """给 api 层用的公开入口：拿当前权重表对应的 cfg，拿不到就返回 None。"""
        return self._current_config()

    def _current_config(self) -> dict | None:
        if self._build_cfg is None:
            self._log("权重解析尚未接线（设计稿 §10 步骤 4），无法导出配置", "FAIL")
            return None
        cfg = self._build_cfg()
        if not cfg:
            self._log("当前没有可导出的权重配置（请先探测题目）", "WARN")
            return None
        return cfg

    # ------------------------------------------------------------ 配置 IO

    def export_config(self, filepath: str) -> None:
        if not self._save_ready("导出"):
            return
        cfg = self._current_config()
        if cfg is None:
            return
        try:
            meta = {
                "name": os.path.splitext(os.path.basename(filepath))[0],
                "description": f"Web 界面导出 · 共 {len(cfg)} 道题",
                "survey_url": self.session.url[:200],
            }
            warnings = self._validate_wc(cfg) if self._validate_wc else []
            if warnings:
                self._log(
                    f"导出前校验发现 {len(warnings)} 条警告，首条：{warnings[0]}", "WARN"
                )
            self._save_wc(filepath, cfg, meta=meta)  # type: ignore[misc]
            self._log(
                f"✓ 已导出配置 → {_relative_to(filepath, self.session.paths.user_data_root)}",
                "OK",
            )
        except Exception as e:
            self._log(f"导出配置失败: {type(e).__name__}: {e}", "FAIL")

    def save_default_config(self) -> None:
        if not self._save_ready("另存默认"):
            return
        cfg = self._current_config()
        if cfg is None:
            return
        path = self.session.paths.default_config_path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            meta = {
                "name": "default_weight_config",
                "description": f"Web 界面另存默认 · 共 {len(cfg)} 道题",
                "survey_url": self.session.url[:200],
            }
            self._save_wc(path, cfg, meta=meta)  # type: ignore[misc]
            self._log(f"✓ 已另存默认配置 → {_relative_to(path, self.session.paths.user_data_root)}",
                      "OK")
        except Exception as e:
            self._log(f"另存默认配置失败: {type(e).__name__}: {e}", "FAIL")

    def import_config(self, path: str) -> None:
        if not (self.session.availability.config_io and self._load_wc is not None):
            self._log("未加载 src/config_io，无法导入配置", "FAIL")
            return
        if not os.path.exists(path):
            self._log(f"配置文件不存在: {path}", "FAIL")
            return
        try:
            cfg, meta = self._load_wc(path)  # type: ignore[misc]
        except Exception as e:
            self._log(f"读取配置失败: {type(e).__name__}: {e}", "FAIL")
            return

        warnings = self._validate_wc(cfg) if self._validate_wc else []
        for w in warnings:
            self._log(f"[校验警告] {w}", "WARN")
        self._log(f"配置载入 · 校验警告 {len(warnings)} 条", "INFO")

        # 整体替换语义（与 CLI --config 同一份实现）：合并会让上一份配置里
        # 本份没有的题号静默残留，于是"载入配置"的结果取决于你之前载入过什么。
        apply_weight_config(cfg, replace=True)

        synced = 0
        for qi, qcfg in cfg.items():
            rendered = format_weights_for_entry(qcfg)
            if qi in self.session.weight_texts and rendered:
                self.session.weight_texts[qi] = rendered
                synced += 1
        meta_name = meta.get("name") or os.path.basename(path)
        self._log(
            f"✓ 已载入「{meta_name}」· 配置 {len(cfg)} 道 · 同步到表格 {synced} 道",
            "OK",
        )
        self.session.emit("state", self.session.snapshot())

    def auto_load_default_config(self) -> None:
        path = self.session.paths.default_config_path
        if not (os.path.exists(path) and self.session.availability.config_io):
            return
        try:
            self.import_config(path)
        except Exception as e:  # 启动期任何失败都只降级，不拦启动
            self._log(f"[启动] 自动载入默认配置跳过: {type(e).__name__}: {e}", "WARN")

    # ------------------------------------------------------------ 二维码

    def import_qr(self, filepath: str) -> None:
        if self._decode_qr is None:
            self._log("二维码模块未加载，无法导入", "FAIL")
            return
        self.session.begin_command("qr")
        self.session.set_status(QR_STATUS)
        self._log(f"正在解析二维码: {os.path.basename(filepath)}", "INFO")
        try:
            result = self._decode_qr(filepath)
        except Exception as e:
            self._log(f"二维码解析失败: {type(e).__name__}: {e}", "FAIL")
            result = None
        if result:
            try:
                self.session.set_field("url", result)
            except ValidationError as e:
                self._log(f"二维码解出的内容不是合法 URL: {e}", "FAIL")
            else:
                self._log(f"二维码解析成功 ✓: {result}", "OK")
        else:
            self._log("未识别到二维码内容 ✗", "FAIL")
        self.session.set_status(READY_STATUS)
        self.session.end_command("qr")

    # ------------------------------------------------------------ 探测题目

    def detect_questions(self) -> None:
        if self._create_driver is None or self._detect_questions is None:
            self._log("探测模块未加载，请检查 src 导入", "FAIL")
            return
        url = self.session.url.strip()
        if not url:
            raise ValidationError("请先填写问卷 URL")
        if self.session.is_busy("detect"):
            return
        self.session.begin_command("detect")
        self.session.set_status(DETECT_STATUS)
        self._log("正在连接问卷页面，探测题目结构...", "HEADER")
        self._spawn(lambda: self._detect_worker(url))

    def _detect_worker(self, url: str) -> None:
        driver = None
        try:
            create = self._create_driver
            detect = self._detect_questions
            if create is None or detect is None:  # pragma: no cover - 只为类型收窄
                return
            driver = create(self.session.browser, use_uc=bool(self.session.use_uc))
            driver.get(url)
            self._wait_page_ready(driver)
            self._sleep(self._settle_seconds)
            if not self._pass_verification(driver):
                return
            if not self._locate_question_area(driver):
                return
            questions = detect(driver)
            driver.switch_to.default_content()
            if not questions:
                self._log("未探测到任何题目", "FAIL")
                return
            self.on_questions_detected(questions)
        except Exception as e:
            self._log(f"探测失败: {type(e).__name__}: {e}", "FAIL")
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    logger.debug("探测收尾 driver.quit() 失败（忽略）", exc_info=True)
            self.session.set_status(READY_STATUS)
            self.session.end_command("detect")

    def _wait_page_ready(self, driver: Any) -> None:
        if WebDriverWait is None:
            return
        try:
            WebDriverWait(driver, self._page_ready_timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            logger.debug("探测页 readyState 等待未完成（继续探测）", exc_info=True)

    def _pass_verification(self, driver: Any) -> bool:
        if self._verification_showing is None or not self._verification_showing(driver):
            return True
        self._log("检测到验证码，请在浏览器中手动完成...", "WARN")
        if self._wait_verification is None:
            return True
        if self._wait_verification(driver):
            return True
        self._log("验证超时，探测失败", "FAIL")
        return False

    def _locate_question_area(self, driver: Any) -> bool:
        """题目可能不在主文档里 —— 逐 iframe 找一遍，与 Tk 宿主同一套做法。"""
        if driver.execute_script(_QUESTION_COUNT_JS):
            return True
        frames = driver.execute_script("return document.querySelectorAll('iframe').length")
        for i in range(frames):
            driver.switch_to.frame(i)
            if driver.execute_script(_QUESTION_COUNT_JS):
                return True
            driver.switch_to.default_content()
        driver.switch_to.default_content()
        self._log("未能在页面中找到题目元素", "FAIL")
        return False

    def on_questions_detected(self, questions: list[dict]) -> None:
        self.session.set_questions(questions)
        counts: dict[str, int] = {}
        for q in questions:
            bucket = _TYPE_LABELS.get(normalize_question_type(q.get("type")))
            if bucket:
                counts[bucket] = counts.get(bucket, 0) + 1
        summary = "、".join(f"{v} {k}" for k, v in counts.items()) or "未识别题型"
        self._log(f"探测完成，共 {len(questions)} 题（{summary}）", "OK")
        self._log("请在表格中调整权重后点击「开始运行」", "INFO")


def format_weights_for_entry(qcfg: dict) -> str:
    """cfg dict → 权重表第 4 列的字符串。是解析的逆运算，步骤 4 与正向一起抽走共用。"""
    weights = qcfg.get("weights")
    if weights:
        return ",".join(
            f"{w:.4f}" if isinstance(w, (int, float)) else str(w) for w in weights
        )
    options = qcfg.get("options")
    if options:
        return ",".join(str(x) for x in options)
    row_weights = qcfg.get("row_weights")
    if isinstance(row_weights, dict) and row_weights:
        lines = [
            f"{rk}:{','.join(str(x) for x in row_weights[rk])}"
            for rk in sorted(row_weights, key=lambda k: int(k) if str(k).isdigit() else k)
        ]
        return " | ".join(lines)
    return ""
