"""长任务异步化：lane 比例式上限、分段续跑段号编码、调度语义。

判据本身（T=10m 的精确率/召回）来自现役 PG 台账 n=108 的离线重放，属**数据校准**
而非单元可断言的性质，故这里只测机制正确性：cap 怎么算、lane 怎么挡、aging 怎么放行、
段号怎么在 resume_hint 里往返。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app.config import Settings                      # noqa: E402
from app.main import _segment_of, _with_segment       # noqa: E402
from app.scheduler import Scheduler                   # noqa: E402


def _cfg(**kw) -> Settings:
    base = dict(db_path="", workspace_root="", shared_dir="", task_root="",
                users_root="")
    base.update(kw)
    return Settings(**base)


# ── lane 上限：比例式，随并发缩放 ──────────────────────────────────────

@pytest.mark.parametrize("mc,reserve,cap", [
    (1, 0, 1),      # 只有 1 槽时无从保留，cap 下限 1（否则长任务被全禁）
    (2, 1, 1),
    (3, 1, 2),      # 现网：重放拐点（短 max 15m／长 max 77m）
    (4, 2, 2),
    (6, 2, 4),
    (8, 3, 5),      # 扩容目标：重放拐点（8 倍流量下短 P90 5m／长 P90 94m）
    (12, 4, 8),
])
def test_long_lane_cap_scales_with_concurrency(mc, reserve, cap):
    """固定 reserve=1 是错的：扩到 8 槽只占 12.5%、护栏形同虚设。比例式两个尺度同为拐点。"""
    s = _cfg(max_concurrent=mc)
    assert s.long_lane_cap == cap
    assert mc - s.long_lane_cap == reserve


def test_long_lane_cap_ratio_override():
    """比例可配：调大即收紧长任务，调小即放开。"""
    assert _cfg(max_concurrent=8, long_lane_reserve_ratio=0.5).long_lane_cap == 4
    assert _cfg(max_concurrent=8, long_lane_reserve_ratio=0.1).long_lane_cap == 7


def test_long_task_defaults():
    """默认 shadow：上线即观察，不改任何行为。"""
    s = _cfg()
    assert s.long_task_mode == "shadow"
    assert s.long_task_threshold_sec == 600.0
    assert s.long_task_max_segments == 3
    assert s.long_wait_promote_sec == 1200.0


def test_long_task_env(monkeypatch):
    monkeypatch.setenv("RELAY_LONG_TASK_MODE", "ON")
    monkeypatch.setenv("RELAY_LONG_TASK_THRESHOLD_SEC", "42")
    monkeypatch.setenv("RELAY_LONG_TASK_MAX_SEGMENTS", "5")
    monkeypatch.setenv("RELAY_LONG_LANE_RESERVE_RATIO", "0.25")
    monkeypatch.setenv("RELAY_LONG_WAIT_PROMOTE_SEC", "7")
    monkeypatch.setenv("RELAY_MAX_CONCURRENT", "8")
    s = Settings.from_env()
    assert s.long_task_mode == "on"          # 大小写归一
    assert s.long_task_threshold_sec == 42.0
    assert s.long_task_max_segments == 5
    assert s.long_wait_promote_sec == 7.0
    assert s.long_lane_cap == 6              # 8 − ceil(8×0.25)=2


# ── 段号编码进 resume_hint（零 DB 迁移）────────────────────────────────

def test_segment_marker_roundtrip():
    assert _segment_of("") == 0
    assert _segment_of("普通续跑提示，无标记") == 0
    h = _with_segment("继续未完成的工作", 2)
    assert _segment_of(h) == 2
    assert "继续未完成的工作" in h


def test_segment_marker_no_stacking():
    """多次续跑不能把标记叠成一串——否则 hint 越滚越长、段号读错。"""
    h = _with_segment("第一段提示", 1)
    h = _with_segment(h, 2)
    h = _with_segment(h, 3)
    assert h.count("[seg:") == 1
    assert _segment_of(h) == 3
    assert "第一段提示" in h


def test_segment_marker_malformed():
    assert _segment_of("[seg:abc] 坏标记") == 0
    assert _segment_of("[seg:7 未闭合") == 0
    assert _segment_of("[seg:-3] 负数") == 0


# ── 调度语义 ──────────────────────────────────────────────────────────

def test_lane_blocks_long_but_short_skips_ahead():
    """lane 满时长任务被**跳过**而非阻塞队列——后面的短任务照样立刻拿到保留槽。

    这是 lane 有效的关键：若改成阻塞式，保留槽会被队头长任务白白占住，等于没设。
    """

    async def main():
        gate = asyncio.Event()
        started: list[int] = []

        async def run_fn(tid: int) -> None:
            started.append(tid)
            await gate.wait()

        s = Scheduler(3, run_fn, long_lane_cap=2, long_wait_promote_sec=3600)
        await s.start()
        for i in (1, 2, 3):                      # 3 个长任务先到
            await s.submit(i, "normal", long=True)
        await s.submit(4, "normal")              # 短任务后到
        await asyncio.sleep(0.5)
        assert s.active == 3, s.active
        assert s.active_long == 2, s.active_long
        assert 4 in started, f"短任务未拿到保留槽: {started}"
        assert 3 not in started, f"lane 未挡住第 3 个长任务: {started}"
        gate.set()
        await asyncio.sleep(0.3)
        await s.stop()

    asyncio.run(main())


def test_lane_disabled_by_default_backward_compat():
    """不传 long_lane_cap（=0）即完全不设限：老调用方行为一字不变。"""

    async def main():
        gate = asyncio.Event()
        started: list[int] = []

        async def run_fn(tid: int) -> None:
            started.append(tid)
            await gate.wait()

        s = Scheduler(3, run_fn)
        await s.start()
        for i in (1, 2, 3):
            await s.submit(i, "normal", long=True)
        await asyncio.sleep(0.5)
        assert sorted(started) == [1, 2, 3], started
        # 计数照做（供 /health 与影子期观测），只是 cap<=0 时**不据此拦截**
        assert s.active_long == 3
        gate.set()
        await asyncio.sleep(0.2)
        await s.stop()

    asyncio.run(main())


def test_lane_aging_promotes_starved_long_task():
    """aging 逃生：cap 过紧时长任务不能无界饥饿（重放出过 8.5h 等待）。"""

    async def main():
        gate = asyncio.Event()
        started: list[int] = []

        async def run_fn(tid: int) -> None:
            started.append(tid)
            await gate.wait()

        # cap=1 且 aging 极短：第 2 个长任务本该被挡死，到点自动放行
        s = Scheduler(2, run_fn, long_lane_cap=1, long_wait_promote_sec=0.3)
        await s.start()
        await s.submit(1, "normal", long=True)
        await asyncio.sleep(0.2)
        await s.submit(2, "normal", long=True)
        await asyncio.sleep(0.3)
        assert started == [1], f"aging 未到点就放行了: {started}"
        await asyncio.sleep(1.2)               # _wait_for_aging 有 0.5s 下限
        assert 2 in started, f"aging 未放行饥饿的长任务: {started}"
        gate.set()
        await asyncio.sleep(0.2)
        await s.stop()

    asyncio.run(main())


def test_continuation_priority_below_normal():
    """续跑段降级排队：它已产出过一部分，边际紧迫度低于用户刚发来的新活。"""

    async def main():
        order: list[int] = []
        gate = asyncio.Event()

        async def run_fn(tid: int) -> None:
            if tid == 1:
                await gate.wait()
            order.append(tid)

        s = Scheduler(1, run_fn)
        await s.start()
        await s.submit(1)
        await asyncio.sleep(0.2)               # 让 1 占满唯一槽位
        await s.submit(3, "normal")
        await s.submit(2, "continuation")
        await s.submit(4, "urgent")
        gate.set()
        await asyncio.sleep(0.8)
        await s.stop()
        assert order.index(4) < order.index(3) < order.index(2), order

    asyncio.run(main())


def test_long_slot_released_on_finish():
    """长任务终态即归还名额，下一个长任务能立刻补位（否则 lane 会越收越死）。"""

    async def main():
        gate = asyncio.Event()
        done: list[int] = []

        async def run_fn(tid: int) -> None:
            if tid == 1:
                await gate.wait()
            done.append(tid)

        s = Scheduler(2, run_fn, long_lane_cap=1, long_wait_promote_sec=3600)
        await s.start()
        await s.submit(1, "normal", long=True)
        await asyncio.sleep(0.2)
        await s.submit(2, "normal", long=True)
        await asyncio.sleep(0.3)
        assert s.active_long == 1 and 2 not in done
        gate.set()                             # 1 结束 → 名额归还
        await asyncio.sleep(0.5)
        assert 2 in done, f"名额未归还，长任务卡死: {done}"
        assert s.active_long == 0
        await s.stop()

    asyncio.run(main())


# ── 可观测面：影子期靠 /health 对账，机队各机是否一致一眼可比 ──────────

def test_health_exposes_long_task_fields(relay_factory):
    import httpx

    r = relay_factory(long_task_mode="on", max_concurrent=8)
    h = httpx.get(r["base"] + "/health", timeout=5).json()
    assert h["long_task_mode"] == "on"
    assert h["max_concurrent"] == 8
    assert h["long_lane_cap"] == 5          # 8 − ceil(8/3)=3
    assert h["active_long"] == 0


def test_health_defaults_to_shadow(relay_factory):
    """默认档必须是 shadow：部署即观察，不悄悄改行为。"""
    import httpx

    h = httpx.get(relay_factory()["base"] + "/health", timeout=5).json()
    assert h["long_task_mode"] == "shadow"
