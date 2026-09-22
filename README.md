# 问卷星自动填写工具

> 基于 Selenium Stealth 的问卷星批量填写与提交工具：加权随机作答、人类行为模拟、智能验证码检测，提供 CLI 与 GUI 两种使用方式。

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![CI](https://github.com/cloudy-one1/outcome-oriented-questionnaire/actions/workflows/ci.yml/badge.svg)](https://github.com/cloudy-one1/outcome-oriented-questionnaire/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-3.0.0-brightgreen.svg)](CHANGELOG.md)

本工具仅供学习与研究 Selenium 浏览器自动化技术使用，请务必遵守问卷星平台使用条款与相关法律法规（详见[免责声明](#免责声明)）。

---

## 功能特性

- **浏览器反检测** — CDP 注入 Stealth JS 隐藏 `navigator.webdriver`，随机化 UA / 屏幕分辨率 / 硬件指纹，可选 `undetected-chromedriver` 增强模式
- **人类行为模拟** — 所有点击与停顿均为「正态分布 + 区间截断」采样，完整鼠标事件链，偶发长停顿，周期性重启浏览器释放内存
- **八类题型全覆盖** — 单选 / 多选 / 下拉 / 量表（含 NPS）/ 填空 / 矩阵单选 / **矩阵多选** / **排序题**，填空题自动识别姓名、手机、邮箱、年龄、地址、公司等字段并按类型生成；**选项自带填空框（"其他____"）时，勾中它就同时把那一格写上文本**（v3.0）
- **多分页问卷** — 按页探测、按页作答、**只在最后一页点提交**；翻页失败时整份判失败，绝不把只答了第一页的问卷交上去（v3.0）
- **加权随机作答** — 每道题可配置选项权重：单选加权采样，多选无放回加权抽样，多选还可控制「选中几个」的分布；矩阵多选可控制每行勾几个，排序题可钉住前几名
- **题干锚定的权重配置** — 预设除了题号还带题干 + 结构签名，问卷中间插一题不再整体错位；锚点认不到题时**该题走等权并提示**，而不是拿别人的分布静默填（v3.0）
- **结构对拍（探测自我体检）** — 把探测结果与问卷星写在题目容器上的 `topic` / `type` 逐题比一次，判错题型、整题漏探测在**作答之前**就说明白。只提示、不拦停、不改任何作答行为；平台没标这些属性的模板完全静默（`src/crosscheck.py`）
- **提交前完整度自检** — 平台标了必答、而我们**整题都没探测到**的那些题，注定被平台按必填拦下：那就别点提交，直接判本轮失败并说出是哪几题。判据只收这一种零歧义事实，拿不到平台结构或没标 `req` 一律照常提交（`src/completeness.py`）
- **智能验证码检测** — DOM / URL 文本 / Shadow DOM 三信号并行检测；检出后弹窗提醒人工处理，人工介入锁保证等待期间不被误判为超时；无头模式下直接判本轮失败（没有可介入的人）
- **随时可停** — 停止/关闭窗口在**逐题边界、每题思考停顿、验证码人工等待**上都以 ≈0.2s 粒度生效，被打断的那一份不提交、不计失败（v3.0）
- **断点续填与续传** — 重试时自动跳过单次提交内已答的题；跨进程可从上次中断的批次继续（CLI `--resume` / GUI 恢复对话框），权重配置随批次快照持久化。批次匹配认的是**同一份问卷**而不是 URL 字符串：`/jq/`、`/m/`、`/vm/`、`/vj/` 几种投放形态与微信带进来的渠道参数都归一到同一个键上（v3.0）
- **运行历史与统计** — SQLite 记录每次批次的元信息与逐题答案明细，GUI 可视化查询、CSV 导出、过期清理
- **可靠的提交语义** — 提交结果三态（成功 / 失败 / 未知），答案明细幂等写入，指数退避重试，异常分层（瞬态 DOM 异常可重试、程序错误直接暴露）；页面的 `alert` 被接管成记录器，**必填校验那句话会变成日志里的失败原因**，而不是把整轮噎成 `UnexpectedAlertPresentException`（v3.0）
- **CLI / GUI 双入口 + 可部署** — CLI 适合脚本与定时批量运行，GUI（Tkinter）适合可视化配置与实时监控；`--url-file` 顺序队列 + `--max-total-time` 时限 + `--profile-dir` 复用浏览器 profile，`pip install -e .` 后可用 `wjx-fill` / `wjx-gui`（v3.0）

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
```

> `--headless` 下智能验证**一出现就判本轮失败** —— 无头窗口里没有可伸手拉滑块的人。

每次提交会输出 `OK` / `FAIL` / `UNKNOWN` 之一：

- `OK` — 页面跳转或出现「提交成功 / 感谢您的参与」等**强**信号，确认成功。
  v2.8 起「已完成」不再单独构成成功信号 —— 它太常出现在页面自带文案里，
  6 秒窗口内命中一次就把失败判成成功，方向是危险的；真完成页必然同时命中
  上面某个强信号或成功提示容器，代价只是退回保守的 `UNKNOWN`
- `FAIL` — 验证码未通过、题目探测失败、按钮定位失败等，确认失败
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
| `--max-total-time SECONDS` | 无 | **v3.0** 整批墙钟上限。到点按**优雅停止**收工（`interrupted`），下次 `--resume` 可继续 —— 与崩溃的 `failed` 不同 |

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
打开问卷页面 → JS 注入探测 8 类题型结构（只看当前可见页 + 题干）
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
【提交前】完整度自检：平台标了必答、而整份流程没探测到它的题 → 判失败，**不点提交**
        ↓
（最后一页才）模拟点击提交 → 三态判定（OK / FAIL / UNKNOWN）
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

# 离线套件（无浏览器环境 / CI，742 项；只装 requirements*.txt 的口径下会有若干 skip）
python -m pytest tests/ -m "not integration" -q

# 仅浏览器 E2E（需本机 Edge / Chrome + WebDriver，14 项）
python -m pytest tests/ -m integration -q
```

当前测试全部通过：**756 项**（离线 742 + E2E 14）。

> **类型门禁不随环境变**：`src/` + `gui/` + 入口在两种环境下都是 **0 error
> 0 warning**。两处可选依赖（`opencv-python`、`undetected_chromedriver`）的动态
> 导入改用 `importlib` + `Any` 声明 —— `try: import x` 配 `# type: ignore` 那个
> 写法必然两类环境各留一条 warning：装了包时 ignore 被判"冗余"（本仓库把
> `reportUnnecessaryTypeIgnoreComment` 也设成了 warning），没装时又报模块解析不了。
>
> 仍随环境变的只剩测试数与覆盖率（`requirements.txt` 只声明必选依赖，CI 装不到
> 那两个可选项）：
>
> | 门禁 | 装齐可选依赖（开发机） | 未装（CI / 干净 venv） |
> |---|---|---|
> | 离线套件 | 742 passed | 740 passed + 2 skipped（二维码解析用例） |
> | 覆盖率 | 实测比 CI 口径高 0.3pp（`src/` +0.1、`gui/` +0.6） | 见下方「已知缺口（诚实记录）」的生成块，那是门禁认的唯一口径 |
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
| 覆盖率 | `pytest --cov=src --cov=gui --cov-fail-under=70` | 实测值见下方「已知缺口（诚实记录）」的生成块（那个数字只能由 `scripts/readme_coverage.py` 写）；地板从本仓库 `ci.yml` 读出并随生成块一起落盘，只许上调——**v3.0 仍不动它**：`3.10` 那条 leg 本机量不到（这台机器只有 3.11/3.12/3.13），不拿没量过的环境赌门禁 |
| 文档口径 | `python scripts/readme_coverage.py --check` | README 的覆盖率段落是生成物：数字漂移超过容差、缺口模块改名、低覆盖模块没登记理由，都在这里红 |

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
  所以离线套件绝无可能开出真实浏览器窗口；`tests/test_gui_panels.py` 全程只建
  **一个** `withdraw()` 的 `tk.Tk()`（同进程反复建/销解释器会在 Windows 间歇性
  抛 TclError），根窗口建不出来就整模块 skip，无显示的 runner 上保持绿。

### E2E 覆盖范围

`tests/fixtures/mock_wjx.html` 是一份 **13 题**的离线 mock 问卷（8 类题型 +
一道 2~10 分的非 1 起量表 + 矩阵多选 + 排序题 + 一个**自带填空框的多选项**），
页面内用 JS 模拟问卷星的 AJAX 提交（URL 不变、延迟渲染「提交成功」文案，并记录
提交按钮被点的次数）。它同时模拟了平台的必填校验：**勾了"其他____"却没写字就
提交 → 弹原生 `alert` 并且不给成功文案** —— 上面那两条 v3.0 能力（补文本、
接管弹窗）因此有了真浏览器证据，而不是只有一堆替身对象。
`tests/fixtures/mock_wjx_multipage.html` 是两页版：第 2 页初始 `display:none`，
靠"下一页"按钮切换。

E2E 因此覆盖到 `run_one_submission` 的完整链路、提交只点一次的保证、量表边界识别、
**题干探测**、矩阵多选真的勾上、排序题同时改 DOM 与写隐藏域、分页问卷
"两页都答完且只在最后一页提交"、带框选项写上文本（含 `maxlength` 截断）、
以及"页面弹 alert 时 WebDriver 不被噎住且文案能被读回 Python 侧"。
v3.0 的 4 个缺陷全是在这一层抓到的（见 CHANGELOG），离线 mock 对它们一律绿。

### 已知缺口（诚实记录）

<!-- BEGIN AUTO-GENERATED 覆盖率口径 · scripts/readme_coverage.py · 不要手改 -->
**口径**：`pytest tests/ -m "not integration" --cov=src --cov=gui`，依赖只装 `requirements*.txt`（即 CI 两条 leg 的环境）。地板 `--cov-fail-under=70`（从 `.github/workflows/ci.yml` 读出来，不是手抄的）。

| 范围 | 离线覆盖率 |
|---|---|
| 全部 | **76.3%** |
| `src/` | 88.2%（2839 条语句剩 335 行） |
| `gui/` | 58.4%（1888 条语句剩 785 行） |

#### 已补齐的缺口（v2.7 那五条）

| 模块 | 补齐前 | 现在 | 契约测试 |
|---|---|---|---|
| `src/logging_setup.py` | 0% | **100.0%**（剩 0 行） | `tests/test_logging_setup.py` |
| `src/browser/driver_factory.py` | 9% | **98.4%**（剩 3 行） | `tests/test_driver_factory_offline.py` |
| `src/pipeline.py` | 26% | **96.4%**（剩 6 行，v3.0 加了分页与弹窗诊断分支） | `tests/test_pipeline_core.py`、`tests/test_pipeline_waits.py` |
| `src/verification.py` | 34% | **97.0%**（剩 3 行，非 Windows 降级桩本机不可达） | `tests/test_verification_flow.py` |
| `gui/`（9 个文件合计） | 15% | **58.4%**（`log_view`、`theme` 已 100%） | `tests/test_gui_panels.py`、`tests/test_gui_proxies.py`、`tests/test_gui_run_loop.py` |

#### 仍然没有防线的地方

| 模块 | 离线覆盖率 | 为什么还留着 |
|---|---|---|
| `gui/controller.py` | 13.5% | 探测题目 / 二维码解码 / 配置导入都要真实 driver 或模态对话框 |
| `gui/app.py` | 25.0% | `SurveyGUI.__init__` 一建就打开真实 `data/history.db` 并启动动画 `after` 循环，测试里无法安全实例化；`_run_loop` 与几个面板薄代理已有契约测试（`tests/test_gui_proxies.py`） |
| `src/pipeline_stages/question_stage.py` | 62.6% | 逐题 DOM 交互主干：等待、「哪道题调哪个填充器」的分发、带框选项只勾不填的降级都已有离线测试（`tests/test_question_stage_dispatch.py`），真实点击仍靠 E2E |
| `src/cli.py` | 72.1% | `run_batch` 主干已测，剩余是 `--save-config` / 统计打印一类输出分支 |
| `src/browser/__init__.py` | 52.2% | `create_driver` 的 edge/chrome 分发与 `cleanup_browser_state` 整段 Cookie/Storage 清理没有离线替身（缺的正是那 11 行）—— `driver_factory` 有替身 driver，这层薄门面反而没人走一遍 |
| `src/interactions/choices.py` | 58.8% | 三个 `@js_execute_retry` 包装函数的函数体一次都没在离线测试里执行（缺 21/31/42-46 行）—— 分发测到「该调哪个填充器」就停了，填充器自身的 `execute_script` 只有 E2E 覆盖 |

> 本块由 `python scripts/readme_coverage.py --write` 从 `coverage.json` 生成，`--check` 已进 CI 当门禁
> —— 手改这里的数字会在下次推送时红掉。`--write` 会拒绝装了 `opencv-python` /
> `undetected-chromedriver` 的解释器，因为本块的口径就是 CI 那个不装可选依赖的环境。
> 模块清单与缺口理由维护在 `scripts/coverage_gaps.json`（reason 留空同样是红）。
<!-- END AUTO-GENERATED 覆盖率口径 -->

> **覆盖率不等于验证过。** 上面这些百分比是拿替身对象跑出来的 —— 它证明
> 「指纹参数拼装、异常回收、锁必然释放」这些**逻辑**成立；但真实浏览器能否启动、
> 注入的 JS 在真 DOM 里是否成立，仍然只有那个非阻塞的 E2E job 说了算。

推送会触发 GitHub Actions（`.github/workflows/ci.yml`）：ruff + pyright +
带覆盖率地板的离线测试，外加一个非阻塞的真实浏览器 E2E job。
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
| GUI 的无头 / 队列 / 时限开关 | GUI 是"看着窗口跑"的入口；无头对它没有意义，队列与时限要的是无人值守，那是 CLI 的场景 |

第二个平台（腾讯问卷 / 金数据 / Google Forms）也**没有**支持：`src/platforms.py` 只是把
问卷星专属的选择器收成了一张表，题型识别与作答注入的 JS 仍是问卷星的 DOM 约定。

---

## 免责声明

本工具仅供学习和研究 Selenium 自动化技术使用。请遵守问卷星平台的使用条款和相关法律法规，不得用于刷票、刷量等任何违规或违法用途。使用者需自行承担所有责任。

## License

[MIT](LICENSE)
