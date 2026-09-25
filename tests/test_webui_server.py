"""``webui/server.py`` 的契约 —— 起真服务、走真回环端口。

设计稿 §10 步骤 2 里"字节进出"那一半没法纯函数式地测，所以这里真的
``bind 127.0.0.1:0`` 起一个临时端口再打请求。全程用 ``http.client`` 而不是
``urllib``：开发机上挂着 ``HTTP_PROXY``，urllib 会把 127.0.0.1 也丢给代理，
那会让同一份测试在"能出网"和"被代理挡"两种机器上表现不一致。

SSE 这几条是本轮最要紧的：首帧必须带全量状态（断线重连才收敛得到）、
槽位必须收得回来（否则长跑一晚线程数跟着标签页涨），以及
**优雅关闭收不回槽位时要靠寿命上限兜底** —— 这两件事在 Windows 上是分开发生的。
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import struct
import threading
import time

import pytest

from webui.api import Api
from webui.session import Availability, RunSession, SessionPaths
from webui.server import MAX_SSE_CLIENTS, WebUIServer, run


class FakeService:
    """api 与 service 的契约面就这几个方法；server 层只碰 detect 与 health。"""

    def __init__(self):
        self.calls: list = []

    def detect_questions(self):
        self.calls.append("detect")

    def import_qr(self, path):  # pragma: no cover - 由 api 测试覆盖
        self.calls.append("qr")

    def import_config(self, path):  # pragma: no cover
        self.calls.append("import")

    def save_default_config(self):  # pragma: no cover
        self.calls.append("save-default")

    def current_config_for_export(self):
        return {1: {"type": "single", "weights": [0.5, 0.5]}}


def make_parts(tmp_path=None):
    root = str(tmp_path / "data") if tmp_path else os.getcwd()
    session = RunSession(
        paths=SessionPaths(root),
        availability=Availability(config_io=True, history=True, qr=True,
                                  selenium=True),
    )
    return session, FakeService()


def body_of(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


def get(handle, path, *, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", handle.port, timeout=5)
    conn.request("GET", path, headers={"Host": f"127.0.0.1:{handle.port}",
                                       **(headers or {})})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def post(handle, path, payload=None, *, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", handle.port, timeout=5)
    conn.request("POST", path, body=json.dumps(payload or {}).encode("utf-8"),
                 headers={"Host": f"127.0.0.1:{handle.port}",
                          "Content-Type": "application/json",
                          **(headers or {})})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def _open_events(handle):
    conn = http.client.HTTPConnection("127.0.0.1", handle.port, timeout=5)
    conn.request("GET", "/api/events",
                 headers={"Host": f"127.0.0.1:{handle.port}"})
    return conn, conn.getresponse()


def _read_frame(resp):
    """读一帧 SSE：跳过 ``retry:`` 与心跳注释，读到空行为止。"""
    event = data = None
    while True:
        line = resp.fp.readline()
        if not line:
            raise AssertionError("SSE 连接被提前关闭")
        text = line.decode("utf-8").rstrip("\n")
        if not text:
            if event or data:
                return event, (json.loads(data) if data else None)
            continue
        if text.startswith(":") or text.startswith("retry:"):
            continue
        if text.startswith("event: "):
            event = text[7:]
        elif text.startswith("data: "):
            data = text[6:]


def _wait_until(predicate, timeout=3.0):
    deadline = threading.Event()
    threading.Timer(timeout, lambda: deadline.set()).start()
    while not predicate():
        if deadline.is_set():
            return False
        threading.Event().wait(0.02)
    return True


@pytest.fixture()
def server(tmp_path):
    session, svc = make_parts(tmp_path)
    api = Api(session, svc)
    handle = WebUIServer(api).start()
    try:
        yield handle, session, svc, api
    finally:
        handle.shutdown()


# ------------------------------------------------------------ 基本进出


def test_the_server_refuses_to_listen_on_a_public_address():
    session, svc = make_parts()
    with pytest.raises(ValueError, match="回环"):
        WebUIServer(Api(session, svc), host="0.0.0.0")


def test_the_listening_port_is_the_one_the_os_actually_gave_us(server):
    handle, _session, _svc, api = server
    assert handle.port > 0
    assert api.port == handle.port          # 端口 0 交给系统分配后要对上
    assert handle.url == f"http://127.0.0.1:{handle.port}/"


def test_state_round_trips_over_http(server):
    handle, session, _svc, _api = server
    session.set_field("count", "9")
    resp, body = get(handle, "/api/state")
    assert resp.status == 200
    assert body_of(body)["state"]["form"]["count"] == 9
    assert resp.getheader("X-Content-Type-Options") == "nosniff"


def test_no_response_is_cacheable(server):
    handle, _session, _svc, _api = server
    resp, _body = get(handle, "/api/health")
    assert resp.getheader("Cache-Control") == "no-store"


def test_field_validation_survives_the_wire(server):
    handle, session, _svc, _api = server
    resp, body = post(handle, "/api/field", {"name": "count", "value": "0"})
    assert resp.status == 400
    assert "1 ~ 9999" in body_of(body)["error"]
    assert session.count == 1


def test_a_missing_frontend_file_says_so_instead_of_500(server):
    """步骤 3 之前 static/index.html 本来就不存在 —— 报错要能指向原因。"""
    handle, _session, _svc, _api = server
    resp, body = get(handle, "/")
    assert resp.status == 404
    assert "缺少前端文件" in body_of(body)["error"]


def test_static_paths_reject_post(server):
    handle, _session, _svc, _api = server
    resp, _body = post(handle, "/app.js")
    assert resp.status == 405


def test_a_broken_content_length_is_a_400_not_a_crash(server):
    handle, _session, _svc, _api = server
    conn = http.client.HTTPConnection("127.0.0.1", handle.port, timeout=5)
    conn.putrequest("POST", "/api/field")
    conn.putheader("Host", f"127.0.0.1:{handle.port}")
    conn.putheader("Content-Length", "abc")
    conn.endheaders()
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 400


def test_a_negative_content_length_is_a_400(server):
    """负长度是"请求写坏了"，不是"请求太大" —— 两个码不能混在一起。"""
    handle, _session, _svc, _api = server
    conn = http.client.HTTPConnection("127.0.0.1", handle.port, timeout=5)
    conn.putrequest("POST", "/api/field")
    conn.putheader("Host", f"127.0.0.1:{handle.port}")
    conn.putheader("Content-Length", "-5")
    conn.endheaders()
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 400


def test_a_foreign_host_header_is_refused_over_http(server):
    """DNS rebinding 的守门：端口对、Host 不对也不行。"""
    handle, _session, _svc, _api = server
    resp, body = get(handle, "/api/state", headers={"Host": "attacker.test"})
    assert resp.status == 403
    assert "本机" in body_of(body)["error"]


def test_a_cross_origin_post_is_refused_over_http(server):
    handle, _session, svc, _api = server
    resp, _body = post(handle, "/api/detect", headers={"Origin": "http://evil.test"})
    assert resp.status == 403
    assert svc.calls == []


def test_unknown_path_is_a_404_over_http(server):
    handle, _session, _svc, _api = server
    resp, _body = get(handle, "/api/nope")
    assert resp.status == 404


# ------------------------------------------------------------ SSE


def test_the_first_sse_frame_carries_the_whole_state(server):
    handle, session, _svc, _api = server
    session.set_field("url", "https://x.test/s")
    conn, resp = _open_events(handle)
    try:
        kind, payload = _read_frame(resp)
        assert kind == "hello"
        assert payload["form"]["url"] == "https://x.test/s"
        assert resp.getheader("Content-Type").startswith("text/event-stream")

        session.log("刚发生的一句话", "OK")
        kind, payload = _read_frame(resp)
        assert kind == "log" and payload["text"] == "刚发生的一句话"
    finally:
        conn.close()


def test_an_abruptly_closed_client_is_unsubscribed(server):
    """RST 走掉（进程被杀、线被拔）：下一次写就失败，订阅要立刻摘掉。

    这里用裸 socket：SSE 响应没有 Content-Length，``http.client`` 认为"读完即完"
    会把 ``conn.sock`` 直接置 None，拿不到套接字就没法设 SO_LINGER 逼出 RST。
    """
    handle, _session, _svc, api = server
    crlf = chr(13) + chr(10)
    request = (f"GET /api/events HTTP/1.1{crlf}"
               f"Host: 127.0.0.1:{handle.port}{crlf}{crlf}")
    sock = socket.create_connection(("127.0.0.1", handle.port), timeout=5)
    sock.sendall(request.encode("ascii"))
    assert b"event-stream" in sock.recv(600)
    assert api.events.client_count == 1

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                    struct.pack("ii", 1, 0))       # 强制 RST 而不是 FIN
    sock.close()
    assert _wait_until(lambda: api.events.client_count == 0, timeout=10), \
        "断开后订阅没被摘掉，长跑一晚线程数会跟着标签页涨"


def test_a_graceful_close_is_reclaimed_by_the_lifetime_cap(server, monkeypatch):
    """优雅关闭（FIN）之后往半连接里写是静默成功的 —— 只靠写失败收不回槽位。

    这条钉的是那个确定性兜底：SSE 到最长寿命就主动收线，浏览器按 ``retry:`` 重连。
    """
    import webui.server as srv

    monkeypatch.setattr(srv, "SSE_MAX_LIFETIME_SECONDS", 1.0)
    handle, _session, _svc, api = server
    conn, resp = _open_events(handle)
    assert _read_frame(resp)[0] == "hello"
    assert api.events.client_count == 1
    conn.close()                                   # 干净关闭，不触发任何写错误
    assert _wait_until(lambda: api.events.client_count == 0, timeout=8), \
        "寿命上限没把槽位收回来"


def test_repeated_refreshes_do_not_exhaust_the_sse_slots(server, monkeypatch):
    """槽位耗光的症状是刷新后一直 503、界面永远空白 —— 比慢更难查。"""
    import webui.server as srv

    monkeypatch.setattr(srv, "SSE_MAX_LIFETIME_SECONDS", 0.5)
    handle, _session, _svc, api = server
    for _ in range(3):
        conn, resp = _open_events(handle)
        assert _read_frame(resp)[0] == "hello"
        conn.close()
        assert _wait_until(lambda: api.events.client_count == 0, timeout=8)
    conn, resp = _open_events(handle)
    assert _read_frame(resp)[0] == "hello"
    conn.close()


def test_the_sse_client_cap_answers_503(server):
    handle, _session, _svc, api = server
    for _ in range(MAX_SSE_CLIENTS):
        api.events.subscribe()
    resp, body = get(handle, "/api/events")
    assert resp.status == 503
    assert "页面" in body_of(body)["error"]


# ------------------------------------------------------------ 收尾


def test_shutdown_releases_the_port(server):
    handle, _session, _svc, _api = server
    port = handle.port
    assert get(handle, "/api/health")[0].status == 200
    handle.shutdown()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


def test_the_shutdown_endpoint_does_not_deadlock_the_request_thread():
    """/api/shutdown 由请求线程处理，它自己去 shutdown() 等于等自己退出。"""
    session, svc = make_parts()
    handle = WebUIServer(Api(session, svc)).start()
    try:
        resp, body = post(handle, "/api/shutdown")
        assert resp.status == 200, body
        assert handle.wait_until_stopped(timeout=3) is True
    finally:
        handle.shutdown()


def test_shutdown_is_idempotent():
    session, svc = make_parts()
    handle = WebUIServer(Api(session, svc)).start()
    handle.shutdown()
    handle.shutdown()                 # 二次收尾不该炸


def _drive_run(monkeypatch, *, open_browser=False):
    """在别的线程里跑 run()，起来之后请求停止，等它自己返回。"""
    import webui.server as srv

    opened: list[str] = []
    monkeypatch.setattr(srv.webbrowser, "open", lambda url: opened.append(url))
    session, svc = make_parts()
    lines: list[str] = []
    out: dict = {}

    def caller():
        out["code"] = run(session, svc, open_browser=open_browser,
                          printer=lines.append,
                          after_start=lambda s: out.__setitem__("h", s))
        out["done"] = True

    threading.Thread(target=caller, daemon=True).start()
    assert _wait_until(lambda: "h" in out), "run() 没把服务起起来"
    handle = out["h"]
    assert get(handle, "/api/health")[0].status == 200
    handle.request_shutdown()
    assert _wait_until(lambda: out.get("done") is True), "run() 没在停止后返回"
    return lines, opened, out


def test_run_blocks_prints_the_url_and_returns_after_a_shutdown_request(monkeypatch):
    lines, opened, out = _drive_run(monkeypatch)
    assert "listening on http://127.0.0.1:" in lines[0]
    assert out["code"] == 0
    assert opened == []                       # 默认不开浏览器


def test_run_opens_the_url_it_is_actually_listening_on(monkeypatch):
    _lines, opened, out = _drive_run(monkeypatch, open_browser=True)
    assert opened == [out["h"].url]


def test_run_logs_its_own_arrival_into_the_log_ring(monkeypatch):
    """启动那句要能在重连后从 /api/log 里读到，否则刷新页面就看不到"已就绪"。"""
    import webui.server as srv

    monkeypatch.setattr(srv.webbrowser, "open", lambda _u: None)
    session, svc = make_parts()
    out: dict = {}
    threading.Thread(
        target=lambda: out.__setitem__(
            "code", run(session, svc, printer=lambda *_a: None,
                        after_start=lambda s: s.request_shutdown())),
        daemon=True).start()
    assert _wait_until(lambda: out.get("code") == 0)
    assert any("Web 界面已就绪" in r["text"] for r in session.log_lines)


# ============================================== 边角与兜底（补覆盖）


def test_a_claimed_body_bigger_than_the_cap_is_refused_without_reading_it(server):
    from webui.api import MAX_BODY_BYTES

    handle, _session, _svc, _api = server
    conn = http.client.HTTPConnection("127.0.0.1", handle.port, timeout=5)
    conn.putrequest("POST", "/api/field")
    conn.putheader("Host", f"127.0.0.1:{handle.port}")
    conn.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
    conn.endheaders()
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 413


def test_static_files_are_served_from_the_configured_directory(server, monkeypatch,
                                                               tmp_path):
    import webui.server as srv

    (tmp_path / "index.html").write_bytes(b"<html>ok</html>")
    monkeypatch.setattr(srv, "STATIC_DIR", str(tmp_path))
    handle, _session, _svc, _api = server
    resp, body = get(handle, "/")
    assert resp.status == 200
    assert body == b"<html>ok</html>"
    assert resp.getheader("Content-Type").startswith("text/html")


def test_the_export_download_keeps_its_filename_header(server):
    """Content-Disposition 是额外响应头的唯一用处，走一遍真 socket 才算数。"""
    handle, _session, _svc, _api = server
    conn = http.client.HTTPConnection("127.0.0.1", handle.port, timeout=5)
    payload = json.dumps({"name": "我的配置"}).encode("utf-8")
    conn.request("POST", "/api/config/export", body=payload,
                 headers={"Host": f"127.0.0.1:{handle.port}",
                          "Content-Type": "application/json"})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    assert resp.status == 200
    header = resp.getheader("Content-Disposition")
    # HTTP 头只允许 latin-1，中文名走 RFC 6266 的 filename*；ASCII 那份是回退
    assert "filename=\"____.json\"" in header
    assert "filename*=UTF-8" in header
    assert json.loads(body.decode("utf-8"))["questions"]


def test_an_idle_client_still_gets_a_heartbeat_frame(server):
    """心跳不只是探活：没有它，本地代理会掐掉一条静默的长连接。"""
    from webui.server import SSE_HEARTBEAT_SECONDS

    handle, _session, _svc, _api = server
    conn, resp = _open_events(handle)
    assert _read_frame(resp)[0] == "hello"
    deadline = time.monotonic() + SSE_HEARTBEAT_SECONDS * 3
    seen_ping = False
    while time.monotonic() < deadline:
        line = resp.fp.readline().decode("utf-8").strip()
        if line.startswith(":"):
            seen_ping = True
            break
    conn.close()
    assert seen_ping, "空闲客户端没收到心跳帧"


def test_ctrl_c_in_the_main_loop_still_shuts_down_cleanly(monkeypatch):
    """Ctrl-C 与 /api/shutdown 必须是同一条收尾路径（设计稿 §7 风险 1）。"""
    import webui.server as srv

    session, svc = make_parts()
    seen: dict = {}

    def interrupt(self, timeout=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(srv.WebUIServer, "wait_until_stopped", interrupt)
    code = run(session, svc, printer=lambda *_a: None,
               after_start=lambda s: seen.__setitem__("h", s))
    assert code == 0
    assert seen["h"].api.shutting_down is True
    assert any("Ctrl-C" in r["text"] for r in session.log_lines)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", seen["h"].port), timeout=2).close()
