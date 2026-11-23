"""Bounded LRU cache for embedding vectors (stdlib only)."""

from __future__ import annotations

import time
from collections import OrderedDict
from threading import RLock


class EmbeddingLRUCache:
    def __init__(self, *, max_mb: int = 100, ttl_seconds: int = 1800) -> None:
        self._max_bytes = max(1, max_mb) * 1024 * 1024
        self._ttl = max(1, ttl_seconds)
        self._items: OrderedDict[tuple[str, str, str], tuple[float, tuple[float, ...]]] = (
            OrderedDict()
        )
        self._bytes = 0
        self._hits = 0
        self._lock = RLock()

    @staticmethod
    def _size(vector: tuple[float, ...]) -> int:
        return 64 + len(vector) * 8

    def get(self, provider: str, model: str, content_hash: str) -> tuple[float, ...] | None:
        key = (provider, model, content_hash)
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            stored_at, vector = item
            if now - stored_at > self._ttl:
                self._items.pop(key, None)
                self._bytes -= self._size(vector)
                return None
            self._items.move_to_end(key)
            self._hits += 1
            return vector

    def put(self, provider: str, model: str, content_hash: str,
            vector: tuple[float, ...]) -> None:
        key = (provider, model, content_hash)
        size = self._size(vector)
        with self._lock:
            existing = self._items.pop(key, None)
            if existing is not None:
                self._bytes -= self._size(existing[1])
            while self._items and self._bytes + size > self._max_bytes:
                _, (_, evicted) = self._items.popitem(last=False)
                self._bytes -= self._size(evicted)
            if size > self._max_bytes:
                return
            self._items[key] = (time.monotonic(), vector)
            self._bytes += size

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._items)
