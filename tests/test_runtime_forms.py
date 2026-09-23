"""v3.0 运行形态的契约：无头 / profile / 顺序队列 / 整批时限。

这一批开关的共性是"**默认关，开了才生效**"，所以最容易被测漏 ——
漏测的后果不是跑不起来，而是"以为开了"：例如 --profile-dir 拼错参数名，
浏览器照样起来，只是每次都是全新 profile，没人会察觉。
因此这里既测 CLI 参数怎么解析，也测它有没有真的传到 create_driver。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import cli, verification  # noqa: E402
from src.browser import driver_factory  # noqa: E402

URL = "https://www.wjx.cn/vm/xxxx.aspx"


# ===========================================================================
#  parse_args
# ===========================================================================
def test_new_flags_default_to_off() -> None:
    """四个开关默认必须全关：默认路径就是 v2.8 的行为。"""
    args = cli.parse_args(["-u", "https://www.wjx.cn/vm/x.aspx"])
    assert args.headless is False
    assert args.profile_dir is None
    assert args.max_total_time is None
    assert args.url_file is None


def test_new_flags_parse_values() -> None:
    args = cli.parse_args([
        "-u", "https://www.wjx.cn/vm/x.aspx",
        "--headless", "--profile-dir", "/tmp/prof",
        "--max-total-time", "600", "--url-file", "urls.txt",
    ])
    assert args.headless is True
    assert args.profile_dir == "/tmp/prof"
    assert args.max_total_time == 600
    assert args.url_file == "urls.txt"


def test_max_total_time_rejects_non_positive() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["-u", "https://x", "--max-total-time", "0"])


# ===========================================================================
#  v3.1 --rescue-gaps
# ===========================================================================
def test_rescue_gaps_defaults_off() -> None:
    """默认关：不写这个开关时，完整度自检判出的缺口维持"判失败、不点提交"。"""
    assert cli.parse_args(["-u", "https://www.wjx.cn/vm/x.aspx"]).rescue_gaps is False


def test_rescue_gaps_parses_on() -> None:
    args = cli.parse_args(["-u", "https://www.wjx.cn/vm/x.aspx", "--rescue-gaps"])
    assert args.rescue_gaps is True


# ===========================================================================
#  run_batch 的透传（"算了却没传"是这个仓库反复付过账的缺陷形态）
# ===========================================================================
class _StubDriver:
    class _SwitchTo:
        def default_content(self) -> None:
            pass

        def frame(self, _t: Any) -> None:
            pass

    switch_to = _SwitchTo()
    current_url = "https://example.com"

    def execute_script(self, _script: str, *_a: Any, **_k: Any) -> Any:
        return True

    def quit(self) -> None:
        pass

    def delete_all_cookies(self) -> None:
        pass


def test_run_batch_forwards_headless_and_profile(monkeypatch) -> None:
    captured: list[dict] = []

    def _fake_create(browser: str = "edge", **kw: Any) -> _StubDriver:
        captured.append(kw)
        return _StubDriver()

    monkeypatch.setattr("src.browser.create_driver", _fake_create)
    monkeypatch.setattr("src.pipeline.run_one_submission",
                        lambda *a, **k: "failed")
    monkeypatch.setattr("src.utils.human_pause", lambda *a, **k: 0.0)

    cli.run_batch("https://example.com/s", 1, headless=True,
                  user_data_dir="/tmp/prof")

    assert captured, "create_driver 没被调用"
    assert captured[0]["headless"] is True
    assert captured[0]["user_data_dir"] == "/tmp/prof"


def test_max_total_time_stops_the_batch_as_interrupted(monkeypatch) -> None:
    """到时限 → 优雅停止（interrupted，可 --resume），而不是崩溃（failed）。"""
    import time as _t

    rounds = {"n": 0}

    def _fake_run(*_a: Any, **_k: Any) -> str:
        rounds["n"] += 1
        _t.sleep(0.02)
        return "failed"

    monkeypatch.setattr("src.browser.create_driver",
                        lambda *a, **k: _StubDriver())
    monkeypatch.setattr("src.pipeline.run_one_submission", _fake_run)
    monkeypatch.setattr("src.utils.human_pause", lambda *a, **k: 0.0)

    from src.history import SubmissionHistory

    with SubmissionHistory(":memory:") as db:
        success, fail = cli.run_batch(
            "https://example.com/s", 50, history_db=db, max_total_seconds=0.05,
        )
        row = db._query_one("SELECT * FROM runs")

    assert rounds["n"] < 50, "时限根本没生效"
    assert (success, fail) == (0, rounds["n"])
    assert row["status"] == "interrupted", (
        f"到时限应可续传（interrupted），实际 {row['status']}"
    )


# ===========================================================================
#  无头 + 验证码
# ===========================================================================
def test_headless_driver_bails_out_of_manual_verification() -> None:
    """无头里等人工 = 等一个不存在的人：必须立刻返回 False，且不挂人工介入锁。"""
    lock_called = {"acquired": False}

    class HoldLock:
        is_holding = False

        def acquire(self) -> None:
            lock_called["acquired"] = True

        def release(self) -> None:
            pass

    class Driver:
        wjx_headless = True

        def execute_script(self, *_a: Any, **_k: Any) -> Any:
            raise AssertionError("无头路径根本不该去探测验证码")

    assert verification.wait_for_manual_verification(
        Driver(), timeout_seconds=30, hold_lock=HoldLock(),
    ) is False
    assert lock_called["acquired"] is False


def test_driver_is_headless_defaults_to_false_for_unknown_objects() -> None:
    assert driver_factory.driver_is_headless(object()) is False
    d = _StubDriver()
    d.wjx_headless = True
    assert driver_factory.driver_is_headless(d) is True


def test_binary_location_follows_the_env_var(monkeypatch) -> None:
    """容器里 chromium 的路径只能靠环境变量指（Dockerfile 就是这么设的）。"""
    class Opts:
        binary_location: str | None = None

    monkeypatch.setenv("WJX_CHROME_BINARY", "/usr/bin/chromium")
    driver_factory._apply_binary_location(Opts())
    # 三个变量按优先级取第一个非空的；这里只断"确实读到了某个"
    o = Opts()
    driver_factory._apply_binary_location(o)
    assert o.binary_location == "/usr/bin/chromium"

    monkeypatch.delenv("WJX_CHROME_BINARY")
    monkeypatch.delenv("WJX_BROWSER_BINARY", raising=False)
    monkeypatch.delenv("WJX_EDGE_BINARY", raising=False)
    o2 = Opts()
    driver_factory._apply_binary_location(o2)
    assert o2.binary_location is None, "没设环境变量时绝不能瞎指路径"


# ===========================================================================
#  v3.1 --start-at：预约开跑
# ===========================================================================
NOW = datetime(2030, 5, 10, 12, 0, 0)


@pytest.mark.parametrize("text,expected", [
    ("2030-06-01 08:00", datetime(2030, 6, 1, 8, 0, 0)),
    ("2030-06-01 08:00:30", datetime(2030, 6, 1, 8, 0, 30)),
    ("2030-06-01T08:00", datetime(2030, 6, 1, 8, 0, 0)),
    ("2030-06-01", datetime(2030, 6, 1, 0, 0, 0)),
    # 只给时刻：还没过就是今天
    ("18:30", datetime(2030, 5, 10, 18, 30, 0)),
    ("18:30:45", datetime(2030, 5, 10, 18, 30, 45)),
])
def test_start_at_accepts_the_documented_shapes(text: str, expected: datetime) -> None:
    assert cli._start_at(text, now=lambda: NOW) == expected


def test_bare_time_already_past_rolls_to_tomorrow() -> None:
    """``08:00`` 说的是"这个点"，今天过了就是明天 —— 而不是报错或立刻开跑。"""
    assert cli._start_at("08:00", now=lambda: NOW) == datetime(2030, 5, 11, 8, 0, 0)


@pytest.mark.parametrize("text", [
    "2030-05-10 11:00",          # 带日期、已经过去
    "2020-01-01 08:00",          # 远古
])
def test_past_deadline_with_a_date_is_refused(text: str) -> None:
    """预约一个过去的时间，症状是"立刻开跑"而用户以为在等 —— 方向危险，硬拒。"""
    with pytest.raises(argparse.ArgumentTypeError, match="已经过去"):
        cli._start_at(text, now=lambda: NOW)


@pytest.mark.parametrize("text", ["明天早上", "08:00:00:00", "2030-13-01 08:00", "   "])
def test_unparsable_start_at_is_refused(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli._start_at(text, now=lambda: NOW)


def test_start_at_is_an_argparse_type_so_bad_input_exits_2() -> None:
    """坏值必须是退出码 2（与 --config 校验不通过同一类"参数就没收"的口径）。"""
    with pytest.raises(SystemExit) as e:
        cli.parse_args(["-u", "https://x", "--start-at", "昨天"])
    assert e.value.code == 2
    ok = cli.parse_args(["-u", "https://x", "--start-at", "2030-06-01 08:00"])
    assert ok.start_at == datetime(2030, 6, 1, 8, 0)
    assert cli.parse_args(["-u", "https://x"]).start_at is None


# ---- _sleep_until：分片、进度、以及"到点之前什么都不做" ----
class FakeClock:
    """假时钟：sleep 会把 now 往前推，于是等待循环可以在几毫秒内跑完整个预约。"""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.slept: list[float] = []
        self.logs: list[str] = []

    def sleep(self, seconds: float) -> None:
        assert seconds <= cli._START_AT_SLICE + 1e-9, "等待必须切片，否则 Ctrl+C 打不断"
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)

    def lines(self, msg: str) -> None:
        self.logs.append(msg)


def test_sleep_until_slices_and_stops_exactly_at_the_deadline() -> None:
    clock = FakeClock(NOW)
    target = NOW + timedelta(seconds=5)

    cli._sleep_until(target, now=lambda: clock.now, sleep=clock.sleep, log=clock.lines)

    assert sum(clock.slept) == pytest.approx(5.0)
    assert len(clock.slept) == 5
    assert clock.now >= target
    assert "不早于" in clock.logs[0] and "不会启动浏览器" in clock.logs[0]
    assert "到点" in clock.logs[-1]


def test_sleep_until_does_nothing_when_the_deadline_already_passed() -> None:
    """校验与开跑之间隔了几毫秒也不该报错或瞎等一轮。"""
    clock = FakeClock(NOW)
    cli._sleep_until(NOW - timedelta(seconds=1), now=lambda: clock.now,
                     sleep=clock.sleep, log=clock.lines)
    assert clock.slept == [] and clock.logs == []


def test_progress_line_cadence_is_not_every_slice() -> None:
    clock = FakeClock(NOW)
    cli._sleep_until(NOW + timedelta(seconds=150), now=lambda: clock.now,
                     sleep=clock.sleep, log=clock.lines)
    assert len(clock.slept) == 150
    progress = [m for m in clock.logs if "还有" in m]
    assert len(progress) == 2, f"150s 里该打两条进度：{clock.logs}"


def test_a_stop_signal_during_the_wait_is_not_eaten() -> None:
    """切片的全部意义：等待期间 Ctrl+C 立刻能生效，而不是等完这几小时。"""
    def _sigint(_s: float) -> None:
        raise KeyboardInterrupt

    clock = FakeClock(NOW)
    with pytest.raises(KeyboardInterrupt):
        cli._sleep_until(NOW + timedelta(hours=3), now=lambda: clock.now,
                         sleep=_sigint, log=lambda _m: None)


# ---- main() 的接线：等待必须在任何提交动作之前 ----
def _ordered_main(monkeypatch, argv: list[str], *, refuse_waiting: bool = False) -> list[str]:
    events: list[str] = []

    def _wait(target: datetime, **_kw: Any) -> None:
        if refuse_waiting:
            raise AssertionError("没给 --start-at 就不该进等待")
        events.append(f"wait:{target:%H:%M}")

    def _batch(*_a: Any, **_k: Any) -> tuple[int, int]:
        events.append("batch")
        return 1, 0

    monkeypatch.setattr(cli, "_sleep_until", _wait)
    monkeypatch.setattr(cli, "run_batch", _batch)
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 0, f"main 应以退出码 0 收场，实际 {e.value.code}"
    return events


def test_main_waits_before_it_starts_the_batch(monkeypatch) -> None:
    """--max-total-time 从 T 起算的**实现方式**就是这句：等待在 run_batch 之前。

    浏览器与第一次 driver.get 都在 run_batch 里，所以这条顺序同时保证"不到点不碰页面"。
    """
    events = _ordered_main(monkeypatch, ["-u", URL, "--start-at", "2030-06-01 08:00"])
    assert events == ["wait:08:00", "batch"]


def test_main_waits_once_for_a_whole_queue(monkeypatch, tmp_path) -> None:
    """队列只预约一次开跑，不是每份问卷都等到同一个点（那是自欺欺人的时钟）。"""
    qfile = _write_queue(tmp_path, [
        "https://www.wjx.cn/vm/a.aspx", "https://www.wjx.cn/vm/b.aspx",
    ])
    events = _ordered_main(monkeypatch, ["--url-file", qfile, "--start-at", "08:00"])
    assert events == ["wait:08:00", "batch", "batch"]


def test_main_without_start_at_does_not_wait(monkeypatch) -> None:
    """默认路径不碰这个开关 —— 与 v3.0 行为逐位一致。"""
    assert _ordered_main(monkeypatch, ["-u", URL], refuse_waiting=True) == ["batch"]


def test_ctrl_c_during_the_wait_runs_no_batch(monkeypatch) -> None:
    """等待中被中断 = 一份都没提交：不启动浏览器、不进批次、非零退出码。"""
    def _sigint(*_a: Any, **_k: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_sleep_until", _sigint)
    monkeypatch.setattr(cli, "run_batch", lambda *a, **k: pytest.fail("不该开跑"))
    with pytest.raises(SystemExit) as e:
        cli.main(["-u", URL, "--start-at", "2030-06-01 08:00"])
    assert e.value.code != 0


def test_past_start_at_is_refused_before_anything_runs(monkeypatch) -> None:
    monkeypatch.setattr(cli, "run_batch", lambda *a, **k: pytest.fail("不该开跑"))
    with pytest.raises(SystemExit) as e:
        cli.main(["-u", URL, "--start-at", "2000-01-01 08:00"])
    assert e.value.code == 2


# ===========================================================================
#  顺序队列
# ===========================================================================
def _main_ok(argv: list[str]) -> None:
    """main() 结尾按失败数 sys.exit —— 队列用例只关心跑了哪些 URL，退出码须为 0。"""
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 0, f"main 以退出码 {e.value.code} 结束"


def _write_queue(tmp_path, lines: list[str]) -> str:
    p = tmp_path / "urls.txt"
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)


def test_url_file_runs_each_survey_in_order(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, int]] = []

    def _fake_batch(url: str, count: int, **_kw: Any) -> tuple[int, int]:
        calls.append((url, int(count)))
        return 1, 0

    monkeypatch.setattr(cli, "run_batch", _fake_batch)
    qfile = _write_queue(tmp_path, [
        "# 注释行要跳过",
        "",
        "https://www.wjx.cn/vm/a.aspx",
        "https://www.wjx.cn/vm/b.aspx,3",
    ])
    _main_ok(["--url-file", qfile, "-n", "2"])

    assert calls == [
        ("https://www.wjx.cn/vm/a.aspx", 2),   # 缺省份数走 -n
        ("https://www.wjx.cn/vm/b.aspx", 3),   # 行内份数优先
    ]


def test_url_and_queue_together_put_the_single_url_first(monkeypatch, tmp_path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        cli, "run_batch",
        lambda url, count, **_kw: calls.append(url) or (1, 0),
    )
    qfile = _write_queue(tmp_path, ["https://www.wjx.cn/vm/b.aspx"])
    _main_ok(["-u", "https://www.wjx.cn/vm/a.aspx", "--url-file", qfile])
    assert calls == ["https://www.wjx.cn/vm/a.aspx", "https://www.wjx.cn/vm/b.aspx"]


def test_resume_with_a_queue_is_refused(monkeypatch, tmp_path) -> None:
    """续传是"同一份问卷的下一份"语义，队列上没有"上一份"可言 → 硬失败。"""
    monkeypatch.setattr(
        cli, "run_batch", lambda *a, **k: pytest.fail("不该开跑"),
    )
    qfile = _write_queue(tmp_path, [
        "https://www.wjx.cn/vm/a.aspx", "https://www.wjx.cn/vm/b.aspx",
    ])
    with pytest.raises(SystemExit) as e:
        cli.main(["--url-file", qfile, "-H", str(tmp_path / "h.db"), "--resume"])
    assert e.value.code == 2


def test_empty_queue_file_is_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        cli, "run_batch", lambda *a, **k: pytest.fail("不该开跑"),
    )
    qfile = _write_queue(tmp_path, ["# 只有注释", "   "])
    with pytest.raises(SystemExit) as e:
        cli.main(["--url-file", qfile])
    assert e.value.code == 2


def test_missing_url_and_missing_queue_both_exit_2(monkeypatch) -> None:
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 2
