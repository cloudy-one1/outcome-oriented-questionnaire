"""``src/qr_utils.py`` 的失败路径契约 —— 不得阻塞，且要说清是哪一类失败。

二维码解析是 **Tk 与 webui 共用**的一条可选依赖路径（OpenCV 装不上时只降级、
不拦启动）。它靠 ``src.dialogs`` 的间接层出话，所以不建 Tk、不等真人点掉，也能
断言"失败时提示了几次、分别是哪一类" —— 这正是那层间接存在的理由。

从 ``tests/test_gui_panels.py`` 拆出来是因为 `qr_utils` 本身零 tkinter 依赖，
它不该随桌面版宿主一起退役（v3.4 把模块从 ``gui/`` 移到 ``src/``）。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import dialogs as dialogs_mod  # noqa: E402
from src import qr_utils  # noqa: E402


def test_has_cv2_returns_a_bool() -> None:
    assert isinstance(qr_utils.has_cv2(), bool)


# kind → 旧 messagebox 函数名：保留这层映射，下面既有断言（showerror/showwarning/
# showinfo）就不必跟着换口径。
_KIND_TO_MESSAGEBOX = {
    "info": "showinfo",
    "warning": "showwarning",
    "error": "showerror",
    "confirm": "askyesno",
}


def _record_dialogs(monkeypatch) -> list[tuple[str, str]]:
    """把 ``src.dialogs`` 的出口换成记录器。

    这正是弹窗间接层的意义所在：不建 Tk、不等真人点掉，也能断言"失败路径提示了
    几次、分别是哪一类"。
    """
    dialogs: list[tuple[str, str]] = []

    def handler(kind: str, _title: str, message: str) -> bool:
        dialogs.append((_KIND_TO_MESSAGEBOX[kind], message))
        return False

    monkeypatch.setattr(dialogs_mod, "_handler", handler)
    return dialogs


def test_decode_nonexistent_image_returns_none_with_one_dialog(
    monkeypatch
) -> None:
    dialogs = _record_dialogs(monkeypatch)
    assert qr_utils.decode_qr_from_image("Z:/definitely/not/here.png") is None
    assert len(dialogs) == 1, f"只该提示一次，实际 {dialogs}"
    expected = "showerror" if qr_utils.has_cv2() else "showwarning"
    assert dialogs[0][0] == expected


def test_decode_image_without_qr_returns_none(monkeypatch, tmp_path) -> None:
    if not qr_utils.has_cv2():
        pytest.skip("本机未安装 opencv，跳过真图片分支")
    import cv2
    import numpy as np

    ok, buf = cv2.imencode(".png", np.zeros((24, 24, 3), dtype=np.uint8))
    assert ok
    png = tmp_path / "blank.png"
    buf.tofile(str(png))

    dialogs = _record_dialogs(monkeypatch)
    assert qr_utils.decode_qr_from_image(str(png)) is None
    assert [d[0] for d in dialogs] == ["showinfo"], "无二维码 → 未识别提示"


def test_decode_non_image_bytes_returns_none(monkeypatch, tmp_path) -> None:
    if not qr_utils.has_cv2():
        pytest.skip("本机未安装 opencv，跳过坏字节分支")
    bogus = tmp_path / "not-an-image.png"
    bogus.write_bytes(b"\x00\x01\x02not a png at all")
    dialogs = _record_dialogs(monkeypatch)
    assert qr_utils.decode_qr_from_image(str(bogus)) is None
    assert dialogs and dialogs[0][0] == "showerror"
    assert "无法解析图片" in dialogs[0][1]
