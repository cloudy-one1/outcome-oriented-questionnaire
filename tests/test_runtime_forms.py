"""v3.0 运行形态的契约：无头 / profile / 顺序队列 / 整批时限。

这一批开关的共性是"**默认关，开了才生效**"，所以最容易被测漏 ——
漏测的后果不是跑不起来，而是"以为开了"：例如 --profile-dir 拼错参数名，
浏览器照样起来，只是每次都是全新 profile，没人会察觉。
因此这里既测 CLI 参数怎么解析，也测它有没有真的传到 create_driver。
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import cli, verification  # noqa: E402
from src.browser import driver_factory  # noqa: E402


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
