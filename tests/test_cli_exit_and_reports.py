"""CLI 收尾路径的离线契约：队列解析、信度/漂移报告、`--save-config` / `--stats`、退出码。

`tests/test_cli_main.py` 钉的是「argv → `run_batch` 关键字参数」那一段，本文件钉它**之后**
的部分：队列怎么解析、跑完之后要不要报错、要不要落文件、要不要出报告、以什么码退出。
这一段此前一行都没执行过 —— 当年 README 缺口表里 `src/cli.py` 的备注写的是"剩余是输出分支"，
而 `--report-alpha` 的整段 α 报告与队列的六个出口是实打实没测过的逻辑。

统一用替身 `run_batch`：只关心入口收尾，不重复测真实提交流程。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import cli, reliability  # noqa: E402
from src.config_io import load_weight_config  # noqa: E402
from src.history import SubmissionHistory  # noqa: E402

URL = "https://example.test/vm/fake.aspx"
OTHER = "https://example.test/vm/other.aspx"

Call = tuple[tuple[Any, ...], dict[str, Any]]


def run_main(
    argv: list[str],
    replies: list[tuple[int, int]] | None = None,
) -> tuple[SystemExit, list[Call]]:
    """跑一次 `cli.main(argv)`，交回退出码与替身 `run_batch` 收到的实参序列。

    实参比 `replies` 长时重复最后一条 —— 队列跑出预期的份数之外更多次应当由断言发现，
    而不是以 `StopIteration` 的形式炸在替身里。
    """
    table = replies or [(0, 0)]
    calls: list[Call] = []

    def fake(*args: Any, **kw: Any) -> tuple[int, int]:
        calls.append((args, kw))
        return table[min(len(calls) - 1, len(table) - 1)]

    with mock.patch.object(cli, "run_batch", side_effect=fake):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
    return exc.value, calls


def urls_of(calls: list[Call]) -> list[Any]:
    return [args[0] for args, _kw in calls]


def counts_of(calls: list[Call]) -> list[Any]:
    return [args[1] for args, _kw in calls]


def queue_body(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def write_queue(tmp_path: str, body: str) -> str:
    path = os.path.join(str(tmp_path), "queue.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


class FakeRuns:
    """只长 `_report_reliability` 用到的那张嘴（读题由 monkeypatch 代劳）。"""

    def __init__(self, runs: list[dict[str, Any]] | None) -> None:
        self._runs = runs or []

    def query_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        assert limit == 1, "α 报告只需要最近一个批次"
        return self._runs


# ============================================================================
#  --url-file 顺序队列
# ============================================================================
def test_queue_skips_comments_and_blanks_and_takes_per_line_counts(tmp_path: str) -> None:
    """第 100 行写错不该毁掉整晚的队列，但注释与空行也不能被当成 URL。"""
    queue = write_queue(tmp_path, queue_body("# 说明行", "", f"{URL},3", f"  {OTHER}  "))
    exit_code, calls = run_main(["--url-file", queue], replies=[(0, 0), (0, 0)])
    assert exit_code.code == 0
    assert urls_of(calls) == [URL, OTHER]
    assert counts_of(calls) == [3, cli.DEFAULT_TOTAL_SUBMISSIONS]


def test_queue_accepts_a_bom_and_crlf(tmp_path: str) -> None:
    """队列文件常由 Excel / PowerShell 产出，首行带 BOM 会把 URL 的第一个字符吃掉。"""
    path = os.path.join(str(tmp_path), "bom.txt")
    with open(path, "wb") as fh:
        fh.write(("\ufeff" + f"{URL},2\r\n").encode("utf-8"))
    _exit, calls = run_main(["--url-file", path])
    assert [args for args, _kw in calls] == [(URL, 2)]


def test_an_illegal_count_skips_that_line_and_says_so(
    tmp_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`0` 份是不存在的目标：跳过这一行并说出来，而不是整晚的队列一红了之。"""
    queue = write_queue(tmp_path, queue_body(f"{URL},0", OTHER))
    exit_code, calls = run_main(["--url-file", queue])
    assert exit_code.code == 0
    assert urls_of(calls) == [OTHER]
    assert "跳过非法行" in capsys.readouterr().out


def test_missing_queue_file_exits_two(tmp_path: str) -> None:
    assert run_main(["--url-file", os.path.join(str(tmp_path), "nope.txt")])[0].code == 2


def test_a_queue_with_no_usable_url_exits_two(tmp_path: str) -> None:
    queue = write_queue(tmp_path, queue_body("# 全是注释", ""))
    assert run_main(["--url-file", queue])[0].code == 2


def test_queue_plus_resume_exits_two(tmp_path: str) -> None:
    """续传按"那一份问卷的上一个批次"找，队列里同时续传多份没有定义。"""
    queue = write_queue(tmp_path, queue_body(URL, OTHER))
    exit_code, calls = run_main(["--url-file", queue, "--resume"])
    assert exit_code.code == 2
    assert calls == []


def test_queue_plus_replay_file_exits_two(
    tmp_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回放表按"第 N 份 ↔ 第 N 行"对齐，队列里每份都从第一行开始 = 数据重一遍。"""
    monkeypatch.setattr(cli.reverse_fill, "begin_replay", lambda *_a: [])
    monkeypatch.setattr(cli.reverse_fill, "remaining_rows", lambda: 5)
    queue = write_queue(tmp_path, queue_body(URL, OTHER))
    exit_code, calls = run_main(["--url-file", queue, "--replay-file", "answers.csv"])
    assert exit_code.code == 2
    assert calls == []


# ============================================================================
#  --start-at 预约：等待期间被打断
# ============================================================================
def test_interrupted_while_waiting_never_launches_a_browser(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """一份都没提交却退 0，会让 cron 以为跑成了。"""
    with mock.patch.object(cli, "_sleep_until", side_effect=KeyboardInterrupt):
        exit_code, calls = run_main(["-u", URL, "--start-at", "2099-01-01T08:00"])
    assert exit_code.code == 1
    assert calls == []
    assert "一份都没提交" in capsys.readouterr().out


# ============================================================================
#  α 报告（整段此前未执行过）
# ============================================================================
def test_alpha_report_without_history_db_says_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._report_reliability(None, {})
    assert "没开历史库" in capsys.readouterr().out


def test_alpha_report_without_any_run_says_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._report_reliability(FakeRuns([]), {})
    assert "没有批次记录" in capsys.readouterr().out


def test_alpha_report_uses_the_declared_dimensions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dims = {"焦虑": [1, 2, 3]}
    seen: dict[str, Any] = {}

    def measure(db: Any, run_id: int, given: Any, reverse: Any) -> list[str]:
        seen.update(run_id=run_id, dims=given, reverse=reverse)
        return ["raw-report"]

    monkeypatch.setattr(reliability, "dimensions_from_config", lambda _cfg: dims)
    monkeypatch.setattr(reliability, "reverse_from_config", lambda _cfg: [2])
    monkeypatch.setattr(reliability, "measure_dimensions", measure)
    monkeypatch.setattr(reliability, "format_reports", lambda reps: [f"α={r}" for r in reps])
    cli._report_reliability(FakeRuns([{"id": "7"}]), {})
    # 库里 id 是字符串，进 measure_dimensions 前必须已经变回 int
    assert seen == {"run_id": 7, "dims": dims, "reverse": [2]}
    out = capsys.readouterr().out
    assert "维度取自权重配置里声明的 dimension" in out
    assert "α=raw-report" in out


def test_alpha_report_fallback_says_it_is_not_a_construct(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """配置没声明 dimension 时兜底分组 —— 那句话必须自己说明它不是某个构念的信度。"""
    monkeypatch.setattr(reliability, "dimensions_from_config", lambda _cfg: {})
    monkeypatch.setattr(reliability, "reverse_from_config", lambda _cfg: [])
    monkeypatch.setattr(reliability, "implicit_dimension", lambda db, run_id: {"全体": [1, 2]})
    monkeypatch.setattr(reliability, "measure_dimensions", lambda *a: [])
    monkeypatch.setattr(reliability, "format_reports", lambda _reps: ["兜底那一行"])
    cli._report_reliability(FakeRuns([{"id": 3}]), {})
    out = capsys.readouterr().out
    assert "不是某个构念的信度" in out
    assert "兜底那一行" in out


def test_alpha_report_stops_when_no_question_can_enter_alpha(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(reliability, "dimensions_from_config", lambda _cfg: {})
    monkeypatch.setattr(reliability, "reverse_from_config", lambda _cfg: [])
    monkeypatch.setattr(reliability, "implicit_dimension", lambda db, run_id: {})
    monkeypatch.setattr(reliability, "measure_dimensions",
                        lambda *a: pytest.fail("没有可测的题就不该去读答卷"))
    cli._report_reliability(FakeRuns([{"id": 9}]), {})
    assert "没有可参与 α 的题" in capsys.readouterr().out


def test_report_alpha_flag_is_wired_after_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--report-alpha` 若没接上，跑完什么也不说；接上则拿到本次那个 history_db。"""
    seen: list[tuple[Any, Any]] = []
    monkeypatch.setattr(cli, "_report_reliability", lambda db, cfg: seen.append((db, cfg)))
    exit_code, _calls = run_main(["-u", URL, "--report-alpha"], replies=[(2, 0)])
    assert exit_code.code == 0
    assert len(seen) == 1 and isinstance(seen[0][1], dict)


# ============================================================================
#  drift 报告
# ============================================================================
def test_drift_report_names_the_worst_share(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.distribution, "control_enabled", lambda: True)
    monkeypatch.setattr(cli.distribution, "drift_report", lambda: {
        1: {"max_share": 0.31, "delivered": 10},
        7: {"max_share": 0.62, "delivered": 10},
    })
    run_main(["-u", URL])
    out = capsys.readouterr().out
    assert "参与统计 2 题" in out
    assert "Q7" in out and "62%" in out


def test_no_drift_line_when_correction_is_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.distribution, "control_enabled", lambda: False)
    monkeypatch.setattr(cli.distribution, "drift_report",
                        lambda: pytest.fail("没开纠正就不该去要报告"))
    run_main(["-u", URL])
    assert "[drift]" not in capsys.readouterr().out


def test_an_empty_drift_report_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.distribution, "control_enabled", lambda: True)
    monkeypatch.setattr(cli.distribution, "drift_report", lambda: {})
    run_main(["-u", URL])
    assert "[drift]" not in capsys.readouterr().out


# ============================================================================
#  --save-config / --stats
# ============================================================================
def test_save_config_without_a_preset_writes_an_empty_template(
    tmp_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """出厂权重的刻意选择就是"空"（v2.8 修过出厂权重污染），所以没 `-c` 时导出空模板
    是对的 —— 这条钉住的是"导出的不是某份藏在代码里的偏置"。"""
    out_file = os.path.join(str(tmp_path), "tpl.json")
    assert run_main(["-u", URL, "--save-config", out_file])[0].code == 0
    cfg, meta = load_weight_config(out_file)
    assert cfg == {}
    assert meta["name"] == "自动导出模板"              # 没 -c 时的默认名
    assert "已保存配置模板" in capsys.readouterr().out


def test_save_config_after_a_preset_round_trips_that_preset(
    tmp_path: str,
) -> None:
    """导出的必须是**当前生效**的那份，并且预设名要跟着走 —— 用户拿它当改动起点。"""
    from src import config as config_mod

    preset = os.path.join(str(tmp_path), "preset.json")
    with open(preset, "w", encoding="utf-8") as fh:
        json.dump({"config": {"1": {"type": "single", "weights": [1, 0]}},
                   "meta": {"name": "我的预设"}}, fh, ensure_ascii=False)
    out_file = os.path.join(str(tmp_path), "tpl.json")
    with mock.patch.object(config_mod, "WEIGHT_CONFIG", {}, create=True):
        assert run_main(["-u", URL, "-c", preset, "--save-config", out_file])[0].code == 0
    cfg, meta = load_weight_config(out_file)
    assert cfg[1]["weights"] == [1, 0]
    assert meta["name"] == "我的预设"


def test_save_config_failure_degrades_without_killing_the_run(
    tmp_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """写不进去（这里目标是个已存在的目录）是收尾的坏消息，不是本批运行的失败。"""
    target = os.path.join(str(tmp_path), "a-dir")
    os.makedirs(target)
    assert run_main(["-u", URL, "--save-config", target])[0].code == 0
    assert "保存失败" in capsys.readouterr().out


def test_stats_prints_the_library_totals(
    tmp_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    db_file = os.path.join(str(tmp_path), "h.db")
    db = SubmissionHistory(db_file)
    run_id = db.start_run(survey_url=URL, total_submissions=4, browser="edge", use_uc=False)
    db.finish_run(run_id, success_count=3, fail_count=1, total_elapsed_seconds=1.0)
    db.close()
    capsys.readouterr()
    assert run_main(["-u", URL, "-H", db_file, "--stats"])[0].code == 0
    out = capsys.readouterr().out
    assert "累计运行: 1 次" in out
    assert "累计提交: 4 份" in out
    assert "累计成功: 3 / 失败: 1" in out
    assert "累计成功率: 75.0%" in out


def test_stats_without_a_history_db_explains_itself(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_main(["-u", URL, "--stats"])[0].code == 0
    assert "--stats 需要配合" in capsys.readouterr().out


def test_a_broken_stats_query_degrades_to_one_line(
    tmp_path: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """读不动库（这里当作被别的进程锁着）不该把一次成功的运行带成失败退出。"""
    db_file = os.path.join(str(tmp_path), "h2.db")
    SubmissionHistory(db_file).close()
    monkeypatch.setattr(SubmissionHistory, "stats_summary",
                        lambda self: (_ for _ in ()).throw(OSError("库被别的进程锁着")))
    assert run_main(["-u", URL, "-H", db_file, "--stats"])[0].code == 0
    out = capsys.readouterr().out
    assert "history.stats_summary" in out
    assert "累计运行" not in out


def test_a_failing_db_close_still_exits_on_the_run_result(
    tmp_path: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收尾关不掉只该少一句提示，不该盖掉"本批有失败"这个结论。"""
    db_file = os.path.join(str(tmp_path), "h3.db")
    real_close = SubmissionHistory.close

    def flaky_close(self: SubmissionHistory) -> None:
        real_close(self)                    # 资源照常放掉，只把"关的时候报错了"演出来
        raise OSError("关不掉")

    monkeypatch.setattr(SubmissionHistory, "close", flaky_close)
    assert run_main(["-u", URL, "-H", db_file], replies=[(0, 2)])[0].code == 1


# ============================================================================
#  多问卷时的汇总与退出码
# ============================================================================
def test_multi_target_run_prints_a_per_survey_header_and_one_total(
    tmp_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = write_queue(tmp_path, queue_body(f"{URL},1", f"{OTHER},2"))
    run_main(["--url-file", queue], replies=[(1, 0), (2, 0)])
    out = capsys.readouterr().out
    assert "[1/2]" in out and "[2/2]" in out
    assert "成功 3, 失败 0" in out and "共 2 份问卷" in out


def test_exit_code_currently_only_reflects_the_last_survey(tmp_path: str) -> None:
    """现状记录，不是期望：收尾用的是循环变量 `fail`（`src/cli.py:1268`）而不是 `total_fail`。

    前面几份全失败、最后一份成功 → 退 0。单问卷时两者等价，所以这个洞只在 `--url-file`
    队列上存在。按"报出来、不顺手改生产行为"的规矩先把现状钉在这里；要修就是那一处
    改成 `total_fail == 0`，并把本条断言改成 `code == 1`。
    """
    queue = write_queue(tmp_path, queue_body(f"{URL},1", f"{OTHER},1"))
    exit_code, calls = run_main(["--url-file", queue], replies=[(0, 4), (4, 0)])
    assert len(calls) == 2
    assert exit_code.code == 0               # ← 现状；修完应为 1
