# 问卷星自动填写工具

> 基于 Selenium Stealth 的问卷星批量填写与提交工具：加权随机作答、人类行为模拟、可靠的三态提交语义，提供 CLI 与本地 Web 控制台两种入口。

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![CI](https://github.com/cloudy-one1/outcome-oriented-questionnaire/actions/workflows/ci.yml/badge.svg)](https://github.com/cloudy-one1/outcome-oriented-questionnaire/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-4.0.0-brightgreen.svg)](CHANGELOG.md)

本工具仅供学习与研究 Selenium 浏览器自动化技术使用，请遵守问卷星平台使用条款与相关法律法规（见[免责声明](#免责声明)）。

---

## 功能特性

- **九类题型** — 单选 / 多选 / 下拉 / 量表（含 NPS）/ 填空 / 矩阵单选 / 矩阵多选 / 矩阵量表 / 排序，全部在问卷星官方 OpenAPI 的 stable 档内，其余 98 种不做也不说成做（[边界详见](docs/architecture.md#题型覆盖的边界在哪)）
- **加权随机作答** — 每题可配选项权重；多选控制「选几个」的分布，矩阵多选控制每行勾几个，排序可钉住名次
- **题干锚定的预设** — 权重配置带题干 + 结构签名，问卷插题 / 换序不再错位；锚点认不到题走等权并提示
- **一份问卷一个人** — 性别 / 年龄 / 学历 / 职业 / 收入 / 婚姻 / 省市链式抽定，姓名 / 手机 / 邮箱 / 地址 / 身份证全部同源派生、真校验位
- **多分页问卷** — 按页探测、按页作答、只在最后一页点提交；翻页失败整份判失败，绝不交半份卷
- **可靠的提交语义** — OK / FAIL / UNKNOWN 三态、提交前完整度自检、答案幂等落库、指数退避重试、跨进程断点续传（权重随批次快照）
- **人工在环** — 验证码只检测并请人处理；`--manual-submit` 把点提交那一下交给人，`--rescue-gaps` 把必答缺口滚进视野等人补答
- **人类行为模拟** — 正态分布停顿、完整鼠标事件链、偶发长停顿；停止 / 关窗以 ≈0.2s 粒度生效，被打断的那一份不提交
- **历史与统计** — SQLite 批次与逐题明细、Web 界面查询、CSV 导出、过期清理；真实答卷回放 / 投递分布纠正 / 信度报告与控制（默认关）
- **浏览器反检测** — Stealth JS 注入隐藏自动化特征，UA / 屏幕 / 硬件指纹随机化，可选 undetected-chromedriver
- **双入口** — `wjx-fill`（CLI）与 `wjx-web`（本地控制台）共用同一引擎；Web 端 SSE 推送进度，关掉页面不中断长跑

---

## 快速开始

### 环境要求

- Python 3.10+（`src/models.py` 使用 `@dataclass(slots=True)`，3.10 是真实下限）
- Microsoft Edge 或 Google Chrome（WebDriver 由 Selenium Manager 自动管理）

### 安装

```bash
git clone <your-repo-url>
cd automation
pip install -r requirements.txt   # 核心依赖
pip install -e .                  # 可选：装成命令行工具，之后直接用 wjx-fill / wjx-web

# 可选依赖，按需装
pip install undetected-chromedriver  # 更强的 Chrome 反检测
pip install opencv-python            # 界面二维码 URL 解析（src/qr_utils.py）
pip install openpyxl                 # 真实答卷回放读 .xlsx（CSV 不需要）
```

### 跑一次（CLI）

```bash
# 默认提交 17 份（Edge）
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx"

# 10 份 + Chrome + 权重预设 + 历史库
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" -n 10 -b chrome \
                  --config ./configs/my_survey.json --history ./data/history.db
```

每次提交输出 `OK` / `FAIL` / `UNKNOWN` 之一。完整示例、参数一览、断点续传与那几个
统计开关的取舍都在 [docs/cli.md](docs/cli.md)。

> `--headless` 下智能验证**一出现就判本轮失败** —— 无头窗口里没有可伸手拉滑块的人。

### Web 控制台（主入口）

```bash
python run_web.py        # 装过本仓库后等价于 wjx-web；可选 --port 8765 / --open-browser
```

终端会打印 `http://127.0.0.1:<端口>/`，浏览器打开它：

1. 填问卷 URL（或点 **📷 二维码导入** 选一张二维码图）
2. 设提交份数与浏览器类型，点 **🔍 探测题目**，在权重表里按题型填权重
3. 点 **▶ 开始运行**；**⏹ 停止** 在下一题边界收尾，**📜 历史记录** 随时可看批次与逐题明细

引擎与 CLI 是同一份实现（`src.cli.run_batch`），不是重写的一套。三条须知：

- 只监听 `127.0.0.1` 且校验 `Host` / `Origin`，**没有登录鉴权** —— 不要转发到局域网或公网（[SECURITY.md](SECURITY.md)）
- 数据树在仓库根（`configs/`、`data/`），`WJX_USER_DATA_DIR` 可整体挪走
- 检测到上次未完成的批次会弹确认框问是否续传，两分钟未答按「取消」处理

---

## 权重配置

三条入口，最后都汇到同一个结构与同一套校验：Web 控制台的权重表、JSON 文件
（`--config` / 界面「💾 导出配置」）、直接改 `src/config.py` 的 `WEIGHT_CONFIG`。

出厂 `WEIGHT_CONFIG` 是**空的**，未配置的题目自动降级为等权重随机；
一份可直接抄的 22 题样例见
[`examples/weight_config.example.json`](examples/weight_config.example.json)：

```python
WEIGHT_CONFIG = {
    1: {"type": "single", "weights": [0.1, 0.3, 0.5, 0.1]},  # 第1题：单选，选项3概率最高
    2: {"type": "multi",  "weights": [0.2, 0.2, 0.3, 0.3],   # 第2题：多选
        "count_options": [2, 3], "count_weights": [0.4, 0.6]},  # 选2个40%，选3个60%
}
```

JSON schema、题干锚点、各题型的关键字段、界面权重表的编辑格式与校验规则，
以及那条 `matrix_scale` 的能力边界 → [docs/config.md](docs/config.md)。

---

## 文档

| 位置 | 是什么 |
|---|---|
| [docs/cli.md](docs/cli.md) | CLI 完整示例、参数一览、三态输出、断点续传、统计类开关、隐私 |
| [docs/config.md](docs/config.md) | 权重配置：JSON schema、题干锚点、题型字段、界面编辑格式、已知边界 |
| [docs/architecture.md](docs/architecture.md) | 工作原理全链路、分层、题型边界、探测的三道判据、术语表、落库结构 |
| [docs/scope.md](docs/scope.md) | 范围边界：评估过、明确不做的事项与各自理由 |
| [docs/coverage.md](docs/coverage.md) | 覆盖率口径与已知缺口（**生成物**，`--check` 是 CI 门禁） |
| [CHANGELOG.md](CHANGELOG.md) | 按版本记的变更，每条带当时的取舍理由 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 环境、提交前必过的门禁、测试基座的几个坑、定版步骤 |
| [SECURITY.md](SECURITY.md) | 哪些落盘位置含个人信息、什么算安全问题、私有报告入口 |
| `docs/design/` | 设计稿 —— 信度那条链的数学与边界、webui 宿主的决策记录 |
| `docs/reviews/` | 历史评估报告的归档：查出过什么缺陷、按哪条落地 |
| `src/` | 引擎：探测 → 作答 → 提交；`src/interactions/` 一种题型一个模块 |
| `webui/` | 本地 Web 控制台宿主（stdlib HTTP + SSE），同样只调 `src/cli.run_batch` |
| `scripts/` | 门禁工具本身（覆盖率口径生成脚本、E2E 计数闸门） |
| `tests/` | 离线套件与浏览器 E2E；mock 问卷在 `tests/fixtures/` |

---

## 开发与测试

```bash
pip install -r requirements-dev.txt          # 钉版本，避免上游发版悄悄改掉门禁口径

python -m pytest tests/ -m "not integration" -q   # 离线套件（无浏览器环境 / CI 跑这个）
python -m pytest tests/ -m integration -q         # 浏览器 E2E，需本机 Edge / Chrome + WebDriver
python -m ruff check . && npx pyright             # 静态检查 + 类型检查
```

当前测试条数以本地运行为准，不在这里手写 —— 手抄的计数会和覆盖率数字一样腐烂。

- **提交前必过的门禁清单在 [CONTRIBUTING.md](CONTRIBUTING.md)**：ruff、pyright（0 error
  0 warning，不设 baseline）、带覆盖率地板的离线套件、文档口径、真实浏览器 E2E（阻塞）。
- **实测覆盖率与「仍然没有防线的地方」在 [docs/coverage.md](docs/coverage.md)**，
  由 `python scripts/coverage_doc.py --write` 从 `coverage.json` 生成；`--check` 已进 CI。
- 命名约定：布尔变量使用 `is_` / `has_` / `should_` / `use_` 前缀；
  题型字符串优先经 `models.normalize_question_type` 归一化。

---

## 隐私

历史库会把填空题原文（姓名 / 手机 / 邮箱一类）写进 `data/history.db`。
`--no-record-text`（CLI）与界面上的「🔒 不记录填空文本」只关掉 SQLite 那一列，DOM 仍要填真实文本。
**默认值两边不同**：CLI 默认记录，界面默认不记录 —— 因为界面会无条件把库落到 `data/history.db`。
`data/`、`configs/`、`*.db`、`*.csv` 已在 `.gitignore` 里。清理与导出注意项见
[docs/cli.md](docs/cli.md#隐私)。

---

## 不在本工具范围内

代理与 IP 池、并发 worker、自动识别验证码、大模型代答、移动端投放形态、第二个平台——
这些方向都评估过并明确不做，逐项理由见 [docs/scope.md](docs/scope.md)。

---

## 免责声明

本工具仅供学习和研究 Selenium 自动化技术使用。请遵守问卷星平台的使用条款和相关法律法规，不得用于刷票、刷量等任何违规或违法用途。使用者需自行承担所有责任。

`--alpha-target` 让工具的能力边界从"填得完"扩到"让造出来的数据通过信度检验"，
`--replay-file` 则是把真实答卷搬进提交链路。这两件事**只适用于自有或已获授权的测试问卷**
（验证算法用）；拿它们去生产环境里制造"看着像真研究数据"的样本，就是本段禁止的刷量本身
（设计稿对这一条的表述见 [DESIGN_reliability_alpha.md](docs/design/DESIGN_reliability_alpha.md) §0）。

## License

[MIT](LICENSE)
