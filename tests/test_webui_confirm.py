"""``webui/confirm.py`` —— 服务端问、浏览器答的那条反向通道。

设计稿 §10 步骤 6。这里钉的是四条**只有并发和时序才会暴露**的性质：

  - 答案在超时之后才到 → 必须作废（批次已经按"取消"开跑了，再让答案生效等于
    往一个在跑的批次里改续传起点）；
  - 同一个 id 答两次 → 第二次必须不生效；
  - 收尾时还挂着的确认 → 必须被叫醒，而不是让那条请求线程干等到超时；
  - 对话框内容只有快照一份真相 → 撤下问题这件事不需要额外的事件。

真线程 + 短超时，不用假时钟：这条通道的全部内容就是时序，把时钟抽走等于
把要验的东西验不到。
"""

from __future__ import annotations

import threading
import time

import pytest

from webui.confirm import MAX_PENDING, ConfirmChannel
from webui.session import Availability, RunSession, SessionPaths


def _channel(timeout=5.0):
    emits = []
    chan = ConfirmChannel(lambda: emits.append(len(chan.pending())), timeout=timeout)
    return chan, emits


def _ask_later(chan, title="断点续传", message="是否从第 4 份继续？"):
    """在另一条线程里问，返回 (结果容器, 线程)。"""
    out: dict = {}

    def worker():
        out["value"] = chan.ask(title, message)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return out, t


def _wait_until(fn, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------ 正常往返


def test_an_answer_reaches_the_asker_and_both_sides_see_the_same_question() -> None:
    chan, emits = _channel()
    out, worker = _ask_later(chan)
    assert _wait_until(lambda: chan.has_pending()), "问题应该已经挂上"

    [pending] = chan.pending()
    assert pending["title"] == "断点续传"
    assert "第 4 份" in pending["message"]
    assert "answer" not in pending, "答案不能出现在给浏览器看的快照里"

    assert chan.answer(pending["id"], True) is True
    worker.join(3)
    assert out["value"] is True
    assert chan.pending() == []
    assert emits, "问与答都各推了一次快照"


def test_a_declined_confirm_comes_back_false() -> None:
    chan, _ = _channel()
    out, worker = _ask_later(chan)
    assert _wait_until(chan.has_pending)
    cid = chan.pending()[0]["id"]

    assert chan.answer(cid, False) is True
    worker.join(3)
    assert out["value"] is False


# --------------------------------------------------------------------- 超时


def test_nobody_answering_falls_back_to_the_conservative_answer() -> None:
    """等不到就按"取消"办：续传那条的取消是从第 1 份重新开始，宁可不续传。"""
    chan, _ = _channel(timeout=0.3)
    out, worker = _ask_later(chan)
    assert _wait_until(chan.has_pending)

    worker.join(3)
    assert out["value"] is False
    assert chan.pending() == [], "超时之后问题必须自己撤下，浏览器里的框跟着没"


def test_an_answer_that_arrives_after_the_timeout_is_rejected() -> None:
    """人 0.6 秒才点"继续"，而批次在 0.3 秒就已经按取消开跑了。"""
    chan, _ = _channel(timeout=0.3)
    out, worker = _ask_later(chan)
    assert _wait_until(chan.has_pending)
    cid = chan.pending()[0]["id"]

    worker.join(3)
    assert out["value"] is False
    assert chan.answer(cid, True) is False, "过期 id 必须答不进去"


# --------------------------------------------------------------- 一次性与幂等


def test_a_token_is_consumed_by_the_first_answer() -> None:
    chan, _ = _channel()
    out, worker = _ask_later(chan)
    assert _wait_until(chan.has_pending)
    cid = chan.pending()[0]["id"]

    assert chan.answer(cid, True) is True
    worker.join(3)
    assert chan.answer(cid, False) is False, "同一个确认答第二次不该生效"


@pytest.mark.parametrize("bad", ["", None, "no-such-id", 12345])
def test_an_unknown_id_is_rejected_rather_than_silently_ignored(bad) -> None:
    chan, _ = _channel()
    assert chan.answer(bad, True) is False


def test_answering_when_nothing_is_pending_does_not_wedge_the_channel() -> None:
    chan, _ = _channel()
    assert chan.answer("stale", True) is False
    out, worker = _ask_later(chan)
    assert _wait_until(chan.has_pending)
    assert chan.answer(chan.pending()[0]["id"], True) is True
    worker.join(3)
    assert out["value"] is True


# ------------------------------------------------------------------- 收尾


def test_shutdown_wakes_waiters_instead_of_making_them_time_out() -> None:
    """``server.shutdown()`` 不 join 请求线程；不叫醒的话那条线程要干等到超时。"""
    chan, _ = _channel(timeout=30.0)
    out, worker = _ask_later(chan)
    assert _wait_until(chan.has_pending)

    chan.cancel_all()
    worker.join(3)
    assert out["value"] is False
    assert chan.pending() == []


def test_the_pending_cap_refuses_instead_of_piling_up() -> None:
    chan, _ = _channel(timeout=0.2)
    waiters = [_ask_later(chan) for _ in range(MAX_PENDING)]
    assert _wait_until(lambda: len(chan.pending()) == MAX_PENDING)

    assert chan.ask("溢出", "第 9 条") is False, "超上限直接按取消办，不排队"
    for _out, worker in waiters:
        worker.join(5)
    assert chan.pending() == []


# --------------------------------------------------------- 与 session/service 的接线


def test_the_session_snapshot_carries_the_open_question(tmp_path) -> None:
    """对话框的内容只有快照一份真相 —— 所以它必须在 ``snapshot()`` 里。"""
    session = RunSession(paths=SessionPaths(str(tmp_path)),
                         availability=Availability())
    assert session.snapshot()["confirms"] == []

    session.confirms._pending["x"] = {          # 直接放一条，避免真的起线程等
        "id": "x", "title": "断点续传", "message": "继续吗",
        "expires_at": 0.0, "event": threading.Event(), "answer": None}
    snap = session.snapshot()
    assert snap["confirms"] == [{"id": "x", "title": "断点续传",
                                 "message": "继续吗"}]


def test_the_service_asks_the_browser_by_default(tmp_path) -> None:
    """没注册桌面宿主时，确认走的是 SSE 通道而不是 ``popup_confirm``。

    上一版默认值是 ``popup_confirm``，在纯 webui 进程里它恒为 False ——
    于是"检测到未完成批次"这个问题被**静默答成取消**，用户只会觉得
    怎么又从第 1 份开始跑了。
    """
    from webui.service import WebService

    session = RunSession(paths=SessionPaths(str(tmp_path)),
                         availability=Availability())
    svc = WebService(session, create_driver=None, detect_questions=None,
                     decode_qr=None)
    assert svc._confirm == session.confirms.ask
