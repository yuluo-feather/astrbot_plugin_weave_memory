from datetime import UTC, datetime, timedelta

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.consents import (
    SQLiteConsentStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    AuthorizationDenied,
    Conflict,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    ConsentState,
    Identity,
    MemoryConsent,
    OperatorType,
    Scope,
    ScopeType,
    SubjectType,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


@pytest.fixture
def consent_stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "consent.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    scope = Scope("scope-1", Identity("astrbot", "platform-instance-1",
                                      ScopeType.PERSONAL, "user-1", SubjectType.USER,
                                      "user-1"), NOW)
    store.create_scope(scope)
    consent_store = SQLiteConsentStore(db)
    yield db, store, consent_store, scope
    db.close()


def consent(consent_id, scope, state, *, at=NOW):
    if state is ConsentState.GRANTED:
        return MemoryConsent(consent_id, "user-1", scope.id, "personal_memory",
                             state, "explicit_user_command", granted_at=at)
    return MemoryConsent(consent_id, "user-1", scope.id, "personal_memory",
                         state, "explicit_user_command", revoked_at=at)


def test_consent_defaults_to_denied_and_grant_is_persisted(consent_stack):
    _, _, repo, scope = consent_stack
    assert repo.current(subject_id="user-1", scope_id=scope.id,
                        consent_type="personal_memory") is None
    assert not repo.is_granted(subject_id="user-1", scope_id=scope.id,
                               consent_type="personal_memory")
    repo.record(consent("c1", scope, ConsentState.GRANTED))
    assert repo.is_granted(subject_id="user-1", scope_id=scope.id,
                           consent_type="personal_memory")
    assert len(repo.history(subject_id="user-1", scope_id=scope.id,
                            consent_type="personal_memory")) == 1


def test_latest_revocation_wins_and_regrant_restores(consent_stack):
    _, _, repo, scope = consent_stack
    repo.record(consent("c1", scope, ConsentState.GRANTED))
    repo.record(consent("c2", scope, ConsentState.REVOKED,
                        at=NOW + timedelta(seconds=1)))
    assert not repo.is_granted(subject_id="user-1", scope_id=scope.id,
                               consent_type="personal_memory")
    repo.record(consent("c3", scope, ConsentState.GRANTED,
                        at=NOW + timedelta(seconds=2)))
    assert repo.is_granted(subject_id="user-1", scope_id=scope.id,
                           consent_type="personal_memory")


def test_same_timestamp_follows_append_order(consent_stack):
    _, _, repo, scope = consent_stack
    repo.record(consent("c1", scope, ConsentState.GRANTED))
    repo.record(consent("c2", scope, ConsentState.REVOKED))
    assert not repo.is_granted(subject_id="user-1", scope_id=scope.id,
                               consent_type="personal_memory")
    # A later grant in the same microsecond must win: order, not timestamp.
    repo.record(consent("c3", scope, ConsentState.GRANTED))
    assert repo.is_granted(subject_id="user-1", scope_id=scope.id,
                           consent_type="personal_memory")


def test_consent_idempotency_and_scope_subject_binding(consent_stack):
    _, _, repo, scope = consent_stack
    grant = consent("c1", scope, ConsentState.GRANTED)
    repo.record(grant)
    repo.record(grant)
    with pytest.raises(Conflict):
        repo.record(consent("c1", scope, ConsentState.REVOKED))
    wrong = MemoryConsent("c2", "someone-else", scope.id, "personal_memory",
                          ConsentState.GRANTED, "explicit", granted_at=NOW)
    with pytest.raises(Conflict):
        repo.record(wrong)


def test_disabled_scope_cannot_be_granted_consent_but_can_be_revoked(consent_stack):
    _, store, repo, scope = consent_stack
    store.disable_scope(scope.id, operation_id="op-disable", operator_type=OperatorType.USER,
                        operator_id="user-1", occurred_at=NOW)
    with pytest.raises(AuthorizationDenied):
        repo.record(consent("c1", scope, ConsentState.GRANTED))
    repo.record(consent("c2", scope, ConsentState.REVOKED))
    assert not repo.is_granted(subject_id="user-1", scope_id=scope.id,
                               consent_type="personal_memory")
