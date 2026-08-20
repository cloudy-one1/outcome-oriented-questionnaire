"""端到端集成冒烟（v2.0 全题型链路）。

使用本地 mock HTML（``tests/fixtures/mock_wjx.html``），在 headless 浏览器
里跑完 5 个核心层：
    detection → answering_v2 → interaction → history → （可选）verification

设计要点：
    - 不依赖网络、不依赖真实问卷账号，可在 CI 里稳定复现
    - 如果本机没有可用的 Edge/Chrome 驱动，整个测试模块会自动 skip
    - 对所有 10 道题（6 类题型）都做 DOM 最终状态的断言
    - 对 SubmissionHistory 写入做断言，保证 DB 落盘
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# ------------------------------ 环境探测 ------------------------------
_DRIVER = None
_DRIVER_ERR: str | None = None

import importlib.util as _iu

if _iu.find_spec("selenium") is None:
    _DRIVER_ERR = "selenium 未安装"
else:
    try:
        from selenium import webdriver  # noqa: F401
    except Exception as _e:  # pragma: no cover - 仅环境失败时走
        _DRIVER_ERR = f"selenium import 失败: {_e}"
        webdriver = None  # type: ignore[assignment]


pytestmark = pytest.mark.skipif(
    _DRIVER_ERR is not None,
    reason=f"E2E 需要 Selenium: {_DRIVER_ERR}",
)


# ------------------------------ Fixtures ------------------------------

PROJ = Path(__file__).resolve().parent.parent
FIXTURE_HTML = PROJ / "tests" / "fixtures" / "mock_wjx.html"

sys.path.insert(0, str(PROJ))


@pytest.fixture(scope="module")
def driver():
    """启动一次 headless 浏览器（Edge → Chrome 回退）。"""
    opts_list = []
    if webdriver is None:  # pragma: no cover - import 时已处理
        pytest.skip("webdriver import 失败")

    # --- 尝试 Edge ---
    try:
        from selenium.webdriver.edge.options import Options as EdgeOpts
        opts = EdgeOpts()
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--window-size=1280,900")
        driver = webdriver.Edge(options=opts)
        opts_list.append("edge")
    except Exception as _e1:
        driver = None
        opts_list.append(f"edge-err:{_e1!r}")

    # --- 回退 Chrome ---
    if driver is None:
        try:
            from selenium.webdriver.chrome.options import Options as ChromeOpts
            opts = ChromeOpts()
            opts.add_argument("--headless=new")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--window-size=1280,900")
            driver = webdriver.Chrome(options=opts)
            opts_list.append("chrome")
        except Exception as _e2:
            opts_list.append(f"chrome-err:{_e2!r}")

    if driver is None:
        pytest.skip(f"无可用 webdriver（尝试: {', '.join(opts_list)}）")

    driver.set_script_timeout(10)
    driver.set_page_load_timeout(30)

    # 加载本地 mock HTML
    url = "file:///" + FIXTURE_HTML.as_posix()
    driver.get(url)

    yield driver

    try:
        driver.quit()
    except Exception:
        pass


@pytest.fixture()
def history_db(tmp_path):
    """临时 SQLite history DB。"""
    from src.history import SubmissionHistory
    db_path = tmp_path / "history.db"
    db = SubmissionHistory(str(db_path))
    yield db
    db.close()


# ------------------------------ 测试主体 ------------------------------

def test_detection_discovers_all_10_questions(driver):
    """detection 必须识别出 mock HTML 的全部 10 道题，类型/参数正确。"""
    from src.detection import detect_questions

    qs = detect_questions(driver)
    # 按题号升序，q1..q10 必须都在
    qnums = [q["q"] for q in qs]
    assert qnums == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], (
        f"题号集不匹配: {qnums}"
    )

    by_q = {q["q"]: q for q in qs}

    # 1. single (单选) - 4 选项
    assert by_q[1]["type"] == "single"
    assert by_q[1]["choices"] == [1, 2, 3, 4]

    # 2. multi (多选) - 5 选项
    assert by_q[2]["type"] == "multi"
    assert by_q[2]["choices"] == [1, 2, 3, 4, 5]

    # 3. dropdown (下拉) - 8 个非空选项
    assert by_q[3]["type"] == "dropdown"
    assert by_q[3]["choices"] == [1, 2, 3, 4, 5, 6, 7, 8]

    # 4. scale (量表) - 5 级
    assert by_q[4]["type"] == "scale"
    assert by_q[4]["scale"] == 5

    # 5. text (姓名)
    assert by_q[5]["type"] == "text"
    assert by_q[5]["field"] == "name"

    # 6. text (手机)
    assert by_q[6]["type"] == "text"
    assert by_q[6]["field"] == "phone"

    # 7. text (邮箱)
    assert by_q[7]["type"] == "text"
    assert by_q[7]["field"] == "email"

    # 8. textarea (地址)
    assert by_q[8]["type"] == "text"
    assert by_q[8]["field"] == "address"

    # 9. textarea (无字段 - 普通留言)
    assert by_q[9]["type"] == "text"
    assert by_q[9]["field"] is None

    # 10. matrix_single (4 行 × 5 列)
    assert by_q[10]["type"] == "matrix_single"
    assert by_q[10]["rows"] == [1, 2, 3, 4]
    assert by_q[10]["cols"] == [1, 2, 3, 4, 5]


def test_full_pipeline_fill_and_history(driver, history_db):
    """端到端：检测 → 生成答案 → 填写 DOM → 落 history → 断言 DOM/DB。"""
    from src.detection import detect_questions
    from src.answering_v2 import generate_answer
    from src.interaction import (
        js_click_question_options,
        js_fill_text,
        js_set_scale,
        js_select_dropdown,
        js_fill_matrix_single,
    )

    qs = detect_questions(driver)
    by_q = {q["q"]: q for q in qs}

    answers_record: dict[int, dict] = {}

    run_id = history_db.start_run(
        survey_url=driver.current_url,
        total_submissions=1,
        browser="edge-mock",
        use_uc=False,
    )
    assert run_id > 0

    submission_index = 1

    # ============ Q1: single ============
    a1 = generate_answer(by_q[1])
    assert a1["type"] == "single"
    v1 = a1["selected"][0]
    assert v1 in by_q[1]["choices"]
    assert js_click_question_options(driver, 1, "single", [int(v1)]) is True
    history_db.record_answer(run_id, submission_index, 1, "single", options_selected=[int(v1)])
    answers_record[1] = a1

    # ============ Q2: multi ============
    a2 = generate_answer(by_q[2])
    assert a2["type"] == "multi"
    vs2 = [int(x) for x in a2["selected"]]
    assert 1 <= len(vs2) <= len(by_q[2]["choices"])
    assert js_click_question_options(driver, 2, "multi", vs2) is True
    history_db.record_answer(run_id, submission_index, 2, "multi", options_selected=vs2)
    answers_record[2] = a2

    # ============ Q3: dropdown ============
    a3 = generate_answer(by_q[3])
    assert a3["type"] == "dropdown"
    v3 = a3["selected"][0]
    assert int(v3) in by_q[3]["choices"]
    assert js_select_dropdown(driver, 3, int(v3)) is True
    history_db.record_answer(run_id, submission_index, 3, "dropdown", options_selected=[int(v3)])
    answers_record[3] = a3

    # ============ Q4: scale ============
    a4 = generate_answer(by_q[4])
    assert a4["type"] == "scale"
    v4 = int(a4["value"])
    assert by_q[4].get("scale_min", 1) <= v4 <= by_q[4]["scale"]
    assert js_set_scale(driver, 4, v4, scale_max=by_q[4]["scale"]) is True
    history_db.record_answer(run_id, submission_index, 4, "scale", options_selected=[v4])
    answers_record[4] = a4

    # ============ Q5~9: text / textarea ============
    for qn in (5, 6, 7, 8, 9):
        ax = generate_answer(by_q[qn])
        assert ax["type"] == "text"
        tx = ax["text"]
        assert isinstance(tx, str) and len(tx) > 0
        assert js_fill_text(driver, qn, tx) is True
        history_db.record_answer(
            run_id, submission_index, qn, "text",
            options_selected=None, text_answer=tx,
        )
        answers_record[qn] = ax

    # ============ Q10: matrix_single ============
    a10 = generate_answer(by_q[10])
    assert a10["type"] == "matrix_single"
    rows_map = a10["rows"]
    assert isinstance(rows_map, dict)
    # rows_map key 是 row 号；value 是 col 值
    assert set(rows_map.keys()) == set(by_q[10]["rows"])
    for rv in rows_map.values():
        assert int(rv) in by_q[10]["cols"]
    fill_arg = {int(k): int(v) for k, v in rows_map.items()}
    assert js_fill_matrix_single(driver, 10, fill_arg) is True
    # 与 pipeline 一致：收集所有列值到扁平 list 存入 options_selected
    flat_cols = [int(v) for v in rows_map.values()]
    history_db.record_answer(
        run_id, submission_index, 10, "matrix",
        options_selected=flat_cols,
    )
    answers_record[10] = a10

    # ---------- finish run ----------
    history_db.finish_run(
        run_id, success_count=1, fail_count=0,
        total_elapsed_seconds=1.0, status="finished",
    )

    # =================================================================
    # DOM 最终状态断言（确认填写真的写回 DOM 了）
    # =================================================================
    def js(script):
        return driver.execute_script(script)

    # Q1 single：应该有且只有 1 个 radio 被选中
    q1_checked = js("return document.querySelectorAll('input[name=q1]:checked').length;")
    assert q1_checked == 1, f"Q1 single: expected 1 checked, got {q1_checked}"
    q1_val = js("return document.querySelector('input[name=q1]:checked').value;")
    assert int(q1_val) == int(answers_record[1]["selected"][0])

    # Q2 multi：被选中的 checkbox 数量应该等于 multi 答案数
    q2_checked = js("return document.querySelectorAll('input[name=q2]:checked').length;")
    assert q2_checked == len(answers_record[2]["selected"])

    # Q3 dropdown：selectedIndex 必须匹配（第一个 option 是空项，index 0）
    q3_idx = js("return document.getElementById('selectq3').selectedIndex;")
    assert q3_idx == int(answers_record[3]["selected"][0])

    # Q4 scale：对应值的隐藏 radio 被选中
    q4_val = js("""
        var r = document.querySelector('input[name=q4]:checked');
        return r ? r.value : null;
    """)
    assert q4_val is not None, "Q4 scale: 没有 radio 被选中"
    assert int(q4_val) == int(answers_record[4]["value"])

    # Q5-Q9 文本：DOM 的 value 必须匹配
    for qn in (5, 6, 7, 8, 9):
        dom_val = js(f"return document.getElementById('q{qn}').value;")
        expect = answers_record[qn]["text"]
        assert dom_val == expect, (
            f"Q{qn} text: DOM={dom_val!r} != 答案={expect!r}"
        )

    # Q10 matrix：4 行各有 1 个 radio 被选中，且值对得上 rows_map
    for row in by_q[10]["rows"]:
        n = js(f"return document.querySelectorAll('input[name=q10_{row}]:checked').length;")
        assert n == 1, f"Q10 row {row}: 期望 1 个 checked，实际 {n}"
        v = js(f"return document.querySelector('input[name=q10_{row}]:checked').value;")
        assert int(v) == int(answers_record[10]["rows"][row])

    # =================================================================
    # History DB 断言
    # =================================================================
    runs = history_db.query_runs(limit=5)
    assert len(runs) == 1
    r0 = runs[0]
    assert r0["id"] == run_id
    assert r0["status"] == "finished"
    assert r0["total_submissions"] == 1
    assert r0["success_count"] == 1
    assert r0["fail_count"] == 0
    assert r0["browser"] == "edge-mock"
    assert r0["use_uc"] == 0

    ans = history_db.query_answers(run_id=run_id)
    # 10 道题 → 10 条 answer 记录
    assert len(ans) == 10, f"期望 10 条 answer，实际 {len(ans)}"
    ans_by_q = {a["question_number"]: a for a in ans}

    # Q1 single
    a1_row = ans_by_q[1]
    assert a1_row["question_type"] == "single"
    assert json.loads(a1_row["options_selected"]) == [int(answers_record[1]["selected"][0])]
    assert a1_row["text_answer"] is None

    # Q5 (name) text
    a5_row = ans_by_q[5]
    assert a5_row["question_type"] == "text"
    assert a5_row["text_answer"] == answers_record[5]["text"]
    assert a5_row["options_selected"] is None

    # Q10 matrix
    a10_row = ans_by_q[10]
    assert a10_row["question_type"] == "matrix"
    cols_from_db = json.loads(a10_row["options_selected"])
    expected_cols = [int(answers_record[10]["rows"][r]) for r in by_q[10]["rows"]]
    assert sorted(cols_from_db) == sorted(expected_cols)

    # stats summary
    stats = history_db.stats_summary()
    assert stats["total_runs"] == 1
    assert stats["total_submissions"] == 1
    assert stats["total_success"] == 1
    assert stats["total_fail"] == 0
    assert 0.0 <= stats["success_rate"] <= 1.0
