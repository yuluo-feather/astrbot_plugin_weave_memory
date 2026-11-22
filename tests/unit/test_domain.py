from dataclasses import replace
from datetime import timedelta

import pytest
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    AuthorizationDenied,
    InvalidTransition,
    ValidationError,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    ConsentState,
    Identity,
    MemoryConsent,
    MemoryStatus,
    OperatorType,
    Scope,
    ScopeType,
    SubjectType,
)
from data.plugins.astrbot_plugin_weave_memory.core.normalization import (
    normalize_content,
)
from data.plugins.astrbot_plugin_weave_memory.core.policy.consent import ConsentPolicy
from data.plugins.astrbot_plugin_weave_memory.core.policy.dedup import (
    DedupKey,
    classify_duplicate,
)
from data.plugins.astrbot_plugin_weave_memory.core.policy.lifecycle import (
    archive_expired_candidate,
    expire_if_due,
    transition,
)

from tests.conftest import NOW


def test_normalization_nfkc_whitespace_case_and_terminal_punctuation():
    assert normalize_content("  ＡＢＣ\t喜欢  咖啡。  ") == "abc 喜欢 咖啡"


def test_memory_rejects_naive_datetime(memory_factory):
    m = memory_factory()
    with pytest.raises(ValidationError):
        replace(m, updated_at=m.updated_at.replace(tzinfo=None))


def test_personal_scope_requires_user_subject():
    with pytest.raises(ValidationError):
        Identity("astrbot", "default", ScopeType.PERSONAL, "u1", SubjectType.GROUP, "g1")


def test_scope_allows_group_and_rejects_project_session():
    identity = Identity("astrbot", "default", ScopeType.GROUP, "g1", SubjectType.GROUP, "g1")
    scope = Scope("s1", identity, NOW)
    assert scope.identity.scope_type is ScopeType.GROUP
    project = Identity("astrbot", "default", ScopeType.PROJECT, "p1", SubjectType.PROJECT, "p1")
    with pytest.raises(ValidationError):
        Scope("s2", project, NOW)


def test_candidate_workflow_expiry_archives_without_using_semantic_valid_until(memory_factory):
    candidate = memory_factory(status=MemoryStatus.CANDIDATE)
    candidate = replace(candidate, valid_until=NOW + timedelta(days=30))
    not_due = archive_expired_candidate(candidate, candidate_expires_at=NOW + timedelta(days=1),
                                        at=NOW + timedelta(hours=1))
    assert not_due.status is MemoryStatus.CANDIDATE
    archived = archive_expired_candidate(candidate, candidate_expires_at=NOW + timedelta(seconds=1),
                                         at=NOW + timedelta(seconds=2))
    assert archived.status is MemoryStatus.ARCHIVED
    assert archived.valid_until == candidate.valid_until


def test_lifecycle_active_expiry_expires(memory_factory):
    active = replace(memory_factory(), valid_until=NOW + timedelta(seconds=1))
    assert expire_if_due(active, at=NOW + timedelta(seconds=2)).status is MemoryStatus.EXPIRED


def test_deleted_is_terminal(memory_factory):
    deleted = transition(memory_factory(), MemoryStatus.DELETED, at=NOW)
    with pytest.raises(InvalidTransition):
        transition(deleted, MemoryStatus.ACTIVE, at=NOW)


def test_superseded_requires_replacement(memory_factory):
    with pytest.raises(InvalidTransition):
        transition(memory_factory(), MemoryStatus.SUPERSEDED, at=NOW)
    changed = transition(memory_factory(), MemoryStatus.SUPERSEDED, at=NOW, superseded_by="m-2")
    assert changed.superseded_by == "m-2"
    assert changed.current_revision == 2
    with pytest.raises(InvalidTransition):
        transition(changed, MemoryStatus.ACTIVE, at=NOW)
    restored = transition(changed, MemoryStatus.ACTIVE, at=NOW, operator_type=OperatorType.USER)
    assert restored.status is MemoryStatus.ACTIVE


def test_dedup_key_scope_isolation(memory_factory):
    a = memory_factory(scope_id="scope-a")
    b = memory_factory(id="m-2", scope_id="scope-b")
    assert DedupKey.from_memory(a) != DedupKey.from_memory(b)
    assert classify_duplicate(a, b) == "DISTINCT"


def test_consent_is_fail_closed_and_revocation_wins():
    policy = ConsentPolicy()
    with pytest.raises(AuthorizationDenied):
        policy.require(subject_id="u1", scope_id="s1", consent_type="STORE")
    grant = MemoryConsent("c1", "u1", "s1", "STORE", ConsentState.GRANTED,
                          "explicit", granted_at=NOW)
    ConsentPolicy([grant]).require(subject_id="u1", scope_id="s1", consent_type="STORE")
    revoke = MemoryConsent("c2", "u1", "s1", "STORE", ConsentState.REVOKED,
                           "explicit", granted_at=NOW, revoked_at=NOW + timedelta(seconds=1))
    with pytest.raises(AuthorizationDenied):
        ConsentPolicy([grant, revoke]).require(subject_id="u1", scope_id="s1", consent_type="STORE")
