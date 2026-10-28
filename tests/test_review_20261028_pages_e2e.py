"""全面审查报告（data/full_review_20261028）页面级缺陷的回归测试。

报告里 5 个用真页面复现的案例，这里逐个翻成**修复后必须成立**的断言：

    P1-1  问卷正文里的感谢语不再被当成提交成功（基线转换判定）
    P1-5  量表后备定位不再点击另一道题（作用域钉死在目标题）
    P1-6  排序后备定位不再重排同级的另一道题（+ 提交值回读核验）
    P2-9  矩阵题缺行时必须报失败（逐行核验）
    P2-10 普通提示弹窗不再被识别成验证码

页面结构与 data/full_review_20261028/reproduce.py 的复现案例保持同构 ——
那份证据证明了缺陷存在，这里证明同一页面上缺陷已消除。
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

import pytest

# ------------------------------ 环境探测 ------------------------------
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

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))


@pytest.fixture(scope="module")
def driver():
    """headless 浏览器（Edge → Chrome 回退），与 test_e2e_integration 同一套取法。"""
    if webdriver is None:  # pragma: no cover - import 时已处理
        pytest.skip("webdriver import 失败")

    driver = None
    errs: list[str] = []
    try:
        from selenium.webdriver.edge.options import Options as EdgeOpts
        opts = EdgeOpts()
        for arg in ("--headless=new", "--disable-gpu", "--no-sandbox",
                    "--window-size=1280,900"):
            opts.add_argument(arg)
        driver = webdriver.Edge(options=opts)
    except Exception as e1:  # pragma: no cover - 依赖本机浏览器
        errs.append(f"edge-err:{e1!r}")
    if driver is None:
        try:
            from selenium.webdriver.chrome.options import Options as ChromeOpts
            opts = ChromeOpts()
            for arg in ("--headless=new", "--disable-gpu", "--no-sandbox",
                        "--window-size=1280,900"):
                opts.add_argument(arg)
            driver = webdriver.Chrome(options=opts)
        except Exception as e2:  # pragma: no cover - 依赖本机浏览器
            errs.append(f"chrome-err:{e2!r}")
    if driver is None:
        pytest.skip(f"无可用 webdriver（{', '.join(errs)}）")

    driver.set_script_timeout(10)
    driver.set_page_load_timeout(30)
    yield driver
    try:
        driver.quit()
    except Exception:  # pragma: no cover
        pass


def _page(driver, html: str) -> None:
    driver.get("data:text/html;charset=utf-8," + quote(html))


# ---------------------------------------------------------------------------
# P1-1 问卷正文里的感谢语 ≠ 提交成功（submit_success_detect_script 基线语义）
# ---------------------------------------------------------------------------
class TestP1P1SuccessDetectionBaseline:
    page = (
        "<body>您好！感谢您的参与！"
        "<button onclick=\"window.clicks=(window.clicks||0)+1\">提交</button></body>"
    )

    def _body_text(self, driver) -> str:
        return str(driver.execute_script(
            "return (document.body && document.body.innerText) || '';"))

    def test_intro_thanks_is_not_success_with_baseline(self, driver) -> None:
        """复现案例 manual_success_without_submission：介绍含感谢语 + 没点提交。

        修复前：判定为真（正文里有"感谢您的参与"）。修复后：基线里已有该
        文案 → 文案不构成成功证据 → 判定为假。
        """
        _page(driver, self.page)
        from src.interactions._scripts import submit_success_detect_script

        baseline = self._body_text(driver)
        pred = driver.execute_script(submit_success_detect_script(baseline))
        assert baseline and "感谢您的参与" in baseline
        assert not pred, "点击前的旧文案不能冒充提交成功"

    def test_no_baseline_keeps_legacy_keyword_verdict(self, driver) -> None:
        """基线拿不到（None）→ 退回旧判据：正文有强文案即真。

        这是刻意的取舍：基线丢失只发生在页面跳转的瞬间，那时宁可用旧判据，
        也不能把真成功全判成 unknown 触发重复提交。
        """
        _page(driver, self.page)
        from src.interactions._scripts import submit_success_detect_script

        assert driver.execute_script(submit_success_detect_script(None)) is True

    def test_newly_appeared_text_is_success(self, driver) -> None:
        """基线里没有、提交后新出现 → 文案恢复证据资格（正常完成页不受影响）。"""
        _page(driver, self.page)
        from src.interactions._scripts import submit_success_detect_script

        assert driver.execute_script(
            submit_success_detect_script("请问您对本服务满意吗？")) is True

    def test_success_container_must_be_visible(self, driver) -> None:
        """专用成功容器：可见才算数 —— display:none 的常驻模板节点不算。"""
        from src.interactions._scripts import submit_success_detect_script

        _page(driver, '<div class="success" style="display:none">done</div>')
        assert not driver.execute_script(submit_success_detect_script(None))
        _page(driver, '<div class="success" style="width:80px;height:20px">done</div>')
        assert driver.execute_script(submit_success_detect_script(None)) is True

    def test_manual_submit_without_click_times_out_as_failed(self, driver) -> None:
        """复现案例全链路：介绍含感谢语的页面，人没点提交 → 等满超时判 failed。"""
        from src.models import SUBMIT_FAILED
        from src.pipeline_stages.manual_submit import wait_for_manual_submit
        from src.utils import ManualHoldLock

        _page(driver, self.page)
        verdict = wait_for_manual_submit(driver, ManualHoldLock(), timeout=1.0)
        assert verdict == SUBMIT_FAILED, "没人点就不是提交成功"
        assert int(driver.execute_script("return window.clicks || 0")) == 0


# ---------------------------------------------------------------------------
# P1-5 量表后备定位必须落在目标题上
# ---------------------------------------------------------------------------
def test_p1_5_scale_fallback_scopes_to_target_question(driver) -> None:
    """复现案例 scale_fallback_clicks_wrong_question：两道评分题，要求填第 2 题。"""
    from src.interactions._scripts import set_scale_script

    html = (
        "<script>window.hits=[]</script>"
        + "".join(
            '<div id="div' + str(q) + '" class="rating">'
            + "".join(
                '<span class="point" onclick="window.hits.push(' + str(q) + ')">'
                + str(i) + "</span>"
                for i in range(1, 6)
            )
            + "</div>"
            for q in (1, 2)
        )
    )
    _page(driver, html)
    ok = driver.execute_script(set_scale_script(2, 5, 5))
    assert ok is True
    assert driver.execute_script("return window.hits") == [2], (
        "点击必须落在第 2 题上（修复前落到文档序第一个 .rating，即第 1 题）"
    )


def test_p1_5_scale_fallback_fails_without_any_question_scope(driver) -> None:
    """找不到目标题的任何锚点 → 失败，而不是拿别的题的通用评分控件兜底。"""
    from src.interactions._scripts import set_scale_script

    html = (
        '<div class="rating">'
        + "".join('<span class="point">' + str(i) + "</span>" for i in range(1, 6))
        + "</div>"
    )
    _page(driver, html)
    assert driver.execute_script(set_scale_script(7, 5, 5)) is False


# ---------------------------------------------------------------------------
# P1-6 排序后备定位必须绑定同一道题 + 提交值回读核验
# ---------------------------------------------------------------------------
_SORT_PAGE = "".join(
    '<div id="div' + str(q) + '"><ul class="lisort">'
    + "".join('<li value="' + str(i) + '">' + str(i) + "</li>" for i in (1, 2, 3))
    + '</ul><input type="hidden" name="q' + str(q) + '"></div>'
    for q in (1, 2)
)


def test_p1_6_sort_fallback_scopes_to_target_question(driver) -> None:
    """复现案例 sort_fallback_reorders_wrong_question：请求第 2 题排序 3,2,1。"""
    from src.interactions._scripts import fill_sort_script

    _page(driver, _SORT_PAGE)
    ok = driver.execute_script(fill_sort_script(2, ["3", "2", "1"]))
    state = driver.execute_script(
        "return {"
        "q1:Array.from(document.querySelectorAll('#div1 li')).map(x=>x.getAttribute('value')),"
        "q2:Array.from(document.querySelectorAll('#div2 li')).map(x=>x.getAttribute('value')),"
        "q2stored:document.querySelector('input[name=q2]').value,"
        "q1stored:document.querySelector('input[name=q1]').value}"
    )
    assert ok is True
    assert state["q1"] == ["1", "2", "3"], "第 1 题的 DOM 不许被顺手动过"
    assert state["q1stored"] in ("", None, "1,2,3"), "第 1 题的提交值不许被改写"
    assert state["q2"] == ["3", "2", "1"]
    assert state["q2stored"] == "3,2,1", "隐藏值必须与重排结果一致"


def test_p1_6_sort_fallback_fails_when_question_scope_is_missing(driver) -> None:
    """题号锚定不到任何容器 → 失败（修复前会爬到父节点抓别的题的列表）。"""
    from src.interactions._scripts import fill_sort_script

    orphan = (
        '<ul class="lisort">'
        + "".join('<li value="' + str(i) + '">' + str(i) + "</li>" for i in (1, 2, 3))
        + '</ul><input type="hidden" name="q9">'
    )
    _page(driver, orphan)
    assert driver.execute_script(fill_sort_script(9, ["3", "2", "1"])) is False


# ---------------------------------------------------------------------------
# P2-9 矩阵题缺行必须报失败
# ---------------------------------------------------------------------------
def test_p2_9_matrix_partial_fill_is_a_failure(driver) -> None:
    """复现案例 matrix_partial_fill_reports_success：请求第 1、2 行，页面只有第 1 行。"""
    from src.interactions._scripts import fill_matrix_single_script

    _page(driver, '<div id="div1"><input type="radio" name="q1_1" value="1"></div>')
    ok = driver.execute_script(fill_matrix_single_script(1, {1: 1, 2: 1}))
    assert ok is False, "缺行的矩阵题必须整体报失败"


def test_p2_9_matrix_full_fill_is_a_success(driver) -> None:
    """对照：两行都存在且都命中 → True（逐行核验不冤枉完整作答）。"""
    from src.interactions._scripts import fill_matrix_single_script

    _page(
        driver,
        '<div id="div1">'
        '<input type="radio" name="q1_1" value="1">'
        '<input type="radio" name="q1_2" value="1"></div>',
    )
    assert driver.execute_script(fill_matrix_single_script(1, {1: 1, 2: 1})) is True


# ---------------------------------------------------------------------------
# P2-10 普通提示弹窗 ≠ 验证码
# ---------------------------------------------------------------------------
def test_p2_10_plain_dialog_is_not_captcha(driver) -> None:
    """复现案例 generic_dialog_false_captcha：纯提示文本的可见 layui 弹窗。"""
    from src.verification import is_smart_verification_showing

    _page(driver, '<div class="layui-layer" style="width:300px;height:100px">'
                  "普通提示：请认真填写问卷</div>")
    assert not is_smart_verification_showing(driver), (
        "弹窗壳（layui-layer）不再构成验证码证据"
    )


def test_p2_10_real_captcha_widget_is_still_detected(driver) -> None:
    """对照：真实验证组件（可见 captcha 容器）必须仍被认出 —— 防止一刀切漏报。"""
    from src.verification import is_smart_verification_showing

    _page(driver, '<div class="captcha" style="width:300px;height:80px">滑块</div>')
    assert is_smart_verification_showing(driver) is True
