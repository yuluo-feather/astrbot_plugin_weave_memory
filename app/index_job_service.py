"""VECTOR outbox worker service: claim, embed outside the lock, persist."""

from __future__ import annotations

import asyncio
import logging

from ..core.errors import (
    DomainError,
    NotFound,
    SensitiveContentBlocked,
    VectorDimensionMismatch,
)
from ..core.model import (
    IndexJob,
    IndexOperation,
    MemoryStatus,
)
from ..core.ports import (
    Clock,
    EmbeddingProvider,
    IndexJobStore,
    MemoryStore,
    VectorIndex,
)
from .write_coordinator import WriteCoordinator

logger = logging.getLogger(__name__)


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, VectorDimensionMismatch):
        return "vector_dim_mismatch"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "provider_timeout"
    return "provider_unavailable"


class IndexJobService:
    def __init__(self, store: MemoryStore, jobs: IndexJobStore,
                 vector: VectorIndex, provider: EmbeddingProvider | None,
                 coordinator: WriteCoordinator, clock: Clock, *,
                 max_attempts: int = 5, batch_size: int = 20,
                 enabled: bool = False) -> None:
        self._store = store
        self._jobs = jobs
        self._vector = vector
        self._provider = provider
        self._coordinator = coordinator
        self._clock = clock
        self._max_attempts = max(1, max_attempts)
        self._batch_size = max(1, batch_size)
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled and self._provider is not None

    def backlog(self) -> dict[str, int]:
        return self._jobs.count_by_status()

    async def _claim(self) -> IndexJob | None:
        now = self._clock.now()
        return await self._coordinator.run_serialized(
            lambda: self._jobs.claim_next(at=now),
        )

    async def _complete(self, job: IndexJob) -> None:
        now = self._clock.now()
        await self._coordinator.run_serialized(
            lambda: self._jobs.mark_done(job.id, at=now),
        )

    async def _fail(self, job: IndexJob, code: str) -> None:
        now = self._clock.now()
        status = await self._coordinator.run_serialized(
            lambda: self._jobs.mark_failed(
                job.id, error_code=code, max_attempts=self._max_attempts, at=now,
            ),
        )
        logger.warning(
            "index job failed: id=%s operation=%s status=%s code=%s",
            job.id, job.operation.value, status.value, code,
        )

    async def _process_upsert(self, job: IndexJob) -> None:
        memory = await self._coordinator.run_serialized(
            lambda: self._store.get_any(job.memory_id),
        )
        if memory.status is not MemoryStatus.ACTIVE:
            await self._coordinator.run_serialized(
                lambda: self._vector.remove(job.memory_id),
            )
            await self._complete(job)
            return
        if self._provider is None:
            raise VectorDimensionMismatch("embedding provider is not configured")
        provider = self._provider
        vectors = await provider.embed_documents((memory.content,))
        if len(vectors) != 1:
            raise VectorDimensionMismatch("embedding provider returned no vector")
        vector = vectors[0]
        lineage, creation_seq = await self._coordinator.run_serialized(
            lambda: self._store.get_lineage(
                memory.id, scope_id=memory.scope_id, subject_id=memory.subject_id,
            ),
        )
        await self._coordinator.run_serialized(
            lambda: self._vector.upsert(
                memory.id, vector, dim=provider.dimension,
                provider=provider.provider_id, model=provider.model,
                content_hash=job.content_hash,
                db_lineage_id=lineage, creation_seq=creation_seq,
                revision=memory.current_revision,
                created_at=self._clock.now(),
            ),
        )
        await self._complete(job)

    async def _process_delete(self, job: IndexJob) -> None:
        await self._coordinator.run_serialized(
            lambda: self._vector.remove(job.memory_id),
        )
        await self._complete(job)

    async def drain_once(self, *, max_jobs: int | None = None) -> int:
        if not self.enabled:
            return 0
        budget = max_jobs if max_jobs is not None else self._batch_size
        processed = 0
        for _ in range(budget):
            job = await self._claim()
            if job is None:
                break
            try:
                if job.operation is IndexOperation.DELETE:
                    await self._process_delete(job)
                else:
                    await self._process_upsert(job)
            except NotFound:
                await self._complete(job)
            except SensitiveContentBlocked:
                # Sensitive text must never be sent; skipping is final, not retryable.
                await self._complete(job)
            except (DomainError, asyncio.TimeoutError, OSError) as exc:
                await self._fail(job, _error_code(exc))
            except Exception:  # noqa: BLE001 - worker must not die
                logger.exception("unexpected index job failure: id=%s", job.id)
                await self._fail(job, "provider_unavailable")
            processed += 1
        return processed
