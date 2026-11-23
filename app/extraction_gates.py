"""V1.5 layered extraction consent gates (ADR-0015)."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.model import Scope
from ..core.ports import ScopeStore
from .consent_service import (
    ConsentService,
    ExtractionConsentType,
    PersonalConsentType,
)


@dataclass(frozen=True, slots=True)
class ExtractionGates:
    global_enabled: bool
    scope_enabled: bool
    personal_memory: bool
    capture_consent: bool
    auto_extract_consent: bool
    external_model_consent: bool
    store_candidate_consent: bool
    sensitive_categories: tuple[str, ...]

    @property
    def can_capture(self) -> bool:
        return (
            self.global_enabled
            and self.scope_enabled
            and self.personal_memory
            and self.capture_consent
        )

    @property
    def can_extract(self) -> bool:
        # external_model is an effective-condition dependency: without it the
        # auto_extract consent record stays untouched but extraction must not run.
        return self.can_capture and self.auto_extract_consent and self.external_model_consent

    @property
    def can_store(self) -> bool:
        return self.can_extract and self.store_candidate_consent

    def missing(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.global_enabled:
            missing.append("extraction_enabled")
        if not self.scope_enabled:
            missing.append("scope_enabled")
        if not self.personal_memory:
            missing.append("personal_memory")
        if not self.capture_consent:
            missing.append("extraction_capture")
        if not self.auto_extract_consent:
            missing.append("extraction_auto_extract")
        if not self.external_model_consent:
            missing.append("extraction_external_model")
        if not self.store_candidate_consent:
            missing.append("extraction_store_candidate")
        return tuple(missing)


def resolve_gates(
    scope: Scope, consent: ConsentService, scopes: ScopeStore, *,
    global_enabled: bool,
) -> ExtractionGates:
    return ExtractionGates(
        global_enabled=global_enabled,
        scope_enabled=scopes.is_scope_enabled(scope.id),
        personal_memory=consent.is_granted(scope, PersonalConsentType.PERSONAL_MEMORY),
        capture_consent=consent.is_granted(scope, ExtractionConsentType.CAPTURE),
        auto_extract_consent=consent.is_granted(scope, ExtractionConsentType.AUTO_EXTRACT),
        external_model_consent=consent.is_granted(scope, ExtractionConsentType.EXTERNAL_MODEL),
        store_candidate_consent=consent.is_granted(scope, ExtractionConsentType.STORE_CANDIDATE),
        sensitive_categories=consent.sensitive_categories(scope),
    )
