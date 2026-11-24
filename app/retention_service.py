"""Soft-delete retention cleanup: purge ARCHIVED rows whose purge_after elapsed."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from ..core.errors import DomainError
from ..core.model import OperatorType
from ..core.ports import Clock, IdGenerator, MemoryStore
from .lifecycle_service import LifecycleService

logger = logging.getLogger(__name__)


class RetentionService:
    def __init__(self, store: MemoryStore, lifecycle: LifecycleService,
                 clock: Clock, ids: IdGenerator, *,
                 batch_size: int = 100, interval_seconds: int = 3600) -> None:
        self._store = store
        self._lifecycle = lifecycle
        self._clock = clock
        self._ids = ids
        self._batch_size = max(1, int(batch_size))
        self._interval_seconds = max(60, int(interval_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    async def expire_overdue(self, now: datetime | None = None) -> int:
        """Move ACTIVE rows past valid_until to EXPIRED; never deletes them."""
        at = now or self._clock.now()
        expired_total = 0
        while True:
            rows = self._store.list_expired_active(at=at, limit=self._batch_size)
            if not rows:
                return expired_total
            progressed = 0
            for memory in rows:
                try:
                    receipt = await self._lifecycle.expire_active(
                        memory, revision_id=self._ids.new_id("revision"), at=at,
                    )
                except (DomainError, OSError) as exc:
                    logger.warning(
                        "semantic expiry failed: memory=%s error=%s",
                        memory.id, type(exc).__name__,
                    )
                    continue
                if receipt is not None:
                    progressed += 1
                    expired_total += 1
            if progressed == 0:
                return expired_total

    async def purge_expired(self, now: datetime | None = None) -> int:
        """Purge one or more batches; return the number of rows removed."""
        at = now or self._clock.now()
        purged_total = 0
        while True:
            rows = self._store.list_expired_archives(at=at, limit=self._batch_size)
            if not rows:
                return purged_total
            progressed = 0
            for memory in rows:
                try:
                    receipt = await self._lifecycle.purge_memory(
                        memory.id, scope_id=memory.scope_id,
                        subject_id=memory.subject_id,
                        operation_id=self._ids.new_id("operation"),
                        operator_type=OperatorType.SYSTEM,
                        operator_id="weave_retention",
                        at=at,
                    )
                except (DomainError, OSError) as exc:
                    logger.warning(
                        "retention purge failed: memory=%s error=%s",
                        memory.id, type(exc).__name__,
                    )
                    continue
                purged_total += receipt.affected_count
                progressed += 1
            if progressed == 0:
                return purged_total

    async def run_forever(self) -> None:
        while not self._stopped:
            try:
                await self.expire_overdue()
                await self.purge_expired()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - retention must not stop the plugin
                logger.exception("retention loop failed; will retry")
            await asyncio.sleep(self._interval_seconds)

    def start(self) -> None:
        if self._task is None or self._task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._stopped = False
            self._task = loop.create_task(self.run_forever())

    async def stop(self) -> None:
        self._stopped = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
