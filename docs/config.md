# 权重配置

每道题都可以给一份选项权重；不给就是等权重随机。配置有三条入口：
Web 控制台的权重表、JSON 文件（`--config` / 界面「💾 导出配置」）、直接改 `src/config.py`。
三条最后都汇到同一个 `WEIGHT_CONFIG` 结构与同一套校验。

## 方式一：编辑 `src/config.py`

适合脚本固定场景，直接修改 `WEIGHT_CONFIG` 字典。出厂它是**空的**（v2.8 起）——
一份可直接抄、且能作为 `--config` 合法输入喂给 CLI 的 22 题样例见
[`examples/weight_config.example.json`](../examples/weight_config.example.json)：

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

## 方式二：JSON 配置文件（推荐）

通过界面里的「💾 导出配置 / ⭐ 另存默认」或 CLI `--save-config` 生成，支持导入导出与版本管理：

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

## 题干锚点（schema 3.0）

题号只是"保存时这道题在第几格"的遗迹：问卷中间插一道题，整份预设就会**向后错位一格**，
而只要选项数恰好还对得上，校验一律放行 —— 跑完 17 份才发现分布全落在别人的题上。
所以「探测题目 → 另存」会给每条配置附上锚点：

```json
"3": { "type": "single", "weights": [0.1, 0.3, 0.6],
       "anchor": { "title": "您所在的城市？", "signature": "single:8" } }
```

- **带 anchor 的条目只按锚点生效**：题干（剥掉 `1.` / `第2题` 这类序号后比对）+ 结构签名
  都命中才算认领；认不到就这一题走等权随机，并打印一行
  `该权重本次**不生效**，不退回答题号`（每进程每条只提示一次）。
- **不带 anchor 的条目照旧按题号**，schema 2.0 的老文件完全可用（只是没有错位保护）。
- 手写 JSON 也可以只给 `"anchor": {"title": "..."}`，省略 `signature` 就只比题干。

## 各题型的关键字段

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

填空题的 `field` 取值走的是内置生成器：姓名 / 手机 / 邮箱 / 年龄 / 地址 / 所在地区（省市）/
公司。字段类型**先问平台的 `verify` 属性**，认不到才用题干正则猜。

## 权重表第 4 列的编辑格式（Web 控制台）

- 单选 / 多选 / 下拉：`0.2, 0.5, 0.3`
- 量表：`0,0,0.1,0.4,0.5`，或直接写 `5`（强制打 5 分）
- 填空：`张三,李四,王五`，或留空（按字段类型自动生成）
- 矩阵：`1:0,0,0.1,0.4,0.5 | 2:0.1,0.2,0.3,0.3,0.1 | 3:1,0,0,0,0`

配置加载时会自动校验：权重非负且总和大于 0、数组长度与选项数匹配、NaN / Inf 拒绝、多选「选中个数」分布一致等。校验不通过时 CLI 拒绝运行并以退出码 2 结束。

## 信度相关的两个可选键

只有 `--alpha-target` / `--report-alpha` 用得到（见 [cli.md](cli.md#统计与数据类开关)）：

- `dimension` — 声明这道题属于哪个构念。没声明的题不参与建计划，且工具会明说为什么
- `reverse` — 反向计分题，算 α 之前先翻过来

## 已知边界：量表式矩阵不支持逐行权重

> **`matrix_scale`（量表式矩阵）不支持逐行权重。** 探测会输出这个类型，它的"行"是提交槽名
> `fid` 而不是行号，而作答侧（`src/answering_v2` 的 `matrix_scale` 分支）读的是**档位
> `weights`**、不看 `row_weights`。所以权重表里这类题写 `0.2,0.3,0.3,0.1,0.1`（各档位的
> 权重）是**有效的**、真的影响分布；写 `1:... | 2:...`（逐行）会被拒。这是能力边界，不是
> 静默丢数据 —— 把它并进矩阵分支只会得到"界面填得进、分布一动不动"那种更难发现的失效。
> 要真支持逐行权重，得连作答分支一起改并用真卷验证落库分布。
