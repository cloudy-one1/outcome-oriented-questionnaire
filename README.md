# 问卷星自动填写工具

> 基于 Selenium Stealth 的问卷星批量填写与提交工具：加权随机作答、人类行为模拟、智能验证码检测，提供 CLI 与本地 Web 控制台两种使用方式。

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![CI](https://github.com/cloudy-one1/outcome-oriented-questionnaire/actions/workflows/ci.yml/badge.svg)](https://github.com/cloudy-one1/outcome-oriented-questionnaire/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-4.0.0-brightgreen.svg)](CHANGELOG.md)

本工具仅供学习与研究 Selenium 浏览器自动化技术使用，请务必遵守问卷星平台使用条款与相关法律法规（详见[免责声明](#免责声明)）。

---

## 功能特性

- **浏览器反检测** — CDP 注入 Stealth JS 隐藏 `navigator.webdriver`，随机化 UA / 屏幕分辨率 / 硬件指纹，可选 `undetected-chromedriver` 增强模式
- **人类行为模拟** — 所有点击与停顿均为「正态分布 + 区间截断」采样，完整鼠标事件链，偶发长停顿，周期性重启浏览器释放内存
- **九类题型全覆盖** — 单选 / 多选 / 下拉 / 量表（含 NPS）/ 填空 / 矩阵单选 / 矩阵多选 / 矩阵量表 / 排序。这九类全部落在问卷星官方 OpenAPI 清单的 stable 档，其余 98 种不做也不说成做（边界的来龙去脉见 [architecture.md](docs/architecture.md#题型覆盖的边界在哪)）
- **填空题按字段类型生成** — 姓名 / 手机 / 邮箱 / 年龄 / 地址 / 所在地区（省市）/ 公司；字段类型**先问平台的 `verify` 属性**，认不到才用题干正则猜
- **一份问卷 = 一个人** — 姓名 / 性别 / 年龄 / 学历 / 职业 / 收入 / 婚姻 / 子女 / 省市一次抽定，其余全部从这份画像派生（身份证前 6 位对得上省市、校验位是真算的、邮箱前缀是姓氏拼音）（`src/persona.py`）
- **加权随机作答** — 每道题可配选项权重；多选还能控制「选中几个」的分布，矩阵多选控制每行勾几个，排序题可钉住前几名
- **题干锚定的预设** — 预设带题干 + 结构签名，问卷中间插一题不再整体错位；锚点认不到题时**该题走等权并提示**，而不是拿别人的分布静默填
- **多分页问卷** — 按页探测、按页作答、**只在最后一页点提交**；翻页失败时整份判失败，绝不把只答了第一页的问卷交上去
- **探测的自我体检** — 结构对拍（探测结果 ↔ 平台自报的 `topic`/`type`）、整页形态诊断（移动端明说「未适配，换 PC 链接」）、逐题作答回执、提交前完整度自检、提交区协议框诊断（**只提示、绝不代勾**）
- **智能验证码检测** — DOM / URL 文本 / Shadow DOM 三信号并行；检出后弹窗请人工处理，人工介入锁保证等待期间不被误判为超时；无头模式下直接判本轮失败（没有可介入的人）
- **把最后一下交给人** — `--manual-submit` 每一份答完就停住由人核对并点提交，`--rescue-gaps` 把拦下来的必答题滚进视野等人补答。要的是它而不是「跑一份读日志」：半份问卷一旦交上去就是平台上一条**收不回来**的回收记录
- **可靠的提交语义** — 三态结果（OK / FAIL / UNKNOWN）、答案幂等写入、指数退避重试、异常分层；页面的 `alert` 接管成记录器，必填校验那句话会变成日志里的失败原因
- **随时可停** — 停止 / 关闭窗口在逐题边界、每题思考停顿、验证码人工等待上都以 ≈0.2s 粒度生效，被打断的那一份不提交、不计失败
- **断点续填与续传** — 重试时跳过单次提交内已答的题；跨进程从上次中断的批次继续，权重配置随批次快照持久化；批次匹配认的是**同一份问卷**而不是 URL 字符串
- **运行历史与统计** — SQLite 记录批次元信息与逐题答案明细，界面可视化查询、CSV 导出、过期清理
- **统计与数据类开关**（全部默认关）— 真实答卷回放 `--replay-file`、投递分布在线纠正 `--drift-correct`、实测信度报告 `--report-alpha`、目标信度控制 `--alpha-target`
- **CLI / Web 控制台双入口** — `pip install -e .` 后可用 `wjx-fill` / `wjx-web`；CLI 另有顺序队列 `--url-file`、整批时限 `--max-total-time`、profile 复用、预约开跑 `--start-at`（Tkinter 桌面版已于 v4.0 退役）

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

### 界面：Web 控制台（主入口）

```bash
python run_web.py        # 装过本仓库后等价于 wjx-web；可选 --port 8765 / --open-browser
```

终端会打印 `http://127.0.0.1:<端口>/`，浏览器打开它 —— 探测、权重表、开始 / 停止、
历史查询都在那一页上：

1. 填问卷 URL（或点 **📷 二维码导入** 选一张二维码图，地址自动解析出来）
2. 设提交份数与浏览器类型（Edge / Chrome，Chrome 可加 UC 反检测）
3. 点 **🔍 探测题目** 自动识别问卷结构，在权重表第 4 列按题型填权重（格式见 [docs/config.md](docs/config.md)）
4. 点 **▶ 开始运行**；跑起来后随时能切到 **📜 历史记录** 看批次与逐题明细，**⏹ 停止** 会在下一题边界收尾

引擎侧一行都没重写：探测、答题、提交、历史落盘走的还是 `src/cli.run_batch`，与 CLI
是同一份实现、同一个 `RunState`。日志与进度由服务端经 SSE 推给页面，**关掉页面不会
中断长跑**，重新打开即接上。

- 只监听 `127.0.0.1`，且每个请求校验 `Host` 与 `Origin` 是本机回环。**没有登录与鉴权**，
  所以不要把它转发到局域网或公网 —— 那等于把"驱动一个浏览器自动提交问卷"的开关
  交给同网段的任何人（详见 [SECURITY.md](SECURITY.md)）。
- 数据树在仓库根：`configs/` 与 `data/`，`WJX_USER_DATA_DIR` 可以把整棵挪走。
- 检测到上次未完成的批次时会在浏览器里弹确认框问"要不要接着继续"，**超过两分钟没答
  按"取消"处理** —— 宁可不续传也不会把同一份问卷交两遍。

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
  由 `python scripts/coverage_doc.py --write` 从 `coverage.json` 生成。改这些数字的唯一
  合法路径是跑那个脚本；`--check` 已进 CI。
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

## 不在本工具范围内（评估过、明确不做）

| 事项 | 为什么不做 |
|---|---|
| 代理 / IP 池 / 伪造 `X-Forwarded-For` | 与下面的免责声明正面冲突；平台计数走服务端真实 IP + cookie + 智能验证，XFF 只在特定反代配置下被采信 —— 效果不可靠，代价却是对方的风控 |
| 并发 worker（同时开 N 个浏览器） | 本工具的立身点是「正态分布的人类行为 + 可靠的提交语义」。N 个实例同时提交会直接稀释前者，并让「人工介入验证码」这个单实例前提失效 |
| 自动识别验证码 | 只检测、只请人帮忙。绕过验证码不是本项目要解决的问题 |
| 大模型代答 / "人设化"答案 | 两个同类项目都做了，我们仍然不做：它把「答案是谁写的」这个问题整体移出了工具，还带上题干原文拼进 prompt 的注入面（理由的完整版在 [CHANGELOG](CHANGELOG.md) 与 `docs/reviews/`） |
| 移动端投放形态的作答 | 判据与注入要换一整套，而 **PC 版链接是现成的替代品**。现在只诊断并说清「换链接」，一行代码都不往注入侧加 |
| Web 控制台的无头 / 队列 / 时限 / 预约开关 | 控制台是"看着页面跑"的入口；队列、时限与 `--start-at` 要的是无人值守，那是 CLI 的场景 |

第二个平台（腾讯问卷 / 金数据 / Google Forms）也**没有**支持：`src/platforms.py` 只是把
问卷星专属的选择器收成了一张表，题型识别与作答注入的 JS 仍是问卷星的 DOM 约定。

---

## 免责声明

本工具仅供学习和研究 Selenium 自动化技术使用。请遵守问卷星平台的使用条款和相关法律法规，不得用于刷票、刷量等任何违规或违法用途。使用者需自行承担所有责任。

`--alpha-target` 让工具的能力边界从"填得完"扩到"让造出来的数据通过信度检验"，
`--replay-file` 则是把真实答卷搬进提交链路。这两件事**只适用于自有或已获授权的测试问卷**
（验证算法用）；拿它们去生产环境里制造"看着像真研究数据"的样本，就是本段禁止的刷量本身
（设计稿对这一条的表述见 [DESIGN_reliability_alpha.md](docs/design/DESIGN_reliability_alpha.md) §0）。

## License

[MIT](LICENSE)
