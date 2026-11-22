"""Private-message identity and memory-context selection for the LLM hook."""

from typing import Any

from ...app.recall_service import MemoryContextComposer
from ...app.scope_service import PersonalScopeService
from .identity import AstrBotIdentityAdapter


class AstrBotMemoryInjector:
    def __init__(self, scopes: PersonalScopeService,
                 composer: MemoryContextComposer) -> None:
        self._scopes = scopes
        self._composer = composer
        self._identity = AstrBotIdentityAdapter()

    async def context_for(self, event: Any) -> str | None:
        identity = self._identity.resolve_identity(event)
        scope = self._scopes.find(identity)
        if scope is None or not self._scopes.is_enabled(scope):
            return None
        context = await self._composer.compose(event.message_str, scope)
        return context.content if context is not None else None
