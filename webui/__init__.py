"""本地 HTTP + 浏览器的问卷控制界面。

设计稿：``docs/design/DESIGN_webui.md``。与 ``gui/``（Tkinter 宿主）是**并列的两个宿主**，
共用 ``src/`` 那套引擎；本轮 ``gui/`` 原样保留当参照实现，退役推到赛后。

包内分层：``session``（状态）→ ``service``（命令）→ ``api``（路由与校验）→
``server``（``http.server`` + SSE，只监听 127.0.0.1）→ ``static``（观感）。
"""
