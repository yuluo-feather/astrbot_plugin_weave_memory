"""Reciprocal rank fusion (stdlib only)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    memory_id: str
    rank: int
    score: float
    channel_ranks: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class FusionResult:
    method: str
    k: int
    candidates: tuple[FusedCandidate, ...]


def reciprocal_rank_fusion(channels: Mapping[str, Sequence[str]], *,
                           k: int = 60) -> FusionResult:
    populated = {
        name: tuple(dict.fromkeys(ids))
        for name, ids in channels.items() if ids
    }
    if not populated:
        return FusionResult(method="NONE", k=k, candidates=())
    if len(populated) == 1:
        name, ids = next(iter(populated.items()))
        return FusionResult(
            method="NONE", k=k,
            candidates=tuple(
                FusedCandidate(memory_id=memory_id, rank=rank, score=0.0,
                               channel_ranks={name: rank})
                for rank, memory_id in enumerate(ids, start=1)
            ),
        )
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for name, ids in populated.items():
        for rank, memory_id in enumerate(ids, start=1):
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (k + rank)
            ranks.setdefault(memory_id, {})[name] = rank
    ordered = sorted(
        scores,
        key=lambda memory_id: (
            min(ranks[memory_id].values()), -scores[memory_id], memory_id,
        ),
    )
    return FusionResult(
        method="RRF", k=k,
        candidates=tuple(
            FusedCandidate(
                memory_id=memory_id, rank=rank, score=scores[memory_id],
                channel_ranks=ranks[memory_id],
            )
            for rank, memory_id in enumerate(ordered, start=1)
        ),
    )
