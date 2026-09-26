<!--
标题形状：fix(vX.Y): 一句话说清改了什么 —— 详见 CONTRIBUTING「分支与提交」。
-->

## 这个 PR 改了什么 / 为什么

<!-- 优先写"为什么"和取舍；diff 已经说明了做了什么 -->

## 影响面

- [ ] 作答 / 探测 / 提交链路（行为改动）
- [ ] 测试或门禁
- [ ] 文档口径
- [ ] 重构，不改行为

## 门禁自查

本地跑绿再交。哪一条跑不动，就在后面写清为什么，不要留空。

- [ ] `python -m ruff check .`
- [ ] `npx pyright` —— `src/` + `webui/` + 入口 0 error 0 warning，且没有新增
      `# type: ignore` / `# pyright:`（现存条目登记在 `tests/test_ci_guards.py` 的 `BASELINE`，只准变小）
- [ ] 离线套件 + 覆盖率地板：`pytest tests/ -m "not integration" --cov=src --cov=webui`
      过 `ci.yml` 里那个 `--cov-fail-under`（地板值只留在那一处，不手抄到别处；
      `--cov` 的名单以 `scripts/coverage_doc.py` 的 `PACKAGES` 为准）
- [ ] 改动碰到生成的 JS、交互链路或宿主页面 →
      `pytest tests/ -m integration --junitxml=e2e-junit.xml && python scripts/e2e_gate.py e2e-junit.xml
      --require-file tests/test_e2e_integration.py --require-file tests/test_webui_e2e.py`
      （离线 mock 抓不住"语法合法、语义非法"的错选择器；两个 `--require-file` 各钉一侧，
      免得只剩全局计数时新宿主整片被吞成 skip 还是绿的）
- [ ] 改动碰到 `coverage.json` 或 `docs/coverage.md` 的覆盖率块 → `python scripts/coverage_doc.py --check`
- [ ] `CHANGELOG.md` 的 `[未发布]` 块记了一行
- [ ] 定版 PR：版本号三处一起改（`pyproject.toml`、`src/__init__.py` 的 `__version__`、README 顶部徽章）

## 诚实性检查

这个仓库把"说到做到"当功能看，下面几条比测试数更容易被审出来：

- [ ] 新能力在 README 特性里**同时写清边界**：哪一类明确不做、哪一种形态未适配、默认开还是默认关
- [ ] 没有把"探测到但不作答"（例如日期题）写成支持
- [ ] 没有把"只提示、不拦停"的判据写成会拦下提交
- [ ] 改动了拦停判据（提交前完整度自检）→ 在这里说明为什么这条判据是零歧义事实

## 隐私

- [ ] 没有把 `data/`、`configs/`、`*.csv`、`*.db` 里的内容带进提交；新测试夹具已脱敏
