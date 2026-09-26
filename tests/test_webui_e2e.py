"""webui 的浏览器自测 E2E（设计稿 §9 前端 gate 的第 ② 条）。

为什么需要它：``node --check`` 只保证 JS 语法合法，离线测试只保证 Python 侧对。
而这一类缺陷 —— ``hidden`` 压不过 ``display: grid``、sticky 控制栏把最后一行压成
点不到的死区、卡片计数滞后几秒、**在途 HTTP 响应把更新的 SSE 事件挤掉**、
批次跑完而历史栏仍停在上一批 —— 全都属于"语法合法、Python 全绿、而界面上不成立"。
本仓库对这一类缺陷的既有答案就是真浏览器（v3.0 那 4 条同理）。

**这里刻意用替身的只有三处**：``create_driver``、``detect_questions``，以及
``run_batch_fn`` 那一段批次循环。真探测与真提交由 ``tests/test_e2e_integration.py``
和一次性长跑脚本覆盖 —— 它们要再开一个浏览器（引擎自己开自己的），放进 CI 的必填
检查只会换来随机红。

替身必须照真引擎的**对外可观察行为**做（更新计数、逐轮回调 ``on_round``、逐题落
``answers``、在轮边界轮询 ``stop_check``、收尾按 ``RunState.history_status()`` 落库），
少做一样，界面那一侧就有面板永远没数据可断言 —— 这一轮就是这么发现"停止按钮只测到
了按钮变灰"的。本文件覆盖的是**宿主这一侧的整条链**：HTTP 路由 → worker 线程 →
SSE → 前端渲染，一步都不假：点下去的是真按钮，走的是真回环端口与真 ``EventSource``。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib.util as _iu  # noqa: E402

if _iu.find_spec("selenium") is None:
    _DRIVER_ERR = "selenium 未安装"
else:
    try:
        from selenium import webdriver  # noqa: F401
        _DRIVER_ERR = None
    except Exception as _e:  # pragma: no cover
        _DRIVER_ERR = f"selenium import 失败: {_e}"
        webdriver = None  # type: ignore[assignment]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_DRIVER_ERR is not None,
                       reason=f"webui E2E 需要 Selenium: {_DRIVER_ERR}"),
]

from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.support import expected_conditions as EC  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402

from src.cli import RoundOutcome  # noqa: E402
from src.models import RunState  # noqa: E402
from webui.api import Api  # noqa: E402
from webui.server import WebUIServer  # noqa: E402
from webui.session import Availability, RunSession, SessionPaths  # noqa: E402
from webui.service import WebService  # noqa: E402

SURVEY_URL = "https://www.wjx.cn/vm/parity.aspx"

#: 与 tests/fixtures/mock_wjx.html 同构的 13 题（真探测的覆盖在 test_e2e_integration）
QUESTIONS: list[dict] = [
    {"q": 1, "type": "single", "title": "常用浏览器", "choices": ["a", "b", "c", "d"]},
    {"q": 2, "type": "multi", "title": "用过的功能",
     "choices": ["1", "2", "3", "4", "5", "6"]},
    {"q": 3, "type": "dropdown", "title": "所在城市",
     "choices": [f"c{i}" for i in range(8)]},
    {"q": 4, "type": "scale", "title": "满意度", "scale": 5, "scale_min": 1},
    {"q": 5, "type": "text", "title": "姓名", "field": "name"},
    {"q": 6, "type": "text", "title": "手机", "field": "phone"},
    {"q": 7, "type": "text", "title": "邮箱", "field": "email"},
    {"q": 8, "type": "matrix", "title": "频率", "rows": ["1", "2"],
     "cols": ["a", "b", "c"]},
    {"q": 9, "type": "matrix_multi", "title": "关注点", "rows": ["1", "2"],
     "cols": ["a", "b"]},
    {"q": 10, "type": "scale", "title": "推荐度", "scale": 10, "scale_min": 0},
    {"q": 11, "type": "text", "title": "公司"},
    {"q": 12, "type": "single", "title": "性别", "choices": ["男", "女"]},
    {"q": 13, "type": "sort", "title": "排序", "items": ["3", "1", "2"]},
]


class FakeDriver:
    """探测路径够用的假 driver（真 DOM 上的探测不在本文件的职责里）。"""

    class _Switch:
        def default_content(self) -> None:
            pass

        def frame(self, _i) -> None:
            pass

    def __init__(self) -> None:
        self.switch_to = FakeDriver._Switch()
        self.quit_calls = 0

    def get(self, _url) -> None:
        pass

    def execute_script(self, script, *_a, **_kw):
        if "readyState" in script:
            return "complete"
        return len(QUESTIONS)          # 题目控件计数：一次就命中"主文档有题"

    def quit(self) -> None:
        self.quit_calls += 1


@dataclass
class Console:
    url: str
    driver: object
    session: RunSession
    service: WebService
    rounds: list
    hold: dict


@pytest.fixture(scope="module")
def console():
    """真服务 + 真回环端口 + 真浏览器；整模块只起一次。"""
    tmp = tempfile.mkdtemp(prefix="wjx_webui_e2e_")
    session = RunSession(
        paths=SessionPaths(tmp),
        availability=Availability(config_io=True, history=True, qr=True,
                                  selenium=True),
    )
    rounds: list = []
    # 用例往这里塞钩子（``hold["after_round"] = fn``），用来把 worker 停在两轮之间
    # —— 「停止」的全部语义就是下一轮开始前看到那个 bool，不停住就没机会按。
    hold: dict = {}

    def run_batch(url, total, **kw) -> None:
        """假引擎：真引擎会更新计数并回调 on_round，替身也必须这么做，
        否则进度与计数断言测的就不是同一条数据通路。"""
        state: RunState = kw["state"]
        kw["log"]("[平台] 假引擎开始跑这一批")
        # 真引擎会开批次、收尾批次；不照着做的话历史栏永远是空的，
        # 而"历史栏空着"看起来像是前端的问题
        db = kw.get("history_db")
        run_id = None
        if db is not None:
            run_id = db.start_run(url, int(total), state.browser, state.use_uc,
                                  weight_config=state.weight_config_snapshot)
            state.run_id = run_id
        for index in range(1, int(total) + 1):
            # 停止只在**轮边界**生效（src/cli.py:724），而且那一行是引擎自己写的，
            # 不是宿主写的 —— 替身不去轮询 stop_check，浏览器里那颗「停止」按钮
            # 测到的就只是"按钮变灰了"。
            stop = kw.get("stop_check")
            if index > 1 and stop is not None and stop():
                state.mark_interrupted()
                kw["log"]("[平台] 已停止运行（已成功份数可下次恢复）")
                break
            state.mark_success()
            state.current_attempt = index
            if db is not None and run_id is not None:
                # 逐题落明细也是真引擎干的（question_stage 里那段 record_answer）。
                # 替身不写，「点一批看明细」就没有数据可点 —— 而这是明细面板
                # 唯一的入口。两道题分别覆盖两个渲染分支：选了什么的、以及
                # --no-record-text 下该留 NULL 的填空题。
                db.record_answer(run_id=run_id, submission_index=index,
                                 question_number=1, question_type="single",
                                 options_selected=[2], text_answer=None,
                                 elapsed_ms=120)
                db.record_answer(run_id=run_id, submission_index=index,
                                 question_number=5, question_type="text",
                                 options_selected=None,
                                 text_answer=None if kw["no_record_text"] else "张三",
                                 elapsed_ms=30)
            outcome = RoundOutcome(index=index, outcome="success",
                                   message="提交成功")
            rounds.append(outcome)
            kw["on_round"](outcome)
            waiter = hold.get("after_round")
            if waiter is not None:
                waiter(index)
        if db is not None and run_id is not None:
            # 状态由 RunState 判：按停止收尾的那批必须是 interrupted，
            # 否则下次进来问"要不要续传"就永远不会出现
            db.finish_run(run_id, state.success_count, state.fail_count, 1.0,
                          state.history_status())

    service = WebService(
        session,
        create_driver=lambda *a, **k: FakeDriver(),
        detect_questions=lambda _driver: [dict(q) for q in QUESTIONS],
        # 验证码那两条必须一起换掉：真实现会把假 driver 的 JS 探测值当成
        # "验证码出现了"，然后卡在等人工滑块上 —— 症状是探测永远不返回。
        is_smart_verification_showing=lambda _d: False,
        wait_for_manual_verification=lambda _d, **_k: True,
        run_batch_fn=run_batch,
        settle_seconds=0.0,
        page_ready_timeout=1,
        question_ready_timeout=1,
        sleeper=lambda _s: None,
    )
    api = Api(session, service)
    server = WebUIServer(api).start()
    # 与 server.run() 同一句：本文件绕开 run()（它是阻塞的），所以启动日志得自己写。
    # 这条断言要验的是"服务端日志能不能经 SSE 落到终端"，不是"谁写的这行"。
    session.log(f"Web 界面已就绪 → {server.url}", "OK")
    driver = _open_browser()
    try:
        yield Console(server.url, driver, session, service, rounds, hold)
    finally:
        driver.quit()
        server.shutdown()
        service.close_db()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def _open_browser():
    from selenium.webdriver.edge.options import Options as EdgeOpts

    opts = EdgeOpts()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,1050")
    try:
        driver = webdriver.Edge(options=opts)
    except Exception:  # pragma: no cover - 只有 Edge 缺席时才走
        from selenium.webdriver.chrome.options import Options as ChromeOpts

        driver = webdriver.Chrome(options=ChromeOpts())
    driver.set_script_timeout(30)
    return driver


@pytest.fixture()
def page(console):
    """每个用例重新加载页面，并把服务端表单**摆回同一个已知状态**。

    浏览器与服务器都是 module 作用域的，用例之间会串状态：上一例留下的空 URL
    会让下一例的「探测题目」拿到 400（"请先填写问卷 URL"），症状是等待超时，
    看起来像前端坏了。
    """
    import json
    import urllib.request

    console.driver.set_script_timeout(30)
    console.driver.get(console.url)
    WebDriverWait(console.driver, 30).until(
        EC.presence_of_element_located((By.ID, "btn-run")))
    # 首帧 hello 带着整份快照；等它落地再断言，否则"页面没渲染完"会伪装成 bug
    WebDriverWait(console.driver, 30).until(
        lambda d: d.execute_script(
            "return document.getElementById('conn').textContent.length > 0;"))
    _set_text(console.driver, "url", SURVEY_URL)
    _set_text(console.driver, "count", 3)
    for _ in range(100):
        with urllib.request.urlopen(console.url + "api/state", timeout=5) as r:
            form = json.loads(r.read().decode("utf-8"))["state"]["form"]
        if form["url"] == SURVEY_URL and form["count"] == 3:
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"表单没同步到后端：{form}")
    return console.driver


# ------------------------------------------------------------------ 辅助


def _set_text(driver, element_id, value):
    """改输入框并触发 change —— 宿主读字段靠的是 change/blur，不是 input。"""
    box = driver.find_element(By.ID, element_id)
    driver.execute_script("arguments[0].value = '';", box)
    box.send_keys(str(value))
    driver.execute_script(
        "arguments[0].dispatchEvent(new Event('change'));", box)


def _text(driver, element_id):
    return _of(driver, driver.find_element(By.ID, element_id))


def _of(driver, element):
    """取**渲染出来**的文字。

    Selenium 的 ``.text`` 按规范只给"已渲染"的文本，而权重表与批次表的行有
    逐行入场动画（``opacity: 0`` + ``animation-delay``），刚建出来那一瞬读它是空串。
    元素确实在 DOM 里、人也看得见，所以这里走 textContent，不去和动画抢时序。
    """
    return driver.execute_script("return arguments[0].textContent;", element)


# ------------------------------------------------------------------ 用例


def test_the_console_boots_and_the_sse_hello_arrives(page) -> None:
    """首帧必须带全量状态：断线重连靠它收敛，缺了就是一段永远看不到的空白。"""
    assert "本机服务已连接" in _text(page, "conn")
    startup = _text(page, "log-lines")
    assert "Web 界面已就绪" in startup, \
        "启动日志要经 SSE 落到终端，而不是只有服务端知道自己起来了"
    assert _text(page, "status-text") == "就绪"
    assert page.find_element(By.ID, "btn-stop").is_enabled() is False
    # 终端那三件套的形状：4 位右对齐行号 + ❯ 前缀 + HH:MM:SS（桌面版 log_view 同形）
    first_line = page.find_element(By.CSS_SELECTOR, "#log-lines .log-line")
    assert first_line.find_element(By.CLASS_NAME, "prompt").text == "❯"
    assert len(first_line.find_element(By.CLASS_NAME, "ts").text) == 8
    gutter_cells = page.find_elements(By.CSS_SELECTOR, "#log-gutter div")
    # 用 textContent 而不是 .text：gutter 的 4 位对齐是**空格**撑出来的，
    # 而 .log-gutter 没设 white-space: pre，Selenium 的 .text 会把前导空格缩掉
    assert gutter_cells and all(len(_of(page, c)) == 4 for c in gutter_cells), \
        f"行号列不再右对齐 4 位：{[_of(page, c) for c in gutter_cells]!r}"


def test_an_invalid_count_is_rejected_and_said_in_plain_words(page) -> None:
    """桌面版手输 abc 是未捕获 TclError，表现成"点了没反应"（设计稿 §5 修第 1 条）。"""
    _set_text(page, "count", 0)
    WebDriverWait(page, 20).until(
        lambda d: "9999" in _text(d, "settings-note"))
    assert "1 ~ 9999" in _text(page, "settings-note")


def test_detecting_fills_the_weight_table_and_the_card_counter_together(
    page
) -> None:
    """表与计数由同一个事件带回来：分开刷就会出现"表有了、卡片还写未探测"。"""
    page.find_element(By.ID, "btn-detect").click()
    WebDriverWait(page, 60).until(
        lambda d: len(d.find_elements(
            By.CSS_SELECTOR, "#table-body .table-row")) >= 13)
    assert _text(page, "table-meta") == "13 题"
    assert page.find_element(By.ID, "table-empty").is_displayed() is False
    first = page.find_element(By.CSS_SELECTOR, "#table-body .table-row")
    assert "单选" in _of(page, first)                      # 胶囊与选项数
    # 权重格里的预填是 input 的 value，不是元素的 text —— .text 拿不到
    assert first.find_element(
        By.TAG_NAME, "input").get_attribute("value") == "0.2500,0.2500,0.2500,0.2500"
    zero_based = page.find_element(By.CSS_SELECTOR,
                                   "#table-body input[data-q='10']")
    assert zero_based.get_attribute("value") == ",".join(["1"] * 11), \
        "0~10 是 11 格：按 scale 当个数预填会少一格，另存时就被自己的校验拒收"
    assert "0~10" in _of(page, page.find_element(
        By.CSS_SELECTOR, "#table-body .table-row:nth-child(10)"))


def test_a_run_drives_log_progress_and_counters_over_sse(page) -> None:
    """点下去的是真按钮，走的是真 worker 线程 + 真 SSE；只有引擎是替身。"""
    _run_a_batch(page)

    assert _text(page, "fail-count") == "0"
    assert _text(page, "rounds") == "3 / 3"
    assert _text(page, "pct").startswith("100")
    assert page.find_element(By.ID, "progress").get_attribute(
        "aria-valuenow") == "100"
    assert "提交成功" in _text(page, "log-lines")
    assert page.find_element(By.ID, "btn-run").is_enabled() is True
    assert page.find_element(By.ID, "btn-stop").is_enabled() is False
    assert _text(page, "status-text") == "就绪"


def test_the_history_view_switch_actually_hides_the_other_one(page) -> None:
    """``hidden`` 压不过 ``display: grid`` 这一类只有真渲染才看得见。"""
    _open_history(page)
    assert page.find_element(By.ID, "view-run").is_displayed() is False
    assert page.find_element(By.ID, "view-history").is_displayed() is True
    assert page.find_element(
        By.ID, "tab-history").get_attribute("aria-selected") == "true"


def _run_a_batch(page, rounds=3):
    """跑一批（引擎是替身，秒级完成）并等收尾横幅。

    这里只按「开始」—— 停止要停在轮边界上才算测到，另见
    ``test_pressing_stop_brakes_the_batch_at_a_round_boundary``。
    """
    page.find_element(By.ID, "btn-run").click()
    WebDriverWait(page, 60).until(
        lambda d: "执行结束" in _text(d, "log-lines"))
    WebDriverWait(page, 15).until(
        lambda d: _text(d, "ok-count") == str(rounds))


def _open_history(page):
    """历史栏的数据是切过去才拉的，而且没切过去时整块在 hidden 子树里、点不到。"""
    page.find_element(By.ID, "tab-history").click()
    WebDriverWait(page, 20).until(
        lambda d: d.find_element(By.ID, "view-history").is_displayed())


def test_a_finished_batch_is_listed_and_its_detail_opens(page) -> None:
    """自己跑一批再看历史栏：用例之间不许有先后依赖。"""
    _run_a_batch(page)
    _open_history(page)
    WebDriverWait(page, 30).until(
        lambda d: d.find_elements(By.CSS_SELECTOR, "#runs-body .run-row"))
    rows = page.find_elements(By.CSS_SELECTOR, "#runs-body .run-row")
    assert rows, "刚跑完的批次应该出现在列表里"
    assert "3 / 0" in _of(page, rows[0])
    # 最后一行必须点得到：sticky 控制栏盖住它就是这次修掉的那个死区
    page.execute_script("arguments[0].scrollIntoView({block: 'center'});", rows[0])
    rows[0].click()
    WebDriverWait(page, 30).until(
        lambda d: len(d.find_elements(
            By.CSS_SELECTOR, "#answers-body .ans-row")) > 0)
    assert "None" not in _text(page, "answers-body"), "NULL 不许印成字面 None"


def test_a_purge_preview_reports_and_deletes_nothing(page) -> None:
    """刚跑完的数据不满 7 天：必须只说"无需清理"，不许出现确认框、更不许真删。"""
    _open_history(page)
    page.find_element(By.ID, "btn-purge").click()
    WebDriverWait(page, 20).until(
        lambda d: "无需清理" in _text(d, "purge-note"))
    assert page.find_element(By.ID, "purge-ask").is_displayed() is False
    assert page.find_elements(By.CSS_SELECTOR, "#runs-body .run-row"), \
        "预览阶段一条都不该少"


def test_pressing_stop_brakes_the_batch_at_a_round_boundary(page, console) -> None:
    """设计稿 §9 要前端 gate 覆盖"点开始/停止" —— 停止是几小时长跑唯一的刹车。

    真引擎的停止生效点只有一个：下一轮开始前读到 ``stop_flag``。所以这里把 worker
    按在第 1 轮做完之后，人在浏览器里按下停止再放行 —— 测的是那颗按钮真能刹住批次，
    而不是只让它变灰。收尾状态顺带查一眼：按停止的那批必须落成 ``interrupted``，
    否则下次进来永远不会问"要不要续传"。
    """
    gate = threading.Event()
    parked = threading.Event()

    def park(_index: int) -> None:
        parked.set()
        gate.wait(20)

    console.hold["after_round"] = park
    try:
        page.find_element(By.ID, "btn-run").click()
        WebDriverWait(page, 30).until(
            lambda d: d.find_element(By.ID, "btn-stop").is_enabled())
        assert parked.wait(20), "worker 没停在轮边界，后面那一下停止等于没测"
        page.find_element(By.ID, "btn-stop").click()
        assert page.find_element(By.ID, "btn-run").is_enabled() is False, \
            "还在跑的时候不许再按一次开始"
        gate.set()
        WebDriverWait(page, 60).until(
            lambda d: "执行结束" in _text(d, "log-lines"))
        WebDriverWait(page, 15).until(lambda d: _text(d, "ok-count") == "1")
    finally:
        gate.set()
        console.hold.pop("after_round", None)

    lines = _text(page, "log-lines")
    assert "用户请求停止" in lines, "宿主那句要说，否则不知道按没按上"
    assert "已停止运行" in lines, "引擎那句也要看得见（src/cli.py:726 同一条）"
    assert _text(page, "rounds") == "1 / 3", "剩下两份没交出去，进度不能走到头"
    assert page.find_element(By.ID, "btn-run").is_enabled() is True
    assert page.find_element(By.ID, "btn-stop").is_enabled() is False
    assert _text(page, "status-text") == "就绪"
    _open_history(page)
    # 只查"这一批"的特征，不数总条数 —— 数条数等于依赖同模块前面几个用例攒下的
    # 批次，单独跑这一条就红（本文件开头那句"用例之间不许有先后依赖"说的就是它）。
    # `interrupted` 全模块只有这一条用例会造出来。
    WebDriverWait(page, 30).until(
        lambda d: [r for r in d.find_elements(
            By.CSS_SELECTOR, "#runs-body .run-row")
            if "interrupted" in _of(d, r)])
    assert len([r for r in page.find_elements(
        By.CSS_SELECTOR, "#runs-body .run-row")
        if "interrupted" in _of(page, r)]) == 1, \
        "按停止收尾的批次标成了 finished 就永远续传不了"
