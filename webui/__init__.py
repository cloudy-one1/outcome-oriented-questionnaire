"""本地 HTTP + 浏览器的问卷控制界面。

设计稿：``docs/design/DESIGN_webui.md``。它曾是 ``gui/``（Tkinter 宿主）的**并列宿主**，
两者共用 ``src/`` 那套引擎；桌面版已在 v4.0 退役，现在它是唯一的那个界面入口。

包内分层：``session``（状态）→ ``service``（命令）→ ``api``（路由与校验）→
``server``（``http.server`` + SSE，只监听 127.0.0.1）→ ``static``（观感）。
"""
