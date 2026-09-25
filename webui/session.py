"""``webui`` 的会话状态 —— 表单值、运行态、日志环形缓冲。

设计稿：``docs/design/DESIGN_webui.md`` §3。

这个模块**不 import tkinter、不 import http**：它只是被 HTTP 处理线程与
Selenium worker 线程共同读写的状态容器，所有"要告诉界面一声"的动作都通过
构造时注入的 ``emit(kind, payload)`` 走。Tk 宿主那边对应的是
``SurveyGUI`` 的一堆 ``StringVar`` + ``root.after(0, ...)`` + 按钮 ``configure``，
这三样在这里分别变成：普通字段、``emit``、``busy`` 集合。

线程约定：worker 线程只允许调 ``log`` / ``set_status`` / ``finish_run`` /
``update_progress`` 与 ``emit``；表单字段只由 HTTP 处理线程写。
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from src.config import BROWSER_OPTIONS
from webui.confirm import ConfirmChannel

# 状态字 → 语义色名。颜色是视图的事，所以这里给名字而不是 #hex。
STATUS_COLORS: dict[str, str] = {
    "就绪": "neutral",
    "正在探测题目...": "warn",
    "正在解析二维码...": "warn",
    "运行中...": "running",
    "正在停止...": "warn",
}

DEFAULT_LOG_CAPACITY = 2000
COUNT_MIN = 1
COUNT_MAX = 9999
URL_MAX = 500  # 与 Tk 宿主一致：超长会被静默截断后真的拿去导航（设计稿 §5「不碰」）

_URL_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


class ValidationError(ValueError):
    """字段级校验失败。api 层把它翻成 400 + 人话错误（设计稿 §5 修第 1 条）。"""


class Availability:
    """可选依赖的可用性快照，决定前端哪些按钮该灰。

    Tk 里这些判定散在 ``_HAS_CONFIG_IO`` / ``_HAS_HISTORY`` 与 controller 的
    整体 try 导入里；这里集中成一个对象，一次算完。
    """

    def __init__(self, **flags: bool) -> None:
        self.config_io = bool(flags.get("config_io", False))
        self.history = bool(flags.get("history", False))
        self.qr = bool(flags.get("qr", False))
        self.selenium = bool(flags.get("selenium", False))

    def as_dict(self) -> dict[str, bool]:
        return {
            "config_io": self.config_io,
            "history": self.history,
            "qr": self.qr,
            "selenium": self.selenium,
        }

    @classmethod
    def probe(cls) -> "Availability":
        """按真实导入结果算一次可用性，对应 Tk 的 ``_HAS_CONFIG_IO`` / ``_HAS_HISTORY``
        与 controller 那个整体 try。集中成一处，前端只读 ``snapshot()['availability']``。
        """
        flags: dict[str, bool] = {}
        try:
            import src.config_io  # noqa: F401
            flags["config_io"] = True
        except Exception:
            flags["config_io"] = False
        try:
            import src.history  # noqa: F401
            flags["history"] = True
        except Exception:
            flags["history"] = False
        try:
            from gui.qr_utils import decode_qr_from_image
            flags["qr"] = decode_qr_from_image is not None
        except Exception:
            flags["qr"] = False
        try:
            import selenium  # noqa: F401
            from src.browser import create_driver  # noqa: F401
            flags["selenium"] = True
        except Exception:
            flags["selenium"] = False
        return cls(**flags)


class SessionPaths:
    """三条数据路径在启动时一次性快照 —— 与 Tk 宿主同一个理由：
    跑到一半改 ``WJX_USER_DATA_DIR`` 会让历史库与配置文件指向两棵不同的树。
    """

    def __init__(self, user_data_root: str) -> None:
        self.user_data_root = user_data_root
        self.config_dir = os.path.join(user_data_root, "configs")
        self.default_config_path = os.path.join(
            self.config_dir, "default_weight_config.json"
        )
        self.history_db_path = os.path.join(user_data_root, "data", "history.db")


def _noop_emit(_kind: str, _payload: Any) -> None:
    return None


class RunSession:
    """一次 webui 服务期间的全部可变状态。"""

    def __init__(
        self,
        *,
        paths: SessionPaths,
        availability: Availability | None = None,
        emit: Callable[[str, Any], None] = _noop_emit,
        log_capacity: int = DEFAULT_LOG_CAPACITY,
    ) -> None:
        self.paths = paths
        self.availability = availability or Availability()
        self._emit = emit
        self._lock = threading.RLock()
        self.log_lines: deque[dict[str, Any]] = deque(maxlen=log_capacity)
        self._log_seq = 0
        # 见 emit_state()：号随状态变化递增
        self._rev = 0

        # ---- 表单 ----
        self.url: str = ""
        self.count: int = 1
        self.browser: str = "edge"
        self.use_uc: bool = False
        # 与 Tk 宿主一致：GUI 侧默认**不**把填空题原文写进历史库（设计稿 §5「不碰」）
        self.no_record_text: bool = True

        # ---- 运行态 ----
        self.status: str = "就绪"
        self.running: bool = False
        self.busy: set[str] = set()
        self.success_count = 0
        self.fail_count = 0
        self.current_round = 0
        self.total_rounds = 0

        # ---- 探测结果与权重表 ----
        self.questions: list[dict[str, Any]] = []
        self.weight_texts: dict[int, str] = {}

        # ---- 确认反向通道（设计稿 §10 步骤 6）----
        # 推的是"整份快照"而不是单独一个 confirm 事件：对话框的内容因此只有
        # snapshot() 一处真相，断线重连 / 刷新页面 / 超时自动关闭都收敛到同一个状态。
        self.confirms = ConfirmChannel(self.emit_state)

    # ------------------------------------------------------------ 事件出口

    def attach_emitter(self, emitter: Callable[[str, Any], None]) -> None:
        """换掉事件出口。api 层建好广播器之后调一次，之后所有状态变化都走 SSE。"""
        with self._lock:
            self._emit = emitter

    def emit(self, kind: str, payload: Any = None) -> None:
        # 版本号在**状态变化**时递增，不是在读快照时递增。
        # 反过来（在 snapshot() 里 ++）会让一份"早就算好、刚刚才送到浏览器"的
        # HTTP 响应带着更高的号，把已经前进过的 SSE 结果盖回去 —— 实测到的样子是
        # 批次跑完、日志写着 ✓3，界面上的成功数却回到 0、进度回到 33.3%。
        with self._lock:
            self._rev += 1
        self._broadcast(kind, payload)

    def emit_state(self) -> dict[str, Any]:
        """广播整份状态，并让**号和内容在同一次加锁里定下来**。

        写成 ``emit("state", snapshot())`` 会漏号：实参先求值，拿到的 rev 是取号
        **之前**的计数，而广播之后计数已经 +1 —— 于是"变更前读的那份 HTTP 响应"
        和"变更后广播的那份载荷"带着同一个号。浏览器先收到前者、再收到同号的后者，
        按 ``rev <=`` 丢弃，界面就少一次更新。实测症状：点「探测题目」之后权重表
        不出现，日志却写着探测成功，要等下一次交互才补齐。
        """
        with self._lock:
            self._rev += 1
            payload = self.snapshot()
        self._broadcast("state", payload)
        return payload

    def _broadcast(self, kind: str, payload: Any) -> None:
        try:
            self._emit(kind, payload)
        except Exception:  # 出口坏了不该把调用方一起拖死；api 层自己会记日志
            pass

    # ------------------------------------------------------------ 表单

    def set_field(self, name: str, value: Any) -> None:
        """带校验的单字段写入。校验失败抛 ``ValidationError``。"""
        with self._lock:
            if name == "url":
                self.url = self._validated_url(value)
            elif name == "count":
                self.count = self._validated_count(value)
            elif name == "browser":
                self.browser = self._validated_browser(value)
            elif name == "use_uc":
                # 刻意不因 browser != chrome 而清零：与 Tk 宿主行为一致（§5「不碰」）
                self.use_uc = bool(value)
            elif name == "no_record_text":
                self.no_record_text = bool(value)
            else:
                raise ValidationError(f"未知字段：{name}")
        self.emit_state()

    @staticmethod
    def _validated_url(value: Any) -> str:
        url = str(value or "").strip()
        if not url:
            raise ValidationError("问卷 URL 不能为空")
        if not _URL_SCHEME_RE.match(url):
            raise ValidationError("问卷 URL 必须以 http:// 或 https:// 开头")
        return url[:URL_MAX]

    @staticmethod
    def _validated_count(value: Any) -> int:
        """手输也要夹范围 —— Tk 只在 +/− 按钮上夹，输 abc 直接抛 TclError。"""
        try:
            count = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValidationError(f"提交份数必须是整数，收到：{value!r}") from None
        if not COUNT_MIN <= count <= COUNT_MAX:
            raise ValidationError(
                f"提交份数要在 {COUNT_MIN} ~ {COUNT_MAX} 之间，收到：{count}"
            )
        return count

    def _validated_browser(self, value: Any) -> str:
        browser = str(value or "").strip().lower()
        if browser not in BROWSER_OPTIONS:
            raise ValidationError(
                "浏览器只支持 {}，收到：{!r}".format(
                    " / ".join(BROWSER_OPTIONS), value)
            )
        return browser

    def form_values(self) -> dict[str, Any]:
        with self._lock:
            return {
                "url": self.url,
                "count": self.count,
                "browser": self.browser,
                "use_uc": self.use_uc,
                "no_record_text": self.no_record_text,
            }

    # ------------------------------------------------------------ 运行态

    def set_status(self, text: str) -> None:
        with self._lock:
            self.status = text
        self.emit("status", {"text": text, "tone": STATUS_COLORS.get(text, "neutral")})

    def restore_idle_status(self, text: str) -> None:
        """后台命令收尾时把状态条还给「就绪」—— 但只在真的没在跑的时候还。

        探测 worker 的 ``finally`` 要先 ``driver.quit()``（一到两秒），这条很容易
        落在用户已经点了「开始运行」之后，于是长跑期间状态条写着「就绪」。
        按钮的禁用态由 ``running`` 单独驱动，所以只有这一行文字在说谎 ——
        比整块界面都不动更难发现。
        """
        with self._lock:
            if self.running:
                return
        self.set_status(text)

    def begin_command(self, name: str) -> None:
        """替代 Tk 的"按钮 configure(disabled)"。"""
        with self._lock:
            self.busy.add(name)
        self.emit_state()

    def end_command(self, name: str) -> None:
        with self._lock:
            self.busy.discard(name)
        self.emit_state()

    def is_busy(self, name: str) -> bool:
        with self._lock:
            return name in self.busy

    def start_run(self, total_rounds: int) -> None:
        with self._lock:
            self.running = True
            self.total_rounds = total_rounds
        self.set_status("运行中...")
        self.emit_state()          # 同 finish_run：禁用态也要有推送通道，不只靠点击的响应

    def request_stop(self) -> None:
        with self._lock:
            if not self.running:
                return
        self.set_status("正在停止...")
        self.emit_state()

    def finish_run(self) -> None:
        """批次收尾。

        ``emit_state()`` 不是多余的：按钮的禁用态由 ``running`` 驱动，而
        ``set_status`` 只发 ``status`` 事件（前端拿它刷一行文字）。少这一播，
        "跑完了"这件事就**没有推送通道** —— 界面只能等下一次 HTTP 往返、
        SSE 重连或队列溢出触发的 ``gap`` 才把「开始运行」重新点亮。实测正是这样：
        runner 上比本机慢，批次结束时在途的 ``POST /api/stop`` 响应还带着
        ``running=true``，把刚由别的通道推进过的 ``rev`` 盖掉，按钮于是永远按不动。
        """
        with self._lock:
            self.running = False
        self.set_status("就绪")
        self.emit_state()

    def update_progress(
        self, *, success: int, fail: int, current_round: int, total_rounds: int
    ) -> None:
        with self._lock:
            self.success_count = success
            self.fail_count = fail
            self.current_round = current_round
            self.total_rounds = total_rounds
        self.emit(
            "progress",
            {
                "success": success,
                "fail": fail,
                "round": current_round,
                "total": total_rounds,
                "percent": (current_round / total_rounds * 100) if total_rounds else 0.0,
            },
        )

    # ------------------------------------------------------------ 日志

    def log(self, message: str, tag: str = "INFO") -> dict[str, Any]:
        """worker 线程也调它，所以必须线程安全，且只做两件事：进环形缓冲、emit。"""
        with self._lock:
            self._log_seq += 1
            row = {
                "n": self._log_seq,
                "ts": time.strftime("%H:%M:%S"),
                "tag": tag,
                "text": str(message),
            }
            self.log_lines.append(row)
        self.emit("log", row)
        return row

    # ------------------------------------------------------------ 探测结果

    def set_questions(self, questions: list[dict[str, Any]]) -> None:
        """收下探测结果，并给每行预填默认权重串（留空 = 等权重随机）。"""
        from webui.weights import default_text_for

        with self._lock:
            self.questions = list(questions)
            for q in questions:
                qi = q.get("q")
                if not isinstance(qi, int):
                    continue
                self.weight_texts.setdefault(qi, default_text_for(q))
            rows = self.table_rows()
        self.emit("questions", {"count": len(questions), "rows": rows})

    def table_rows(self) -> list[dict[str, Any]]:
        """权重表的行模型：题号 · 类型标签 · 选项/规模 · 当前文本。"""
        from webui.weights import scale_label, type_label

        return [
            {
                "q": int(q["q"]),
                "type": str(q.get("type", "")),
                "label": type_label(q.get("type")),
                "n": scale_label(q),
                "text": self.weight_texts.get(int(q["q"]), ""),
            }
            for q in self.questions
            if isinstance(q.get("q"), int)
        ]

    def set_weight_texts(self, texts: dict[int, str]) -> None:
        """写回权重表的第 4 列。改完必须广播：它是快照的一部分。

        静默写会让 ``rev`` 和内容脱钩 —— 前端按号丢弃一份"同号但更新"的响应，
        于是自己敲进去的权重在某些时序下根本不落地。
        """
        with self._lock:
            self.weight_texts.update({int(k): str(v) for k, v in texts.items()})
        self.emit_state()

    # ------------------------------------------------------------ 快照

    def snapshot(self) -> dict[str, Any]:
        """整份可渲染状态。``rev`` 是这批数据的版本号，由 ``emit_state()`` 定号。

        前端只接受 rev 更大的快照 —— ``POST /api/run`` 的响应体就是一份快照，
        而它完全可能在 worker 推进之后才送到浏览器。没有这道闸，界面会**倒回去**，
        且如果之后没有新事件，就永久停在旧值上。
        """
        with self._lock:
            return {
                "rev": self._rev,
                "form": self.form_values(),
                "status": self.status,
                "status_tone": STATUS_COLORS.get(self.status, "neutral"),
                "running": self.running,
                "busy": sorted(self.busy),
                "counts": {
                    "success": self.success_count,
                    "fail": self.fail_count,
                    "round": self.current_round,
                    "total": self.total_rounds,
                },
                "questions": len(self.questions),
                "confirms": self.confirms.pending(),
                "table": self.table_rows(),
                "log_lines": self._log_seq,
                "availability": self.availability.as_dict(),
                "limits": {"count_min": COUNT_MIN, "count_max": COUNT_MAX},
                "paths": {
                    "user_data_root": self.paths.user_data_root,
                    "config_dir": self.paths.config_dir,
                    "history_db": self.paths.history_db_path,
                },
            }
