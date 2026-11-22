"""Fail-closed consent policy for the V0 personal-memory boundary."""

from ..errors import AuthorizationDenied
from ..model import ConsentState, MemoryConsent


class ConsentPolicy:
    def __init__(self, consents: list[MemoryConsent] | tuple[MemoryConsent, ...] = ()) -> None:
        self._consents = tuple(consents)

    def require(self, *, subject_id: str, scope_id: str, consent_type: str) -> None:
        matches = [c for c in self._consents
                   if c.subject_id == subject_id and c.scope_id == scope_id
                   and c.consent_type == consent_type]
        # Consent events are append-only; the last matching event wins.
        if not matches:
            raise AuthorizationDenied("consent not found")
        if matches[-1].state is not ConsentState.GRANTED:
            raise AuthorizationDenied("consent is not granted")
