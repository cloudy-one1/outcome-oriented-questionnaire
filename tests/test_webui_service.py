"""``webui/session.py`` 与 ``webui/service.py`` 的离线契约（设计稿 §10 步骤 1）。

这一层是 webui"可测的那一半"：不 import tkinter、不起 HTTP、不碰真浏览器 ——
探测与扫码全部对着假 driver / 假解码器跑，``spawn`` 注入成同步执行，
所以线程里的分支也能一行行走到（Tk 那份 controller 的 worker 长期只有 37% 覆盖，
就是因为闭包里的分支没法不起浏览器就测）。

钉住的东西分三类：

    1. **校验真的在服务端**（§5 修第 1、2 条）：手输非数字、0、10000、无 scheme 的 URL
       都要被拒并给出人话；Tk 那边是"点了没反应"。
    2. **刻意原样保留的行为**（§5「不碰」）：UC 在切到 Edge 后不清零、URL 静默截断 500、
       GUI 侧隐私默认 True。这些不是漏改，是决定不改 —— 所以要有测试钉住，
       免得下一个人当 bug 顺手"修好"。
    3. **降级路径的文案**：可选依赖缺失时用户看到什么，与 Tk 宿主逐字对齐。
"""

from __future__ import annotations

import json
import os

import pytest

from src import config as cfg_module
from webui.session import (
    Availability,
    RunSession,
    SessionPaths,
    ValidationError,
)
from webui.service import WebService, format_weights_for_entry


# ------------------------------------------------------------------ 假件


class FakeSwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def frame(self, index):
        self.driver.frame = index

    def default_content(self):
        self.driver.frame = None


class FakeDriver:
    """够用的 Selenium 替身：只实现探测路径真调的那几个方法。"""

    def __init__(self, *, counts=(3,), ready="complete", get_error=None):
        self.counts = list(counts)          # [主文档, frame0, frame1, ...]
        self.ready = ready
        self.get_error = get_error
        self.frame = None
        self.switch_to = FakeSwitchTo(self)
        self.visited: list[str] = []
        self.quit_called = False

    def get(self, url):
        self.visited.append(url)
        if self.get_error:
            raise self.get_error

    def execute_script(self, js):
        if "readyState" in js:
            return self.ready
        if "iframe" in js:
            return max(0, len(self.counts) - 1)
        idx = 0 if self.frame is None else self.frame + 1
        return self.counts[idx] if idx < len(self.counts) else 0

    def quit(self):
        self.quit_called = True


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, object]] = []

    def __call__(self, kind, payload):
        self.events.append((kind, payload))

    def kinds(self):
        return [k for k, _ in self.events]

    def last(self, kind):
        for k, payload in reversed(self.events):
            if k == kind:
                return payload
        raise AssertionError(f"没有 {kind} 事件")


def make_session(root, **flags):
    paths = SessionPaths(str(root))
    avail = Availability(**{"config_io": True, "history": True, "qr": True,
                            "selenium": True, **flags})
    rec = Recorder()
    return RunSession(paths=paths, availability=avail, emit=rec), rec


@pytest.fixture(autouse=True)
def _clean_weight_config():
    cfg_module.WEIGHT_CONFIG.clear()
    yield
    cfg_module.WEIGHT_CONFIG.clear()


@pytest.fixture()
def session(tmp_path):
    """每个用例一棵独立的数据树 —— 否则「有没有默认配置文件」会跨用例串味。"""
    return make_session(tmp_path / "data")


def make_service(sess, **kw):
    params = dict(
        save_weight_config=kw.get("save"),
        load_weight_config=kw.get("load"),
        validate_weight_config=kw.get("validate"),
        build_config=kw.get("build"),
        create_driver=kw.get("create_driver"),
        detect_questions=kw.get("detect"),
        is_smart_verification_showing=kw.get("verification_showing"),
        wait_for_manual_verification=kw.get("wait_verification"),
        decode_qr=kw.get("decode_qr"),
        spawn=kw.get("spawn", lambda fn: fn()),
        sleeper=kw.get("sleeper", lambda _s: None),
    )
    return WebService(sess, **params)


def logs_of(sess):
    return [(row["tag"], row["text"]) for row in sess.log_lines]


# ================================================================== session


def test_url_is_stripped_but_not_otherwise_restricted(session):
    """只校验 scheme 与非空 —— 不加新限制（设计稿 §5 修第 2 条）。"""
    sess, _ = session
    sess.set_field("url", "  https://ww.wjx.top/vm/abc.aspx  ")
    assert sess.url == "https://ww.wjx.top/vm/abc.aspx"
    sess.set_field("url", "http://insecure.example/x")   # http 照样放行
    assert sess.url.startswith("http://")


def test_empty_url_is_rejected_with_a_readable_message(session):
    sess, _ = session
    with pytest.raises(ValidationError, match="不能为空"):
        sess.set_field("url", "   ")


def test_url_without_scheme_is_rejected(session):
    sess, _ = session
    with pytest.raises(ValidationError, match="http"):
        sess.set_field("url", "ww.wjx.top/vm/abc")


def test_long_url_is_still_truncated_to_500_like_tk_does(session):
    """Tk 会把 URL 截到 500 后真的拿去导航 —— 刻意原样保留，所以钉住而不是修好。"""
    sess, _ = session
    sess.set_field("url", "https://x.test/" + "a" * 600)
    assert len(sess.url) == 500


def test_count_accepts_string_input_and_rejects_junk(session):
    sess, _ = session
    sess.set_field("count", "12")
    assert sess.count == 12
    # Tk 在这里抛未捕获 TclError，表现为"点了没反应"
    with pytest.raises(ValidationError, match="整数"):
        sess.set_field("count", "abc")


@pytest.mark.parametrize("bad", [0, -1, 10000, 99999])
def test_count_range_is_enforced_on_typed_input(session, bad):
    """Tk 只在 +/− 按钮上夹范围，手输不受约束。"""
    sess, _ = session
    with pytest.raises(ValidationError, match="1 ~ 9999"):
        sess.set_field("count", bad)


@pytest.mark.parametrize("ok", [1, 9999, "5"])
def test_count_accepts_the_ends_of_the_range(session, ok):
    sess, _ = session
    sess.set_field("count", ok)
    assert sess.count == int(ok)


def test_browser_is_normalized_and_unknown_is_rejected(session):
    sess, _ = session
    sess.set_field("browser", "Edge")
    assert sess.browser == "edge"
    with pytest.raises(ValidationError, match="firefox"):
        sess.set_field("browser", "firefox")


def test_uc_stays_set_when_browser_moves_to_edge(session):
    """刻意不清零 —— 与 Tk 宿主一致（§5「不碰」第一条）。"""
    sess, _ = session
    sess.set_field("browser", "chrome")
    sess.set_field("use_uc", True)
    sess.set_field("browser", "edge")
    assert sess.use_uc is True


def test_privacy_default_matches_the_tk_host_not_the_cli(session):
    sess, _ = session
    assert sess.no_record_text is True


def test_unknown_field_is_rejected(session):
    sess, _ = session
    with pytest.raises(ValidationError, match="未知字段"):
        sess.set_field("total_rounds", 5)


def test_log_rows_get_increasing_line_numbers_and_tags(session):
    sess, rec = session
    first = sess.log("第一行", "OK")
    second = sess.log("第二行")
    assert (first["n"], second["n"]) == (1, 2)
    assert first["tag"] == "OK" and second["tag"] == "INFO"
    assert len(first["ts"]) == 8           # HH:MM:SS
    assert rec.last("log") is second       # 每条都推给出口


def test_log_ring_buffer_drops_oldest_not_newest(tmp_path):
    sess = RunSession(paths=SessionPaths(str(tmp_path)), log_capacity=3)
    for i in range(5):
        sess.log(f"行 {i}")
    assert [r["text"] for r in sess.log_lines] == ["行 2", "行 3", "行 4"]
    assert sess._log_seq == 5              # 行号继续往前走，不回卷


def test_broken_emit_sink_cannot_break_the_caller(tmp_path):
    def boom(_kind, _payload):
        raise RuntimeError("sink is gone")

    sess = RunSession(paths=SessionPaths(str(tmp_path)), emit=boom)
    sess.log("照常")
    sess.set_status("运行中...")
    sess.update_progress(success=1, fail=0, current_round=1, total_rounds=2)
    assert sess.status == "运行中..."


def test_status_event_carries_a_tone_name_not_a_hex_color(session):
    sess, rec = session
    sess.set_status("运行中...")
    assert rec.last("status") == {"text": "运行中...", "tone": "running"}
    sess.set_status("随便什么")
    assert rec.last("status")["tone"] == "neutral"


def test_busy_set_replaces_tk_button_disabling(session):
    sess, rec = session
    sess.begin_command("detect")
    assert sess.is_busy("detect")
    assert "detect" in rec.last("state")["busy"]
    sess.end_command("detect")
    assert not sess.is_busy("detect")


def test_progress_percent_is_zero_when_nothing_is_planned(session):
    sess, rec = session
    sess.update_progress(success=0, fail=0, current_round=0, total_rounds=0)
    assert rec.last("progress")["percent"] == 0.0
    sess.update_progress(success=3, fail=1, current_round=4, total_rounds=8)
    assert rec.last("progress")["percent"] == pytest.approx(50.0)


def test_request_stop_only_while_running(session):
    sess, _ = session
    sess.request_stop()
    assert sess.status == "就绪"
    sess.start_run(3)
    sess.request_stop()
    assert sess.status == "正在停止..."
    sess.finish_run()
    assert (sess.running, sess.status) == (False, "就绪")


def test_detecting_questions_seeds_weight_texts_and_keeps_edited_ones(session):
    sess, _ = session
    sess.set_weight_texts({1: "0.5,0.5"})
    sess.set_questions([{"q": 1, "type": "single"}, {"q": 2, "type": "scale"}])
    assert sess.weight_texts == {1: "0.5,0.5", 2: ""}


def test_snapshot_reports_availability_and_limits(session):
    sess, _ = session
    snap = sess.snapshot()
    assert snap["availability"]["config_io"] is True
    assert snap["limits"] == {"count_min": 1, "count_max": 9999}
    assert snap["form"]["count"] == 1
    assert snap["questions"] == 0


def test_paths_are_snapshotted_once(tmp_path):
    paths = SessionPaths(str(tmp_path))
    assert paths.config_dir == os.path.join(str(tmp_path), "configs")
    assert paths.default_config_path.endswith(
        os.path.join("configs", "default_weight_config.json"))
    assert paths.history_db_path.endswith(os.path.join("data", "history.db"))


def test_availability_probe_returns_a_usable_snapshot():
    avail = Availability.probe()
    assert isinstance(avail, Availability)
    assert set(avail.as_dict()) == {"config_io", "history", "qr", "selenium"}
    # 这个仓库的测试环境里 config_io 与 history 一定在，探测说假话要能被发现
    assert avail.config_io is True
    assert avail.history is True


# ================================================================== service


def test_detect_without_selenium_says_so_and_starts_no_thread(session):
    sess, _ = session
    sess.set_field("url", "https://x.test/s")
    spawned: list = []
    svc = make_service(sess, create_driver=None, spawn=spawned.append)
    svc.detect_questions()
    assert spawned == []
    assert ("FAIL", "探测模块未加载，请检查 src 导入") in logs_of(sess)


def test_detect_with_empty_url_raises_instead_of_popping_a_dialog(session):
    sess, _ = session
    svc = make_service(sess, create_driver=lambda *a, **k: FakeDriver(),
                       detect=lambda d: [])
    with pytest.raises(ValidationError, match="URL"):
        svc.detect_questions()


def test_detect_happy_path_lands_questions_and_cleans_up(session):
    sess, _ = session
    driver = FakeDriver(counts=(7,))
    seen: dict = {}

    def create(browser, use_uc=False, **kw):
        seen["browser"], seen["use_uc"] = browser, use_uc
        return driver

    def detect(d):
        return [{"q": 1, "type": "single", "choices": ["a", "b"]},
                {"q": 2, "type": "scale"}]

    sess.set_field("url", "https://x.test/s")
    sess.set_field("browser", "chrome")
    sess.set_field("use_uc", True)
    svc = make_service(sess, create_driver=create, detect=detect)
    svc.detect_questions()

    assert seen == {"browser": "chrome", "use_uc": True}
    assert driver.visited == ["https://x.test/s"]
    assert driver.quit_called is True
    assert len(sess.questions) == 2
    assert ("OK", "探测完成，共 2 题（1 单选、1 量表）") in logs_of(sess)
    assert sess.status == "就绪"
    assert not sess.is_busy("detect")


def test_detect_passes_edge_and_uc_false_to_the_factory(session):
    """UC 在 Edge 下仍原样传下去 —— 引擎侧自己忽略，宿主不擅自改值。"""
    sess, _ = session
    calls: dict = {}

    def create(browser, use_uc=False, **kw):
        calls.update(browser=browser, use_uc=use_uc)
        return FakeDriver()

    sess.set_field("url", "https://x.test/s")
    sess.set_field("browser", "edge")
    sess.set_field("use_uc", True)
    make_service(sess, create_driver=create,
                 detect=lambda d: [{"q": 1, "type": "single"}]).detect_questions()
    assert calls == {"browser": "edge", "use_uc": True}


def test_detect_falls_back_to_iframes_and_stays_in_the_found_frame(session):
    sess, _ = session
    driver = FakeDriver(counts=(0, 0, 5))

    def detect(d):
        # 找到题目的那一刻应当停在 frame 2 上
        assert d.frame == 1
        return [{"q": 1, "type": "single"}]

    sess.set_field("url", "https://x.test/s")
    make_service(sess, create_driver=lambda *a, **k: driver,
                 detect=detect).detect_questions()
    assert ("OK", "探测完成，共 1 题（1 单选）") in logs_of(sess)
    assert driver.quit_called is True


def test_detect_without_any_question_element_fails_before_calling_detect(session):
    sess, _ = session
    called: list = []
    driver = FakeDriver(counts=(0,))
    sess.set_field("url", "https://x.test/s")
    make_service(sess, create_driver=lambda *a, **k: driver,
                 detect=lambda d: called.append(1) or []).detect_questions()
    assert called == []
    assert ("FAIL", "未能在页面中找到题目元素") in logs_of(sess)
    assert driver.quit_called is True


def test_detect_verification_timeout_stops_and_reports(session):
    sess, _ = session
    driver = FakeDriver()
    sess.set_field("url", "https://x.test/s")
    make_service(
        sess,
        create_driver=lambda *a, **k: driver,
        detect=lambda d: [{"q": 1, "type": "single"}],
        verification_showing=lambda d: True,
        wait_verification=lambda d: False,
    ).detect_questions()
    tag_text = logs_of(sess)
    assert ("WARN", "检测到验证码，请在浏览器中手动完成...") in tag_text
    assert ("FAIL", "验证超时，探测失败") in tag_text
    assert sess.status == "就绪"


def test_detect_verification_cleared_continues(session):
    sess, _ = session
    sess.set_field("url", "https://x.test/s")
    make_service(
        sess,
        create_driver=lambda *a, **k: FakeDriver(),
        detect=lambda d: [{"q": 1, "type": "single"}],
        verification_showing=lambda d: True,
        wait_verification=lambda d: True,
    ).detect_questions()
    assert ("OK", "探测完成，共 1 题（1 单选）") in logs_of(sess)


def test_detect_reports_driver_errors_and_still_releases_busy(session):
    sess, _ = session
    driver = FakeDriver(get_error=RuntimeError("net down"))
    sess.set_field("url", "https://x.test/s")
    make_service(sess, create_driver=lambda *a, **k: driver,
                 detect=lambda d: []).detect_questions()
    assert ("FAIL", "探测失败: RuntimeError: net down") in logs_of(sess)
    assert not sess.is_busy("detect")
    assert sess.status == "就绪"


def test_detect_with_zero_questions_reports_it(session):
    sess, _ = session
    sess.set_field("url", "https://x.test/s")
    make_service(sess, create_driver=lambda *a, **k: FakeDriver(),
                 detect=lambda d: []).detect_questions()
    assert ("FAIL", "未探测到任何题目") in logs_of(sess)


def test_detect_is_not_started_twice_while_running(session):
    sess, _ = session
    sess.set_field("url", "https://x.test/s")
    spawned: list = []
    svc = make_service(sess, create_driver=lambda *a, **k: FakeDriver(),
                       detect=lambda d: [], spawn=spawned.append)
    svc.detect_questions()
    svc.detect_questions()
    assert len(spawned) == 1


def test_detect_summary_for_unrecognized_types(session):
    sess, _ = session
    make_service(sess).on_questions_detected([{"q": 1, "type": "weird"}])
    assert ("OK", "探测完成，共 1 题（未识别题型）") in logs_of(sess)


def test_qr_without_the_module_says_so(session):
    sess, _ = session
    make_service(sess, decode_qr=None).import_qr("whatever.png")
    assert ("FAIL", "二维码模块未加载，无法导入") in logs_of(sess)


def test_qr_success_fills_the_url_field(session):
    sess, _ = session
    svc = make_service(sess, decode_qr=lambda p: "https://x.test/ok")
    svc.import_qr(os.path.join("tmp", "码.png"))
    assert sess.url == "https://x.test/ok"
    assert ("OK", "二维码解析成功 ✓: https://x.test/ok") in logs_of(sess)
    assert sess.status == "就绪"
    assert not sess.is_busy("qr")


def test_qr_that_decodes_to_junk_is_not_written_into_the_field(session):
    sess, _ = session
    sess.set_field("url", "https://keep.me/x")
    make_service(sess, decode_qr=lambda p: "not a url").import_qr("c.png")
    assert sess.url == "https://keep.me/x"
    assert any(t == "FAIL" and "不是合法 URL" in x for t, x in logs_of(sess))


def test_qr_that_finds_nothing_reports_it(session):
    sess, _ = session
    make_service(sess, decode_qr=lambda p: None).import_qr("c.png")
    assert ("FAIL", "未识别到二维码内容 ✗") in logs_of(sess)


def test_qr_decoder_exception_is_reported_not_raised(session):
    sess, _ = session

    def boom(_p):
        raise ValueError("bad image")

    make_service(sess, decode_qr=boom).import_qr("c.png")
    assert any(t == "FAIL" and "bad image" in x for t, x in logs_of(sess))
    assert sess.status == "就绪"


def test_export_without_config_io_says_so(session):
    sess, _ = session
    sess.availability.config_io = False
    make_service(sess, save=lambda *a, **k: None,
                 build=lambda: {1: {"type": "single"}}).export_config("out.json")
    assert ("FAIL", "未加载 src/config_io，无法导出配置") in logs_of(sess)


def test_export_with_nothing_parsed_warns_without_popping(session):
    sess, _ = session
    make_service(sess, save=lambda *a, **k: None, build=lambda: {}).export_config("o.json")
    assert ("WARN", "当前没有可导出的权重配置（请先探测题目）") in logs_of(sess)


def test_current_config_for_export_is_the_public_seam_for_the_api(session):
    """api 层只认这个方法；它没测到就等于导出接口的入口没人看过。"""
    sess, _ = session
    svc = make_service(sess, build=lambda: {1: {"type": "single"}})
    assert svc.current_config_for_export() == {1: {"type": "single"}}
    empty = make_service(sess, build=lambda: {})
    assert empty.current_config_for_export() is None


def test_export_without_the_parser_wired_says_which_step_owns_it(session):
    sess, _ = session
    make_service(sess, save=lambda *a, **k: None).export_config("o.json")
    assert any(t == "FAIL" and "步骤 4" in x for t, x in logs_of(sess))


def test_export_writes_meta_and_reports_relative_path(tmp_path, session):
    sess, _ = session
    sess.set_field("url", "https://x.test/s" + "y" * 300)
    saved: dict = {}

    def save(path, cfg, meta=None):
        saved["path"], saved["cfg"], saved["meta"] = path, cfg, meta

    target = os.path.join(sess.paths.user_data_root, "configs", "w.json")
    make_service(sess, save=save,
                 build=lambda: {1: {"type": "single", "weights": [1, 1]}}
                 ).export_config(target)
    assert saved["cfg"] == {1: {"type": "single", "weights": [1, 1]}}
    assert saved["meta"]["name"] == "w"
    assert saved["meta"]["survey_url"] == sess.url[:200]
    assert "共 1 道题" in saved["meta"]["description"]
    assert ("OK", f"✓ 已导出配置 → {os.path.join('configs', 'w.json')}") in logs_of(sess)


def test_export_survives_a_path_on_another_drive(tmp_path, session):
    """Tk 在 relpath 跨盘时抛 ValueError，导出明明成功了却报 FAIL。"""
    sess, _ = session
    done: list = []
    make_service(sess, save=lambda *a, **k: done.append(a[0]),
                 build=lambda: {1: {"type": "single"}}
                 ).export_config("Z:\\elsewhere\\w.json")
    assert done == ["Z:\\elsewhere\\w.json"]
    assert any(t == "OK" and "Z:" in x for t, x in logs_of(sess))


def test_export_logs_the_first_validation_warning(session):
    sess, _ = session
    make_service(
        sess,
        save=lambda *a, **k: None,
        validate=lambda cfg: ["Q1 权重和为 0", "Q2 长度不符"],
        build=lambda: {1: {}},
    ).export_config("o.json")
    assert any(t == "WARN" and "2 条警告" in x and "Q1 权重和为 0" in x
               for t, x in logs_of(sess))


def test_export_failure_is_reported(session):
    sess, _ = session

    def save(*a, **k):
        raise OSError("disk full")

    make_service(sess, save=save, build=lambda: {1: {}}).export_config("o.json")
    assert any(t == "FAIL" and "disk full" in x for t, x in logs_of(sess))


def test_save_default_creates_the_directory_and_uses_the_fixed_path(tmp_path, session):
    sess, _ = session
    written: dict = {}

    def save(path, cfg, meta=None):
        written["path"] = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

    make_service(sess, save=save, build=lambda: {1: {"type": "single"}}
                 ).save_default_config()
    assert written["path"] == sess.paths.default_config_path
    assert os.path.exists(written["path"])
    assert any(t == "OK" and "另存默认" in x for t, x in logs_of(sess))


def test_import_missing_file_fails_without_raising(session):
    sess, _ = session
    make_service(sess, load=lambda p: (None, None)).import_config("nope.json")
    assert any(t == "FAIL" and "配置文件不存在" in x for t, x in logs_of(sess))


def test_import_replaces_the_global_config_rather_than_merging(session, tmp_path):
    """合并会让上一份配置里本份没有的题号静默残留。"""
    sess, _ = session
    cfg_module.WEIGHT_CONFIG.update({9: {"type": "single", "weights": [1, 1]}})
    path = tmp_path / "w.json"
    path.write_text("{}", encoding="utf-8")
    make_service(
        sess,
        load=lambda p: ({1: {"type": "single", "weights": [0.5, 0.5]}},
                        {"name": "我的配置"}),
    ).import_config(str(path))
    assert set(cfg_module.WEIGHT_CONFIG) == {1}
    assert any(t == "OK" and "我的配置" in x for t, x in logs_of(sess))


def test_import_syncs_only_questions_that_are_on_the_table(session, tmp_path):
    sess, _ = session
    cfg_file = tmp_path / "w.json"
    cfg_file.write_text("{}", encoding="utf-8")
    sess.set_questions([{"q": 1, "type": "single"}, {"q": 2, "type": "single"}])
    make_service(
        sess,
        load=lambda p: ({1: {"type": "single", "weights": [0.25, 0.75]},
                         7: {"type": "single", "weights": [1, 1]}}, {}),
    ).import_config(str(cfg_file))
    assert sess.weight_texts[1] == "0.2500,0.7500"
    assert sess.weight_texts[2] == ""
    assert 7 not in sess.weight_texts
    assert any("同步到表格 1 道" in x for _, x in logs_of(sess))


def test_import_logs_every_validation_warning(session, tmp_path):
    sess, _ = session
    cfg_file = tmp_path / "w.json"
    cfg_file.write_text("{}", encoding="utf-8")
    make_service(
        sess,
        load=lambda p: ({1: {}}, {}),
        validate=lambda cfg: ["A 有问题", "B 有问题"],
    ).import_config(str(cfg_file))
    texts = [x for _, x in logs_of(sess)]
    assert "[校验警告] A 有问题" in texts
    assert "配置载入 · 校验警告 2 条" in texts


def test_import_reports_a_structurally_broken_file(session, tmp_path):
    sess, _ = session
    cfg_file = tmp_path / "broken.json"
    cfg_file.write_text("[]", encoding="utf-8")

    def load(_p):
        raise ValueError("顶层不是 object")

    make_service(sess, load=load).import_config(str(cfg_file))
    assert any(t == "FAIL" and "顶层不是 object" in x for t, x in logs_of(sess))


def test_auto_load_is_a_noop_without_a_default_file(session):
    sess, rec = session
    make_service(sess, load=lambda p: ({}, {})).auto_load_default_config()
    assert rec.kinds() == []


def test_auto_load_of_a_broken_default_file_degrades_to_one_log_line(session):
    """启动期任何失败都只降级，不许把服务起不来。"""
    sess, _ = session
    os.makedirs(os.path.dirname(sess.paths.default_config_path), exist_ok=True)
    with open(sess.paths.default_config_path, "w", encoding="utf-8") as fh:
        fh.write("{}")

    def load(_p):
        raise RuntimeError("坏文件")

    make_service(sess, load=load).auto_load_default_config()
    assert any(t == "FAIL" and "坏文件" in x for t, x in logs_of(sess))


# ------------------------------------------------------- cfg → 表格字符串


def test_format_weights_prefers_weights():
    assert format_weights_for_entry({"weights": [0.5, 0.5]}) == "0.5000,0.5000"


def test_format_weights_falls_back_to_options():
    assert format_weights_for_entry({"options": ["张三", "李四"]}) == "张三,李四"


def test_format_weights_renders_matrix_rows_sorted_numerically():
    cfg = {"row_weights": {"10": [1, 0], "2": [0, 1], "1": [1, 1]}}
    assert format_weights_for_entry(cfg) == "1:1,1 | 2:0,1 | 10:1,0"


def test_format_weights_of_an_empty_entry_is_blank():
    assert format_weights_for_entry({}) == ""
    assert format_weights_for_entry({"row_weights": {}}) == ""


# ================================================ 降级与收尾路径（补覆盖）


def test_save_default_without_config_io_says_so(session):
    sess, _ = session
    sess.availability.config_io = False
    make_service(sess, save=lambda *a, **k: None,
                 build=lambda: {1: {}}).save_default_config()
    assert ("FAIL", "未加载 src/config_io，无法另存默认配置") in logs_of(sess)


def test_save_default_with_nothing_parsed_warns(session):
    sess, _ = session
    make_service(sess, save=lambda *a, **k: None,
                 build=lambda: {}).save_default_config()
    assert ("WARN", "当前没有可导出的权重配置（请先探测题目）") in logs_of(sess)


def test_save_default_reports_a_write_failure(session):
    sess, _ = session

    def save(*a, **k):
        raise PermissionError("read only")

    make_service(sess, save=save, build=lambda: {1: {}}).save_default_config()
    assert any(t == "FAIL" and "read only" in x for t, x in logs_of(sess))


def test_import_without_config_io_says_so(session):
    sess, _ = session
    sess.availability.config_io = False
    make_service(sess, load=lambda p: ({}, {})).import_config("w.json")
    assert ("FAIL", "未加载 src/config_io，无法导入配置") in logs_of(sess)


def test_auto_load_degrades_when_applying_the_config_blows_up(session, monkeypatch):
    """启动路径上任何异常都只留一句 WARN —— 一个坏默认文件不该拦停整个服务。

    要 patch 的是 ``webui.service`` 里那个已绑定的名字，不是 ``src.config_io`` 的模块属性：
    ``from x import y`` 在导入时就绑好了，改模块属性骗不过去。
    """
    import webui.service as svc_mod

    sess, _ = session
    os.makedirs(os.path.dirname(sess.paths.default_config_path), exist_ok=True)
    with open(sess.paths.default_config_path, "w", encoding="utf-8") as fh:
        fh.write("{}")

    def blow_up(*a, **k):
        raise TypeError("题号必须是整数")

    monkeypatch.setattr(svc_mod, "apply_weight_config", blow_up)
    make_service(
        sess, load=lambda p: ({"x": {"type": "nope"}}, {}),
    ).auto_load_default_config()
    assert any(t == "WARN" and "自动载入默认配置跳过" in x for t, x in logs_of(sess))


def test_a_driver_that_refuses_to_quit_still_finishes_cleanly(session):
    sess, _ = session

    class NastyDriver(FakeDriver):
        def quit(self):
            raise RuntimeError("browser already gone")

    sess.set_field("url", "https://x.test/s")
    make_service(sess, create_driver=lambda *a, **k: NastyDriver(),
                 detect=lambda d: [{"q": 1, "type": "single"}]).detect_questions()
    assert ("OK", "探测完成，共 1 题（1 单选）") in logs_of(sess)
    assert not sess.is_busy("detect")


def test_a_page_that_never_becomes_ready_still_gets_probed(session):
    """readyState 等不到只 debug —— 与 Tk 宿主一样继续往下探。"""
    sess, _ = session

    class StuckDriver(FakeDriver):
        def execute_script(self, js):
            if "readyState" in js:
                raise RuntimeError("wait timed out")
            return super().execute_script(js)

    driver = StuckDriver()
    sess.set_field("url", "https://x.test/s")
    make_service(sess, create_driver=lambda *a, **k: driver,
                 detect=lambda d: [{"q": 1, "type": "single"}]).detect_questions()
    assert ("OK", "探测完成，共 1 题（1 单选）") in logs_of(sess)


def test_the_default_spawn_really_starts_a_thread(session):
    """生产用的是默认 spawn；测试全注入同步执行会把它整条留成死代码。"""
    import threading

    sess, _ = session
    done = threading.Event()
    sess.set_field("url", "https://x.test/s")
    # 直接构造 WebService 时，验证那两个函数用的是**真实现** —— 它对假 driver
    # 回了句"有验证码"，于是真等 120s。这条测的是默认 spawn，不是默认依赖。
    svc = WebService(sess, create_driver=lambda *a, **k: FakeDriver(),
                     detect_questions=lambda d: done.set() or [],
                     is_smart_verification_showing=lambda d: False,
                     sleeper=lambda _s: None)
    svc.detect_questions()
    assert done.wait(5), "默认 spawn 没把 worker 跑起来"


@pytest.mark.parametrize("broken", [
    "src.config_io", "src.history", "gui.qr_utils", "selenium",
])
def test_probe_reports_false_for_whichever_optional_import_breaks(monkeypatch, broken):
    real_import = __import__

    def fake_import(name, *a, **k):
        if name == broken:
            raise ImportError(f"{name} is gone")
        return real_import(name, *a, **k)

    monkeypatch.setitem(__import__("builtins").__dict__, "__import__", fake_import)
    avail = Availability.probe()
    key = {"src.config_io": "config_io", "src.history": "history",
           "gui.qr_utils": "qr", "selenium": "selenium"}[broken]
    assert getattr(avail, key) is False


def test_probe_reports_true_for_everything_present_here():
    """这台机器上 config_io / history / selenium 一定在；探测说假话要当场被抓住。"""
    avail = Availability.probe()
    assert (avail.config_io, avail.history, avail.selenium) == (True, True, True)


def test_detect_without_selenium_skips_the_ready_wait(session, monkeypatch):
    """没装 selenium 时不该去等 readyState —— 直接照现在的 DOM 探。"""
    import webui.service as svc_mod

    sess, _ = session
    sess.set_field("url", "https://x.test/s")
    monkeypatch.setattr(svc_mod, "WebDriverWait", None)
    make_service(sess, create_driver=lambda *a, **k: FakeDriver(),
                 detect=lambda d: [{"q": 1, "type": "single"}]).detect_questions()
    assert ("OK", "探测完成，共 1 题（1 单选）") in logs_of(sess)


def test_verification_without_a_waiter_continues_the_probe(session):
    """说得出"有验证码"却没人等它过去 —— 与 Tk 宿主一样继续往下探，不卡住。"""
    sess, _ = session
    sess.set_field("url", "https://x.test/s")
    make_service(
        sess,
        create_driver=lambda *a, **k: FakeDriver(),
        detect=lambda d: [{"q": 1, "type": "single"}],
        verification_showing=lambda d: True,
        wait_verification=None,
    ).detect_questions()
    assert ("WARN", "检测到验证码，请在浏览器中手动完成...") in logs_of(sess)
    assert ("OK", "探测完成，共 1 题（1 单选）") in logs_of(sess)


def test_no_record_text_can_be_turned_off(session):
    sess, _ = session
    sess.set_field("no_record_text", False)
    assert sess.no_record_text is False
    sess.set_field("no_record_text", 1)
    assert sess.no_record_text is True
