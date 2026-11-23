"""VECTOR index rebuild and configuration verification."""

from __future__ import annotations

import logging
from datetime import datetime
from hashlib import sha256

from ..core.errors import IndexNotConfigured
from ..core.ports import (
    Clock,
    EmbeddingProvider,
    MemoryStore,
    VectorIndex,
)
from .write_coordinator import WriteCoordinator

logger = logging.getLogger(__name__)


class IndexRebuildService:
    def __init__(self, store: MemoryStore, vector: VectorIndex,
                 provider: EmbeddingProvider | None,
                 coordinator: WriteCoordinator, clock: Clock) -> None:
        self._store = store
        self._vector = vector
        self._provider = provider
        self._coordinator = coordinator
        self._clock = clock

    def verify_config(self, *, at: datetime | None = None) -> bool:
        now = at or self._clock.now()
        if self._provider is None:
            self._vector.mark_stale(at=now)
            return False
        current = self._vector.status()
        if (
            current is None
            or current.status.value != "HEALTHY"
            or current.dim != self._provider.dimension
            or current.config_hash != getattr(self._vector, "config_hash", None)
        ):
            self._vector.mark_stale(
                at=now, dim=self._provider.dimension,
                config_hash=getattr(self._vector, "config_hash", None),
            )
            return False
        return True

    async def rebuild_all(self, *, scope_id: str | None = None,
                          subject_id: str | None = None) -> int:
        if self._provider is None:
            self._vector.mark_stale(at=self._clock.now())
            raise IndexNotConfigured("embedding provider is not configured")
        provider = self._provider
        await self._coordinator.run_serialized(
            lambda: self._vector.mark_rebuilding(at=self._clock.now()),
        )
        rebuilt = 0
        try:
            await self._coordinator.run_serialized(
                lambda: self._vector.clear(
                    scope_id=scope_id, subject_id=subject_id,
                ),
            )
            if scope_id is not None and subject_id is not None:
                memories = self._store.list_active(
                    scope_id=scope_id, subject_id=subject_id,
                )
            else:
                memories = self._store.list_active_all()
            for memory in memories:
                scope_type = self._store.scope_type(memory.scope_id)
                vectors = await provider.embed_documents(
                    (memory.content,), scope_type=scope_type,
                    scope_id=memory.scope_id,
                )
                if not vectors:
                    continue
                lineage, creation_seq = await self._coordinator.run_serialized(
                    lambda: self._store.get_lineage(
                        memory.id, scope_id=memory.scope_id,
                        subject_id=memory.subject_id,
                    ),
                )
                await self._coordinator.run_serialized(
                    lambda: self._vector.upsert(
                        memory.id, vectors[0], dim=provider.dimension,
                        provider=provider.provider_id,
                        model=provider.model,
                        content_hash=sha256(
                            memory.normalized_content.encode("utf-8"),
                        ).hexdigest(),
                        db_lineage_id=lineage, creation_seq=creation_seq,
                        revision=memory.current_revision,
                        created_at=self._clock.now(),
                    ),
                )
                rebuilt += 1
            await self._coordinator.run_serialized(
                lambda: self._vector.mark_healthy(at=self._clock.now()),
            )
            return rebuilt
        except Exception:
            logger.exception("vector index rebuild failed")
            await self._coordinator.run_serialized(
                lambda: self._vector.mark_failed(at=self._clock.now()),
            )
            raise

    def mark_stale(self) -> None:
        self._vector.mark_stale(at=self._clock.now())
