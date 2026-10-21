from __future__ import annotations

import asyncio
import time

from app.scheduler import Scheduler


def test_scheduler_concurrent_slots():
    """两个任务必须并行开始（修复前为单 worker 串行执行）。"""

    async def main():
        marks: dict[str, float] = {}
        t0 = time.monotonic()

        async def run_fn(tid: int) -> None:
            marks[f"s{tid}"] = time.monotonic() - t0
            if tid == 1:
                await asyncio.sleep(0.6)
            marks[f"e{tid}"] = time.monotonic() - t0

        s = Scheduler(2, run_fn)
        await s.start()
        await s.submit(1)
        await s.submit(2)
        await asyncio.sleep(1.5)
        await s.stop()
        assert marks["s2"] < marks["e1"], f"未并行: {marks}"

    asyncio.run(main())


def test_scheduler_respects_max_concurrent():
    async def main():
        active = 0
        peak = 0

        async def run_fn(tid: int) -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.3)
            active -= 1

        s = Scheduler(2, run_fn)
        await s.start()
        for i in range(1, 4):
            await s.submit(i)
        await asyncio.sleep(2.0)
        await s.stop()
        assert peak <= 2, f"超过并发上限: peak={peak}"

    asyncio.run(main())


def test_scheduler_urgent_first():
    async def main():
        order: list[int] = []

        async def run_fn(tid: int) -> None:
            order.append(tid)

        s = Scheduler(1, run_fn)
        await s.start()
        await s.submit(1)
        await asyncio.sleep(0.2)  # 让 1 先跑（占满唯一槽位）
        await s.submit(3, "normal")
        await s.submit(2, "urgent")
        await asyncio.sleep(1.0)
        await s.stop()
        assert order.index(2) < order.index(3), f"urgent 未插队: {order}"

    asyncio.run(main())


def test_scheduler_queue_info():
    async def main():
        started = asyncio.Event()

        async def run_fn(tid: int) -> None:
            started.set()
            await asyncio.sleep(0.3)

        s = Scheduler(1, run_fn)
        await s.start()
        await s.submit(1)
        await asyncio.wait_for(started.wait(), timeout=1)
        await s.submit(2, "normal")
        await s.submit(3, "normal", long=True)
        info = s.queue_info(2)
        assert info["pending"] == 2
        assert info["active"] == 1
        assert info["queued"] is True
        assert info["position"] == 1
        long_info = s.queue_info(3)
        assert long_info["long"] is True
        await s.stop()

    asyncio.run(main())
