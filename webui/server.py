"""``webui`` 的 HTTP 服务层 —— 只做"字节进出"，判断都在 ``api`` 里。

设计稿：``docs/design/DESIGN_webui.md`` §3、§7。

四条与"长跑不能被界面拖死"直接相关的行为：

  - **SSE 每个客户端占一个线程**，所以有并发上限；超了直接 503，而不是让
    ``ThreadingHTTPServer`` 无节制地开线程。
  - **客户端断开不影响 worker**：这里只 ``unsubscribe`` 一条订阅，运行状态照旧推进。
  - **``/api/shutdown`` 不自己收尾**：处理请求的线程去调 ``httpd.shutdown()`` 等于等自己
    退出（必死锁）。它只置标志 + 唤醒主线程，真正的收尾由主线程走 ``shutdown()``。
  - **``Ctrl-C`` 与 ``/api/shutdown`` 是同一条路径**（设计稿 §7 风险 1）：浏览器没有可靠的
    "用户走了"事件，所以退出必须显式，且必须把 worker 的 join 走完。

静态文件只认表里那几个名字，不做目录解析 —— 路径穿越在结构上不存在，而不是"被过滤掉了"。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from webui.api import MAX_BODY_BYTES, Api, Response

logger = logging.getLogger("wjx.webui.server")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
MAX_SSE_CLIENTS = 4
# 心跳间隔同时也是**断连检测间隔**：服务端只有在写的时候才会发现客户端走了。
# 但这条不可靠 —— 客户端优雅关闭（FIN）后，往半关闭连接里写是静默成功的，
# 实测 http.client 的 close() 就永远触发不了写失败。所以只有"到点主动收线"
# 是确定性的：每条 SSE 有最长寿命，收了浏览器会按 retry: 自己重连，
# 而首帧带全量状态，重连对用户透明。
SSE_HEARTBEAT_SECONDS = 2.0
SSE_MAX_LIFETIME_SECONDS = 30.0
SSE_RETRY_MS = 1500
POLL_SECONDS = 0.3


def _sse_frame(kind: str, payload: Any) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode(
        "utf-8")


class WebUIRequestHandler(BaseHTTPRequestHandler):
    """把 socket 里的请求要素交给 ``Api.handle``，再把 ``Response`` 写回去。"""

    api: Api
    protocol_version = "HTTP/1.1"        # SSE 要长连接，必须 1.1 + 显式 Content-Length

    def log_message(self, fmt: str, *args: Any) -> None:      # 别往 stderr 刷
        logger.debug("webui %s - " + fmt, self.address_string(), *args)

    # ------------------------------------------------------------ 方法入口

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/api/events":
            self._serve_events()
            return
        static = STATIC_FILES.get(path)
        if static is not None:
            self._send(self._static(*static))
            return
        self._dispatch("GET", path, b"")

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path in STATIC_FILES:
            self._send(Response.error(HTTPStatus.METHOD_NOT_ALLOWED,
                                      "静态文件不接受 POST"))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(Response.error(HTTPStatus.BAD_REQUEST,
                                      "Content-Length 无法解析"))
            return
        if length < 0:
            self._send(Response.error(HTTPStatus.BAD_REQUEST,
                                      "Content-Length 非法"))
            return
        if length > MAX_BODY_BYTES:
            self._send(Response.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                      "请求体过大"))
            return
        self._dispatch("POST", path, self.rfile.read(length) if length else b"")

    # ------------------------------------------------------------ 内部

    def _query(self) -> dict[str, str]:
        raw = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        return {k: v[0] for k, v in raw.items() if v}

    def _dispatch(self, method: str, path: str, body: bytes) -> None:
        resp = self.api.handle(
            method, path,
            host=self.headers.get("Host"),
            origin=self.headers.get("Origin"),
            body=body,
            query=self._query(),
        )
        self._send(resp)

    def _send(self, resp: Response) -> None:
        try:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in resp.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD" and resp.body:
                self.wfile.write(resp.body)
        except (BrokenPipeError, ConnectionResetError, OSError):   # pragma: no cover
            logger.debug("webui 客户端提前断开（忽略）", exc_info=True)

    @staticmethod
    def _static(filename: str, content_type: str) -> Response:
        path = os.path.join(STATIC_DIR, filename)
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return Response.error(
                HTTPStatus.NOT_FOUND,
                f"缺少前端文件 {filename}（安装不完整？见设计稿 §7 风险 3）",
            )
        return Response(status=HTTPStatus.OK, body=body, content_type=content_type)

    def _serve_events(self) -> None:
        api = self.api
        if api.events.client_count >= MAX_SSE_CLIENTS:
            self._send(Response.error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"已连接 {MAX_SSE_CLIENTS} 个页面，先关掉一个再刷新"))
            return
        sub = api.events.subscribe()
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(f"retry: {SSE_RETRY_MS}\n\n".encode("ascii"))
            # 首帧给全量状态：断线重连不依赖错过的历史事件，语义照样收敛
            self.wfile.write(_sse_frame("hello", api.session.snapshot()))
            self.wfile.flush()
            self._pump_events(sub)
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.debug("webui SSE 连接中断（忽略）", exc_info=True)
        finally:
            api.events.unsubscribe(sub)
            # 别再让 keep-alive 去读下一个请求：连接已经死了，读了就是
            # 一条从 socketserver.handle_error 冒到 stderr 的 ConnectionAbortedError。
            self.close_connection = True

    def _pump_events(self, sub: "queue.Queue") -> None:
        opened = time.monotonic()
        last_beat = opened
        while not self.api.shutting_down:
            if time.monotonic() - opened >= SSE_MAX_LIFETIME_SECONDS:
                return          # 主动收线；浏览器按 retry: 重连，首帧会重发全量状态
            try:
                kind, payload = sub.get(timeout=0.5)
            except queue.Empty:
                if time.monotonic() - last_beat >= SSE_HEARTBEAT_SECONDS:
                    self.wfile.write(b": ping\n\n")     # 注释帧：探活兼防代理掐线
                    self.wfile.flush()
                    last_beat = time.monotonic()
                continue
            self.wfile.write(_sse_frame(kind, payload))
            self.wfile.flush()
            last_beat = time.monotonic()


class WebUIServer:
    """可测试的服务句柄：起在临时端口上，收尾只有一条路径。"""

    LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

    def __init__(self, api: Api, *, host: str = "127.0.0.1", port: int = 0) -> None:
        if host not in self.LOOPBACK_HOSTS:
            raise ValueError(f"webui 只允许监听回环地址，收到：{host!r}")
        self.api = api
        self.host = host
        self._httpd = self._make_httpd(host, port, api)
        self.port = int(self._httpd.server_address[1])
        api.port = self.port
        api._request_shutdown = self.request_shutdown
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._done = threading.Event()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    # ------------------------------------------------------------ 生命周期

    def start(self) -> "WebUIServer":
        self._thread = threading.Thread(target=self._serve, name="wjx-webui",
                                        daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        try:
            self._httpd.serve_forever(poll_interval=POLL_SECONDS)
        finally:
            self._done.set()

    def request_shutdown(self) -> None:
        """``/api/shutdown`` 走这里：只置标志与唤醒，不碰 shutdown()。"""
        self.api.shutting_down = True
        self._stop_requested.set()

    def wait_until_stopped(self, timeout: float | None = None) -> bool:
        return self._stop_requested.wait(timeout)

    def shutdown(self) -> None:
        """唯一的真收尾入口，必须由主线程调。"""
        self.request_shutdown()
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._done.set()

    @staticmethod
    def _make_httpd(host: str, port: int, api: Api) -> ThreadingHTTPServer:
        family = socket.AF_INET6 if host == "::1" else socket.AF_INET

        class _Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            address_family = family

        handler = type("BoundHandler", (WebUIRequestHandler,), {"api": api})
        return _Server((host, port), handler)


def run(session: Any, service: Any, *, host: str = "127.0.0.1", port: int = 0,
        open_browser: bool = False, printer: Any = print,
        after_start: Any = None) -> int:
    """阻塞式入口：起服务、打印地址、Ctrl-C 后走同一条收尾。

    ``after_start(server)`` 是给测试留的接缝 —— 端口是系统分配的，
    让测试去解析打印出来的 URL 只会多一条假失败的路。
    """
    api = Api(session, service)
    server = WebUIServer(api, host=host, port=port).start()
    session.log(f"Web 界面已就绪 → {server.url}", "OK")
    printer(f"webui listening on {server.url}  (Ctrl-C 或页面里的「退出」停止并退出)")
    if after_start is not None:
        after_start(server)
    if open_browser:
        webbrowser.open(server.url)
    try:
        while True:
            if server.wait_until_stopped(timeout=1.0):
                break
    except KeyboardInterrupt:
        session.log("收到 Ctrl-C，正在停止并收尾…", "WARN")
    finally:
        server.shutdown()
    return 0


__all__ = ["Api", "WebUIServer", "WebUIRequestHandler", "run", "STATIC_DIR",
           "STATIC_FILES", "MAX_SSE_CLIENTS"]
