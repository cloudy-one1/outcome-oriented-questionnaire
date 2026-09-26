# 覆盖率口径与已知缺口

**这份文档里的数字是生成物，不是手抄的。** 唯一来源是 `coverage.json` →
`python scripts/coverage_doc.py --write`；`--check` 已进 CI 当门禁（`ci.yml` 的
「覆盖率口径一致性」那一步）。手改块内数字、缺口模块改名、低覆盖模块没登记理由，
都在这里红。地板值 `--cov-fail-under` 从 `.github/workflows/ci.yml` 读出来一起落盘，
它是本仓库唯一还手写的门禁数字，且只许上调。

<!-- BEGIN AUTO-GENERATED 覆盖率口径 · scripts/coverage_doc.py · 不要手改 -->
**口径**：`pytest tests/ -m "not integration" --cov=src --cov=webui`，依赖只装 `requirements*.txt`（即 CI 两条 leg 的环境）。地板 `--cov-fail-under=70`（从 `.github/workflows/ci.yml` 读出来，不是手抄的）。

| 范围 | 离线覆盖率 |
|---|---|
| 全部 | **93.2%** |
| `src/` | 91.7%（4864 条语句剩 405 行） |
| `webui/` | 99.1%（1294 条语句剩 12 行） |

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
| `src/cli.py` | 74.3% | **81.9%**（剩 95 行，剩余是 run_batch 内的浏览器接线与降级分支） | `tests/test_cli_exit_and_reports.py`、`tests/test_cli_main.py`、`tests/test_cli_batch.py` |

#### 仍然没有防线的地方

| 模块 | 离线覆盖率 | 为什么还留着 |
|---|---|---|
| `src/pipeline_stages/question_stage.py` | 63.3% | 逐题 DOM 交互主干：等待、「哪道题调哪个填充器」的分发、带框选项只勾不填的降级都已有离线测试（`tests/test_question_stage_dispatch.py`），真实点击仍靠 E2E |
| `src/qr_utils.py` | 51.5% | 缺口全在「真的解一张图」那 16 行：要 OpenCV，而 CI 口径不装可选依赖，`tests/test_qr_utils.py` 里两条真读图用例因此 skip。缺依赖时的降级顺序、失败路径提示几次与是哪一类、解不出必给 None 都已有契约。该模块 v3.4 从 `gui/` 移到 `src/`，v4.0 桌面版退役后它是 webui 独占的可选依赖路径 |

> 本块由 `python scripts/coverage_doc.py --write` 从 `coverage.json` 生成，`--check` 已进 CI 当门禁
> —— 手改这里的数字会在下次推送时红掉。`--write` 会拒绝装了 `opencv-python` /
> `undetected-chromedriver` 的解释器，因为本块的口径就是 CI 那个不装可选依赖的环境。
> 模块清单与缺口理由维护在 `scripts/coverage_gaps.json`（reason 留空同样是红）。
<!-- END AUTO-GENERATED 覆盖率口径 -->

## 同一套门禁在两种环境下的口径

类型门禁不随环境变：`src/` + `webui/` + 入口在两种环境下都是 **0 error 0 warning**。
三处可选依赖（`opencv-python`、`undetected_chromedriver`、`openpyxl`）的动态导入改用
`importlib` + `Any` 声明 —— `try: import x` 配 `# type: ignore` 那个写法必然两类环境
各留一条 warning：装了包时 ignore 被判"冗余"（本仓库把 `reportUnnecessaryTypeIgnoreComment`
也设成了 warning），没装时又报模块解析不了。

仍随环境变的只剩测试数与覆盖率（`requirements.txt` 只声明必选依赖，CI 装不到那几个可选项）：

| 门禁 | 装齐可选依赖（开发机） | 未装（CI / 干净 venv） |
|---|---|---|
| 离线套件 | 全部 passed | 若干 skipped：二维码解析 2 项、`.xlsx` 真读 1 项，外加全新检出时 `coverage.json` 还不存在 —— 文档口径比对那一项也 skip，它是同一次运行**末尾**才产出的 |
| 覆盖率 | 总口径比 CI 高约 0.2pp（`src/` 高、`webui/` 不变）—— 三个可选项各自改变一侧的分支走向 | 上面生成块里那份，是门禁认的唯一口径 |

数字为什么写到一位小数：`--cov-report=term` 会把相邻两个真值都四舍五入成同一个整数，
两处曾因此互相指对方口径不对。批次循环里有 `random` 分支，同一环境两次跑出 ±0.1pp 属
正常抖动，所以门禁按容差比数字、按精确匹配比缺口清单的**增删**。

`--write` 会拒绝装了 `opencv-python` / `undetected-chromedriver` 的解释器，除非显式加
`--force-env` —— 上面那份口径指的是不装可选依赖的 CI 环境。要在本地复现，就得用一个
3.13 的干净 venv，只装 `requirements*.txt`。

## 覆盖率不等于验证过

上面那些百分比是拿替身对象跑出来的 —— 它证明「指纹参数拼装、异常回收、锁必然释放」
这些**逻辑**成立；但真实浏览器能否启动、注入的 JS 在真 DOM 里是否成立，仍然只有那个
E2E job 说了算（v3.1 起它是**阻塞**的，而"驱动起不来 → 全 skip → 看着也是绿"这条路另有
`scripts/e2e_gate.py` 数 junit 堵住）。

模块清单与缺口理由维护在 [`scripts/coverage_gaps.json`](../scripts/coverage_gaps.json)。
能力边界（哪一类题型不支持逐行权重、哪一种投放形态未适配）不在这张表里，
在 [README 的「不在本工具范围内」](../README.md#不在本工具范围内评估过明确不做) 与
[config.md 的已知边界](config.md#已知边界量表式矩阵不支持逐行权重)。
