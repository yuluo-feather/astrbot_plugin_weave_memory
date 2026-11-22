"""Versioned content normalization used by the deduplication key."""

from __future__ import annotations

import unicodedata

NORMALIZATION_VERSION = 1
# Sentence-ending punctuation only; internal punctuation is intentionally kept.
_TERMINAL_PUNCTUATION = ".。!?！？;；:：…．｡"


def normalize_content(content: str) -> str:
    """Apply V0 normalization: NFKC, whitespace folding, casefold, terminal trim.

    The original content is never replaced by this value. This function only
    produces the persisted comparison key. Empty normalized content is invalid
    at the entity boundary.
    """
    normalized = unicodedata.normalize("NFKC", content)
    normalized = " ".join(normalized.split()).casefold()
    return normalized.rstrip(_TERMINAL_PUNCTUATION).rstrip()
