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
  项目面向 Windows：人工介入那几步走的是系统弹窗。

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
pip install -e .                                    # 之后可直接用 wjx-fill / wjx-gui
```

依赖一律钉版本。运行时依赖的单一真相是 `requirements.txt`，`pyproject.toml` 由
`tests/test_packaging.py` 逐条比对 —— 两处各自演化迟早会变成"CI 装的和文档说的不是一套"。

## 提交前必须过的门禁

这几条就是 CI 的全部内容，本地跑不过不必推上来：

| 门禁 | 命令 |
|---|---|
| 静态检查 | `python -m ruff check .` |
| 类型检查 | `npx pyright`（`src/` + `gui/` + `webui/` + 入口 **0 error 0 warning**，不设 baseline、不写 `# type: ignore`） |
| 离线套件 + 覆盖率地板 | `pytest tests/ -m "not integration" --cov=src --cov=gui --cov-report=json:coverage.json --cov-fail-under=$(grep -oE 'cov-fail-under=[0-9.]+' .github/workflows/ci.yml | cut -d= -f2)` |
| 文档口径 | `python scripts/readme_coverage.py --check` |
| 浏览器 E2E | `pytest tests/ -m integration -v --junitxml=e2e-junit.xml && python scripts/e2e_gate.py e2e-junit.xml` |

- **覆盖率地板只许上调**，而且它写在 `ci.yml` 里、是本仓库唯一还手写的门禁数字。
  README 的实测覆盖率是 `scripts/readme_coverage.py` 从 `coverage.json` 生成的，
  手改 README 那一块会在 `--check` 处变红。
- **E2E 是阻塞的**。`scripts/e2e_gate.py` 会数 junit 里的实际执行条数 —— 驱动没起来
  导致整片 skip 也照样是绿，那条就是堵这个洞的。只跑离线套件不算验过。
- **`src/` + `gui/` + `webui/` + 入口里不许新增 `# type: ignore` / `# pyright:`**，现存条目登记在
  `tests/test_ci_guards.py` 的 `BASELINE` 里、只准变小。

## 测试怎么写

- 新增行为要带离线测试；涉及生成 JS 的改动还要有 E2E 证据。`node --check` 与离线
  mock 只能保证语法合法，`input[name='q' + q + '']` 这类**语法合法、语义非法**的
  错选择器只有真浏览器抓得住。
- 不要在离线套件里开真浏览器：`tests/test_driver_factory_offline.py` 用 autouse 夹具
  把 `webdriver.Edge/Chrome` 换成一调用就 `AssertionError` 的兜底替身。
- Tkinter 的根窗口由 `conftest.py` 的会话级 `tk_root` 提供，全会话只建一个：Windows 上
  第二个 `tk.Tk()` 会抛 "Can't find a usable tk.tcl"，症状是后面整片测试**静默 skip**。
  要"整个窗口"的用例挂在它的 `Toplevel` 上。
- 新增 mock 问卷放 `tests/fixtures/`，脱敏后再交 —— `*.csv` / `*.db` / `data/` /
  `configs/` 已在 `.gitignore` 里，因为 `data/history.db` 存着填空题原文（姓名、手机、邮箱）。

## 分支与提交

- `main` 是唯一长期分支。工作分支从 `main` 切出来，名字带上类型与前缀（`fix/...`、
  `feat/...`、`chore/...`、`test/...`），合掉就删。
- 提交信息首行照 `fix(v3.3): 一句话说清改了什么` 这个形状写；正文写**为什么**和取舍，
  不写"做了什么"—— diff 已经说明了做什么。
- 一次提交只做一件事。行为改动与格式化分开，否则回归追不回来。

## 定版（维护者动作）

1. 版本号三处一起改：`pyproject.toml` 的 `project.version`、`src/__init__.py` 的
   `__version__`、README 顶部的 Version 徽章。三处都由 `tests/test_packaging.py` 钉着，
   漏改徽章 CI 会红。
2. CHANGELOG 的 `[未发布]` 块转成 `## [X.Y.Z] - YYYY-MM-DD`，新开一个空的 `[未发布]`。
   版本号没升之前不要把批次写成"已发布的某版"。
3. `python scripts/readme_coverage.py --write` 重生成 README 的覆盖率口径块。
4. 打 annotated tag（`git tag -a vX.Y.Z -m ...`）并推到 `main`。

## 提问与讨论

Issue 优先走模板。描述缺陷时请写清问卷是 **PC 版还是移动端投放**：移动端那一套
（jQuery-Mobile 的 `.ui-radio` / `.ui-input-text`）本仓库未适配，"一道题都探测不到"
在那个形态下是预期行为，不是缺陷。
