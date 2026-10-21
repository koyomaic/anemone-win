from __future__ import annotations

import asyncio
import heapq
import itertools
import time

# 优先级档位：urgent 插队 → normal FIFO → continuation（长任务分段续跑）垫底。
# 续跑段为什么降级：它已经产出过一部分、且是被系统主动续上的，边际紧迫度低于用户
# 刚发来的新活；降级还能把「续跑把长任务耗时放大 3 倍」的压力自然摊开。
_PRIO = {"urgent": 0, "normal": 1, "continuation": 2}


class Scheduler:
    """任务调度器：并发上限 + 优先级 + long lane（为短任务保留槽位）。

    long lane 的存在理由：长任务只占 11.1% 的任务数却吃掉 49.7% 的槽位时间，而分段
    续跑让单个长任务最坏占槽 90m（原来的 3 倍）。不设 lane 时，8 倍流量重放下短任务
    P90 排队 58m——一个"帮我查个电话"要等一小时，主力体验就砸了。

    cap 按**比例**算（见 Settings.long_lane_cap），不是固定值：固定 reserve=1 只在
    max_concurrent=3 时合适，扩到 8 后只占 12.5%、护栏形同虚设（短任务 P90 仍 39m）。
    1/3 比例在 3 槽（cap=2）与 8 槽（cap=5）两个尺度上同为重放拐点。

    关键语义：lane 满时长任务**被跳过而不是阻塞队列**——排在它后面的短任务照样立刻
    拿到保留槽。若改成阻塞式，等于没设 lane。
    """

    def __init__(self, max_concurrent: int, run_fn, on_error=None,
                 long_lane_cap: int = 0, long_wait_promote_sec: float = 1200.0):
        self.max_concurrent = max_concurrent
        self.run_fn = run_fn  # async (task_id)
        self.on_error = on_error  # (task_id, exc)
        # 0/负值＝不设 lane（向后兼容：老调用方不传即维持原行为）
        self.long_lane_cap = long_lane_cap
        self.long_wait_promote_sec = long_wait_promote_sec
        self._heap: list[tuple[int, int, int]] = []
        self._seq = itertools.count()
        self._cond = asyncio.Condition()
        self._active = 0
        self._active_long = 0
        self._stop = False
        self._worker: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()
        # task_id → (is_long, enqueued_at)。is_long 对续跑段在入队时即已知；
        # 首段任务则是跑到 T 才由 mark_long 补记（此时它已在跑，只影响 active 计数）。
        self._meta: dict[int, tuple[bool, float]] = {}
        self._running_long: set[int] = set()

    async def start(self) -> None:
        self._worker = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        self._stop = True
        async with self._cond:
            self._cond.notify_all()
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        for t in list(self._inflight):
            t.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)

    async def submit(self, task_id: int, priority: str = "normal",
                     long: bool = False) -> None:
        async with self._cond:
            heapq.heappush(
                self._heap, (_PRIO.get(priority, 1), next(self._seq), task_id))
            self._meta[task_id] = (long, time.monotonic())
            self._cond.notify()

    def mark_long(self, task_id: int) -> None:
        """任务跑到 T 被判定为长任务（executor 读循环内调用，同步、不阻塞）。

        只影响 active 计数与后续入队归类；已在跑的它不会被抢占。
        释放不需要对应方法——`_run_one` 的 finally 统一归还名额并 notify，
        续跑段重新入队时会带 long=True 拿到新的 meta。
        """
        was_long, enq = self._meta.get(task_id, (False, time.monotonic()))
        self._meta[task_id] = (True, enq)
        if not was_long and task_id not in self._running_long:
            self._running_long.add(task_id)
            self._active_long += 1

    @property
    def pending(self) -> int:
        return len(self._heap)

    @property
    def active(self) -> int:
        return self._active

    @property
    def active_long(self) -> int:
        return self._active_long

    def queue_info(self, task_id: int | None = None) -> dict:
        now = time.monotonic()
        queued = [tid for _prio, _seq, tid in self._heap]
        info = {"pending": len(queued), "active": self._active,
                "active_long": self._active_long}
        if task_id is not None:
            info["queued"] = task_id in queued
            try:
                info["position"] = queued.index(task_id) + 1
            except ValueError:
                info["position"] = 0
            is_long, enq = self._meta.get(task_id, (False, now))
            info["long"] = bool(is_long)
            info["lane_open"] = self._lane_open(task_id, now)
            info["wait_sec"] = max(0.0, now - enq) if is_long else 0.0
        return info

    def _lane_open(self, task_id: int, now: float) -> bool:
        """该任务此刻是否允许占用槽位（lane 视角）。"""
        if self.long_lane_cap <= 0:
            return True
        is_long, enq = self._meta.get(task_id, (False, now))
        if not is_long:
            return True                      # 短任务永远走保留槽，不受 lane 约束
        if self._active_long < self.long_lane_cap:
            return True
        # aging 逃生：等够久就放行，杜绝无界饥饿（cap 过紧时重放出过 8.5h 等待）
        return (now - enq) >= self.long_wait_promote_sec

    def _pop_eligible(self, now: float):
        """按优先级取出第一个 lane 允许的任务；被跳过的原样压回堆。

        队列深度是个位数，pop/push 一轮的代价可忽略；换来的是「长任务被跳过而非
        阻塞队列」这个关键语义。
        """
        deferred = []
        chosen = None
        while self._heap:
            item = heapq.heappop(self._heap)
            if self._lane_open(item[2], now):
                chosen = item
                break
            deferred.append(item)
        for it in deferred:
            heapq.heappush(self._heap, it)
        return chosen

    async def _loop(self) -> None:
        while not self._stop:
            async with self._cond:
                chosen = None
                while not self._stop:
                    if not self._heap or self._active >= self.max_concurrent:
                        await self._cond.wait()
                        continue
                    now = time.monotonic()
                    chosen = self._pop_eligible(now)
                    if chosen is not None:
                        break
                    # 有任务但全被 lane 挡住：不能裸等——aging 是时间条件，没人会
                    # notify，必须定时醒来自检。
                    await self._wait_for_aging(now)
                if self._stop or chosen is None:
                    break
                _, _, task_id = chosen
                self._active += 1
                if self._meta.get(task_id, (False, 0.0))[0]:
                    self._running_long.add(task_id)
                    self._active_long += 1
            t = asyncio.get_running_loop().create_task(self._run_one(task_id))
            self._inflight.add(t)
            t.add_done_callback(self._inflight.discard)

    async def _wait_for_aging(self, now: float) -> None:
        """在 cond 上带超时地等：notify＝有新事件，超时＝到点重估 lane（aging）。"""
        queued = {h[2] for h in self._heap}
        due = [enq + self.long_wait_promote_sec
               for tid, (lg, enq) in self._meta.items() if lg and tid in queued]
        timeout = 60.0                      # 兜底自检周期，相对任务耗时可忽略
        if due and self.long_wait_promote_sec > 0:
            timeout = max(0.5, min(timeout, min(due) - now))
        try:
            await asyncio.wait_for(self._cond.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def _run_one(self, task_id: int) -> None:
        try:
            await self.run_fn(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self.on_error:
                try:
                    self.on_error(task_id, e)
                except Exception:
                    pass
        finally:
            async with self._cond:
                self._active -= 1
                if task_id in self._running_long:
                    self._running_long.discard(task_id)
                    self._active_long = max(0, self._active_long - 1)
                self._meta.pop(task_id, None)
                self._cond.notify()
