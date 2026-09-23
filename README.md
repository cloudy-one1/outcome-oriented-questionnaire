# 问卷星自动填写工具

> 基于 Selenium Stealth 的问卷星批量填写与提交工具：加权随机作答、人类行为模拟、智能验证码检测，提供 CLI 与 GUI 两种使用方式。

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![CI](https://github.com/cloudy-one1/outcome-oriented-questionnaire/actions/workflows/ci.yml/badge.svg)](https://github.com/cloudy-one1/outcome-oriented-questionnaire/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-3.3.0-brightgreen.svg)](CHANGELOG.md)

本工具仅供学习与研究 Selenium 浏览器自动化技术使用，请务必遵守问卷星平台使用条款与相关法律法规（详见[免责声明](#免责声明)）。

---

## 功能特性

- **浏览器反检测** — CDP 注入 Stealth JS 隐藏 `navigator.webdriver`，随机化 UA / 屏幕分辨率 / 硬件指纹，可选 `undetected-chromedriver` 增强模式
- **人类行为模拟** — 所有点击与停顿均为「正态分布 + 区间截断」采样，完整鼠标事件链，偶发长停顿，周期性重启浏览器释放内存
- **九类题型全覆盖** — 单选 / 多选 / 下拉 / 量表（含 NPS）/ 填空 / 矩阵单选 / **矩阵多选** / **矩阵量表** / **排序题**，填空题自动识别姓名、手机、邮箱、年龄、地址、**所在地区（省市）**、公司等字段并按类型生成；字段类型**先问平台的 `verify` 属性**再用题干正则猜（v3.1）
  这九类全部落在问卷星官方 OpenAPI 题型清单的 **stable 档（12 种，官方"无需预检即可创建"那一档）**；
  清单共 110 种，其余 86 种 advanced、7 种框架草稿、5 种创建接口直接拒 —— 那 98 种我们不做，也不说成做。
  **注意官方 `q_type` 与页面容器上的 `type` 属性不是一套码**（官方 1/2 是分页栏与段落说明，而真卷实测
  页面上的 1/2 是填空与多行文本；下拉、量表、矩阵、排序、多空填空五处也各不相同），所以本项目的码表
  只认真卷。快照与这些边界由 `scripts/official_qtypes_0_4_5.json` + `tests/test_official_qtype_taxonomy.py`
  看着；日期与时间在官方映射里同样没有数字码，本工具对它只探测不作答
- **一份问卷 = 一个人** — 姓名 / 性别 / 年龄 / 学历 / 职业 / 收入 / 婚姻 / 子女 / 省市 一次抽定，其余字段全部从这份画像派生：身份证前 6 位就是画像那个省市、出生日期与年龄同一、第 17 位奇偶对得上性别、校验位是真算的（ISO 7064 MOD 11-2）、邮箱前缀是姓氏拼音、住址与地区题同省。此前是四个各自随机的池子，同一份卷里能长出「女名池的张伟 · 深圳地址 · 3 岁 · 乌鲁木齐公司」这种不存在的人（v3.2，`src/persona.py`）
- **真实答卷回放**（`--replay-file`） — 拿一份已收集到的答卷表（CSV，装了 openpyxl 时也可 .xlsx）逐份回放：表里有的题按表答，认不到列 / 解析不出的格子**照旧随机**并说明为什么。第 N 份用第 N 行，**只有提交成功才推进队列**（失败重投拿到同一行，避免出现两份一模一样的真实答卷）。多选 / 排序 / 矩阵多选本版本明确不支持（v3.2，`src/reverse_fill.py`）
- **投递分布在线纠正**（`--drift-correct`，默认关） — 加权随机只管每次抽样的期望，管不了"失败与 UNKNOWN 吃掉几份之后落地还剩什么比例"。开启后按**已提交成功**的实际比例对目标权重做小幅指数修正（因子夹 ±1/3、前 8 份完全不纠正）。它改变答题结果，所以不是默认行为（v3.2，`src/distribution.py`）
- **实测信度报告**（`--report-alpha`，只测不改） — 批次结束后从历史库读已落库的答案，按维度打印**实测 Cronbach α**；维度取权重配置里人显式声明的 `dimension` / `reverse`。配置没声明时按全部量表题兜底分组，**并在输出里明说那不是某个构念的信度**
- **信度控制**（`--alpha-target 0.60~0.95`，**仅 CLI**） — 先按权重把每道题的选项配额精确摊到总份数上，再用潜变量 + 按秩映射兑现它，使整批的量表结构逼近目标 α（`src/plan.py`，设计稿 `docs/design/DESIGN_reliability_alpha.md`）。必须在权重配置里用 `dimension` 显式声明哪些题属于同一构念，没声明就不建计划并说清原因；**边际配额优先**，α 不达标只告警不返工。少于 30 份不参与
- **矩阵量表** — 格子里是一排可点的 `<a dval>`、值由平台同步写回 `tr[fid]` 指名的提交槽；
  行按平台标的 `fid` 认而不是数行号（同类做法是「容器子元素数 − 3」这种魔数）（v3.1）
- **日期题与新版排序题** — 日期题是只读的 `input.datebox`（值由平台面板回填），按 `field="date"` 生成合规格式的日期并照守 `datelimit` 区间；排序题兼容两种控件形态 —— 老页面往一个隐藏域写逗号串，新页面的**排名只写在 li 的 DOM 顺序里**，只能按目标顺序点击（v3.1）；**选项自带填空框（"其他____"）时，勾中它就同时把那一格写上文本**（v3.0）
- **多分页问卷** — 按页探测、按页作答、**只在最后一页点提交**；翻页失败时整份判失败，绝不把只答了第一页的问卷交上去（v3.0）
- **加权随机作答** — 每道题可配置选项权重：单选加权采样，多选无放回加权抽样，多选还可控制「选中几个」的分布；矩阵多选可控制每行勾几个，排序题可钉住前几名
- **题干锚定的权重配置** — 预设除了题号还带题干 + 结构签名，问卷中间插一题不再整体错位；锚点认不到题时**该题走等权并提示**，而不是拿别人的分布静默填（v3.0）
- **结构对拍（探测自我体检）** — 把探测结果与问卷星写在题目容器上的 `topic` / `type` 逐题比一次，判错题型、整题漏探测在**作答之前**就说明白。只提示、不拦停、不改任何作答行为；平台没标这些属性的模板完全静默（`src/crosscheck.py`）
- **整页形态诊断（探测不到题目时先分清原因）** — 移动端投放（jQuery-Mobile 那一套 `.ui-radio` / `.ui-input-text`）的问卷交进来，本工具一道题都认不出，而「探测不到题目」那句话把责任指向了我们的适配质量和你自己的网络。现在这种页面会明说：这是移动端形态，本工具**未适配**它，换 PC 版链接（`/jq/` 那种或桌面端分享地址）再来一次。只在**一道题都没探测到**且 jQM 控件成规模命中时出声，跑得正常的 PC 页面永远看不到这行（`detection.mobile_layout_notice`）
- **提交前完整度自检** — 平台标了必答、而我们**整题都没探测到**的那些题，注定被平台按必填拦下：那就别点提交，直接判本轮失败并说出是哪几题。判据只收这一种零歧义事实，拿不到平台结构或没标 `req` 一律照常提交（`src/completeness.py`）。可选的**补漏轮**（`--rescue-gaps`，默认关）把拦下来的人工接进来：滚进视野、等他在窗口里补答，补齐了才点提交 —— 答的是人，不是模型
- **提交区协议框诊断（只提示、绝不代勾）** — 移动端投放模板在提交按钮旁挂一个隐私协议同意框（`#checkxiexi`），不勾平台就把提交弹回来；它**不在题目容器里**，逐题探测、结构对拍、完整度自检三道判据全都看不见它，于是症状变成"题题都填了、点提交没反应"而没人说出原因。现在提交前扫一次并说出来。**不代勾** —— 替被调查者签隐私协议与替他答一道题不是同一件事，与"只接管 `alert`、不替页面回答 `confirm`"是同一条线；也**不拦停** —— 兜底那条判据是相邻文案含关键词，认错框的代价不该是一单被白拦。`#checkxiexi` 这个 id 来自同类脚本对移动端模板的用法，本仓库还没在真卷上见过这个框（`detection.consent_notice`）
- **逐题作答回执** — 每一题的填充函数都会回话说"这一下落上了没有"，那个布尔值此前在 `src/pipeline.py` 里被直接丢掉。现在它被逐页收上来、跨页累计，点提交之前报一行 `[作答回执] Q3、Q7 我们照着答了一遍，但页面上没落上`。**只报、不拦停**：False 有两种形状（控件真没找到、作答中途抛异常被降级），后一种下页面可能已经落上了一部分，拿它当拦停依据就是拿认错一单的代价换一句早说的一句话（v3.3）
- **人工提交（把最后一下交给人）** — CLI `--manual-submit`（v3.3）：每一份答完之后工具**不点提交**，只把提交按钮滚进视野并停住，人在窗口里核对、必要时改两格，然后**由人自己点**。要的是这个开关而不是"跑一份读日志"：半份问卷一旦交上去就是平台上一条**收不回来**的真实回收记录。没人点 = 这一份没交出去 → 计失败（不是"交了没确认"）；停止/Ctrl+C 取消这一份且不计失败；无头模式与它互斥 —— 那里没有可点的人。程序点击这条路径在该开关打开时**一次都不会执行**（真 DOM 上由 `window.__submitClicks` 计数器钉住）
- **智能验证码检测** — DOM / URL 文本 / Shadow DOM 三信号并行检测；检出后弹窗提醒人工处理，人工介入锁保证等待期间不被误判为超时；无头模式下直接判本轮失败（没有可介入的人）
- **随时可停** — 停止/关闭窗口在**逐题边界、每题思考停顿、验证码人工等待**上都以 ≈0.2s 粒度生效，被打断的那一份不提交、不计失败（v3.0）
- **断点续填与续传** — 重试时自动跳过单次提交内已答的题；跨进程可从上次中断的批次继续（CLI `--resume` / GUI 恢复对话框），权重配置随批次快照持久化。批次匹配认的是**同一份问卷**而不是 URL 字符串：`/jq/`、`/m/`、`/vm/`、`/vj/` 几种投放形态与微信带进来的渠道参数都归一到同一个键上（v3.0）
- **运行历史与统计** — SQLite 记录每次批次的元信息与逐题答案明细，GUI 可视化查询、CSV 导出、过期清理
- **可靠的提交语义** — 提交结果三态（成功 / 失败 / 未知），答案明细幂等写入，指数退避重试，异常分层（瞬态 DOM 异常可重试、程序错误直接暴露）；页面的 `alert` 被接管成记录器，**必填校验那句话会变成日志里的失败原因**，而不是把整轮噎成 `UnexpectedAlertPresentException`（v3.0）
- **CLI / GUI 双入口 + 可部署** — CLI 适合脚本与定时批量运行，GUI（Tkinter）适合可视化配置与实时监控；`--url-file` 顺序队列 + `--max-total-time` 时限 + `--profile-dir` 复用浏览器 profile，`pip install -e .` 后可用 `wjx-fill` / `wjx-gui`（v3.0）；`--start-at` 预约开跑（等待期间不启动浏览器，时限从到点算起）+ `--rescue-gaps` 补漏轮（v3.1）

---

## 快速开始

### 环境要求

- Python 3.10+（`src/models.py` 使用 `@dataclass(slots=True)`，3.10 是真实下限）
- Microsoft Edge 或 Google Chrome（WebDriver 由 Selenium Manager 自动管理）

### 安装

```bash
git clone <your-repo-url>
cd automation

# 核心依赖
pip install -r requirements.txt

# 可选：装成命令行工具（之后可直接用 wjx-fill / wjx-gui，不必 python run_cli.py）
pip install -e .

# 可选：更强的 Chrome 反检测模式
pip install undetected-chromedriver

# 可选：GUI 二维码 URL 解析
pip install opencv-python

# 可选：真实答卷回放的 .xlsx 读表（`--replay-file`，CSV 不需要它）
pip install openpyxl
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

# v3.0 顺序队列：一个文件多份问卷，共用同一份预设，整批最多跑 2 小时
cat > urls.txt <<'EOF'
# 每行一条：URL[,份数]
https://www.wjx.cn/vm/aaaa.aspx,10
https://www.wjx.cn/vm/bbbb.aspx
EOF
python run_cli.py --url-file ./urls.txt --config ./configs/my_survey.json \
                  --history ./data/history.db --max-total-time 7200

# v3.0 复用浏览器 profile（登录态/磁盘痕迹）；无头只用于调试
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" -n 3 --profile-dir ./profiles/p1

# v3.1 预约开跑：不早于明早 08:00（今天的点已过则顺延到明天）。等待期间浏览器根本不
# 启动，所以 --max-total-time 7200 是从 08:00 起算的两小时，不是从你敲下命令算起
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" --start-at "08:00" \
                  --max-total-time 7200

# v3.1 补漏轮：必答缺口题（我们探测不到、但人在窗口里看得懂的那些）先交给人补答，
# 复检读得到答案才点提交；不写这个开关就是「判失败、不点提交」
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" --rescue-gaps

# v3.3 人工提交：每一份都答完就停住，由你在窗口里核对（要改的直接改）后**自己点**提交。
# 要的是它而不是"-n 1 跑完读日志"：半份问卷交上去就是平台上一条收不回来的回收记录
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" -n 3 --manual-submit
```

> `--headless` 下智能验证**一出现就判本轮失败** —— 无头窗口里没有可伸手拉滑块的人。

每次提交会输出 `OK` / `FAIL` / `UNKNOWN` 之一：

- `OK` — 页面跳转或出现「提交成功 / 感谢您的参与」等**强**信号，确认成功。
  v2.8 起「已完成」不再单独构成成功信号 —— 它太常出现在页面自带文案里，
  6 秒窗口内命中一次就把失败判成成功，方向是危险的；真完成页必然同时命中
  上面某个强信号或成功提示容器，代价只是退回保守的 `UNKNOWN`
- `FAIL` — 验证码未通过、题目探测失败、按钮定位失败等，确认失败。开着 `--manual-submit`
  而没人点提交也算这一类，而且是其中最干净的一种：**这一份根本没交出去**，平台上不留记录，
  重跑没有重复提交风险（区别于 `UNKNOWN` —— 那一下点了、只是没确认）
- `UNKNOWN` — 按钮已点击但等待效果超时（可能是 AJAX 异步提交，也可能是服务端拒绝），保守计为失败并单独统计，便于事后复盘

### CLI 参数一览

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
| `--resume` | 关 | 断点续传：复用**同一份问卷**最近一次未完成批次的 run_id、计数与权重快照，并沿用其计划总份数（需配合 `--history`）。同一份问卷的不同 URL 形态算同一个批次 |
| `--log-file PATH` | 关 | 启用 logging 并把运行日志写入指定文件 |
| `--url-file PATH` | 关 | **v3.0** 顺序队列：每行一条 `URL[,份数]`（`#` 开头为注释）。与 `-u` 同给时 `-u` 先跑；共用同一份 `--config` 与 `--history`；与 `--resume` 互斥（退出码 2） |
| `--headless` | 关 | **v3.0** 无头模式。**只建议调试**：问卷星对无头敏感，且智能验证一出现就判本轮失败（无头里没有可介入的人） |
| `--profile-dir PATH` | 无 | **v3.0** 复用浏览器 profile 目录（登录态与磁盘痕迹）。同一目录不能同时被两个实例占用，所以队列是顺序跑的 |
| `--max-total-time SECONDS` | 无 | **v3.0** 整批墙钟上限。到点按**优雅停止**收工（`interrupted`），下次 `--resume` 可继续 —— 与崩溃的 `failed` 不同。与 `--start-at` 同给时**从预约时刻起算**，不含等待 |
| `--start-at '...'` | 无 | **v3.1** 预约开跑：不早于指定时刻开始（`'YYYY-MM-DD HH:MM[:SS]'` / `'HH:MM'`，后者过点即顺延到明天；带日期又已经过去 → 退出码 2）。等待期间不启动浏览器、不访问问卷地址，所以**没有"提前打开页面等着"那回事**。只承诺「不早于 T」—— 不做时钟同步，也不为抢整点提前加载 |
| `--rescue-gaps` | 关 | **v3.1** 补漏轮：完整度自检拦下必答缺口题时不再直接判失败，而是把那道题滚进视野、等在场的人工补答（最长 300s），**复检空了才点提交**。v3.3 起等着的集合还包括「我们答过、但作答回执照说没落上」的题 —— 那类**等不到也照提交**，它没有拦停资格。默认关 = 行为与此前逐位一致；`--headless` 下不等待（窗口里没有可补答的人） |
| `--manual-submit` | 关 | **v3.3** 人工提交：答完、跑完三道提交前判据之后**不自己点提交** —— 把提交按钮滚进视野并停住，由在场的人核对（要改的直接改）后自己点（最长 180s）。成功与否仍走同一套三态判据（人提交的那份与自动提交的那份必须可比）；等到超时没人点算**这一份没交出去** → 计失败。与 `--headless` 互斥（argparse 直接退 2，不静默降级成自动提交） |
| `--replay-file PATH` | 无 | **v3.2** 真实答卷回放：第 N 份用第 N 行，**只有提交成功才推进队列**。CSV 零依赖，`.xlsx` 需另装 `openpyxl`。与 `--url-file` 互斥（退出码 2，因为每份问卷的行号都从 1 重新开始）；多选 / 排序 / 矩阵多选不支持 |
| `--drift-correct` | 关 | **v3.2** 按**已提交成功**的实际份额小幅纠正加权采样（因子夹 ±1/3、前 8 份不纠正），修正结果**不回写**配置。默认关：它改变答题结果，不是防线 |
| `--report-alpha` | 关 | **v3.2** 批次结束后从历史库按维度打印**实测** Cronbach α（只读不改作答行为；需配合 `--history`） |
| `--alpha-target 0.60-0.95` | 无 | **v3.2** 用计划矩阵 + 按秩映射把整批的量表结构逼近目标 α。越界直接退出码 2；维度归属取权重配置里的 `dimension` / `reverse`，没声明就不建计划并说明原因；**仅 CLI**，GUI 无此开关 |

### GUI 使用

```bash
python run_gui.py
```

1. 输入问卷 URL（或导入二维码图片自动解析）
2. 设置提交份数与浏览器类型
3. 点击 **🔍 探测题目** 自动识别问卷结构
4. 在权重表格中按题型填写权重（格式见[权重配置](#权重配置)）
5. 可选：点 **⭐ 另存默认** 存为 `configs/default_weight_config.json`，下次启动自动加载
6. 可选：取消 **🔒 不记录填空文本** 才会把填空题原文写入历史库（默认勾选，即默认不写）
7. 点 **▶ 开始运行**；运行中可随时切到 **📜 历史记录** Tab 查看批次列表与答题明细，**⏹ 停止** 可随时优雅中断

> 关闭窗口会先请求停止并等待当前轮次收尾（上限 30s），再关闭浏览器并闭合历史记录 ——
> 直接强杀进程会让 `runs` 表停在中途状态并残留浏览器进程。

---

## 断点续传

工具在两个层面处理中断，避免重复作答或从头重跑：

**单次提交内（续填）** — 每次打开问卷后先扫描 DOM 已填状态，重试时跳过已答的题，避免重复点击触发反检测。勾中了自带填空框的选项（"其他____"）却没写字，**不算已答** —— 那种状态交上去只会被平台拒收。

**跨进程（续传）** — 每个批次在 SQLite 中有一条状态记录（`running / finished / failed / interrupted`）：

- 用户主动停止（GUI 停止按钮 / CLI Ctrl+C / `--max-total-time` 到点）→ 状态记为 `interrupted`，已成功份数保留
  - **v3.0**：停止在轮内也生效 —— 逐题边界、每题思考停顿、等题轮询、验证码人工等待都以 ≈0.2s 粒度问一次。
    被打断的那一份**从未被提交**，所以既不计成功也不计失败，下一份的位置就是它（`--resume` 会重做这一份）
- 下次对**同一份问卷**启动时（v3.0 起按归一化键匹配，不再是 URL 字符串相等）：
  - **CLI**：加 `--resume` 自动查找 24 小时内最近的未完成批次，复用其批次号与计数、接续答案编号，从第 K+1 份继续
  - **GUI**：自动弹出对话框「Run #N · 已成功 K/M 份 · 是否从第 K+1 份继续？」
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

---

## 工作原理

```
用户配置 URL + 份数 + 权重（CLI 参数 / JSON / GUI 表格）
        ↓
启动 Stealth 浏览器（指纹随机化 + 反检测 JS 注入）
        ↓
打开问卷页面 → JS 注入探测九类题型结构（只看当前可见页 + 题干）
        ↓
（启用续传时）按归一化键查找同一问卷的未完成批次 → 复用批次号与权重快照
        ↓
循环提交：
  ├── 【每份】接管 window.alert（原生弹窗会噎住 WebDriver，且藏着失败原因）
  ├── 【逐页】结构对拍：探测结果 ↔ 平台在题目容器上自报的 topic/type，
  │            判错题型 / 整题漏探测时打一行诊断（只提示，不改作答）
  ├── 【逐页】扫描已填题集合，跳过已答题（续填）
  ├── 【逐页】逐题按权重随机生成答案（填空题按字段类型生成文本；
  │            勾中自带填空框的选项时同时写上那一格）
  ├── 【逐页】JS 注入模拟人类操作（事件链 + 正态分布停顿）
  ├── 【逐页】每题答案幂等写入 SQLite
  ├── 【逐页】定期检查验证码 → 弹窗等待人工介入（无头则直接判失败）
  └── 有下一页 → 翻页后继续【逐页】；翻页失败 → 整份判失败，**不提交**
        ↓
【提交前】作答回执：某题我们照着答了、交互层回话说没落上 → 报出是哪几题（只报，不拦）
        ↓        （回执 False 有两种形状：控件真没找到 / 作答中途抛异常被降级，
        ↓          后一种页面可能已经落上了一部分 —— 没资格当拦停判据）
        ↓
【提交前】完整度自检：平台标了必答、而整份流程没探测到它的题 → 判失败，**不点提交**
        ↓        （--rescue-gaps：先把这几题、外加上面那些没落上的，一起滚进视野、
        ↓          等在场的人工补答；复检读得到答案才往下点提交。
        ↓          等不到时：整题没探测到的那类维持判失败，没落上的那类照提交）
        ↓
【提交前】协议框诊断：提交区那个不在题目容器里的隐私协议框，没勾就说明一句
        ↓        （只提示；不代勾、不拦停）
        ↓
（最后一页才）模拟点击提交 → 三态判定（OK / FAIL / UNKNOWN）
        ↓        （--manual-submit：不点，把按钮滚进视野停住等**人**点；
        ↓          人点下去之后走同一套三态判定；没人点 → 这一份没交出去，计失败）
        ↓        非 OK 时把页面弹出的必填提示一并打进日志
        ↓
结束落盘：finished（完成）/ interrupted（可恢复中断）/ failed（崩溃）
```

---

## 权重配置

### 方式一：编辑 `src/config.py`

适合脚本固定场景，直接修改 `WEIGHT_CONFIG` 字典。出厂它是**空的**（v2.8 起）——
一份可直接抄、且能作为 `--config` 合法输入喂给 CLI 的 22 题样例见
[`examples/weight_config.example.json`](examples/weight_config.example.json)：

```python
WEIGHT_CONFIG = {
    1: {"type": "single", "weights": [0.1, 0.3, 0.5, 0.1]},  # 第1题：单选，选项3概率最高
    2: {"type": "multi",  "weights": [0.2, 0.2, 0.3, 0.3],   # 第2题：多选
        "count_options": [2, 3], "count_weights": [0.4, 0.6]},  # 选2个40%，选3个60%
}
```

未配置的题目自动降级为等权重随机。

> 为什么默认是空的：v2.7 及以前这里残留过一份**特定真实问卷**的 Q1–Q22 权重。
> CLI 不传 `--config` 时不会清空全局，于是任何问卷的前 22 道单选/多选题都会静默
> 套用那份分布（且被写进批次快照）—— 与上面那句承诺正好相反。现在由
> `tests/test_config_defaults.py` 用新解释器把"出厂必须为空"钉住。

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

### 题干锚点（schema 3.0，v3.0 新增）

题号只是"保存时这道题在第几格"的遗迹：问卷中间插一道题，整份预设就会**向后错位一格**，
而只要选项数恰好还对得上，校验一律放行 —— 跑完 17 份才发现分布全落在别人的题上。
所以 GUI「探测题目 → 另存」会给每条配置附上锚点：

```json
"3": { "type": "single", "weights": [0.1, 0.3, 0.6],
       "anchor": { "title": "您所在的城市？", "signature": "single:8" } }
```

- **带 anchor 的条目只按锚点生效**：题干（剥掉 `1.` / `第2题` 这类序号后比对）+ 结构签名
  都命中才算认领；认不到就这一题走等权随机，并打印一行
  `该权重本次**不生效**，不退回答题号`（每进程每条只提示一次）。
- **不带 anchor 的条目照旧按题号**，schema 2.0 的老文件完全可用（只是没有错位保护）。
- 手写 JSON 也可以只给 `"anchor": {"title": "..."}`，省略 `signature` 就只比题干。

各题型的关键字段：

| 题型（type） | 关键字段 | 说明 |
|---|---|---|
| `single`（别名 `radio`） | `weights` | 长度 = 选项数 |
| `multi`（别名 `checkbox`） | `weights`, `count_options`, `count_weights` | 后两者控制「选中几个」 |
| `dropdown` | `weights` | 单选语义 |
| `scale`（别名 `rating`） | `scale`, `weights` | weights 对齐分值 1..N |
| `text`（别名 `textarea` / `fillblank`） | `field?`, `options?` | 留空 options 走内置随机生成 |
| `matrix_single`（别名 `matrix`） | `rows`, `cols`, `row_weights` | 每行独立权重 |
| `matrix_multi` | `rows`, `cols`, `row_weights`, `pick_options`, `pick_weights` | 每行勾几个由这两个键控制，默认 1 |
| `sort`（别名 `ordering` / `rank`） | `order?` | 给定前几名的固定顺序，其余随机补在后面；留空即整题随机排序 |
| `scale` 的别名 `nps` | 同 `scale` | 0~10 十一级，作答方式与量表相同 |

所有题型都可选 `anchor`（见上）。

### GUI 中的编辑格式（权重表第 4 列）

- 单选 / 多选 / 下拉：`0.2, 0.5, 0.3`
- 量表：`0,0,0.1,0.4,0.5`，或直接写 `5`（强制打 5 分）
- 填空：`张三,李四,王五`，或留空（按字段类型自动生成）
- 矩阵：`1:0,0,0.1,0.4,0.5 | 2:0.1,0.2,0.3,0.3,0.1 | 3:1,0,0,0,0`

配置加载时会自动校验：权重非负且总和大于 0、数组长度与选项数匹配、NaN / Inf 拒绝、多选「选中个数」分布一致等。

---

## 运行历史

启用 `--history`（GUI 默认启用）后，每次运行写入 `data/history.db`：

- **runs 表** — 批次元信息：`id, survey_url, survey_key, total_submissions, browser, use_uc, status(running/finished/failed/interrupted), success_count, fail_count, total_elapsed_seconds, error_message, weight_config_json, started_at, finished_at`
- **answers 表** — 逐题明细：`run_id, submission_index, question_number, question_type, options_selected(JSON), text_answer, elapsed_ms, created_at`，`(run_id, submission_index, question_number)` 唯一，重试不会产生重复行

三种使用方式：

1. **GUI** — 「📜 历史记录」Tab：批次列表 → 点击查看答题明细 → 导出 CSV → 清理 7 天前数据
2. **CLI** — `--stats` 在运行结束后打印累计成功率等汇总
3. **导出分析** — GUI「📤 导出 CSV」生成 `history_runs.csv` + `history_runs_answers.csv`，可直接用 Pandas 分析

老数据库文件会被自动迁移（补充新列、去重并建立唯一索引），多次打开幂等无损。
v3 的迁移还会**回填** `survey_key` —— 留着 NULL 等于没修：续传是按这个键匹配的，
升级前建的批次没键就永远续不上，而那正是这次要消灭的"安静地不弹续传提示"。

续传相关 API（`src/history.py`）：

| API | 用途 |
|---|---|
| `find_resumable_run(survey_url, max_age_hours=24)` | 找**同一份问卷**最近一次 `interrupted` / `running` 的批次（内部按 `canonical_survey_key` 归一化后匹配） |
| `mark_interrupted(run_id, ...)` | 主动停止时把状态写为 `interrupted` |
| `count_done_submissions(run_id)` | 读取已完成份数，决定从第几份继续 |
| `start_run(..., weight_config=...)` | 启动批次时持久化权重快照 |
| `SubmissionHistory.deserialize_weight_config(row)` | 反序列化权重快照（str 键 → int，损坏 JSON 返回空 dict） |

---

## 术语表

| 术语 | 含义 |
|---|---|
| **run**（批次） | 一次「从启动到退出」的批量提交任务，对应 `runs` 表一行 |
| **survey_key**（问卷键） | 同一份问卷各种 URL 投放形态的归一化键（`host:标识`），续传按它匹配而不是按 URL 字符串相等 |
| **submission**（提交） | 一次完整的「打开问卷 → 答题 → 提交」流程，批次内的子单元 |
| **attempt**（尝试） | 目标成功模式下的一次尝试；失败的尝试不占用成功数 |
| **anchor**（锚点） | 权重条目携带的题干 + 结构签名，用来在题号漂移后把预设认领回原题 |
| **success_count** | 批次内确认成功的份数 |
| **fail_count** | 批次内确认失败的份数（含保守计入的未知） |
| **target_count** | 用户期望的成功份数或总尝试份数（取决于运行模式） |
| **question** | 问卷中的一道题目 |

---

## 开发与测试

安装开发工具（钉版本，避免上游发版悄悄改变门禁口径）：

```bash
pip install -r requirements-dev.txt
```

```bash
# 全量测试（含依赖真实浏览器驱动的 E2E）
python -m pytest tests/ -v

# 离线套件（无浏览器环境 / CI，1189 项；只装 requirements*.txt 的口径下会有若干 skip）
python -m pytest tests/ -m "not integration" -q

# 仅浏览器 E2E（需本机 Edge / Chrome + WebDriver，25 项）
python -m pytest tests/ -m integration -q
```

当前测试全部通过：**1214 项**（离线 1189 + E2E 25）。

> **类型门禁不随环境变**：`src/` + `gui/` + 入口在两种环境下都是 **0 error
> 0 warning**。三处可选依赖（`opencv-python`、`undetected_chromedriver`、`openpyxl`）
> 的动态导入改用 `importlib` + `Any` 声明 —— `try: import x` 配 `# type: ignore` 那个
> 写法必然两类环境各留一条 warning：装了包时 ignore 被判"冗余"（本仓库把
> `reportUnnecessaryTypeIgnoreComment` 也设成了 warning），没装时又报模块解析不了。
>
> 仍随环境变的只剩测试数与覆盖率（`requirements.txt` 只声明必选依赖，CI 装不到
> 那几个可选项：`opencv-python`、`undetected_chromedriver`、`openpyxl`）：
>
> | 门禁 | 装齐可选依赖（开发机） | 未装（CI / 干净 venv） |
> |---|---|---|
> | 离线套件 | 1189 passed | 1185 passed + 4 skipped（二维码解析 2 项、`.xlsx` 真读 1 项，外加全新检出时 `coverage.json` 还不存在 —— 文档口径比对那一项也 skip，它是同一次运行**末尾**才产出的） |
> | 覆盖率 | 总口径比 CI 高约 0.2pp（`src/` +0.1、`gui/` +0.6）—— 三个可选项各自改变一侧的分支走向 | 见下方「已知缺口（诚实记录）」的生成块，那是门禁认的唯一口径 |
>
> 覆盖率数字现在只有一个来源：`scripts/readme_coverage.py` 从 `coverage.json` 生成，
> `ci.yml` 里 `--cov-fail-under=` 那个地板值是手写的另一边。写一位小数是因为
> `--cov-report=term` 会把相邻两个真值都四舍五入成同一个整数，两边曾因此互相指对方口径不对；
> 批次循环里有 `random` 分支，同一环境两次跑出 ±0.1pp 属正常抖动，故门禁按容差比数字、
> 按精确匹配比缺口清单的**增删**。

### 代码质量门禁

| 门禁 | 命令 | 当前状态 |
|---|---|---|
| 静态检查 | `python -m ruff check .` | 通过 |
| 类型检查 | `npx pyright` | `src/` + `gui/` + 入口 **0 error 0 warning**（不设 baseline、不豁免，两种依赖口径下都一样） |
| 覆盖率 | `pytest --cov=src --cov=gui --cov-fail-under=70` | 实测值见下方「已知缺口（诚实记录）」的生成块（那个数字只能由 `scripts/readme_coverage.py` 写）；地板从本仓库 `ci.yml` 读出并随生成块一起落盘，只许上调——**v3.1 不动它**：`3.10` 那条 leg 本机量不到（这台机器只有 3.11/3.12/3.13），不拿没量过的环境赌门禁 |
| 文档口径 | `python scripts/readme_coverage.py --check` | README 的覆盖率段落是生成物：数字漂移超过容差、缺口模块改名、低覆盖模块没登记理由，都在这里红 |
| 类型抑制禁令 | `pytest tests/test_ci_guards.py` | `src/` + `gui/` + 入口里不许出现新的 `# type: ignore` / `# pyright:`；现存 3 处登记在 `BASELINE` 里、只准变小（v3.1 前是零登记，靠 v2.8 那批清零） |
| E2E 阻塞性 | `pytest -m integration --junitxml=… && python scripts/e2e_gate.py …` | e2e job 从 v3.1 起**阻塞**：浏览器/驱动自身故障由 `tests/conftest.py` 降级成 skip，"全 skip 也算绿"由数 junit 的 `e2e_gate.py` 堵住 |
| 镜像构建 | `.github/workflows/docker-smoke.yml`（每周一 + 手动） | `docker build` → `wjx-fill --help` → `import src.*`，只构建不发布；**不在** push 的必填检查里，所以 Dockerfile 的口径写在它自己头上、由 `tests/test_packaging.py` 与 workflow 双向对齐 |

- **`ruff.toml`** 启用 `E9/F63/F7/F82` + `F401/F841/F541`。后三条是刻意加的零误报
  「接线断链」防线：v2.4 的 `--resume` 静默失效（`main()` 算出 `resume_fail`
  却没传给 `run_batch`）正是 `F841` 一条规则就能在 CI 拦住的真实缺陷。
- **`pyrightconfig.json`** 纳入 `src/`、`gui/` 与入口脚本。`gui/` 是 v2.8 才进来的：
  此前挂着 45 条诊断（30 error + 14~15 warning），清零靠三类改造 —— Tkinter 上
  动态挂的属性改成类级声明（`SurveyGUI.FONT_*`、`widgets.CardFrame`）、可选导入的
  `Callable | None` 在闭包内重新收窄、`weight_panel` 的 `cfg` 显式声明 `dict[str, Any]`。
  一条 `# type: ignore` 都没用 —— 需要豁免的门禁只是装饰。
- **JS 生成器有真语法校验**：`tests/test_js_scripts.py` 会用 `node --check`
  实际解析每个脚本生成器的输出（本机无 node 时降级为退化片段检测）。
  但它只能保证**语法**合法 —— `input[name='q' + q + '']` 这类
  "语法合法、语义非法"的错选择器只有真浏览器 E2E 抓得住，v2.6 就抓到过一次。
- **浏览器与 Tkinter 都有专用基座**：`tests/test_driver_factory_offline.py` 用
  autouse 夹具把 `webdriver.Edge/Chrome` 换成会 `AssertionError` 的兜底替身，
  所以离线套件绝无可能开出真实浏览器窗口。Tkinter 侧根窗口由 `conftest.py` 的
  会话级 `tk_root` 提供，**全会话只建一个**：Windows 上第二个 `tk.Tk()` 会抛
  "Can't find a usable tk.tcl"，此前各 GUI 模块各自建销，症状是后面的模块整片
  **静默 skip**（比红难发现）。需要"整个窗口"的用例（`tests/test_gui_user_data.py`
  构造真的 `SurveyGUI`）挂在这棵根窗口的 `Toplevel` 上；根窗口建不出来就 skip，
  无显示的 runner 上保持绿。
- **弹窗与文件框是可注入的出口**（v3.1，`src/dialogs.py`）：GUI 启动时注册真
  `messagebox` / `filedialog`，CLI 与测试不注册即拿到确定性默认值（提示无操作、
  确认与选文件都是"否/取消"）。此前 `gui/` 里 13 处模态框直调就是那两行缺口
  （`app.py` 25%、`controller.py` 14%）写着"模态对话框"的直接原因；换出口之后
  断点续传那 5 个字段的自洽性、配置导出导入的往返都有离线契约。

### E2E 覆盖范围

`tests/fixtures/mock_wjx.html` 是一份 **13 题**的离线 mock 问卷（8 类题型 +
一道 2~10 分的非 1 起量表 + 矩阵多选 + 排序题 + 一个**自带填空框的多选项**），
页面内用 JS 模拟问卷星的 AJAX 提交（URL 不变、延迟渲染「提交成功」文案，并记录
提交按钮被点的次数）。它同时模拟了平台的必填校验：**勾了"其他____"却没写字就
提交 → 弹原生 `alert` 并且不给成功文案** —— 上面那两条 v3.0 能力（补文本、
接管弹窗）因此有了真浏览器证据，而不是只有一堆替身对象。
`tests/fixtures/mock_wjx_multipage.html` 是两页版：第 2 页初始 `display:none`，
靠"下一页"按钮切换。`tests/fixtures/mock_wjx_consent_box.html` 专门给提交区协议框
诊断用：一个未勾的 `#checkxiexi`（相邻文案同样含「同意」「协议」）、一个已勾的同意框、
以及多选题里一个文案就是「我同意接收后续邮件」的**选项**。这一条不是走过场 ——
协议框判据全靠元素之间的真实关系（`label[for]` 关联、`closest('div[topic]')` 祖先链），
离线替身喂不出这棵树；id 那条与文案那条**重复计数**的缺陷就是在这层第一次跑出来时发现的。

E2E 因此覆盖到 `run_one_submission` 的完整链路、提交只点一次的保证、量表边界识别、
**题干探测**、矩阵多选真的勾上、排序题同时改 DOM 与写隐藏域、分页问卷
"两页都答完且只在最后一页提交"、带框选项写上文本（含 `maxlength` 截断）、
以及"页面弹 alert 时 WebDriver 不被噎住且文案能被读回 Python 侧"、
"协议框只报提交区那一个且只算一处"。
v3.0 的 4 个缺陷全是在这一层抓到的（见 CHANGELOG），离线 mock 对它们一律绿。

### 已知缺口（诚实记录）

<!-- BEGIN AUTO-GENERATED 覆盖率口径 · scripts/readme_coverage.py · 不要手改 -->
**口径**：`pytest tests/ -m "not integration" --cov=src --cov=gui`，依赖只装 `requirements*.txt`（即 CI 两条 leg 的环境）。地板 `--cov-fail-under=70`（从 `.github/workflows/ci.yml` 读出来，不是手抄的）。

| 范围 | 离线覆盖率 |
|---|---|
| 全部 | **87.9%** |
| `src/` | 91.9%（4600 条语句剩 371 行） |
| `gui/` | 78.1%（1917 条语句剩 419 行） |

#### 已补齐的缺口（"补齐前"一列是登记时的实测）

| 模块 | 补齐前 | 现在 | 契约测试 |
|---|---|---|---|
| `src/logging_setup.py` | 0% | **100.0%**（剩 0 行） | `tests/test_logging_setup.py` |
| `src/browser/driver_factory.py` | 9% | **98.4%**（剩 3 行） | `tests/test_driver_factory_offline.py` |
| `src/pipeline.py` | 26% | **97.0%**（剩 7 行，v3.0 加了分页与弹窗诊断分支） | `tests/test_pipeline_core.py`、`tests/test_pipeline_waits.py` |
| `src/verification.py` | 34% | **97.0%**（剩 3 行，非 Windows 降级桩本机不可达） | `tests/test_verification_flow.py` |
| `src/browser/__init__.py` | 52.2% | **100.0%**（剩 0 行） | `tests/test_browser_facade_offline.py` |
| `src/interactions/choices.py` | 58.8% | **100.0%**（剩 0 行） | `tests/test_choices_interaction.py` |
| `src/interactions/sort.py` | 22.2% | **100.0%**（剩 0 行） | `tests/test_sort_interaction.py` |
| `src/cli.py` | 74.3% | **84.6%**（剩 81 行，剩余是 run_batch 内的浏览器接线与降级分支） | `tests/test_cli_exit_and_reports.py`、`tests/test_cli_main.py`、`tests/test_cli_batch.py` |
| `gui/`（9 个文件合计） | 15% | **78.1%**（`log_view`、`theme` 已 100%） | `tests/test_gui_panels.py`、`tests/test_gui_proxies.py`、`tests/test_gui_run_loop.py` |

#### 仍然没有防线的地方

| 模块 | 离线覆盖率 | 为什么还留着 |
|---|---|---|
| `gui/controller.py` | 37.1% | 配置导出/导入与二维码选文件已可注入替身文件框（`tests/test_gui_user_data.py`），剩 37% 的坎是探测题目那条 worker —— 它要真实 driver（`on_detect_questions` 整段 300-403 行） |
| `gui/app.py` | 71.1% | `WJX_USER_DATA_DIR` 把 `configs/` + `data/` 整棵挪走之后，整窗已能在测试里构造（`tests/test_gui_user_data.py`：构造、输入校验、续传决策、历史库接线）。剩 206 行是 canvas 重绘与 resize/关窗回调，要真实 paint 事件与 mainloop 才走得到 |
| `src/pipeline_stages/question_stage.py` | 63.3% | 逐题 DOM 交互主干：等待、「哪道题调哪个填充器」的分发、带框选项只勾不填的降级都已有离线测试（`tests/test_question_stage_dispatch.py`），真实点击仍靠 E2E |

> 本块由 `python scripts/readme_coverage.py --write` 从 `coverage.json` 生成，`--check` 已进 CI 当门禁
> —— 手改这里的数字会在下次推送时红掉。`--write` 会拒绝装了 `opencv-python` /
> `undetected-chromedriver` 的解释器，因为本块的口径就是 CI 那个不装可选依赖的环境。
> 模块清单与缺口理由维护在 `scripts/coverage_gaps.json`（reason 留空同样是红）。
<!-- END AUTO-GENERATED 覆盖率口径 -->

> **覆盖率不等于验证过。** 上面这些百分比是拿替身对象跑出来的 —— 它证明
> 「指纹参数拼装、异常回收、锁必然释放」这些**逻辑**成立；但真实浏览器能否启动、
> 注入的 JS 在真 DOM 里是否成立，仍然只有那个 E2E job 说了算（v3.1 起它是**阻塞**的，
> 而"驱动起不来 → 全 skip → 看着也是绿"这条路另有 `scripts/e2e_gate.py` 数 junit 堵住）。

推送会触发 GitHub Actions（`.github/workflows/ci.yml`）：ruff + pyright +
带覆盖率地板的离线测试，外加一个真实浏览器 E2E job（v3.1 起阻塞，不再是 `continue-on-error`）。
命名约定：布尔变量使用 `is_` / `has_` / `should_` / `use_` 前缀；
题型字符串优先经 `models.normalize_question_type` 归一化。

---

## 隐私保护建议

`--no-record-text`（CLI）/「🔒 不记录填空文本」（GUI）仅控制 SQLite `answers.text_answer` 列的写入；DOM 仍需填入真实文本（否则提交会失败）。

**默认值不同**：CLI 默认 `关`（会记录），GUI 默认 `开`（不记录）——因为 GUI 会无条件把历史库写到 `data/history.db`，默认不落地原文是更安全的取舍。想保留填空原文供分析的话，在 GUI 里取消勾选即可。

如担心本地历史库包含敏感内容：

1. 长期开启 `--no-record-text` / GUI 勾选锁，仅记录选项索引与题型
2. 定期使用 GUI「🗑 清理 7 天前」或 `purge_old(days_older_than=7)` 清理
3. 敏感问卷跑完后直接删除 `data/history.db`
4. `data/`、`configs/`、`*.db`、`*.csv` 已列入 `.gitignore` —— 历史库和权重预设里都有可识别文本，别靠手动留意来防一次 `git add -A`

> 导出的 CSV 中，以 `=` `+` `-` `@` 开头的单元格会被自动加前缀 `'`，避免问卷页面回传的文本在 Excel/WPS 里被当作公式执行。

---

## 不在本工具范围内（评估过、明确不做）

| 事项 | 为什么不做 |
|---|---|
| 代理 / IP 池 / 伪造 `X-Forwarded-For` | 与下面的免责声明正面冲突；且问卷星的计数走服务端真实 IP + cookie + 智能验证，XFF 只在特定反代配置下被采信 —— 效果不可靠，代价却是对方的风控 |
| 并发 worker（同时开 N 个浏览器） | 本工具的立身点是「正态分布的人类行为 + 可靠的提交语义」。N 个实例同时提交会直接稀释前者，并让「人工介入验证码」这个单实例前提失效 |
| 自动识别验证码 | 只检测、只请人帮忙。绕过验证码不是本项目要解决的问题 |
| 大模型代答 / "人设化"答案 | 两个同类项目都做了这件事（`woshicainiao6/autoQuestionnaire` 的填空题、`kelryry/wjx-auto-sniper` 干脆把它写进标题），我们仍然不做：它把「答案是谁写的」这个问题整体移出了工具 —— 内置文本池生成的是一眼假的占位文本，至少不伪装成被调查者的真实意见，而语义连贯的陈述会让工具从「验证自动化流程」滑向「造问卷数据」。代价还不止语义：多一类网络往返与计费、多一份密钥运维（同类脚本是让用户把 `API_KEY` 明文写进脚本、还把 key 挂在 URL 上），以及**题干原文直接拼进 prompt 的注入面** —— 卷面上写一句"忽略上面的要求"，模型照做、脚本照填照提交 |
| 移动端投放形态的作答 | 判据与注入要换一整套（jQuery-Mobile 的 `.field` / `.ui-radio` / `.ui-input-text` 语义），而 **PC 版链接是现成的替代品** —— 给作答路径加第二套选择器，等于把"本工具只跑 PC 形态"这条写在 README 里的边界悄悄挪掉。现在只诊断并说清「换链接」（`detection.mobile_layout_notice`），一行代码都不往注入侧加 |
| GUI 的无头 / 队列 / 时限 / 预约开关 | GUI 是"看着窗口跑"的入口；无头对它没有意义，队列、时限与 `--start-at` 要的是无人值守，那是 CLI 的场景 |

第二个平台（腾讯问卷 / 金数据 / Google Forms）也**没有**支持：`src/platforms.py` 只是把
问卷星专属的选择器收成了一张表，题型识别与作答注入的 JS 仍是问卷星的 DOM 约定。

---

## 免责声明

本工具仅供学习和研究 Selenium 自动化技术使用。请遵守问卷星平台的使用条款和相关法律法规，不得用于刷票、刷量等任何违规或违法用途。使用者需自行承担所有责任。

> **v3.2 起这一条多了一层意思**：`--alpha-target` 让工具的能力边界从"填得完"扩到
> "让造出来的数据通过信度检验"，`--replay-file` 则是把真实答卷搬进提交链路。这两件事
> **只适用于自有或已获授权的测试问卷**（验证算法用），拿它们去生产环境里制造"看着像真
> 研究数据"的样本，就是本段禁止的刷量本身。设计稿对这一条的表述见
> `docs/design/DESIGN_reliability_alpha.md` §0。

## 仓库结构与文档

| 位置 | 是什么 |
|---|---|
| `README.md` | 这一份：能力边界、安装、CLI / GUI 用法、门禁与已知缺口 |
| `CHANGELOG.md` | 按版本记的变更，每条带当时的取舍理由 |
| `docs/design/` | 设计稿 —— `DESIGN_reliability_alpha.md` 写失信度那条链的数学与边界 |
| `docs/reviews/` | 历史评估报告的归档：查出过什么缺陷、按哪条落地 |
| `CONTRIBUTING.md` | 环境、提交前必过的门禁、测试基座的两个坑、定版步骤 |
| `SECURITY.md` | 哪些落盘位置含个人信息、什么算安全问题、私有报告入口 |
| `src/` | 引擎：探测 → 作答 → 提交；`src/interactions/` 一种题型一个模块 |
| `gui/` | Tkinter 界面，与 CLI 共用同一条 pipeline |
| `scripts/` | 门禁工具本身（README 覆盖率口径的生成脚本、E2E 计数闸门） |
| `tests/` | 离线套件与浏览器 E2E；mock 问卷在 `tests/fixtures/` |

贡献前先读 `CONTRIBUTING.md`，安全问题按 `SECURITY.md` 走私有报告 —— 问卷地址与
答卷原文里都有真实个人信息，公开 issue 不是它们该去的地方。

## License

[MIT](LICENSE)
