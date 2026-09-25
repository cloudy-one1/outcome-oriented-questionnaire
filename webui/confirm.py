"""服务端问、浏览器答的**确认反向通道**（设计稿 §10 步骤 6）。

桌面版用 ``messagebox``，那是操作系统给的模态框。Web 这边没有等价物：
``window.confirm`` 会被浏览器拦重复弹窗、也不阻塞 SSE；而自己起一个模态却
"等用户点"必须把请求线程挂住 —— 挂多久、谁叫醒它、页面刷新了怎么办，
都是新问题。所以这里把它做成一条显式的通道：

    ask()  ──发──▶  SSE（快照里的 confirms）  ──点──▶  POST /api/confirm
       ◀── 一个 bool ──┘

**引擎侧签名与桌面版逐字相同**（``(title, message) -> bool``），所以
``service._apply_resumable_run`` 一行都不用改 —— 换的只是"这个 bool 从哪来"。

三条不能含糊的地方：

  - **等不到就按最保守的答案办**（``False``）。续传那条的 ``False`` 是"从第 1 份
    重新开始"，宁可不续传也不能把同一份问卷交两遍。
  - **超时之后到达的答案一律作废**。人 125 秒才点"继续"，而批次已经在第 120 秒
    从第 1 份开跑了 —— 这时候再让答案生效，等于往一个已经在跑的批次里改续传起点。
  - **对话框的内容只有一份真相**：它活在 ``snapshot()["confirms"]`` 里，前端只渲染
    快照。所以断线重连、刷新页面、超时自动关闭，全都收敛到同一个状态，
    不需要额外的"关掉对话框"事件。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable

#: 一次确认最多等多久。到点按最保守答案办并撤下对话框。
CONFIRM_TIMEOUT_SECONDS = 120.0

#: 同时挂着的确认上限：超了就不再生成新的（正常流程只会有一条）
MAX_PENDING = 8


class ConfirmChannel:
    """一条 SSE 出去、一个 HTTP 回来的问答通道。"""

    def __init__(
        self,
        # object 而不是 None：广播器就是 session.emit_state，它顺手把发出去的那份
        # 载荷返回给调用方，而这里用不着。
        emit: Callable[[], object],
        *,
        timeout: float = CONFIRM_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._emit = emit
        self._timeout = float(timeout)
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    # ------------------------------------------------------------ 问

    def ask(self, title: str, message: str, *, default: bool = False) -> bool:
        """把问题推给浏览器，阻塞等答案；等不到就回 ``default``。

        调用方是"点开始运行"那条 HTTP 请求线程，不是跑批 worker —— 所以这里等住
        只会让那一个请求慢一点，长跑本身不受影响。
        """
        cid = uuid.uuid4().hex[:16]
        entry = {
            "id": cid,
            "title": str(title),
            "message": str(message),
            "expires_at": self._clock() + self._timeout,
            "event": threading.Event(),
            "answer": None,
        }
        with self._lock:
            if len(self._pending) >= MAX_PENDING:
                return default
            self._pending[cid] = entry
        self._emit()
        answered = entry["event"].wait(self._timeout)
        with self._lock:
            got = entry.get("answer")
            self._pending.pop(cid, None)
        self._emit()
        if not answered or got is None:
            return default
        return bool(got)

    # ------------------------------------------------------------ 答

    def answer(self, cid: Any, accept: Any) -> bool:
        """浏览器交回答案。返回是否命中一条挂着的确认（没命中要回 400）。"""
        with self._lock:
            entry = self._pending.get(str(cid or ""))
            if entry is None:
                return False
            entry["answer"] = bool(accept)
            self._pending.pop(str(cid), None)
        entry["event"].set()
        self._emit()
        return True

    def cancel_all(self) -> None:
        """服务收尾：叫醒所有等待者，让它们按最保守的答案继续。"""
        with self._lock:
            entries = list(self._pending.values())
            self._pending.clear()
        for entry in entries:
            entry["event"].set()

    # ------------------------------------------------------------ 快照

    def pending(self) -> list[dict[str, Any]]:
        """还没答的确认，前端照着渲染对话框。``answer`` 字段刻意不给。"""
        with self._lock:
            return [
                {"id": e["id"], "title": e["title"], "message": e["message"]}
                for e in self._pending.values()
            ]

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)


__all__ = ["ConfirmChannel", "CONFIRM_TIMEOUT_SECONDS", "MAX_PENDING"]
