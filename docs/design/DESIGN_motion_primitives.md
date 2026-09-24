# GUI 动效层（motion primitives）—— 设计稿，不含实现

> 状态：**设计已逐节确认（2026-09-24），未开始实现**。
> 对标来源：第七个对标源 [`ibelick/motion-primitives`](https://github.com/ibelick/motion-primitives)
> （MIT，33 个 `components/core/*.tsx` 动效原语，React + [motion](https://motion.dev/) + Tailwind CSS）。
> **只借设计语言，不借代码** —— 它是网页组件库，本项目是 Tkinter 桌面端，没有可移植的实现。
> 与本仓库其它设计稿的差别：这份不解决算法问题，解决的是"用户摸得到的那一层测不了"。

---

## 0. 先说清这件事的性质

`gui/` 约占生产代码的 41%，是用户唯一真正摸得到的表面，而它的可测试性一直是负的：
`tests/test_gui_panels.py:31-33` 明写"**刻意不构造 `SurveyGUI`**：它的 `__init__` 会打开历史库并
启动动画 after 循环"。也就是说现有动效不是"没测"，是**结构上测不了** —— 动效逻辑和 Tk 主循环绑死。

所以这次改造的真正产出不是"更好看"，而是把动效抽成一层**纯时钟驱动、可离线钉死**的原语。
观感提升是顺带的。

## 1. 目标 / 非目标

**目标**

- G1：动效逻辑与 Tk 解耦，`gui/motion.py` 能在不起 `mainloop`、不建控件的前提下逐帧断言。
- G2：事件驱动 —— 空闲时**一个 `after` 作业都不留**；把现有三个常驻循环收进可停摆的单一 ticker。
- G3：一处开关能整体关掉动效，关掉后界面立刻是最终态，功能一项不少。
- G4：动效至少解决一个真实布局问题（折叠卡片缓解左栏纵向压力），不是纯装饰。
- G5：黑白极简主题与"所有文字对比度满足 WCAG AA"这条现状**不动**。

**非目标**

- 不做暗色皮肤、不加第二套配色（`theme.py` 的 `COLORS` 不扩项，动效只吃时长与缓动）。
- 不做 §6 那张"Tk 做不出来"清单上的任何东西。
- 不引入任何新依赖（纯 stdlib + Tk 8.6）。
- 不改 `src/` 的任何行为；GUI 的自动化语义一行不动。

## 2. 三个岔路口（决策记录）

| 岔路 | 选定 | 否掉的选项与原因 |
|---|---|---|
| 改造层级 | **Tkinter 内新增动效原语层** | 借分层令牌（观感提升有限）；换 Web 前端（等于重做 GUI，`gui/` 全部测试契约作废，赛前崩台风险不成比例） |
| 动效定位 | **事件驱动为主** | 常驻氛围（窗口开着就持续重绘）；纯功能性（要删掉现有扫描线/呼吸灯，离参考观感最远） |
| 配色走向 | **保持黑白极简** | 加暗色皮肤（两套配色都要过对比度、写死 `COLORS` 的卡片全要改读令牌）；整体转暗色（`theme.py:22-23` 那句 WCAG 立身说明作废） |

## 3. 模块边界

```
gui/motion.py    纯时钟层：原语状态机 + motion token + enabled()      ← 不 import Tk 控件，不调 after
gui/ticker.py    唯一的 after 循环：登记 / 分发 dt / 执行 apply 回调  ← schedule 是注入参数
gui/app.py       接入点：_set_status / _update_progress / _redraw_progress / _tick_*
gui/widgets.py   make_card(collapsible=...)
gui/weight_panel.py · gui/history_panel.py   错峰入场接入
```

**`gui/motion.py`** —— 每个原语是一个只吃 `dt_ms` 的小状态机：`step(dt_ms) -> 显示值`，
外加 `done` 标志。它不知道 Tk 的存在，赋值由调用方做。与 `theme.py::_lerp_color` 同一层纪律。

Motion token（集中一处，别再散进 2900 行 `app.py`）：

| token | 值 | 用途 |
|---|---|---|
| `DUR_FAST` | 120ms | 折叠箭头、hover 态 |
| `DUR_BASE` | 220ms | 数字滚动、进度追赶 |
| `DUR_SLOW` | 400ms | 状态字溶解 |
| `STAGGER` | 18ms | 逐行入场步长 |
| `STAGGER_MAX` | 24 项 | 超过直接给终态 |
| `ease_out_cubic` | `1-(1-t)^3` | 默认缓动 |
| `spring_damped` | 阻尼近似 | 进度条收尾的一点过冲 |

**`gui/ticker.py`** —— `MotionTicker(schedule, log_fn)`。`schedule` 生产传 `root.after`、
测试传手动队列。空登记即停摆。

**开关** —— `WJX_MOTION=0` 环境变量 + `motion.enabled()`。关掉时 ticker 不启动、
所有 `step()` 第一次就返回终值。**读取时机要说清**：ticker 启动时读一次（决定要不要起循环），
每个原语 `step()` 内再各读一次（决定要不要直接给终值）—— 后者是为了测试能 monkeypatch 掉
而不用重开进程。**不进配置文件、不加 GUI 复选框**：`_build_browser_row`
刚刚才因为文案溢出被拆成两行，不该为这个再挤一行回去。

**顺带清掉** `app.py:204` 的 `self._stars`（星空粒子留下的死状态，主题换成黑白后没人读它），
以及 `app.py:1-10` 那段"深色极光渐变 + 动态星空粒子 + 毛玻璃"的模块 docstring 漂移。

## 4. 六个原语与接入点

| 原语（参考名） | 接口 | 接入点 | 触发 |
|---|---|---|---|
| `NumberTween`（animated-number / sliding-number） | `NumberTween(frm, to, dur)` → `step(dt)->float` | `app.py:1487-1491` `_update_progress`，成功/失败计数与 `pct` 的唯一写点 | 每份答卷成败计数变化 |
| `ScrambleText`（text-scramble） | `ScrambleText(old, new, rng)` → `step(dt)->str` | `app.py:511` `_set_status`（文字画在 Canvas x=70，见 `app.py:505-509`） | 就绪→探测中→运行中→已完成/已停止 |
| `stagger`（animated-group） | `stagger(n, step, cap)` → 每项延时 | `weight_panel.populate`（`weight_panel.py:337` 现在只有一处局部 `after(10,_draw_badge)`）+ 历史 runs 表刷新 | 探测成功、刷新历史 |
| `Collapse`（accordion / disclosure） | `Collapse(h_from, h_to, dur)` → 高度补间 + 箭头字符 | `widgets.make_card(collapsible=True)`（header 见 `widgets.py:98-100`） | 点「基本设置」「权重配置」标题条 |
| `EasedProgress`（scroll-progress） | 目标值 + 220ms 追赶 | `app.py:1493` `_redraw_progress` | 每轮进度跳变 |
| `TextLoop`（text-loop） | `TextLoop(phrases, period=900)` | 探测与停止两个会把人卡住的等待点 | 仅等待态存在，拿到结果立刻停 |

三条判断：

1. **`Collapse` 是这轮唯一顺手解决真实问题的原语。** 左栏 notebook 只有 ~540px 宽、纵向更紧，
   权重表一长「基本设置」就被顶下去。代价要说清：`make_card` 的返回契约要分成
   "可折叠返回 content frame / 不可折叠返回 body"，而 `_make_card` 有三个代理调用方
   （`log_view` / `history_panel` / `weight_panel`，都通过 `make_card_fn=self._make_card` 注入），
   实现时必须逐个确认没被这次契约变更打到。
2. **`stagger` 必须设上限。** 长跑 9999 份时历史表不能一行行等动画，超 `STAGGER_MAX` 直接终态。
3. **`TextLoop` 的短语必须对应真实阶段，不能编。** `gui/controller.py` 探测路径里已有
   建 driver / 等题目渲染 / `detect_questions` 三段（见 `controller.py:370-383`），
   停止路径有 `_on_close` 的 `join(timeout=30)` 一段 —— 只轮播这四条真话。

## 5. 事件驱动收口：现有三个循环怎么办

| 现状（`app.py:288-323`） | 改成 |
|---|---|
| `_tick_breath` 每 80ms 常驻重绘状态光晕 | 只在状态 ≠ 就绪时登记，回到就绪即停 |
| `_tick_scanline` 每 40ms 常驻推进 | 每条新日志行触发 6 帧（240ms）推进，走完即停 |
| `_tick_cursor` 每 530ms 常驻闪 | 只在窗口聚焦且日志非空时闪 |

三个方法名与 `log_view` 的接口（`scan_phase` / `cursor_blink` / `redraw_scanline`）都保留，
只把"自己排下一次 after"换成"向 ticker 登记"。
异常处理从 `except Exception: pass` 改成"摘掉该原语 + 记一条 WARN" ——
动效静默死掉没人知道，是现在这三个 `pass` 的真实代价。

## 6. 显式不做清单

`spotlight`、`magnetic`、`tilt`、`progressive-blur`、`dock`、`morphing-dialog`、
`morphing-popover`、`cursor`、`carousel`、`image-comparison`、`spinning-text`。

原因统一：Tk 没有合成层、3D 变换与模糊，控件也不能自由定位。要做得把整块 UI 改成
Canvas 自绘 —— 那是"重写 `_make_card` + 动到全部卡片与既有 gui 测试契约"的量级，
与这轮的收益不成比例。

## 7. 测试口径

**A. `gui/motion.py` 纯时钟层，完全不碰 Tk**（`tests/test_gui_motion.py`）

- 端点：t=0 给起点，t≥dur 给终点且 `done` 置位；`NumberTween` 必须**精确**落在整数终值，
  不能停在 0.9999。
- 单调：计数中途不回退（长跑时数字倒着走是最刺眼的假 bug）。
- 目标值中途被改（连点两下）不跳回 0。
- `stagger` 超 `STAGGER_MAX` 直接终态。
- 开关关掉时第一次 `step()` 就返回终值。
- 确定性：`ScrambleText` 收 `rng` 参数（默认 `random.Random()`），测试注入固定种子；
  乱码字符集取"新文案自身的字符 + 固定一小撮符号"，不引入随机源差异。
- 目标覆盖 **100%**（纯函数没有不可达分支）。

**B. `gui/ticker.py`，`schedule` 是注入参数**

- 登记为空时**一个 `after` 作业都不留**（G2 的验收判据）。
- `dt` 分发不丢帧。
- 某个回调抛异常只摘掉它自己并记 WARN，整条循环不死。

**C. 接入点只钉可观察结果，不钉动画过程**

走 `conftest.py::tk_root`（全会话唯一根窗口、withdraw、不 `mainloop`）+ `Toplevel`。
钉的是：`_set_status("运行中")` 跑完 tick 后 Canvas 上的文字**必须等于**"运行中"
（停在乱码中间态就是回归）、`_update_progress` 后三个 StringVar 的终值、
折叠到位后 content 的 `winfo_ismapped()` 为 `False` 而 header 仍为 `True`、再展开后两者都 `True`。
**不断言像素、不断言"看起来在动"、不依赖真实帧率** —— 无显示 runner 上必然 flaky。

**D. 覆盖率与门禁**

- 地板是 `.github/workflows/ci.yml:58` 的 `--cov-fail-under=70`，只许往上走。
- README 缺口表由 `python scripts/readme_coverage.py --write` 从 `coverage.json` 生成，
  `--check` 已在 CI 当门禁，手改数字下次推送就红。
- **口径警告**：该脚本只挡 `opencv-python` 这类可选依赖，**不查 Python 版本**；
  实测只有 3.13 + 只装 `requirements*` 能复现 README 现有数字，本机 `.venv310` 跑
  `--check` 一定漂红。`--write` 必须在 CI 等价环境里跑，不能拿本机结果落盘。

**E. 人工验收（离线测不了的那一半）**

PrintWindow 离屏抓图 + 像素探针，改前/改后各一轮：状态字四态、进度条中段、
折叠前后、错峰入场第 3 帧。配方见附录。

## 8. 落地顺序

1. `gui/motion.py` + `tests/test_gui_motion.py`（A 组）—— 独立可交，不接任何控件。
2. `gui/ticker.py` + B 组测试 —— 仍然独立可交。
3. 接入 `_set_status` / `_update_progress` / `_redraw_progress` + 三循环收口（§5）+ C 组测试。
4. `make_card(collapsible=)` + 三个代理调用方回归确认 + 错峰入场。
5. `WJX_MOTION` 开关与文档（README「GUI 使用」一节加一行）。
6. CI 等价环境跑 `--write` 重生成缺口表。

第 1、2 步合起来就是"一层能被测试关死的纯函数"，第 3 步之后才碰用户看得见的东西。
任何一步想停下来，前一步都是完整可用的。

## 附录：离屏抓图配方

`PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)` 能在窗口被其它程序遮挡时抓到它自己那块
表面，因此不需要抢前台焦点（实测 `SetForegroundWindow` / `SwitchToThisWindow` 从后台进程
都抢不动，抢到的截图是 IDE 或浏览器）。要点：PowerShell 里先 `SetProcessDPIAware()`，
否则 `GetWindowRect` 给的是虚拟化坐标、像素探针会读错位置。

## 偏离说明

- 本稿放在 `docs/design/`（仓库既有约定，见 `DESIGN_reliability_alpha.md`），
  不在 skill 默认的 `docs/superpowers/specs/`。
- 对标仓库的"组件"是 React 组件，本稿的"原语"是 Tkinter 里的纯函数状态机 ——
  同名不同物，只共享时序与分层观念。参考项目的暗色霓虹气质明确不跟（见 §2）。

### 实现期偏离（2026-09-24 落地时踩到并改掉的，按本稿原方案实现会翻车）

1. **只有「基本设置」可折叠**，不是 §4 说的两张卡。权重表那张是 `expand=True` 的
   受让方，把它也做成可折叠会留下一个没人要的空页；折叠的意义就是给它腾地方。
2. **折叠改的是 `card_canvas` 的 `-height` 加 `outer` 的 `expand`，不是 `pack_propagate`。**
   卡片的实际高度由"同页两张卡 `expand=True` 平分"决定，只缩高度选项整张卡纹丝不动 ——
   开发机上实测卷起来只剩一个空白框。现在两处一起改，并断言隔壁卡片确实拿到了空间。
3. **默认时钟必须是毫秒。** `time.monotonic()` 返回秒，照 §3 的字面写法接上去，
   每个补间慢一千倍、表现成"动画永远不动"，而注入假毫秒时钟的 B 组用例全绿。
   现在 `ticker.monotonic_ms()` 直接量墙钟钉住单位。
4. **`pack()` 之后要 `update_idletasks()` 才能读到新的 `winfo_reqheight()`**，
   立刻量拿到的是折叠态的 50，展开就永远停在 50。
5. §7C 说的"折叠后 `winfo_ismapped()` 为 False"补了一条更硬的：折叠后**隔壁卡片变高**。
   前者只证明内容收起来了，后者才证明 G4 成立。
6. 实现期还捎带修了一个既有缺陷：同进程构造第二个 `SurveyGUI` 会
   `TclError: Duplicate element aurora_tab`，而 `tests/test_gui_user_data.py` 把它
   `except Exception: pytest.skip` 咽成了整模块静默跳过 —— 新测试文件改成构造失败
   直接判失败，元素名冲突则复用。
7. **折叠逼出了一条本稿没写的前提**：卡片本体是一块固定 `-height` 的 Canvas，内容超出
   是**静默裁掉**而不是把框撑大。上一轮把两个长文案复选框各拆成一行之后，「配置文件」
   整行就是这么没的。所以加了 `widgets.fit_card()`：让设置卡按内容自然高度收形、
   不再与权重表 `expand=True` 平分页面。§4 判断 1 说的"折叠缓解纵向压力"，
   实际是靠这条才成立的 —— 光有折叠，被裁掉的那几行也回不来。


