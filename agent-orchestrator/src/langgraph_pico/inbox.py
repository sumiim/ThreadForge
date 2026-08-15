"""队列化输入（inbox）——1.5 运行中追加新请求的输入抽象。

DSH inbox 三态（followup / steer / inject）在 V1.2 简化为 ``wake`` + ``priority``：

- followup：``wake=True, priority=0`` —— 追加到下一轮，唤醒消费。
- steer：``wake=True, priority=1`` —— 追加到下一步，唤醒消费（优先）。
- inject：``wake=False`` —— 追加但不唤醒，等后续 followup/steer 才消费。

``InboxSource`` 是线程安全的：Worker 的 ``run_agent`` 在循环边界（intent_router
入口）调用 ``pop_wake`` 排空，而 WebSocket ``task.message`` 从另一线程 ``append``。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class InboxItem:
    message: str
    wake: bool = True  # followup/steer 唤醒；False = inject 不唤醒
    priority: int = 0  # 0 普通 followup；1 steer（下一步前优先）


class InboxSource:
    """线程安全 inbox 队列。

    ``pop_wake`` 只弹出下一条 ``wake=True`` 的项；``wake=False``（inject）留在队列
    里等后续 followup/steer。V1.2 只实现 FIFO，``priority`` 字段预留（steer 语义）。
    """

    def __init__(self, initial_message: str = ""):
        self._lock = threading.Lock()
        self._items: deque[InboxItem] = deque()
        initial = str(initial_message or "").strip()
        if initial:
            self._items.append(InboxItem(message=initial, wake=True))

    def append(self, message: str, *, wake: bool = True, priority: int = 0) -> None:
        text = str(message or "").strip()
        if not text:
            return
        with self._lock:
            self._items.append(InboxItem(message=text, wake=wake, priority=priority))

    def pop_wake(self) -> InboxItem | None:
        """弹出下一条唤醒项；无唤醒项返回 None（inject 项留在队列里）。"""
        with self._lock:
            for index, item in enumerate(self._items):
                if item.wake:
                    del self._items[index]
                    return item
        return None

    def has_wake(self) -> bool:
        with self._lock:
            return any(item.wake for item in self._items)

    def pending(self) -> int:
        with self._lock:
            return len(self._items)
