"""Small standard-library implementations of Clock and IdGenerator ports."""

from datetime import UTC, datetime
from uuid import uuid4


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class UUID4IdGenerator:
    def new_id(self, entity_kind: str) -> str:
        # Entity kind is intentionally not encoded in the opaque identifier.
        return uuid4().hex
