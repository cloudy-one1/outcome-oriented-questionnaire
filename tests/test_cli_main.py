"""CLI **入口接线**测试（走 main()，不直接调 run_batch）。

为什么单独一个文件：tests/test_cli_batch.py 全部直接调用 run_batch()，
于是「argparse 解析出来了、但没往下传」这一整类 bug 在离线套件里不可见。
真实回归就是 v2.4 的 --resume：main() 算出 resume_run_id/resume_done/
resume_fail 并打印了「断点续传 Run #N」横幅，却一个都没传给 run_batch，
静默变成新批次从第 1 份重跑（= 在问卷站上重复提交）。

本文件的职责：把 argv → run_batch 关键字参数这条链路钉死。
"""

from __future__ import annotations

import os
import tempfile
from typing import Any
from unittest import mock

import pytest

from src import cli
from src.history import SubmissionHistory

URL = "https://example.test/vm/fake.aspx"


def _run_main(argv: list[str]) -> dict[str, Any]:
    """执行 cli.main(argv)，返回 run_batch 收到的关键字参数。

    main() 结尾无条件 sys.exit()，这里统一吞掉 SystemExit。
    """
    with mock.patch.object(cli, "run_batch") as rb:
        rb.return_value = (0, 0)
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
    assert exc.value.code == 0, f"run_batch 返回 (0,0) 时退出码应为 0：{exc.value.code}"
    assert rb.call_count == 1, f"run_batch 应恰好调用一次，实际 {rb.call_count}"
    return rb.call_args.kwargs


def _seed_interrupted_run(db_path: str, *, planned: int, done: int,
                          weight_config: dict | None = None) -> int:
    """造一个「中断且部分完成」的批次，供 --resume 查找。"""
    db = SubmissionHistory(db_path)
    try:
        run_id = db.start_run(
            survey_url=URL, total_submissions=planned,
            browser="edge", use_uc=False, weight_config=weight_config,
        )
        db.finish_run(
            run_id, success_count=done, fail_count=0,
            total_elapsed_seconds=1.0, status="interrupted",
        )
        return run_id
    finally:
        db.close()


@pytest.fixture()
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # SubmissionHistory 自己建文件
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ---------------------------------------------------------------------------
#  --resume：三条续传参数必须传到 run_batch（这是 v2.4 的真实回归）
# ---------------------------------------------------------------------------
def test_main_forwards_resume_kwargs(db_path: str) -> None:
    run_id = _seed_interrupted_run(db_path, planned=50, done=12)

    kwargs = _run_main(["-u", URL, "-H", db_path, "--resume"])

    assert kwargs["resume_run_id"] == run_id, (
        "resume_run_id 没传给 run_batch —— 续传会另起新批次并重复提交"
    )
    assert kwargs["resume_done"] == 12
    assert kwargs["resume_fail"] == 0


def test_main_resume_positional_total_is_planned(db_path: str) -> None:
    """续传必须沿用上次的计划份数；漏掉就会按默认 17 份重新规划。"""
    _seed_interrupted_run(db_path, planned=50, done=12)

    with mock.patch.object(cli, "run_batch") as rb:
        rb.return_value = (0, 0)
        with pytest.raises(SystemExit):
            cli.main(["-u", URL, "-H", db_path, "--resume"])

    args, kwargs = rb.call_args
    # run_batch(SURVEY_URL, TOTAL_SUBMISSIONS, ...) → 位置参数 [0]=url, [1]=total
    assert args[0] == URL
    assert args[1] == 50, (
        f"续传时应沿用上次计划份数 50，实际 {args[1]}"
    )


def test_main_resume_explicit_count_overrides_planned(db_path: str) -> None:
    """用户显式 -n 时以用户为准，而不是快照里的旧计划。"""
    _seed_interrupted_run(db_path, planned=50, done=12)

    with mock.patch.object(cli, "run_batch") as rb:
        rb.return_value = (0, 0)
        with pytest.raises(SystemExit):
            cli.main(["-u", URL, "-H", db_path, "-n", "5", "--resume"])

    args, kwargs = rb.call_args
    assert args[1] == 5
    assert kwargs["resume_run_id"] is not None, "-n 覆盖份数不应关掉续传本身"


def test_main_resume_restores_weight_snapshot(db_path: str) -> None:
    """续传要恢复批次权重快照（docs/cli.md「断点续传」承诺的行为）。"""
    snap = {1: {"type": "single", "weights": [0.7, 0.3]}}
    _seed_interrupted_run(db_path, planned=10, done=4, weight_config=snap)

    from src import config as cfg

    cfg.WEIGHT_CONFIG.clear()
    try:
        _run_main(["-u", URL, "-H", db_path, "--resume"])
        assert 1 in cfg.WEIGHT_CONFIG, "续传未从批次快照恢复权重配置"
        assert list(cfg.WEIGHT_CONFIG[1]["weights"]) == [0.7, 0.3]
    finally:
        cfg.WEIGHT_CONFIG.clear()


def test_main_without_resume_starts_fresh(db_path: str) -> None:
    """不加 --resume 时绝不能误续传。"""
    _seed_interrupted_run(db_path, planned=50, done=12)

    kwargs = _run_main(["-u", URL, "-H", db_path])

    assert kwargs["resume_run_id"] is None
    assert kwargs["resume_done"] == 0


def test_main_resume_no_match_falls_back_to_new_run(db_path: str) -> None:
    kwargs = _run_main(["-u", URL, "-H", db_path, "--resume"])
    assert kwargs["resume_run_id"] is None, "无可恢复批次时应按全新批次开始"


# ---------------------------------------------------------------------------
#  其它入口参数的接线（同一类 bug 的横向防线）
# ---------------------------------------------------------------------------
def test_main_forwards_privacy_and_mode_flags(db_path: str) -> None:
    kwargs = _run_main([
        "-u", URL, "-n", "3", "-H", db_path,
        "--no-record-text", "--target-success", "--max-attempts", "9",
    ])
    assert kwargs["no_record_text"] is True
    assert kwargs["target_success"] is True
    assert kwargs["max_attempts"] == 9
    assert kwargs["history_db"] is not None


def test_main_rescue_gaps_defaults_off_and_forwarded(db_path: str) -> None:
    """v3.1 补漏轮：不写就是 False（默认路径 = v3.0 行为），写了必须传到 run_batch。"""
    assert _run_main(["-u", URL])["rescue_gaps"] is False
    assert _run_main(["-u", URL, "--rescue-gaps"])["rescue_gaps"] is True


def test_main_manual_submit_defaults_off_and_forwarded(db_path: str) -> None:
    """v3.3 人工提交：不写就是 False（默认路径 = 自动提交），写了必须传到 run_batch。"""
    assert _run_main(["-u", URL])["manual_submit"] is False
    assert _run_main(["-u", URL, "--manual-submit"])["manual_submit"] is True


def test_main_manual_submit_rejects_headless(db_path: str) -> None:
    """无头窗口里没有可点的人 —— 两个开关同时给是配置错了，必须动手前退掉。

    取其一（比如"无头时自动降级成不等待"）会静默交出一份没人核对过的问卷，
    而那正是这个开关要避免的事。
    """
    with mock.patch.object(cli, "run_batch") as rb:
        with pytest.raises(SystemExit) as exc:
            cli.main(["-u", URL, "-H", db_path, "--manual-submit", "--headless"])
    assert exc.value.code == 2
    rb.assert_not_called()


def test_main_exit_code_nonzero_when_failures(db_path: str) -> None:
    """CLI 常被 cron 调用，失败必须反映到退出码。"""
    with mock.patch.object(cli, "run_batch") as rb:
        rb.return_value = (2, 3)
        with pytest.raises(SystemExit) as exc:
            cli.main(["-u", URL, "-H", db_path])
    assert exc.value.code == 1


def test_main_rejects_invalid_config_before_running(db_path: str) -> None:
    """v2.5：--config 校验不通过要硬失败，不能带着坏权重去真实提交。"""
    import json

    bad = {"1": {"type": "single", "weights": [0, 0]}}  # 全 0 权重
    fd, cfg_path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"schema_version": "2.0", "config": bad}, f)
        with mock.patch.object(cli, "run_batch") as rb:
            with pytest.raises(SystemExit) as exc:
                cli.main(["-u", URL, "-c", cfg_path])
            assert exc.value.code == 2
            assert rb.call_count == 0, "非法配置必须在任何提交之前被拦下"
    finally:
        if os.path.exists(cfg_path):
            os.unlink(cfg_path)
