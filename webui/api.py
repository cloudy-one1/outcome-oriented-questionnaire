"""``webui`` 的路由与校验层 —— 纯函数式的请求→响应，不碰 socket。

设计稿：``docs/design/DESIGN_webui.md`` §6、§8。

刻意与 ``http.server`` 解耦：``Api.handle()`` 收一组已经解好的请求要素、返回一个
``Response``，所以路由、校验、安全边界全部可以像普通函数那样测，不需要起端口、
不需要真浏览器。``server.py`` 只剩"把 socket 里的字节搬进来再搬出去"这一件事。

安全边界的三条：

  1. **只认回环 Host，且 Origin 必须同源**（挡 DNS rebinding：攻击者让自己域名
     解析到 127.0.0.1 后，浏览器发的 ``Host`` 会是那个域名）。
  2. **导入/导出不接受路径**：导出是 HTTP 下载（保存位置归浏览器管），导入收请求体
     里的 JSON 文本。这比"白名单目录内的文件名"更硬 —— 服务端根本没有"按请求给的路径
     读写"这条代码路径。唯一固定的写目标是"另存默认"那条既定路径。
  3. **请求体有上限**，二维码图片按 base64 收，落盘只写进自己建的临时目录。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import queue
import re
import tempfile
from dataclasses import dataclass, field
from http import HTTPStatus
from urllib.parse import quote
from typing import Any, Callable

from webui.session import ValidationError

MAX_BODY_BYTES = 8 * 1024 * 1024          # 8MB：够一张二维码截图，也够一份大配置
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_NAME_FORBIDDEN_RE = re.compile(r"""[\\/:*?"<>|\x00-\x1f]|^\.+""")
_JSON_SUFFIX = ".json"


@dataclass
class Response:
    status: int = HTTPStatus.OK
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, payload: Any, status: int = HTTPStatus.OK,
             **headers: str) -> "Response":
        return cls(status=status,
                   body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   headers=headers)

    @classmethod
    def error(cls, status: int, message: str) -> "Response":
        return cls.json({"ok": False, "error": message}, status=status)

    @classmethod
    def text(cls, body: str, status: int = HTTPStatus.OK,
             content_type: str = "text/plain; charset=utf-8") -> "Response":
        return cls(status=status, body=body.encode("utf-8"),
                   content_type=content_type)


def host_is_loopback(host: str | None) -> bool:
    """``127.0.0.1:8760`` / ``localhost`` / ``[::1]:9000`` 都算，其余一律拒。"""
    if not host:
        return False
    name = host.strip().lower()
    if name.startswith("["):                      # IPv6 字面量：[::1] 或 [::1]:port
        end = name.find("]")
        if end < 0:
            return False
        return name[1:end] in LOOPBACK_HOSTS
    if name.count(":") == 1:                      # host:port
        name = name.split(":", 1)[0]
    return name in LOOPBACK_HOSTS


def origin_is_same_origin(origin: str | None, host: str | None) -> bool:
    """无 Origin（同导航请求）放行；有就必须与 Host 完全同 host:port。"""
    if not origin:
        return True
    match = re.match(r"^https?://([^/]+)$", origin.strip().lower())
    if not match:
        return False
    got = match.group(1)
    want = (host or "").strip().lower()
    if got == want:
        return True
    # 浏览器可能省略默认端口，也可能带端口；只补一次再比
    return f"{got}:80" == want or got == f"{want}:80"


def safe_download_name(name: Any) -> str:
    """把用户给的名字收成能当文件名用的样子：**消毒而不是拒绝**。

    这个名字只用于下载文件名与临时文件名，永远不参与"解析到哪个目录"的决定，
    所以没必要因为用户起了个中文名就 400；但分隔符、``..``、控制字符必须拿掉。
    """
    text = str(name or "").strip()
    text = _NAME_FORBIDDEN_RE.sub("_", text)
    text = text.replace("..", "_")
    text = text[:80].strip() or "weight_config"
    return text


def content_disposition(filename: str) -> str:
    """下载头。HTTP 头只允许 latin-1，中文文件名必须按 RFC 6266 编码。

    踩过的坑：直接 ``filename="我的配置.json"`` 会让 ``send_header`` 抛
    ``UnicodeEncodeError``，于是"用中文起名导出配置"这个最自然的动作直接 500。
    """
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip() or "weight_config.json"
    star = quote(filename, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{star}'


class Api:
    """路由表。新增命令只需要在这里加一行，``server.py`` 不用动。"""

    def __init__(
        self,
        session: Any,
        service: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        request_shutdown: Callable[[], None] | None = None,
        broadcaster: "Broadcaster | None" = None,
    ) -> None:
        self.session = session
        self.service = service
        self.host = host
        self.port = port
        # SSE 心跳循环看这个标志收工。由 WebUIServer 在收尾时置位 —— 不能由
        # 请求线程自己去 shutdown()，那等于等自己退出。
        self.shutting_down = False
        self._request_shutdown = request_shutdown or (lambda: None)
        self.events = broadcaster or Broadcaster()
        session.attach_emitter(self.events.publish)   # 状态变化一律经广播出去

    # ------------------------------------------------------------ 入口

    def handle(
        self,
        method: str,
        path: str,
        *,
        host: str | None = None,
        origin: str | None = None,
        body: bytes = b"",
        query: dict[str, str] | None = None,
    ) -> Response:
        if not host_is_loopback(host or f"{self.host}:{self.port}"):
            return Response.error(HTTPStatus.FORBIDDEN, "只允许本机回环地址访问")
        if not origin_is_same_origin(origin, host or f"{self.host}:{self.port}"):
            return Response.error(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")
        if len(body) > MAX_BODY_BYTES:
            return Response.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求体过大")

        route = (method.upper(), path.rstrip("/") or "/")
        handler = self.ROUTES.get(route)
        if handler is None:
            return Response.error(HTTPStatus.NOT_FOUND, f"没有这个接口：{path}")
        try:
            return handler(self, body, query or {})
        except ValidationError as e:
            return Response.error(HTTPStatus.BAD_REQUEST, str(e))
        except Exception as e:  # 兜底：不把栈甩给前端，但要留一句能查的日志
            self.session.log(f"接口 {path} 失败: {type(e).__name__}: {e}", "FAIL")
            return Response.error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------ 各路由

    def get_state(self, _body: bytes, _query: dict) -> Response:
        return Response.json({"ok": True, "state": self.session.snapshot()})

    def get_log(self, _body: bytes, query: dict) -> Response:
        """重连补齐用：前端 SSE 断了再连回来，按行号取一段。"""
        try:
            since = max(0, int(query.get("since", "0")))
        except ValueError:
            since = 0
        rows = [r for r in self.session.log_lines if r["n"] > since]
        return Response.json({"ok": True, "lines": rows})

    def post_field(self, body: bytes, _query: dict) -> Response:
        payload = self._json_body(body)
        name = str(payload.get("name", ""))
        self.session.set_field(name, payload.get("value"))
        return Response.json({"ok": True, "state": self.session.snapshot()})

    def post_detect(self, _body: bytes, _query: dict) -> Response:
        self.service.detect_questions()
        return Response.json({"ok": True, "state": self.session.snapshot()})

    def post_qr(self, body: bytes, _query: dict) -> Response:
        """二维码按 base64 收，写进自己建的临时目录后交给 service 解码。"""
        payload = self._json_body(body)
        raw = str(payload.get("image_base64", ""))
        if "," in raw[:64]:                     # 容忍 data:image/png;base64, 前缀
            raw = raw.split(",", 1)[1]
        try:
            blob = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise ValidationError("二维码图片不是合法的 base64") from None
        if not blob:
            raise ValidationError("二维码图片是空的")
        suffix = str(payload.get("filename", ""))
        ext = os.path.splitext(suffix)[1].lower()
        if ext not in {".png", ".jpg", ".jpeg", ".bmp", ".gif"}:
            ext = ".png"
        tmp_dir = tempfile.mkdtemp(prefix="wjx_qr_")
        path = os.path.join(tmp_dir, f"qr{ext}")
        try:
            with open(path, "wb") as fh:
                fh.write(blob)
            self.service.import_qr(path)
        finally:
            try:
                os.remove(path)
                os.rmdir(tmp_dir)
            except OSError:
                pass
        return Response.json({"ok": True, "state": self.session.snapshot()})

    def post_config_import(self, body: bytes, _query: dict) -> Response:
        """导入收 JSON 文本本身，不收路径 —— 服务端没有"按请求读写文件"这条路。"""
        payload = self._json_body(body)
        text = payload.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("配置内容为空")
        name = safe_download_name(payload.get("name", "上传的配置"))
        tmp_dir = tempfile.mkdtemp(prefix="wjx_cfg_")
        path = os.path.join(tmp_dir, f"{name}{_JSON_SUFFIX}")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            self.service.import_config(path)
        finally:
            try:
                os.remove(path)
                os.rmdir(tmp_dir)
            except OSError:
                pass
        return Response.json({"ok": True, "state": self.session.snapshot()})

    def post_config_export(self, body: bytes, _query: dict) -> Response:
        """导出=当场把配置序列化给浏览器下载，不在服务器上留副本。"""
        payload = self._json_body(body)
        name = safe_download_name(payload.get("name", "weight_config"))
        cfg = self.service.current_config_for_export()
        if cfg is None:
            raise ValidationError("当前没有可导出的权重配置（请先探测题目）")
        doc = json.dumps({"questions": cfg,
                          "meta": {"name": name,
                                   "survey_url": self.session.url[:200]}},
                         ensure_ascii=False, indent=2)
        quoted = f"{name}{_JSON_SUFFIX}"
        return Response(
            status=HTTPStatus.OK,
            body=doc.encode("utf-8"),
            content_type="application/json; charset=utf-8",
            headers={"Content-Disposition": content_disposition(quoted)},
        )

    def post_save_default(self, _body: bytes, _query: dict) -> Response:
        self.service.save_default_config()
        return Response.json({"ok": True, "state": self.session.snapshot()})

    def post_shutdown(self, _body: bytes, _query: dict) -> Response:
        self._request_shutdown()
        return Response.json({"ok": True, "stopping": True})

    def get_health(self, _body: bytes, _query: dict) -> Response:
        return Response.text("ok", content_type="text/plain; charset=utf-8")

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _json_body(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("请求体不是合法的 JSON") from None
        if not isinstance(parsed, dict):
            raise ValidationError("请求体必须是一个 JSON 对象")
        return parsed

    ROUTES: dict[tuple[str, str], Callable[..., Response]] = {
        ("GET", "/api/state"): get_state,
        ("GET", "/api/log"): get_log,
        ("GET", "/api/health"): get_health,
        ("POST", "/api/field"): post_field,
        ("POST", "/api/detect"): post_detect,
        ("POST", "/api/qr"): post_qr,
        ("POST", "/api/config/import"): post_config_import,
        ("POST", "/api/config/export"): post_config_export,
        ("POST", "/api/config/save-default"): post_save_default,
        ("POST", "/api/shutdown"): post_shutdown,
    }


class Broadcaster:
    """SSE 扇出：每个客户端一条有界队列，慢客户端丢最旧而不是拖死别人。

    丢了消息不是灾难 —— 队列里塞一个 ``gap`` 标记，前端收到就重取一次
    ``/api/state``，语义上仍然收敛到最新状态。
    """

    def __init__(self, capacity: int = 500) -> None:
        self.capacity = capacity
        self._subs: list[queue.Queue] = []

    def subscribe(self) -> queue.Queue:
        sub: queue.Queue = queue.Queue(maxsize=self.capacity)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: queue.Queue) -> None:
        try:
            self._subs.remove(sub)
        except ValueError:
            pass

    def publish(self, kind: str, payload: Any) -> None:
        item = (kind, payload)
        for sub in list(self._subs):
            try:
                sub.put_nowait(item)
            except queue.Full:
                # 慢客户端不该拖死别人：把积压整段丢掉，留一个 gap 让它去重取快照，
                # 再把最新这条送出去。语义上仍然收敛到"当前状态"。
                dropped = 0
                while True:
                    try:
                        sub.get_nowait()
                    except queue.Empty:
                        break
                    dropped += 1
                try:
                    sub.put_nowait(("gap", {"dropped": dropped}))
                    sub.put_nowait(item)
                except queue.Full:            # pragma: no cover - 竞争窗口
                    pass

    @property
    def client_count(self) -> int:
        return len(self._subs)
