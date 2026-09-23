"""排序题 DOM 交互实现（v3.1：两种控件形态各走各的）。

平台在这道题上有两套形态，探测用 ``sort_mode`` 分开（见 ``src/detection.py`` 5b 节）：

  * ``"value"`` —— 老页面：``ul.lisort`` + 同域**一个** ``input[name=qN]`` 承载逗号串。
    两条腿一起走：重排 DOM + 写那个隐藏域。
  * ``"click"`` —— 2026-09-22 真卷实测：``ul.ui-controlgroup.ui-listview``，
    每个 ``li`` 一个 ``span.sortnum`` + ``input[type=hidden][name=qN][value=序号]``。
    **隐藏域的 value 恒为 1,2,3 不变，排名只活在 li 的 DOM 顺序里** ——
    提交时同名 input 按 DOM 顺序一起交上去（实测按 3→1→2 点完之后
    ``FormData.get('q12')`` 就是 "3,1,2"）。
    所以这种形态**只能按目标顺序点击**：照老办法往第一个 input 里写 "3,1,2"，
    等于把选项 1 的身份换成了一串数字，交上去是脏数据。

两种形态都遵循同一条底线：**点不成、验不到就返回 False**，让上层把这题记成失败，
而不是交一份"看起来答了"的排序题。点击式的每一项点完都要等平台把名次写进
``.sortnum``（它内部有 400ms 动画），点完不看等于没点。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ._common import js_execute_retry
from ._scripts import (
    click_sort_item_script,
    fill_sort_script,
    sort_state_script,
)

#: 单项点完之后等平台写名次的等待粒度（与 page_nav 的翻页轮询同一套路）
_POLL = 0.15


def _state(driver: Any, q: int) -> list[dict] | None:
    """``[{"value":..., "rank":...}, ...]``，按 DOM 顺序；容器没了返回 ``None``。"""
    raw = driver.execute_script(sort_state_script(q))
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


@js_execute_retry()
def js_fill_sort(
    driver: Any,
    q: int,
    order: list[Any],
    *,
    mode: str = "value",
    timeout: float = 6.0,
    _sleep: Any = time.sleep,
) -> bool:
    """把 Q``q`` 的排序列表按 ``order``（item 值序列）定稿。

    :param mode: ``"click"`` 走点击式（新形态），其它值走逗号串式（老形态）。
    :param timeout: 全部点击完成的总等待预算（不是单项预算）—— 超时即返回 False。
    """
    if mode != "click":
        return bool(driver.execute_script(fill_sort_script(q, order)))

    values = [str(v) for v in order]
    deadline = time.monotonic() + timeout

    # 1) 清残留名次：点过的项目再点一次即取消。不清就糟 —— 已勾项的第二次点击是
    #    "取消"而不是"排到第二位"，续填/重试时会把顺序点反。
    for _ in range(len(values) + 2):
        state = _state(driver, q)
        if state is None:
            return False                      # 容器都找不到了
        ranked = [it for it in state if it.get("rank")]
        if not ranked:
            break
        if not driver.execute_script(
            click_sort_item_script(q, ranked[-1].get("value", ""))
        ):
            return False
        _sleep(_POLL)
    else:
        return False                           # 取消不动：状态机不按预期走，别硬交

    # 2) 按目标顺序逐项点击，并等平台把名次写进 .sortnum
    for rank, value in enumerate(values, start=1):
        if not driver.execute_script(click_sort_item_script(q, value)):
            return False
        while time.monotonic() < deadline:
            _sleep(_POLL)
            state = _state(driver, q)
            if state is None:
                return False
            if len(state) >= rank and str(state[rank - 1].get("value")) == value \
                    and str(state[rank - 1].get("rank")) == str(rank):
                break
        else:
            return False                       # 这一项没排上，整题判失败
    return True
