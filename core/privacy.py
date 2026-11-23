"""Query redaction helpers shared by trace writers."""

from __future__ import annotations

import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_LONG_NUMBER = re.compile(r"(?<!\d)\d{12,}(?!\d)")


def redact_query(query: str) -> str:
    text = _EMAIL.sub("[email]", query)
    text = _PHONE.sub("[phone]", text)
    return _LONG_NUMBER.sub("[number]", text)
