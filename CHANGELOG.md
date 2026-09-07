# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [2.4.0] - 2026-09-07

### 修复

- 修复 v2.3 重构引入的三处 P0 集成回归：
  - `run_one_submission` 必传的 `lock` 参数在 CLI / GUI 两个调用点均漏传，导致 CLI 首次提交即 `TypeError`；
  - `gui/controller.py` 从错误模块导入 `detect_questions` 等符号（实际位于 `src.detection` / `src.verification`），且引用不存在的 `src.utils.qr` 包，「探测题目 / 扫码导入」功能静默失效；
  - `gui/history_panel.py` 引用不存在的列名（`ok_count` / `q_number` / `q_type` / `recorded_at` / `note`）且对 `sqlite3.Row` 误用 `.get()`，历史记录 Tab 无法刷新、导出与清理。
- 提交效果超时不再被误判为成功（保持 2.2 的三态语义并在 2.4 中验证回归）。

### 新增

- CLI `--resume`：断点续传读取侧落地——复用旧 runs 行、成功/失败计数绝对累计、提交序号接续编号；
- CLI `--log-file`：可选 logging 文件日志（新增 `src/logging_setup.py`）；
- `RunState.mark_crashed()` 崩溃语义：未捕获异常的批次在 history 记为 `failed`，不再误标 `finished` 污染成功率；
- `tests/test_cli_batch.py`：批处理主链路冒烟测试（假驱动打通 run_batch，守护 lock 传参等调用方回归）；
- `tests/test_history_gui_contract.py`：GUI ↔ history schema 契约测试，锁定历史面板消费的字段名；
- 工程设施：`pytest.ini`、`conftest.py`、`ruff.toml`、GitHub Actions CI。

### 变更

- GUI 轮间停顿统一为与 CLI 相同的高斯分布，移除均匀随机的 `ROUND_INTERVAL_MIN/MAX`；
- 默认问卷 URL 置空，CLI `-u/--url` 必填（不再内置真实线上问卷）；
- GUI 版本号改由 `src.__version__` 提供，消除版本漂移；
- 单一真相收敛：题型别名（`models.QUESTION_TYPE_ALIASES`）、提交按钮选择器（`_scripts.SUBMIT_SELECTORS`）、浏览器状态清理（`browser.cleanup_browser_state`）；controller 内重复的数题 JS 合并；Edge 驱动工厂复用 `_apply_stealth_cdp`；
- README 重写为标准结构，版本历史移至本文件（CHANGELOG.md）。

## [2.3.0] - 2026-08-23

### 新增

- `src/models.py`：`QuestionType` 题型枚举 + `QuestionData` / `AnswerData` / `SubmitResult` / `WeightConfigEntry` 数据类（`from_dict` / `as_dict` 双向兼容）；
- CLI / GUI 共用的 `RunState` 批次状态对象，状态判定下沉到 `history_status()` / `history_error_message()`；
- `src/exceptions.py` 异常分层：`TRANSIENT_DOM_EXCEPTIONS`（可重试）与 `NON_RECOVERABLE_BASE_EXCEPTIONS`（永不吞），统一 `format_exc_log` 日志格式。

### 变更

- 模块化拆分：`src/interactions/`（按题型）、`src/pipeline_stages/`（按阶段）、`gui/` 面板化（theme / widgets / history_panel / weight_panel / log_view / controller）；
- 布尔命名统一 `is_` / `has_` / `should_` / `use_` 前缀，README 增加术语表。

## [2.2.0] - 2026-08-23

### 修复

- 提交结果三态（`success` / `failed` / `unknown`）：效果超时不再被误判为成功，污染成功率与历史数据；
- `answers` 表幂等写入：`(run_id, submission_index, question_number)` 唯一索引 + `INSERT OR REPLACE` + DELETE+INSERT 兜底，重试不再产生重复答案；老库自动去重迁移；
- CLI `Ctrl+C` 后批次状态记为 `interrupted`（此前为 `running`/`finished`），可被续传正确识别。

### 变更

- 权重配置校验增强：NaN / Inf / 全 0 权重 / choices 长度不匹配 / count 分布一致性 / scale 与 matrix 行长度匹配；
- 新增 `--no-record-text`（隐私：填空文本不落盘）、`--target-success` / `--max-attempts`（厘清「目标成功份数 vs 总尝试次数」）；
- 依赖锁定：`selenium==4.39.0`、`numpy==2.2.4`。

## [2.1.0] - 2026-08-22

### 新增

- 单次提交内断点续填：`detect_answered_questions` 扫描 DOM 已填状态，重试时跳过已答题；
- 跨进程断点续传：`interrupted` 状态 + `find_resumable_run` + GUI 恢复对话框（从第 K+1 份继续）；
- 权重快照持久化：`runs.weight_config_json` 列（自动迁移）、续传时反序列化回填 `WEIGHT_CONFIG` 与 GUI 表格。

## [2.0.0] - 2026-08-23

### 新增

- 六类题型全覆盖：单选 / 多选 / 下拉 / 量表 / 填空 / 矩阵单选，填空题字段自动识别（姓名 / 手机 / 邮箱 / 年龄 / 地址 / 公司）；
- JSON 权重配置导入导出（schema 2.0）与运行时热更新；
- SQLite 运行历史（runs + answers 双表）与全库统计、CSV 导出、过期清理；
- GUI 全面适配：双 Tab（配置 / 历史记录）、探测题目、权重表格编辑。

## [1.0.0] - 2026-07-03

### 首个可用版本

- Edge Stealth 反检测批量填写，单选 / 多选加权随机作答，基础验证码检测与 CLI 批量循环。
