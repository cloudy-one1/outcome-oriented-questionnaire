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

from src import config as cfg_module
from src.config_io import apply_weight_config
from src.dialogs import popup_confirm
from src.models import RunState, normalize_question_type
from webui.session import RunSession, ValidationError
from webui.weights import parse_weights

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
    WebDriverWait = None
    _create_driver = None
    _detect_questions = None
    _is_smart_verification_showing = None
    _wait_for_manual_verification = None
    SELENIUM_AVAILABLE = False

try:
    from gui.qr_utils import decode_qr_from_image as _decode_qr
except Exception:  # pragma: no cover
    _decode_qr = None

try:
    from src.history import SubmissionHistory as _SubmissionHistory
except Exception:  # pragma: no cover
    _SubmissionHistory = None

_ROUND_LEVELS: dict[str, str] = {
    "success": "OK",
    "failed": "FAIL",
    "unknown": "FAIL",
    "error": "FAIL",
    "browser_dead": "WARN",
    "aborted": "WARN",
}

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
        history_db_cls: Any | None = _SubmissionHistory,
        confirm: Callable[[str, str], bool] = popup_confirm,
        run_batch_fn: Callable[..., None] | None = None,
        join_timeout: float = 30.0,
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
        self._history_db_cls = history_db_cls
        self._confirm = confirm
        self._run_batch = run_batch_fn
        self._join_timeout = join_timeout
        self._db_cached: Any = None
        self._state: RunState | None = None
        self._run_thread: threading.Thread | None = None

    # ------------------------------------------------------------ 内部

    def _log(self, msg: str, tag: str = "INFO") -> None:
        self.session.log(msg, tag)

    def _save_ready(self, action: str) -> Callable[..., None] | None:
        """导出类动作的前置检查：能救就返回那个导出函数，不能就报过原因并给 None。

        返回**函数本身**而不是 bool：调用方拿到的是同一个被验过的对象，
        于是"检查说可以、调用时又是 None"这种错位在结构上不存在，
        也不用在调用点挂一条 ``# type: ignore``。
        """
        if not self.session.availability.config_io or self._save_wc is None:
            self._log(f"未加载 src/config_io，无法{action}配置", "FAIL")
            return None
        return self._save_wc

    def current_config_for_export(self) -> dict | None:
        """给 api 层用的公开入口：拿当前权重表对应的 cfg，拿不到就返回 None。"""
        return self._current_config()

    def _current_config(self) -> dict | None:
        cfg = self.build_weight_config() if self._build_cfg is None else self._build_cfg()
        if not cfg:
            self._log("当前没有可导出的权重配置（请先探测题目）", "WARN")
            return None
        return cfg

    # ------------------------------------------------------------ 配置 IO

    def export_config(self, filepath: str) -> None:
        save = self._save_ready("导出")
        if save is None:
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
            save(filepath, cfg, meta=meta)
            self._log(
                f"✓ 已导出配置 → {_relative_to(filepath, self.session.paths.user_data_root)}",
                "OK",
            )
        except Exception as e:
            self._log(f"导出配置失败: {type(e).__name__}: {e}", "FAIL")

    def save_default_config(self) -> None:
        save = self._save_ready("另存默认")
        if save is None:
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
            save(path, cfg, meta=meta)
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
            cfg, meta = self._load_wc(path)
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
        self.session.restore_idle_status(READY_STATUS)
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
            self.session.restore_idle_status(READY_STATUS)
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


    # ------------------------------------------------------------ 历史库

    def get_db(self) -> Any | None:
        """进程内唯一的 ``SubmissionHistory``（懒构造 + 缓存）。

        与 ``gui/history_panel.get_db`` 同一套理由：每个实例构造都要跑一遍含全表
        去重扫描的迁移，句柄不关就泄漏，而库里那把"串行化所有 DB 操作"的锁跨不了连接。
        """
        if self._db_cached is not None:
            return self._db_cached
        if not self.session.availability.history or self._history_db_cls is None:
            return None
        path = self.session.paths.history_db_path
        try:
            dirname = os.path.dirname(path)
            if dirname and not os.path.exists(dirname):
                os.makedirs(dirname, exist_ok=True)
            self._db_cached = self._history_db_cls(path)
            return self._db_cached
        except Exception as e:
            self._log(f"历史记录数据库打开失败: {type(e).__name__}: {e}", "WARN")
            return None

    def close_db(self) -> None:
        db, self._db_cached = self._db_cached, None
        if db is None:
            return
        try:
            db.close()
        except Exception as e:
            self._log(f"关闭历史库失败: {type(e).__name__}: {e}", "WARN")

    # ------------------------------------------------------------ 权重表

    def build_weight_config(self) -> dict:
        cfg, warnings = parse_weights(self.session.questions,
                                     self.session.weight_texts)
        for w in warnings:
            self._log(w, "WARN")
        return cfg

    # ------------------------------------------------------------ 运行循环

    def start_run(self) -> None:
        """与 Tk 的 ``_on_start`` 同序：校验 → 落权重 → 建 RunState → 问续传 → 起线程。

        ``RunState`` 建好之后 worker 再也不读界面状态，所以跨线程读 UI 这件事
        在这里就断掉了 —— 这是整套 GUI 编排里最容易出事的一步。
        """
        if self.session.running:
            return
        url = self.session.url.strip()
        if not url:
            raise ValidationError("请先填写问卷 URL")
        total = self.session.count

        if self.session.questions:
            cfg = self.build_weight_config()
            apply_weight_config(cfg, replace=True)
            self._log(f"已加载 {len(cfg_module.WEIGHT_CONFIG)} 道题的自定义权重",
                      "INFO")
        else:
            apply_weight_config({}, replace=True)
            self._log("未配置权重表格，所有题目使用等权重随机", "WARN")

        state = RunState(
            attempts_cap=total,
            total_target=total,
            resume_start_idx=1,
            browser=self.session.browser,
            use_uc=self.session.use_uc,
            survey_url=url[:500],
            no_record_text=self.session.no_record_text,
            weight_config_snapshot=RunState.snapshot_weight_config(
                dict(cfg_module.WEIGHT_CONFIG)),
        )
        db = self.get_db()
        if db is not None:
            self._apply_resumable_run(state, url, db)

        self._state = state
        self.session.start_run(state.total_target)
        self._sync_progress()
        self._log("═" * 40, "HEADER")
        if state.resume_start_idx > 1:
            self._log(f"▶ 断点续传启动：从第 {state.resume_start_idx} 份 → "
                      f"第 {state.total_target} 份（共 {state.attempts_cap} 份待跑）",
                      "HEADER")
        else:
            self._log(f"▶ 开始执行，目标 {state.total_target} 份", "HEADER")
        self._log("═" * 40, "HEADER")
        self._run_thread = threading.Thread(target=self._run_loop, args=(state,),
                                            name="wjx-run", daemon=True)
        self._run_thread.start()

    def request_stop(self) -> None:
        state = self._state
        if state is None or not self.session.running:
            return
        state.request_stop()
        self.session.request_stop()
        self._log("用户请求停止：当前轮次会在下一个题目边界收尾（不再提交该份）",
                  "WARN")

    def wait_for_run(self, timeout: float | None = None) -> bool:
        thread = self._run_thread
        if thread is None:
            return True
        thread.join(timeout if timeout is not None else self._join_timeout)
        return not thread.is_alive()

    def _run_loop(self, state: RunState) -> None:
        """批次语义只有一份实现：这里只是把 RunState 交给 ``src.cli.run_batch``。

        与 Tk 宿主共用同一个入口，所以"GUI 与 CLI 跑出不同结果"这类问题
        在结构上不存在。
        """
        run_batch = self._run_batch
        if run_batch is None:                                  # pragma: no cover
            from src.cli import run_batch as _run_batch        # 真引擎入口
            run_batch = _run_batch

        def on_round(res) -> None:
            self._log(
                f"[{res.index}/{state.total_target}] {res.message}"
                f"  (✓{state.success_count} ✕{state.fail_count})",
                _ROUND_LEVELS.get(res.outcome, "INFO"),
            )
            self._sync_progress()

        try:
            run_batch(state.survey_url, int(state.total_target),
                      browser=state.browser, use_uc=state.use_uc,
                      history_db=self.get_db(),
                      weight_config=state.weight_config_snapshot,
                      no_record_text=state.no_record_text,
                      state=state,
                      on_round=on_round,
                      log=lambda msg: self._log(msg, "INFO"),
                      stop_check=lambda: state.stop_flag,
                      error_suffix=f"Web · browser={state.browser} "
                                   f"uc={state.use_uc}")
        except Exception as e:
            self._log(f"运行异常: {type(e).__name__}: {e}", "FAIL")
        finally:
            self._finish_run()

    def _sync_progress(self) -> None:
        state = self._state
        if state is None:
            return
        current = (max(1, state.resume_start_idx) if state.current_attempt <= 0
                   else state.displayed_round)
        self.session.update_progress(success=state.success_count,
                                     fail=state.fail_count,
                                     current_round=current,
                                     total_rounds=state.total_target)

    def _finish_run(self) -> None:
        state = self._state
        self._sync_progress()
        self.session.finish_run()
        self._log("═" * 40, "HEADER")
        self._log(f"执行结束 — 成功 {self.session.success_count}"
                  f"  ·  失败 {self.session.fail_count}", "HEADER")
        if state is not None and state.unknown_count > 0:
            self._log(f"[统计] 其中 {state.unknown_count} 次提交结果未知"
                      "（按钮已点击但未观察到成功信号），已保守计入失败数。", "WARN")
        self._log("═" * 40, "HEADER")

    def _apply_resumable_run(self, state: RunState, url: str, db: Any) -> None:
        """问一次"要不要接着上次中断的批次继续"，结论就地写进 state。

        确认走 ``self._confirm``：步骤 6 会把它换成 SSE 反向通道。现在没注册宿主时
        ``src.dialogs`` 的确定性默认值是 False，也就是"从第 1 份重新开始" ——
        宁可不续传，也不能重复提交。
        """
        try:
            prev = db.find_resumable_run(url[:500])
            if prev is None:
                return
            done = int(prev["success_count"])
            planned = int(prev["total_submissions"])
            if not 0 < done < planned:
                return
            try:
                restored = type(db).deserialize_weight_config(prev)
            except Exception:
                restored = {}
            if restored:
                apply_weight_config(restored, replace=True)
                state.weight_config_snapshot = RunState.snapshot_weight_config(
                    restored)
                self._log(f"[续传] 已自动恢复上次权重配置：{len(restored)} 道题",
                          "OK")
                self.session.set_weight_texts({
                    int(qi): format_weights_for_entry(qcfg)
                    for qi, qcfg in restored.items()})
            msg = (f"检测到上次未完成的批次：\n\n"
                   f"  Run #{prev['id']} · 状态 = {prev['status']}\n"
                   f"  已成功 {done} / {planned} 份\n"
                   f"  开始时间 {str(prev['started_at'])[:19]}\n\n")
            if state.weight_config_snapshot:
                msg += ("✅ 上次权重已自动恢复到表格，\n"
                        "    可在配置区检查 / 修改后再启动。\n\n")
            msg += (f"是否从第 {done + 1} 份继续？"
                    "（取消则从第 1 份重新开始，但权重恢复仍生效）")
            if self._confirm("断点续传", msg):
                state.resume_start_idx = done + 1
                state.run_id = int(prev["id"])
                state.success_count = done      # 历史成功已计入，只用于进度显示
                state.total_target = planned
                state.attempts_cap = planned - done
                self._log(f"[续传] 恢复 Run #{state.run_id}：从第 "
                          f"{state.resume_start_idx} 份继续（共 {planned} 份，"
                          f"剩余 {state.attempts_cap} 份待跑）", "OK")
            else:
                self._log("[续传] 已忽略上次中断批次，从第 1 份重新开始"
                          "（权重恢复仍生效）", "INFO")
        except Exception as e:
            self._log(f"[续传] 检查可恢复批次失败（不影响运行）: "
                      f"{type(e).__name__}: {e}", "WARN")


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
