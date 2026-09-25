"""``webui/api.py`` 的离线契约（设计稿 §10 步骤 2 的判断部分）。

api 层刻意与 socket 解耦：``handle()`` 收已解好的请求要素、返回 ``Response``，
所以路由、校验、安全边界都能当普通函数测。真 socket 那一半在
``tests/test_webui_server.py``。

钉住的重点是**安全边界的形状**：只认回环 Host、Origin 必须同源、请求体有上限、
以及"导入/导出根本没有路径这个概念"——导出是下载，导入收 JSON 文本，
服务端不存在"按请求给的路径读写"这条代码路径。
"""

from __future__ import annotations

import base64
import json
import os
import queue
import threading
import time

import pytest

from webui.api import (
    MAX_BODY_BYTES,
    Api,
    Broadcaster,
    Response,
    content_disposition,
    host_is_loopback,
    origin_is_same_origin,
    safe_download_name,
)
from webui.session import (Availability, RunSession, SessionPaths,
                            ValidationError)


class FakeService:
    """api 与 service 的契约面就这几个方法。"""

    def __init__(self, cfg=None):
        self.calls: list = []
        self.cfg = cfg
        self.seen_paths: dict[str, bool] = {}
        self.temp_dirs: list[str] = []

    def detect_questions(self):
        self.calls.append("detect")

    def start_run(self):
        self.calls.append("run")

    def request_stop(self):
        self.calls.append("stop")

    def import_qr(self, path):
        self.calls.append("qr")
        self.seen_paths["qr"] = os.path.exists(path)
        self.temp_dirs.append(os.path.dirname(path))

    def import_config(self, path):
        self.calls.append("import")
        self.seen_paths["import"] = os.path.exists(path)
        self.temp_dirs.append(os.path.dirname(path))

    def save_default_config(self):
        self.calls.append("save-default")

    def current_config_for_export(self):
        return self.cfg

    # ---- 历史记录 ----
    def history_runs(self, limit=50):
        self.calls.append(("runs", limit))
        return [{"id": 1, "status": "finished"}]

    def history_stats(self):
        return {"total_runs": 1, "total_success": 1}

    def history_answers(self, run_id):
        self.calls.append(("answers", run_id))
        return [{"question_number": 1, "question_type": "single"}]

    def history_export(self, kind):
        self.calls.append(("export", kind))
        if kind not in ("runs", "answers"):
            raise ValidationError(f"未知的导出类型：{kind!r}")
        return f"history_{kind}.csv", "id\r\n1\r\n".encode("utf-8-sig")


    def purge_preview(self):
        self.calls.append("preview")
        return {"days": 7, "count": 3, "token": "tok-1"}

    def purge_confirm(self, token):
        self.calls.append(("purge", token))
        if token != "tok-1":
            raise ValidationError("确认凭据无效或已用过，请重新点一次「清理」")
        return 3


@pytest.fixture()
def svc():
    return FakeService()


@pytest.fixture()
def api(tmp_path, svc):
    session = RunSession(
        paths=SessionPaths(str(tmp_path / "data")),
        availability=Availability(config_io=True, history=True, qr=True,
                                  selenium=True),
    )
    made = Api(session, svc)
    return made, session, svc


def body_of(resp: Response):
    return json.loads(resp.body.decode("utf-8"))


def _sweep(dirs):
    """清掉"故意让 os.remove 失败"那两条用例留下的临时目录。"""
    import shutil

    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


def post(api, path, payload=None, *, raw=None):
    body = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")
    return api.handle("POST", path, host="127.0.0.1:8000", body=body)


# ============================================================ 回环与同源


@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1", True),
    ("127.0.0.1:8760", True),
    ("localhost", True),
    ("localhost:80", True),
    ("[::1]", True),
    ("[::1]:9000", True),
    ("evil.example", False),
    ("127.0.0.1.evil.example", False),
    ("0.0.0.0", False),
    ("attacker.test:8760", False),
    ("", False),
    (None, False),
])
def test_only_loopback_hosts_are_accepted(host, ok):
    assert host_is_loopback(host) is ok


@pytest.mark.parametrize("origin,host,ok", [
    (None, "127.0.0.1:8000", True),                       # 同导航请求没有 Origin
    ("http://127.0.0.1:8000", "127.0.0.1:8000", True),
    ("http://127.0.0.1", "127.0.0.1:80", True),           # 默认端口可省略
    ("http://evil.test", "127.0.0.1:8000", False),
    ("http://127.0.0.1:9999", "127.0.0.1:8000", False),
    ("not-a-url", "127.0.0.1:8000", False),
])
def test_cross_origin_is_rejected(origin, host, ok):
    assert origin_is_same_origin(origin, host) is ok


def test_a_rebinding_request_never_reaches_a_handler(api):
    made, session, svc = api
    resp = made.handle("GET", "/api/state", host="attacker.test")
    assert resp.status == 403
    assert svc.calls == []
    assert len(session.log_lines) == 0        # 连日志都不该被写


def test_cross_origin_post_is_rejected(api):
    made, _session, svc = api
    resp = made.handle("POST", "/api/detect", host="127.0.0.1:8000",
                       origin="http://evil.test", body=b"{}")
    assert resp.status == 403
    assert svc.calls == []


def test_an_oversized_body_is_refused_before_parsing(api):
    made, _session, _svc = api
    resp = made.handle("POST", "/api/field", host="127.0.0.1:8000",
                       body=b"x" * (MAX_BODY_BYTES + 1))
    assert resp.status == 413


# ============================================================ 路由


def test_unknown_route_is_a_404_not_a_crash(api):
    made, _session, _svc = api
    assert made.handle("GET", "/api/nope", host="127.0.0.1:8000").status == 404
    # 方法不匹配同样 404：路由表是 (method, path) 的键
    assert made.handle("GET", "/api/detect", host="127.0.0.1:8000").status == 404


def test_state_endpoint_reports_the_whole_form(api):
    made, session, _svc = api
    session.set_field("url", "https://x.test/s")
    payload = body_of(made.handle("GET", "/api/state", host="127.0.0.1:8000"))
    assert payload["ok"] is True
    assert payload["state"]["form"]["url"] == "https://x.test/s"
    assert payload["state"]["availability"]["config_io"] is True


def test_log_endpoint_can_resume_from_a_line_number(api):
    made, session, _svc = api
    for i in range(5):
        session.log(f"行 {i}")
    rows = body_of(made.handle("GET", "/api/log", host="127.0.0.1:8000",
                               query={"since": "3"}))["lines"]
    assert [r["n"] for r in rows] == [4, 5]


def test_log_endpoint_treats_a_junk_cursor_as_zero(api):
    made, session, _svc = api
    session.log("一行")
    rows = body_of(made.handle("GET", "/api/log", host="127.0.0.1:8000",
                               query={"since": "abc"}))["lines"]
    assert len(rows) == 1


def test_field_endpoint_writes_and_validates(api):
    made, session, _svc = api
    assert post(made, "/api/field", {"name": "count", "value": "7"}).status == 200
    assert session.count == 7
    bad = post(made, "/api/field", {"name": "count", "value": "0"})
    assert bad.status == 400
    assert "1 ~ 9999" in body_of(bad)["error"]


def test_field_endpoint_rejects_a_non_object_body(api):
    made, _session, _svc = api
    assert post(made, "/api/field", raw=b"[1,2]").status == 400
    assert post(made, "/api/field", raw=b"{not json").status == 400
    assert post(made, "/api/field", raw=b"\xff\xfe").status == 400


def test_weights_endpoint_is_the_only_way_the_table_gets_back(api):
    """权重表整列一次写回：前端在浏览器里，服务端是唯一能收下它的地方。

    键是 JSON 的字符串，而内部表示按 int 题号存 —— 这里不转，``parse_weights``
    拿到的是 ``"1"`` 而不是 ``1``，整列会静默变成"没填"，于是所有题退化成等权重。
    """
    made, session, _svc = api
    session.set_questions([{"q": 1, "type": "single", "choices": ["a", "b"]}])
    resp = post(made, "/api/weights", {"texts": {"1": "0.9,0.1", "2": None}})
    assert resp.status == 200
    assert session.weight_texts == {1: "0.9,0.1", 2: ""}
    assert body_of(resp)["state"]["table"][0]["text"] == "0.9,0.1"


def test_weights_endpoint_rejects_a_body_that_is_not_a_table(api):
    made, _session, _svc = api
    for payload in ({"texts": ["0.5,0.5"]}, {"texts": None}, {}):
        assert post(made, "/api/weights", payload).status == 400
    bad = post(made, "/api/weights", {"texts": {"第一题": "1,1"}})
    assert bad.status == 400
    assert "整数" in body_of(bad)["error"]


def test_detect_endpoint_delegates(api):
    made, _session, svc = api
    assert post(made, "/api/detect").status == 200
    assert svc.calls == ["detect"]


def test_an_empty_config_export_is_a_400_not_an_empty_download(api):
    made, _session, svc = api
    svc.cfg = None
    resp = post(made, "/api/config/export", {"name": "w"})
    assert resp.status == 400
    assert "权重配置" in body_of(resp)["error"]


def test_config_export_is_a_download_and_leaves_no_server_copy(api):
    made, session, svc = api
    svc.cfg = {1: {"type": "single", "weights": [0.5, 0.5]}}
    session.set_field("url", "https://x.test/s" + "y" * 300)
    resp = post(made, "/api/config/export", {"name": "我的配置"})
    assert resp.status == 200
    assert resp.headers["Content-Disposition"].startswith("attachment")
    doc = json.loads(resp.body.decode("utf-8"))
    # JSON 的键只能是字符串，导出形态与 Tk 那份 save_weight_config 一致
    assert doc["questions"] == {"1": {"type": "single", "weights": [0.5, 0.5]}}
    assert doc["meta"]["survey_url"] == session.url[:200]


def test_config_import_reads_the_request_body_not_a_path(api, tmp_path):
    made, _session, svc = api
    resp = post(made, "/api/config/import",
                {"name": "x", "content": '{"1": {"type": "single"}}'})
    assert resp.status == 200
    assert svc.calls == ["import"]
    assert svc.seen_paths["import"] is True       # 调用时那个临时文件确实存在
    assert all(not os.path.exists(d) for d in svc.temp_dirs)


def test_config_import_requires_content(api):
    made, _session, svc = api
    assert post(made, "/api/config/import", {"content": "  "}).status == 400
    assert post(made, "/api/config/import", {}).status == 400
    assert svc.calls == []


def test_qr_upload_is_written_to_our_temp_dir_and_removed_after(api):
    made, _session, svc = api
    blob = base64.b64encode(b"\x89PNG fake").decode()
    resp = post(made, "/api/qr", {"image_base64": blob, "filename": "码.png"})
    assert resp.status == 200
    assert svc.calls == ["qr"]
    assert svc.seen_paths["qr"] is True           # service 拿到时文件在
    assert svc.temp_dirs and all(not os.path.exists(d) for d in svc.temp_dirs)


def test_qr_upload_accepts_a_data_url_prefix(api):
    made, _session, svc = api
    blob = base64.b64encode(b"xx").decode()
    resp = post(made, "/api/qr", {"image_base64": f"data:image/png;base64,{blob}"})
    assert resp.status == 200
    assert svc.calls == ["qr"]


def test_qr_upload_rejects_junk_base64(api):
    made, _session, svc = api
    assert post(made, "/api/qr", {"image_base64": "!!!"}).status == 400
    assert post(made, "/api/qr", {"image_base64": ""}).status == 400
    assert svc.calls == []


def test_save_default_endpoint_delegates(api):
    made, _session, svc = api
    assert post(made, "/api/config/save-default").status == 200
    assert svc.calls == ["save-default"]


def test_shutdown_endpoint_only_asks_the_owner_to_finish(api):
    fired: list = []
    session = RunSession(paths=SessionPaths("x"))
    made = Api(session, FakeService(), request_shutdown=lambda: fired.append(1))
    resp = made.handle("POST", "/api/shutdown", host="127.0.0.1:8000")
    assert resp.status == 200 and fired == [1]


def test_a_blowing_handler_becomes_a_500_plus_one_log_line(api):
    made, session, svc = api

    def boom():
        raise RuntimeError("history db is locked")

    svc.detect_questions = boom
    resp = post(made, "/api/detect")
    assert resp.status == 500
    assert "history db is locked" in body_of(resp)["error"]
    assert any(t == "FAIL" and "history db is locked" in x
               for t, x in [(r["tag"], r["text"]) for r in session.log_lines])


# ============================================================ 名字消毒


@pytest.mark.parametrize("raw,expected", [
    ("我的配置", "我的配置"),
    ("weight config", "weight config"),
    ("../../etc/passwd", "____etc_passwd"),
    ("a/b\\c", "a_b_c"),
    (".hidden", "_hidden"),
    ("", "weight_config"),
    ("x\x00y", "x_y"),
])
def test_download_names_are_sanitized_not_rejected(raw, expected):
    assert safe_download_name(raw) == expected


def test_a_very_long_download_name_is_capped():
    assert len(safe_download_name("x" * 500)) == 80


# ============================================================ 广播


def test_publish_reaches_every_subscriber():
    bus = Broadcaster(capacity=5)
    a, b = bus.subscribe(), bus.subscribe()
    bus.publish("log", {"n": 1})
    assert a.get_nowait() == ("log", {"n": 1})
    assert b.get_nowait() == ("log", {"n": 1})
    bus.unsubscribe(a)
    assert bus.client_count == 1
    bus.publish("log", {"n": 2})
    assert b.get_nowait() == ("log", {"n": 2})
    assert a.qsize() == 0


def test_a_laggy_subscriber_drops_the_oldest_and_gets_a_gap_marker():
    bus = Broadcaster(capacity=2)
    sub = bus.subscribe()
    for i in range(4):
        bus.publish("log", {"n": i})
    drained = []
    while not sub.empty():
        drained.append(sub.get_nowait())
    assert drained == [("gap", {"dropped": 2}), ("log", {"n": 3})]


def test_session_events_flow_into_the_broadcaster(api):
    made, session, _svc = api
    sub = made.events.subscribe()
    session.log("一句话", "OK")
    session.set_status("运行中...")
    kinds = [sub.get(timeout=1)[0] for _ in range(2)]
    assert kinds == ["log", "status"]
    with pytest.raises(queue.Empty):
        sub.get_nowait()


def test_response_helpers_stay_json_and_utf8():
    resp = Response.json({"msg": "中文"})
    assert resp.content_type.endswith("charset=utf-8")
    assert "中文" in resp.body.decode("utf-8")
    err = Response.error(400, "坏了")
    assert body_of(err) == {"ok": False, "error": "坏了"}


# ================================================== 边角与清理（补覆盖）


def test_a_malformed_ipv6_host_is_not_loopback():
    assert host_is_loopback("[::1") is False


def test_an_empty_body_is_read_as_an_empty_object(api):
    """空 body 不该炸在 JSON 解析上；缺字段由校验给出 400。"""
    made, _session, _svc = api
    resp = made.handle("POST", "/api/field", host="127.0.0.1:8000", body=b"")
    assert resp.status == 400
    assert "未知字段" in body_of(resp)["error"]


def test_unsubscribing_the_same_client_twice_is_harmless():
    bus = Broadcaster()
    sub = bus.subscribe()
    bus.unsubscribe(sub)
    bus.unsubscribe(sub)
    assert bus.client_count == 0


def test_qr_temp_files_are_cleaned_even_when_the_cleanup_itself_fails(api, monkeypatch):
    """临时文件删不掉是环境问题，不该把一次成功的解析报成失败。"""
    import webui.api as api_mod

    made, _session, svc = api
    monkeypatch.setattr(api_mod.os, "remove",
                        lambda _p: (_ for _ in ()).throw(OSError("locked")))
    blob = base64.b64encode(b"xx").decode()
    resp = post(made, "/api/qr", {"image_base64": blob})
    assert resp.status == 200
    assert svc.calls == ["qr"]
    _sweep(svc.temp_dirs)


def test_config_temp_files_survive_a_failing_cleanup(api, monkeypatch):
    import webui.api as api_mod

    made, _session, svc = api
    monkeypatch.setattr(api_mod.os, "remove",
                        lambda _p: (_ for _ in ()).throw(OSError("locked")))
    resp = post(made, "/api/config/import", {"content": '{"1": {}}'})
    assert resp.status == 200
    assert svc.calls == ["import"]
    _sweep(svc.temp_dirs)


@pytest.mark.parametrize("name", ["我的配置", "../../etc/passwd", "ok name", "",
                                  "a\"b", "带 空格 和 中文"])
def test_content_disposition_is_always_header_safe(name):
    """HTTP 头只允许 latin-1 —— 中文名直接塞进去会让导出整个 500。"""
    header = content_disposition(name + ".json")
    header.encode("latin-1")                  # 这就是踩过的坑本体
    assert header.startswith("attachment; filename=")
    assert "filename*=UTF-8" in header
    assert chr(13) not in header and chr(10) not in header


def test_run_and_stop_routes_delegate_to_the_service(api):
    made, _session, svc = api
    assert post(made, "/api/run").status == 200
    assert post(made, "/api/stop").status == 200
    assert svc.calls == ["run", "stop"]


# ============================================================ 历史记录路由


def test_runs_route_returns_the_list_and_the_header_stats(api) -> None:
    made, _session, svc = api
    body = body_of(made.handle("GET", "/api/history/runs",
                               host="127.0.0.1:8000"))
    assert body["runs"] == [{"id": 1, "status": "finished"}]
    assert body["stats"]["total_runs"] == 1
    assert ("runs", 50) in svc.calls, "缺 limit 时走默认值"


def test_the_runs_limit_comes_from_the_query_and_a_junk_one_falls_back(api) -> None:
    """query 到 api 层已经是 ``dict[str, str]``（server 把 parse_qs 拍平过一次）。"""
    made, _session, svc = api
    made.handle("GET", "/api/history/runs", host="127.0.0.1:8000",
                query={"limit": "12"})
    made.handle("GET", "/api/history/runs", host="127.0.0.1:8000",
                query={"limit": "abc"})
    assert ("runs", 12) in svc.calls
    assert ("runs", 50) in svc.calls, "非法 limit 回默认而不是 500"


def test_answers_route_passes_the_run_id_through(api) -> None:
    made, _session, svc = api
    body = body_of(made.handle("GET", "/api/history/answers",
                              host="127.0.0.1:8000",
                              query={"run_id": "7"}))
    assert body["run_id"] == 7
    assert ("answers", 7) in svc.calls


def test_the_csv_export_is_a_download_and_never_a_path(api) -> None:
    """导出与配置导出同一条边界：响应体就是那份 CSV，请求里没有路径可给。"""
    made, _session, _svc = api
    resp = made.handle("GET", "/api/history/export", host="127.0.0.1:8000",
                       query={"kind": "runs"})
    assert resp.status == 200
    assert resp.content_type.startswith("text/csv")
    assert "history_runs.csv" in resp.headers["Content-Disposition"]
    assert resp.body.startswith(b"\xef\xbb\xbf"), "BOM 掉了 Excel 就是一屏乱码"


def test_an_unknown_export_kind_is_a_400_with_the_reason(api) -> None:
    made, _session, _svc = api
    resp = made.handle("GET", "/api/history/export", host="127.0.0.1:8000",
                       query={"kind": "../../etc/passwd"})
    assert resp.status == 400
    assert "导出类型" in body_of(resp)["error"]


def test_purge_without_a_token_only_previews(api) -> None:
    """第一步不许有任何删除动作 —— 点一下「清理」就清空历史是不可接受的。"""
    made, _session, svc = api
    body = body_of(post(made, "/api/history/purge", {}))
    assert body["count"] == 3 and body["days"] == 7
    assert body["token"] == "tok-1"
    assert svc.calls == ["preview"], "预览阶段不该调 purge_confirm"


def test_purge_with_the_token_deletes_and_reports_the_count(api) -> None:
    made, _session, svc = api
    body = body_of(post(made, "/api/history/purge", {"token": "tok-1"}))
    assert body["removed"] == 3
    assert ("purge", "tok-1") in svc.calls


def test_a_forged_token_is_rejected_at_the_http_layer(api) -> None:
    made, _session, svc = api
    resp = post(made, "/api/history/purge", {"token": "guess"})
    assert resp.status == 400
    assert "state" not in body_of(resp)
    assert ("purge", "guess") in svc.calls, "service 自己会拒，路由不替它兜"


def test_history_routes_answer_only_get_and_post_as_declared(api) -> None:
    """方法用错就是 404：导出若允许 POST，等于多一条绕过 body 上限的写法。"""
    made, _session, _svc = api
    assert made.handle("POST", "/api/history/runs",
                       host="127.0.0.1:8000").status == 404
    assert made.handle("GET", "/api/history/purge",
                       host="127.0.0.1:8000").status == 404


def test_the_history_refresh_avoids_shipping_the_whole_state_back(api) -> None:
    """换视图/刷新只要批次，不要整张权重表。

    没有这个端点的话前端只能退回 ``/api/state`` —— 那里面有 13 题的表，
    和"刚跑完的那几批"没有半点关系。
    """
    made, _session, svc = api
    body = body_of(post(made, "/api/history/refresh", {}))
    assert body["runs"] and "state" not in body
    assert ("runs", 50) in svc.calls


# ============================================================ 确认反向通道


def test_a_confirm_without_an_id_is_a_400(api) -> None:
    made, _session, _svc = api
    assert post(made, "/api/confirm", {"accept": True}).status == 400


def test_answering_a_confirm_that_already_expired_says_so(api) -> None:
    """静默接受一个过期答案，用户只会觉得"我明明点了继续"。"""
    made, _session, _svc = api
    resp = post(made, "/api/confirm", {"id": "stale", "accept": True})
    assert resp.status == 400
    assert "过期" in body_of(resp)["error"]


def test_the_http_answer_reaches_the_thread_that_is_asking(api) -> None:
    """整条往返：service 线程阻塞在 ask()，HTTP 请求把答案递回去。

    这里走真线程而不是替身 —— 通道要验的就是跨线程唤醒。
    """
    made, session, _svc = api
    out = {}
    started = threading.Event()

    def ask():
        started.set()
        out["value"] = session.confirms.ask("断点续传", "是否从第 4 份继续？")

    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    assert started.is_set()
    for _ in range(150):
        if session.confirms.pending():
            break
        time.sleep(0.02)

    cid = session.confirms.pending()[0]["id"]
    body = body_of(post(made, "/api/confirm", {"id": cid, "accept": True}))
    assert body["state"]["confirms"] == [], "答完之后快照里就该撤下"
    worker.join(5)
    assert out["value"] is True
