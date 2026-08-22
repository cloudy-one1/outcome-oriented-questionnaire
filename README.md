# 问卷星自动填写工具

> 基于 Selenium Stealth 浏览器的问卷星批量填写与提交工具，支持加权随机策略、人类行为模拟、智能验证码检测 —— 提供 CLI 与 GUI 两种使用方式。

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-2.1.0-brightgreen.svg)](src/__init__.py)

---

## 特性

- **🛡️ 反检测**：CDP 注入 Stealth JS 隐藏 `navigator.webdriver`，随机化浏览器指纹（UA / 屏幕分辨率 / 硬件配置）
- **⚖️ 权重随机**：每道题可配置各选项的选中概率权重，单选加权采样，多选无放回加权抽样
- **👤 人类行为模拟**：正态分布停顿 + 完整事件链（mouseover → mousedown → change → click），3% 概率触发长停顿
- **🤖 智能验证码检测**：三信号并行检测（DOM / URL / iframe+Shadow DOM），弹窗提醒人工介入
- **🖥️ 双入口**：CLI 适合脚本批量运行，GUI 适合可视化配置和实时监控
- **🔄 容错机制**：指数退避重试、浏览器自动重启释放内存、线程安全的人工介入锁
- **🆕 V2 · 6 类题型全覆盖**：单选 / 多选 / 下拉选择 / 量表打分 / 填空 / 矩阵单选（含字段自动识别 name/phone/email/age/address/company）
- **🆕 V2 · 配置文件导入导出**：JSON 配置（schema_version=2.0）+ 启动自动加载默认权重，支持 GUI 一键导入导出
- **🆕 V2 · 运行历史记录**：SQLite 持久化 runs + answers 双表，GUI 「历史记录」Tab 可查询、导出 CSV、清理过期数据
- **🆕 V2 · CLI 增强**：`--config` / `--save-config` / `--history` / `--stats` 四个新参数
- **🆕 V2.1 · 断点续填（单次提交内）**：`detect_answered_questions` 一次 JS 注入扫描 6 类题型已填状态，retry 重试时跳过已答的题（避免重复点击触发反检测）
- **🆕 V2.1 · 断点续传（跨进程批次）**：runs 表新增 `interrupted` 状态 + `find_resumable_run` 接口，下次启动同 URL 时弹「是否从第 K+1 份继续」对话框
- **🆕 V2.1 · 权重持久化**：`runs.weight_config_json` 列存本次批次权重快照，跨进程续传时自动反序列化注入 + 重建 GUI 表格显示，确保"最后完成的权重还是用户设置的"

---

## 快速开始

### 环境要求

- Python 3.9+
- Microsoft Edge 或 Google Chrome 浏览器

### 安装

```bash
git clone <your-repo-url>
cd automation

# 安装核心依赖
pip install -r requirements.txt

# 可选：更强的 Chrome 反检测模式
pip install undetected-chromedriver

# 可选：GUI 二维码 URL 解析
pip install opencv-python
```

### CLI 使用

```bash
# 默认参数运行（使用 config.py 中的默认 URL）
python run_cli.py

# 自定义问卷 URL，提交 10 份
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" -n 10

# 使用 Chrome 浏览器
python run_cli.py -b chrome

# Chrome + undetected-chromedriver 模式（反检测更强）
python run_cli.py -b chrome --uc

# ── V2 新增 ──────────────────────────────────────────
# 使用预设权重配置（JSON 格式，见下文）
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" \
                  --config ./configs/default_weight_config.json

# 运行完把当前权重另存为 JSON 文件（方便分享/版本管理）
python run_cli.py -n 1 --save-config ./configs/my_survey.json

# 启用运行历史记录（写入 data/history.db，默认开启，可显式指定）
python run_cli.py --history ./data/history.db

# 仅查看历史汇总（不运行问卷）
python run_cli.py --stats
```

> **V2.1 续传行为**：CLI 启动时若发现同 URL 下 24 小时内有 `interrupted` 状态的 run，会自动加载上次的权重配置到 `WEIGHT_CONFIG`（但 CLI 当前不弹对话框，需要用户自行决定是否从第 K+1 份继续；GUI 才有交互式恢复对话框）。

### GUI 使用

```bash
python run_gui.py
```

GUI 界面操作流程：

1. 输入问卷星 URL（或导入二维码图片自动解析）
2. 设置提交份数和浏览器类型
3. 点击 **🔍 探测题目** 自动识别问卷结构（单选 / 多选 / 下拉 / 量表 / 填空 / 矩阵）
4. 切换到 **📋 配置** Tab，在权重表格中按题型格式填写：
   - 单选 / 多选 / 下拉：`w1,w2,w3,...`（浮点权重）
   - 量表：`1,1,1,1,5`（按 1..N 分值）或直接填 `5`（强制该分）
   - 填空：`张三,李四,王五`（候选文本，留空走内置随机生成器）
   - 矩阵：`1:1,2,3,4,5 | 2:5,4,3,2,1`（按行写权重）
5. （可选）点 **⭐ 另存默认** 把当前表格存成 `configs/default_weight_config.json`，下次启动自动加载
6. 点 **▶ 开始运行**；运行过程中可随时切到 **📜 历史记录** Tab 查看 runs 列表和答题明细

> **V2.1 断点续传流程**：
> - 运行中点 **⏹ 停止** → 当前 run 标记为 `interrupted`（区别于 `finished` / `failed`），已成功份数 K 不丢失
> - 下次启动同 URL → GUI 自动检测 `find_resumable_run(url)` → 弹对话框"Run #N · 已成功 K/M 份 · 是否从第 K+1 份继续？"
> - 同时自动从 `runs.weight_config_json` 反序列化上次权重 → 注入 `WEIGHT_CONFIG` + 刷回表格（用户可检查/修改后再启动）
> - 点"是" → 进度条立刻显示 `K/M`，从第 K+1 份开始跑，闭合时 `interrupted → finished`

---

## 项目结构

```
automation/
├── run_cli.py                  # CLI 启动入口（V2 支持 --config/--save-config/--history/--stats）
├── run_gui.py                  # GUI 启动入口（赛博朋克极光主题 · V2 双 Tab）
├── requirements.txt            # Python 依赖
├── configs/                    # V2 新增：权重 JSON 配置目录（默认另存位置）
│   └── default_weight_config.json
├── data/                       # V2 新增：历史记录 SQLite 默认目录
│   └── history.db
├── src/
│   ├── __init__.py             # 版本号（v2.1.0）
│   ├── config.py               # 全局配置：权重定义、运行时常量、反检测参数
│   ├── pipeline.py             # 核心编排：单次问卷填写 + 提交流程（含断点续填跳过已答题 + history 可选接入）
│   ├── detection.py            # 题目结构自动探测（JS 注入扫描 6 类题型 DOM + detect_answered_questions 续填扫描）
│   ├── answering.py            # V1：单选/多选 答案生成策略（保留，完全兼容）
│   ├── answering_v2.py         # V2：6 类题型统一 dict 格式答案生成器
│   ├── interaction.py          # DOM 交互层（点击/填空/量表/下拉/矩阵 + 提交按钮）
│   ├── verification.py         # 智能验证码检测与人工等待
│   ├── utils.py                # 工具库：正态分布、指数退避、UA 池、人工介入锁
│   ├── cli.py                  # 命令行入口：批量循环 + V2 参数 + weight_config 持久化
│   ├── config_io.py            # V2：JSON 权重配置读写 + JSON Schema 风格校验
│   ├── history.py              # V2 SubmissionHistory（runs+answers SQLite）+ V2.1 find_resumable_run / mark_interrupted / weight_config 持久化
│   └── browser/
│       ├── driver_factory.py           # Edge/Chrome 驱动 + CDP Stealth 配置
│       └── driver_factory_stealth.py   # Stealth JS 反检测脚本构建器
├── gui/
│   ├── app.py                  # Tkinter GUI 主窗口（双 Tab：配置 / 历史记录）
│   └── qr_utils.py             # 二维码 URL 解析
└── tests/
    ├── test_answering.py       # V1 答案生成逻辑测试
    ├── test_answering_v2.py    # V2 6 类题型答案生成测试（15 项）
    ├── test_cli.py             # CLI 参数解析（含 V2 --config 等，17 项）
    ├── test_config_io.py       # 配置读写 / 校验测试（10 项）
    ├── test_history.py         # SQLite 历史记录测试（18 项，含 V2.1 续传 + 权重持久化 6 项）
    ├── test_detection_resume.py # V2.1 detect_answered_questions 容错测试（6 项）
    ├── test_e2e_integration.py # headless E2E：检测 → 答题 → 交互 → history 全链路
    ├── fixtures/
    │   └── mock_wjx.html       # V2 测试夹具：含 10 题 6 类型的 mock 问卷
    └── test_utils.py           # 工具函数测试
```

---

## 工作原理

```
用户配置 URL + 份数 + 权重（GUI/JSON/config.py）
        ↓
启动 Stealth 浏览器（指纹随机化 + 反检测 JS）
        ↓
打开问卷页面 → JS 注入探测 6 类题型
        ↓
V2.1 检查 find_resumable_run(url)
  ├── 有 interrupted 的旧 run + 用户点"是" → 复用 run_id + 反序列化上次权重
  └── 无 → 全新 start_run（V2.1 同时持久化当前 WEIGHT_CONFIG 到 weight_config_json）
        ↓
循环 N 次 submission（V2.1 续传时从 start_idx 起）：
  ├── detection 返回 questions 列表
  ├── detect_answered_questions 扫描 DOM 已答集合（retry 重试场景）
  ├── for q in questions:
  │     ├── if q.q in answered_set: continue   ← 跳过已答的题
  │     ├── answering_v2.generate_answer(q) 按 WEIGHT_CONFIG 加权
  │     ├── interaction.js_* JS 注入模拟人类操作
  │     └── history.record_answer(...)
  ├── 正态分布随机停顿
  └── 遇验证码 → 弹窗等待人工介入
        ↓
模拟点击提交 → success_count / fail_count 统计
        ↓
用户主动停止 → mark_interrupted（V2.1 续传状态，下次可恢复）
正常完成 → finish_run(status="finished")
异常崩溃 → finish_run(status="failed")
```

---

## 配置权重

### 方式 1：V1 硬编码（适合脚本固定场景）

编辑 `src/config.py` 中的 `WEIGHT_CONFIG` 字典：

```python
WEIGHT_CONFIG = {
    1: {"type": "single", "weights": [0.1, 0.3, 0.5, 0.1]},  # 第1题：单选题，选项2和3概率高
    2: {"type": "multi",  "weights": [0.2, 0.2, 0.3, 0.3],     # 第2题：多选题
        "count_options": [2, 3], "count_weights": [0.4, 0.6]}, # 选2个40%，选3个60%
    # ...
}
```

- `type`: `"single"` 单选 / `"multi"` 多选
- `weights`: 各选项被选中的概率权重（自动归一化）
- `count_options` / `count_weights`: 多选题选中个数的分布
- 未配置的题目自动降级为等权重随机

### 方式 2：V2 JSON 配置（推荐，支持 GUI 导入导出 · 6 类题型完整覆盖）

使用 GUI 的「💾 导出配置 / ⭐ 另存默认」或 CLI `--save-config` 生成标准 JSON：

```json
{
  "schema_version": "2.0",
  "saved_at": "2026-07-04T20:50:00",
  "meta": {
    "name": "客户满意度预设",
    "description": "示例：4 题覆盖不同题型",
    "author": "张三",
    "survey_url": "https://www.wjx.cn/vm/xxxxx.aspx"
  },
  "config": {
    "1": { "type": "single",       "weights": [0.2, 0.5, 0.3] },
    "2": { "type": "multi",        "weights": [0.1, 0.2, 0.3, 0.4],
           "count_options": [2, 3], "count_weights": [0.4, 0.6] },
    "3": { "type": "dropdown",     "weights": [0.1, 0.3, 0.6] },
    "4": { "type": "scale",        "scale": 5,
           "weights": [0, 0, 0.1, 0.4, 0.5] },
    "5": { "type": "text",         "field": "name",
           "options": ["张三", "李四", "王五"] },
    "6": { "type": "text",         "field": "phone" },
    "7": { "type": "matrix_single",
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

各题型字段说明：

| 题型 (type) | 关键字段 | 说明 |
| --- | --- | --- |
| `single` / `radio` | `weights: List[float]` | 长度 = 选项数 |
| `multi` / `checkbox` | `weights`, `count_options`, `count_weights` | 后两者控制"选中几个" |
| `dropdown` | `weights` | 单选语义 |
| `scale` / `rating` | `scale: int`, `weights` | weights 对齐分值 1..N |
| `text` / `textarea` / `fillblank` | `field?`, `options?` | 留空 options 走内置随机句生成 |
| `matrix_single` / `matrix` | `rows`, `cols`, `row_weights` | 每行独立权重 |

### GUI 中的编辑格式（权重表第 4 列）

对应上面 6 类题型分别输入：

- single/multi/dropdown：`0.2, 0.5, 0.3`
- scale：`0,0,0.1,0.4,0.5`，或直接写 `5`（强制打 5 分）
- text：`张三,李四,王五`，或留空（按 field 自动生成 name/phone/email/age/address/company）
- matrix：`1:0,0,0.1,0.4,0.5 | 2:0.1,0.2,0.3,0.3,0.1 | 3:1,0,0,0,0`

---

## 运行历史（V2）

每次运行会在 `data/history.db` 生成两张表：

- **runs**：`id, started_at, finished_at, status, survey_url, total_submissions, ok_count, fail_count, note, weight_config_json (V2.1)`
- **answers**：`run_id, submission_index, q_number, q_type, options_selected(JSON), text_answer, elapsed_ms, recorded_at`

三种使用方式：

1. **GUI**：切到「📜 历史记录」Tab → 顶部 runs 表 → 点击某一行 → 下方 answers 明细刷新
2. **CLI**：`python run_cli.py --stats` 打印汇总，`--history PATH` 指定 DB 路径
3. **数据科学家模式**：在 GUI 里用「📤 导出 CSV」导出 `history_runs.csv` + `history_runs_answers.csv`，Pandas 自由分析

默认保留策略：GUI 的「🗑 清理 7 天前」按钮调用 `SubmissionHistory.purge_old(days=7)`。

### V2.1 新增：断点续传 + 权重持久化 API

| API | 用途 |
|---|---|
| `find_resumable_run(survey_url, max_age_hours=24)` | 找同 URL 下最近一次 `interrupted`/`running` 的 run（断点续传入口） |
| `mark_interrupted(run_id, success_count, fail_count, ...)` | 用户主动停止时调用，把 status 写为 `interrupted`（区别于 finished/failed） |
| `count_done_submissions(run_id)` | 读 runs.success_count（用于决定从第几份继续） |
| `start_run(..., weight_config=dict)` | 启动批次时把当前 WEIGHT_CONFIG 序列化为 JSON 存入 `weight_config_json` 列 |
| `SubmissionHistory.deserialize_weight_config(row)` (静态) | 反序列化 row 里的 weight_config_json，键名 str → int，损坏 JSON 返回空 dict |

老 DB 自动迁移：第一次打开时 `_apply_migrations()` 用 `PRAGMA table_info` 检测到缺 `weight_config_json` 列 → 自动 `ALTER TABLE ADD COLUMN`。幂等，多次打开不会报错。

---

## 运行测试

```bash
# V1 + V2 + V2.1 全量单元 + 集成 + E2E（headless，不需要 GUI）
python -m pytest tests/ -v

# 仅跑 V2 新增（answering_v2 + config_io + history + e2e）
python -m pytest tests/test_answering_v2.py tests/test_config_io.py \
                 tests/test_history.py tests/test_e2e_integration.py -v

# 仅跑 V2.1 断点续传相关（detection 续填容错 + history 续传/权重持久化）
python -m pytest tests/test_detection_resume.py tests/test_history.py -v
```

当前测试全部通过：**93/93** —— V1 (38) + V2 answering_v2/config_io (25) + V2 history (12) + V2 E2E (2) + V2.1 续传与权重持久化 (12) + utils (4)。E2E 使用 `tests/fixtures/mock_wjx.html` 离线跑通检测 → 答题 → 交互 → history 的完整链路。

---

## 免责声明

本工具仅供学习和研究 Selenium 自动化技术使用。请遵守问卷星平台的使用条款和相关法律法规，不得用于任何违规或违法用途。使用者需自行承担所有责任。

---

## License

MIT License — 详见 [LICENSE](LICENSE) 文件。
