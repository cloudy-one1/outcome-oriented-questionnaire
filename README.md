# 问卷星自动填写工具

> 基于 Selenium Stealth 浏览器的问卷星批量填写与提交工具，支持加权随机策略、人类行为模拟、智能验证码检测 —— 提供 CLI 与 GUI 两种使用方式。

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-2.2.0-brightgreen.svg)](src/__init__.py)

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
- **🆕 V2.2 · 提交三态（审查整改 P1-1）**：`find_and_click_submit` / `_wait_until_submit_effect` 返回 `success` / `failed` / `unknown` 三态；超时不再误判为成功，由上层保守计为失败并单独打印 `UNKNOWN` 便于事后复盘（避免污染 success_count / 成功率 / 历史数据）
- **🆕 V2.2 · answers 幂等化（审查整改 P1-2）**：`(run_id, submission_index, question_number)` 唯一索引 + `INSERT OR REPLACE` + DELETE+INSERT 兜底，retry 重试同一份提交不会产生重复答案；老库自动 dedup 迁移保留 `max(id)`
- **🆕 V2.2 · CLI 中断语义（审查整改 P1-3）**：Ctrl+C → `status='interrupted'`（而非 `running`/`finished`），保证下次 `find_resumable_run` 能正确恢复
- **🆕 V2.2 · 配置校验增强（审查整改 P2-1）**：`validate_weight_config` 检测 NaN / Inf / 全 0 权重 / choices 长度不匹配 / count_options 与 count_weights 不一致 / scale 长度不匹配 / matrix row_weights 行长度不匹配 + 总和>0
- **🆕 V2.2 · CLI 隐私 + 语义（审查整改 P2-2/P2-3）**：`--no-record-text` 不把填空答案写入 SQLite（避免明文保存姓名/手机/邮箱）；`--target-success` / `--max-attempts` 厘清"目标份数 vs 总尝试次数"语义
- **🆕 V2.2 · 依赖锁定（审查整改 P2-4）**：`requirements.txt` 锁定 `selenium==4.39.0` / `numpy==2.2.4`，可选依赖也给出实测可用版本

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

# ── V2.2 审查整改新增 ──────────────────────────────
# 隐私保护：不把填空题答案写入 SQLite（避免明文保存姓名/手机/邮箱等敏感内容）
python run_cli.py --history ./data/history.db --no-record-text

# 语义厘清：把 -n 10 解释为「目标成功 10 份」而非「总尝试 10 次」
# 循环会持续到成功数达标；--max-attempts 防止极端失败场景下死循环（默认 = -n * 2）
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" -n 10 \
                  --target-success --max-attempts 30
```

> **V2.2 提交三态语义**：每次提交输出 `OK` / `FAIL` / `UNKNOWN` 三种标签之一：
> - `OK`：URL 变化或页面出现"提交成功 / 感谢您的参与"等关键词 → 已确认成功（计入 `success_count`）
> - `FAIL`：按钮定位失败 / 验证码未通过 / iframe 失败 / 题目探测失败 → 已确认失败（计入 `fail_count`）
> - `UNKNOWN`：按钮已点击但等待效果超时（可能是 AJAX 异步提交，也可能服务端拒绝/校验失败/网络异常）→ 保守计为失败并单独打印一行 `[统计] 其中 N 次提交结果未知…` 便于事后复盘。**关键修复（审查 P1-1）**：旧版超时被误判为成功，污染了 success_count / 成功率 / 历史数据。
>
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
│   ├── __init__.py             # 版本号（v2.2.0）+ 模块变更日志
│   ├── config.py               # 全局配置：权重定义、运行时常量、反检测参数
│   ├── pipeline.py             # 核心编排：单次问卷填写 + 提交流程（含断点续填跳过已答题 + history 可选接入 + V2.2 三态返回 + no_record_text 透传）
│   ├── detection.py            # 题目结构自动探测（JS 注入扫描 6 类题型 DOM + detect_answered_questions 续填扫描）
│   ├── answering.py            # V1：单选/多选 答案生成策略（保留，完全兼容）
│   ├── answering_v2.py         # V2：6 类题型统一 dict 格式答案生成器
│   ├── interaction.py          # DOM 交互层（点击/填空/量表/下拉/矩阵 + 提交按钮 V2.2 三态 SubmitOutcome）
│   ├── verification.py         # 智能验证码检测与人工等待
│   ├── utils.py                # 工具库：正态分布、指数退避、UA 池、人工介入锁
│   ├── cli.py                  # 命令行入口：批量循环 + V2 参数 + V2.2 no_record_text/target_success/max_attempts + interrupted 状态
│   ├── config_io.py            # V2：JSON 权重配置读写 + JSON Schema 风格校验 + V2.2 NaN/Inf/长度匹配增强校验
│   ├── history.py              # V2 SubmissionHistory（runs+answers SQLite）+ V2.1 续传/权重持久化 + V2.2 record_answer 幂等化 + dedup 迁移
│   └── browser/
│       ├── driver_factory.py           # Edge/Chrome 驱动 + CDP Stealth 配置
│       └── driver_factory_stealth.py   # Stealth JS 反检测脚本构建器
├── gui/
│   ├── app.py                  # Tkinter GUI 主窗口（双 Tab：配置 / 历史记录 · V2.2 三态结果统计）
│   └── qr_utils.py             # 二维码 URL 解析
└── tests/
    ├── test_answering.py       # V1 答案生成逻辑测试
    ├── test_answering_v2.py    # V2 6 类题型答案生成测试（15 项）
    ├── test_cli.py             # CLI 参数解析（含 V2 --config 等 + V2.2 新参数 7 项，共 24 项）
    ├── test_config_io.py       # 配置读写 / 校验测试（含 V2.2 增强校验 18 项，共 28 项）
    ├── test_history.py         # SQLite 历史记录测试（含 V2.2 幂等化 + dedup 迁移 4 项，共 27 项）
    ├── test_interaction_submit.py # V2.2 提交三态（success/failed/unknown）单元测试（7 项，fake driver）
    ├── test_detection_resume.py # V2.1 detect_answered_questions 容错测试（6 项）
    ├── test_e2e_integration.py # headless E2E：检测 → 答题 → 交互 → history 全链路（依赖真实浏览器驱动）
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
模拟点击提交（V2.2 三态 success/failed/unknown，超时计 unknown→fail）
  → success_count / fail_count 统计
        ↓
用户主动停止 → mark_interrupted（V2.1 续传状态，下次可恢复）
正常完成 → finish_run(status="finished")
异常崩溃 → finish_run(status="failed")
Ctrl+C → finish_run(status="interrupted")（V2.2 修复，下次可恢复）
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

## V2.2 审查整改清单

针对批判式审查报告的全部问题已逐项整改，对应代码位置见下表：

| 审查编号 | 问题摘要 | 整改方案 | 涉及文件 |
|---|---|---|---|
| **P1-1** | `_wait_until_submit_effect` 超时被误判为成功（`return True`），污染 `success_count` / 成功率 / 历史数据 | 提交结果改三态 `SubmitOutcome = Literal["success","failed","unknown"]`；超时返回 `"unknown"`，由上层保守计为失败并单独打印 `UNKNOWN` | [interaction.py](src/interaction.py#L35-L38) · [pipeline.py](src/pipeline.py) · [cli.py](src/cli.py) · [gui/app.py](gui/app.py) |
| **P1-2** | pipeline 重试同一份提交时 `record_answer` 产生重复 answers 行（无唯一约束） | 双层幂等：`(run_id, submission_index, question_number)` 唯一索引 + `INSERT OR REPLACE`，老库 dedup 迁移保留 `max(id)`；DELETE+INSERT 兜底应对索引创建失败的极端场景 | [history.py](src/history.py#L88-L102) · [history.py record_answer](src/history.py#L315-L363) |
| **P1-3** | CLI `KeyboardInterrupt` 后状态只设为 `running`/`finished` 而非 `interrupted`，导致 `find_resumable_run` 无法识别可恢复的 run | 显式 `interrupted` 标记，`finally` 块根据该标志写 `status='interrupted'` + `error_message='Ctrl+C 用户中断'` | [cli.py run_batch](src/cli.py#L352-L384) |
| **P2-1** | `validate_weight_config` 校验不足（缺 NaN/Inf、全 0、长度匹配检查） | 重写为四个子校验函数：`_is_finite_number` / `_validate_weights_array` / `_validate_count_options` / `_validate_scale_length` / `_validate_matrix_row_weights`，全面覆盖 NaN/Inf/非负/总和>0/长度匹配 | [config_io.py](src/config_io.py) |
| **P2-2** | 目标份数 / 尝试份数 / 成功数语义不清 | 新增 `--target-success` / `--max-attempts`，`run_batch` 循环改为 `while idx < attempts_cap`，target_success 模式下达成目标数提前跳出 | [cli.py](src/cli.py#L143-L160) |
| **P2-3** | 历史数据明文保存姓名 / 手机 / 邮箱等敏感内容 | 新增 `--no-record-text`，pipeline 在 `no_record_text=True` 时给 `record_answer` 传 `text_answer=None`，SQLite `text_answer` 列写 NULL；DOM 仍填入实际文本（流程需要） | [cli.py](src/cli.py#L135-L142) · [pipeline.py](src/pipeline.py) |
| **P2-4** | `requirements.txt` 依赖版本过宽（`selenium>=4.0`、`numpy<2.0`） | 锁定 `selenium==4.39.0` + `numpy==2.2.4`（升到 2.x，旧版与 Python 3.13 不兼容），可选依赖也注明实测可用版本 | [requirements.txt](requirements.txt) |
| **文档** | README 声称 93/93 但实际 91 passed 2 skipped（E2E 被跳过） | 修正为 130/130 实际通过，新增「E2E 依赖真实浏览器驱动」说明 + `--ignore` 跳过参数 | [README.md](README.md)（见上文「运行测试」段） |

### 隐私保护说明（P2-3）

`--no-record-text` 仅控制 SQLite `answers.text_answer` 列的写入；DOM 仍需填入真实文本（否则问卷提交会失败）。默认 `False`（旧行为，写入文本便于事后复盘）。如担心本地 SQLite 被他人访问，建议：

1. 长期开启 `--no-record-text` —— 仅记录选项索引与题型，不落盘任何文本
2. 搭配 GUI 「🗑 清理 7 天前」或 `purge_old(days=7)` 定期清理老 run
3. 敏感问卷（含姓名/手机/身份证）跑完后立即删除 `data/history.db`
4. 如需完全可复现构建，参考 `requirements.txt` 顶部注释用 `pip freeze > requirements.lock` 生成完整锁定文件

### answers 幂等化迁移说明（P1-2）

老 DB 第一次被 V2.2 代码打开时，`_apply_migrations` 会按以下顺序执行：

1. V2.1：检测 `runs` 表缺 `weight_config_json` 列 → `ALTER TABLE ADD COLUMN`（幂等）
2. V2.2：执行 `_DEDUP_ANSWERS_SQL` —— 删除重复行只保留 `MAX(id)` 那条（避免建唯一索引失败）
3. V2.2：`CREATE UNIQUE INDEX IF NOT EXISTS idx_answers_unique ON answers(run_id, submission_index, question_number)`

> **设计要点**：唯一索引不放在 `_SCHEMA_SQL` 里（`executescript` 无条件执行，老库若有重复行会直接失败），而是放在 `_apply_migrations` 中先 dedup 再创建，保证幂等。即使极端情况下 dedup 没清干净导致索引创建失败，应用层 `INSERT OR REPLACE` + `_record_answer_fallback`（DELETE+INSERT）的兜底逻辑依然保证幂等。

---

## 运行测试

```bash
# V1 + V2 + V2.1 + V2.2 全量单元 + 集成 + E2E（headless，不需要 GUI）
python -m pytest tests/ -v

# 跳过依赖真实浏览器驱动的 E2E（CI 友好，无浏览器环境也能跑）
python -m pytest tests/ --ignore=tests/test_e2e_integration.py -q

# 仅跑 V2 新增（answering_v2 + config_io + history + e2e）
python -m pytest tests/test_answering_v2.py tests/test_config_io.py \
                 tests/test_history.py tests/test_e2e_integration.py -v

# 仅跑 V2.1 断点续传相关（detection 续填容错 + history 续传/权重持久化）
python -m pytest tests/test_detection_resume.py tests/test_history.py -v

# 仅跑 V2.2 审查整改相关（三态提交 + 幂等化 + 增强校验 + CLI 新参数）
python -m pytest tests/test_interaction_submit.py tests/test_history.py \
                 tests/test_config_io.py tests/test_cli.py -v
```

当前测试全部通过：**130/130** ——
- V1 `test_answering` (6) + `test_utils` (15) = 21
- V2 `test_answering_v2` (15) + `test_config_io` (28) + `test_history` (27) + `test_e2e_integration` (2) = 72
- V2.1 `test_detection_resume` (6)
- V2.2 `test_interaction_submit` (7) + `test_history` 幂等化 (4) + `test_config_io` 增强校验 (18) + `test_cli` 新参数 (7) = 36（含跨文件）

> **E2E 测试依赖真实浏览器驱动**：`tests/test_e2e_integration.py`（2 项）使用 `tests/fixtures/mock_wjx.html` 作为离线 mock 问卷，但仍需要本机装好 Edge / Chrome + 对应 WebDriver 才能跑通 Selenium 全链路（检测 → 答题 → 交互 → history）。无浏览器环境或 CI 上建议加 `--ignore=tests/test_e2e_integration.py` 跳过这两项，其余 128 项可在纯 Python 环境下完整通过。
>
> 审查报告原先提到「README 声称 93/93 但实际 91 passed 2 skipped」的问题已在 V2.2 整改中修正：现在 130 项全部通过，不再有 skipped 项，README 测试数与实际一致。

---

## 免责声明

本工具仅供学习和研究 Selenium 自动化技术使用。请遵守问卷星平台的使用条款和相关法律法规，不得用于任何违规或违法用途。使用者需自行承担所有责任。

---

## License

MIT License — 详见 [LICENSE](LICENSE) 文件。
