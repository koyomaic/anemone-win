from __future__ import annotations

import asyncio
import itertools

TERMINAL = {"done", "conflict", "failed", "cancelled"}


class EventBus:
    """每任务事件总线：保留有限历史 + 向订阅者推送（SSE 数据源）。"""

    def __init__(self, history_max: int = 500):
        self.history_max = history_max
        self._history: dict[int, list[dict]] = {}
        self._subs: dict[int, list[asyncio.Queue]] = {}
        self._ids = itertools.count(1)

    def publish(self, task_id: int, **ev) -> None:
        ev = {"id": next(self._ids), "task_id": task_id, **ev}
        h = self._history.setdefault(task_id, [])
        h.append(ev)
        if len(h) > self.history_max:
            del h[: len(h) - self.history_max]
        for q in self._subs.get(task_id, []):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def history(self, task_id: int) -> list[dict]:
        return list(self._history.get(task_id, []))

    def subscribe(self, task_id: int) -> tuple[list[dict], asyncio.Queue]:
        history = list(self._history.get(task_id, []))
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.setdefault(task_id, []).append(q)
        return history, q

    def unsubscribe(self, task_id: int, q: asyncio.Queue) -> None:
        lst = self._subs.get(task_id)
        if lst and q in lst:
            lst.remove(q)
