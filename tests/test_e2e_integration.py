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
import sys
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


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _DRIVER_ERR is not None,
        reason=f"E2E 需要 Selenium: {_DRIVER_ERR}",
    ),
]


# ------------------------------ Fixtures ------------------------------

PROJ = Path(__file__).resolve().parent.parent
FIXTURE_HTML = PROJ / "tests" / "fixtures" / "mock_wjx.html"
MP_FIXTURE_HTML = PROJ / "tests" / "fixtures" / "mock_wjx_multipage.html"
GAP_FIXTURE_HTML = PROJ / "tests" / "fixtures" / "mock_wjx_required_gap.html"
REAL_WIDGETS_HTML = PROJ / "tests" / "fixtures" / "mock_wjx_real_widgets.html"
REGION_FIXTURE_HTML = PROJ / "tests" / "fixtures" / "mock_wjx_region.html"

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


@pytest.fixture(autouse=True)
def _fresh_page(driver):
    """每个用例重新加载一次 mock 页面。

    ``driver`` 是 module 作用域（整个文件只开一次浏览器），于是 DOM 状态在用例之间
    是共享的：上一个用例勾过的 checkbox 会留在页面上，"每行各勾 1 个"这种断言
    实际测的是累计值（v3.0 加矩阵多选时就是这么红的）。file:// 重新加载只要几十毫秒，
    换来的是用例可以任意换顺序跑。
    """
    driver.get("file:///" + FIXTURE_HTML.as_posix())
    driver.switch_to.default_content()
    yield


@pytest.fixture()
def history_db(tmp_path):
    """临时 SQLite history DB。"""
    from src.history import SubmissionHistory
    db_path = tmp_path / "history.db"
    db = SubmissionHistory(str(db_path))
    yield db
    db.close()


# ------------------------------ 测试主体 ------------------------------

def test_detection_discovers_all_13_questions(driver):
    """detection 必须识别出 mock HTML 的全部 13 道题，类型/参数正确。"""
    from src.detection import detect_questions

    qs = detect_questions(driver)
    # 按题号升序，q1..q13 必须都在
    qnums = [q["q"] for q in qs]
    assert qnums == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13], (
        f"题号集不匹配: {qnums}"
    )

    by_q = {q["q"]: q for q in qs}

    # 1. single (单选) - 4 选项
    assert by_q[1]["type"] == "single"
    assert by_q[1]["choices"] == [1, 2, 3, 4]

    # 2. multi (多选) - 6 选项，其中第 6 项自带填空框（"其他____"）
    assert by_q[2]["type"] == "multi"
    assert by_q[2]["choices"] == [1, 2, 3, 4, 5, 6]
    assert by_q[2]["blank_options"] == [6], (
        "带填空框的选项必须被单独标出来（值是 option value，不是下标）"
    )

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



    # 11. scale —— 起点不是 1 的量表（v2.6 新增的 fixture 题）
    # detection 此前硬编码 scale_min=1 且把"格子数"当 scale，
    # 于是 2~10 分的量表被读成 scale=9 / scale_min=1：永远点不到 10，还会去点 1。
    assert by_q[11]["type"] == "scale"
    assert by_q[11]["scale"] == 10, by_q[11]
    assert by_q[11]["scale_min"] == 2, by_q[11]

    # 12. matrix_multi —— 行内是 checkbox 的矩阵（v3.0 新增题型）
    # 只看 radio 的旧逻辑会把它判成"没有矩阵"，或干脆被 section 1 留在 multi：
    # 于是每题答的是"一个列值"，形状与页面要求不符，提交被平台拒。
    assert by_q[12]["type"] == "matrix_multi", by_q[12]
    assert by_q[12]["rows"] == [1, 2, 3], by_q[12]
    assert by_q[12]["cols"] == [1, 2, 3, 4], by_q[12]
    assert "choices" not in by_q[12], "矩阵题的顶层 choices 必须清掉（与单选同规则）"

    # 13. sort —— 排序题（v3.0 新增题型）
    assert by_q[13]["type"] == "sort", by_q[13]
    assert by_q[13]["items"] == ["1", "2", "3", "4"], by_q[13]


def test_matrix_multi_actually_gets_checked_in_the_dom(driver):
    """矩阵多选要真的把 checkbox 勾上，而不是"JS 生成了但一行都没命中"。

    ``anyHit`` 返回 True 只说明脚本跑完了；这里直接读 DOM 的 checked 状态，
    并且确认**每行至少一个** —— 少一行就会触发平台的"每行必选"校验。
    """
    import json

    from src.answering_v2 import generate_answer
    from src.detection import detect_questions
    from src.interactions.matrix import js_fill_matrix_multi

    q = next(x for x in detect_questions(driver) if x["q"] == 12)
    ans = generate_answer(q)
    assert ans["type"] == "matrix_multi"
    assert js_fill_matrix_multi(driver, 12, ans["rows"]) is True

    states = driver.execute_script(
        "return JSON.stringify([1,2,3].map(function(r) {"
        "  var box = document.querySelectorAll('input[name=q12_' + r + ']');"
        "  var hit = 0;"
        "  box.forEach(function(b) { if (b.checked) hit += 1; });"
        "  return hit;"
        "}));"
    )
    hits = json.loads(states)
    assert hits == [1, 1, 1], f"每行应各勾中 1 个（默认 pick_options=[1]），实际 {hits}"


def test_option_level_blank_is_filled_and_gates_the_resume_scan(driver):
    """勾中"其他____"必须同时把那格写上，而它又决定这题算不算"答完"。

    三件事只有真浏览器能一起验出来：
      1. 作答侧点完之后还能定位到那一格 —— "探测说有框、注入找不到框"是最坏的组合，
         Python 侧全绿而页面空着；
      2. 写入受 maxlength 约束（``el.value = ...`` 不会被浏览器裁，服务端却按超长拒）；
      3. 已答扫描在"勾了、格子空着"时必须报未答，否则 ``--resume`` 会跳过这一题，
         交上去只剩一个看不出原因的 unknown。
    """
    import json as _json

    from src import config
    from src.detection import detect_answered_questions, detect_questions
    from src.pipeline_stages.question_stage import _answer_one_question

    config.WEIGHT_CONFIG[2] = {
        "type": "multi",
        "weights": [0, 0, 0, 0, 0, 1],
        "count_options": [1],
        "count_weights": [1],
    }
    try:
        q2 = next(x for x in detect_questions(driver) if x["q"] == 2)
        assert _answer_one_question(driver, q2) is True

        state = driver.execute_script(
            "return JSON.stringify({"
            "  checked: !!document.querySelector('#q2_6').checked,"
            "  text: document.querySelector('#q2_6_text').value"
            "});"
        )
        info = _json.loads(state)
        assert info["checked"] is True, "第 6 项应被勾中"
        assert info["text"].strip(), "被勾中的'其他'那一格必须有文本"
        assert len(info["text"]) <= 6, "maxlength=6 必须在写入时就被尊重"
        assert 2 in detect_answered_questions(driver)

        # 反证：格子清空后这题不能再算已答（续填才会重做它）
        driver.execute_script("document.querySelector('#q2_6_text').value = '';")
        assert 2 not in detect_answered_questions(driver)
    finally:
        config.WEIGHT_CONFIG.pop(2, None)


def test_page_alert_is_captured_instead_of_blocking_the_driver(driver):
    """页面弹 alert 时：WebDriver 不被噎住，而且那句原因能读回 Python 侧。

    不打钩的旧行为是：必填校验的原生弹窗让**下一条**命令抛
    ``UnexpectedAlertPresentException`` → 整轮按瞬态异常重跑 → 重跑之后
    弹窗原因早就没了，日志里只剩一行看不出所以然的 unknown。
    这里验的是接管之后的三个后果：命令照常执行、文案被读回、且读一次就清空。
    """
    from src.pipeline_stages.page_loader import (
        collect_blocked_alerts,
        describe_blocked_alerts,
        install_alert_recorder,
    )

    # 勾中带填空框的选项但不写字 → 提交时页面必然弹 alert
    driver.execute_script("document.querySelector('#q2_6').checked = true;")
    assert install_alert_recorder(driver) is True
    # 原生 alert 留着作对照：接管只是把它包起来，没有破坏页面
    assert driver.execute_script("return typeof window.__wjxNativeAlert;") == "function"

    driver.execute_script("document.getElementById('submit_button').click();")
    # 关键一步：页面刚刚"弹"过 alert，而这条命令没有抛
    assert driver.execute_script("return document.readyState") == "complete"

    alerts = collect_blocked_alerts(driver)
    assert any("第 2 题" in a for a in alerts), f"没捞到必填提示：{alerts}"
    assert describe_blocked_alerts(alerts)[0].startswith("[页面弹窗]")
    # 读过即清空：同一条原因不该在后面的轮次里反复出现
    assert collect_blocked_alerts(driver) == []
    # 提交确实被校验挡下了（成功文案不该出现）
    assert driver.execute_script(
        "return document.querySelector('#submit_result') === null;"
    ) is True


def test_clean_submission_does_not_trip_the_blank_validation(driver):
    """反面对照：写字之后再提交，页面不弹、也不拦。

    只有"填了也不让过"能被这条测出来 —— 那说明 fixture 的校验写错了，
    上面那条用例通过的原因也就不可信了。
    """
    from src.pipeline_stages.page_loader import (
        collect_blocked_alerts,
        install_alert_recorder,
    )

    # 照样打钩：万一校验写错了，也不该留一个原生弹窗把后续用例一起拖死
    install_alert_recorder(driver)
    driver.execute_script(
        "var c = document.querySelector('#q2_6');"
        "c.checked = true;"
        "document.querySelector('#q2_6_text').value = '自建渠道';"
    )
    driver.execute_script("document.getElementById('submit_button').click();")
    assert collect_blocked_alerts(driver) == []
    assert driver.execute_script("return window.__submitClicks;") == 1


def test_sort_question_writes_order_into_dom_and_hidden_input(driver):
    """排序题必须**同时**重排 DOM 与写隐藏 input，缺一条就是"看起来答了"。

    只重排不写值 → 平台收到空序；只写值不重排 → 页面的 sortable 监听会按
    界面上的顺序把值覆盖回去。这两条在真 DOM 里都测不出来，Python 侧全绿。
    """
    import json as _json

    from src.answering_v2 import generate_answer
    from src.detection import detect_questions
    from src.interactions.sort import js_fill_sort

    q = next(x for x in detect_questions(driver) if x["q"] == 13)
    order = generate_answer(q)["order"]
    assert sorted(order) == ["1", "2", "3", "4"], f"每项都得有一个位置: {order}"

    assert js_fill_sort(driver, 13, order) is True

    dom_order = driver.execute_script(
        "return JSON.stringify(Array.prototype.map.call("
        "  document.querySelectorAll('#q13_list li'),"
        "  function(li) { return li.getAttribute('value'); }));"
    )
    hidden_val = driver.execute_script(
        "return document.querySelector('input[name=q13]').value;"
    )
    assert _json.loads(dom_order) == order, f"DOM 顺序没跟着答案走: {dom_order}"
    assert hidden_val == ",".join(order), f"提交值没写进隐藏域: {hidden_val!r}"

    # 结构对不上时必须 False（宁可该题判失败，也不交一份假答好的排序题）
    assert js_fill_sort(driver, 99, ["1", "2"]) is False


def test_detection_extracts_question_titles_for_anchoring(driver):
    """v3.0 权重锚定的前提：探测能把**题干**带回来（只有真 DOM 抓得住这条）。

    Python 侧的单测全部喂手工 dict，锚点匹配、序号剥离都只算"逻辑成立"；
    题面元素的选择器写错（或只取到整个容器的 textContent），
    离线一律绿，实际保存出去的 anchor 却是空的或一串选项文字 —— 锚定静默失效。
    """
    from src import anchoring
    from src.detection import detect_questions

    by_q = {q["q"]: q for q in detect_questions(driver)}

    # 1) 题面被识别到，且不含选项文案
    title1 = by_q[1].get("title", "")
    assert "您的性别" in title1, f"题干没取到: {by_q[1]!r}"
    assert "男" not in title1, f"题干把选项文案吸进来了: {title1!r}"

    # 2) 归一化后能剥掉 <span class="idx">1.</span> 这层序号
    norm = anchoring.normalize_title(title1)
    assert norm.startswith("您的性别"), f"题号前缀没剥掉: {title1!r} → {norm!r}"

    # 3) 每题都该有题面；缺一个就意味着 anchor 会静默不写
    missing = [n for n, q in by_q.items() if not str(q.get("title") or "").strip()]
    assert not missing, f"以下题目没取到题干: {missing}"

    # 4) 锚点闭环：由探测结果造锚点 → 放回同一题必须认领，且结构签名对得上
    anchor = anchoring.make_anchor(by_q[2])
    assert anchor is not None
    assert anchoring.anchor_matches_question(anchor, by_q[2]) is True
    assert anchoring.anchor_matches_question(anchor, by_q[1]) is False


def test_platform_probe_reads_markers_and_crosscheck_stays_silent(driver):
    """v3.1 结构对拍在真 DOM 上的两头：读得到平台自报结构，且探测对了就不许出声。

    为什么必须占一个 E2E 用例：Python 侧单测喂的是手工 dict，"候选选择器 + 属性名
    怎么传进 JS""分页过滤与 ``detect_questions`` 口径是否一致""契约 2 的静默反面
    —— 探测明明判对了却刷一堆假警"这三件事都只有真浏览器能证。
    fixture 的 13 个容器都按真卷形态标了 ``topic`` / ``type``（见 fixture 内注释），
    所以这里期望的是：读数齐、码与形状一致、对拍**零提示**。
    """
    from src.crosscheck import crosscheck_questions
    from src.detection import detect_platform_questions, detect_questions
    from src.platforms import WJX

    items = detect_platform_questions(driver, WJX)
    assert [it["q"] for it in items] == list(range(1, 14)), f"平台读数: {items}"
    assert [it["code"] for it in items] == [
        "3", "4", "7", "5", "1", "1", "1", "2", "2", "6", "5", "6", "11",
    ], f"题型码读数: {items}"

    drift = crosscheck_questions(detect_questions(driver), items, WJX)
    assert drift == [], f"探测与平台自报本来一致，对拍却出声了: {drift}"


def test_required_but_undetected_question_becomes_a_pre_submit_gap(driver):
    """真 DOM 上锁住完整度自检的判据：平台标了必答、我们整题没探测到 → 缺口 = [那一题]。

    fixture 用的是**文件上传题** —— 它不在本工具的作答能力内，永远不会被"顺手修好"，
    所以这条反例不会哪天变成假测试（先前这里拿日期题举例，而 v3.1 已经把日期题
    探测修好了，反例就失效了）。为什么只能放在 E2E：判据的一半是"探测看不见这道题"，
    那取决于真实 DOM 与 CSS —— 手工 dict 里怎么构造都行，到了真页面上未必不可。
    另一半是"没标 ``req``（含 ``req=""``）的题不许拦" —— 拦错一次就是一单
    本来能交的问卷被判失败。
    """
    from src.completeness import describe_gap, unanswered_required
    from src.detection import detect_platform_questions, detect_questions
    from src.platforms import WJX

    driver.get("file:///" + GAP_FIXTURE_HTML.as_posix())

    ours = detect_questions(driver)
    assert [q["q"] for q in ours] == [1, 4], (
        f"上传题（Q2/Q3）本该探测不到，而 req 为空串的 Q4 该探测到: {ours}"
    )

    items = detect_platform_questions(driver, WJX, visible_only=False)
    assert [(it["q"], it["required"]) for it in items] == [
        (1, True), (2, True), (3, False), (4, False),
    ], f"平台自报结构读歪了（空 req 必须算不必答）: {items}"

    gap = unanswered_required(items, {int(q["q"]) for q in ours})
    assert gap == [2], f"缺口应该正好是那道必答题: {gap}"
    assert "Q2" in describe_gap(gap)


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
    from src.interactions.matrix import js_fill_matrix_multi

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
    # 勾中带填空框的选项（"其他____"）就必须同时写上那一格 —— 生产路径
    # （``_answer_one_question``）是这么做的，这里跟着模仿才能测出真实行为。
    from src.answering_v2 import generate_option_blank_text
    from src.interactions.choices import js_fill_option_blank
    for _blank_value in by_q[2].get("blank_options") or []:
        if int(_blank_value) in vs2:
            assert js_fill_option_blank(
                driver, 2, _blank_value, generate_option_blank_text()
            ) is True
            assert driver.execute_script(
                "return (document.querySelector('#q2_6_text').value || '').trim().length;"
            ), "写了却读不回来：框定位错了或赋值没生效"
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

    # ============ Q12: matrix_multi（v3.0 新题型）============
    a12 = generate_answer(by_q[12])
    assert a12["type"] == "matrix_multi"
    rows12 = a12["rows"]
    assert set(rows12.keys()) == set(by_q[12]["rows"])
    for picked in rows12.values():
        assert isinstance(picked, list) and 1 <= len(picked) <= len(by_q[12]["cols"])
        assert len(set(picked)) == len(picked), "同一行不能勾出重复列"
        assert all(int(c) in by_q[12]["cols"] for c in picked)
    assert js_fill_matrix_multi(driver, 12, rows12) is True
    flat12 = [int(c) for vals in rows12.values() for c in vals]
    history_db.record_answer(
        run_id, submission_index, 12, "matrix_multi",
        options_selected=flat12,
    )
    answers_record[12] = a12

    q12_hits = driver.execute_script(
        "return JSON.stringify([1,2,3].map(function(r) {"
        "  return document.querySelectorAll("
        "    'input[name=q12_' + r + ']:checked').length;"
        "}));"
    )
    assert json.loads(q12_hits) == [len(x) for x in rows12.values()], (
        f"矩阵多选的 DOM 实际勾选数与答案不符: {q12_hits} vs {rows12}"
    )

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
    # 11 道题 → 11 条 answer 记录
    assert len(ans) == 11, f"期望 11 条 answer，实际 {len(ans)}"
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


# ==========================================================================
#  真实提交路径（v2.6 新增）
#
#  此前两个 E2E 只做"填写"，从不走 find_and_click_submit / run_one_submission，
#  所以本轮修掉的三个 P0（重复点击提交、成功路径冒泡触发整批重试、
#  selector 拼进 JS 字面量导致非法 CSS）它一个都抓不到。
#  mock 页面现在会用 JS 模拟问卷星的 AJAX 提交：URL 不变、150ms 后渲染
#  「提交成功」文案，并把点击次数记在 window.__submitClicks 上。
# ==========================================================================

def test_submit_clicks_once_and_detects_ajax_success(driver):
    """提交必须恰好点一次，并通过页面文案（而非 URL 变化）判定成功。"""
    from src.interaction import SUBMIT_SUCCESS, find_and_click_submit

    assert driver.execute_script("return window.__submitClicks;") == 0

    result = find_and_click_submit(driver, wait_url_change_timeout=5.0)

    assert result == SUBMIT_SUCCESS, (
        "AJAX 式提交（URL 不变 + 稍后出现成功文案）必须被识别为成功，"
        f"实际 {result!r}"
    )
    clicks = driver.execute_script("return window.__submitClicks;")
    assert clicks == 1, f"提交按钮必须恰好点一次，实际 {clicks} 次（重复提交风险）"


def test_submit_does_not_reclick_when_page_is_busy(driver):
    """点击后页面跳转期间读 current_url 抛错，绝不能导致重新点击。

    这是 v2.5 那个 P0 的真浏览器版本：旧实现把有副作用的点击和点击后的
    URL 确认放在同一个 @js_execute_retry(3) 区域内，确认阶段一抛异常
    就把整份问卷重新提交一遍。
    """
    from selenium.common.exceptions import WebDriverException

    from src.interaction import find_and_click_submit

    before = driver.execute_script("return window.__submitClicks;")

    class FlakyUrlProxy:
        """代理 driver：前 N 次 current_url 读取抛 WebDriverException。

        这正是"提交导致页面跳转、驱动短暂读不到 URL"的真实形态。
        """

        def __init__(self, inner, fail_first: int):
            self._inner = inner
            self._remaining = fail_first

        @property
        def current_url(self):
            self._remaining -= 1
            if self._remaining >= 0:
                raise WebDriverException("navigation in progress")
            return self._inner.current_url

        def find_element(self, *a, **kw):
            return self._inner.find_element(*a, **kw)

        def execute_script(self, *a, **kw):
            return self._inner.execute_script(*a, **kw)

    proxy = FlakyUrlProxy(driver, fail_first=3)
    find_and_click_submit(proxy, wait_url_change_timeout=1.0)

    after = driver.execute_script("return window.__submitClicks;")
    assert after - before == 1, (
        f"URL 读取抖动导致重复提交：本轮实际点击 {after - before} 次"
    )


def test_multipage_survey_answers_both_pages_then_submits_once(driver, history_db):
    """v3.0 分页问卷：两页都要答完，且**只在最后一页**点提交。

    这是分页支持真正的验收点，三条都只能在真浏览器里查：
      1. 探测必须只看当前可见页 —— 否则会在第 1 页去点第 3 题（第 2 页的控件），
         平台只收当前页的输入，结果是"整卷答完"仍被判未答；
      2. 翻页后必须继续答第二页，而不是把第一页当整卷交上去；
      3. 提交按钮只能点一次（``#ctlNext`` 一类歧义键误点会提前翻页/提交）。
    """
    from src.detection import detect_questions
    from src.pipeline import run_one_submission
    from src.utils import ManualHoldLock

    url = "file:///" + MP_FIXTURE_HTML.as_posix()
    driver.get(url)

    page1 = detect_questions(driver)
    assert [q["q"] for q in page1] == [1, 2], (
        f"第 1 页不该看到第 2 页的题: {[q['q'] for q in page1]}"
    )

    run_id = history_db.start_run(url, 1, "edge", False)
    outcome = run_one_submission(
        driver, url, ManualHoldLock(),
        history_db=history_db, run_id=run_id, submission_index=1,
    )
    history_db.finish_run(
        run_id,
        success_count=1 if outcome == "success" else 0,
        fail_count=0 if outcome == "success" else 1,
        total_elapsed_seconds=1.0, status="finished",
    )

    assert outcome == "success", f"分页 mock 应能答完两页并提交，实际 {outcome!r}"
    assert driver.execute_script("return window.__submitClicks;") == 1, "只能提交一次"

    # 两页各自的题都被真的勾/填上了
    for name in ("q1", "q3"):
        assert driver.execute_script(
            f"return document.querySelectorAll('input[name={name}]:checked').length;"
        ) == 1, f"{name} 没被答上"
    assert driver.execute_script(
        "return (document.getElementById('q2').value || '').trim().length > 0;"
    ) is True, "第 1 页的填空没写进去"

    qnums = sorted({int(a["question_number"]) for a in history_db.query_answers(run_id=run_id)})
    assert qnums == [1, 2, 3], f"逐题明细应覆盖两页的全部题目，实际 {qnums}"


def test_full_submission_roundtrip_through_pipeline(driver, history_db):
    """走完整 run_one_submission：探测 → 作答 → 提交 → 三态判定 → 落盘。

    这条是整套 E2E 真正的"全链路"，覆盖 iframe 适配、断点续填扫描、
    验证码探测、6 类题型的 JS 交互与提交确认。
    """
    import pathlib

    from src.pipeline import run_one_submission
    from src.utils import ManualHoldLock

    url = "file:///" + (
        pathlib.Path(__file__).resolve().parent / "fixtures" / "mock_wjx.html"
    ).as_posix()

    run_id = history_db.start_run(url, 1, "edge", False)
    outcome = run_one_submission(
        driver, url, ManualHoldLock(),
        history_db=history_db, run_id=run_id, submission_index=1,
        no_record_text=True,
    )
    history_db.finish_run(
        run_id,
        success_count=1 if outcome == "success" else 0,
        fail_count=0 if outcome == "success" else 1,
        total_elapsed_seconds=1.0, status="finished",
    )

    assert outcome == "success", f"mock 问卷应能被完整答完并提交，实际 {outcome!r}"
    clicks = driver.execute_script("return window.__submitClicks;")
    assert clicks == 1, f"整轮只应提交一次，实际 {clicks} 次"

    answers = history_db.query_answers(run_id=run_id)
    qnums = sorted({int(a["question_number"]) for a in answers})
    assert len(qnums) >= 8, f"逐题明细应覆盖绝大多数题目，实际 {qnums}"
    # no_record_text 必须真的屏蔽填空原文
    texts = [a["text_answer"] for a in answers if a["question_type"] == "text"]
    assert texts and all(t is None for t in texts), (
        f"--no-record-text 下填空文本应全为 NULL，实际 {texts[:3]}"
    )


# ----------------------- 真卷形态（v3.1 排序点击式 + 日期题）-----------------------

def _load_real_widgets(driver) -> None:
    driver.get("file:///" + REAL_WIDGETS_HTML.as_posix())


def test_real_markup_sort_and_date_are_detected(driver):
    """真卷那两种形态必须进得了探测 —— 当天它们**整题都没被认出来**。

    排序题的 ul class 是 ``ui-controlgroup ui-listview``（不含 sort），日期题的
    输入框是 ``readonly``。两者原先分别被 ``/sort/i`` 和 ``el.readOnly`` 两条判据
    挡在门外，症状不是报错而是"这题不存在"。
    """
    from src.crosscheck import crosscheck_questions
    from src.detection import detect_platform_questions, detect_questions
    from src.platforms import WJX

    _load_real_widgets(driver)
    by_q = {q["q"]: q for q in detect_questions(driver)}

    assert by_q[1]["type"] == "sort", f"排序题没认出来: {by_q.get(1)}"
    assert by_q[1]["sort_mode"] == "click", f"控件形态判错了: {by_q[1]}"
    assert by_q[1]["items"] == ["1", "2", "3"], f"选项值不对: {by_q[1]}"
    assert by_q[2]["type"] == "text" and by_q[2]["field"] == "date", (
        f"日期题应当是『填空 + date 字段』，实际: {by_q.get(2)}"
    )

    # 平台自报 11=排序、1=填空，与我们认出来的对得上 → 对拍该闭嘴
    items = detect_platform_questions(driver, WJX)
    assert crosscheck_questions(list(by_q.values()), items, WJX) == []


def test_click_mode_sort_is_filled_by_clicking_and_keeps_option_values(driver):
    """点击式排序：按目标顺序点，名次要落进 ``.sortnum``，而**不能碰隐藏域的 value**。

    这是这条修复最要命的一条断言：老办法往第一个 ``input[name=q1]`` 写 "3,1,2"，
    在真页面上等于把选项 1 的身份换成一串数字 —— 交上去的是脏数据，
    比"整题没答"更难发现。
    """
    from src.interactions.sort import js_fill_sort

    _load_real_widgets(driver)
    assert js_fill_sort(driver, 1, ["3", "1", "2"], mode="click") is True

    lis = driver.execute_script(
        "return JSON.stringify(Array.prototype.map.call("
        "document.querySelectorAll('#div1 ul li'), function (li) {"
        "  var i = li.querySelector('input[type=hidden]');"
        "  var s = li.querySelector('.sortnum');"
        "  return [i.value, (s.textContent || '').trim()];}));"
    )
    state = json.loads(lis)
    assert [v for v, _ in state] == ["3", "1", "2"], f"DOM 顺序不是点击顺序: {state}"
    assert [r for _, r in state] == ["1", "2", "3"], f"名次没落进 sortnum: {state}"

    raw = driver.execute_script(
        "return JSON.stringify(['q1_1','q1_2','q1_3'].map("
        "function (id) { return document.getElementById(id).value; }));"
    )
    assert json.loads(raw) == ["1", "2", "3"], f"隐藏域的选项身份被写坏了: {raw}"


def test_click_mode_sort_times_out_instead_of_lying(driver):
    """点不动（名次不落地）时返回 False，而不是交一份"看起来答了"的排序题。"""
    from src.interactions.sort import js_fill_sort

    _load_real_widgets(driver)
    driver.execute_script(
        "document.querySelectorAll('#div1 ul').forEach(function (u) {"
        "  u.addEventListener('click', function (e) { e.stopPropagation(); }, true);});"
    )
    assert js_fill_sort(driver, 1, ["2", "3", "1"], mode="click",
                        timeout=0.6, _sleep=lambda _s: None) is False


def test_readonly_datebox_receives_a_date_shaped_value(driver):
    """只读日期框：探测认出 date 字段，答案按日期格式生成并真的写进框里。

    JS 赋值不受 ``readonly`` 限制（readonly 只挡用户键入），而平台自己也是
    laydate 选好之后回写 value 再触发 blur —— 走的是同一条路。
    """
    import re

    from src.answering_v2 import generate_answer
    from src.detection import detect_questions
    from src.interactions.text import js_fill_text

    _load_real_widgets(driver)
    qdate = next(q for q in detect_questions(driver) if q["q"] == 2)
    text = generate_answer(qdate)["text"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", text), f"日期题答成了别的: {text!r}"
    assert js_fill_text(driver, 2, text) is True
    assert driver.find_element("id", "q2").get_attribute("value") == text


def test_matrix_scale_is_not_mistaken_for_a_single_scale(driver):
    """v3.1 矩阵量表：真卷上它被判成一个 4 级量表，因为容器 class 含 "rating"。

    这条要同时锁三件事，缺一件都会回到原来的静默错答：
      1. 探测最终落在 ``matrix_scale`` 上（量表分支先抢过，5c 必须抢回来并清掉残留）；
      2. 行按平台标的 ``tr[fid]`` 认，不是数行号 —— 行数算错时同类项目会静默漏行；
      3. 作答真的把分值写进提交槽，且 [对拍] 不再出声（码表里 6 现在接受三种矩阵形态）。
    """
    from src.crosscheck import crosscheck_questions
    from src.detection import detect_platform_questions, detect_questions
    from src.interactions.matrix import js_fill_matrix_scale
    from src.platforms import WJX

    _load_real_widgets(driver)
    q3 = next(q for q in detect_questions(driver) if q["q"] == 3)
    assert q3["type"] == "matrix_scale", f"又被判成量表了: {q3}"
    assert q3["rows"] == ["q3_0", "q3_1"], f"提交槽名不对: {q3}"
    assert q3["cols"] == [1, 2, 3, 4, 5], f"分值列不对: {q3}"
    assert "scale" not in q3 and "scale_min" not in q3, f"量表残留没清: {q3}"

    assert js_fill_matrix_scale(driver, 3, {"q3_0": 4, "q3_1": 2}) is True
    got = driver.execute_script(
        "return JSON.stringify([document.getElementById('q3_0').value,"
        " document.getElementById('q3_1').value]);")
    assert json.loads(got) == ["4", "2"], f"提交槽没写上: {got}"

    ours = [q for q in detect_questions(driver) if q["q"] == 3]
    plat = [p for p in detect_platform_questions(driver, WJX) if p["q"] == 3]
    assert crosscheck_questions(ours, plat, WJX) == []


def test_matrix_scale_fill_reports_missing_rows(driver):
    """槽或格子找不到时必须返回 False —— 不交一份"看起来点了"的量表。"""
    from src.interactions.matrix import js_fill_matrix_scale

    _load_real_widgets(driver)
    assert js_fill_matrix_scale(driver, 3, {"q3_0": 9}) is False      # 没有 dval=9 这格
    assert js_fill_matrix_scale(driver, 3, {"q9_9": 3}) is False      # 根本没有这一行


def test_region_signals_are_detected_and_answered_by_one_persona(driver):
    """地区题三条识别信号 + 答案必须与画像同省（v3.2 persona 绑定）。

    这一条只能在真浏览器里验：判据是探测注入 JS 里的 DOM 事实，离线替身看不见
    ``classList`` / ``onclick`` / ``verify``。
    """
    from src.answering_v2 import generate_answer
    from src.detection import detect_questions
    from src.interactions.text import js_fill_text
    from src.persona import active_persona, new_persona

    driver.get("file:///" + REGION_FIXTURE_HTML.as_posix())
    new_persona()
    p = active_persona()
    fields = {q["q"]: q.get("field") for q in detect_questions(driver)}
    assert fields[1] == "region", f"class=get_Local + onclick=opencitybox 没认出来: {fields}"
    assert fields[2] == "region", f"verify=省市 没认出来: {fields}"
    assert fields[3] == "region", f"题干兜底没认出来: {fields}"
    assert fields[4] == "address", f"真地址题被抢判成 region: {fields}"
    assert fields[5] == "name", fields

    qs = {q["q"]: q for q in detect_questions(driver)}
    regions = [generate_answer(qs[i])["text"] for i in (1, 2, 3)]
    assert regions == [f"{p.province} {p.city}"] * 3, f"三道地区题答出三个地方: {regions}"

    addr = generate_answer(qs[4])["text"]
    assert addr.startswith(p.city if p.municipality else p.province), (addr, p.province)
    assert generate_answer(qs[5])["text"] == p.name, "同一份问卷里换人了"

    assert js_fill_text(driver, 1, regions[0]) is True
    got = driver.find_element("id", "q1").get_attribute("value")
    assert got == f"{p.province} {p.city}", f"页面上留下的是 {got!r}"
