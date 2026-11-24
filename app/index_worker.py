"""Async lifecycle for the VECTOR outbox worker."""

from __future__ import annotations

import asyncio
import logging

from ..core.ports import Clock, IndexJobStore
from .index_job_service import IndexJobService
from .index_rebuild_service import IndexRebuildService

logger = logging.getLogger(__name__)


class IndexWorker:
    def __init__(self, service: IndexJobService, jobs: IndexJobStore,
                 rebuild: IndexRebuildService, clock: Clock, *,
                 interval_seconds: int = 15, enabled: bool = False) -> None:
        self._service = service
        self._jobs = jobs
        self._rebuild = rebuild
        self._clock = clock
        self._interval_seconds = max(1, int(interval_seconds))
        self._enabled = enabled
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    def reset(self) -> None:
        reset = self._jobs.reset_running_to_pending(at=self._clock.now())
        if reset:
            logger.info("index worker reset %s RUNNING job(s) to PENDING", reset)
        self._rebuild.verify_config()

    def start(self) -> None:
        self.reset()
        if not self._enabled or not self._service.enabled:
            return
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._stopped = False
        self._task = loop.create_task(self._loop())

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                await self._service.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - worker must not die
                logger.exception("index worker loop failed; will retry")
            await asyncio.sleep(self._interval_seconds)

    async def stop(self) -> None:
        self._stopped = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
