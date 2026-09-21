# 项目全面评估审查报告

**审查对象**：问卷星自动填写工具（`automation`，版本 **v2.7.0**）
**审查日期**：2026-09-21
**审查方式**：全量源码走读（src / gui / tests / CI / 文档）+ 在干净环境中**实际运行**测试与质量门禁复核
**复核环境**：Windows、Python **3.14.7**（高于声明下限 3.10 与 CI 的 3.13）、Edge 浏览器

---

## 一、总体结论

| 项 | 结论 |
|---|---|
| **综合评级** | **A−（8.5 / 10）—— 工程化成熟度明显高于一般个人/小团队项目** |
| 能否使用 | 可以。核心链路经真浏览器 E2E 验证，质量门禁齐备且**实测属实** |
| 最突出的优点 | 测试是"防真实历史回归"而非凑数；安全注入防护成体系；文档极度诚实 |
| 最该先修的问题 | ① `config.py` 内置一份 22 题特定问卷权重，会**默认污染所有 CLI 运行**；② `pipeline.py` 4 处兜底 `except` 静默吞掉纯代码异常，违背项目自己定的异常契约 |
| 最大技术债 | `gui/` 约 44 条 pyright 诊断（30 error）未纳入类型门禁（已在 README 诚实记录） |
| 合规前提 | 属 dual-use 工具，**仅限学习/授权测试**；批量代填可能违反问卷星条款，见第六节 |

一句话：**这是一个"知道自己哪里没测、哪里可能坏、并把它写下来"的项目**，可信度高；扣分项集中在少量默认配置卫生、个别异常吞咽点和 GUI 类型债，均不致命、可快速修复。

---

## 二、客观验证结果（审查者实际执行，非引用文档）

| 检查 | 命令 | 实测结果 | 与 README 声明对照 |
|---|---|---|---|
| 离线测试 | `pytest tests/ -m "not integration"` | **491 passed, 2 skipped, 0 failed**（2 skip 为未装可选 opencv） | README 称离线 493 → 实为 493 collected（491+2），口径一致 ✅ |
| 浏览器 E2E | `pytest tests/ -m integration` | **5 passed in 15.32s**（真实 Edge，headless 跑本地 mock 问卷） | README 称 E2E 5 项，属实 ✅ |
| 静态检查 | `ruff check .` | **All checks passed** | 属实 ✅ |
| 类型检查（src+入口） | `npx pyright`（按 pyrightconfig） | **0 error, 1 warning** | README/CI 称"0 error 0 warning"，实测多 1 条可选依赖 import warning ⚠️ |
| 类型检查（gui） | `npx pyright gui` | **30 error, 14 warning**（未纳入门禁） | README 称 45 条（30e+15w），版本差异，量级一致 ✅ |
| 覆盖率 | `pytest --cov=src --cov=gui` | **TOTAL 71%**（src≈84%、gui≈58%） | README 写 72%、ci.yml 注释写 71%，本机 71%，过 70 门禁 ⚠️ |
| 高版本兼容 | Python 3.14.7 全量跑 | 离线 + E2E 全绿 | 声明 3.10+，在更新版本上同样通过 ✅ |

> 说明：依赖按钉定版本安装（selenium 4.39.0 / pytest 9.1.1 / ruff 0.16.6），numpy 因 3.14 兼容性装到 2.5.3（requirements 钉 2.2.4），未影响任何结果。

---

## 三、分维度评分

| 维度 | 评分 | 评价要点 |
|---|---:|---|
| 功能完整性 | **9.0** | 6 类题型、加权随机、人类行为模拟、验证码三态检测、断点续填/续传、SQLite 历史、CLI+GUI 双入口，功能闭环 |
| 架构与代码质量 | **8.5** | 分层清晰（编排/阶段/交互/驱动/数据），DRY 做得好；少量历史残留与死代码锚点 |
| 测试与质量保障 | **9.0** | 496 测试含真浏览器 E2E、`node --check` JS 语法防线、注入回归、覆盖率 ratchet；扣分在 gui 无类型门禁 |
| 可靠性/健壮性 | **8.0** | 三态提交、指数退避、孤儿回收、driver 泄漏强杀、关窗 join；扣分在 pipeline 静默吞异常与续传语义小瑕 |
| 安全性 | **8.5** | JS/SQL 注入、CSV 公式注入、隐私开关、`.gitignore` 防泄露均到位；默认权重残留属数据卫生问题 |
| 可维护性/文档 | **9.0** | README/CHANGELOG/注释极详尽且诚实，命名规范统一；gui 类型债、dataclass 死锚点略增维护成本 |
| 工程化/可复现 | **9.0** | 依赖钉版本、CI 双 Python 矩阵（3.10 验证真实下限）、非阻塞 E2E job、autouse 替身防误开真浏览器 |
| 合规/伦理 | **6.5** | 有免责声明、默认 URL 置空，姿态正确；但工具本质 dual-use，可被用于刷问卷/污染调研数据 |

---

## 四、架构与数据流（核查后确认）

```
入口                编排                    阶段实现                     外部
run_cli.py ┐                                     ┌─ page_loader     浏览器(Edge/Chrome)
           ├─ src.cli.main / run_batch ───────── ├─ verification     Selenium 4 + Stealth CDP
run_gui.py ┘   (RunState 状态机, 唯一批次语义)    ├─ question_stage   [可选] undetected-chromedriver
 gui.app      GUI _run_loop 仅薄封装一次调用 ↑     ├─ detection (注入JS)
                                               └─ interactions      SQLite(data/history.db)
submit 三态: success / failed / unknown(保守计败)   choices/scale/     JSON 权重配置(schema 2.0)
                                                  text/dropdown/
                                                  matrix/submit
```

- **批次语义只有一份实现**：GUI `_run_loop` 已从 170 行手抄副本收敛为对 `cli.run_batch` 的一次调用（V2.6），消除了"修一处漏一处"。
- **题型分工**：单选/多选走 `answering.build_answer_strategy`（v1，仍是生产主路径），填空/量表/下拉/矩阵走 `answering_v2.generate_answer`；加权抽样算法已上收 `utils.weighted_sample_no_replace` 单一实现。
- **命名单一真相**：题型别名、三态常量、版本号、状态机分别集中在 `models.py`，避免散落漂移。

---

## 五、问题清单（按优先级，含定位与修复建议）

### P2 — 建议尽快修复

#### P2-1　默认权重配置污染所有 CLI 运行（且与文档承诺不符）
- **定位**：`src/config.py` L32-59（`WEIGHT_CONFIG` 内置 Q1–Q22 权重）；`src/cli.py` L818；`src/answering.py` L57。
- **现象**：`config.py` 残留了一份**特定真实问卷**的 22 题权重（分布很偏，如 Q5 选项 4 权重 `0.64`、Q6 选项 4 `0.60`）。CLI **不传 `--config`** 时从不调用 `apply_weight_config(replace=True)` 清空，而 `build_answer_strategy` 直接读全局 `WEIGHT_CONFIG`，于是**任何问卷的前 22 道单选/多选题都会默认套用这份来历不明的分布，而非 README 承诺的"未配置→等权重随机"**；L818 还会把它写入批次快照。
- **GUI 基本免疫**：`app._on_start` 未探测题目时会显式 `apply_weight_config({}, replace=True)`。
- **附带**：L33 注释 `# ... existing config entries remain unchanged ...` 是代码生成工具的占位符残留，不应入库。
- **建议**：把 `WEIGHT_CONFIG` 默认置为 `{}`；将该示例移到 `configs/weight_config.example.json`（README 已引用 `configs/`，但目录当前为空）。

#### P2-2　`pipeline.py` 4 处兜底 `except` 静默吞掉纯代码异常
- **定位**：`src/pipeline.py` L109-111、L138-140、L205-207、L270-272，模式均为：
  ```python
  except Exception as _e:
      raise_non_recoverable(_e)
      pass
  ```
- **为何是问题**：`TRANSIENT_DOM_EXCEPTIONS` 已把 `WebDriverException` 基类纳入，因此能走到 `except Exception` 的几乎只剩 `KeyError/TypeError/ValueError/AttributeError` 这类**纯 Python bug**。而 `exceptions.py` 的契约（L11、L65-66）明确写着这些应"向上抛、保留堆栈"；`raise_non_recoverable` 只放行 Ctrl+C/SystemExit/MemoryError，对这些 bug 不动作，随后 `pass` **无日志吞掉**。这也违背项目 V2.4 自己定的"静默路径至少 `logger.debug` 留痕"规范。
- **对照正确写法**：`question_stage.py` L133-139 在同样位置调用了 `print(format_exc_log(...))`。
- **建议**：统一改为记录（`logger.debug(..., exc_info=True)` 或 `format_exc_log`）；对真正的数据契约错误直接 `raise`，避免掩盖缺陷。

### P3 — 计划内修复

| 编号 | 定位 | 问题 | 建议 |
|---|---|---|---|
| P3-1 | `gui/`（9 文件） | pyright **30 error + 14 warning**，未纳入 CI 类型门禁（最大一组是失效的 `# type: ignore` 与 Tkinter 动态赋值写法） | 分批清零后把 `gui` 纳入 `pyrightconfig`，先以 error 为门禁 |
| P3-2 | README L307 / ci.yml L37 | 称 src "0 error **0 warning**"，但 CI 不装可选依赖 `undetected-chromedriver`，实测 **0 error + 1 import warning**（warning 不致 CI 失败） | 改为"0 error（可选依赖缺失时有 1 条环境性 warning）"，或对该行精确 `# type: ignore` |
| P3-3 | README L308 vs ci.yml L45 | 覆盖率一处写 **72%**、一处写 **71%**，本机实测 71% | 统一口径并注明 Python 版本 |
| P3-4 | `cli.py`（target-success 续传） | `attempts_cap = base_cap - resume_done` 只减成功数、不减失败数；"最大尝试次数"口径下续传后上限会偏多 `resume_fail` 次（不致死循环，影响小） | 明确口径：按"尝试次数"续传则减 `done+fail`，或在文档说明按成功数续 |
| P3-5 | `interactions/_scripts.py` 成功判定 | 成功关键词含较弱的"已完成"，提交后 6s 窗口内若正文含该词，理论上可能把失败**误判为成功**（偏危险方向）；URL 变化与"提交成功/感谢参与"为强信号 | 弱化/移除"已完成"，或要求强信号 + 多条件命中 |

### Nit — 顺手可改

- `src/cli.py` L388：`from .interaction import SUBMIT_SUCCESS  # noqa: F401` 为**未使用的死导入**，可删。
- `src/verification.py`：非 Windows 降级桩写的是 `except ImportError`，但 Linux/mac 上 `import ctypes` 也成功，访问 `ctypes.windll` 在调用期才抛 `AttributeError`，故该桩不可达（实际靠函数内 `try/except Exception` 兜底，不崩溃）。建议改为 `except (ImportError, AttributeError)` 或 `hasattr(ctypes, "windll")` 探测。README 已诚实标注"剩 3 行不可达降级桩"。
- iframe 适配（`page_loader` / `controller`）只遍历**同级**顶层 frame，不支持嵌套 iframe（问卷星罕见）；建议注释明示该限制。
- `src/models.py`：4 个 dataclass（约 250 语句）**无生产调用点**，作者已明确其定位为"契约文档 + 未来重构锚点"并记录了"第 2/3 步判定不做"的理由。保留合理，但提示其持续维护/同步成本。
- `answering_v2` 量表默认权重 `[1,2,4,6,5]` 对 >5 级量表尾部补 `max=6`，略粗糙（可用）。
- `history.close()` 后无 closed 守卫，关闭后再调用会 `AttributeError`（当前生命周期清晰、风险低）。

---

## 六、值得肯定的工程实践（这些是真实防线，不是摆设）

1. **提交防重复点击（P0 级）**：把"有副作用的点击"与"点击后的效果确认"分离，`old_url=None` 防导航抖动误判；并有真浏览器回归 `test_submit_does_not_reclick_when_page_is_busy`、`__submitClicks==1` 断言。
2. **三态提交语义**：`success/failed/unknown`，unknown（按钮已点但未见成功信号）保守计败但单独计数，避免虚报成功。
3. **成体系的注入防护**：注入 JS 的外部值一律 `json.dumps` 固化（防闭合字符串注入）、SQL 全参数化、CSV 对 `= + - @ TAB CR` 前缀加 `'` 防公式注入；并有专门的注入回归用例。
4. **隐私默认**：GUI 默认 `no_record_text=True`（填空姓名/手机/邮箱不落 SQLite），CLI/GUI 默认值差异在 README 显式说明；`data/`、`*.db`、`*.csv` 进 `.gitignore`。
5. **资源与状态收尾**：driver 进程泄漏强杀、启动时 `reap_stale_runs` 回收孤儿批次、关窗 `join(timeout=30)`、断点续传复用 `run_id` 并快照权重。
6. **测试针对真实历史缺陷**：lock 必传参数断链、单轮异常导致整批夭折、手关窗口后重建、量表 `scale_min` 越界点错格、selector 被拼进 JS 字符串字面量等，都有对应回归。
7. **质量门禁设计有想法**：`F401/F841/F541` 作为"接线断链"防线（曾一条规则拦住 `--resume` 静默失效）；`node --check` 真校验生成 JS + "新增脚本生成器必须纳入语法用例"的元测试；覆盖率地板只许上调。
8. **文档诚实度罕见**：明确列出"已知缺口/覆盖率数字/为什么还留着"，并直言"覆盖率不等于验证过，100% 是替身跑出来的"。

---

## 七、合规与用途风险（dual-use 提示）

- 该工具可对问卷星进行**批量自动填写与提交**，即便内置人类行为模拟与反检测，仍可能违反问卷星平台条款，并可能被用于刷票/刷量、污染调研样本数据。
- 项目已做的正确动作：README 顶部与文末有免责声明、CLI `--url` 必填且**不内置任何真实线上问卷**（L66-68）、License 为 MIT。
- 建议（不改变工具能力，仅强化边界）：保留并前置"仅限学习与获得授权的测试"提示；不内置真实问卷/不提供分发渠道；可考虑默认限速与单次数量上限，降低被滥用为刷量工具的便利度。

---

## 八、修复优先级建议（落地路线）

1. **立即（10 分钟级）**：P2-1 清空默认 `WEIGHT_CONFIG` 并迁移示例；删除 P2-1 占位注释与 Nit 死导入。
2. **短期（半天级）**：P2-2 统一 pipeline 异常吞咽点为"记录或上抛"；P3-2/P3-3 校正文档门禁口径；P3-5 收紧成功关键词。
3. **中期**：P3-1 分批清零 `gui/` pyright error 并纳入门禁；P3-4 明确续传计数口径。
4. **长期/可选**：嵌套 iframe 支持、dataclass 锚点去留决策、非 Windows 降级桩修正。

---

*本报告所有"实测"数字均由审查者在干净虚拟环境中执行命令获得，可按第二节命令复现。*
