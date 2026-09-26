# CLI 用法

安装与最简运行见 [README](../README.md)。这一份是命令行侧的完整参考：常见运行方式、
参数一览、三态输出、断点续传，以及无人值守 / 人工介入那几条开关的取舍。
界面（Web 控制台）的用法在 README，引擎侧两边是同一份 `src/cli.run_batch`。

## 环境要求

- Python 3.10+（`src/models.py` 使用 `@dataclass(slots=True)`，3.10 是真实下限）
- Microsoft Edge 或 Google Chrome（WebDriver 由 Selenium Manager 自动管理）
- 面向 Windows：Edge 驱动与人工介入那条路径是在 Windows 上量的

## 常见运行方式

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

# 顺序队列：一个文件多份问卷，共用同一份预设，整批最多跑 2 小时
cat > urls.txt <<'EOF'
# 每行一条：URL[,份数]
https://www.wjx.cn/vm/aaaa.aspx,10
https://www.wjx.cn/vm/bbbb.aspx
EOF
python run_cli.py --url-file ./urls.txt --config ./configs/my_survey.json \
                  --history ./data/history.db --max-total-time 7200

# 复用浏览器 profile（登录态/磁盘痕迹）；无头只用于调试
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" -n 3 --profile-dir ./profiles/p1

# 预约开跑：不早于明早 08:00（今天的点已过则顺延到明天）。等待期间浏览器根本不
# 启动，所以 --max-total-time 7200 是从 08:00 起算的两小时，不是从你敲下命令算起
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" --start-at "08:00" \
                  --max-total-time 7200

# 补漏轮：必答缺口题（我们探测不到、但人在窗口里看得懂的那些）先交给人补答，
# 复检读得到答案才点提交；不写这个开关就是「判失败、不点提交」
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" --rescue-gaps

# 人工提交：每一份答完就停住，由你在窗口里核对（要改的直接改）后**自己点**提交。
# 要的是它而不是"-n 1 跑完读日志"：半份问卷交上去就是平台上一条收不回来的回收记录
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" -n 3 --manual-submit
```

## 每次提交的输出：三态

- `OK` — 页面跳转或出现「提交成功 / 感谢您的参与」等**强**信号，确认成功。
  v2.8 起「已完成」不再单独构成成功信号 —— 它太常出现在页面自带文案里，
  6 秒窗口内命中一次就把失败判成成功，方向是危险的；真完成页必然同时命中
  上面某个强信号或成功提示容器，代价只是退回保守的 `UNKNOWN`
- `FAIL` — 验证码未通过、题目探测失败、按钮定位失败等，确认失败。开着 `--manual-submit`
  而没人点提交也算这一类，而且是其中最干净的一种：**这一份根本没交出去**，平台上不留记录，
  重跑没有重复提交风险（区别于 `UNKNOWN` —— 那一下点了、只是没确认）
- `UNKNOWN` — 按钮已点击但等待效果超时（可能是 AJAX 异步提交，也可能是服务端拒绝），保守计为失败并单独统计，便于事后复盘

## 参数一览

| 参数 | 默认 | 说明 |
|---|---|---|
| `-u, --url URL` | （必填*） | 问卷完整 URL，出于合规考虑不提供默认值。*给了 `--url-file` 时可省 |
| `-n, --count N` | `17` | 提交份数；配合 `--target-success` 时解释为「目标成功份数」 |
| `-b, --browser` | `edge` | 浏览器类型：`edge` / `chrome` |
| `--uc` | 关 | 仅 Chrome 生效：优先使用 undetected-chromedriver，失败自动回退原生 Selenium |
| `-c, --config PATH` | 无 | 从 JSON 加载权重配置（schema 3.0，兼容 2.0），**整体替换**当前配置；校验不通过则拒绝运行并以退出码 2 结束 |
| `--save-config PATH` | 无 | 运行结束后把当前权重保存为 JSON 模板 |
| `-H, --history PATH` | 关 | 启用 SQLite 历史记录（指定 DB 路径） |
| `--stats` | 关 | 运行结束后打印全库统计（需配合 `--history`） |
| `--no-record-text` | 关 | 隐私保护：填空题答案不写入 SQLite（DOM 仍需填入真实文本） |
| `--target-success` | 关 | 把 `-n` 解释为「目标成功份数」，达成即提前结束 |
| `--max-attempts N` | `2×n` | 仅 `--target-success` 时生效：最大尝试次数上限。`--resume` 时按**已尝试次数**扣减（上次成功数 + 失败数），所以续传后总尝试数不会超过 N |
| `--resume` | 关 | 断点续传：复用**同一份问卷**最近一次未完成批次的 run_id、计数与权重快照，并沿用其计划总份数（需配合 `--history`）。同一份问卷的不同 URL 形态算同一个批次，见下节 |
| `--log-file PATH` | 关 | 启用 logging 并把运行日志写入指定文件 |
| `--url-file PATH` | 关 | 顺序队列：每行一条 `URL[,份数]`（`#` 开头为注释）。与 `-u` 同给时 `-u` 先跑；共用同一份 `--config` 与 `--history`；与 `--resume` 互斥（退出码 2） |
| `--headless` | 关 | 无头模式。**只建议调试**：问卷星对无头敏感，且智能验证一出现就判本轮失败（无头里没有可介入的人） |
| `--profile-dir PATH` | 无 | 复用浏览器 profile 目录（登录态与磁盘痕迹）。同一目录不能同时被两个实例占用，所以队列是顺序跑的 |
| `--max-total-time SECONDS` | 无 | 整批墙钟上限。到点按**优雅停止**收工（`interrupted`），下次 `--resume` 可继续 —— 与崩溃的 `failed` 不同。与 `--start-at` 同给时**从预约时刻起算**，不含等待 |
| `--start-at '...'` | 无 | 预约开跑：不早于指定时刻开始（`'YYYY-MM-DD HH:MM[:SS]'` / `'HH:MM'`，后者过点即顺延到明天；带日期又已经过去 → 退出码 2）。等待期间不启动浏览器、不访问问卷地址，所以**没有"提前打开页面等着"那回事**。只承诺「不早于 T」—— 不做时钟同步，也不为抢整点提前加载 |
| `--rescue-gaps` | 关 | 补漏轮：完整度自检拦下必答缺口题时不再直接判失败，而是把那道题滚进视野、等在场的人工补答（最长 300s），**复检空了才点提交**。v3.3 起等着的集合还包括「我们答过、但作答回执照说没落上」的题 —— 那类**等不到也照提交**，它没有拦停资格。默认关 = 行为与此前逐位一致；`--headless` 下不等待（窗口里没有可补答的人） |
| `--manual-submit` | 关 | 人工提交：答完、跑完三道提交前判据之后**不自己点提交** —— 把提交按钮滚进视野并停住，由在场的人核对（要改的直接改）后自己点（最长 180s）。成功与否仍走同一套三态判据（人提交的那份与自动提交的那份必须可比）；等到超时没人点算**这一份没交出去** → 计失败。与 `--headless` 互斥（argparse 直接退 2，不静默降级成自动提交） |
| `--replay-file PATH` | 无 | 真实答卷回放：第 N 份用第 N 行，**只有提交成功才推进队列**。CSV 零依赖，`.xlsx` 需另装 `openpyxl`。与 `--url-file` 互斥（退出码 2，因为每份问卷的行号都从 1 重新开始）；多选 / 排序 / 矩阵多选不支持 |
| `--drift-correct` | 关 | 按**已提交成功**的实际份额小幅纠正加权采样（因子夹 ±1/3、前 8 份不纠正），修正结果**不回写**配置。默认关：它改变答题结果，不是防线 |
| `--report-alpha` | 关 | 批次结束后从历史库按维度打印**实测** Cronbach α（只读不改作答行为；需配合 `--history`） |
| `--alpha-target 0.60-0.95` | 无 | 用计划矩阵 + 按秩映射把整批的量表结构逼近目标 α。越界直接退出码 2；维度归属取权重配置里的 `dimension` / `reverse`，没声明就不建计划并说明原因；**仅 CLI**，Web 控制台也没有这个开关 |

## 断点续传

工具在两个层面处理中断，避免重复作答或从头重跑：

**单次提交内（续填）** — 每次打开问卷后先扫描 DOM 已填状态，重试时跳过已答的题，避免重复点击触发反检测。勾中了自带填空框的选项（"其他____"）却没写字，**不算已答** —— 那种状态交上去只会被平台拒收。

**跨进程（续传）** — 每个批次在 SQLite 中有一条状态记录（`running / finished / failed / interrupted`）：

- 用户主动停止（界面里的「⏹ 停止」/ CLI Ctrl+C / `--max-total-time` 到点）→ 状态记为 `interrupted`，已成功份数保留
  - 停止在轮内也生效 —— 逐题边界、每题思考停顿、等题轮询、验证码人工等待都以 ≈0.2s 粒度问一次。
    被打断的那一份**从未被提交**，所以既不计成功也不计失败，下一份的位置就是它（`--resume` 会重做这一份）
- 下次对**同一份问卷**启动时（按归一化键匹配，不再是 URL 字符串相等）：
  - **CLI**：加 `--resume` 自动查找 24 小时内最近的未完成批次，复用其批次号与计数、接续答案编号，从第 K+1 份继续
  - **Web 控制台**：页面上弹确认框「Run #N · 已成功 K/M 份 · 是否从第 K+1 份继续？」，两分钟无人应答按"取消"
- 续传同时会从该批次的权重快照（`weight_config_json`）自动恢复上次使用的权重配置，确保「最后使用的权重就是用户设置的」
- 续传默认**沿用上次的计划总份数**（不是 `-n` 的默认 17）；显式给 `-n` 时以你输入的为准
- 显式给了 `--config` 时以文件为准，不再从快照恢复权重

### 什么算"同一份问卷"

同一份问卷的答卷链接至少有 `jq`（电脑端）/ `m`（移动端）/ `vm`、`vj`（短码分享）/
`hj` 几种投放形态，微信与渠道参数还会往 query 上挂 `kd`、`source` 一类可变字段 ——
二维码解出来的链接和手输的链接常常字符串就是不一样的。归一化规则是
**host（去 `www.`、转小写）+ 路径最后一段（去 `.aspx`，大小写原样保留）**，
函数在 `src/platforms.py::canonical_survey_key`。

两件刻意**没做**的事，都是"误并批次"比"少并批次"贵：

- **不跨 host 合并** — `www.wjx.cn` 与 `v.wjx.cn` 可能是同一问卷的不同投放渠道，
  也可能真是两件事。并错了会把另一份问卷的中断计数拿来续传 → 重复提交。
- **短码大小写不归一** — `/vj/Pq8k2A` 与 `/vj/pq8k2a` 判成两份问卷。

少并的后果只是"这次没续上传，重跑一次"；误并的后果是交出去的数据不可用。

异常崩溃的批次记为 `failed`（不可恢复）；只有主动中断的批次可续传。

## 无人值守 vs 人工介入

`--url-file` + `--max-total-time` + `--start-at` + `--resume` 是无人值守那一头：
队列顺序跑（profile 目录不能被两个实例同时占），到点优雅停止，下次接着跑。
`--rescue-gaps` / `--manual-submit` / 验证码检测是人工介入那一头：都要**有个活人看着窗口**。
两者不叠加使用 —— 无头模式与后两个开关互斥，因为那里没有可伸手的人。

## 统计与数据类开关

三条都在改"这批数据长什么样"，所以都不是默认行为，也都只说事实不说结论：

- **真实答卷回放**（`--replay-file`） — 拿一份已收集到的答卷表（CSV，装了 openpyxl 时也可 .xlsx）逐份回放：表里有的题按表答，认不到列 / 解析不出的格子**照旧随机**并说明为什么。第 N 份用第 N 行，**只有提交成功才推进队列**（失败重投拿到同一行，避免出现两份一模一样的真实答卷）。多选 / 排序 / 矩阵多选本版本明确不支持（`src/reverse_fill.py`）
- **投递分布在线纠正**（`--drift-correct`） — 加权随机只管每次抽样的期望，管不了"失败与 UNKNOWN 吃掉几份之后落地还剩什么比例"。开启后按**已提交成功**的实际比例对目标权重做小幅指数修正（因子夹 ±1/3、前 8 份完全不纠正）。它改变答题结果，所以不是默认行为（`src/distribution.py`）
- **实测信度报告**（`--report-alpha`，只测不改） — 批次结束后从历史库读已落库的答案，按维度打印**实测 Cronbach α**；维度取权重配置里人显式声明的 `dimension` / `reverse`。配置没声明时按全部量表题兜底分组，**并在输出里明说那不是某个构念的信度**
- **信度控制**（`--alpha-target`） — 先按权重把每道题的选项配额精确摊到总份数上，再用潜变量 + 按秩映射兑现它，使整批的量表结构逼近目标 α（`src/plan.py`，设计稿 `docs/design/DESIGN_reliability_alpha.md`）。必须在权重配置里用 `dimension` 显式声明哪些题属于同一构念，没声明就不建计划并说清原因；**边际配额优先**，α 不达标只告警不返工。少于 30 份不参与

这四条里只有 `--report-alpha` 是纯读侧。`--alpha-target` 与 `--replay-file`
把工具的能力边界从"填得完"推到"让造出来的数据通过信度检验"，
因此只适用于自有或已获授权的测试问卷 —— 见 README 的免责声明。

## 运行历史

启用 `--history`（界面默认启用）后，每次运行写入 `data/history.db`：

- **runs 表** — 批次元信息（含 `survey_key`、状态、成功/失败计数、权重快照）
- **answers 表** — 逐题明细，`(run_id, submission_index, question_number)` 唯一，重试不会产生重复行

表结构与续传 API 在 [architecture.md](architecture.md#运行历史落什么)。

三种使用方式：

1. **Web 控制台** — 「📜 历史记录」页签：批次列表 → 点击查看答题明细 → 导出 CSV → 清理 7 天前数据（清理是两步确认）
2. **CLI** — `--stats` 在运行结束后打印累计成功率等汇总
3. **导出分析** — 界面里的「📤 导出 CSV」生成 `history_runs.csv` + `history_runs_answers.csv`，可直接用 Pandas 分析

## 隐私

`--no-record-text`（CLI）/「🔒 不记录填空文本」（界面）仅控制 SQLite `answers.text_answer` 列的写入；DOM 仍需填入真实文本（否则提交会失败）。

**默认值不同**：CLI 默认 `关`（会记录），界面默认 `开`（不记录）——因为界面会无条件把历史库写到 `data/history.db`，默认不落地原文是更安全的取舍。想保留填空原文供分析的话，在界面上取消勾选即可。

如担心本地历史库包含敏感内容：

1. 长期开启 `--no-record-text` / 界面上的勾选锁，仅记录选项索引与题型
2. 定期使用「🗑 清理 7 天前」或 `purge_old(days_older_than=7)` 清理
3. 敏感问卷跑完后直接删除 `data/history.db`
4. `data/`、`configs/`、`*.db`、`*.csv` 已列入 `.gitignore` —— 历史库和权重预设里都有可识别文本，别靠手动留意来防一次 `git add -A`

> 导出的 CSV 中，以 `=` `+` `-` `@` 开头的单元格会被自动加前缀 `'`，避免问卷页面回传的文本在 Excel/WPS 里被当作公式执行。
