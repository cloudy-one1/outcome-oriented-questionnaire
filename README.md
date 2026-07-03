# 问卷星自动填写工具

> 基于 Selenium Stealth 浏览器的问卷星批量填写与提交工具，支持加权随机策略、人类行为模拟、智能验证码检测 —— 提供 CLI 与 GUI 两种使用方式。

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/Version-1.0.0-brightgreen.svg)](src/__init__.py)

---

## 特性

- **🛡️ 反检测**：CDP 注入 Stealth JS 隐藏 `navigator.webdriver`，随机化浏览器指纹（UA / 屏幕分辨率 / 硬件配置）
- **⚖️ 权重随机**：每道题可配置各选项的选中概率权重，单选加权采样，多选无放回加权抽样
- **👤 人类行为模拟**：正态分布停顿 + 完整事件链（mouseover → mousedown → change → click），3% 概率触发长停顿
- **🤖 智能验证码检测**：三信号并行检测（DOM / URL / iframe+Shadow DOM），弹窗提醒人工介入
- **🖥️ 双入口**：CLI 适合脚本批量运行，GUI 适合可视化配置和实时监控
- **🔄 容错机制**：指数退避重试、浏览器自动重启释放内存、线程安全的人工介入锁

---

## 快速开始

### 环境要求

- Python 3.9+
- Microsoft Edge 或 Google Chrome 浏览器

### 安装

```bash
git clone <your-repo-url>
cd automation

# 安装核心依赖
pip install -r requirements.txt

# 可选：更强的 Chrome 反检测模式
pip install undetected-chromedriver

# 可选：GUI 二维码 URL 解析
pip install opencv-python
```

### CLI 使用

```bash
# 默认参数运行（使用 config.py 中的默认 URL）
python run_cli.py

# 自定义问卷 URL，提交 10 份
python run_cli.py -u "https://www.wjx.cn/vm/xxxxx.aspx" -n 10

# 使用 Chrome 浏览器
python run_cli.py -b chrome

# Chrome + undetected-chromedriver 模式（反检测更强）
python run_cli.py -b chrome --uc
```

### GUI 使用

```bash
python run_gui.py
```

GUI 界面操作流程：

1. 输入问卷星 URL（或导入二维码图片自动解析）
2. 设置提交份数和浏览器类型
3. 点击 **探测题目** 自动识别问卷结构
4. 在权重表格中为每道题设置选项权重
5. 点击 **开始** 运行，实时查看日志和统计

---

## 项目结构

```
automation/
├── run_cli.py                  # CLI 启动入口
├── run_gui.py                  # GUI 启动入口
├── requirements.txt            # Python 依赖
├── src/
│   ├── config.py               # 全局配置：权重定义、运行时常量、反检测参数
│   ├── pipeline.py             # 核心编排：单次问卷填写 + 提交流程
│   ├── detection.py            # 题目结构自动探测（JS 注入扫描 DOM）
│   ├── answering.py            # 答案生成策略（加权/等权重随机）
│   ├── interaction.py          # DOM 交互层（JS 注入点击、提交按钮查找）
│   ├── verification.py         # 智能验证码检测与人工等待
│   ├── utils.py                # 工具库：正态分布、指数退避、UA 池、人工介入锁
│   ├── cli.py                  # 命令行入口：批量提交循环
│   └── browser/
│       ├── driver_factory.py           # Edge/Chrome 驱动 + CDP Stealth 配置
│       └── driver_factory_stealth.py   # Stealth JS 反检测脚本构建器
├── gui/
│   ├── app.py                  # Tkinter GUI 主窗口
│   └── qr_utils.py             # 二维码 URL 解析
└── tests/
    ├── test_answering.py       # 答案生成逻辑测试
    ├── test_cli.py             # CLI 参数解析测试
    └── test_utils.py           # 工具函数测试
```

---

## 工作原理

```
用户配置 URL + 份数 + 权重
        ↓
启动 Stealth 浏览器（指纹随机化 + 反检测 JS）
        ↓
打开问卷页面 → JS 注入探测题目结构
        ↓
循环 N 次：
  ├── 权重随机生成答案
  ├── JS 注入模拟人类点击（完整事件链）
  ├── 正态分布随机停顿
  └── 遇验证码 → 弹窗等待人工介入
        ↓
模拟点击提交 → 统计成功/失败
```

---

## 配置权重

编辑 `src/config.py` 中的 `WEIGHT_CONFIG` 字典：

```python
WEIGHT_CONFIG = {
    1: {"type": "single", "weights": [0.1, 0.3, 0.5, 0.1]},  # 第1题：单选题，选项2和3概率高
    2: {"type": "multi",  "weights": [0.2, 0.2, 0.3, 0.3],     # 第2题：多选题
        "count_options": [0.1, 0.6, 0.3]},                     # 选2个概率60%
    # ...
}
```

- `type`: `"single"` 单选 / `"multi"` 多选
- `weights`: 各选项被选中的概率权重（自动归一化）
- `count_options`: 多选题选中个数的概率分布
- 未配置的题目自动降级为等权重随机

---

## 运行测试

```bash
python -m pytest tests/ -v
```

---

## 免责声明

本工具仅供学习和研究 Selenium 自动化技术使用。请遵守问卷星平台的使用条款和相关法律法规，不得用于任何违规或违法用途。使用者需自行承担所有责任。

---

## License

MIT License — 详见 [LICENSE](LICENSE) 文件。
