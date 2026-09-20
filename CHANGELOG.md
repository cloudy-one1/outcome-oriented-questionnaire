# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [2.6.0] - 2026-09-20

评估报告的后续迭代批次：把 2.5 里判定"留给下轮"的 15 项逐一做完。
这一轮的重心从"修单个缺陷"转向"消除让缺陷重复出现的结构原因"。

### 变更（架构收敛）

- **GUI 不再复刻批处理循环**：`gui/app.py::_run_loop` 从 **172 行降到 57 行**，
  改为调用 `src.cli.run_batch`。后者新增四个接缝参数：
  `state`（外部 RunState）、`on_round`（每轮回调）、`log`（输出目的地）、
  `stop_check`（优雅停止轮询）。
  重复的代价付过两次：v2.4 的 `lock` 漏传要改两处，v2.5 的
  `no_record_text` / 单轮异常韧性 / 崩溃优先级又是三处双份修改。
  附带收益：`gui/app.py` 自身不再直接 import selenium，也不再知道
  `InvalidSessionIdException` / `RESTART_BROWSER_EVERY` / `human_pause`
  这些批次内部概念（浏览器依赖收敛到 `gui/controller.py` 与 `src/`），
  批次语义只剩一份实现。
- **加权无放回抽样合一**：A-Res / numpy 两套副本上收为
  `utils.weighted_sample_no_replace` + `equal_sample_no_replace`，
  与 v2.5 已统一的非法权重判定共用同一把尺子。
- **题型命名表合一**：`QuestionType.from_str` 内那份手写 alias 映射与
  `QUESTION_TYPE_ALIASES` 此前平行维护（矩阵题 `matrix_single` vs `matrix`
  正是漂移高发点），现统一由 `_STORAGE_NAMES` + `_ALIASES` 派生，
  并加用例保证两张视图不可能再各自演化。
- **`ManualHoldLock` 的文档契约终于落地**：README 一直宣称"人工介入锁保证
  等待期间不被误判为超时"，但 `is_holding` / `wait_until_released` 生产代码
  从未调用。现在 `_wait_for_questions` 在 holding 期间暂停计时预算 ——
  此前验证码在题目等待阶段弹出时，15s 超时会把用户拉到一半的滑块判成本轮失败。
- **`--no-record-text` 的下拉题语义已澄清并固化**（不扩范围）：
  `text_answer` 列的 dropdown 分支存的是**问卷页面的 option 文案**（站点内容），
  不是用户输入；把它一起抹掉会让历史明细失去可读性。
  代码注释与 README 均已写明该边界，真要匿名化站点文案应另开开关。

### 修复

- **量表起点不是 1 时识别与点击双双出错**（v2.6 新增 2~10 分 mock 题暴露）：
  `detection` 把格子数当 `scale`、并硬编码 `scale_min = 1`，于是 2~10 分量表
  被读成 `scale=9 / scale_min=1` —— **永远点不到 10，还会去点不存在的 1**。
  现优先按隐藏 radio 的真实 value 区间取边界；`set_scale_script` / `js_set_scale`
  也补上 `scale_min` 参数，策略 B 的下标换算由 `val - 1` 改为 `val - scaleMin`。
- **非法权重可能崩掉整批**：合一后的采样器顺带堵上一个新暴露的洞 ——
  numpy 的 `p=` 抽样在"正权重个数 < k"时抛
  `ValueError: Fewer non-zero entries in p than size`（如多选权重 `[0,0,1]` 而要选 2 个）。
  现自动改走带零权重下限的 A-Res 路径。
- **权重配置写入语义三处不一**：`gui/app.py` 的两处 `clear()+update()` 与
  `gui/controller.py` 的裸 `update()`（合并，会让上一份配置的题号静默残留）
  全部改走 `apply_weight_config(cfg, replace=True)`，与 CLI 一致。
- **孤儿 `running` 批次会被当作可续传**：进程被强杀 / 断电时收尾代码根本没跑，
  `runs` 行永停 `running`，而 `find_resumable_run` 把它算作可恢复 ——
  下次启动会提示续传一个页面与浏览器状态完全未知的批次。
  新增 `SubmissionHistory.reap_stale_runs()`，CLI 与 GUI 启动时各调一次
  （60 分钟阈值，避免误杀另一个进程正在跑的批次）。
- **`driver.quit()` 失败导致浏览器进程孤儿化**：CLI 侧现会强杀 WebDriver
  服务进程并留一行日志；`driver_factory` 补上两处
  "驱动已建好、初始化步骤随后失败"的回收窗口（UC 回退路径此前每失败一次
  就泄漏一个真实 Chrome 窗口）。
- **SQLite 并发与重复全表扫描**：连接现启用 WAL + 显式 `busy_timeout`；
  迁移改按 `PRAGMA user_version` 记账，v2.2 那次含全表
  `DELETE ... NOT IN (SELECT MAX(id) ...)` 的去重只在真正需要升级的老库上跑一次，
  不再每次开库（GUI 每点一次历史 Tab）都重扫。
  唯一索引创建失败也不再静默吞掉，改为 `logger.warning`。
- **`sqlite3.Cursor.lastrowid` 可能为 None** 却直接 `int(...)`：
  现由 `_require_lastrowid()` 给出可定位的错误信息。
- **停止按钮响应延迟**：`human_pause` 新增 `abort_check`，轮间停顿
  （最长 20s、此前是一次性 sleep）改为分片睡眠并逐片询问，
  由 `run_batch(stop_check=...)` 透传。

### 安全

- **最后一处未转义的 JS 插值已消除**：`click_option_script` 的 `q` / `choice`
  改走 `json.dumps` 成 JS 字面量，selector 在 JS 运行时拼接。
  审计确认此前不可利用（detection 把选项值 parseInt 成整数），
  但那道边界依赖上游数据形状 —— 现在不依赖了。
  配套新增"值不得逃出字面量"的用例，并把 selector 拼接位置写成断言。

### 新增（测试与门禁）

- **E2E 现在真的提交**：mock 问卷新增 1 道题（第 11 题，2~10 分量表）与一段
  模拟问卷星 AJAX 提交的 JS（URL 不变、延迟渲染成功文案、记录点击次数）。
  E2E 从 2 项增至 **5 项**，新增覆盖：`run_one_submission` 完整链路、
  提交恰好一次、URL 读取抖动不得触发重点击、`--no-record-text` 落盘为 NULL。
  **它当场抓到本会话引入的一次回归** ——
  `input[name='q' + q + '']` 是合法 JS（`node --check` 过得去）但运行时报
  非法 CSS selector。这条经验已写进 README：语义级错误只有真浏览器能兜。
- **pyright 门禁**：新增 `pyrightconfig.json`（basic / py310 / 纳入 `src/` 与入口脚本）。
  `src/` 现为 **0 error 0 warning**，无需 baseline。达成它顺手修掉 10 条真实类型问题，
  并删掉 4 处已无对象的 `# type: ignore`。
- **覆盖率地板**：CI 加 `--cov-fail-under=43`（实测 44%），只许上调；
  README 新增「已知缺口」表，诚实列出 `logging_setup.py` 0%、
  `driver_factory.py` 9%、`gui/` 13~35% 等现状。
- **`requirements-dev.txt`**：钉住 pytest / pytest-cov / ruff 版本
  （此前 CI 用 `pip install pytest ruff` 不钉版本，门禁口径随上游发版漂移）。
- `tests/test_pipeline_waits.py`（6 项）：`_wait_for_questions` 与人工介入锁的
  契约，含"不传锁时同样场景必须超时"的对照组，避免把"暂停"测成"永不超时"。
- `tests/test_history.py` 新增 5 项：迁移不重复执行（用触发器计数证明）、
  孤儿批次改判、阈值内不误杀、WAL 与 busy_timeout 生效。
- `tests/test_utils.py` 新增 `abort_check` 用例；GUI 测试改用 stub host
  而非真实 `tk.Tk()`（同进程反复建销 Tcl 解释器会在 Windows 间歇性 TclError，
  用它当测试基座只会得到一套随机变红的用例），运行时间从 7.2s 降到 0.3s。

### 已知缺口（未在本轮处理）

- `gui/` 的 33 条 pyright 诊断（Tkinter 子类赋值风格为主），纳入门禁需先做
  一轮纯风格改造。
- `models.py` 的 4 个题型 dataclass **判定为不迁移**（理由已写进模块 docstring），
  保留为契约文档；漂移隐患已消除。
- 4 个入口脚本级的 `argparse` 行为差异（GUI 与 CLI 的默认份数、默认浏览器
  来源不同）未统一。

## [2.5.0] - 2026-09-20

全面评估后的整改批次。重点是**四个已复现的正确性缺陷**——其中两个会直接造成
问卷站上的重复提交，一个会让用户 PII 默默落盘并处于可提交状态。

### 修复（P0）

- **CLI `--resume` 此前是静默空操作**：`main()` 算出 `resume_run_id/resume_done/resume_fail`
  并打印续传横幅，却一个都没传给 `run_batch`，实际从第 1 份重跑新批次。
  后果是在问卷站上重复提交、历史裂成两条、旧行永停 `interrupted`。
  现补齐转发，并顺带修正两处同源缺陷：续传沿用上次计划总份数（此前会按默认 17 重新规划）、
  从批次快照恢复权重配置（此前仅 GUI 有、CLI 从未实现）。
  > 同时勘误：2.4.0 条目所称「CLI `--resume` 断点续传读取侧落地」并不成立，读取侧接线在本版本才完成。
- **提交成功之后仍可能整体重试，导致同一份问卷被二次提交**：
  `_do_one_submission_core` 在判定成功后裸调 `driver.switch_to.default_content()`，
  提交跳转使该调用抛 `WebDriverException` 时，会冒泡进
  `run_one_submission` 的 `@retry_with_backoff` 把整份问卷重填重交；
  `find_and_click_submit` 整体被 `@js_execute_retry(max_attempts=3)` 包住，
  而有副作用的点击与其后的 URL 确认混在同一重试区域内，最坏点击 6 次。
  现拆为 `_click_submit_button`（可安全重试，点击即返回）+ 重试区外的效果确认，
  成功路径的清理不再可能上抛。回归测试实测旧代码点击 **4 次**。
- **GUI 无条件把填空题原文写入 SQLite**：`no_record_text` 在 `gui/` 里根本不存在，
  而 GUI 默认启用 `data/history.db`。现新增「🔒 不记录填空文本」开关并**默认开启**，
  透传至 `run_one_submission`。
- **`.gitignore` 未覆盖任何运行产物**：`data/history.db`（含填空原文）、
  `configs/*.json`（含用户写的姓名/手机/邮箱候选池）、导出 CSV 全部处于可提交状态。
  现补 `data/`、`configs/`、`*.db`、`*.sqlite*`、`*.csv`。
- **非法权重会让整批任务在第一道题崩掉**：全 0 权重在 numpy 分支除零
  （`answering.py` 缺失 `answering_v2` 早已修过的守卫），NaN/Inf 权重使
  `random.choices` 抛 `ValueError`——两者都不在 `TRANSIENT_DOM_EXCEPTIONS` 内。
  现统一降级为等权重并留一行可见日志；CLI 侧 `--config` 改为**加载即校验、不通过即拒绝运行**
  （此前 CLI 完全不调 `validate_weight_config`）。
- **`set_scale_script` 在 `scale_max=None` 时输出语法非法的 JS**（`var scaleMax = ;`），
  触发 `JavascriptException` → 重试 3 次 → 该题记失败。现输出 `null`，
  正好命中 JS 里已有的 `if (!scaleMax) scaleMax = items.length` 兜底。

### 修复（P1）

- **单轮异常不再终止整批**：CLI/GUI 此前只接 `InvalidSessionIdException`，
  一次常见的 `TimeoutException` 就会 `mark_crashed` → 批次记 `failed` →
  `find_resumable_run` 拒绝续传，前面已成功的份数被静默丢弃。
  现降级为该份失败并继续，`NoSuchWindowException`（用户手关窗口）纳入浏览器重建分支。
- **关闭 GUI 窗口不再孤儿化浏览器**：无 `WM_DELETE_WINDOW` 处理且 `_run_loop` 是
  daemon 线程，窗口一关其 `finally` 根本不执行 —— 既漏 `driver.quit()`，
  也把 `runs` 行永久留在 `running`（而 `running` 被当作可续传，给出错误的续传）。
  现请求停止 → join（上限 30s）→ 关闭历史库连接 → 销毁窗口。
- **验证码人工介入提示不再能挂死整批**：`force_focus()` 在 worker 线程上调用同步模态
  `MessageBoxW`，无人点确定则永久阻塞，且 Win32 模态框吃不到 Ctrl+C、GUI 停止按钮也打不断。
  现改为守护线程弹窗 + `FindWindowW`/`WM_CLOSE` 定时自动关闭，等待上限交还给
  `wait_for_manual_verification` 自己的 `timeout_seconds`。实测调用返回 1.1ms。
- **`HistoryPanel.get_db()` 每次调用新建且从不关闭连接**：每次构造都会重跑含全表
  `DELETE ... NOT IN (SELECT MAX(id) ...)` 的迁移，把一次全表扫描放到 Tk 主线程上；
  连接句柄持续泄漏；且读写分属不同连接使 `SubmissionHistory` 内部那把
  「串行化所有 DB 操作」的锁完全失效。现改为进程内单实例缓存 + `close_db()`。
- **`src/models.py`（纯数据层）反向依赖 Selenium 栈**：`import src.models` 会连带加载
  100+ 个 `selenium.*` 模块。`SubmitOutcome` 及三个常量已上移到 `models.py`，
  `interactions/submit.py` 反向再导出以保持既有调用点不变。
- **崩溃批次不再被误标为可续传**：GUI 收尾时只要 `stop_flag` 置位就走 `mark_interrupted`，
  忽略 `crash_message`——「用户点了停止、在途那轮又抛了异常」会被记成 `interrupted`
  并丢掉崩溃信息。现与 `RunState.history_status()` 一致：崩溃优先于中断。
- **`find_resumable_run` / `query_runs` 缺排序兜底**：`started_at` 由 `datetime('now')`
  写入、**只有秒级精度**，同秒内建的多个批次排序不确定，`LIMIT 1` 可能挑到旧批次。
  现补 `id DESC` 兜底；回归测试实测旧代码在 5 个同秒批次里挑中了 id=1 而非 5。
- **`driver.quit()` 失败被静默吞掉**：改为 `_quit_quietly()` 留一行日志，
  使孤儿浏览器进程至少可诊断（长批次内存堆积后 quit 恰恰最容易失败）。

### 变更

- **`apply_weight_config` 新增 `replace` 参数**：此前同一操作在三处入口语义不同
  （`config_io` 合并 / `gui/app` 替换 / `gui/controller` 合并），同一个 `--config`
  文件在 CLI 与 GUI 会产生不同分布。CLI `--config` 与续传快照恢复现统一为**整体替换**。
- **`ruff.toml` 增开 `F401/F841/F541`** 并清理存量 25 处告警。理由是把
  「参数算了却没传」这类零误报缺陷纳入 CI——`--resume` 静默失效正是 `F841` 一条就能拦住的。
- **`target-version` 由 `py39` 改为 `py310`**，README 的「Python 3.9+」同步改为 3.10+；
  此前 README、ruff 目标版本与 `@dataclass(slots=True)` 的 3.10 真实下限三者互相矛盾。
- **CI 改用 `-m "not integration"` 取代 `--ignore`**，并新增非阻塞 `e2e` job；
  主测试矩阵扩展为 `3.10` + `3.13`。E2E 此前被按路径硬关掉，而它本身就会在缺浏览器时自 skip。
- 验证码提示框增加 `MB_SETFOREGROUND | MB_TOPMOST`，并支持 `auto_close_seconds`。

### 新增（测试）

- `tests/test_cli_main.py`（9 项）：**走 `main()` 的入口接线测试**。
  旧套件全部直接调 `run_batch`，所以「argparse 解析出来了但没往下传」这一整类 bug 不可见 ——
  这正是 `--resume` 存活的原因。经变异测试验证：移除修复后 4 项立即转红。
- JS 语法真值校验：`_SCRIPT_CASES` 覆盖 8 个脚本生成器的边界输入，
  `node --check` 实际解析每段产出的 JS（本机无 node 时降级为退化片段检测，仍然有效）。
  新增元测试确保后续新增的 `*_script` 生成器不会漏出这道防线。
  同时修正 `test_js_scripts.py` 中把 `var scaleMax = ;` **锁进断言**的 bug-certifying 用例。
- `run_batch` 批次韧性：单轮 `TimeoutException` 后仍跑满剩余份数、
  `NoSuchWindowException` 重建浏览器继续（2 项）。
- 点击次数守卫：`TestNoDoubleClickOnWaitFailure`（2 项），含「基线 URL 读不到时不得白记一次成功」。
- 权重清洗：`TestWeightsUsable`（4 项），含 v1/v2 生成器端到端只降级不抛异常。
- 排序稳定性：同秒内 5 个批次的 `query_runs` 与 `find_resumable_run` 兜底（1 项）。
- **首批 GUI 测试** `tests/test_gui_run_loop.py`（v2.5 起 4 项，v2.6 扩到 8 项）：`gui/` 此前零覆盖（约占生产代码 41%），覆盖参数透传（lock / no_record_text）、单轮异常不终止整批、崩溃优先于中断的收尾优先级；无可用显示时整模块自动 skip。
- 离线套件从 207 → **256 项**，全量 **258 项**通过。

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
