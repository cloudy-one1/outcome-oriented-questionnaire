# 参与贡献

先读 [README](README.md) 的「不在本工具范围内（评估过、明确不做）」与
[「免责声明」](README.md#免责声明)两节。这个仓库的能力边界是刻意划出来的，越界的 PR
（代理池换 IP、并发提速、大模型代答）不是没考虑过，是评估后明确不做 ——
提之前请先看那一节的理由，省掉双方一轮往返。

## 环境

- Python 3.10 是**真实下限**，不是随手写的数字：`src/models.py` 用了
  `@dataclass(slots=True)`。CI 因此跑 3.10 与 3.13 两条 leg，只测高版本会让
  "最低支持 3.10"这句话永远不被验证。
- Microsoft Edge 或 Google Chrome（WebDriver 由 Selenium Manager 自动管理）。
  项目面向 Windows：Edge 驱动那条路径是在 Windows 上量的。
- **人工介入发生在浏览器窗口里**，不在系统弹窗里：验证码、补漏轮、人工提交那几步要的是
  真人看着页面动手。桌面宿主（Tkinter）已在 v4.0 退役，`src/dialogs.py` 只剩间接层 ——
  CLI 与测试不注册 handler 就拿到确定性默认值，Web 控制台的确认走一条 SSE 反向通道
  （`webui/confirm.py`：服务端 `ask()` → 快照里的 confirms → 页面 POST 回 `/api/confirm`）。
- `wjx-web` / `python run_web.py` 起的本地控制台**只监听 127.0.0.1**（`api.py` 还另判
  回环 Host 与同源 Origin）。它没有鉴权，别把它端口转发出去。

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
pip install -e .                                    # 之后可直接用 wjx-fill / wjx-web
```

依赖一律钉版本。运行时依赖的单一真相是 `requirements.txt`，`pyproject.toml` 由
`tests/test_packaging.py` 逐条比对 —— 两处各自演化迟早会变成"CI 装的和文档说的不是一套"。

## 提交前必须过的门禁

这几条就是 push 到 `main` 与开 PR 时会跑的全部内容（镜像另有 `docker-smoke.yml`，每周一
与手动触发，不在必填检查里）。本地跑不过不必推上来：

| 门禁 | 命令 |
|---|---|
| 静态检查 | `python -m ruff check .` |
| 类型检查 | `npx pyright`（`src/` + `webui/` + 入口 **0 error 0 warning**，不设 baseline、不写 `# type: ignore`） |
| 离线套件 + 覆盖率地板 | `pytest tests/ -m "not integration" --cov=src --cov=webui --cov-report=json:coverage.json --cov-fail-under=$(grep -oE 'cov-fail-under=[0-9.]+' .github/workflows/ci.yml \| cut -d= -f2)`（`--cov` 的权威名单是 `scripts/coverage_doc.py` 的 `PACKAGES`，与 `ci.yml` 不一致会红） |
| 文档口径 | `python scripts/coverage_doc.py --check` |
| 浏览器 E2E | `pytest tests/ -m integration -v --junitxml=e2e-junit.xml && python scripts/e2e_gate.py e2e-junit.xml --require-file tests/test_e2e_integration.py --require-file tests/test_webui_e2e.py` |

### 门禁背后的几条判据

- **覆盖率地板只许上调**，而且它写在 `ci.yml` 里、是本仓库唯一还手写的门禁数字。
  `docs/coverage.md` 的实测覆盖率是 `scripts/coverage_doc.py` 从 `coverage.json` 生成的，
  手改那一块会在 `--check` 处变红。
- **E2E 是阻塞的**。`scripts/e2e_gate.py` 会数 junit 里的实际执行条数 —— 驱动没起来
  导致整片 skip 也照样是绿，那条就是堵这个洞的。两个 `--require-file` 堵的是另一半：
  全局计数看得见"作答那批跑了"，看不见"新宿主那批全被吞成 skip"，两边各自点名、缺一即红。
  只跑离线套件不算验过。
- **`src/` + `webui/` + 入口里不许新增 `# type: ignore` / `# pyright:`**，现存条目登记在
  `tests/test_ci_guards.py` 的 `BASELINE` 里、只准变小。
- **ruff** 启用 `E9/F63/F7/F82` 加 `F401/F841/F541`。后三条是刻意挑的、零误报的「接线断链」防线：
  一个真实发出去的缺陷（算好的续传 flag 从没传给 `run_batch`，于是续传静默空转）正好是 `F841`
  抓的那种形状。
- **pyright** 覆盖 `src/`、`webui/` 与入口脚本。从已退役的 Tkinter 宿主带过来的判断是：清零时
  一条 `# type: ignore` 都没用 —— 需要豁免的门禁只是装饰。
- **JS 生成器做真语法检查**：`tests/test_js_scripts.py` 对每个生成器的输出跑 `node --check`
  （缺 node 时退化成残缺片段检测）。但它只证明语法 —— `input[name='q' + q + '']` 这类
  「语法合法、语义非法」的选择器，只有真浏览器 E2E 抓得住。
- **离线套件永不碰真浏览器**：`tests/test_driver_factory_offline.py` 用 autouse 夹具把
  `webdriver.Edge/Chrome` 换成一调用就抛 `AssertionError` 的兜底替身。
- **对话框 / 文件框是可注入的出口**（`src/dialogs.py`）：宿主注册真实现，CLI 与测试拿确定性默认值。
  这么改是因为被两条模态路径挡住的代码曾停在约 25% 与 14% 覆盖，借口是「模态对话框」。
- **宿主契约钉成字面量**：`tests/test_webui_host_contract.py` 断言表单 → `RunState`、权重表 →
  快照、交给 `run_batch` 的每个关键字、进度数字、导出的文档形状 —— 没有第二个宿主可对拍，它的
  前身是 23 条跨宿主对拍断言。做过变异测试：故意改坏 18 处，17 处让它变红；唯一没红的是快照里
  `int(k)` → `k`，因为那时 key 早就是整数，属等价变异。
- **离线 mock 问卷**（`tests/fixtures/mock_wjx.html`）13 题：8 个容器题型码，外加一个非 1 基的
  2~10 量尺、矩阵多选、排序，和一个自带填空框的多选项。它模拟 wjx 的 AJAX 提交（URL 不变、延迟弹
  「提交成功」、数提交按钮点击次数）与必填校验（勾「其他____」却不写字 → 原生 `alert`、不给成功
  文案），这正是填空与接管 `alert` 那两套行为的真浏览器证据，不是替身。`mock_wjx_multipage.html`
  是两页变体（第 2 页起始 `display:none`）。`mock_wjx_consent_box.html` 给同意框判据用：一个未勾的
  `#checkxiexi` 配相邻含「同意」/「协议」的文案、一个已勾的同意框，外加一个标签字面就是
  「我同意接收后续邮件」的选项 —— 那条判据靠的是真实元素关系（`label[for]`、`closest('div[topic]')`），
  重复计数那个缺陷最早就是在这一层被抓到，而不是离线替身。
- **Docker**：`.github/workflows/docker-smoke.yml` 每周加手动触发，`docker build` →
  `wjx-fill --help` → `import src.*`，只构建不发布；它不在必填的 push 检查里，所以 Dockerfile
  的口径由 `tests/test_packaging.py` 与这条 workflow 双向对齐。
- **文档里的本地链接是查出来的，不是看出来的**（`tests/test_doc_links.py`）：文件在不在磁盘上、
  锚点按 GitHub 的 slug 规则算不算得出来、目标文件在不在版本库里，三条各挡一种真实失效。
  最后一条专门对着 `.gitignore` 的 `docs/*` —— 新文档忘了显式放行时本地一切正常，GitHub 上 404。

## 测试怎么写

- 新增行为要带离线测试；涉及生成 JS 的改动还要有 E2E 证据。`node --check` 与离线
  mock 只能保证语法合法，`input[name='q' + q + '']` 这类**语法合法、语义非法**的
  错选择器只有真浏览器抓得住。
- 不要在离线套件里开真浏览器：`tests/test_driver_factory_offline.py` 用 autouse 夹具
  把 `webdriver.Edge/Chrome` 换成一调用就 `AssertionError` 的兜底替身。
- 界面只有一个宿主：Tkinter 时代 `conftest.py` 里那个会话级 `tk_root`（全会话只建一个根
  窗口，否则 Windows 上第二个 `tk.Tk()` 会抛 "Can't find a usable tk.tcl"，症状是后面整片
  测试**静默 skip**）已随桌面版在 v4.0 一起退役，新宿主用例不要再找它。
- 新增 mock 问卷放 `tests/fixtures/`，脱敏后再交 —— `*.csv` / `*.db` / `data/` /
  `configs/` 已在 `.gitignore` 里，因为 `data/history.db` 存着填空题原文（姓名、手机、邮箱）。

## 分支与提交

- `main` 是唯一长期分支。工作分支从 `main` 切出来，名字带上类型与前缀（`fix/...`、
  `feat/...`、`chore/...`、`test/...`），合掉就删。
- 提交信息首行照 `fix(vX.Y): 一句话说清改了什么` 这个形状写（类型用 `fix` / `feat` /
  `test` / `refactor` / `docs` / `chore` / `ci`）；正文写**为什么**和取舍，
  不写"做了什么"—— diff 已经说明了做什么。
- 一次提交只做一件事。行为改动与格式化分开，否则回归追不回来。

## 定版（维护者动作）

1. 版本号三处一起改：`pyproject.toml` 的 `project.version`、`src/__init__.py` 的
   `__version__`、README 顶部的 Version 徽章。三处都由 `tests/test_packaging.py` 钉着，
   漏改徽章 CI 会红。
2. CHANGELOG 的 `[未发布]` 块转成 `## [X.Y.Z] - YYYY-MM-DD`，新开一个空的 `[未发布]`。
   版本号没升之前不要把批次写成"已发布的某版"。
3. `python scripts/coverage_doc.py --write` 重生成 `docs/coverage.md` 的覆盖率口径块。
4. 打 annotated tag（`git tag -a vX.Y.Z -m ...`）并推到 `main`。

## 提问与讨论

Issue 优先走模板。描述缺陷时请写清问卷是 **PC 版还是移动端投放**：移动端那一套
（jQuery-Mobile 的 `.ui-radio` / `.ui-input-text`）本仓库未适配，"一道题都探测不到"
在那个形态下是预期行为，不是缺陷。
