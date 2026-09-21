"""二维码解析工具。

依赖 OpenCV（pip install opencv-python），未安装时返回 None 并给出提示。
"""

from __future__ import annotations

import importlib
from typing import Any

import numpy as np
from tkinter import messagebox

# OpenCV — 用于二维码解析（可选依赖）。
# 用 importlib 而不是 "try: import cv2 / except ImportError + # type: ignore"：
# 后者在装了 cv2 的环境里被 pyright 判成冗余 ignore，在没装的环境里又报模块解析不了，
# 两侧各留一条 warning —— 数量会随环境 ±1，gui 纳入门禁时必须先把它消掉（v2.8）。
cv2: Any = None
try:
    cv2 = importlib.import_module("cv2")
except ImportError:
    pass

_HAS_CV2 = cv2 is not None


def has_cv2() -> bool:
    """是否已安装 OpenCV。"""
    return _HAS_CV2


def decode_qr_from_image(filepath: str) -> str | None:
    """从图片文件中解析二维码，返回解码后的 URL 字符串。

    需要 pip install opencv-python；未安装时弹出提示框并返回 None。
    """
    if not _HAS_CV2:
        messagebox.showwarning(
            "缺少依赖",
            "二维码解析需要 OpenCV 库。\n\n请在终端中运行：\n  pip install opencv-python",
        )
        return None

    # 用二进制读取再解码，避免 OpenCV imread 在 Windows 上不支持中文路径
    try:
        with open(filepath, "rb") as f:
            raw = bytearray(f.read())
        img = cv2.imdecode(np.asarray(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception as e:
        messagebox.showerror("错误", f"无法读取图片：\n{filepath}\n\n{type(e).__name__}: {e}")
        return None

    if img is None:
        messagebox.showerror(
            "错误",
            f"无法解析图片：\n{filepath}\n\n请确认文件是有效的图片格式（PNG/JPG/BMP）。",
        )
        return None

    detector = cv2.QRCodeDetector()
    data, _, _ = detector.detectAndDecode(img)

    if not data:
        messagebox.showinfo("未识别", "未在图片中检测到二维码，请确认图片清晰且包含完整二维码。")
        return None

    return data
