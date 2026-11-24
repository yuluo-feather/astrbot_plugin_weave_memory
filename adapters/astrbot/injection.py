"""Private-message identity and memory-context selection for the LLM hook."""

from typing import Any

from ...app.consent_service import ConsentService, PersonalConsentType
from ...app.recall_service import MemoryContextComposer
from ...app.scope_service import PersonalScopeService
from ...core.errors import AuthorizationDenied
from .identity import AstrBotIdentityAdapter
from .notices import AUTO_ENROLL_NOTICE, AUTO_ENROLL_PROMPT


class AstrBotMemoryInjector:
    def __init__(self, scopes: PersonalScopeService,
                 consent: ConsentService,
                 composer: MemoryContextComposer, *,
                 auto_enroll: bool = False) -> None:
        self._scopes = scopes
        self._consent = consent
        self._composer = composer
        self._auto_enroll = auto_enroll
        self._identity = AstrBotIdentityAdapter()
        self._prompted: set[tuple[str, str, str]] = set()

    async def _send_auto_enroll_notice(self, event: Any) -> None:
        try:
            await event.send(event.plain_result(AUTO_ENROLL_NOTICE))
        except Exception:
            # A notice failure must not break the chat request.
            pass

    async def _send_auto_enroll_prompt(self, event: Any, identity: Any) -> None:
        key = (identity.platform, identity.tenant_id, identity.subject_id)
        if key in self._prompted:
            return
        self._prompted.add(key)
        try:
            await event.send(event.plain_result(AUTO_ENROLL_PROMPT))
        except Exception:
            # A prompt failure must not break the chat request.
            pass

    async def context_for(self, event: Any) -> str | None:
        try:
            identity = self._identity.resolve_identity(event)
        except AuthorizationDenied:
            # Group chats and incomplete identities are expected non-targets.
            return None
        scope = self._scopes.find(identity)
        if scope is None:
            if not self._auto_enroll:
                await self._send_auto_enroll_prompt(event, identity)
                return None
            scope = self._scopes.resolve(identity)
        if not self._scopes.is_enabled(scope):
            return None
        if not self._consent.is_granted(scope, PersonalConsentType.PERSONAL_MEMORY):
            if not self._auto_enroll:
                await self._send_auto_enroll_prompt(event, identity)
                return None
            try:
                enrolled = self._consent.ensure_auto_enrolled(
                    scope, PersonalConsentType.PERSONAL_MEMORY,
                    operator_id=identity.subject_id,
                )
            except AuthorizationDenied:
                return None
            if not enrolled:
                return None
            await self._send_auto_enroll_notice(event)
        context = await self._composer.compose(event.message_str, scope)
        return context.content if context is not None else None
