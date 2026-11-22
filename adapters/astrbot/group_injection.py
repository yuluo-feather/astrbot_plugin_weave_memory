"""Private-only group memory context selection for the LLM hook."""

from typing import Any

from ...app.consent_service import ConsentService, GroupConsentType
from ...app.group_recall_service import GroupContextComposer
from ...app.scope_service import GroupScopeService
from ...core.errors import AuthorizationDenied
from .identity import AstrBotIdentityAdapter


class AstrBotGroupMemoryInjector:
    def __init__(self, scopes: GroupScopeService, consent: ConsentService,
                 composer: GroupContextComposer) -> None:
        self._scopes = scopes
        self._consent = consent
        self._composer = composer
        self._identity = AstrBotIdentityAdapter()

    async def context_for(self, event: Any) -> str | None:
        try:
            identity = self._identity.resolve_group_identity(event)
        except AuthorizationDenied:
            return None
        scope = self._scopes.find(identity)
        if scope is None or not self._scopes.is_enabled(scope):
            return None
        if not self._consent.is_granted(scope, GroupConsentType.GROUP_MEMORY):
            return None
        context = await self._composer.compose(event.message_str, scope)
        return context.content if context is not None else None
