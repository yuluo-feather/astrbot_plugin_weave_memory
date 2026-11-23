"""Pre-model sensitive information filter for V1.5 extraction (ADR-0015)."""

from __future__ import annotations

import re
from dataclasses import dataclass

CREDENTIAL = "credential"
ID_NUMBER = "id_number"
BANK_CARD = "bank_card"
ADDRESS = "address"

KNOWN_CATEGORIES = (CREDENTIAL, ID_NUMBER, BANK_CARD, ADDRESS)

_PATTERNS: dict[str, re.Pattern[str]] = {
    CREDENTIAL: re.compile(
        r"(?i)(?:密码|口令|令牌|密钥|password|passwd|pwd|secret|token|api[\s_-]?key)"
        r"\s*[:=：]?\s*\S+"
    ),
    ID_NUMBER: re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])"),
    BANK_CARD: re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)"),
    ADDRESS: re.compile(
        r"[一-龥]{1,12}(?:省|市|自治区)"
        r"[一-龥]{0,12}(?:市|区|县)"
        r"[一-龥]{0,20}(?:路|街|巷|号|弄)"
        r"[一-龥\d]{0,12}(?:号|栋|楼|室)?"
    ),
}


@dataclass(frozen=True, slots=True)
class SensitiveScan:
    detected: frozenset[str]
    blocked: frozenset[str]

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked)


def detect_sensitive_categories(text: str) -> frozenset[str]:
    return frozenset(
        category for category, pattern in _PATTERNS.items() if pattern.search(text)
    )


def scan_sensitive(text: str, allowed_categories: tuple[str, ...]) -> SensitiveScan:
    detected = detect_sensitive_categories(text)
    allowed = frozenset(allowed_categories) & frozenset(KNOWN_CATEGORIES)
    return SensitiveScan(detected=detected, blocked=detected - allowed)


def redact_sensitive(text: str, categories: frozenset[str]) -> str:
    redacted = text
    for category in categories:
        pattern = _PATTERNS.get(category)
        if pattern is not None:
            redacted = pattern.sub(f"[已隐藏:{category}]", redacted)
    return redacted
