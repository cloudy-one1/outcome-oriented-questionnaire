# Web GUI（`webui/`）—— 设计稿，不含实现

> 状态：**设计已逐节确认（2026-09-24 至 09-25），未开始实现**。
> 第八个对标源 [`DavidHDev/react-bits`](https://github.com/DavidHDev/react-bits)
> （208 个动画组件：Backgrounds 57 / Components 45 / Animations 39 / Micro 34 / TextAnimations 33，
> 48k stars，**License 是 MIT + Commons Clause，不是纯 MIT**）。
> 与第七个对标源 `motion-primitives` 同一处理：**只借观念与观感，不取代码** ——
> Commons Clause 限制把软件本身拿去卖/做成服务，与本仓库 MIT 声明并排会留话柄；
> 而且它是 React + DOM/CSS/WebGL，本仓库是 Python。
> 本稿是 `DESIGN_motion_primitives.md` 的后续，不是替代：Tk 那一层原样留着。

---

## 0. 先说清这件事的性质

`gui/` 是 4913 行 11 个文件，配套 GUI 测试 3381 行 7 个文件。这次不是"给它换皮"，
而是**新增一个平行的宿主**：`src/` 用 Selenium 对问卷星作答，`gui/` 只是控制面板 ——
已核实 **`src/` 零 `tkinter` import**（`src/dialogs.py` 里那句 tkinter 只出现在 docstring 的
WHY 中），也没有一处 `import gui`。弹窗与文件选择早就收敛成可注入的出口。
所以换宿主不动引擎，作答链路的风险为零，E2E（`-m integration` + `scripts/e2e_gate.py`）继续守着它。

真正要设计的是**控制与生命周期**这一层 —— 那里有三个 Tk 不存在的新故障模式（§7）。

## 1. 目标 / 非目标

**目标**

- G1：`webui/` 覆盖 Tk 现有全部用户表面（§4 的 parity 清单），一条不少。
- G2：`src/` 与那 1200 项非 GUI 测试**一行不改**。
- G3：零新运行时依赖 —— 只用 stdlib（`http.server` + SSE），不碰 `requirements.txt` /
  `pyproject` 依赖表 / 打包门禁。
- G4：后端全部可离线测（纯 Python，不需要浏览器、不需要真 Selenium），
  UI 编排代码的覆盖率第一次高于 Tk。
- G5：长跑对前端存活**零依赖** —— 关标签、刷新、前端崩了，批次照常跑完。
- G6：react-bits 的观感在真浏览器里 1:1 落地，不再受 Tk 能力边界限制。

**非目标**

- 不删 `gui/`（推到赛后，§11）。本轮结束时两个入口并存，Tk 是参照实现。
- 不做 pywebview 桌面壳、不做多用户/远程访问（只 `127.0.0.1`）。
- 不改任何自动化语义：抽样、提交、验证码判定、续传判定、隐私默认值、URL 截断 500。
- 不搬 §12 那张清单里的任何东西。

## 2. 三个岔路口（决策记录）

| 岔路 | 选定 | 否掉的与原因 |
|---|---|---|
| 技术形态 | **本地 HTTP 服务 + 默认浏览器** | pywebview 桌面壳（引入硬依赖、WebView2 Runtime 缺失即起不来、Docker 里仍用不上）；两者都做（两套壳都要测） |
| Tk 去留 | **本轮不删，作为参照实现并存** | 一轮内替换（中途无可用 GUI、刚建好的动效层白扔、赛前崩台风险最高）；Tk 降级成无头宿主（拆出没人认领的东西） |
| 现状缺陷 | **只修输入校验 / 安全边界 / 日志可见性三类** | 八条全修（行为变更要改文档、且与既有 GUI 测试契约冲突）；逐条复刻含 bug（把"点了没反应""一键删库无确认"搬进新界面） |

## 3. 分层与接缝

```
webui/session.py   RunSession：表单值 / 运行态 / 计数 / 日志环形缓冲 / RunState
webui/service.py   命令层，由 gui/controller.py 搬过来（它本来就不是视图代码）
webui/api.py       路由 → service/session，JSON in/out，全部校验在这一层落地
webui/server.py    ThreadingHTTPServer + SSE，只 bind 127.0.0.1
webui/static/      index.html · app.js · styles.css      ← 观感落这里
```

- **搬而不是重写**：`gui/controller.py`（432 行，7 个命令）只依赖 host 的几个回调，
  改成 service 层即可。它现在只有 37% 覆盖，搬过来顺手补到高位。
- 对接点原样复用：`src.cli.run_batch`、`src.models.RunState`、`src.history.SubmissionHistory`、
  `src.config_io`、`src/qr_utils.py` 的解码函数（**它刻意不 import tkinter，可直接复用**）、
  `src.dialogs.register_popup_handler` / `register_file_picker`。
- `qr_utils` 从 `gui/` 移到 `webui/` 或 `src/`？—— 本轮**不移**（§10 的原计划），
  `webui` 先直接 import `gui.qr_utils`。**2026-09-26 已移到 `src/qr_utils.py`**：
  它是第 7 步的第一件事 —— Tk 与 webui 都吃它，留在 `gui/` 里就等于"删宿主会连带删掉
  一个宿主无关的模块"。零 tkinter 依赖没变，失败路径的契约测试一起从
  `tests/test_gui_panels.py` 拆成 `tests/test_qr_utils.py`，好让它不随桌面版退役。

## 4. parity 清单（Tk 全部表面 → webui 对应）

| Tk 表面 | 位置 | webui 落点 |
|---|---|---|
| URL / 份数 / 浏览器 / UC / 不记录填空 五个字段 | `app:723,766,825,851,865` | `GET /api/state` + `POST /api/field` |
| 探测题目（含 iframe 兜底、25s/15s 超时只 debug 不报错） | `ctl:302-403` | `POST /api/detect` —— **对拍已钉**：同一台假 driver 喂两边，比回流表、预填串、总结两句、`quit` 次数与命中的帧；顺带抓到"先导入配置再探测会把配置丢掉"那条真缺陷 |
| 扫码导入（opencv 缺失时**仍开选择框**、选完才提示缺依赖） | `ctl:274-296`、`qr:21-27` | `POST /api/qr`（上传图，保留同一降级顺序）—— **对拍已钉**：模块整体缺失时两边都在开框前拒绝；解出/解不出同一句话、都不动原有 URL。刻意不同的一处：webui 还过一遍 URL 校验（§5 校验类） |
| 导入 / 导出 / 另存默认配置 + `config_io` 缺失时三按钮禁用 | `ctl:148-257`、`app:900-908` | `/api/config/*` + `state.config_io_available` —— **对拍已钉**（同一张表导出的 JSON 除宿主标签外逐键相等；交叉导入同一份 `WEIGHT_CONFIG`；缺依赖三句措辞相同） |
| 启动静默自动加载默认配置 | `app:270`、`ctl:259-268` | 服务启动时同一条路径 —— **对拍已钉**（分布、表格文本、两句日志逐字相同） |
| 权重表 6 类题型的解析与预填、留空语义、矩阵 `1:w,w \| 2:w,w`、sort 作废规则 | `wp:232-595` | `POST /api/weights`，**解析函数直接复用 `weight_panel.build_weight_config` 的逻辑**（见 §5 末） |
| 反向回填（未探测时按 cfg 反构最小 questions） | `wp:602-667` | **2026-09-26 才真的成立**：规则抽进 `src.weight_text.reconstruct_questions`，两个宿主共用同一份并有对拍（此前 webui 只同步文本、不建行，"已恢复到表格"那句是空的） |
| 开始 / 停止、`running` 幂等、按钮禁用 | `app:1420-1507` | `POST /api/run` `/api/stop` |
| 进度与成功/失败/未知计数、`displayed_round` | `app:1300-1318,1605-1611` | SSE `progress` 事件 |
| 断点续传确认（答"否"仍保留权重恢复） | `app:1347-1418` | SSE `confirm` 事件 → 前端弹 → `POST /api/confirm` —— **对拍已钉两个方向**（答"是"= 第 K+1 份起、`attempts_cap = planned - done`；答"否"= 从第 1 份但权重照恢复），各配一条绝对值断言 |
| 孤儿批次收尾（>60min `running` 改判 failed） | `app:272-289` | 服务启动时同一步 —— **对拍已钉**（同一份"90 分钟前的 running"，改判后的 `status` 与那句 WARN 逐字相同） |
| 日志：8 种 tag、行号、`HH:MM:SS`、`❯` 前缀、吸底、`N lines` | `lv:28-37,191-209` | SSE `log` 事件 + 前端渲染 —— **对拍已钉**（轮次结果→级别的两份表逐键比、行号连续且 4 位右对齐、时间戳形状、不认识的 tag 都不丢字） |
| 历史 runs 8 列 / 明细 6 列 / 200 条上限 | `hp:184-234,256` | `GET /api/history/*` |
| 导出 CSV = **两个文件**、`_answers` 后缀、`utf-8-sig`、公式注入前缀拦截 | `hp:348-402`、`hp:38-50` | `/api/history/export` 打包两个下载 |
| 清理 7 天前（Tk 里**无确认**） | `hp:408-417` | `POST /api/history/purge` + 一次性 confirm token（§5 安全类） |
| `src.history` 未加载时常驻黄条、刷新/选行静默无反馈 | `hp:151-155,249-252` | `state.history_available` + 前端黄条 + **给"无反馈"补一条提示**（属日志可见性类） |

**这 16 行的处置（2026-09-26，第 7 步 B 段收口时记）**

- **有跨宿主对拍：12 行** —— 1 表单五字段、2 探测、3 扫码、4 配置导入导出、5 自动载入、
  6 权重解析、7 反向回填、8 开始/停止、9 进度与计数、10 续传两个答案、11 孤儿批次、
  12 日志。当时都在 `tests/test_host_parity.py`（含 `test_weight_parser_parity.py`），
  这两份随桌面版一起在 D-2 删掉了。
  **D-1 之后期望值有一处独立落点**：`tests/test_webui_host_contract.py` 用字面量钉住其中
  14 个对拍函数（右边不出现 `gui`），其余 9 个在 `test_webui_service.py` 早就是单侧断言 ——
  对拍那条"和那边一样"的判据随桌面版一起走，删之前必须先有这份能独立活着的记录。
  这一趟抓出三条真缺陷（`running` 没有推送通道不算在内，那是 runner 红出来的）：
  反向回填 webui 不建行、导入配置再探测会静默丢掉配置、日志 4 位行号此前没人钉。
- **已经是同一份实现，无从"两边各测一遍"：1 行** —— 14 导出 CSV（`src/history_export`）。
  这一格本来就是"先对拍再共用"走出来的，共用之后相等由构造保证。
- **刻意不同或纯显示层，不对拍：3 行** —— 15 清理 7 天前（Tk 里无确认直删，webui 走
  两步式一次性 token，§5 安全类，抹平等于把修复改回去）；13 历史 runs 8 列 / 明细 6 列 /
  200 条上限、16 `src.history` 未加载的黄条与"无反馈补提示"（列宽、黄条这类没有第二个
  宿主可比的东西，删掉参照实现之后由 webui 自己的用例 + 浏览器 E2E 负责；E2E 里那
  "行号 4 位、❯ 前缀、时间戳形状"三条就是这个意思）。

## 5. 现状缺陷：修哪三类，不碰哪五条

**修（本轮）**

1. 份数手输不校验：Tk 只在 `+/−` 按钮上夹范围（`app:800-804`），手输 50000 照跑、
   输 `abc` 抛未捕获 `TclError` 表现为"点了没反应"（`app:1427`）。
   → 服务端 `POST /api/run` 与 `/api/field` 统一校验，非法值回 400 + 人话错误。
2. URL 无格式校验 → 只校验 scheme 与非空，**不加新限制**（不拦 http、不查长度）。
3. 量表填越界单值（级数 5 填 `9`）生成全 0 权重且无提示（`wp:462-466`）→ 补一条 WARN。
4. 路径白名单：Tk 的任意路径由系统文件框兜着，HTTP 上必须收（§6）。
5. 清理/删除类动作加确认（Tk 是一键删库不可撤销）。
6. 日志可见性：验证码等待、补漏轮、人工提交阶段的提示现在全走 `print()` 到 stdout
   （`ver:255-300` 与 pipeline 十余处），**GUI 面板里看不到**，只表现为"日志静止 + 状态仍运行中"。
   → 用一个**按线程分流的 stdout 代理**接进日志队列，具体见 §13 第一条。

**不碰（原样搬，只在界面上加说明文字）**

- UC 复选框切到 Edge 只变灰、**值不清零**仍传引擎（`app:835-837` → `app:1444`）。
- 运行中导入配置/编辑权重表**不禁用**，而 `WEIGHT_CONFIG` 启动瞬间已快照（`app:1433-1438`）
  → 前端标一句"对当前批次无效"，不禁用。
- 隐私默认 GUI=`True`、CLI=`False` 相反（`app:214` vs `cli:502`）→ webui 跟 GUI 走 `True`。
- `survey_url` 被截断到 500 字符后真的拿去导航（`app:1451`）→ 保持，另加一条 WARN。
- 未知 tag 回落 INFO、探测超时只 debug 等既有宽松处 → 保持。

**权重解析不重写**：`weight_panel.build_weight_config` 那 200 多行是本轮唯一"必须逐字一致"的
逻辑，也是被测得最厚的部分。做法是把它**抽成 `src` 侧或 `webui` 侧的纯函数**（输入 questions +
字符串表，输出 cfg dict + WARN 列表），Tk 与 webui 共用一份 —— 这才是 parity 能验真身的前提。

## 6. 安全边界（本地 HTTP 服务不是免费的）

- 只 `bind 127.0.0.1`；启动即断言。
- 每个请求校验 `Host` 与 `Origin` 是本机回环，挡 DNS rebinding。
- 导入/导出/上传只接受**配置目录内的白名单文件名**，拒绝 `..`、绝对路径、符号链接、
  跨盘符（清点已发现 `os.path.relpath` 跨盘抛 `ValueError` 被通用 except 收成一句 FAIL，`ctl:178`）。
- 不自动拉起浏览器（要 `--open-browser` 显式开）；端口 `--port`，默认 0 让系统分配并打印实际地址。
- SSE 与所有 POST 只认同源。全部端点无鉴权 —— 因此**不提供任何远程访问**，README 明写。

## 7. 生命周期：三个 Tk 不存在的新故障模式

1. **关标签 ≠ 停止。** Tk 的"关窗 → `request_stop` → `join(30s)` → 关浏览器 → 闭合历史"
   是确定事件；浏览器里没有可靠的"用户走了"事件。不设计就会看到"我关了它还在跑、
   chromedriver 还挂着"。解法：显式「停止并退出」+ 服务进程收到 `Ctrl-C`/SIGTERM 走同一条
   收尾路径 + SSE 断连只做重连，**绝不据断连停长跑**。
2. **确认弹窗反向通道可能挂死。** Tk 是主线程阻塞等真人点；webui 必须服务端发起、前端回话。
   写错就是 worker 卡在一个再也不会回来的回答上。解法：该通道必须有超时 + 确定性默认值
   （`src/dialogs` 已定义无宿主时 confirm→False），且只有专职线程等，worker 不阻塞。
3. **部署面两个坑**：本机代理规则可能影响 `127.0.0.1`（这台机器有 `HTTP_PROXY`，
   要显式验证 `no_proxy` 覆盖回环）；静态文件没进 wheel → `pip install` 之后白屏，
   属于"看起来整个工具坏了"级别。→ 打包测试必须断言装完之后 `GET /` 拿到非空 HTML。

反过来**变稳的一面也要记**：Tk 里 worker 每轮要 `root.after(0, ...)` 甩回主线程，用户中途关窗
会打出一串 `TclError`，现在靠 `except` 静默吞（`ctl:387-401`）。webui 的 worker 只往队列写，
对界面存活零依赖 —— 这是 G5 的实质。

## 8. API 契约

```
GET  /                       index.html；/app.js /styles.css 静态
GET  /api/state              首屏一次拿全：表单值 + 运行态 + 三个可用性开关
GET  /api/events             SSE：log / progress / status / questions / state（+ gap 标记）
                                    confirm 走 state 里的 confirms 字段，不另开事件：
                                    对话框内容因此只有一份真相，重连/刷新/超时都收敛
POST /api/field              单字段写入（服务端校验）
POST /api/detect             探测题目            POST /api/qr             上传二维码图
POST /api/config/import      上传 JSON（白名单）  /export  /save-default
POST /api/weights            存权重表，错误以 WARN 列表回流
POST /api/run                开始（全部校验在此）  POST /api/stop
GET  /api/history/runs?limit=   /api/history/answers?run_id=   /api/history/export?kind=
POST /api/history/refresh        只要批次，不夹带整份 state
POST /api/history/purge          不带 token = 预览并发放一次性 token；带 token = 真删
POST /api/shutdown           停止 + 收尾 + 退出（等价于 Tk 关窗）
```

## 9. 测试与门禁

- **后端**（session/service/api）纯 Python，沿用「假 driver + 记录型事件出口」那类打法
  （stub driver、`RecordingRoot` 那一族），不需要浏览器。目标 ≥ Tk `app.py` 的 76%。
- **对拍测试**（本轮因保留 Tk 才可能）：同一条命令分别打到 Tk 宿主与 webui 宿主，
  断言 `run_batch` 收到的 `RunState` 与回调参数一致。这是 parity 的唯一硬证据。
- **前端**不进 Python 覆盖率，两道 gate：① `node --check` 语法门禁（先例：
  `tests/test_js_scripts.py::test_script_is_syntactically_valid_js`）；② 一条 `integration`
  标记的 **Selenium 自测 E2E** —— 用 Chrome 打开 `127.0.0.1:<port>`，点开始/停止、
  断言 SSE 日志到达、断言非法份数被拒。本仓库自带 Selenium 栈，零新依赖。
- **安全测试**单独一组：`../` 穿越、绝对路径、符号链接、跨盘符、非回环 `Host`。
- **打包**：`pyproject` 的 `packages` 加 `"webui"`（保留 `"gui"`）、
  `[project.scripts]` 加 `wjx-web`、静态文件进 `package-data`；`tests/test_packaging.py`
  是逐条比对的，同步改。
- **覆盖率口径**：新增 webui Python 会改 TOTAL，README 缺口表与 `ci.yml:58` 的
  `--cov-fail-under=70` 必须在 CI 等价环境（3.13 + 只装 `requirements*`）`--write` 重生成，
  本机 3.10 口径不落盘。

## 10. 落地顺序（每步可停）

1. ✅ `webui/session.py` + `service.py`（搬 controller）+ 后端测试 —— 不起服务也能测
2. ✅ `webui/server.py` + `api.py` + SSE + 安全测试
3. ✅ 第一版前端（表单 / 日志 / 进度）+ `wjx-web` 入口 —— **止损点已过，见下**
4. ✅ 权重表与探测回流 + §5 末的解析函数抽取 —— 表与回流随第 3 步落地；解析已合成
   **`src/weight_text.parse_weight_texts`** 一份（先逐题型对拍、抓到三条才合并），
   对拍测试改钉两个宿主的接线
5. ✅ 历史 Tab（列表 / 明细 / CSV 两个文件 / purge+confirm）—— 2026-09-25 落地。
   与原设计的一处偏离：**清理不走 SSE 反向通道**（那是第 6 步给"引擎中途要人确认"
   准备的），而是 `POST /api/history/purge` 的两步式一次性 token —— 删除范围由服务端定，
   请求体里没有可篡改的参数。CSV 的两个文件各一个 `kind`，不做 zip 也不做多文件下载。
6. ✅ 确认弹窗反向通道与断点续传 —— 2026-09-25 落地：`webui/confirm.py` 一条 SSE 出去、
   `POST /api/confirm` 回来的通道；引擎侧签名与桌面版逐字相同，所以
   `_apply_resumable_run` 一行没改。三条硬性质：等不到按最保守答案（不续传）、
   **超时之后到达的答案作废**、收尾 `cancel_all()` 叫醒等待者而不是让它干等。
   这一条修掉的实际缺陷是：纯 webui 进程里默认确认是 `popup_confirm`，它恒为 `False`，
   于是"检测到未完成批次"被**静默答成取消**。
7. 删 `gui/` —— **2026-09-26 做完**（用户当场推翻「推到赛后」，按 §11 的 A/B/C/D 四段走）
8. ✅ Selenium 自测 E2E 进测试套件 + 对拍测试 + ~~CI 等价环境 `--write`~~ ——
   2026-09-25 落地：`tests/test_webui_e2e.py` 8 项进 integration（真 HTTP + 真 SSE +
   真 headless Edge，替身只有 driver 工厂与探测/答题与引擎内部的批次循环三处；
   §9 那句「点开始/停止」由其中两条分头覆盖 —— 停止要在轮边界上才算数），`tests/test_host_parity.py`
   4 项进离线阻塞门禁（同一套表单值 → 两个宿主交给 `run_batch` 的 `RunState` 逐字段）。
   CI 等价环境的 `--write` 是这一条之前先还的账（动效提交欠下的）。
   抓到三条真缺陷 + 两条替身不够真（快照漏号把新事件挤掉、历史栏在批次结束后不作废、
   `running` 变回 false 没有推送通道 —— 最后这条是推上 runner 才红的，靠新装的
   "失败写成注解"那一步抓到名字），见 CHANGELOG「E2E 进套件 + 两宿主对拍 —— 步骤 8」一节。

**止损点**：第 3 步结束时如果 webui 还没跑通一次真实长跑，就停在那里。Tk 全程完好，
所以任何一步停下都是可用状态 —— 这是推迟第 7 步换来的东西。

2026-09-25 实测过闸：真浏览器（headless Edge 驱动控制台 + 有窗口 Edge 答题）对
`tests/fixtures/mock_wjx.html`（经本地 HTTP 提供，因为 §5 只放行 http/https 的 URL）
跑完 3 份真实提交，23 项断言全绿 —— 逐轮日志经 SSE 到达页面、计数与进度、`runs`
收尾 `finished`、39 条答案落盘。抓到四条只有跑起来才看得见的问题，见 CHANGELOG
「界面（第八个对标源）」一节。那轮确实是**一次性脚本**，但它没有原地蒸发：宿主这一侧的
整条链（HTTP 路由 → worker 线程 → SSE → 前端渲染）已由第 8 步的 7 项 integration 接住，
真探测与真提交仍由 `tests/test_e2e_integration.py`（25 项）覆盖。两边分工写在
`tests/test_webui_e2e.py` 的模块文档里 —— 前者要真浏览器但把引擎换成替身（引擎自己还要
再开一个浏览器，两个浏览器挤进必填检查只会换来随机红），后者正相反。

## 11. 赛后退役 Tk（另开一轮）

本轮**不做**，只登记条件，免得赛后重新推演：
webui 达到 ① parity 清单逐条有对拍测试 ② 真实长跑连续若干批无回归 ③ README/CHANGELOG
的 GUI 章节已改写成以 webui 为主 —— 三条齐了才删 `gui/`（**2026-09-26 实测：10 个文件
4671 行**）与 GUI 测试（`tests/test_gui_*.py` 7 个文件 **3241 行**），并把这几件东西
一起安置：`gui/motion.py` / `gui/ticker.py`（本轮刚建、100% 覆盖，浏览器里 CSS/JS 更强，
届时是死代码，该删就删）、`gui/controller.py` 里 webui 尚未覆盖到的残余分支，
以及**两处不是"删"而是"改写"的依赖**：`tests/test_packaging.py` 钉着 `wjx-gui = gui.app:main`
与 `packages` 名单，`tests/test_webui_entry.py` 拿 `gui/app.py` 的 `user_data_root` 当参照物
（"第二份实现"这个前提会随宿主一起消失）。

`gui/qr_utils.py` 已经在 2026-09-26 移进 `src/qr_utils.py`（§3 那条"先不移"到此作废）。

2026-09-25（第 8 步之后）对这三条的实测状态：**② 到位**（真浏览器 3 份长跑 + 8 项
webui integration 在 `.venv-ci313` 连跑四遍全绿，且在 GitHub 的 windows runner 上也绿 ——
那条只在 runner 上红的竞态已查明并修掉）；**① 只到一小块** —— §4 那 16 行里目前有跨宿主对拍的是
「表单五字段 → `RunState`」「权重表 → `weight_config_snapshot`」「交给 `run_batch` 的
位置参数与关键字」这三条（`tests/test_host_parity.py` 4 项 +
`tests/test_weight_parser_parity.py`），其余各行仍是"两个宿主各自有测试"而没有同一条
命令打到两边的硬证据；**③ 未动**。所以别把"E2E 进套件"读成"可以删 Tk 了"。

**2026-09-26 第 7 步开工，按四段走，每段都停在可用状态**：

- **A 拆耦合**（已做完）：`gui/qr_utils.py` → `src/qr_utils.py`，webui 不再 import 宿主包。
- **B 补对拍**（已收口）：§4 补了 9 行，每补一行都在查有没有真差距 —— 查出**两条真缺陷**：
  ① 续传时 webui 只写文本不建行，"已恢复到表格，可检查/修改"那句是空的；
  ② 先导入配置再探测会把刚导入的配置**静默丢掉**（新会话表是空的 → 导入什么都没写进表 →
  探测按等权重预填 → 点开始时整张表被重新解析并 `replace` 全局）。
  配置工件、开机自动载入、孤儿批次、进度算法、日志 tag 与行号那几行查下来两边等价，
  同样钉着。现在 **12/16** 行有对拍，剩下 4 行的处置逐条记在 §4 末尾。
- **C 改文档**（已做完）：README 以 Web 控制台为主入口（`### 界面：Web 控制台（主入口）`
  在前，桌面版降为"参照实现，后续版本移除"），三入口写进功能特性与 `pyproject` 的
  description，权重表编辑格式 / 历史 / 二维码 / 续传那几处的"GUI 独占"措辞改掉。
  CHANGELOG 的历史条目不重写 —— 它记的是当时的事实。
- **D 删**：分两刀，先转换后落刀。
  - **D-1 转契约**（已做完）：新增 `tests/test_webui_host_contract.py`（20 项，断言右边
    不出现 `gui`）。对拍那 23 个函数逐条查过去向：14 个的期望值改由这个文件钉成字面量，
    另外 9 个（探测 5 例、续传三个方向、`config_io` 缺席的拒绝措辞、日志行号）在
    `tests/test_webui_service.py` 里早就是 webui 单侧断言。18 处改坏验过 17 处变红，
    唯一活下来的是 `snapshot_weight_config` 里 `int(k)`→`k` 这个等价变异。
    两条顺手补出来的判据是**删 Tk 才会露出的洞**：`_QUESTION_COUNT_JS` 少一家选择器时
    只有对拍红（`test_webui_service.py` 132 项 + E2E 8 项全绿，替身返回的是写死的题数），
    而"两份抄本一致"这条判据本身随对拍一起走。
  - **D-2 落刀**（已做完，2026-09-26）：`git rm gui/`（10 个文件 4631 行）+
    `tests/test_gui_*.py`（7 个文件 3241 行 / 190 项）+ `run_gui.py` + `wjx-gui` 入口 +
    `conftest.py` 的 `tk_root` 基座；`pyproject` 的 `packages` 与 description、`ci.yml` 与
    `readme_coverage.PACKAGES` 的 `--cov=gui` 一起收成两包。
    随宿主一起走的还有两份对拍：`test_host_parity.py`（30 项）与
    `test_weight_parser_parity.py`（49 项，其规则由 `test_weight_text.py` 的字面量用例接管）。
    落刀前先搬的判据：桌面版续传那 13 项（`test_dialogs.py` 第 2 节，靠未绑定方法调
    `SurveyGUI._apply_resumable_run`）改写成 webui 侧 5 项 —— 含五个字段之间的**等式**、
    确认文案里的 Run 号与份数、续传查询按 500 字符截断键、恢复权重用整体替换；
    整卷 13 题的接线改成一条字面量用例；`test_webui_entry.py` 的「两份解析一致性」改成
    写死路径（15 → 16 项）；`test_history_gui_contract.py` 改名
    `test_history_schema_contract.py`（它钉的是 schema 列名，与谁在消费无关）。
    顺手拆掉的一条机制：`coverage_gaps.json` 的 `group_exempt_prefix: "gui/"` —— 那 2222 条
    语句的 89.5% 原本靠**整组豁免**过关而不是靠登记理由，豁免口随宿主一起删。
    定版 **v4.0.0**（`wjx-gui` 消失是破坏性变更）；README 的桌面版一节删掉，七步用法并进
    Web 控制台那一节。

三条前提到 C 段结束时的状态：**① 按条有结论**（12 行对拍 + 4 行写明为什么不对拍）、
**② 到位**、**③ 已改写**。所以 D 可以动 —— 但它是这一整程里唯一不可逆的一步，
落刀前先让人看一眼清单。

B 段每补一行都可能像今天这样抓出真缺陷 —— 这正是它排在删之前的理由：**删掉参照实现之后，
同一处差异再没有第二个宿主可以作证**。

## 12. 不做清单（react-bits 里明确不碰的）

`Backgrounds` 全部 57 个（WebGL/CSS shader，本机界面不需要动态背景）、
`Animations` 里的光标物理与合成层类（`GhostCursor` `MetaBalls` `LaserFlow`
`RippleDistortion` `ElectricBorder` `SplashCursor`）、`TextAnimations` 里依赖字体可变轴
与 hover 距离的（`VariableProximity` `TrueFocus` `WarpText` `StrokeText`）。
理由统一：它们是**演示型网页**的注意力工具，不是操作型工具的信息层。
本项目要的是"哪一题没过、为什么、还剩几份"，加动态背景只会降低对比度。

**要借的只有三样**：`Micro` 的 `SlideCommit` / `HoldButton`（滑动或长按才执行 —— 用在
「清理 7 天前」和「开始运行」上，比现在的一键删库和 messagebox 更贴不可逆动作）、
`TextAnimations` 的读数造型（`CountUp` 已有等价物、可加 `SplitFlapText` 翻页式计数）、
以及它把组件按"文本 / 微交互 / 背景 / UI"分四类登记的**目录组织方式**。

## 13. 三条实现期决定（写死，不留"到时候看"）

1. **`print()` 怎么进日志面板。** 已核实 `src/verification.py` 没有任何 log 接缝
   （`ver:255-300` 全是裸 `print`），所以"注入回调"必然要改 `src/` 的签名 —— 与 G2 冲突，**否掉**。
   `contextlib.redirect_stdout` 是**进程级**的，会把 HTTP 服务线程和任何杂项 print 一起吞进面板。
   选定做法：`webui` 装一个**按当前线程分流的 stdout 代理**（约 20 行）——
   只有 worker 线程的写进日志队列，其余原样透传给真 stdout。它可单测（假 worker 线程 +
   主线程各写一行，断言只有前者进队列），且 `src/` 一行不动。**若实现中发现必须改 `src/`，
   停下来改本稿的 G2 并说明，不许顺手改。**
2. **前端不引入构建步骤。** 手写单文件 `app.js` + `styles.css`，react-bits 的东西按它的观念
   在浏览器端重写，不装 node 工具链 —— node 只在 CI 里跑 `--check`（与 `pyright` 走 `npx` 现取
   同一先例，见 `pyproject.toml` 的 dev 注释），**因此 G3 的"零新依赖"仍然成立**：
   约束的是运行时依赖。
3. **主题沿用 Tk 那套身份**：高对比度黑白 + 蓝点缀、文字过 WCAG AA，不做暗色霓虹。
   react-bits 是暗色气质，这条是刻意不跟 —— 与 `DESIGN_motion_primitives.md` §2 的第三个岔路口一致。

