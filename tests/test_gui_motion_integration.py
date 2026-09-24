"""动效接入点的离线契约（设计稿 §7C 组）。

这里钉的是**可观察结果**，不是动画过程：状态字溶解完必须正好等于目标文案、
计数与进度补间完必须正好等于目标读数、空闲下来之后不能再留任何定时器作业。
不断言像素、不依赖真实帧率、也不需要 ``mainloop`` —— 把 ``app._ticker`` 换成
带假时间轴的 ticker 就能逐毫秒推进（`schedule` 是注入参数，这正是设计稿 §3
坚持要的那件事）。

窗口只在整模块构造一次，挂在会话级根窗口的 Toplevel 上，构造手法与
``tests/test_gui_user_data.py`` 的 ``gui`` 夹具一致（一个进程只能可靠地建一个
Tk 根窗口，见 ``conftest.py`` 的 WHY）。
"""

from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")

from gui import app as app_module  # noqa: E402
from gui import motion, widgets  # noqa: E402
from gui.app import SurveyGUI  # noqa: E402
from gui.ticker import MotionTicker  # noqa: E402
from src import dialogs  # noqa: E402


class FakeLoop:
    """假时间轴：记录排了多少作业，`advance` 才让作业兑现。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.pending: list[list] = []
        self.scheduled = 0
        self.logs: list[str] = []

    def schedule(self, ms, fn):
        self.scheduled += 1
        self.pending.append([self.now + ms, fn])

    def clock(self):
        return self.now

    def advance(self, ms):
        target = self.now + ms
        for _ in range(10_000):
            due = [job for job in self.pending if job[0] <= target]
            if not due:
                break
            due.sort(key=lambda job: job[0])
            job = due[0]
            self.pending.remove(job)
            self.now = max(self.now, job[0])
            job[1]()
        self.now = target

    @property
    def idle(self) -> bool:
        return not self.pending


@pytest.fixture(scope="module")
def gui(tk_root):
    """构造一次真 SurveyGUI，数据树指到 tmp；建不出来直接判失败，不静默跳过。"""
    tmp = tempfile.mkdtemp(prefix="wjx_motion_")
    prev_env = os.environ.get(app_module.USER_DATA_DIR_ENV)
    os.environ[app_module.USER_DATA_DIR_ENV] = tmp
    app: SurveyGUI | None = None
    real_tk_ctor = tk.Tk
    tk.Tk = lambda *_a, **_k: tk.Toplevel(tk_root)  # type: ignore[assignment]
    try:
        try:
            app = SurveyGUI()
        except Exception as exc:
            # 刻意不像 test_gui_user_data 那样把构造失败咽成 skip：那次"第二个实例
            # 建不出来"被咽掉之后整模块静默跳过，比红难发现。无显示 runner 由
            # tk_root 先 skip，走到这里还建不出来就是真故障。
            pytest.fail(
                f"SurveyGUI 构造失败（不该被跳过）: {type(exc).__name__}: {exc}")
        app.root.withdraw()
        yield app
    finally:
        tk.Tk = real_tk_ctor
        if app is not None:
            # 先停心跳：补间闭包攥着 StringVar，窗口销毁后再被 GC 就是
            # "main thread is not in main loop" 的 unraisable 警告
            if getattr(app, "_ticker", None) is not None:
                app._ticker.shutdown()
            after_info = getattr(app.root, "after_info", None)
            if after_info is not None:  # Tk.after_info 是 3.11 才进 tkinter 的
                for job in after_info():
                    app.root.after_cancel(job)
            app.running = False
            try:
                app.root.destroy()
            except Exception:  # pragma: no cover - 根窗口随进程回收
                pass
            dialogs.register_popup_handler(None)
            dialogs.register_file_picker(None)
        if prev_env is None:
            os.environ.pop(app_module.USER_DATA_DIR_ENV, None)
        else:
            os.environ[app_module.USER_DATA_DIR_ENV] = prev_env
        shutil.rmtree(tmp, ignore_errors=True)


def _collapsible_canvas(w):
    """找到那张可折叠卡片的画布（``attach_collapse`` 会在它上面挂 ``_card_toggle``）。"""
    for child in w.winfo_children():
        if isinstance(child, tk.Canvas) and hasattr(child, "_card_toggle"):
            return child
        got = _collapsible_canvas(child)
        if got is not None:
            return got
    return None


@pytest.fixture()
def stage(gui):
    """把 ticker 换成假时间轴驱动，并把读数清回起点（模块级窗口要防串味）。"""
    loop = FakeLoop()
    app = gui
    if app._ticker is not None:
        app._ticker.shutdown()          # 真 ticker 不能留着，它会往真 after 队列里排作业
    app._ticker = MotionTicker(schedule=loop.schedule, clock=loop.clock,
                               log_fn=loop.logs.append)
    app.success_count = 0
    app.fail_count = 0
    app.current_round = 0
    app.total_rounds = 0
    app._status_target = "就绪"
    app._status_text = "就绪"
    app._progress_value = 0.0
    app._pct_value = 0.0
    app._scan_phase = 0.0
    app._breath_phase = math.pi / 2
    app._window_active = True
    app._ticker.shutdown()
    app._ticker = MotionTicker(schedule=loop.schedule, clock=loop.clock,
                               log_fn=loop.logs.append)
    yield app, loop
    app._ticker.shutdown()


# ---------------------------------------------------------------- 状态字


def test_status_dissolves_onto_the_exact_target(stage):
    app, loop = stage
    app._set_status("运行中...", "#0066cc")
    assert app._status_text == "就绪"      # 登记不等于已经画上去
    loop.advance(1000)
    assert app._status_text == "运行中..."  # 停在乱码中间态就是回归
    assert app._status_target == "运行中..."


def test_two_status_changes_in_a_row_end_on_the_later_one(stage):
    app, loop = stage
    app._set_status("运行中...", "#0066cc")
    loop.advance(60)                        # 溶解到一半
    app._set_status("正在停止...", "#cc6600")
    loop.advance(1000)
    assert app._status_text == "正在停止..."


def test_back_to_idle_stops_the_breathing_and_parks_it_bright(stage):
    app, loop = stage
    app._set_status("运行中...", "#0066cc")
    loop.advance(500)
    assert app._ticker.has("breath")
    app._set_status("就绪", "#5a6b85")
    loop.advance(1000)
    assert not app._ticker.has("breath")
    assert app._breath_phase == pytest.approx(math.pi / 2)
    assert app._status_text == "就绪"


# ---------------------------------------------------------------- 读数


def test_counters_and_bar_land_on_exact_final_values(stage):
    app, loop = stage
    app.total_rounds = 4
    app.current_round = 4
    app.success_count = 3
    app.fail_count = 1
    app._update_progress()
    loop.advance(1000)
    assert app.success_var.get() == "3"
    assert app.fail_var.get() == "1"
    assert app.progress_pct_var.get() == "100.0%"
    assert app._progress_value == pytest.approx(1.0)
    assert app.progress_text_var.get() == "4 / 4"   # 比值读数不补间，直接给


def test_the_bar_overshoots_once_and_the_percentage_never_goes_backwards(stage):
    """进度条用带过冲的 spring（收尾那一点弹跳是设计），百分比用单调缓动。

    两者钉的是同一件事的两面：允许弹一下，但**必须**落在目标上，
    而读数那一侧连一帧回退都不许有。
    """
    app, loop = stage
    app.total_rounds = 10
    app.current_round = 0
    app._update_progress()
    app.current_round = 8
    app._update_progress()
    bars: list[float] = []
    pcts: list[float] = []
    for _ in range(40):
        loop.advance(30)
        bars.append(app._progress_value)
        pcts.append(app._pct_value)
    assert bars[-1] == pytest.approx(0.8)
    assert all(0.0 <= b <= 1.0 for b in bars)
    assert max(bars) > 0.8                      # 确实过冲了一下
    assert pcts == sorted(pcts)                 # 读数单调
    assert pcts[-1] == pytest.approx(0.8)


def test_a_second_batch_does_not_send_the_counter_back_to_zero(stage):
    app, loop = stage
    app.total_rounds = 5
    app.success_count = 1
    app.current_round = 1
    app._update_progress()
    loop.advance(30)
    low = int(app.success_var.get())
    app.success_count = 4
    app.current_round = 4
    app._update_progress()
    loop.advance(1000)
    assert int(app.success_var.get()) == 4
    assert low <= 1


# ---------------------------------------------------------------- 日志触发


def test_a_new_log_line_kicks_the_scanline_which_then_stops_itself(stage):
    app, loop = stage
    app._log("第一行")
    app._drain_log_queue()
    assert app._ticker.has("scan")
    loop.advance(1000)
    assert app._scan_phase > 0.0
    assert not app._ticker.has("scan")          # 走完 6 帧自己退场


def test_no_log_line_means_the_scanline_never_moves(stage):
    app, loop = stage
    before = app._scan_phase
    app._drain_log_queue()
    loop.advance(2000)
    assert app._scan_phase == before
    assert loop.scheduled == 0


def test_cursor_only_blinks_while_the_window_is_active(stage):
    app, loop = stage
    app._log("一行日志")
    app._drain_log_queue()
    loop.advance(30)
    assert app._ticker.has("cursor")
    app._on_window_blur()
    assert not app._ticker.has("cursor")
    app._on_window_focus()
    assert app._ticker.has("cursor")


# ---------------------------------------------------------------- 空闲即停


def test_a_settled_window_is_left_with_only_the_cursor_heartbeat(stage):
    """忙过一轮回到就绪之后：补间全部退场，只剩一个 530ms 的光标心跳。

    这条是 G2 的真实判据 —— 改造前是 30/40/80ms 三个循环一起转，
    现在空闲态既没有补间、心跳也慢到跟终端光标同量级。
    """
    app, loop = stage
    app.total_rounds = 2
    app.current_round = 2
    app.success_count = 2
    app._set_status("运行中...", "#0066cc")
    app._update_progress()
    app._log("执行结束")
    app._drain_log_queue()
    app._set_status("就绪", "#5a6b85")
    loop.advance(3000)
    assert set(app._ticker._items) == {"cursor"}
    assert loop.logs == []                       # 没有任何原语被异常摘掉
    job = next(iter(app._ticker._items.values()))
    assert job.every_ms == motion.CURSOR_PERIOD_MS // 2  # 空闲心跳慢到跟光标同量级


def test_blurred_and_idle_leaves_no_timer_work_at_all(stage):
    app, loop = stage
    app._log("一行日志")
    app._drain_log_queue()
    loop.advance(3000)
    app._on_window_blur()                        # 失焦：光标也不该继续闪
    assert len(app._ticker) == 0
    settled = loop.scheduled
    loop.advance(5000)                           # 兑现那个已在途的作业
    assert loop.idle
    assert app._ticker.running is False
    assert loop.scheduled == settled             # 它没有把自己续排下去


# ---------------------------------------------------------------- 总开关


def test_switch_off_lands_every_reading_immediately(stage, monkeypatch):
    app, loop = stage
    monkeypatch.setenv("WJX_MOTION", "0")
    app.total_rounds = 4
    app.current_round = 4
    app.success_count = 3
    app.fail_count = 1
    app._set_status("已完成", "#0066cc")
    app._update_progress()
    assert app._status_text == "已完成"
    assert app.success_var.get() == "3"
    assert app.fail_var.get() == "1"
    assert app.progress_pct_var.get() == "100.0%"
    assert app._progress_value == pytest.approx(1.0)
    assert loop.scheduled == 0              # 关掉动效＝一个定时器都不许排


def test_switch_off_leaves_the_ambient_effects_untouched(stage, monkeypatch):
    app, loop = stage
    monkeypatch.setenv("WJX_MOTION", "0")
    app._log("一行日志")
    app._drain_log_queue()
    app._set_status("运行中...", "#0066cc")
    loop.advance(2000)
    assert app._status_text == "运行中..."
    assert app._scan_phase == 0.0           # 扫描线/呼吸灯/光标全都不动
    assert not app._ticker.has("breath")
    assert not app._ticker.has("cursor")
    assert loop.scheduled == 0


def test_settings_card_is_tall_enough_for_all_its_rows(gui):
    """卡片本体是一块固定 ``-height`` 的 Canvas：内容超出会被静默裁掉。

    上一轮把两个长文案复选框拆成两行之后，「配置文件」整行就是这么从界面上消失的 ——
    没有报错、没有滚动条，只是少了。所以这里钉住"卡高 >= 内容自然高"。
    """
    canvas = _collapsible_canvas(gui.root)
    assert canvas is not None, "没找到可折叠卡片的画布"
    outer = canvas.master                     # CardFrame
    body = outer._card_body                   # 挂在画布 window item 上的那一层
    body.update_idletasks()
    assert canvas.winfo_reqheight() >= body.winfo_reqheight()
    assert not outer.pack_info()["expand"]


def test_motion_layer_is_reachable_from_the_app_module():
    """接入点靠 gui.motion 的 token 取值，别让谁偷偷把时长写回字面量。"""
    assert motion.DUR_SLOW == 400
    assert app_module.ScrambleText is motion.ScrambleText


# ---------------------------------------------------------------- 折叠卡片


@pytest.fixture()
def card(tk_root):
    """一张真卡片，挂在屏幕外的 Toplevel 上（折叠要真实几何才测得出来）。"""
    top = tk.Toplevel(tk_root)
    top.geometry("+10000+10000")     # 别盖住开发机正在看的窗口
    loop = FakeLoop()
    ticker = MotionTicker(schedule=loop.schedule, clock=loop.clock,
                          log_fn=loop.logs.append)
    holder = tk.Frame(top)
    holder.pack()
    content = widgets.make_card(holder, "基本设置", icon="⚙",
                                collapsible=True, ticker=ticker)
    for i in range(20):
        tk.Label(content, text=f"第 {i} 行").pack(fill=tk.X)
    top.update()
    yield top, content, holder, loop, ticker
    ticker.shutdown()
    top.destroy()


def _settle(top, loop, ms=600):
    """推进假时间轴，同时让 Tk 真的重排一次。"""
    for _ in range(ms // 30):
        loop.advance(30)
        top.update_idletasks()
        top.update()


def test_card_collapses_to_its_header_and_back(card):
    top, content, holder, loop, ticker = card
    outer = holder.winfo_children()[0]
    full = outer.winfo_height()
    assert full > 200, "卡片没被 realize 出真实高度，这条用例测不到补间"

    content._card_toggle()
    _settle(top, loop)
    assert content.winfo_ismapped() == 0        # 内容真的收起来了
    assert outer.winfo_height() < full / 2      # 只剩标题条
    assert not ticker.has(("card", id(outer)))  # 补间走完自己退场

    content._card_toggle()
    _settle(top, loop)
    assert content.winfo_ismapped() == 1
    assert outer.winfo_height() > full * 0.8    # 展开回到接近原高
    assert loop.logs == []


def test_collapsed_card_gives_its_share_of_the_page_to_the_sibling(tk_root):
    """两张卡都是 ``expand=True`` 在平分页面 —— 折叠必须同时让出 expand。

    只把画布高度缩到标题条是不够的：开发机上实测那样卷起来只剩一个空白框，
    下面的权重表一寸地方都没多出来，G4 就是空的。
    """
    top = tk.Toplevel(tk_root)
    top.geometry("700x620+10000+10000")
    loop = FakeLoop()
    ticker = MotionTicker(schedule=loop.schedule, clock=loop.clock,
                          log_fn=loop.logs.append)
    content = widgets.make_card(top, "基本设置", collapsible=True, ticker=ticker)
    for i in range(8):
        tk.Label(content, text=f"第 {i} 行").pack(fill=tk.X)
    other = widgets.make_card(top, "权重配置")
    tk.Label(other, text="表").pack()
    top.update()

    outer_a = content.master.master.master
    outer_b = other.master.master.master
    a_before, b_before = outer_a.winfo_height(), outer_b.winfo_height()
    assert a_before > 100 and b_before > 100, "两张卡没被 realize 出真实高度"

    content._card_toggle()
    _settle(top, loop)
    assert outer_a.winfo_height() < a_before / 2
    assert outer_b.winfo_height() > b_before      # 空出来的地方真的给了隔壁

    content._card_toggle()
    _settle(top, loop)
    assert outer_a.winfo_height() > a_before / 2
    assert loop.logs == []
    ticker.shutdown()
    top.destroy()


def test_card_without_a_ticker_still_collapses(card, tk_root):
    """没给 ticker（或关掉动效）时，折叠必须照样能用 —— 状态是权威的，补间是装饰。"""
    top = tk.Toplevel(tk_root)
    top.geometry("+10000+10000")
    holder = tk.Frame(top)
    holder.pack()
    content = widgets.make_card(holder, "无心跳卡片", collapsible=True)
    tk.Label(content, text="一行").pack()
    top.update()
    outer = holder.winfo_children()[0]
    full = outer.winfo_height()
    content._card_toggle()
    top.update()
    assert content.winfo_ismapped() == 0
    assert outer.winfo_height() < full
    top.destroy()
