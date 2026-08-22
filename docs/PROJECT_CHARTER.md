# 问卷星自动填写工具 · 项目宪章（Project Charter）

> **文档编号**：PC-WJX-AUTOMATION-2026-001  
> **宪章版本**：v1.0  
> **对应项目版本**：v2.0.0  
> **编写日期**：2026-07-04  
> **生效状态**：✅ 草稿 → ⚠️ 待评审 → ✅ 已批准（见文末签字区）

---

## 1. 项目基本信息

| 项 | 内容 |
| :--- | :--- |
| 项目名称 | 问卷星自动填写工具（Survey Auto-Fill for WJX） |
| 项目代号 | WJX-Automation |
| 当前版本 | `v2.0.0`（SemVer，发布分支：`release/v2.0.0`，锚定 tag `v1.0.0`） |
| 项目类型 | 研究型 / 个人开源 · 自动化工具 |
| 发起部门 / 发起人 | 个人研究项目 · 开发者（见利益相关者） |
| 项目代码仓库 | 本地：`e:\practise\ppp.py\automation`；远程：待用户指定 |
| 技术栈 | Python 3.9+ / Selenium / Tkinter / SQLite / Pytest |
| 宪章批准人 | （见 §11 签字区） |
| 下一次宪章评审日 | `2026-09-04` 或 V2.1 启动前（以更早者为准） |

---

## 2. 项目背景与目标

### 2.1 业务背景

问卷星（WenJuanXing）是国内广泛使用的在线调研平台，在用户研究、市场投放、教学评测等场景中，调研运营方常需要**验证问卷结构、校验问卷跳转逻辑、压力测试数据落库**，或在经授权的场景下以受控方式批量填充基准样本数据。传统手工逐份填写存在三大痛点：

1. **低效**：典型 10 题问卷人工一份 60s，100 份需超 1.5 小时；
2. **不可重复**：人工随机分布不稳定，无法保证多次验证时的答案分布可比；
3. **易触发风控**：手工高频提交仍可能被平台风控拦截，缺乏可解释的"人类行为"模拟基线。

本项目旨在以 Selenium + 反检测 Stealth 技术为核心，提供**可复现、可配置、可追溯**的批量问卷填写与提交能力，同时兼顾研究教育意义。

### 2.2 高层目标（SMART）

| # | 目标 | 可量化指标 | 截止日期 |
| :--- | :--- | :--- | :--- |
| G1 | **正确性**：稳定支持 6 类常见问卷题型的探测→作答→提交 | `single/multi/scale/dropdown/text/matrix_single` 全部通过 headless E2E | 2026-07-10 |
| G2 | **可配置**：权重/题型/规模/默认值均支持 JSON 配置文件导入导出 | GUI 可一键导入导出，schema_version=2.0 可校验 | 2026-07-10 |
| G3 | **可追溯**：每次运行写入 runs + answers 双表 | SQLite 历史记录，GUI History Tab 可查询 | 2026-07-10 |
| G4 | **可观测**：CLI + GUI 双入口，运行日志实时可见 | GUI 日志面板 ≥ 6 色分类；CLI --stats 汇总 | 2026-07-10 |
| G5 | **合规免责**：项目文档明确"仅供研究学习，不得违规使用"免责声明 | README + CLI --help 内嵌免责说明 | 随 v2.0 同步 |

### 2.3 禁止用途（红线）

**本项目严禁用于：**
- 未获得问卷所有者/平台授权的真实线上问卷刷票、刷单、伪造受访者数据；
- 违反《网络安全法》《个人信息保护法》《反不正当竞争法》等法律法规的任何场景；
- 绕过平台付费能力、或进行容量破坏式的 DoS 攻击。

---

## 3. 范围边界

### 3.1 In-Scope（v2.0 交付范围）

- **Layer 1 检测**：`detection.py` 基于 JS 注入识别 6 类题型 DOM（单选/多选/量表/下拉/填空/矩阵单选）；
- **Layer 2 作答**：
  - V1：`answering.py`（单选/多选）—— 向后兼容；
  - V2：`answering_v2.py`（6 类题型统一 dict 输出 + 内置中文姓名/手机/邮箱/地址生成器）；
- **Layer 3 交互**：`interaction.py` 新增 `js_fill_text / js_set_scale / js_select_dropdown / js_fill_matrix_single`；
- **编排**：`pipeline.py` 统一串联，兼容 V1 流程；
- **历史**：`history.py` 实现 `SubmissionHistory`（SQLite runs + answers）；
- **配置 IO**：`config_io.py` 实现 save/load/validate/apply；
- **GUI**：`gui/app.py` 赛博极光主题，双 Tab（配置 / 历史记录），题型感知 Badge，导入导出按钮；
- **CLI**：`cli.py` + `run_cli.py` 新增 `--config / --save-config / --history / --stats`；
- **测试**：
  - V2 模块单元测试 36 项（answering_v2/config_io/history）；
  - headless E2E 2 项（`tests/fixtures/mock_wjx.html` 10 题覆盖 6 类）；
- **文档**：README.md（版本徽标→2.0.0，V2 功能/配置格式/历史记录说明）、本宪章。

### 3.2 Out-of-Scope（v2.0 明确排除）

1. **多选矩阵 / 评分矩阵 + 多级联动题 / 逻辑跳转题**：DOM 结构复杂，v2.1 之后再评估；
2. **图片/附件上传**：浏览器上传窗口非 DOM 自动化范畴；
3. **滑块/滑动验证**：需独立的行为轨迹模型研究，属 v3.x；
4. **真实账号登录 + Cookie 持久化**：暂以访客问卷为主；
5. **移动端 App / 微信小程序版本问卷**：仅支持桌面 Web 浏览器；
6. **跨平台 macOS / Linux 图形界面集成测试**：代码保持可移植（Python 标准库 + Selenium 跨平台），但 GUI 像素级兼容测试不在 v2.0 范围；
7. **问卷发布端、数据分析可视化 Dashboard**：仅作答端工具。

### 3.3 假设与约束

- **Assumption A**：问卷星桌面 Web 端 DOM 结构保持当前语义（`div.field`、`ul/li` 选项、`select` 下拉、`div.ui-slider` 量表），若平台改版需升级 detection.js；
- **Assumption B**：用户本地已安装 Edge/Chrome 浏览器，Python 3.9+ 可用；
- **Constraint 1**：不得引入闭源或与 MIT License 冲突的第三方付费 SDK；
- **Constraint 2**：GUI 必须基于 Tkinter（Python 自带）以降低分发门槛，不得强制 PyQt/GTK 等 GUI 依赖；
- **Constraint 3**：默认反检测方案为 CDP Stealth JS + undetected-chromedriver（可选），不使用商业代理/指纹服务。

---

## 4. 利益相关者（Stakeholders）

| 角色 R & R 标识 | 姓名/名称 | 职责（Responsibility） | 权力（Authority） | 沟通频率 | 评审/批准 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 项目负责人 / 主程 | 开发者 | 架构设计、编码、测试、版本发布 | ✅ 合并代码、创建分支与 Tag | 按需 | ✅ 需签署批准本宪章 |
| 最终用户（研究人员） | 使用者 | 提出需求、反馈 Bug、验证功能 | ✅ 发起 CR、优先级投票 | 里程碑节点 | ➖ |
| 代码评审人 | 同侪 | PR Review、安全审查、Conventional Commits 规范审查 | ✅ 在合并前可驳回 | 每次提交 | ✅ 需签署批准本宪章 |
| 平台方（问卷星） | 第三方 | 提供问卷运行环境；若反馈不当使用可要求下线 | ➖ 外部干系人 | 重大版本前 | ➖ |
| 开源社区（若公开仓库） | Contributors | 提交 Issue / PR、贡献文档 | ✅ 提建议 | 每周汇总 | ➖ |
| 安全 & 合规 | 评审人 | 审查反检测能力与使用条款合规性 | ✅ 可叫停违反免责条款的功能 | 大版本前 | ✅ 需签署批准本宪章 |

---

## 5. 关键里程碑（Milestones）

| 里程碑 | 版本 | 交付内容 | 状态（v2.0 宪章时点） | 计划日期 |
| :--- | :--- | :--- | :--- | :--- |
| M0 · 启动立项 | v0.1 | 章程初稿 + 脚手架 + config/cli 骨架 | ✅ 已完成 | 2026-06-10 |
| M1 · V1 MVP | v1.0.0 | 单选/多选 2 类题型 + 基础 GUI + 基础 CLI | ✅ 已完成（Tag v1.0.0 @ commit a93a30e） | 2026-06-25 |
| M2 · 版本治理 | Branch | 创建 `release/v2.0.0` 分支 + 建立 Tag 规范 | ✅ 已完成 | 2026-07-01 |
| M3 · V2 模块开发 | v2.0.0-dev | detection/interaction/pipeline answering_v2/config_io/history + 对应单元测试 | ✅ 已完成（36 项通过） | 2026-07-03 |
| M4 · E2E 离线冒烟 | v2.0.0-rc1 | mock_wjx.html（10 题 6 类）+ test_e2e_integration 2 项全链路 | ✅ 已完成（75/75 通过） | 2026-07-04 |
| **M5 · GUI 适配 & 文档交付** | v2.0.0-rc2 | 6 类题型权重表 + 配置 IO 按钮 + 历史记录双 Tab + README + 项目宪章 | 🚧 **当前阶段交付** | 2026-07-04 |
| M6 · 正式发布 | v2.0.0 GA | 上述 GA + 推送到远程 + 创建 Release Tag v2.0.0 | ⬜ 待执行 | 宪章批准后 1 工作日 |
| M7 · v2.1 规划启动 | v2.1.0-dev | 矩阵多选 + 逻辑跳转 + 数据导出 XLSX 评估 | ⬜ 待立项 | 2026-08-01 |

---

## 6. 资源分配

### 6.1 人力资源预算

| 角色 | 投入（人日） | 分配说明 |
| :--- | :--- | :--- |
| 主程（架构+编码+测试+文档） | 15 | V2 开发全程（M2→M6），M6 占 3 人日 |
| 代码评审 | 2 | 交叉评审提交、安全走查 |
| 测试验证（GUI 人工走查） | 1 | GUI 6 类题型实际交互 |

### 6.2 技术与基础设施资源

| 类别 | 选择 | 说明 |
| :--- | :--- | :--- |
| 编程语言 | Python 3.9+ | CI 优先 3.13（当前本机 3.13.2） |
| 浏览器驱动 | Selenium 官方 + EdgeDriver/ChromeDriver + undetected-chromedriver（可选） | 通过 `src/browser/driver_factory.py` 抽象 |
| 测试框架 | Pytest 9.1 | headless E2E：Chrome headless=new |
| GUI 框架 | Tkinter（stdlib） | 不引入额外 GUI 依赖 |
| 持久化存储（配置） | JSON (UTF-8, no BOM) | `configs/default_weight_config.json` |
| 持久化存储（历史） | SQLite 3（stdlib） | `data/history.db`，不引入 ORM |
| 版本治理 | Git · GitFlow-lite | `main`（稳定）/ `release/vX.Y.Z`（发布冻结）/ feature 分支 |
| 日志追踪 | 终端 stdout + 内存 deque GUI buffer | V2 不引入文件日志（避免多路径权限问题） |

### 6.3 预算与合规

- 项目类型：非盈利研究性质；预算 ¥ 0（无服务器/商业工具采购）；
- 合规义务：使用时必须遵守问卷星使用条款与相关法律法规；
- 免责机制：README 第 § 免责声明 + GUI/CLI 启动提示。

---

## 7. 风险登记与应对策略（Risk Register）

| # | 风险描述 | 可能性 | 影响 | 风险等级 | 应对策略（Mitigation / Contingency） | 责任人 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| R1 | 问卷星前端 DOM 改版 → detection.js 误判 | 中 | 高 | 🟧 高 | **缓解**：detection 用 JS IIFE + 兜底 fallback（空 list 返回，不抛异常）+ E2E 夹具；**应急**：升级 detection，打 patch release。 | 主程 |
| R2 | 反检测能力失效导致批量提交被拦截 | 中 | 中 | 🟧 高 | **缓解**：CDP Stealth JS + UC 可选 + 人类行为模拟（正态停顿、3% 长停）+ 自动重启浏览器释放内存；**应急**：切到 UC 模式或拉长间隔。 | 主程 |
| R3 | 长时间运行内存泄漏（WebDriver + 日志） | 低 | 中 | 🟨 中 | **缓解**：`RESTART_BROWSER_EVERY=N` 轮次后强制 quit + 重启；GUI 日志队列有最大行数上限（不无限增长）。 | 主程 |
| R4 | 用户把工具用于违规场景引发法律风险 | 中 | 极高 | 🟥 高 | **缓解**：README 免责声明、GUI 启动 banner、CLI --help 打印"合规用途提示"；**应急**：若被滥用，即刻下线公开仓库、停止发行。 | 负责人 |
| R5 | V1→V2 向后兼容性破坏 | 低 | 高 | 🟧 高 | **缓解**：V1 single/multi 分支完全保留 answering.py 原逻辑；新增 answering_v2 独立模块。 | 主程 |
| R6 | SQLite 文件并发写入损坏（多 GUI 实例同时跑） | 低 | 中 | 🟨 中 | **缓解**：history 每次写入 commit；**应急**：损坏时 GUI 提供「🗑 清理」按钮 + 允许用户换 db 路径。 | 主程 |
| R7 | GUI 跨平台（mac/Linux）Tk 主题差异导致视觉 Bug | 低 | 低 | 🟩 低 | **缓解**：使用 platform 无关的 Canvas 绘制胶囊/Badge，不依赖原生控件。 | 主程 |
| R8 | 推送失败（远程未配置、认证错误） | 中 | 低 | 🟨 中 | **缓解**：提交前先 `git remote -v` 验证；推送失败时在交付中明确给出 `git push` 的完整错误输出，不伪造成功。 | 主程 |

---

## 8. 沟通计划

| 沟通事项 | 形式 | 频率 | 受众 | 媒介 / 工具 |
| :--- | :--- | :--- | :--- | :--- |
| 进度同步（M2→M6 期间） | 每日站会（async OK if 单人） | 每日 1 次 10 分钟 | 主程 | TodoWrite / 提交消息 |
| 里程碑评审 | 文档评审会（或走查） | 每个 Mx 节点 | 全部利益相关者 | 项目宪章 §11 签字 + Release Notes |
| Bug / 需求 CR | Issue 追踪 | 按需 | 用户 / 社区 | GitHub Issues（若公开仓库）|
| 安全走查 | 专项评审 | 大版本前一次 | 安全 & 合规 | 书面评审记录 |
| 变更公告 | Release Notes + Git Tag | 每次发布 | 用户 + 社区 | Tag 注释 + CHANGELOG.md（v2.1 引入） |

**文档仓库结构（v2.0）：**

```
automation/
├── README.md                 # 用户可见文档（本版已 V2 化）
├── docs/
│   └── PROJECT_CHARTER.md    # 本宪章（正式文档，评审后锁定）
└── src/__init__.py           # __version__ 单一信源（v2.0.0）
```

---

## 9. 变更控制流程（Change Request Process）

任何对交付范围、里程碑、资源、使用策略的重大变动，必须走 CR（Change Request）流程：

1. **提交 CR**：提交人以书面 Markdown 形式（或 Issue）说明 **"变更原因 / 变更内容 / 对里程碑与资源的影响 / 风险"**；
2. **Impact Analysis**：主程在 1 工作日内给出《影响分析》，标注「范围扩大 / 纯文档 / 测试变更 / 兼容性」；
3. **决策**：
   - 小变更（测试补充、文档修正、低风险 Bug Fix）→ 主程直接批准；
   - 大变更（新增题型、重写 detection、引入新依赖）→ 至少获得宪章批准人 1 名签字；
   - 涉及合规红线的变更 → 安全 & 合规干系人一票否决。
4. **执行 & 记录**：批准后对应到新的 feature 分支，PR 合并后更新本宪章（附录 A 变更记录）。

---

## 10. 成功标准（Success Criteria & Acceptance）

### 10.1 功能验收（对应 §2.2 SMART 目标）

| 验收编号 | 验收标准 | 验证方式 | 当前状态（v2.0 GA 时点） |
| :--- | :--- | :--- | :--- |
| AC1 | 6 类题型 detection 可识别 | pytest `test_detection_discovers_all_10_questions` 通过 | ✅ 已通过 |
| AC2 | 6 类题型 answering + interaction 可填写 | pytest `test_full_pipeline_fill_and_history` 通过 | ✅ 已通过 |
| AC3 | JSON 配置 save/load 往返一致 | pytest `test_save_and_load_roundtrip` 通过 | ✅ 已通过 |
| AC4 | 配置结构非法能返回警告 | pytest `test_validate_rejects_bad_key` 等通过 | ✅ 已通过 |
| AC5 | history.start_run → record_answer → finish_run → query_runs/answers 闭环 | pytest `test_subquery_by_run` / `test_stats_*` 通过 | ✅ 已通过 |
| AC6 | 运行历史可在 GUI「📜 历史记录」Tab 查看 | 人工走查：选 runs 行 → answers 明细刷新 | ✅ 代码就绪（待签署后人工走查） |
| AC7 | GUI 配置「📂 导入 / 💾 导出 / ⭐ 另存默认」按钮可用 | 人工走查：导出→清→导入→权重一致 | ✅ 代码就绪（待签署后人工走查） |
| AC8 | 启动标题、徽章、日志均显示 v2.0.0 | 检查 README 徽标 / src/__init__.py / APP_VERSION / 窗口标题 | ✅ 已通过 |
| AC9 | CLI `--config / --save-config / --history / --stats` 不报错 | pytest test_cli（已覆盖 --browser/--uc/--url/--count） | ✅ 基线通过，V2 参数走查通过 |
| AC10 | 免责声明存在且明确 | README § 免责声明 章节非空且提及条款与法律 | ✅ 已存在 |

### 10.2 测试与质量门禁

项目进入 GA 的硬性条件：

- **全量测试**：`python -m pytest tests/ -v` **100% 通过**（当前 75/75 通过 ✅）；
- **语法 & 类型**：GUI 代码 `py_compile` 成功，IDE diagnostics 0 错误；
- **安全**：未提交任何秘密（密钥 / PAT / 真实问卷 URL / 个人隐私数据）；
- **可构建**：`requirements.txt` 中所有依赖当前版本可用。

### 10.3 OKR（v2.0 发布季度）

- **O1：把工具研究可复现性提升一个台阶**
  - KR1：≥ 6 类题型稳定支持（=100% 6/6）；
  - KR2：≥ 90% 单元测试覆盖率（按函数计），V2 新模块均有测试；
  - KR3：配置/历史/运行日志三大"可追溯"能力全部落地。
- **O2：使用者体验提升**
  - KR1：GUI 不崩溃、探测阶段不抛 V2 题型 KeyError；
  - KR2：JSON 配置 save-load 往返一致；
  - KR3：历史记录 CSV 可用（Excel 打开不乱码，含 utf-8-sig BOM）。

---

## 11. 宪章评审与批准签字区

> 评审说明：下列利益相关者签名（或在协作平台批注"已审阅/同意"）后，本宪章 v1.0 正式生效。若采用异步电子评审，需保留完整邮件/评论记录作为附件。

### 11.1 评审记录

| 评审人角色 | 评审人 | 评审日期 | 评审结论（通过 / 附意见通过 / 驳回） | 意见摘要 |
| :--- | :--- | :--- | :--- | :--- |
| 项目负责人 / 主程 | （填写姓名） | YYYY-MM-DD | ⬜ 通过 | —— |
| 代码评审人 | （填写姓名） | YYYY-MM-DD | ⬜ 通过 | —— |
| 安全 & 合规评审 | （填写姓名） | YYYY-MM-DD | ⬜ 通过 | —— |
| 最终用户代表 | （填写姓名） | YYYY-MM-DD | ⬜ 通过 | —— |

### 11.2 批准签字

| 批准角色 | 批准人 | 签字（电子等价） | 批准日期 |
| :--- | :--- | :--- | :--- |
| 项目发起人（最终批准） | （填写姓名） | —— | YYYY-MM-DD |

---

## 附录 A · 宪章修订记录

> 本宪章修改后必须记录，以确保可追溯。

| 版本 | 修改日期 | 修改人 | 修改摘要（ROI / CR 编号） |
| :--- | :--- | :--- | :--- |
| v1.0 | 2026-07-04 | 主程 | 初稿，对应 v2.0.0 GA 交付节点。 |

---

## 附录 B · 参考文档（References）

1. [Semantic Versioning 2.0.0](https://semver.org/lang/zh-CN/)
2. [Conventional Commits 1.0.0](https://www.conventionalcommits.org/zh-hans/v1.0.0/)
3. [问卷星 · 使用条款](https://www.wjx.cn/)（用户访问时以实时版本为准）
4. Python 3.13 标准库 · sqlite3 / tkinter / json 模块官方文档
5. Selenium 文档：WebDriver / JavaScript 注入 / WebDriverWait 用法

