# 项目全量代码审查报告（v4.0）

**审查对象**：问卷星自动填写工具（`wjx-autofill`，当前版本 **v4.0.0**）
**审查日期**：2026-09-30
**审查方式**：全仓库 7 组并行逐文件走读——基础层 / 答题策略层 / 检测与校验层 / 流水线与历史层 / 交互与浏览器层 / CLI 与 WebUI / 测试与工程设施。源码与 WebUI 约 2.15 万行全部整读，测试约 1.55 万行逐文件清点结构、重点文件整读；跨组发现互相交叉核实，主线问题（P0/P1）另经独立抽查确认行号与代码。本次为纯审查，未修改任何文件。
**审查范围**：src/ 34 个文件、webui/ 13 个文件、scripts/ 5 个、tests/ 61 个、工程配置与 CI、文档一致性、仓库卫生。

---

## 一、总体结论

工程纪律延续 v3.3.0 的高水准：三态归类、异常留痕、依赖三方对拍、文档数字生成化、防假绿门禁等此前整改**全部确认落地且无回退**。测试基座质量高于绝大多数同类项目（未发现空断言、弱断言、顺序依赖；`make_service` 拒绝未知注入参数、离线套件禁止开真浏览器都是制度化防呆）。

但本轮深审发现了**上一轮评审没有触到的一条结构性裂缝**：**作答生成器两代并存（v1/v2），而信度计划（plan）与投递纠正（distribution）只接到了 v2——问卷上最常见的 single/multi 题生产流量走的是 v1**。由此产生 2 个 P0：

1. `--alpha-target` 建计划时把 single 列为参与题型、精确算出配额并打印"计划已建"，但 single 生成时永不查计划表——**计划 α 与实测 α 必然分叉，且绿测掩盖（test_plan 用 v2 入口自证）**。
2. `--drift-correct` 反馈环键域错位：统计端记的是选项 value/分值（1-based），修正端按 0-based 权重下标取数——**开着纠正时全体选择题被系统性纠正错方向**。两个功能默认关闭，所以平时无害，一旦开启即错。

第二条结构性欠账在**续传链路**：history 无「逐份最终成败」列 + `success_count` 只在批次收尾落库，使 `--resume` 在它最该工作的场景（进程硬崩）静默失效、从头重跑已提交的批次；WebUI 侧还漏掉了 CLI 侧已修过的一次口径教训（少减 fail、索引撞号覆盖旧行）。

其余 P1 集中在：SSE 端点绕过同源校验、多问卷退出码只看最后一批、验证码探测误报面、已答扫描缺隐藏控件守卫、两个注入 JS 的实际缺陷、Docker 镜像裹挟 PII 数据库。

| 项 | 结论 |
|---|---|
| P0（正确性，条件触发） | **2**（plan/distribution 只接 v2；反馈环键域错位） |
| P1 | **9**（详见第二节） |
| P2 | 约 30（详见各节） |
| P3 | 约 60（文末汇总代表性条目） |
| v3.3.0 遗留 5 项 | 3 项仍在（其中"逐份成败列"建议本版根治、未落地），2 项 by design 维持 |

---

## 二、主线问题（P0 / P1，跨组去重后）

### P0-1 single/multi 生产路径绕过信度计划与投递纠正

- 证据链：`question_stage.py:156-157` `if qtype in ("single", "multi"): answer_values = replay_values if replay_values else build_answer_strategy(q)` —— 走 v1 生成器（`answering.py`），而 `plan.forced_choice` 全仓库唯一调用点是 `answering_v2.py:185`；`distribution.adjust` 同样只被 v2 调用。
- `plan.py:532` `PARTICIPATING_TYPES = ("single", "scale", "rating", "dropdown")` 把 single 纳入计划并为它精确建配额（`ensure_plan`），打印"[信度] 计划已建"，但配额永不兑现。
- `tests/test_plan.py:600-627` 恰恰用 `answering_v2.generate_answer` 验证"每一类都真的查表"——测错入口，绿测掩盖裂缝。
- 影响：维度含 single 时计划 α 无法兑现且无任何告警；`--drift-correct` 开着时 single/multi 的统计照收、纠正永不施加。
- 修法：把 single/multi 路由到 v2（v2 已有 `("single","radio")` 别名支持，一行改动），或把 "single" 移出参与题型并在 `ensure_plan` 明确拒绝。**建议顺势退役 v1 生成器**——两代并存正是这两个 P0 的结构性根因。

### P0-2 distribution 反馈环键域错位（开着 `--drift-correct` 时纠正错方向）

- 写入端按 value 记账：`question_stage.py:182` `options_selected = list(answer_values)`（value 域，`detection.py:148` 明示"与 choices 同域，不是下标"）；scale 分支 `:226` 写分值 1..5；随后 `:358` `distribution.buffer_answer(qnum, picks, total)`。
- 读取端按下标取数：`distribution.py:113-116`
  ```python
  for i, w in enumerate(weights):
      target = max(float(w), 0.0) / total_w
      actual = st.counts.get(i, 0) / n
  ```
- 推导：选项 value 取 1..n 时，`actual[0] ≡ 0`（没有 value=0 的 pick）→ 0 号选项恒被判"严重欠投"顶到 `FACTOR_MAX=1.35`；其余修正整体错一位；末位份额永远丢弃；矩阵题每份产生 len(rows) 个 pick，`actual` 份额之和 ≈ rows 倍，factor 长期钉死在夹紧边界。
- 为什么测试没抓住：`tests/test_distribution.py:114,123,131` 直接用 **0-based** 数组喂 `buffer_answer`，与 adjust 自洽，掩盖了生产端 value 域。
- 修法：缓冲处把 value 映射回 0-based（`choices.index(v)`，找不到丢弃该 pick），或全链改 value 键；矩阵系按"每份每行归一"分桶。

### P1-1 硬崩后 `--resume` 静默失效 → 重复提交（CLI 与 WebUI 同根）

- schema 无逐份成败列：`history.py:46-91` runs 表只有批次级 `success_count`（DEFAULT 0）/`fail_count`，answers 表无任何成败标记；`finish_run` 是 success_count 唯一写点且只在批次 finally 调用（`history.py:392-396` + `cli.py:875`）。
- 硬崩后行停 `running`、`success_count=0`：续传守卫 `cli.py:1068-1070` `if 0 < prev_done < prev_planned:` 恒假 → 打一句"按全新批次开始"从头重跑，崩溃前已提交的 K 份全部重复提交且无提示；60 分钟后被 `reap_stale_runs` 收成 failed，同样续不上。
- `reliability.py:195-197` 注释自证"schema 里没有逐份成败标记……P2 补上逐份状态后由它接入"——v3.3.0 评审建议的根治项，本版预留位仍空置。
- WebUI 侧同根：`service.py:757-759` 读同一个收尾才落库的 `success_count`。
- 修法：新增 submissions 表（run_id, submission_index, final_status, finished_at）或逐份标记列，每份成功即增量 flush；续传起点、α 统计、导出全部改按它取数，并收敛为 history 层单一函数供两个宿主共用。

### P1-2 WebUI 续传口径落后于 CLI 已修的同款 bug

- `service.py:782-786`：`attempts_cap = planned - done` 少减 fail（对照 `cli.py:640-643` 已修为 `done + fail`）；`resume_start_idx = done + 1` 未跳过失败轮 → 新份 `submission_index` 与旧失败轮撞号，`record_answer` 是 `INSERT OR REPLACE`（`history.py:433`）**覆盖旧明细**；`state.fail_count` 不恢复，收尾覆写后上批失败数从库和界面双蒸发。
- 后果：上批 done=3/fail=4/planned=10 时恢复后多跑 4 份（总尝试 14 > 10）。

### P1-3 SSE 端点 `/api/events` 绕过 Host/Origin 校验

- `server.py:80-83` 在进入 `_dispatch`（校验在 `api.py:157-160`）之前直接 `self._serve_events(); return`。
- api.py docstring 明说 Host 回环校验是挡 DNS rebinding 的；而 rebinding 后攻击者域对 `http://evil:port/api/events` 同源，`EventSource` 可持续读 `hello` 首帧（= `session.snapshot()`，含问卷 URL、数据路径）与全部日志流——泄露面最大的流恰恰没设防。
- 修法：events 分支前补同样的 `host_is_loopback` / `origin_is_same_origin` 检查（两行）。

### P1-4 多问卷队列退出码只看最后一批

- `cli.py:1268` `sys.exit(0 if fail == 0 else 1)` 用的是循环残留变量 `fail`（`:1182` 每轮覆盖），累计值 `total_fail`（`:1201-1202`）只用于打印。前几批失败、最后一批全成 → 退出码 0，与同函数"方便脚本判断成功/失败"的自述矛盾。修法：改 `total_fail`。

### P1-5 验证码探测误报面 × 每 2 题探测一次 = 整批瘫痪

- `verification.py:88` 关键词表含 `"请点击"`；`:140-143` 对**整个** `document.body.innerText` 子串匹配；`config.py:121` `VERIFY_EVERY_N_QUESTIONS = 2`，pipeline 每 2 题探测一次、提交前后再各一次。
- 题面/说明含"请点击"（多选题常见指令）的问卷：第一次探测即判"有验证"→ 弹窗 + 持锁空等满 120s（关键词一直在，永不"已通过"）→ 判失败 → 每份重复，整批全灭且日志误导排障。
- 同型误报源：`:104-105` `.layui-layer`（问卷星自己的普通弹窗）、`[id*="verify"]`（邮箱/手机验证元素）。
- 修法："请点击"移出 body 全文匹配（保留在 title/URL/专用容器内），或要求与 DOM 信号 a 共同判定；选择器收窄为 captcha/geetest/yidun 等专有词根。

### P1-6 已答扫描缺隐藏控件守卫，可把未答量表题整题跳过

- `detection.py:370-374` detect_questions 有真卷证据的守卫：`if (el.offsetParent === null) return;`（注释：真卷上量表每级标注文字装在 display:none 的 textarea 里）。
- `detect_answered_questions` 填空分支（`:839-869`）用同一组选择器却没有这条守卫，也没有 pageHidden 检查 → 非空隐藏 textarea 把未答题标成已答 → `pipeline.py:233` `if q_num in answered_set: continue` 整题跳过，完整度自检发现不了（题在探测集合里）。
- 修法：补齐与 detect_questions 相同的守卫，并全分支拼入 `_PAGE_HIDDEN_JS`。

### P1-7 `fill_text_script` 不按 maxlength 截断

- `_scripts.py:231-234` option-blank 路径已修（读 maxlength 并 substring），主填空路径 `:256-319` 对 `el.value = txt` 无任何长度处理，docstring 自己写明"程序赋值不受 maxlength 约束……提交返回 unknown"。
- 后果：超长文本被平台校验拒 → 最难查的 unknown 计败。修法：把截断片段提为共享 prelude。

### P1-8 `fill_sort_script` 的"认不出的项"守卫是死代码

- `_scripts.py:626-627`：第二个 forEach 把所有未匹配的 li 补进 `wanted`，随后 `wanted.length !== lis.length` 恒假。
- 后果：order 引用不存在的选项值时，脚本静默按"认识的项+剩余项"重排并返回 True，上层记成功，实际提交错序。value 形态的验收承诺（`sort.py:15-17`）因此落空。修法：第一个 forEach 统计未命中数，>0 即 return false。

### P1-9 Dockerfile 无 `.dockerignore`，本地构建把 PII 数据库打进镜像

- `Dockerfile:33` `COPY . ./`，仓库无 .dockerignore；构建上下文含 `data/history.db(+wal/shm)`（含填空题原文等 PII，.gitignore:68-72 自己承认）、`.venv*/`、`coverage.json`、`_review_tmp/`。
- `docker-smoke.yml` 在干净 checkout 上构建，结构性发现不了，只有开发者本机会踩。修法：一个 10 行的 .dockerignore。

---

## 三、v3.3.0 遗留项现状对照

| 上轮遗留项 | 现状 |
|---|---|
| 逐份成败列（根治 α 口径） | **未落地**（见 P1-1；`reliability.py:195-197` 预留位空置），并已升级为续传正确性问题的公共根因 |
| verification 非 Windows 桩 `except ImportError` 不可达 | **仍在**（`verification.py:45-48, 78-81`；`ctypes.wintypes` 在非 Windows 也可导入，桩永不生效，实际后果是每次调用白起线程） |
| 嵌套 iframe 不支持 | **仍在**：`page_loader.py:49-56` 只遍历一层顶层 iframe；叠加 stealth `contentWindow` 全劫持（见 P2 组），iframe 内脚本环境进一步失真 |
| persona 身份证区划断号 | **仍在且根因更宽**：`persona.py:231,391` 省内序号 = 截断城市表下标+1，多数省份尾段错码（河北沧州→130700、山东临沂→371100），docstring 只承认"盟/地区段"；校验位算法本身经手工复算无误（ISO 7064 MOD 11-2） |
| 回放不支持多选/排序 | **仍在，by design**（判定收敛在 `unsupported_reason` 单点 `reverse_fill.py:108, 503-519`） |
| `_session` 单例家族 | **闭环确认有效**：`cli.py:645-647` 每份问卷成组 `distribution.start_run()` / `reverse_fill.reset_for_survey()` / `alpha_plan.reset_survey()`，pipeline 四路 `discard_buffer` 无泄漏；残留仅 `plan.end_session` 无生产调用方（P3，GUI 若接 plan 前必须补） |

---

## 四、分组逐文件审查

### 4.1 基础层

**src/\_\_init\_\_.py（111 行）** — 包入口，暴露 `WEIGHT_CONFIG` 与版本号。
[P2] L1-106 是 106 行 v2.0–v2.4 历史变更叙事堆在包 docstring，标题停在"v2.4 模块变更日志"，与 v4.0 脱节且无校验，建议外迁 CHANGELOG。[P3] `__all__` 不含 `__version__`。

**src/config.py（163 行）** — 全局常量与权重表。
[P3] `apply_weight_config(replace=True)` 的 clear→refill 中间态窗口（config_io L182-185），热更新瞬间读到半配置会静默回落等权；建议组装好再一次 update。

**src/config_io.py（527 行）** — 配置 JSON 保存/加载/热更新/校验（schema 3.0）。
[P2] `_validate_scale_length` L448 `int(qcfg.get("scale_min", 1))` 未捕获——校验器契约是返回错误列表，畸形配置却直接抛（对照 L442-446 对 scale_max 有守卫）；CLI 有 catch-all 但报错退化。[P2] `save_weight_config` L100 非原子写，中途被杀损坏用户预设；建议 tempfile + `os.replace`。[P3] L154/L252 两个题型集合均漏 `matrix_scale`，其 `row_weights` 不做配置期校验（下游 `_row_weight_map` 兜住键型但值非法不拦）；[P3] L499-525 是 `_validate_weights_array` 的复制粘贴变体（docstring 恰在警告这种做法）；[P3] load 不校验 `schema_version`、顶层键拼错静默得空配置；[P3] 错误消息格式两处不一致。

**src/models.py（608 行）** — 题型枚举/别名表 + 数据模型。
[P3] `snapshot_weight_config` 注释称"深拷贝"实为一层浅拷贝（L562 vs 568-571），未来原地改权重会污染已入 RunState 的快照；[P3] `normalize_question_type` docstring"6 类"实为 9 类；[P3] `QuestionData.from_dict` 对垃圾值裸抛。别名表单一声明派生两视图实现正确，`RunState` 终态优先级语义自洽。

**src/exceptions.py（181 行）** — 异常分类学。
[P3] `TRANSIENT_DOM_EXCEPTIONS` 收录 `InvalidSelectorException`（代码 bug，重试不自愈）与 `SessionNotCreatedException`（环境问题）；[P3] `format_exc_log` 只有 qtype 没有 question 时静默丢题型。`SubmissionAborted` 继承 BaseException 的动机论证完整，设计质量高。

**src/utils.py（400 行）** — 重试/停顿/UA 池/锁/加权抽样。
[P1]（与策略组交叉确认）`weights_are_usable` L306-308 不拒负权重——混合正负时 numpy 路径 L368-371 `w/total` 除零或 `p` 含负抛 ValueError；CLI 路径被 config_io 硬校验拦住，但 GUI/手写配置路径的运行时兜底被击穿；一行修复 `if w < 0: return False`。[P3] `weighted_sample_no_replace` L357 `k = max(1, ...)` 把"选 0 个"夹成 1（经校验路径不可达）；[P3] `ManualHoldLock.wait_until_released` 的 `check_interval` 是死参数（L258 vs L273）。

**src/logging_setup.py（83 行）** — 统一 logging。
[P3] 文件 handler 判重缺 `normcase`，Windows 同路径不同大小写写法会重复挂载、日志写两遍（L39/41）。

**src/platforms.py（267 行）** — 平台常量表。
[P3] hosts 表冗余条目与恒真分支（L120/91）；[P3] `canonical_survey_key` 空 URL 退化键 `":"`，若 GUI/webui 放行空 URL 会跨问卷误续传（CLI 有守卫，L922-925）；[P3] `_query_ident` 不做 percent-decode。取舍论证质量是本组最高。

**src/qr_utils.py（70 行）** — 二维码解码。
[P3] `import numpy` 无条件硬导入，与"numpy 可选"口径矛盾（调用方均延迟导入，实害小）；[P3] `detectAndDecode` 不在 try 保护内，cv2.error 裸抛不走统一错误出口。

**conftest.py（14 行） / run_cli.py（6 行） / run_web.py（11 行）** — 未发现问题。两个入口壳的退出码传递各自正确（`cli.main -> None` 不包 exit、`webui.main -> int` 包 `sys.exit`）。

### 4.2 答题策略层

**src/answering.py（101 行）** — v1 生成器。
[P0-1 关联] 它不是死代码，而是 single/multi（最常见题型）的生产生成器，但通篇没有 `plan.forced_choice`/`distribution.adjust` 接线——见主线 P0-1，建议路由 v2 后整体退役。[P2] 负权重穿透运行时防线（同 utils P1）。[P2] `count_options` 空列表穿过 config_io 校验（`_validate_count_options` L392-398 对空列表循环零次）后运行期 `random.choices([])` IndexError。[P3] 空 choices 崩溃，与 weight_text"探测没给选项放行"的口径不一致。

**src/answering_v2.py（472 行）** — v2 统一生成器，plan/distribution 唯一接线点。
[P1] 身份证字段无人接线：field 分派（L367-387）与 detection 分类都没有 idcard，`persona.id_card` 全仓库零消费——含"身份证"的填空题会被填入**随机中文短句**；且 `Persona` 只存 `birth_year`，`birth_date_text` 每次重抽月日（persona.py:196-199），接线后会与身份证出生段矛盾，需一并补 `birth_date` 字段。[P2] scale 分支 `scale_max < scale_min` 时 `random.choice([])` IndexError（L341/350）；[P2] `min`/`max` 非数字时 `int()` 裸抛（L380，同函数其它取值处都有防御）；[P3] sort 固定顺序不去重；[P3] matrix_multi `pick_options` 含 0 被夹成 1。本文件是七文件中工程质量最高的。

**src/anchoring.py（234 行）** — 锚定认领。
[P2] 两条锚点命中同一题时按 dict 插入序静默首中，无告警（L153-158）——复制配置忘删旧条目时用户看到的与生效的可以不同。[P3] 包含匹配阈值偏松（L106-107，"您的性别"命中"您的性别与年龄"）；[P3] `_REPORTED` 进程级去重使队列模式后续问卷不重报（注释已声明取舍）；[P3] L122-123 冗余守卫。三条锚定契约落实干净。

**src/weight_text.py（326 行）** — 权重文本唯一解析/重建层。
[P2] 未知题型兜底（L249-253）不查 `_bad_number`——而 docstring 明确 matrix_scale 恰恰被赶到这条路上，`0.2,-0.2,1` 静默入库产生错误分布；[P2] `_matrix_entry` L172-177 不校验行权重个数与列数一致，界面收得下、运行期整行静默等权；[P3] `_floats` 跳过空段、`_sort_entry` 不查重复 id、`reconstruct_questions` 重建 scale 恒写 `scale_min = 1`（docstring 已声明刻意保留）。

**src/persona.py（423 行）** — 虚拟人画像。
[P2] 区划断号见第三节；[P2] `id_card` 死字段 + 生日与身份证出生段不同源（见 answering_v2 P1）；[P3] 生日恒 ≤28 号（L198/376，29/30/31 从不出现，分布可被统计识别）；[P3] `_DEFAULT_RNG` 模块级共享实例（当前单线程无碍，放开并发前需改 thread-local）；[P3] 条件链小瑕疵（23 岁硕士不判学生）。

**src/distribution.py（133 行）** — 投递在线纠正。
[P0-2] 键域错位见主线。[P2] 缓冲入口不按题型过滤，sort 的 item id 混进选项计数 → factor 全被压到 0.75（question_stage 侧同责）；[P3] `drift_report` 的"最大缺口"实为"已收到选项中的最大份额"，零份额选项（最欠投的）不进统计；[P3] warm 爬坡边界与注释出入；[P3] `_QStat.options` 死字段。生命周期闭环正面确认。

**src/plan.py（700 行）** — 信度配额规划。
[P0-1] single 参与题型永不兑现（见主线）。[P2] 全链按题号直读 `WEIGHT_CONFIG`（L636/642/683），绕过锚定契约——插题问卷上配额被精确兑现到错位的题上且无口径声明；[P2] `_weights_for` L690 只查正和不查负，负权重可让 `_quotas` 产出负配额；[P2] 配额精确性是"按尝试序"而非"交付序"（失败尝试烧计划行），与 distribution"落地才需要纠正"的哲学缺少对应声明；[P3] `end_session` 无生产调用方。数学内核正面确认：α↔σ_e 反解、秩映射边际配额构造性精确、σ_e 搜索的钳位/塌缩保护无除零。

### 4.3 检测与校验层

**src/detection.py（1093 行）** — JS 注入探测。
[P1-6] 已答扫描缺隐藏守卫（见主线）；[P2] 填空字段分类无 idcard（L399-430，坐实策略组发现）；[P2] 已答扫描全部小节不查 pageHidden，与 detect_questions 分页口径不一致（L786-874）；[P3] radio/checkbox 题号 `/q(\d+)/` 匹配过松（`freq5` 命中 q5）；[P3] blank_options 只在 value 首次入列时判一次；[P3] 题干英文子串误命中（`/tel/` 命中 hotel）；[P3] verify="数字"一律按年龄档；[P3] `_CONSENT_JS` 用页面 el.id 拼选择器（畸形 id 静默丢提示，非注入面）；[P3] `detect_questions` 返回值解析不判 isinstance。注入脚本本体除上述一处外是静态字符串，无注入面。

**src/reverse_fill.py（1031 行）** — 真实答卷回放。
[P2] `begin_replay` 在探测发生前用空题目列表跑 preflight（L939，调用点 cli.py:955），启动日志对每道题列打 blocked 假警——训练用户忽略回放告警；[P3] 编码嗅探兜底 gb18030"必成功"，错码静默成乱码；[P3] `_split_header` 把第一个非空行当表头，顶部有标题行时整表错位无告警；[P3] time 校验 `99:99` 通过；[P3] 前导序号正则与 anchoring 双头维护；[P3] 超长行尾列静默丢弃。ReplayQueue 幂等语义（peek 固定分配 + 成功才 mark_consumed）正确。

**src/completeness.py（80 行）** — 未发现问题。"宁可少拦、不能拦错"落地最干净的样本。

**src/crosscheck.py（145 行）** — [P3] 跳题隐藏题的单向盲区产生每进程一条固定假警（平台侧 `displayHidden` 剔除、我方探测不分），消耗对拍可信度；建议比对前用同源口径过滤。

**src/verification.py（317 行）** — [P1-5] 误报面（见主线）；[P2] 非 Windows 桩不可达（第三节）；[P3] 探测失败提示每 2s 一行共 60 行；[P3] 关键词数组用 `str(list(...))` 生成 JS 字面量，引入单引号即语法错误，建议 `json.dumps`。三态探测状态机本身可靠。

**src/reliability.py（380 行）** — [P2] `implicit_dimension` L359 未传 limit，静默吃 5000 行默认截断——违背本模块自己"部分样本冒充全样本比报错更糟"的原则（L51-53）；[P2] 反向题按维度合并观测值域翻转（L282-291），混刻度时失真，per-item 值域就在手边；[P3] `ALPHA_TYPES` 含永不命中的 "matrix_single"（落库前已归一为 matrix）。数学层（两遍方差/fsum/负 α 保留/交集）无问题。

### 4.4 流水线与历史层

**src/pipeline.py（666 行）** — 主状态机。
[P2]（与 page_nav 同链）多页问卷"翻页按钮认不出"被归为 no_more（`page_nav.py:140-141`）→ `pipeline.py:477` break 进提交，唯一防线完整度自检只拦"平台标必答+整题没探测到"（`detection.py:719-720` marker 为空直接放行）——后续页非必答题静默丢失；建议 `pages >= 2` 且 no_button 时改判 failed。[P3] 无头补漏文案"等了还是读不到"实际一秒没等（L337-339 vs 516-518）；[P3] `except Exception` 内 `raise_non_recoverable` 不可达（BaseException 捕不到，防御式写法但易误读）。正面确认：三态归类完整、成功后清理不上抛（v3.3 的重复提交洞确认封死）、distribution 四路 discard 闭环、`SubmissionAborted` 穿透且不重试、driver 归调用方分层正确、批次计数口径一致。

**src/pipeline_stages/question_stage.py（366 行）** — [P0-1/P0-2] 接线缺位与键域错位的写入端（行号独立核实属实）；[P2] 回执 False 的题答案仍以成功形状落库（`text_answer` 在交互调用前赋值 L216-217，`record_answer` L308-341 照写）——叠加无逐份成败列后事后不可分辨，建议加 `write_ok` 列；[P3] `_wait_for_questions` 把会话级死亡当业务失败；[P3] 矩阵摊平静默丢 float/负值。题型分派 9 类全覆盖无缺失。

**src/pipeline_stages/page_loader.py（162 行）** — [P2] iframe 只遍历一层（第三节遗留项）；[P3] selector f-string 拼进 JS（当前受控常量，模式脆弱）；[P3] `NoSuchFrameException` 冒泡导致整份重跑。

**src/pipeline_stages/page_nav.py（155 行）** — [P2] `no_button` 与"已在最后一页"混同（P2 链另一半，锚点 L140-141）；[P3] 翻页等待循环内瞬态异常冒泡（此时翻页可能已成功）。

**src/pipeline_stages/manual_submit.py（132 行）** — [P3] `_submitted` 以 URL 变化为成功判据之一（L69-70，与 submit.py 同一误判面，docstring 声明刻意复用）；超时返回 FAILED 而非 UNKNOWN 的语义正确。

**src/pipeline_stages/gap_rescue.py（134 行）** — 未发现问题。锁 finally 必还、stop 响应、三路超时契约完整，是本组最干净的文件。

**src/pipeline_stages/verification_stage.py（36 行）** — 未发现问题。[P3] 瞬态异常直接冒泡判整份重试，与其它探测点"降级返回无信号"风格不一致。

**src/pipeline_stages/\_\_init\_\_.py（54 行）** — 未发现问题。

**src/history.py（696 行）** — [P1-1] schema 缺口（见主线，含硬崩→超投的完整证据链）。[P2] `query_answers` L637 LIMIT 默认 5000 静默截断——webui 导出按默认值消费（service.py:525），100 份×60 题的批次导出 CSV 缺行无提示（reliability 自己显式传 1_000_000 绕开，说明调用方已知这个坑）；[P3] 填空原文默认落库（有 `--no-record-text` 开关与文档备案）；[P3] fallback 的 DELETE+INSERT 非原子；[P3] close 后再调用抛 AttributeError 而非 ProgrammingError。正面：SQL 全参数化、WAL+busy_timeout、迁移逐步自愈幂等、purge 预览与实删共用 WHERE。

**src/history_export.py（108 行）** — [P3] `csv_safe` 前缀黑名单缺 `\n`（补一个字符即完整）；[P3] 无流式写出（当前量级可接受）；[P3] docstring 引用的 gui/history_panel 已在 v4.0 删除。BOM 契约由调用方正确履行。

### 4.5 交互与浏览器层

**src/interactions/\_\_init\_\_.py（64 行）** — [P3] `__all__` 导出 `_wait_until_submit_effect` 私有符号。

**src/interactions/_common.py（79 行）** — [P3] `_JS_RETRYABLE` 按类名字符串过滤（L29-33），上游改名重试面静默收窄；[P3] `__all__` 把转手 import 的 `By`/`time` 一并导出。

**src/interactions/_scripts.py（874 行）** — [P1-7] maxlength 缺口；[P1-8] sort 死守卫（均见主线，行号已独立核实）。[P2] `q` 参数在 9 处脚本裸 f-string 插值（L116/262/342/414/487/585/660/690/736），与 `click_option_script` 自述的 json.dumps 转义原则不一致，靠"detection 保证 int"的隐式约定兜底——传字符串即从静默失败升级为页面上下文 JS 注入，建议统一 `json.dumps` 或入口 `int(q)` 收口；[P2] `set_scale_script` 的 `scale_max` 参数是死代码（L344 赋值后 L384 的兜底赋值再未被使用），API 误导；[P3] 事件链样板在 7 处重复，可提共享 prelude；[P3] 合成 click 对 checkbox 的翻转风险（当前只服务单选，建议加注释约束）；[P3] `submit_success_detect_script` 的 `.success` 选择器过宽，可能把弱信号重新引入成功判据。

**src/interactions/choices.py（46 行）** — [P3] 单选路径有 hover/mousemove 模拟、多选批量脚本没有，同题两路径反检测强度不对称。

**src/interactions/dropdown.py（17 行）/ scale.py（26 行）/ text.py（17 行）/ matrix.py（53 行）** — 未发现问题，薄度恰当。

**src/interactions/sort.py（101 行）** — [P2] value 形态验收承诺随 P1-8 落空；[P3] click 形态清残留循环不受 deadline 约束（量级可忽略）。按 value 点击、轮询验收、for/else 兜底的严谨度高。

**src/interactions/submit.py（149 行）** — [P2] URL 变化即判 `SUBMIT_SUCCESS`（L134-135）——提交后跳人机验证页/错误页会被记成功；docstring 只修了"基线拿不到"的旧洞；建议 URL 变化后再要求命中一次强信号。[P3] 重试装饰器下 JS 兜底点击有极窄双击窗口；[P3] 裸 print 而非 logging；[P3] 不处理 confirm() 型确认框（方向安全，建议 docstring 标注）。

**src/browser/\_\_init\_\_.py（94 行）** — [P3] Edge 传 `use_uc=True` 静默忽略（docstring 已声明，建议加日志）。异常分层的 `cleanup_browser_state` 正确。

**src/browser/driver_factory.py（532 行）** — [P2] `opts.add_argument(f"user-agent={ua}")` **缺 `--` 前缀**（L207，原生 Chrome L489 同）——Chromium 只把 `-`/`/` 开头的 token 当 switch，UA 参数被当位置参数丢弃，UA 伪装完全依赖会静默失败的 CDP override（失败处 `except Exception: pass` 无痕迹）；行号已独立核实。[P2] UC 分支（L389-456）不应用 `BINARY_ENV_VARS`（仅 Edge L180 与原生 Chrome L471 调用）。[P2] 无条件 `--inprivate`/`--incognito`（L192/474）否定 `--profile-dir` 的跨批次登录态承诺（cli.py:547 附近宣称可复用）。[P2] `Sec-Fetch-*` document 头经 setExtraHTTPHeaders 强加给所有请求（L106-109）——真实浏览器 XHR 发 empty/cors，全 document 反成指纹特征。[P3] stealth CDP 两步静默 pass 无留痕；[P3] `wow64` 写死 True 与 UA 池不自洽；[P3] 仅设 page load 超时，script/implicit 未显式设置；[P3] `--no-sandbox` 风险未文档标注。正面：异常路径 driver 回收完整（三处 `_discard` + raise）、UC 缺失降级正确、无临时目录泄漏点、headless 与 stealth 组合自洽。

**src/browser/driver_factory_stealth.py（391 行）** — [P2] `contentWindow` 劫持把**所有** iframe 的 contentWindow 指向顶层 window（L356-358）——嵌 iframe 的问卷页会被破坏，且该过强不变量本身是检测点。[P3] 时区/permissions 覆盖函数不在 toString 伪装名单；[P3] plugins 伪造是普通 Array（`instanceof PluginArray` 为 false）、`plugins.length = plugins.length` 无意义语句；[P3] WebGL2 走真实值与伪装 GPU 不一致。插值变量全部来自 config 整型，无注入面。

**src/interaction.py（46 行）** — [P3] 门面只转发 13 个旧符号，与 interactions 包 17 项并存且集合不同，新代码易 import 错层；建议改 `from .interactions import *` 并标注 deprecated。

### 4.6 CLI 与 WebUI

**src/cli.py（1302 行）** — [P1-1] 续传守卫（见主线）；[P1-4] 退出码（见主线）。[P2] Ctrl+C 中断批次后 `fail==0` 可退 0（L854-857 吞中断正常返回），与预约等待期"中断退非零"（L1173-1175）口径相悖；[P2] `count_explicit` 启发式认不出"显式传了默认值"（L1059，`-n 17` 续传时被覆盖）；[P3] `--url-file` 帮助文本与代码矛盾（单行队列时 resume 被放行）；[P3] URL 含逗号的队列行被静默拆坏；[P3] `_start_run_quietly` docstring 契约与实现不符（数据契约异常仍被降级吞掉）；[P3] `run_batch` 重抛异常后 main 收尾（close/save-config/stats）全跳过（队列循环无 try/finally）；[P3] `--alpha-target` 与 `--url-file` 组合下计划份数只按 `-n` 配一次。正面：`_sleep_until` 分片、续传尝试数口径注释、reap 60 分钟阈值。

**src/dialogs.py（118 行）** — 未发现问题。确认不依赖 tkinter，消息出口统一，默认值与"取消即不做"分支对齐。

**webui/\_\_init\_\_.py（8 行）** — 未发现问题。

**webui/\_\_main\_\_.py（96 行）** — [P3] `--port` 无范围校验，`--port 99999` 到 `_Server` 才炸裸 traceback；[P3] `build_service` 静默吞导入异常（有注释兜底理由，但与 `Availability.probe` 两处口径靠人肉一致）。finally 关库、孤儿批次收尾降级正确。

**webui/api.py（457 行）** — [P2] `get_log` 无锁迭代 deque（L189），并发 append 时 RuntimeError → 500，建议锁内取切片；[P3] ROUTES 表末四行缩进混乱。正面：`_json_body` 严格、`safe_download_name` 消毒 + RFC 6266 双文件名、purge 两步 token 设计扎实。

**webui/confirm.py（126 行）** — 未发现问题。[P3] `expires_at` 死字段。token 单次使用、超时作废、锁序单向（session→confirm）全部核实正确。

**webui/server.py（315 行）** — [P1-3] SSE 绕过校验（见主线）。[P3] SSE 并发上限检查-订阅竞态（可短暂超限 1-2）；[P3] POST 体读取无 socket 超时。SSE 30s 寿命 + retry + 全量首帧的重连设计正确。

**webui/service.py（815 行）** — [P1-2] 续传口径（见主线）。[P2] `get_db` 懒构造无锁（L451-452），并发首调建双连接、一个被覆盖后无人 close 且绕过库内单连接锁；[P2] 运行中不拦探测（L350-362 只查 URL 与 `is_busy("detect")` 不查 `running`，前端禁用只是 UI 态）；[P3] `_TYPE_LABELS` 副本漏 `matrix_scale`（与 weights.py 的母本不同步——与续传教训同一个模式：跨层副本各自漂移）；[P3] 导出硬编码 10000 runs + N+1 查询；[P3] `format_weights_for_entry` 排序 key int/str 混比可 TypeError；[P3] 导入配置只警告不拒（CLI 硬退 2，两个入口对同一份坏配置结论相反）；[P3] `import_qr` 无 busy 互斥。

**webui/session.py（437 行）** — [P3] `restore_idle_status` check-then-act 竞窗（短暂谎报"就绪"，下一帧自愈）；[P3] URL 超长静默截断 500 字符后仍拿去导航，与其余字段"不合法即 400"风格不一致。emit 取号与快照同锁、验证器白名单、锁序推演都核实正确。

**webui/weights.py（95 行）** — 未发现问题。显示适配职责收窄正确，`_int_or` 修掉过 `0` 起评量表显示成 1~10 的坑。

**webui/static/app.js（727 行）** — 未发现问题（重点核实）。**无任何 XSS 注入面**——40 处 DOM 写入全部 `textContent`，唯一 innerHTML 出现在注释里；fetch 错误链完整、确认框防连点、SSE gap→refresh 收敛。[P3] 输入可能被并发快照回写打断（400ms 防抖窗口，自愈）；[P3] `els.table` 缓存未使用。

**webui/static/index.html（247 行）** — 未发现问题。app.js 缓存的 56 个 id 全部存在且唯一，确认框超时文案与后端 120s 一致，无死元素。

**webui/static/styles.css（481 行）** — [P3] 缺 `.st-interrupted` 样式，中断批次状态字无配色（app.js:468 动态拼接）。

### 4.7 测试与工程设施

**pyproject.toml / requirements*.txt** — 依赖三方对拍零漂移（test_packaging 逐条钉死），同类仓库少见的好做法。[P3] packages 只防"少声明"不防"多声明"；license 旧式 table 写法。

**pytest.ini / ruff.toml** — [P3] ruff per-file-ignores 的 E402 豁免是死配置（E402 未启用），三个测试文件里的 `# noqa: E402` 同为无效注解；[P3] `line-length = 100` 无执行者。

**pyrightconfig.json** — [P2] 门禁范围不含 tests/ 与 scripts/（约 1.2 万行在门外；scope 由 test_ci_guards 钉住，属决策而非遗漏，但建议至少纳入 scripts）；[P3] "零豁免"叙事与 BASELINE 允许 src/ 现存 3 条 `# type: ignore` 并存，注释互不引用。

**Dockerfile** — [P1-9] 无 .dockerignore（见主线）；[P3] 基础镜像未按 digest 固定，且 3.12 既非 CI 矩阵的 3.10/3.13。其余层设计良好（非 root、层缓存、apt 清理、系统 chromedriver 免联网）。

**ci.yml / docker-smoke.yml** — [P2] pyright 版本未钉（`pyright-action@v2` 拉最新版，与 requirements-dev"上游发版不得悄悄改门禁口径"的自家原则直接冲突，三份钉版清单里唯一裸奔的门禁）；[P3] 注释"webui 那 8 项"实为 10 项；[P3] 无 concurrency 组；[P3] actions 未按 SHA 固定。覆盖率地板→coverage_doc 生成→--check 进 CI 的防漂移链核实有效；E2E 阻塞 + 双 --require-file + 失败注解三件套闭环。

**.gitignore / .gitattributes** — [P3] `.env` 重复两处；[P3] `.qoder/` 未忽略。docs 白名单、PII 防提交、会话产物前科记录都是亮点。

**scripts/ci_annotate_e2e.py（127 行）** — 退出码恒 0 与"事后取证"定位一致，转义/截断/编码处理完整。[P3] error 类失败的注解正文偏薄；[P3] `counts()` 把 error 算作 ran（仅展示层）。

**scripts/coverage_doc.py（280 行）** — 退出码 0/1/2 契约清晰且有子进程测试。[P3] docs/coverage.md 被删时退裸异常，退出码口径错成 1 而非 2；[P3] `canon()` 前缀定位偏脆（纯理论）。数字容差（0.2pp/6 行）设计得当，实测 docs/coverage.md 三行数字与 coverage.json 完全吻合。

**scripts/e2e_gate.py（131 行）** — 三态退出码三方一致，`--require-file` 按 classname 精确匹配有专测。[P3] `counts()` 不感知 `<error>`（CI 顺序保证当前不暴露）。

**tests/（59 个测试文件 + conftest）** — 总体质量显著高于同类：无空断言/弱断言、mock 克制（替身集中在 driver 边界，且明确拒绝 patch 掉"import 了没调用"的检查）、无跨文件顺序依赖、fixture 无滥用。四个重点大文件（test_e2e_integration 1120 行 / test_webui_service 1799 行 / test_pipeline_core 1207 行 / test_driver_factory_offline 779 行）逐一整读：断言的是真 DOM 终态与 DB 落盘值、续传五字段的**等式**、P0 夹逼完整、离线套件不可能开真浏览器且有自证。[P3] 若干文件头仍以已删除的"Tk 宿主"为对照物（叙事腐烂，语义仍成立）；[P3] test_e2e_integration 的 driver fixture 用裸 selenium 而非自家 driver_factory（离线侧已补，docstring 自认）。
无专测文件兜底的源模块：`src/interaction.py`（纯转发，可接受）、`interactions/dropdown|scale|text|matrix`（thin 包装，JS builder 已全量钉住，行为靠 E2E）、`page_loader.py`（test_pipeline_waits 覆盖部分）、`platforms.py`（分散但都有人管）、`webui/weights.py`（仅间接覆盖）。已登记的两个真缺口（question_stage 63.3%、qr_utils 51.5%）reason 完整。

**文档一致性** — [P2] `docs/cli.md:176` 导出文件名写错：实际是 `history_answers.csv` 不是 `history_runs_answers.csv`（`history_export.py:93-96`），用户照文档找不到文件；[P3] `docs/config.md:34` 示例 schema_version 停在 "2.0"（实际 3.0，`examples/` 同源）；[P3] `tests/conftest.py:9`"e2e job 现在是 continue-on-error: true"与现状相反；[P3] CHANGELOG 未发布节硬编码 1454/34 无门禁。版本号三处一致且被钉；README 宣称的二维码导入、webui 默认值、架构文档模块名、CLI 参数互斥全部抽查通过。

**仓库卫生** — `gui/` 只剩 `__pycache__`（v4.0 删桌面版的残留，git 视角目录不存在）→ 建议整个删掉；`CODE_REVIEW_v3.3.0.md` 未跟踪 → 按 v2.7.0 先例归档进 docs/reviews/ 或明确废弃；`项目评估看板_v2.7.0/v3.3.0.html` 已过期 → 删或归档；`_review_tmp/`（本次审查临时目录）→ 删除；`coverage.json/.coverage/data/` 留本地（真风险在 P1-9）。

---

## 五、修复优先级建议

1. **第一优先（结构性，收益最大）**：single/multi 路由到 answering_v2（一行），随后退役 v1 生成器——同时根治 P0-1 与 P0-2 的一半；distribution 键域在缓冲处换算回 0-based（P0-2 另一半）。两处都补上"从 question_stage 生产入口测"的集成断言。
2. **第二优先（数据正确性）**：history 加逐份成败标记并每份增量 flush，续传起点收敛为 history 层单一函数供 CLI/WebUI 共用，WebUI 侧补上 `done+fail` 口径（P1-1/P1-2）。这同时根治 α 口径（v3.3.0 遗留第 1 项）与导出语义。
3. **第三优先（低成本高回报）**：SSE 分支补两行校验（P1-3）；`total_fail` 退出码（P1-4 一词）；`weights_are_usable` 加一行负数检查；`.dockerignore` 一个文件；"请点击"移出 body 匹配（P1-5）；已答扫描补 offsetParent 守卫（P1-6）；maxlength 共享 prelude（P1-7）；sort 死守卫改计数（P1-8）；`--user-agent` 补前缀（driver_factory P2 首条）。
4. **中期**：提交成功判定统一"URL 变化 + 强信号"（submit.py / manual_submit.py / verification 链）；身份证链接线（detection 加 idcard 分类 + persona 补 birth_date 同源 + 真实地级市码表）；plan 接入锚定查表或打印口径声明；`get_db`/`get_log` 并发缝隙加锁。
5. **持续**：pyright 版本钉死并把 scripts 纳入；文档五处口径腐烂（cli.md 文件名、config.md schema、conftest 注释、ci.yml 注释、CHANGELOG 硬编码）一次性清掉并考虑纳入既有 doc 一致性门禁。

---

## 六、合规与用途（维持上轮结论）

dual-use 本质未变：批量代填可能违反问卷星条款、可用于刷量或污染样本。v4.0 的合规姿态延续并略有增强（manual-submit / rescue-gaps 把关键动作交还人、协议框只提示不代勾、PII 默认不落库、运行产物进 .gitignore）；本轮新发现须留意：**P1-9（Docker 镜像裹挟含 PII 的 history.db）与 history 填空原文落库（replay 模式落的是真实答卷内容）**是本工具仅有的两处真实数据落地风险面，建议随 .dockerignore 与既有 `--no-record-text` 文档一并前置提示。

---

*本报告由 7 组并行逐文件走读产出，主线 P0/P1 与关键 P2 均经独立抽查核实行号与代码；所有数字与行号可按文中引用复现。*
