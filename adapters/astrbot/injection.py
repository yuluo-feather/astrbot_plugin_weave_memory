"""Private-message identity and memory-context selection for the LLM hook."""

from typing import Any

from ...app.consent_service import ConsentService, PersonalConsentType
from ...app.recall_service import MemoryContextComposer
from ...app.scope_service import PersonalScopeService
from ...core.errors import AuthorizationDenied
from .identity import AstrBotIdentityAdapter

_AUTO_ENROLL_NOTICE = (
    "织忆已默认开启：只记录你明确提到的长期偏好、背景或目标，并在对话中按需参考。"
    "发送 /织忆 关闭 可停止，/织忆 清空 可删除。"
)


class AstrBotMemoryInjector:
    def __init__(self, scopes: PersonalScopeService,
                 consent: ConsentService,
                 composer: MemoryContextComposer, *,
                 auto_enroll: bool = True) -> None:
        self._scopes = scopes
        self._consent = consent
        self._composer = composer
        self._auto_enroll = auto_enroll
        self._identity = AstrBotIdentityAdapter()

    async def _send_auto_enroll_notice(self, event: Any) -> None:
        try:
            await event.send(event.plain_result(_AUTO_ENROLL_NOTICE))
        except Exception:
            # A notice failure must not break the chat request.
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
                return None
            scope = self._scopes.resolve(identity)
        if not self._scopes.is_enabled(scope):
            return None
        if not self._consent.is_granted(scope, PersonalConsentType.PERSONAL_MEMORY):
            if not self._auto_enroll:
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
