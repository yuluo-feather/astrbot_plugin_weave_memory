"""Private-message identity and memory-context selection for the LLM hook."""

from typing import Any

from ...app.consent_service import ConsentService, PersonalConsentType
from ...app.recall_service import MemoryContextComposer
from ...app.scope_service import PersonalScopeService
from ...core.errors import AuthorizationDenied
from .identity import AstrBotIdentityAdapter


class AstrBotMemoryInjector:
    def __init__(self, scopes: PersonalScopeService,
                 consent: ConsentService,
                 composer: MemoryContextComposer) -> None:
        self._scopes = scopes
        self._consent = consent
        self._composer = composer
        self._identity = AstrBotIdentityAdapter()

    async def context_for(self, event: Any) -> str | None:
        try:
            identity = self._identity.resolve_identity(event)
        except AuthorizationDenied:
            # Group chats and incomplete identities are expected non-targets.
            return None
        scope = self._scopes.find(identity)
        if scope is None or not self._scopes.is_enabled(scope):
            return None
        if not self._consent.is_granted(scope, PersonalConsentType.PERSONAL_MEMORY):
            return None
        context = await self._composer.compose(event.message_str, scope)
        return context.content if context is not None else None
