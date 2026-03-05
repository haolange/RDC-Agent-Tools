"""
RDX-MCP 的 GPU worker 调度器。

将 replay worker slots 作为稀缺资源管理。每个 slot 表示对 GPU replay
context（本地或远程）的独占访问。调度器对每种 backend 使用
:class:`asyncio.Semaphore`，调用方会阻塞直到 slot 可用，并遵循简单
优先级方案：

    0 = interactive  （最高 —— 用户触发的单次操作）
    1 = batch        （中等 —— 自动化实验循环）
    2 = regression   （最低 —— 后台回归巡检）

数值越小优先级越高。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Priority constants（优先级常量）
# ---------------------------------------------------------------------------

PRIORITY_INTERACTIVE: int = 0
PRIORITY_BATCH: int = 1
PRIORITY_REGRESSION: int = 2

# ---------------------------------------------------------------------------
# WorkerSlot
# ---------------------------------------------------------------------------


@dataclass
class WorkerSlot:
    """表示单个 GPU replay worker。

    Attributes
    ----------
    slot_id:
        该 slot 的唯一标识（未提供时自动生成）。
    gpu_index:
        该 slot 绑定的 GPU 设备索引。
    backend_type:
        ``"local"`` 或 ``"remote"``。
    busy:
        是否已被占用。
    current_task_id:
        当前占用该 slot 的任务标识，或 ``None``。
    device_info:
        任意设备元数据（driver 版本、设备名称等）。
    """

    slot_id: str = field(default_factory=lambda: f"slot_{uuid.uuid4().hex[:8]}")
    gpu_index: int = 0
    backend_type: str = "local"
    busy: bool = False
    current_task_id: Optional[str] = None
    device_info: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal priority-aware waiter queue（内部优先级等待队列）
# ---------------------------------------------------------------------------


class _PriorityWaiter:
    """优先级队列中的单个等待者。

    每个等待者带有 *priority*（数值越小优先级越高），并持有一个
    :class:`asyncio.Event`，当 slot 可用时被置位。
    """

    __slots__ = ("priority", "event", "_id")

    # 单调计数器，用于按 FIFO 顺序打破优先级相同的并列。
    _counter: int = 0

    def __init__(self, priority: int) -> None:
        self.priority = priority
        self.event = asyncio.Event()
        _PriorityWaiter._counter += 1
        self._id = _PriorityWaiter._counter

    def __lt__(self, other: _PriorityWaiter) -> bool:  # type: ignore[override]
        if self.priority != other.priority:
            return self.priority < other.priority
        return self._id < other._id


# ---------------------------------------------------------------------------
# WorkerScheduler
# ---------------------------------------------------------------------------


class WorkerScheduler:
    """GPU replay worker slots 的 async-safe 调度器。

    Parameters
    ----------
    max_local_workers:
        可同时获取的 *local* worker slots 最大数量。
    max_remote_workers:
        可同时获取的 *remote* worker slots 最大数量。
    """

    def __init__(
        self,
        max_local_workers: int = 1,
        max_remote_workers: int = 1,
    ) -> None:
        self._max: Dict[str, int] = {
            "local": max_local_workers,
            "remote": max_remote_workers,
        }

        # Semaphores limit overall concurrency per backend type.
        self._semaphores: Dict[str, asyncio.Semaphore] = {
            "local": asyncio.Semaphore(max_local_workers),
            "remote": asyncio.Semaphore(max_remote_workers),
        }

        # All registered slots keyed by slot_id.
        self._slots: Dict[str, WorkerSlot] = {}

        # Protects internal mutations.
        self._lock = asyncio.Lock()

        # Per-backend priority waiter queues.
        self._waiters: Dict[str, List[_PriorityWaiter]] = {
            "local": [],
            "remote": [],
        }

    # -- slot registration --------------------------------------------------

    def register_slot(self, slot: WorkerSlot) -> None:
        """向池中添加一个 worker slot。

        Parameters
        ----------
        slot:
            要注册的 :class:`WorkerSlot`。其 ``backend_type`` 必须为
            ``"local"`` 或 ``"remote"``。

        Raises
        ------
        ValueError
            当 backend 类型不支持或已有相同 ``slot_id`` 时抛出。
        """
        if slot.backend_type not in self._semaphores:
            raise ValueError(
                f"Unsupported backend_type: {slot.backend_type!r}. "
                f"Expected one of {list(self._semaphores)}"
            )
        if slot.slot_id in self._slots:
            raise ValueError(f"Slot already registered: {slot.slot_id}")
        self._slots[slot.slot_id] = slot
        logger.info(
            "Registered worker slot %s (backend=%s, gpu=%d)",
            slot.slot_id,
            slot.backend_type,
            slot.gpu_index,
        )

    # -- acquire / release --------------------------------------------------

    async def acquire(
        self,
        backend_type: str = "local",
        priority: int = PRIORITY_INTERACTIVE,
    ) -> WorkerSlot:
        """获取空闲的 worker slot，若无则阻塞等待。

        高优先级等待者（更小数值）优先于低优先级。

        Parameters
        ----------
        backend_type:
            ``"local"`` 或 ``"remote"``。
        priority:
            优先级（0 = interactive，1 = batch，2 = regression）。

        Returns
        -------
        WorkerSlot
            已获取的 slot，``busy=True``。

        Raises
        ------
        ValueError
            当 *backend_type* 不被识别时抛出。
        RuntimeError
            当未注册该 backend 类型的 slot 时抛出。
        """
        if backend_type not in self._semaphores:
            raise ValueError(
                f"Unknown backend_type: {backend_type!r}. "
                f"Expected one of {list(self._semaphores)}"
            )

        sem = self._semaphores[backend_type]

        # 快速路径：尝试无阻塞获取。
        acquired = sem._value > 0  # noqa: SLF001 -- peek at semaphore value
        if not acquired:
            # 慢路径：注册优先级等待者并等待。
            waiter = _PriorityWaiter(priority)
            async with self._lock:
                self._waiters[backend_type].append(waiter)
                self._waiters[backend_type].sort()
            # 等待直到成为 *首位* 等待者且有 slot 空闲。
            while True:
                await waiter.event.wait()
                waiter.event.clear()
                async with self._lock:
                    # Only proceed if we are the highest-priority waiter.
                    queue = self._waiters[backend_type]
                    if queue and queue[0] is waiter and sem._value > 0:  # noqa: SLF001
                        queue.pop(0)
                        break
                    # Spurious wake or overtaken -- keep waiting.

        await sem.acquire()

        # Find a free slot of the matching backend type.
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        async with self._lock:
            for slot in self._slots.values():
                if slot.backend_type == backend_type and not slot.busy:
                    slot.busy = True
                    slot.current_task_id = task_id
                    logger.debug(
                        "Acquired slot %s for task %s (priority=%d)",
                        slot.slot_id,
                        task_id,
                        priority,
                    )
                    return slot

            # No registered slot is free -- this should only happen when
            # more semaphore permits exist than registered slots.  Create a
            # transient virtual slot so the caller is not stuck.
            virtual = WorkerSlot(
                backend_type=backend_type,
                busy=True,
                current_task_id=task_id,
            )
            self._slots[virtual.slot_id] = virtual
            logger.warning(
                "No free registered slot; created virtual slot %s",
                virtual.slot_id,
            )
            return virtual

    async def release(self, slot_id: str) -> None:
        """将已获取的 worker slot 释放回池中。

        Parameters
        ----------
        slot_id:
            要释放的 slot 的 ``slot_id``。

        Raises
        ------
        KeyError
            当 slot id 未知时抛出。
        RuntimeError
            当 slot 当前不处于 busy 时抛出。
        """
        async with self._lock:
            slot = self._slots.get(slot_id)
            if slot is None:
                raise KeyError(f"Unknown slot_id: {slot_id}")
            if not slot.busy:
                raise RuntimeError(
                    f"Slot {slot_id} is not currently acquired"
                )

            prev_task = slot.current_task_id
            slot.busy = False
            slot.current_task_id = None

            backend = slot.backend_type
            self._semaphores[backend].release()

            logger.debug(
                "Released slot %s (was task %s)", slot_id, prev_task
            )

            # 唤醒该 backend 类型的最高优先级等待者。
            queue = self._waiters[backend]
            if queue:
                queue[0].event.set()

    # -- status -------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """返回各 backend 类型的当前池状态。

        Returns
        -------
        dict
            backend 类型到 ``{"total", "busy", "free"}`` 计数的映射，
            并包含 ``"all"`` 汇总。

        Example::

            {
                "local":  {"total": 2, "busy": 1, "free": 1},
                "remote": {"total": 1, "busy": 0, "free": 1},
                "all":    {"total": 3, "busy": 1, "free": 2},
            }
        """
        result: Dict[str, Dict[str, int]] = {}
        all_total = 0
        all_busy = 0

        for backend in self._semaphores:
            slots = [
                s for s in self._slots.values()
                if s.backend_type == backend
            ]
            total = len(slots)
            busy = sum(1 for s in slots if s.busy)
            free = total - busy
            result[backend] = {"total": total, "busy": busy, "free": free}
            all_total += total
            all_busy += busy

        result["all"] = {
            "total": all_total,
            "busy": all_busy,
            "free": all_total - all_busy,
        }
        return result
