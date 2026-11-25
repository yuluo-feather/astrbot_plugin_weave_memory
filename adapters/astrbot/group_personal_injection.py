"""AstrBot event adapter for V2.4 Group personal context injection."""

from typing import Any

from ...app.group_personal_recall_service import GroupPersonalContextComposer
from ...app.group_personal_service import GroupPersonalService
from ...core.errors import AuthorizationDenied
from .identity import AstrBotIdentityAdapter


class AstrBotGroupPersonalMemoryInjector:
    def __init__(
        self, service: GroupPersonalService,
        composer: GroupPersonalContextComposer,
    ) -> None:
        self._service = service
        self._composer = composer
        self._identity = AstrBotIdentityAdapter()

    async def context_for(self, event: Any) -> str | None:
        try:
            group_identity = self._identity.resolve_group_identity(event)
            personal_identity = self._identity.resolve_personal_identity_for_group(event)
        except AuthorizationDenied:
            return None
        scope = self._service.eligible_personal_scope(
            group_identity, personal_identity,
        )
        if scope is None:
            return None
        context = await self._composer.compose(event.message_str, scope)
        return context.content if context is not None else None
