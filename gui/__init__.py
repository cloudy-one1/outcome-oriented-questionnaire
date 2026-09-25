"""GUI 子包。

基于 Tkinter 的图形界面封装，使用自定义 ttk 主题打造现代化界面。

模块：
    - app        : SurveyGUI 主窗口类（设置 + 权重表格 + 日志 + 运行控制）

（二维码解析曾在本子包，v3.4 移到 ``src/qr_utils.py`` —— Tk 与 webui 共用一个宿主，
它零 tkinter 依赖，不该随桌面版一起退役。）
"""
