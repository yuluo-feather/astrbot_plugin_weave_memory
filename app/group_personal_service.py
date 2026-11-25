"""V2.4 Group personal consent, policy epochs and sensitivity rules.

Group-level state lives in ``group_personal_policies`` (enabled + epoch); member
opt-ins stay in the append-only ``memory_consents`` ledger with the member as
subject and the policy epoch in metadata. Re-enabling a group bumps the epoch,
so earlier opt-ins never come back silently (ADR-0032).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..core.errors import AuthorizationDenied
from ..core.model import Identity, Scope, ScopeType
from ..core.ports import GroupPersonalPolicyStore
from .consent_service import ConsentService, PersonalConsentType
from .scope_service import GroupScopeService, PersonalScopeService

GROUP_PERSONAL_MEMBER = "group_personal_member"

SAFE = "SAFE"
UNKNOWN = "UNKNOWN"


def group_personal_consent_type(identity: Identity) -> str:
    """The group scope ID is the group dimension; subject_id is the member."""
    if identity.scope_type is not ScopeType.GROUP:
        raise AuthorizationDenied("group personal consent requires a group identity")
    return GROUP_PERSONAL_MEMBER


class GroupPersonalService:
    def __init__(
        self,
        group_scopes: GroupScopeService,
        personal_scopes: PersonalScopeService,
        consent: ConsentService,
        policies: GroupPersonalPolicyStore,
        *,
        global_enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        self._group_scopes = group_scopes
        self._personal_scopes = personal_scopes
        self._consent = consent
        self._policies = policies
        self._global_enabled = global_enabled

    def set_group_enabled(
        self, identity: Identity, *, operator_id: str, decision: Any,
        enabled: bool,
    ) -> bool:
        if not decision.allowed:
            raise AuthorizationDenied("group personal switch requires a group administrator")
        scope = self._group_scopes.ensure_scope(identity, decision)
        self._policies.set_enabled(scope.id, enabled=enabled, operator_id=operator_id)
        return enabled

    def set_user_opt_in(
        self, group_identity: Identity, personal_identity: Identity, *,
        operator_id: str, enabled: bool,
    ) -> bool:
        if personal_identity.scope_type is not ScopeType.PERSONAL:
            raise AuthorizationDenied("group personal opt-in requires a personal identity")
        if personal_identity.subject_id != operator_id:
            raise AuthorizationDenied("group personal opt-in must be performed by the user")
        group_scope = self._group_scopes.find(group_identity)
        if group_scope is None:
            if enabled:
                raise AuthorizationDenied("a group administrator must enable Group personal first")
            return False
        policy = self._policies.get_policy(group_scope.id)
        if enabled:
            if policy is None or not policy["enabled"]:
                raise AuthorizationDenied("a group administrator must enable Group personal first")
            personal_scope = self._personal_scopes.find(personal_identity)
            if personal_scope is None or not self._personal_scopes.is_enabled(personal_scope):
                raise AuthorizationDenied(
                    "personal memory scope is not enabled; authorize personal memory in private chat first",
                )
            if not self._consent.is_granted(personal_scope, PersonalConsentType.PERSONAL_MEMORY):
                raise AuthorizationDenied("personal memory is not authorized")
            self._consent.grant(
                group_scope, group_personal_consent_type(group_identity),
                operator_id=operator_id, subject_id=personal_identity.subject_id,
                source="explicit_group_personal_opt_in",
                metadata={"member_opt_in": True, "epoch": int(policy["epoch"])},
                allow_disabled_scope=True,
            )
        else:
            self._consent.revoke(
                group_scope, group_personal_consent_type(group_identity),
                operator_id=operator_id, subject_id=personal_identity.subject_id,
                source="explicit_group_personal_opt_out",
            )
        return enabled

    def group_enabled(self, identity: Identity) -> bool:
        if not self._global_enabled():
            return False
        scope = self._group_scopes.find(identity)
        if scope is None:
            return False
        policy = self._policies.get_policy(scope.id)
        return bool(policy is not None and policy["enabled"])

    def user_opted_in(self, group_identity: Identity, personal_identity: Identity) -> bool:
        group_scope = self._group_scopes.find(group_identity)
        personal_scope = self._personal_scopes.find(personal_identity)
        if group_scope is None or personal_scope is None:
            return False
        policy = self._policies.get_policy(group_scope.id)
        if policy is None or not policy["enabled"]:
            return False
        if not self._personal_scopes.is_enabled(personal_scope):
            return False
        if not self._consent.is_granted(personal_scope, PersonalConsentType.PERSONAL_MEMORY):
            return False
        consent = self._consent.current(
            group_scope, group_personal_consent_type(group_identity),
            subject_id=personal_identity.subject_id,
        )
        if consent is None or not self._consent.is_granted(
            group_scope, group_personal_consent_type(group_identity),
            subject_id=personal_identity.subject_id,
        ):
            return False
        return int(consent.metadata.get("epoch", -1)) == int(policy["epoch"])

    def eligible_personal_scope(
        self, group_identity: Identity, personal_identity: Identity,
    ) -> Scope | None:
        if not self.group_enabled(group_identity):
            return None
        if not self.user_opted_in(group_identity, personal_identity):
            return None
        return self._personal_scopes.find(personal_identity)

    def status(self, group_identity: Identity, personal_identity: Identity) -> dict[str, bool]:
        return {
            "group_enabled": self.group_enabled(group_identity),
            "user_opted_in": self.user_opted_in(group_identity, personal_identity),
            "personal_memory_enabled": self._personal_memory_enabled(personal_identity),
        }

    def _personal_memory_enabled(self, identity: Identity) -> bool:
        scope = self._personal_scopes.find(identity)
        return bool(
            scope is not None
            and self._personal_scopes.is_enabled(scope)
            and self._consent.is_granted(scope, PersonalConsentType.PERSONAL_MEMORY)
        )


# Conservative group-surface exclusions. These are deliberately separate from
# V1.5 extraction's configurable sensitive allow-list: Group personal never
# inherits an extraction whitelist.
_SENSITIVE_GROUP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("credential", re.compile(r"(?i)(?:密码|口令|令牌|密钥|password|passwd|secret|token|api[\s_-]?key)\s*[:=：]?\s*\S+")),
    ("address", re.compile(r"[一-龥]{1,12}(?:省|市|自治区)[一-龥\d]{0,24}(?:区|县|路|街|巷|号|弄|栋|楼|室)")),
    ("identity", re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])")),
    ("financial", re.compile(r"(?i)(?:银行卡|银行账户|卡号|信用卡|借款|贷款|债务|收入|工资|存款|资产|投资金额|账户余额|bank account|credit card|salary|income|debt)")),
    ("health", re.compile(r"(?i)(?:诊断|病史|疾病|患有|服药|药物|处方|治疗方案|检查结果|住院|手术|抑郁症|焦虑症|糖尿病|高血压|medical history|diagnosis|medication|prescription|treatment|hospitalized)")),
)


def group_personal_sensitive_categories(content: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in _SENSITIVE_GROUP_PATTERNS if pattern.search(content))


def classify_sensitivity(content: str) -> tuple[str, str]:
    """Return (category, source). Rule-based; UNKNOWN means fail closed.

    A memory that matches no rule is SAFE *by rule*, which is what the plan
    calls a rule classification. If classification itself fails the caller gets
    UNKNOWN and must not inject.
    """
    try:
        categories = group_personal_sensitive_categories(content)
    except Exception:  # noqa: BLE001 - classification must fail closed
        return UNKNOWN, "RULE"
    if categories:
        return categories[0], "RULE"
    return SAFE, "RULE"


def is_sensitive(category: str) -> bool:
    return category not in {SAFE, ""}
