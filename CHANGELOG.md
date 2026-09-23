# 更新日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [未发布]（目标 3.3.0）

> 本块横跨 `v3.1-dev` / `v3.2` / `v3.3` 三个批次，归属看提交标题的前缀（条目正文大多没标）。
> 不另立 `[3.1.0]` / `[3.2.0]` 小节：`src.__version__` 与 `pyproject` 还锁在 **3.0.0**
> （`test_packaging.py` 管两边对齐），`v2.4.0` 之后也没再打过 tag —— 版本号没升就把它标成
> "已发布的 3.1.0"是虚构发布。要按阶段切开，先升版本、连带打 tag，再拆小节。

### 新增

- **结构对拍：探测结果 ↔ 平台自报的题目结构**（`src/crosscheck.py`、
  `src/detection.py::detect_platform_questions`、`src/platforms.py::WJX_TYPE_CODES`，接线在
  `src/pipeline.py::_answer_current_page`）。
  要补的是"探测发现自己判错了"这条路：`detect_questions` 从控件形状反推题型，而权重锚定的
  `question_signature` 用的就是这份探测结果 —— 判错时签名跟着错，锚点比对一路放行，逐题作答
  又照着同一个错结果点控件，症状一路延后到「提交后平台说该题未答」，日志里看不出从哪步开始错。
  问卷星在题目容器上明写着题号与题型码（真卷形态
  `<div id="divN" class="field" topic="N" type="C">`），那是一条独立于我们逻辑的读数，拿它来比。
  三条契约：① **只提示、不拦停、不改作答**，码表未收录的码（8 / 10…）不参与题型比对；
  ② **没有平台信号就彻底静默** —— 模板不标 `topic` 不是我们的探测错了，用假警训练用户忽略提示
  比少一个诊断糟得多；③ 同一句提示每进程只印一次（分页每页都要对拍、一批又跑十几份）。
  对拍与 `detect_questions` **共用** `pageHidden` 分页过滤（抽成 `_PAGE_HIDDEN_JS`）：
  两边分页口径不一致时，每份分页问卷都会刷一堆"平台有 Q9 我们没探测到"的假警。
- **码表来源是实测，不是抄文档**：2026-09-22 在一份真问卷（15 题，覆盖题型码
  1/2/3/4/5/6/7/9/11）上把容器属性与容器内真实形状逐题对过。同一份卷上跑对拍，
  **7 道出声**：Q7/Q8/Q9（矩阵量表被判成 `scale`）、Q10（量表被判成 `text`）、
  Q14（多空填空被判成 `scale`）5 道题型不符，Q12（排序题）、Q15（日期题）2 道整题没探测到。
  这些是真卷上现存的探测缺口，本条只负责把它们说出来，修复另计。
- 离线契约测试 `tests/test_crosscheck.py` 32 项（平台读数的畸形输入免疫、三类差异各自出声、
  未知码静默、无信号静默、去重）；浏览器 E2E 加 1 项：fixture 按真卷形态补上 `topic`/`type`
  后，要求读数齐且**对拍零提示** —— 探测判对时不许出声，是这条功能的另一半。
- **提交前完整度自检**（`src/completeness.py`，接线在 `pipeline` 的 Step 7.5，点提交之前）。
  平台对必答题的拦截本来发生在**点提交之后**（弹一句"第 N 题未答"），本工具那时候的样子是
  提交返回失败 / unknown，再从日志里猜是哪一题。而"某道题我们整份流程压根没探测到"
  （真卷上的排序题、日期题就是这样）是**注定被拦**的一次提交：白点一次，还留下一条
  看不懂原因的失败。这一步把这种题挑出来，直接判本轮失败并说出题号。
  判据只收一种零歧义事实：**平台标了 `req` 且题号不在"整份探测到的题号并集"里** → 我们必定没答
  → 平台必定拦。探测到了但题型判错（矩阵量表被判成 `scale` 那一类）**不归它管** —— 那种判定要靠
  "已答扫描"，而已答扫描读的是同一套可能判错的结构，拿它当判据等于让错的判定自己给自己背书。
  两条降级都是同一句话"宁可少拦，不能拦错"：拿不到平台结构 → 照常提交；容器没有 `req` 属性 →
  视为不必答。被跳题 / 互斥逻辑藏起来的题在 JS 侧就不读出来（那种题平台本来不要求答）。
  跨页口径：判据用的是逐页**并集** `detected_all`，只看最后一页会把前面几页答过的必答题误判成漏答。
  测试：`tests/test_completeness.py` 13 项离线 + `test_pipeline_core.py` 加 5 项接线
  （含两页并集、"无信号不拦"、"非必答不拦"、对拍看本页 / 自检看整卷）+ E2E 1 项
  （真 DOM 上文件上传题探测不到、而平台标 `req=1` → 缺口正好是那一题；没标 `req`
  与 `req=""` 的题各占一条反例，确保"拦错"这件事不会发生）。

- **新增题型：矩阵量表 `matrix_scale`**（`src/detection.py` 5c 节、`interactions/_scripts.py::
  fill_matrix_scale_script`、`answering_v2`、`question_stage`、`models` 题型表、`platforms` 码表）。
  问卷星的 `type=6` 同时覆盖三种形态：矩阵单选 / 多选（格子里是 radio/checkbox，v3.0 已支持）
  与**矩阵量表**（格子里是一排 `<a dval="1..5">`，容器 `table.matrix-rating`）。真卷上那 3 道
  矩阵量表原先被判成"一个 4 级量表"——因为容器 class 含 `rating`，被量表分支先抢走了。
  两条判据按一手 DOM 事实写：行是 `tr[tp="d"][fid]`，**`fid` 就是这一行的提交槽名**（平台自己标的，
  不用猜行号；5c 只在题型还是 `scale` 时才抢回来，别的人为判定的题型不动它）；格子是行内的
  `a[dval]`。实测点击**同步**写回 `input[name=fid]`，所以填充脚本点完当场回读每个槽，
  对不上就返回 False —— 不交一份"看起来点了"的量表。
- **量表兼容 `ul.modlen*` 形态 + 填空不再捡隐藏框**（同一处真卷缺陷的两半，Q10）。
  平台把评分条渲染成 `ul.modlen5`、每级一个 `<a>`，既没有隐藏 radio 也没有数字文本，
  原先的"数格子"规则数不到它 → 级数算不出 → 而它旁边那 5 个 `display:none` 的
  `textarea.wjxui-textage` 标注框被填空分支当成"填空题"捡走了。现在：`modlen` 列表的 li 数
  即级数；填空探测只认**可见**控件（`offsetParent === null` 一律跳过 —— 量表/矩阵的存储框
  全是这种）。两处必须一起改：只加后者会把这题从"判错型"变成"整题失踪"，反而更糟。
- **补漏轮：完整度自检拦下之后，把人工接进来**（CLI `--rescue-gaps`，默认关；
  `src/pipeline_stages/gap_rescue.py`，接线在 `pipeline` Step 7.5 判出缺口之后）。
  上一条自检只做对了一半 —— 它拦住了一次注定被必填挡下的提交，却也把一种**本来还能救**
  的提交一起判死了：必答题我们探测不到，**坐在屏幕前的人看得懂**，那道题他点两下就答完
  了。开关打开后的顺序是：说清楚缺哪几题（原来那行 `describe_gap` 保留，不取代）→
  把第一道缺口题滚进视野 → 挂上人工介入锁等（上限 `GAP_HOLD_TIMEOUT=300s`，每 2s 复检
  一次，每 20s 报一行进度）→ **只有复检空了才点提交**。等不到 / 超时 / `--headless`
  （窗口里没有可补答的人）→ 维持原判失败，**不新增第三种结果**。
  复检为什么不能只重跑 `detect_questions`：缺口题按定义就不在我们的探测结果里，人工补完
  再探测一遍照样看不见 —— 只重跑探测的复检永远不会变空，"等人工"就等成了形式。所以复检
  额外把"页面上现在读得到值的题号"并进来当证据（`detect_answered_questions` 读的是提交用
  的存储字段 `input[name=qN]`，不是我们的控件假设）。两种证据都只能让缺口**变小**，
  `unanswered_required` 的判据一个字没改；复检自身读不到就按"没有新证据"处理。
  三条不变量：① 停止优先于提交 —— 等待期间 `stop_check` 一置位就抛
  `SubmissionAborted`，半补的问卷不会被交出去，锁在 `finally` 里必还（锁不还是整批挂死）；
  ② 人工介入锁在等待期间处于 holding，外部任何超时判断照既有契约给它让路；
  ③ 刻意**不做**"按一下回车由人宣布补好了"那种放行 —— 那等于把判定权交给一句口令，
  而这一步要的是"我们确实看得见那道题有值"。代答更不是选项（见 README「不做」表）。
  测试：`tests/test_gap_rescue.py` 12 项（三个出口各自出声、锁的生命周期、滚动的降级、
  复检异常不许咽掉）+ `tests/test_pipeline_core.py` 加 9 项接线（默认关一次都不等、
  复检空了才提交、人工没补齐维持判失败、停止不提交、无头跳过等待、复检把已答证据算进来）
  + CLI 三处 5 项（`parse_args` 默认值、`main → run_batch`、`run_batch → run_one_submission`
  的透传 —— "算了却没传"是这个仓库反复付过账的缺陷形态）。
  另改一处既有断言：`test_run_one_submission_never_retries_a_successful_core` 钉的是
  `run_one_submission → _do_one_submission_core` 的**精确关键字表**，新增参数必须进那份
  清单，否则补漏开关又会安静地不生效（正是这条测试要防的形状）。

- **`--start-at`：预约开跑**（CLI，`src/cli.py::_start_at` / `_sleep_until`）。
  要在某个整点开始交卷的需求一直存在（活动 08:00 开放、早高峰前跑完），此前的做法是把
  机器摆在那儿手动掐点。这里只承诺一句话：**不早于 T**。刻意**不做**的另一半才是要点
  —— 不做时钟同步、不预留"提前加载页面待命"的余量、不在到点前 re-navigate 去抢开闸瞬间：
  第一次 `driver.get` 就是 T 之后的第一件事，与"一次真实提交"的形状完全一致。
  实现上等待放在 `main()` 里所有校验之后、`run_batch` 之前，于是三条性质是**结构性**成立
  而不是另外维护计数器：坏配置在启动那一刻就红（不会让人等三小时后才发现白等）、等待期间
  浏览器根本不启动（`--profile-dir` 不会被长期锁住）、`--max-total-time` 天然从 T 起算
  （那条上限的计时本来就在 `run_batch` 里开始）。
  切片睡（`_START_AT_SLICE=1s`）而不是一发 `sleep(剩余)`：预约动辄几小时，一发的 sleep
  期间 Ctrl+C 要等这一觉睡完才生效，而"到点之前先取消"正是等待期唯一会被做的事。
  进度行每 60s 一条（每秒一条会把日志埋掉）。被中断时以非零码退出且一份都不提交 ——
  CLI 常被 cron 调，退出 0 会说谎。
  解析口径：`'YYYY-MM-DD HH:MM[:SS]'` / `'YYYY-MM-DDTHH:MM'` / `'YYYY-MM-DD'` /
  `'HH:MM[:SS]'`；只给时刻 = 今天这个点，点已过则顺延到明天（这是这种写法的通常意思，
  报错反而是添乱），带日期而已经过去 → `ArgumentTypeError` → 退出码 2（与 `--config`
  校验不通过同一类"参数就没收"的口径）。不猜相对量词、不猜时区、不猜"下一个工作日"。
  与 `--url-file`（整个队列只预约一次）与 `--resume` 都不互斥；GUI 不给这个开关，
  理由与队列/时限同一条（README「不做」表已把那句改成含预约）。
  测试：`tests/test_runtime_forms.py` 加 23 项 —— 五种形态各自解析、裸时刻顺延、
  过去的时间点被拒、`parse_args` 对坏值退 2、`_sleep_until` 的切片/进度/到点即止/
  Ctrl+C 不被吃掉（假时钟把 sleep 变成推进 `now`，于是一场三小时的预约在几毫秒内跑完），
  以及 `main()` 的顺序契约（等待必须先于 `run_batch`、队列只等一次、不给开关就一次都不进
  等待、等待中被中断不跑批次）。顺手抓到一个真缺陷：`strptime("%H:%M")` 把日期填成
  **1900-01-01**，不换成今天的话任何"裸时刻"都判成已经过去。

- **整页形态诊断：一道题都没探测到时先分清原因**（`detection.mobile_layout_notice`，
  接线在 `pipeline._answer_current_page` 的 Step 5 空结果分支）。
  要补的是"一句话把责任指错地方"：一份移动端投放（jQuery-Mobile 模板）的问卷交进来，
  `detect_questions` 认不出任何题（探测入口是 `#fieldset1` / `input[name=qN]` 那套 PC 约定），
  日志只有「探测不到题目 → 整批失败」。这句话把矛头指向我们自己的适配质量与用户的网络，
  而真正的原因只是**链接给错了** —— 换 PC 版地址立刻有解，等改版没有解。同一个形状此前已由
  `platforms.unsupported_url_notice`（认不出域名）说过一次，所以这是同一个家族：
  **只提示、不拦停、判定一字不改**。
  判据收到最窄才敢说：只在 (a) 本页一道题都没探测到 **且** (b) jQM 独有的控件类
  （`.ui-input-text` / `.ui-radio` / `.ui-checkbox`，常量在
  `platforms.WJX_MOBILE_LAYOUT_SELECTORS`）成规模命中（≥3 处）时出声。
  **`.field` 被刻意排除在触发条件之外** —— 问卷星 PC 端模板的题目容器就写着 `class="field"`
  （真卷实测 `<div id="divN" class="field" topic="N">`），拿它判断会在本来跑得好的问卷上
  到处误报，而假警的代价是这句真话从此没人看。读数拿不到（None / 空串 / 被页面改写）同样
  沉默：没有信号不等于有缺口。同一句话每进程只印一次（一批 17 份每份都会走到这里）。
  明确**不做**的两件事写进了 README「不在本工具范围内」：不实现移动端作答，也不给作答路径
  加第二套选择器 —— 那等于把"本工具只跑 PC 形态"这条边界悄悄挪掉，而它是写在 README 里的承诺。
  测试：`tests/test_mobile_layout.py` 17 项（触发条件只有 `ui-*`、`.field` 不许进那张表、
  门槛边界 2/3、九种读不到或不够规模的读数各自沉默、探针异常退化成"没说"、Ctrl+C 仍上抛、进程级去重）
  + `tests/test_pipeline_core.py` 加 3 项接线（空结果时出声且判定不变、有题时根本不调探针、
  用**真**探针跑一遍恒真页面要求一字不说 —— "只提示"的另一半就是"不许瞎报"）。

- **README 的覆盖率口径改成生成物**（`scripts/readme_coverage.py`、`scripts/coverage_gaps.json`、
  `tests/test_doc_consistency.py`，CI 多一步 `--check`）。要补的是"口径同步"这条重复劳动本身：
  2026-09-22 一天里发过两次标题为「README / CHANGELOG / ci.yml 口径同步」的提交，只为把同一组
  百分比抄到三处，而且缺口理由散在正文里，改代码的人不知道该同步哪几行。现在数字的唯一来源是
  `coverage.json`（CI 跑测试那步顺手产出），`--write` 重写 README 的生成块，`--check` 当门禁；
  地板 `--cov-fail-under` 反过来由 `ci.yml` 供给 README，**手写的覆盖率数字只剩地板那一处**。
  三条设计：
  ① **数字按容差比、清单按精确比** —— 批次循环里有 `random` 分支，同一环境两次实测
  `src/answering_v2.py` 是 92.1% / 94.2%（TOTAL 差 0.08pp），精确串比对会随机变红。故百分比容忍
  0.2pp、行数容忍 6 行，而缺口清单的模块**增删改名**是硬失败。
  ② **低覆盖必须登记理由**（对标 `wjx-ai-kit` 的 capability-matrix：`intentional-gap` 的 reason
  留空即红），并且反向要求"低于 60% 又没登记的模块"直接失败。上线当天就抓到两条 README 里
  从没有过的：`src/browser/__init__.py` 52.2%（`create_driver` 的 edge/chrome 分发与
  `cleanup_browser_state` 整段没有替身）、`src/interactions/choices.py` 58.8%（三个
  `@js_execute_retry` 包装函数的 `execute_script` 那行离线一次都没执行）。**只登记，未修复。**
  ③ **只在 3.13 那条 leg 上比** —— 3.10 本机量不到，拿没量过的环境断言"数字必须等于 3.13 实测"
  等于把口径差异伪装成缺陷。`--write` 还会拒绝装了 `opencv-python` / `undetected_chromedriver`
  的解释器（开发机口径实测比 CI 高约 0.2pp，v3.2 复量：`src/` +0.1、`gui/` +0.6，
  两个可选项各自改变一侧的分支走向。放任它写就会把口径悄悄换掉），确要如此用 `--force-env`。
  顺带说实三句此前不实的话：`gui/` 是 **9** 个文件不是 8 个（合计行现在由脚本数）；
  正文抄来的 674 passed / 75.6% 在本轮就已过期（实测 740 passed + 2 skipped / 76.3%）；
  「离线覆盖率不等于验证过」那句保留。
  测试：`tests/test_doc_consistency.py` 16 项 —— 哨兵在位、生成块之外不许再出现一位小数的百分比
  或"剩 N 行"、清单指向的模块与契约测试文件必须真实存在、reason 非空、地板能从 `ci.yml` 读到且不低于 70。

- **一份问卷 = 一个人**（`src/persona.py`，v3.2 作答内容真实性第一件）。
  要补的是"每一格单看都合法、连起来是个不存在的人"这类缺陷 —— 它过得了长度正则、
  过得了平台校验，只有跨字段比对才能发现，所以此前的四个各自随机的池子（姓名 /
  手机 / 邮箱 / 地址）必须换成一份画像的派生值。
  一次抽签定：地区（按省级人口粗权重）→ 性别 → 年龄段 → 学历 → 职业 → 收入 → 婚姻 →
  子女，之后
  - **身份证**：前 6 位就是画像那个「省码 + 省内市级序号」，中间 8 位是画像的出生年月日，
    第 17 位奇偶对齐性别，末位 ISO 7064 MOD 11-2 真算校验位；
  - **姓名**按性别分池（旧实现两池混抽却不与人绑），**邮箱**前缀是姓氏拼音，
    **住址**与地区题、身份证前缀同省，**年龄**与身份证出生年、生日题同源；
  - 条件链保证不自相矛盾：学生不会超过 24 岁、退休不会低于 60 岁、`children=有` 必然已婚；
  - 地址在画像里**一次算好**而不是调用时随机 —— 一份问卷里两道地址题（现居住地 + 户籍地）
    很常见，延迟随机会长出两个住址；
  - 题目把数值范围卡死时（如年龄 16~25）画像值会被夹进区间：越界直接被平台拦下，
    比"这一格与身份证不一致"更糟。
  接线在 `_do_one_submission_core` 开头换人（Step 0）：那里第一步就重新导航到问卷页，
  DOM 是干净的，所以"重试的每一次尝试算一个人"不会造成半份问卷混进两个人。
  画像放 thread-local：GUI 批次在 worker 线程跑，测试常在主线程直调 `generate_answer`，
  共用全局会让两边互相看见对方的画像。
- **地区题识别 + 只交"省 市"**（`src/detection.py` 三条平台信号）。
  `class=get_Local` / `onclick` 含 `opencitybox` / `verify` 含 省市·地区·地图·区县 任一命中
  即标 `field="region"`，题干正则把原来的"所在地区"从 address 里拆出来单独成一条。
  判成 address 会把整条街道门牌塞进级联框 —— 那是"看着填了其实不对"。
  **仍然留着的缺口**：真做成只读框 + 城市选择面板的那种地区题，探测在上游按只读跳过
  （只有日期题例外），本工具目前不填它；症状由提交前完整度自检报出来，不会静默交上去。
  测试：`tests/test_persona.py` 15 项（校验位在测试里**独立重写一份**，不 import 被测实现）、
  `tests/fixtures/mock_wjx_region.html` + E2E 一条（三条信号 + address 对照组 + 同省一致性，
  判据是 DOM 事实，只有真浏览器能验）。

- **投递分布在线纠正**（`src/distribution.py`，CLI `--drift-correct`，**默认关**）。
  加权随机只保证"每次抽样的期望分布"，不保证"真正投递成功的那几份"的分布 ——
  失败 / UNKNOWN / 中断会随机吃掉若干份，目标 3:1 完全可能落成 6:2，而用户看到的
  配置还写着 3:1。现在按**已成功提交**的答案统计实际份额，对目标权重做
  `exp(gain·warmup·gap)` 的小幅修正。三处刻意：因子夹在 ±1/3（单份样本不许掀翻分布）、
  前 8 份**完全不纠正**（个位数样本的 gap 是噪声）、修正后的权重**不回写**
  `WEIGHT_CONFIG`（用户配的那份是意图，必须始终可读可导出）。
  默认关的理由：它改变答题结果，不是防线。
  对标 `core/questions/distribution.py:116`，差别在统计口径 —— 它算计划与尝试，
  我们只算提交成功的那些（`run_one_submission` 的三态收尾处 commit / discard）。
  测试 15 项，含"开纠正后 400 份模拟的落地比例更靠近目标"的方向性断言。
- **真实答卷回放**（`src/reverse_fill.py`，CLI `--replay-file`）。
  把一份已收集到的答卷表逐份回放成提交：CSV 走 stdlib（必选路径），.xlsx 走
  `importlib` 可选导入（不为一个导出格式给 CLI 用户加必装依赖，套路同 `gui/qr_utils.py`）。
  表头认题复用 `anchoring.normalize_title`，只认相等/包含/前缀，**不做模糊匹配** ——
  认不到就是 `blocked` 并说清原因，按位置猜会把上一列的答案安到这一题头上。
  三种编码按"信文本还是信数字"的代价排序：选项全文 → 选项分值 → 1-based 序号。
  覆盖只发生在"生成答案"这一步（`_answer_one_question` 里替换 `build_answer_strategy` /
  `generate_answer_v2` 的结果），点击、DOM 回读、落盘全沿用原路径 —— 回放的答案同样
  要在真 DOM 里落上才算成功。队列是游标不是查表：同一 `submission_index` 反复问拿
  同一行，**只有提交成功才 `mark_consumed`**。多选 / 排序 / 矩阵多选本版本明确不支持
  （`unsupported_reason()` 一处说了算，认列、编码、启动告警三条路径共读它）。
  与 `--url-file` 队列互斥并直接报错 —— 多份问卷的 `submission_index` 都从 1 开始，
  同用一张表会让它们各自都从第一行答起，症状是"数据重了一遍"，比报错难发现。
  测试 80 项（含 8 项批次状态层）+ 分发链 2 项。
- **实测信度报告**（`src/reliability.py`，CLI `--report-alpha`；设计稿
  `DESIGN_reliability_alpha.md` 的 P0，**只测不改**）。
  从历史库读已落库答案，按维度算真实 Cronbach α。为什么先做这个而不是做控制：
  现在这套独立加权随机的 α 到底是多少，决定了"计划矩阵 + 秩映射"那套值不值得引入。
  两条口径上的自律：维度与反向题**必须人声明**（`dimension` / `reverse` 写在权重配置里，
  自动聚类会静默把不相干的题算进同一个漂亮的数字），以及历史库**没有逐份成败标记**
  （`answers` 在作答阶段落盘、早于点提交），所以实测值把 failed / UNKNOWN 那几份也
  算进去了 —— 这条限制写进模块 docstring，并作为 `scope_caveat` 常驻每条报告，
  报告行里也不许蒸发；`submission_filter` 参数已留好，将来加一列就能收紧。
  α 控制同期落地为 CLI `--alpha-target`（`src/plan.py`：`α↔ρ↔σ_e` + 潜变量 θ +
  按秩映射兑现精确配额 + 一维 σ_e 搜索，边际优先、α 不达标只记 notes 不改配额）。
  接线一处签名都没动：`cli.main` 里 `configure(目标 α, 本次份数)`，
  `pipeline._answer_current_page` 第一页探测完 `begin_submission` + `ensure_plan`，
  `answering_v2` 在**既有的加权采样出口**上查表（`_drift_weights`）—— 另开一条出口就
  多一处忘记校验的地方。**只到 CLI**，GUI 没有这个开关。
  本轮推送前复核抓到两处接线漏口并已修：① `--url-file` 队列里计划一进程只建一次
  （`ensure_plan` 的 built 门是为**一份问卷的分页**设的），第二份卷要么沿用上一份的
  配额、要么静默不建且不给一句原因 → `run_batch` 开头与 `distribution.start_run()`
  同址补 `plan.reset_survey()`；② `PARTICIPATING_TYPES` 把下拉列为参与题型，而
  `generate_answer` 的下拉分支没传题号，计划对下拉题根本不生效 → 症状是"计划 α 达标、
  实测 α 差一截"，两句告警都不会响。
  测试：`tests/test_reliability.py` 33 项（参考实现独立写一遍，不拿被测函数验自己）、
  `tests/test_plan.py` 44 项。

- **逐题作答回执收上来，点提交之前先说哪几题没落上**（`pipeline._answer_current_page`
  多返回一个题号集合、`completeness.describe_not_written`、Step 7.5 一行 `print`）。
  要补的是 v3.1 那条候选剩下的一半：`_answer_one_question` 的返回值（每一题填充函数
  回说的"这一下落上了没有"）此前在 `src/pipeline.py:231` 被直接丢掉 —— 于是"我们答过、
  页面上其实没写上"这件事只有在点提交之后、平台弹出"第 N 题未答"时才看得见，而那时候
  日志里只剩一条看不出原因的失败。
  **报，但不拦**，这一条是本轮的设计核心：`False` 有两种形状 —— 交互层明确说控件没找到
  （真没落上），或作答中途抛异常被降级跳过（也许已经落上了一部分，只是没读到回执）。
  拿它当拦停依据，就是把"认错一单"的代价从一句废话换成一单本来能交的问卷被判失败，
  与本仓库 `completeness` 那句"宁可少拦，不能拦错"相反。所以默认路径上它只是多一行日志，
  判定与提交行为逐位不变。
  `--rescue-gaps` 打开时它才多出一步：**等着**的集合并入这些题（人点两下就能补），
  复检按"已答扫描现在读得到值"消解；**等不到也照提交** —— 拦停集合始终只有
  "平台标了必答而我们整题没探测到"那一类。无头模式不等待，与 v3.1 同一取舍。
  实现上一处签名外扩：`_answer_current_page` 的返回从三元组变四元组（两个既有 stub 同步
  改掉），跨页在调用方取并集 —— 只看最后一页会把前面几页没落上的题漏掉。
  测试：`tests/test_pipeline_core.py` 加 8 项（收到并报出题号、续填跳过的题不算回执失败、
  全落上一字不说、默认关绝不拦提交、开关打开时等的是那道题且停止谓词透传、消解后不再补一句
  解释、复检的读页消解条件、已答扫描抖一下不许在这里放行）+ `tests/test_completeness.py` 加 2 项
  文案契约（那一行必须自己讲清"为什么不拦"，且不许把"没落上"说成"没探测到"）。
  真浏览器侧：23 项 E2E 全过且**一行 `[作答回执]` 都没出现** —— 这一行在形状正常的页面上
  零误报，是它该有的一半行为；反过来，"确实没落上时报得出来"目前只有离线证据。

- **人工提交：把最后那一下点击交给人**（CLI `--manual-submit`，默认关；
  `src/pipeline_stages/manual_submit.py`，分叉点在 `pipeline` 的 Step 8）。
  要补的是"先验证再放开"这条路上一直没解决的事实：半份问卷一旦交上去就是平台上一条
  **收不回来**的真实回收记录，而此前唯一的确认手段是 `-n 1` 跑完再读日志 —— 读到日志时
  那份已经交了。开关打开后：答完、跑完三道提交前判据，把提交按钮滚进视野并停住，
  人在窗口里核对（要改的直接改）然后**自己点**。
  三条设计决定：
  ① **成功与否仍用同一套判据** —— 认的就是 `_wait_until_submit_effect` 那个成功脚本
  （URL 变化或"提交成功/感谢您的参与"强文案），不另立标准；否则人提交的那一份与自动
  提交的那一份在历史与成功率里不可比。
  ② **没人点不是"未知"而是"没交"** —— 超时返回 FAILED 而不是 UNKNOWN：工具根本没点提交，
  平台上不留任何记录，算成"交了没确认"是谎报。
  ③ **与 `--headless` 互斥，在 argparse 就退 2** —— 不静默降级成"无头时自动提交"，
  那等于把用户以为已经取消的动作替他做了。
  与补漏轮的分工也写进模块 docstring：那边等的是**我们答不了的题**（人补完、工具点提交），
  这里等的是**那一下点击本身**（人看完整份、人自己点）；两个开关同时打开是先补答后交人点。
  等待按 1s 切片睡（`--stop` 与 Ctrl+C 不用等满 180s）、每 30s 一行进度、人工介入锁
  在等待期间处于 holding 且 `finally` 必还（与验证码/补漏轮同一条红线）。
  测试：`tests/test_manual_submit.py` 14 项（两个出口、锁的生命周期、基线 URL 读不到时
  不许拿 "" 兜底、瞬态失败继续轮询、只滚不点、选择器沿用 `submit.SELECTORS` 不抄第二份）、
  `tests/test_pipeline_core.py` 加 4 项接线（走人工路径时 `find_and_click_submit`
  **一次都不调用**、超时不补一次自动提交、开关关时连问都不问、停止谓词透传）、
  CLI 三处 4 项（默认 False、写了必须传到那一轮、与 --headless 同时给退 2 且不进批次）。
  **两条真浏览器证据**（这一条只能在真 DOM 上成立）：新 E2E 用 mock 页自带的
  `window.__submitClicks` 计数器钉住"等待路径一次都没程序点击"，以及"人点下去之后
  同一套判据认得出这份提交"。E2E 23 → 25 项。

### 测试与门禁（对标 SurveyController 借来的四件事）

第六个对标源：[SurveyController/SurveyController](https://github.com/SurveyController/SurveyController)
—— 143★、GPL-3.0、86k 行 Python，适配问卷星 / 腾讯问卷 / Credamo 的**纯 HTTP 高并发**
提交器（PySide6 + QFluentWidgets，发 .exe）。技术路线与本项目相反（绕开浏览器直接重放
提交接口），所以**借下来的全部是工程分层，一行代码都没取**（GPL-3.0 的码不能进 MIT 仓库）。

它同时也反向印证了三条既有口径：它的代理池里身份证前 6 位从 368 个市码里 `random.choice`、
与出口代理省份互不知情（`software/core/questions/utils.py:212-223`）；提交判定是
`"success" in text or text.startswith("10")` 这种子串且**没有"未知"态**
（`wjx/provider/http_runtime.py:312-326`）；POST 超时按"代理不可用"换 IP 重跑整轮
（`:279-287`）。三态判定 + 幂等写入正是对着这一类问题写的，不动。

- **弹窗与文件框变成可注入的出口**（`src/dialogs.py`）。借它
  `log_popup_*` + `register_popup_handler` 那层（`software/logging/log_utils.py:642,799-811`）：
  GUI 启动时注册真 `messagebox` / `filedialog`，CLI 与测试不注册就拿到**确定性默认值**
  （提示类无操作，确认类与选文件类都是"否 / 取消"—— 无人应答时不做需要人拍板的额外动作）。
  宿主抛异常时按默认值走并记一条日志：关窗竞态下 Tk 已销毁，弹窗不该把 worker 线程带走。
  替换掉 `gui/` 里 **13 处**模态框直调（9 处 `messagebox` + 4 处 `filedialog`），并顺手
  给真实现补上了此前没传的 `parent=self.root`（弹窗落到主窗口背后、却挡住主窗口点击）。
- **断点续传决策从 `_on_start` 里拆出来单独测**（`gui/app.py::_apply_resumable_run`）。
  确认框点"是"之后那 5 个字段（`resume_start_idx` / `run_id` / `success_count` /
  `total_target` / `attempts_cap`）必须互相自洽，是全 GUI 最容易造成**重复提交**的地方，
  过去它罩在模态框底下一次没被测过。现在钉住：只问一次、答"是"后
  `resume_start_idx + attempts_cap - 1 == total_target`、答"否"仍恢复权重、
  查库抛错只记 WARN 不影响启动、查询键跟着 `url[:500]` 截断。
- **`WJX_USER_DATA_DIR` 把用户数据树整棵挪走**（`gui/app.py::user_data_root` 等三个解析函数）。
  借的是它 `CI/conftest.py:47-58` 那套"环境变量指到 tmp + autouse 夹具"的隔离做法。
  于是 `SurveyGUI` **第一次能在测试里被真的构造出来**（`tests/test_gui_user_data.py`：
  构造、两处输入校验、历史库接线、配置导出→导入往返、另存默认配置只写进被指到的那棵树），
  并且测试结束时断言仓库里的 `data/history.db` 与 `configs/default_weight_config.json`
  指纹没变过。路径解析刻意放在**调用时**而不是模块级常量 —— 否则测试 import 之后再设
  环境变量就晚了。
- **根窗口提到 `conftest.py` 做成会话级唯一**（顺带修掉一个既有隐患）。Windows 上一个进程
  只能可靠地建**一个** `tk.Tk()`，第二个会抛 "Can't find a usable tk.tcl"；此前
  `test_gui_panels.py` 自己建自己销，任何"先建根窗口的模块排在前面"的顺序都会让后面的
  GUI 模块**整片静默 skip**（实测 51 条）。现在根窗口建一次、只 withdraw、不销毁，
  需要整窗的用例挂在它的 `Toplevel` 上。
- **E2E job 从非阻塞改成阻塞**（`tests/conftest.py` + `scripts/e2e_gate.py` + `ci.yml`）。
  借它的回归白名单思路（`CI/live_tests/test_live_runtime_regression.py:16-22`）：命中
  "浏览器/驱动自己起不来"那组正则的失败降级为 skip 并打出条目数，于是剩下的红只剩
  "代码回归"一种解释，这个 job 才敢变成必填。它**没**借的另一半是"已知不支持题型"白名单 ——
  那是给真卷准备的，我们的 E2E 跑本地 mock。配套堵住这套机制唯一的作弊面：
  `e2e_gate.py` 数 junit 的数字，**全 skip 的 job 判红**（退出码三态：0 通过 / 1 一条都没
  真跑 / 2 报告读不了）。
- **类型抑制禁令**（`tests/test_ci_guards.py`）。借它 `CI/python_checks/common.py:32-41`
  的"零豁免"，但改成基线制：`src/` + `gui/` + 入口里出现任何未登记的
  `# type: ignore` / `# pyright:` 即红，现存 3 处各自带理由登记、**只准变小**（基线比
  实际大同样判红，它在掩盖已经修好的东西）。用 tokenize 只认「注释本身以抑制指令开头」，
  所以 `gui/qr_utils.py:15` 那种"解释为什么不用 ignore"的散文注释不会误伤。
  没借的一条是它禁 `\uXXXX` 转义 —— 本仓库 `src/answering_v2.py` 用它判中日韩字符区间，
  那是代码语义不是文案。
- **Dockerfile 有了构建门禁**（`.github/workflows/docker-smoke.yml`）：每周一 + 手动
  跑 `docker build` → `wjx-fill --help` → `import src.*`，只构建不发布。CHANGELOG 3.0.0
  末尾"未在本仓库 CI 里构建过"那条已知限制就此解除，代价是它不在 push 的必填检查里 ——
  这句话同时写在 `Dockerfile` 头上和 `tests/test_packaging.py` 的断言里，两边口径以
  workflow 的真实触发键为准。

效果：离线覆盖率 **76.3% → 83.7%**（CI 口径；`gui/` 58.4% → 78.1%，其中
`gui/app.py` 25.0% → 71.1%、`gui/controller.py` 13.5% → 37.1%，`src/dialogs.py` 100%），
用例 758 → 799。地板**仍留在 70** —— 与 README 那句话说的一致：3.10 那条 leg 本机量不到
就不抬。`src/interactions/sort.py` 22.2% 是新登记的一条缺口（v3.1 点击式排序的函数体
只有 E2E 走过），本轮不替别人的批次编理由，只把账记下。

没借的方向也说清楚：并发 slot 池、Nuitka + Velopack 安装包与自动更新链路（本项目不发
.exe）、`answer_datetime_window`（Credamo 专有配额参数）。
- **提交区协议框诊断：只提示、绝不代勾**（`detection.consent_notice` +
  `platforms.WJX_CONSENT_IDS` / `WJX_CONSENT_KEYWORDS`，接线在 `pipeline` 的 Step 7.6，
  点提交之前）。补的是三道判据共同的盲区：移动端投放模板在提交按钮旁挂一个隐私协议同意框
  （`#checkxiexi`），不勾平台就把提交原样弹回来，而它**不在 `#fieldset1` 的题目容器里** ——
  逐题探测看不见它、结构对拍看不见它（平台没把它当题）、完整度自检也看不见它（没有 `req` 属性）。
  于是日志里一切正常、提交没反应，而"没反应"在本工具现有的词典里只对应"网络"或"探测漏题"两种解释，
  两种都指向错的责任方。
  两条边界各守一头。**不代勾**：同类油猴脚本把这一步叫"协议秒签"并当作卖点，但替一个编造的
  人设签署隐私协议与替他答一道题不是同一件事 —— 前者是只有真人能行使的同意动作，
  与"只接管 `alert`、刻意不替页面回答 `confirm`"（`interactions/_scripts.py`）是同一条线。
  **不拦停**：兜底那条判据是"不在题目容器里 + 相邻文案含同意/协议/隐私"，认错框的代价不该是
  一单本来能交成的问卷被判失败，所以本行说完照旧点提交 —— 与 `completeness` 那句
  "宁可少拦，不能拦错"同一条。容器条件是必需的：少了它，"我同意接收后续邮件"这种正经多选题的
  一个选项就会被报成协议框。读数拿不到（None / 非字符串 / 坏 JSON / 计数 0）一律沉默，同一句话每进程只印一次。
  诚实记账：`#checkxiexi` 这个 id 来自同类脚本对移动端模板的用法，**本仓库还没在真卷上见过这个框**
  （见过的形态都是 PC 端题目容器），所以它是"按已知形状预防"而不是"复现过的缺陷"。
  测试：`tests/test_consent_notice.py` 13 项（出声那侧只允许说一句话不许点框、id 与关键词当参数传
  不许拼进 JS 源码、六种读不到各自沉默、探针异常退化成"没说"、进程级去重第二次连页面都不问）
  + `tests/test_pipeline_core.py` 加 3 项接线（说了那句话之后**提交照点**、自检已拦下时不去问提交区、
  真探针跑恒真页面一字不说）。
  **E2E 抓到一条离线造不出来的缺陷**：新 fixture `mock_wjx_consent_box.html` 第一次跑就把同一个框
  数成了 2 处 —— `#checkxiexi` 的相邻文案本来写着"我已阅读并同意《隐私协议》"，于是 id 那条与
  文案那条各命中一次。离线 FakeDriver 只喂得出一个返回值，喂不出 `label[for]` 关联与
  `closest('div[topic]')` 祖先链这棵树，所以这条只有在真浏览器里才现形（修法是文案那条循环跳过
  已被 id 那条收走的元素）。同一个 fixture 另钉住两条：多选题里那个文案为「我同意接收后续邮件」的
  **选项**不许被报成协议框；框已勾上时整句不出声。E2E +2 项（本分支与 v3.2 合流之后
  该文件共 23 项，绝对数以 README 为准）。
- **README「不在本工具范围内」补一行「大模型代答 / 人设化答案」**：这一条此前只活在
  会话记录里（那份规划稿不入库）、没进 README 那张表，而同类里已有**两个独立来源**做了这件事
  （`woshicainiao6/autoQuestionnaire` 的填空题、`kelryry/wjx-auto-sniper` 直接把它写进标题），
  继续不写就等于把一条会被反复重新论证的取舍留在文档外面。理由那栏把代价一起说清：
  语义连贯的陈述会把工具从"验证自动化流程"推向"造问卷数据"，外加网络/计费/密钥三类失效面，
  以及**题干原文拼进 prompt 的注入面**（卷面写一句"忽略上面的要求"，模型照做、脚本照填照提交）。

### 修复

- **`tests/test_packaging.py` 在 3.10 那条 leg 上把整套离线测试拖红**：它顶部裸
  `import tomllib`，而 `tomllib` 是 **Python 3.11 才进标准库**的 —— 3.10 上这不是一个
  失败的用例，而是**收集阶段**的 `ModuleNotFoundError`，pytest 直接中断，733 项一起没跑。
  CI 表现为 `test (3.10)` 红、`test (3.13)` 绿，和本仓库上一次 `Tk.after_info()` 那个
  基座缺陷同一个形状。改为 `pytest.importorskip("tomllib")` 做模块级 skip：这些契约
  （pyproject / requirements / 版本三方对齐）本身与解释器版本无关，且在 3.13 那条 leg
  上照样真跑，少一条 leg 覆盖不损失任何判定。
  > 根因是上一轮自己写进 README 的那句「3.10 本机量不到，这台机器只有 3.11/3.12/3.13」——
  > 量不到就别拿它断言数字，是对的；但**同一理由不能用来放过 3.10 上的可运行性**。
  > 已用隔离的 3.10.21 环境按 CI 原样命令复验：733 passed / 3 skipped，覆盖率 76.31% 过地板。
- **README 代码块注释里还留着一个手抄的过期计数**：「离线套件（…，676 项；CI 口径
  674 passed + 2 skipped）」与 7 行之后「离线 742」自相矛盾，实测 742。新增的
  `test_doc_consistency.py` 只禁止**生成块之外**出现一位小数的百分比，整数计数不在其列，
  所以这个洞没被拦住 —— 已按实测改为 742，并去掉无法本地复验的「CI 口径 674 passed」那半句。
  （同一周期内下面的题型修复又加了 30 项，本批结束时最新实测是 **758 离线 / 20 E2E**；
  这正是"手抄数字"的病：只要它还是手抄的，一个周期内就会再过期一次。）
- **排序题在真页面上整题认不出来**（当时写作「八类题型全覆盖」，其中排序题那半句被真卷证伪）。
  探测要求列表 class 含 `sort`（照老页面 `ul.lisort` 写的），而 2026-09-22 那份真卷的
  排序控件是 `ul.ui-controlgroup.ui-listview` + 每个 `li` 一个 `span.sortnum` 与
  `input[type=hidden][name=qN][value=序号]` —— class 里没有 sort，于是这道题连进不了
  探测结果，症状是"必答题静默失踪"。现在 5b 认两种形态并用 `sort_mode` 分开：
  `"value"`（老形态，一个隐藏域装逗号串）与 `"click"`（新形态）。
  **新形态只能按目标顺序点击**：那些隐藏域的 `value` 恒为 1,2,3 不变，排名只写在
  **li 的 DOM 顺序**里，提交时同名 input 按 DOM 顺序一起交上去 —— 照老办法往第一个
  input 写 `"3,1,2"`，等于把选项 1 的身份换成一串数字，交上去是脏数据，
  比"整题没答"更难发现。
  验证方式（不动服务端一行数据）：`FormData.get('q12')` 就是点提交时服务端会收到的
  那一列 —— 真卷上按 `3,1,2` 与 `2,3,1` 两种顺序各走一遍本工具的作答路径，
  `FormData` 读数与目标序逐位一致、`.sortnum` 名次 1..3 落位、三个隐藏域的 value
  仍是 `1,2,3`。E2E 另加两条：一条用 `mock_wjx_real_widgets.html`（内含 jqmobo2.js
  点击行为的**复刻**，注释里写明它不是我们的代码、复刻走样时先怀疑它）；
  一条把点击吃掉后要求返回 False —— 名次没落地点不交，与既有底线一致。
- **日期题被"只读就跳过"这条规则误伤，整题失踪**。真卷上是
  `input#q15.datebox[data-role=datebox][verify=日期][readonly]`：值由 laydate 面板
  选完回填，所以它天生只读，而探测的填空那一步开头就是 `if (el.disabled || el.readOnly) return;`
  —— 那条规则是用来挡量表 / 矩阵的隐藏存储框的，**不能整体放宽**。现在只放日期框过去
  （`class=datebox` / `data-role=datebox` / `verify` 含日期时间，三种信号任一命中），
  并把它标成 `field="date"`（平台题型码仍是 `1`，它本来就是填空题，码表不用动）。
  字段类型现在**先问平台的 `verify` 属性、再用题干正则猜** —— 真卷上"出生日期"那个框
  的题干不含日期正则的关键词，靠猜是猜不出来的。
  同时把 laydate 的边界一起带回来（`datelimit` = "min|max"、`datelimittype` = 月 / 日 / 时分），
  因为**范围外的值会被平台自己的校验清掉**：辛辛苦苦填一个，交上去那题还是空的。
  两端都没有时默认落在 18~55 年前 —— 题干是"请输入您的出生日期"，
  随机到今天附近会答出一个婴儿的生日。真卷实测：生成 `1991-05-12` → 写入 →
  `FormData` 读数一致。

### 已知限制（诚实记录）

- `--manual-submit` **只到 CLI**。GUI 其实是最自然的落点（它本来就是"看着窗口跑"的入口），
  但要多一个"等人点提交"的运行态，得动 `gui/app.py` 的循环与按钮状态机 —— 那里至今只有
  25% 的离线防线，本轮不碰。README「不在本工具范围内」那张表**没有**把它写成"刻意不做"，
  因为理由不成立（无头才对 GUI 没有意义）；它是待办，不是取舍。
- 本版本号的口径仍未收口：`src.__version__` 与 `pyproject` 锁在 3.0.0，而未发布块里已经
  堆了 `v3.1-dev` / `v3.2` / `v3.3` 三个批次（详见块首那三行说明）。

## [3.0.0] - 2026-09-22

v3 主线：**从"只能本机开着窗口跑"往可部署、可停、可维护走**，同时补上题型覆盖。
五个批次（停止谓词 / 题干锚定 / 新题型 / 平台层 / 运行形态）逐条带防线，
每一条都在真实浏览器 E2E 上复量过 —— 这一轮 E2E 抓到 4 个离线全绿的缺陷，
下文"修复"一节按现场列出。

### 新增

- **停止谓词贯穿单次提交**（`src/exceptions.py::SubmissionAborted`、`src/pipeline.py`）。
  v2.6 只让**轮间**停顿可打断，逐题边界与每题"思考时间"（均值 ≈4.5s/题）打不断，
  点了停止仍要等整份问卷答完并**真的点到提交**。现在：逐题边界、每题停顿
  （`human_pause(abort_check=)`）、等题轮询、验证码人工等待都以 ≈0.2s 粒度问一次；
  被打断的那一份**不提交、既不计成功也不计失败**，批次以 `interrupted` 闭合，
  下次 `--resume` 从这一份重来。
  - 刻意继承 `BaseException`：提交路径上有十几处为"浏览器偶发异常"写的
    `except Exception`，停止信号若继承 `Exception` 会被就地吞成"本题失败、继续下一题"，
    最后照样提交 —— 那正是要防的后果。
  - 提交成功之后才到的停止信号**不改判这一份**（`except SubmissionAborted: pass`）：
    成功已是既成事实，抹掉会让成功数少一、`--resume` 起点跟着错。
- **权重配置支持题干锚定**（`src/anchoring.py`，JSON schema **3.0**）。
  键仍是题号，但每条可选 `anchor = {title, signature}`；GUI「探测题目 → 另存」自动写入。
  三条契约：① 带 anchor 的条目**只**按锚点生效，认不到题就走等权并提示一次，
  **绝不退回答题号**（错位从来不报错，只是安静地给出错的分布）；② 不带 anchor 的老
  配置行为与 v2.8 逐位一致，schema 2.0 文件照旧可读；③ 锚点优先于题号。
  题干归一化会剥 `1.` / `第2题` / `（3）` 这类序号，但不会误剥 `2024年收入`；
  结构签名（`single:4` / `scale:2-10` / `matrix:4x5` / `sort:4`）变了即脱钩。
- **四类新覆盖**：
  - **矩阵多选** `matrix_multi`（探测按行内是 radio 还是 checkbox 分流；与单选共用一个
    JS 生成器；每行勾几个由 `pick_options` / `pick_weights` 控制，默认 1）
  - **排序题** `sort`（`ul.lisort` + 隐藏 `input[name=qN]`；同时重排 DOM 与写提交值，
    任一条做不到就整题判失败）
  - **多分页问卷**（`src/pipeline_stages/page_nav.py`：≥2 个分页容器才承认是分页，
    翻页后按可见题号继续作答；`no_more` / `advanced` / `failed` 三出口，
    `failed` 时**不点提交**）
  - **NPS**：`nps` 作为 `scale` 的别名（DOM 与作答完全同量表，单列题型只会复制分支）
- **平台适配层**（`src/platforms.py`）。平台专属选择器此前散在 5 处，现在是一张
  `SurveyPlatform` 表 + `platform_for_url()`；`_scripts.SUBMIT_SELECTORS` /
  `page_nav.NEXT_PAGE_SELECTORS` / `page_loader.QUESTION_CONTROL_SELECTOR` 都成了它的
  派生视图。**明确没做的部分也写在文档里**：题型识别与作答注入的 JS 仍是问卷星 DOM
  约定，换平台要换的是那些脚本，不是这份常量表。附带一条真实能力：域名不在已适配
  范围时批次开始就提示（此前表现为"探测不到题目 → 整批失败"，容易被误读成适配 bug）。
- **运行形态**：`pyproject.toml` + `wjx-fill` / `wjx-gui` 控制台入口 + `Dockerfile`；
  CLI 新增 `--headless`、`--profile-dir`、`--url-file`（顺序队列，每行 `URL[,份数]`，
  与 `--resume` 互斥）、`--max-total-time`（到点按优雅停止收工，可续传）。
- **同一份问卷的批次身份**（`platforms.canonical_survey_key` + `runs.survey_key` 新列）。
  同一份问卷的答卷链接有 `jq`（电脑）/ `m`（移动）/ `vm`、`vj`（短码）/ `hj` 几种投放形态，
  微信还会往 query 上挂 `kd`、`source` 一类可变字段 —— 而 `find_resumable_run` 此前是
  `WHERE survey_url = ?` 字符串相等。对不上时的样子不是报错，是**安静地不弹续传提示**、
  权重快照找不回来、统计被拆成几行。现在批次匹配走归一化键，v2→v3 迁移会把老库全部回填
  （留 NULL 等于没修）。刻意**不**跨 host 合并、短码大小写**不**归一：误并批次的后果是
  拿另一份问卷的中断计数去续传 → 重复提交，比少并（重跑一次）贵得多。
- **选项自带填空框（"其他____"）**。`detect_questions` 标出 `blank_options`，判据是结构性的
  （选项自己的 `label`/`li`/`td` 里住着一个文本框），不是 `class="underline"` 那类模板产物；
  勾中这些项时同时写入短文本并按 `maxlength` 截断（程序赋值不会被浏览器裁，服务端却按超长拒），
  没写成功就把本题记为失败。已答扫描同步收紧：**勾了却没写字不算已答**，续填才会重做它 ——
  此前那种状态会被跳过，交上去只剩一个看不出原因的 unknown。探测与作答共用同一段定位 JS
  （`option_blank_helper_script`），两边各写一份必然漂移。
- **接管 `window.alert`**：切到题目 frame 后把 alert 换成记录器，提交非成功时把攒下的文案
  作为失败原因打进日志。两件事一起解决：原生弹窗不再让下一条命令抛
  `UnexpectedAlertPresentException`（整轮被当成瞬态异常重跑，重跑之后原因早就没了），
  而问卷星必填校验那句"您第 N 题未填写"本来是页面自己说出来的。**只接管 `alert`**：
  `confirm`/`prompt` 的返回值是页面控制流的一部分，替用户回答"是"就是替用户提交；
  也不学同类脚本那样在 alert 里 `location.reload()`（那是把"页面在报错"变成"页面重来一遍"）。
- **测试**：离线 **509 → 676 项**（CI 口径 674 + 2 skipped），E2E **5 → 12 项**。
  新文件 `tests/test_anchoring.py`(46) `test_page_nav_and_sort.py`(18 → 30)
  `test_runtime_forms.py`(13) `test_packaging.py`(7) `test_question_stage_dispatch.py`(3 → 7)。
  批次键、选项填空、alert 接管三处各自补了离线契约 + 真浏览器用例（后两处的离线断言只到
  "注入的脚本长什么样"，页面里到底成不成立仍由 E2E 说）。

### 修复（4 条全部由真浏览器 E2E 抓到，离线 mock 一律绿）

- **矩阵多选勾不上**：共用生成器里"先 `checked=true` 再补一个合成 click"对 radio 无害
  （点只会选中），对 checkbox 却是**再翻回未选中** —— 脚本返回 True、页面一格没勾。
  现在按 `r.type` 分流，复选框走原生 `click()`。
- **排序题探测不到**：`closest('.field, ..., [id]')` 会先匹配到 `<ul>` 自己
  （它有 `id="q13_list"`），scope 缩成一个查不到隐藏域的节点 → 整道题静默消失。
- **翻页兜底会点到包裹 `<div>`**：文案匹配遍历 `span/div` 时，装着按钮的容器
  `textContent` 也是"下一页"且自身可见 → 点中一个没有翻页语义的 div，白等 8s 后判失败。
  现在只允许叶子节点。
- **矩阵题题干取不到**：`closest` 停在控件包装层 `.field`，而题面是它的兄弟节点 ——
  改为从命中元素**逐层往上爬**找题面；矩阵题的 `name="qN_R"` 也补进了定位候选。

另外三条不是 E2E 抓的：

- **矩阵行权重与填空候选词此前从不生效**（README 一直写着这两个配置项）。
  `answering_v2.generate_answer` 只读 `question["row_weights"]` /
  `question["options"]`，而 `detect_questions` 回来的题目**永远没有**这两个键 ——
  于是 `--config` / GUI 表格里填的矩阵分布和候选词全部静默走内置随机。
  现在经 `anchoring.lookup_weight_entry()` 查全局配置，并把 GUI 产出的字符串行号键
  统一成 `int`（`row_weights.get(1)` 在 GUI 那条路上此前永远查不到）。
- **无头模式撞验证码 = 白等 120s**：`wait_for_manual_verification` 现在一进函数就
  在无头实例上返回 False（不挂人工介入锁、不刷新页面）。等的是一个不存在的人，
  停止谓词也救不了"没人能点"。
- **E2E 用例之间共享 DOM 状态**：`driver` 是 module 作用域且页面只加载一次，
  上一个用例勾过的 checkbox 会留在页面上 —— "每行各勾 1 个"实际测的是累计值。
  加了 autouse 的 `_fresh_page` 逐用例重载页面，用例现在可以任意换顺序跑。

### 变更（门禁与口径）

- `README`「六类题型」→ 八类；权重配置 schema 2.0 → 3.0；`--resume` 与 `--url-file` 互斥；
  `--resume` 的匹配口径由"URL 字符串相等"改成"归一化问卷键相等"（README 新增一节
  「什么算同一份问卷」，把不跨 host 合并这条取舍写在明处）。
- 覆盖率实测 **75.6%（CI 口径）/ 75.7%（装齐可选依赖）**，`src/` 87.5~87.6%、
  `gui/` 58.4~59.0%。地板**仍留 70**：`3.10` 那条 leg 本机量不到（这台机器只有
  3.11/3.12/3.13），v2.8 定的"不拿没量过的环境赌门禁"照旧生效。
- `tests/fixtures/mock_wjx.html` 11 题 → **13 题**（新增矩阵多选 Q12、排序题 Q13），
  Q2 多选的最后一项改成**自带填空框的"其他"**；页面脚本照问卷星的做法加了必填校验
  （勾了带框的项却没写字 → 弹 `alert` 且不给成功文案）。有了这条平台规则，
  补文本与接管弹窗两项能力才算被真浏览器验过，而不只是替身对象上的断言。
  新增 `tests/fixtures/mock_wjx_multipage.html`（两页问卷）。
- `history.runs` 多一列 `survey_key`（v2→v3 迁移，含一次全表回填，只在升级那一次跑）；
  `detection.detect_questions` 的题目字典对 single/multi 多一个可选键 `blank_options`
  （值是 option value，不是下标）。两者都不影响 `answers` 表与 CSV 导出的既有列。
- `history.answers.options_selected` 值域变宽：排序题写入的是 item id 序列（可能是字符串），
  矩阵多选是各行勾中列值摊平。落库列本来就是 JSON 文本，无需迁移。
- **明确不做**（评估过、写在这里免得反复重提）：
  代理 / IP 池（与 README 免责声明正面冲突，且问卷星按服务端真实 IP + cookie + 智能验证
  计数，同类项目的 XFF 做法本来就是不稳定的）；并发 worker（同时开 N 个浏览器直接稀释
  「正态分布人类行为」这条立身点，还会让 `ManualHoldLock` 与人工介入的前提崩掉）。
- GUI 侧**没有**跟上 v3.0 的运行形态开关（无头 / profile / 队列 / 时限都只在 CLI）：
  GUI 是"看着窗口跑"的入口，无头对它没意义；队列与时限要的是无人值守，那是 CLI 的场景。

### 已知限制（诚实记录）

- 排序题与多分页的结构假设来自问卷星的公开 DOM 约定 + 自建 mock，**没有在真机分页/排序
  问卷上验证过**。失败模式是显式的（探测不到该题 / 整份判失败），不会静默交出半份问卷。
- `Dockerfile` 未在本仓库 CI 里构建过（没有可用的 docker runner），文件里就写着这件事。
- `gui/controller.py` 14%、`gui/app.py` 25% 仍无离线防线，与 v2.8 同因（要真实 driver
  或模态对话框）。本轮新起的 `src/pipeline_stages/question_stage.py` 从 29% 抬到 **63%**。
- 问卷键**不跨 host 合并**：同一份问卷若一次走 `www.wjx.cn`、一次走 `v.wjx.cn`，
  仍算两个批次、续传不会串起来。并过来的代价是拿别的问卷的中断计数去续传（→ 重复提交），
  所以这条宁可留着不修；`canonical_survey_key` 的文档里写着理由。
- "其他____"那格填的是内置短文本池（`answering_v2._OPTION_BLANK_TEXTS`），**不能按题配置**。
  权重配置的 `options` 目前只服务填空题，扩到选项级要重新设计条目形状（一个题号下
  既要选项权重又要"第几项写什么"），这一轮没做。

## [2.8.0] - 2026-09-21

针对 v2.7.0 全面评估报告（`CODE_REVIEW_v2.7.0.md`）的整改批次。逐条复测过：
报告的门禁数字与代码定位基本可复现，两条 P2 都成立，按下表落地。

### 修复

- **P2-1 出厂默认权重污染每一次 CLI 运行**（数据正确性缺陷，不是配置卫生）。
  `src/config.py` 里残留过一份特定真实问卷的 Q1–Q22 偏斜权重，而 CLI 不传
  `--config` 时**从不调用** `apply_weight_config(replace=True)`，`answering.py:57`
  又直接读那个全局 dict —— 实测不传配置时 Q1 四选项落在
  `0.244/0.394/0.263/0.099`、Q5 落在 `0.01/0.05/0.10/0.65/0.19`，与内置值逐位吻合，
  和 README「未配置的题目自动降级为等权重随机」正好相反，且会被 `cli.py:818`
  写进批次快照。现在出厂默认是 `{}`，原值迁到 `examples/weight_config.example.json`
  （**不是**报告建议的 `configs/` —— 那是运行产物目录、已在 `.gitignore` 里，
  示例放进去就永远进不了版本库），并删掉 L33 的代码生成器占位注释。
- **P2-2 pipeline 兜底 except 静默吞异常**：`pipeline.py` 的 body 就绪探测、
  断点续填扫描、提交后验证码二次探测三处补 `format_exc_log` 留痕，与
  `question_stage.py:133` 同一写法。**报告点名的第 4 处（L270）经复核是误伤**：
  它在 `except Exception as e` 分支内部，主异常已打印并随后 `raise` 重抛，
  被吞的只是次生的 `switch_to` 清理异常 —— 正是该写的写法，未动。
- **P3-5 成功判定弱关键词**：`已完成` 从 `submit_success_detect_script()` 移除。
  它太常出现在页面自带文案里，6 秒窗口内命中一次就把失败判成成功，方向危险；
  真完成页必然同时命中「提交成功 / 感谢您的参与」或成功提示容器，代价只是退回
  本来就保守的 `UNKNOWN`。
- **P3-4 续传上限口径**：`attempts_cap` 此前只减 `resume_done`，而两个分支的上限
  都是**尝试次数**语义，于是续传后总尝试数会凭空多出 `resume_fail` 次。
  现在按「已尝试 = 上次成功 + 上次失败」扣减。这改了一条既有测试的期望值
  （`test_cli_batch.py` 里那句 `attempts_cap = 5 - 3 = 2` 注释正是旧口径）。
- **Nit**：删 `cli.py` 的死导入 `SUBMIT_SUCCESS`。

### 新增（测试）

离线套件 **493 → 509 项**（全量 514 = 离线 509 + E2E 5；CI 口径 507 passed + 2 skipped）：

| 新文件 / 新用例 | 钉住的契约 |
|---|---|
| `tests/test_config_defaults.py`（6 项） | 出厂 `WEIGHT_CONFIG` 必须为空、空配置真的等权、示例文件仍可 `load` + 过校验 |
| `tests/test_gui_proxies.py`（7 项） | 面板薄代理：拿得到就原样转发、拿不到（root 未就绪）静默跳过 |
| `test_interaction_submit.py::test_weak_keyword_alone_is_not_success` | 只有「已完成」时不得判成功 |
| `test_interaction_submit.py::test_persistent_bug_logged_once_and_stays_unknown` | 轮询期的纯代码异常留痕恰好一次，控制流不变 |
| `test_cli_batch.py::test_resume_cap_subtracts_consumed_attempts` | 续传上限按已尝试次数扣减 |

**为什么 493 个全绿的测试没抓到 P2-1**：`test_answering.py` 的 `setUp` 无条件
`WEIGHT_CONFIG.clear()`，所以"未配置走等权"那条用例验的是一个人造的空字典，
从来没验过出厂值。因此新测试用 `subprocess` 新起解释器读 `src.config` ——
本进程内那个全局随时会被别的用例改写，"出厂那一刻是什么"在这里不可知。
把旧权重塞回去做变异检验：2 条用例立刻红。

### 类型门禁扩面到 `gui/`（报告列为"中期"的 P3-1 一并做完）

`pyrightconfig.json` 的 `include` 现在有 `gui`，45 条诊断全部清零，**没有一条靠
`# type: ignore` 豁免**。三类改造：

- **Tkinter 上动态挂的属性** → 类级声明：`SurveyGUI.FONT_*`（`_setup_theme()` 一直
  是用 `setattr` 挂的，只加注解不建属性，所以 `hasattr(self, "FONT_NORMAL")`
  那两处"主题未就绪"守卫照旧生效）、`widgets.CardFrame`（`make_card` 的容器改用
  这个 `tk.Frame` 子类，`_card_canvas` / `_card_body` 从"凭空赋值"变成有类型）。
- **可选导入留下的 `Callable | None`** → 在闭包内重新收窄一次（`controller._worker`：
  外层的 `if create_driver is None: return` 不会传进闭包，pyright 仍认为可能为 None）。
- **推断过窄的字面量** → `weight_panel` 的 `cfg` 显式声明 `dict[str, Any]`
  （同一份 cfg 后面还要塞 options / rows / cols / row_weights）。

顺带两处：

- **`try: import x` + `# type: ignore` 这个可选依赖写法被换掉**（`gui/qr_utils.py`
  的 cv2、`src/browser/driver_factory.py` 的 undetected_chromedriver）：装了包时那条
  ignore 被判"冗余"，没装时又报模块解析不了 —— 两类环境各留一条 warning，
  诊断总数会随环境 ±1。改成 `importlib.import_module()` + `Any` 声明后，
  pyright 在两种依赖口径下都是 **0 error 0 warning**，门禁数字第一次完全不随环境漂。
- `src/interactions/submit.py` 轮询循环里的 `except Exception: pass`（P2-2 的同类
  站点、报告未点名）改为**一次性留痕**：控制流刻意不动，仍然轮询到超时、仍然返回
  unknown，只是那条 TypeError 级别的 bug 不再被彻底吞掉。

### 文档口径（报告三条"口径不一"其实是同一个根因）

README 的 72%、`ci.yml` 的 71%、README 的 45 条 gui 诊断 vs 报告的 44 条、
"0 error 0 warning" vs 实测多 1 条 —— 在干净 venv（只装 `requirements*.txt`）
与本机（额外装了可选 `opencv-python` / `undetected-chromedriver`）两侧重跑后确认：
**四个数字都是对的，差在可选依赖装没装 + `--cov-report=term` 的四舍五入**。
例如 gui 的第 15 条 warning 是 `qr_utils.py` 上那条 `# type: ignore`，
只有装了 cv2 时它才算"冗余"。修法分两步：类型门禁侧把环境相关性**消掉**（见上一节），
剩下确实相关的测试数与覆盖率则在 README 里列成"开发机 / CI"两列，
`ci.yml`、`pyrightconfig.json`（其注释里的"33 条"是陈值）同步。

### 仍然留着

- `verification.py` 非 Windows 降级桩不可达（Nit，README 已记录）
- 覆盖率地板仍留在 70：本轮实测 72.1%（CI）/ 72.4%（本机），抬到 71 只留 1pp 余量，
  而 CI 矩阵里 Python 3.10 那条 leg 在本机没法测 —— 不拿没量过的环境赌门禁

## [2.7.0] - 2026-09-20

补齐 README「已知缺口（诚实记录）」表里列出的五条覆盖率缺口，并修掉补测过程中
暴露的 6 个真实缺陷。这一轮的顺序是刻意的：**先给模块装上测试，再改它** ——
每条修复都配一条断言正确行为的用例，而不是改完再补一张网。

### 新增（测试）

离线套件从 **285 → 493 项**（全量 498 = 离线 493 + E2E 5）：

| 新文件 | 覆盖对象 | 项数 | 覆盖率 |
|---|---|---|---|
| `tests/test_logging_setup.py` | `src/logging_setup.py` | 12 | 0% → **100%** |
| `tests/test_driver_factory_offline.py` | `src/browser/driver_factory.py` | 54 | 9% → **100%** |
| `tests/test_pipeline_core.py` | `src/pipeline.py` | 32 | 26% → **100%** |
| `tests/test_verification_flow.py` | `src/verification.py` | 32 | 34% → **97%** |
| `tests/test_gui_panels.py` | `gui/` 六个面板/组件文件 | 78 | 15% → **58%** |

- **"人工介入路径难以自动化"这句注释是不成立的**：`verification` 与 `driver_factory`
  全靠替身对象驱动，两个文件合计 86 项、不到 3 秒跑完。前者用假 `driver` 的
  `execute_script` 脚本化三信号，后者整体替换 `df.webdriver` 并遮蔽
  `sys.modules["undetected_chromedriver"]`。
- **离线套件不可能开出真实浏览器**：`test_driver_factory_offline.py` 的 autouse 夹具
  先把 `webdriver.Edge/Chrome` 换成会抛 `AssertionError` 的兜底替身，忘了打桩的用例
  只会拿到断言失败。实测把 `subprocess.Popen.__init__` 插桩后整轮**零次**进程创建。
  同理 `force_focus` 走的是假 `ctypes`，跑测试期间不会有任何 Win32 弹窗。
- **Tkinter 基座只建一个根窗口**：`test_gui_panels.py` 用 module 作用域 fixture 建一个
  `withdraw()` 的 `tk.Tk()`，finalizer 里 `after_cancel` 掉所有排队任务再销毁；
  根窗口建不出来（无显示的 runner）整模块 skip。刻意**不构造 `SurveyGUI`** ——
  它的 `__init__` 会打开真实 `data/history.db`、启动动画 `after` 循环并自动载入
  `configs/default_weight_config.json`。
- **`_CONFIGURED` 与 `wjx` logger 都是进程级状态**：`test_logging_setup.py` 的 autouse
  夹具逐用例快照/还原全局标志、handlers、level、propagate，并关掉自己造的
  FileHandler（Windows 上句柄不释放会让 `tmp_path` 删不掉）。
- **CI 的 3.10 那条腿当场抓到一个测试基座缺陷**：`test_gui_panels.py` 的 finalizer 用了
  `Tk.after_info()`，而那是 **Python 3.11 才进 tkinter** 的 API —— 3.10 上取它会
  `AttributeError`，让 fixture 拆台时炸掉（3.13 全绿、3.10 独红）。改为存在性判断后再取消：
  本模块从不跑 mainloop，3.10 枚举不出待兑现任务也不影响销毁。
  > 这恰好是 README「Python 3.10 是真实下限」那句话**唯一被自动验证**的地方 —— 只有一个
  > 3.13 的 job 时，`slots=True` 之外的高低版本差异全是纸面承诺。
- 钉住的都是**已修复但零防线**的行为：v2.6 的 `_discard` 进程回收窗口、
  v2.5 的"成功之后清理不得上抛"（重复提交）、v2.6 的 `_csv_safe` CSV 公式注入前缀、
  v2.6 的 `get_db()` 单实例缓存。

### 修复（补测过程中暴露的 6 个真实缺陷 + 1 处回收窗口对齐）

1. **验证码探测不再把"探测本身抛异常"当成"验证码已消失"**（`src/verification.py`）：
   `is_smart_verification_showing` 的 `except Exception: pass` 让一次
   `JavascriptException` 返回 False，而等待循环把 False 读成"用户已经做完验证了" ——
   打印"验证已通过"、**释放 `ManualHoldLock`**、继续撞进一个仍被滑块挡住的页面，
   人工拉到一半就被判过。现在探测拆出三态的 `_probe_verification_state`
   （True / False / None = 未知），`is_smart_verification_showing` 保持原 bool 契约不变，
   等待循环只在**确认 False** 时放行，未知则继续等（上限仍是 `timeout_seconds`，
   不会变成死等）。
   > `src/pipeline_stages/verification_stage.py` 的入口判断刻意**没有**跟着改：
   > 那里误报成"有验证码"会让整批原地空等，代价比"这一轮失败重试一次"更高。
2. **渐变线的 tag 真正落到 canvas 上**（`gui/theme.py`）：两个渐变 helper 此前算出
   `kwargs["tags"] = tag` 却在 `create_line(...)` 时不带它，于是
   `paint_card_border` 的 `canvas.delete("card_border")` 只删得掉两条描边矩形，
   四角渐变的每段线段全部留下 —— 用户每拖动一次窗口就累积一批删不掉的 canvas item
   （`make_stat_badge` 的 `delete("glow")` 同理）。现在每段都带 tag，重绘严格幂等。
3. **`Sec-CH-UA` 的大版本改为跟着本次真实 UA 走**（`src/browser/driver_factory.py`）：
   `_cdp_extra_headers` 此前硬编码 `v="131"`，而同一实例的
   `Network.setUserAgentOverride` 按 UA 推导版本 —— 两个 UA 池各 4 条里有 2 条是
   129/130，即**一半**的实例会出现"请求头说 131、`userAgentMetadata` 说 129"的
   自相矛盾，正是本模块要防的 client-hints 特征。版本解析收进 `_ua_major_version`
   供两层注入共用；不传 `ua` 时仍是 131，默认指纹不变。
4. **UC 回退的异常回收面与另两条路径对齐**：`create_chrome_driver` 的 UC 分支此前捕
   `Exception`，而 `KeyboardInterrupt` 不是它的子类 —— Ctrl+C 落在 `uc.Chrome()` 之后时，
   v2.6 那句 `_discard` 根本不会执行，浏览器照样孤儿化。现在捕 `BaseException`
   → 先回收 → `raise_non_recoverable` 原样上抛，且**不会**再继续去开一个原生 Chrome。
5. **`create_chrome_driver` 的原生回退路径补上回收窗口**：v2.6 给 Edge 和 UC 两条路径
   补了"驱动已建好、初始化步骤随后失败"的 `_discard`，Step 3 的原生 Chrome 分支漏了 ——
   `webdriver.Chrome()` 成功后 `set_page_load_timeout` 或 `_apply_stealth_cdp` 抛异常
   仍会白漏一个进程。由 `test_chrome_native_leak_guard_*` 三条钉住。
6. **`setup_logging` 不再重复挂同一个文件**（`src/logging_setup.py`）：追加前按
   `baseFilename` 判重，已存在则只调级别 —— 此前两次 `setup_logging(p)` 会在同一文件上
   挂两个 handler，每条日志写两遍，事后复盘时行号与计数全部失真。同时 `level` 改为
   **每次调用都生效**：此前第二次传 `level=DEBUG` 得到的是 DEBUG 的 handler 配 INFO 的
   logger，落盘出来是一个空文件。全项目原本只有 `src/cli.py` 一个调用点，属潜在缺陷。
7. **权重面板在输入阶段就拒绝非法值**（`gui/weight_panel.py`）：此前只校验格式与个数，
   `-1,2,2` 静默通过，到运行期才被 `utils.weights_are_usable` 按"正权重之和 > 0"当合法值
   用（等于把 -1 当极低权重，与直觉相反），用户全程看不到提示。现在负数与 NaN/Inf
   在面板层就 WARN 并弃用；`0` 仍是合法权重（"基本不选"）。量表分支只丢权重、保留
   `scale` / `scale_min` 结构信息，矩阵行权重则整组弃用。
   > 这条其实是补一处**入口不一致**：`config_io.validate_weight_config` 一直会拒掉
   > 负数与 NaN/Inf（CLI `--config` 加载即校验、不通过就退出码 2），只有 GUI 表格是漏的
   > —— 同一个 `-1` 写在 JSON 里会被拒绝，敲进权重格却会被接受。

### 已知缺口（本轮暴露、刻意留到下轮）

- **逐题停顿打不断**（`src/pipeline.py::_do_one_submission_core`）：v2.6 给轮间
  `human_pause` 接了 `abort_check`，但每题之间的"思考时间"没有，核心流程也不接受停止
  谓词 —— 点了停止仍要等完整份问卷（约 4.5s/题）。修它要把谓词穿过
  pipeline / question_stage / cli / gui 四处接缝，与 v2.6 收敛 `_run_loop` 是同一类工程，
  单独一轮做。

### 变更（门禁）

- **覆盖率地板 43% → 70%**（实测 72%：`src/` 84%、`gui/` 58%）。
- README「已知缺口」表重写为"已补齐"与"仍无防线"两段，并加上一条限定：
  **覆盖率不等于验证过** —— 100% 是拿替身跑出来的，真实浏览器能否启动、
  注入的 JS 在真 DOM 里是否成立，仍然只有非阻塞的 E2E job 说了算。

### 勘误

- 2.6.0 条目「已知缺口」所称"`gui/` 的 33 条 pyright 诊断（Tkinter 子类赋值风格为主）"
  不准：按 `npx pyright --project pyrightconfig.json gui` 实测是 **45 条
  （30 error + 15 warning）**，其中最大一组是 11 条已无对象的 `# type: ignore`，
  其余才是往 `Frame` 子类上赋值、向 `dict[str, str]` 塞 list 一类 Tkinter 写法。
  本版本改了 `gui/theme.py` 与 `gui/weight_panel.py` 之后重测，仍是 **45 条**
  （30 error + 15 warning）—— 这两处改动没有引入新的诊断。

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
