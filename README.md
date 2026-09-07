# 问卷星自动填写工具

> 基于 Selenium Stealth 的问卷星批量填写与提交工具：加权随机作答、人类行为模拟、智能验证码检测，提供 CLI 与 GUI 两种使用方式。

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![CI](https://github.com/cloudy-one1/outcome-oriented-questionnaire/actions/workflows/ci.yml/badge.svg)](https://github.com/cloudy-one1/outcome-oriented-questionnaire/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-2.4.0-brightgreen.svg)](CHANGELOG.md)

本工具仅供学习与研究 Selenium 浏览器自动化技术使用，请务必遵守问卷星平台使用条款与相关法律法规（详见[免责声明](#免责声明)）。

---

## 功能特性

- **浏览器反检测** — CDP 注入 Stealth JS 隐藏 `navigator.webdriver`，随机化 UA / 屏幕分辨率 / 硬件指纹，可选 `undetected-chromedriver` 增强模式
- **人类行为模拟** — 所有点击与停顿均为「正态分布 + 区间截断」采样，完整鼠标事件链，偶发长停顿，周期性重启浏览器释放内存
- **六类题型全覆盖** — 单选 / 多选 / 下拉 / 量表 / 填空 / 矩阵单选，填空题自动识别姓名、手机、邮箱、年龄、地址、公司等字段并按类型生成
- **加权随机作答** — 每道题可配置选项权重：单选加权采样，多选无放回加权抽样，多选还可控制「选中几个」的分布
- **智能验证码检测** — DOM / URL 文本 / Shadow DOM 三信号并行检测；检出后弹窗提醒人工处理，人工介入锁保证等待期间不被误判为超时
- **断点续填与续传** — 重试时自动跳过单次提交内已答的题；跨进程可从上次中断的批次继续（CLI `--resume` / GUI 恢复对话框），权重配置随批次快照持久化
- **运行历史与统计** — SQLite 记录每次批次的元信息与逐题答案明细，GUI 可视化查询、CSV 导出、过期清理
- **可靠的提交语义** — 提交结果三态（成功 / 失败 / 未知），答案明细幂等写入，指数退避重试，异常分层（瞬态 DOM 异常可重试、程序错误直接暴露）
- **CLI / GUI 双入口** — CLI 适合脚本与定时批量运行，GUI（Tkinter）适合可视化配置与实时监控

---

## 快速开始

### 环境要求

- Python 3.9+
- Microsoft Edge 或 Google Chrome（WebDriver 由 Selenium Manager 自动管理）

### 安装

```bash
git clone <your-repo-url>
cd automation

# 核心依赖
pip install -r requirements.txt

# 可选：更强的 Chrome 反检测模式
pip install undetected-chromedriver

# 可选：GUI 二维码 URL 解析
pip install opencv-python
```

### CLI 快速上手

```bash
# 最简运行：指定问卷 URL，默认提交 17 份（Edge）
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx"

# 自定义份数 + Chrome 浏览器
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" -n 10 -b chrome

# Chrome + undetected-chromedriver 模式（反检测更强）
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" -b chrome --uc

# 使用 JSON 权重配置 + 启用历史记录
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" \
                  --config ./configs/default_weight_config.json \
                  --history ./data/history.db

# 目标成功 10 份（而非总尝试 10 次），最多尝试 30 次防死循环
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" -n 10 \
                  --target-success --max-attempts 30

# 从上次中断的批次继续（需与 --history 配合，详见「断点续传」）
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" \
                  --history ./data/history.db --resume

# 运行完导出当前权重为 JSON 模板（便于分享与版本管理）
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" -n 1 --save-config ./configs/my_survey.json
```

每次提交会输出 `OK` / `FAIL` / `UNKNOWN` 之一：

- `OK` — 页面跳转或出现「提交成功 / 感谢您的参与」等关键词，确认成功
- `FAIL` — 验证码未通过、题目探测失败、按钮定位失败等，确认失败
- `UNKNOWN` — 按钮已点击但等待效果超时（可能是 AJAX 异步提交，也可能是服务端拒绝），保守计为失败并单独统计，便于事后复盘

### CLI 参数一览

| 参数 | 默认 | 说明 |
|---|---|---|
| `-u, --url URL` | （必填） | 问卷完整 URL，出于合规考虑不提供默认值 |
| `-n, --count N` | `17` | 提交份数；配合 `--target-success` 时解释为「目标成功份数」 |
| `-b, --browser` | `edge` | 浏览器类型：`edge` / `chrome` |
| `--uc` | 关 | 仅 Chrome 生效：优先使用 undetected-chromedriver，失败自动回退原生 Selenium |
| `-c, --config PATH` | 无 | 从 JSON 加载权重配置（schema 2.0）并热更新 |
| `--save-config PATH` | 无 | 运行结束后把当前权重保存为 JSON 模板 |
| `-H, --history PATH` | 关 | 启用 SQLite 历史记录（指定 DB 路径） |
| `--stats` | 关 | 运行结束后打印全库统计（需配合 `--history`） |
| `--no-record-text` | 关 | 隐私保护：填空题答案不写入 SQLite（DOM 仍需填入真实文本） |
| `--target-success` | 关 | 把 `-n` 解释为「目标成功份数」，达成即提前结束 |
| `--max-attempts N` | `2×n` | 仅 `--target-success` 时生效：最大尝试次数上限 |
| `--resume` | 关 | 断点续传：从同 URL 最近一次未完成批次继续（需配合 `--history`） |
| `--log-file PATH` | 关 | 启用 logging 并把运行日志写入指定文件 |

### GUI 使用

```bash
python run_gui.py
```

1. 输入问卷 URL（或导入二维码图片自动解析）
2. 设置提交份数与浏览器类型
3. 点击 **🔍 探测题目** 自动识别问卷结构
4. 在权重表格中按题型填写权重（格式见[权重配置](#权重配置)）
5. 可选：点 **⭐ 另存默认** 存为 `configs/default_weight_config.json`，下次启动自动加载
6. 点 **▶ 开始运行**；运行中可随时切到 **📜 历史记录** Tab 查看批次列表与答题明细，**⏹ 停止** 可随时优雅中断

---

## 断点续传

工具在两个层面处理中断，避免重复作答或从头重跑：

**单次提交内（续填）** — 每次打开问卷后先扫描 DOM 已填状态，重试时跳过已答的题，避免重复点击触发反检测。

**跨进程（续传）** — 每个批次在 SQLite 中有一条状态记录（`running / finished / failed / interrupted`）：

- 用户主动停止（GUI 停止按钮 / CLI Ctrl+C）→ 状态记为 `interrupted`，已成功份数保留
- 下次对同一 URL 启动时：
  - **CLI**：加 `--resume` 自动查找 24 小时内最近的未完成批次，复用其批次号与计数、接续答案编号，从第 K+1 份继续
  - **GUI**：自动弹出对话框「Run #N · 已成功 K/M 份 · 是否从第 K+1 份继续？」
- 续传同时会从该批次的权重快照（`weight_config_json`）自动恢复上次使用的权重配置，确保「最后使用的权重就是用户设置的」

异常崩溃的批次记为 `failed`（不可恢复）；只有主动中断的批次可续传。

---

## 工作原理

```
用户配置 URL + 份数 + 权重（CLI 参数 / JSON / GUI 表格）
        ↓
启动 Stealth 浏览器（指纹随机化 + 反检测 JS 注入）
        ↓
打开问卷页面 → JS 注入探测 6 类题型结构
        ↓
（启用续传时）查找同 URL 未完成批次 → 复用批次号与权重快照
        ↓
循环提交：
  ├── 扫描已填题集合，跳过已答题（续填）
  ├── 逐题按权重随机生成答案（填空题按字段类型生成文本）
  ├── JS 注入模拟人类操作（事件链 + 正态分布停顿）
  ├── 每题答案幂等写入 SQLite
  └── 定期检查验证码 → 弹窗等待人工介入
        ↓
模拟点击提交 → 三态判定（OK / FAIL / UNKNOWN）
        ↓
结束落盘：finished（完成）/ interrupted（可恢复中断）/ failed（崩溃）
```

---

## 权重配置

### 方式一：编辑 `src/config.py`

适合脚本固定场景，直接修改 `WEIGHT_CONFIG` 字典：

```python
WEIGHT_CONFIG = {
    1: {"type": "single", "weights": [0.1, 0.3, 0.5, 0.1]},  # 第1题：单选，选项3概率最高
    2: {"type": "multi",  "weights": [0.2, 0.2, 0.3, 0.3],   # 第2题：多选
        "count_options": [2, 3], "count_weights": [0.4, 0.6]},  # 选2个40%，选3个60%
}
```

未配置的题目自动降级为等权重随机。

### 方式二：JSON 配置文件（推荐）

通过 GUI「💾 导出配置 / ⭐ 另存默认」或 CLI `--save-config` 生成，支持导入导出与版本管理：

```json
{
  "schema_version": "2.0",
  "saved_at": "2026-07-04T20:50:00",
  "meta": {
    "name": "客户满意度预设",
    "description": "示例：4 题覆盖不同题型",
    "survey_url": "https://www.wjx.cn/vm/xxxxx.aspx"
  },
  "config": {
    "1": { "type": "single",   "weights": [0.2, 0.5, 0.3] },
    "2": { "type": "multi",    "weights": [0.1, 0.2, 0.3, 0.4],
           "count_options": [2, 3], "count_weights": [0.4, 0.6] },
    "3": { "type": "dropdown", "weights": [0.1, 0.3, 0.6] },
    "4": { "type": "scale",    "scale": 5,
           "weights": [0, 0, 0.1, 0.4, 0.5] },
    "5": { "type": "text",     "field": "name",
           "options": ["张三", "李四", "王五"] },
    "6": { "type": "matrix_single",
           "rows": [1, 2, 3],
           "cols": [1, 2, 3, 4, 5],
           "row_weights": {
             "1": [0, 0, 0.1, 0.4, 0.5],
             "2": [0.1, 0.2, 0.3, 0.3, 0.1],
             "3": [1, 0, 0, 0, 0]
           } }
  }
}
```

各题型的关键字段：

| 题型（type） | 关键字段 | 说明 |
|---|---|---|
| `single`（别名 `radio`） | `weights` | 长度 = 选项数 |
| `multi`（别名 `checkbox`） | `weights`, `count_options`, `count_weights` | 后两者控制「选中几个」 |
| `dropdown` | `weights` | 单选语义 |
| `scale`（别名 `rating`） | `scale`, `weights` | weights 对齐分值 1..N |
| `text`（别名 `textarea` / `fillblank`） | `field?`, `options?` | 留空 options 走内置随机生成 |
| `matrix_single`（别名 `matrix`） | `rows`, `cols`, `row_weights` | 每行独立权重 |

### GUI 中的编辑格式（权重表第 4 列）

- 单选 / 多选 / 下拉：`0.2, 0.5, 0.3`
- 量表：`0,0,0.1,0.4,0.5`，或直接写 `5`（强制打 5 分）
- 填空：`张三,李四,王五`，或留空（按字段类型自动生成）
- 矩阵：`1:0,0,0.1,0.4,0.5 | 2:0.1,0.2,0.3,0.3,0.1 | 3:1,0,0,0,0`

配置加载时会自动校验：权重非负且总和大于 0、数组长度与选项数匹配、NaN / Inf 拒绝、多选「选中个数」分布一致等。

---

## 运行历史

启用 `--history`（GUI 默认启用）后，每次运行写入 `data/history.db`：

- **runs 表** — 批次元信息：`id, survey_url, total_submissions, browser, use_uc, status(running/finished/failed/interrupted), success_count, fail_count, total_elapsed_seconds, error_message, weight_config_json, started_at, finished_at`
- **answers 表** — 逐题明细：`run_id, submission_index, question_number, question_type, options_selected(JSON), text_answer, elapsed_ms, created_at`，`(run_id, submission_index, question_number)` 唯一，重试不会产生重复行

三种使用方式：

1. **GUI** — 「📜 历史记录」Tab：批次列表 → 点击查看答题明细 → 导出 CSV → 清理 7 天前数据
2. **CLI** — `--stats` 在运行结束后打印累计成功率等汇总
3. **导出分析** — GUI「📤 导出 CSV」生成 `history_runs.csv` + `history_runs_answers.csv`，可直接用 Pandas 分析

老数据库文件会被自动迁移（补充新列、去重并建立唯一索引），多次打开幂等无损。

续传相关 API（`src/history.py`）：

| API | 用途 |
|---|---|
| `find_resumable_run(survey_url, max_age_hours=24)` | 找同 URL 最近一次 `interrupted` / `running` 的批次 |
| `mark_interrupted(run_id, ...)` | 主动停止时把状态写为 `interrupted` |
| `count_done_submissions(run_id)` | 读取已完成份数，决定从第几份继续 |
| `start_run(..., weight_config=...)` | 启动批次时持久化权重快照 |
| `SubmissionHistory.deserialize_weight_config(row)` | 反序列化权重快照（str 键 → int，损坏 JSON 返回空 dict） |

---

## 术语表

| 术语 | 含义 |
|---|---|
| **run**（批次） | 一次「从启动到退出」的批量提交任务，对应 `runs` 表一行 |
| **submission**（提交） | 一次完整的「打开问卷 → 答题 → 提交」流程，批次内的子单元 |
| **attempt**（尝试） | 目标成功模式下的一次尝试；失败的尝试不占用成功数 |
| **success_count** | 批次内确认成功的份数 |
| **fail_count** | 批次内确认失败的份数（含保守计入的未知） |
| **target_count** | 用户期望的成功份数或总尝试份数（取决于运行模式） |
| **question** | 问卷中的一道题目 |

---

## 开发与测试

```bash
# 全量测试（含依赖真实浏览器驱动的 E2E）
python -m pytest tests/ -v

# 离线套件（无浏览器环境 / CI，207 项）
python -m pytest tests/ --ignore=tests/test_e2e_integration.py -q
```

当前测试全部通过：**209 项**（离线 207 + E2E 2）。E2E 使用 `tests/fixtures/mock_wjx.html` 作为离线 mock 问卷，但仍需本机 Edge / Chrome + WebDriver 才能跑通全链路。

代码质量：

```bash
python -m ruff check .        # 静态检查（配置见 ruff.toml）
```

推送会触发 GitHub Actions（`.github/workflows/ci.yml`）：ruff + 离线测试。命名约定：布尔变量使用 `is_` / `has_` / `should_` / `use_` 前缀；题型字符串优先经 `models.normalize_question_type` 归一化。

---

## 隐私保护建议

`--no-record-text` 仅控制 SQLite `answers.text_answer` 列的写入；DOM 仍需填入真实文本（否则提交会失败）。如担心本地历史库包含敏感内容：

1. 长期开启 `--no-record-text`，仅记录选项索引与题型
2. 定期使用 GUI「🗑 清理 7 天前」或 `purge_old(days_older_than=7)` 清理
3. 敏感问卷跑完后直接删除 `data/history.db`

---

## 免责声明

本工具仅供学习和研究 Selenium 自动化技术使用。请遵守问卷星平台的使用条款和相关法律法规，不得用于刷票、刷量等任何违规或违法用途。使用者需自行承担所有责任。

## License

[MIT](LICENSE)
