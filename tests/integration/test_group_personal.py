import asyncio

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.consents import (
    SQLiteConsentStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.fts import SQLiteFTSIndex
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.ledger import SQLiteLedger
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.webui_state import (
    SQLiteGroupPersonalPolicyStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.system import (
    SystemClock,
    UUID4IdGenerator,
)
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import (
    ConsentService,
    PersonalConsentType,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_lifecycle_service import (
    GroupLifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_personal_recall_service import (
    GroupPersonalContextComposer,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_personal_service import (
    GroupPersonalService,
    group_personal_consent_type,
)
from data.plugins.astrbot_plugin_weave_memory.app.ledger_service import LedgerService
from data.plugins.astrbot_plugin_weave_memory.app.lifecycle_service import (
    LifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.memory_service import (
    MemoryApplicationService,
)
from data.plugins.astrbot_plugin_weave_memory.app.recall_service import RecallService
from data.plugins.astrbot_plugin_weave_memory.app.scope_service import (
    GroupScopeService,
    PersonalScopeService,
)
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    AuthorizationDenied,
    Conflict,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    AdminDecision,
    AdminSource,
    ConsentState,
    Identity,
    LedgerEventType,
    MemoryConsent,
    MemoryType,
    ScopeType,
    SubjectType,
)


class Admins:
    def resolve(self, identity, sender_id, event=None):
        return AdminDecision(
            AdminSource.EXPLICIT_CONFIG, ("admin",), sender_id == "admin", True,
        )


def group_identity(group="g-1"):
    return Identity("astrbot", "bot-1", ScopeType.GROUP, group, SubjectType.GROUP, group)


def personal_identity(user="u-1"):
    return Identity("astrbot", "bot-1", ScopeType.PERSONAL, user, SubjectType.USER, user)


@pytest.fixture
def stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "group-personal.sqlite3")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    clock = SystemClock()
    ids = UUID4IdGenerator()
    ledger_store = SQLiteLedger(tmp_path / "memory_ledger.sqlite3")
    ledger = LedgerService(ledger_store, clock, ids, enabled=True)
    coordinator = WriteCoordinator(store, index, ledger)
    consent = ConsentService(SQLiteConsentStore(db), coordinator, clock, ids)
    group_scopes = GroupScopeService(store, Admins(), coordinator, SystemClock())
    personal_scopes = PersonalScopeService(store, coordinator, SystemClock())
    group_personal = GroupPersonalService(
        group_scopes, personal_scopes, consent,
        SQLiteGroupPersonalPolicyStore(db, clock),
    )
    lifecycle = LifecycleService(coordinator, store, consent)
    group_lifecycle = GroupLifecycleService(
        store, coordinator, consent, index, clock, ids, tmp_path / "group-backups",
        ledger=ledger,
    )
    memories = MemoryApplicationService(store, coordinator, consent, lifecycle, clock, ids)
    recall = RecallService(store, index, consent, clock, coordinator, store)
    yield locals()
    ledger_store.close()
    db.close()


def test_group_personal_requires_both_group_admin_switch_and_member_opt_in(stack):
    group = group_identity()
    user = personal_identity()
    decision = stack["group_scopes"].admin_decision(group, "admin")
    stack["group_personal"].set_group_enabled(group, operator_id="admin", decision=decision, enabled=True)
    personal_scope = stack["personal_scopes"].resolve(user)
    stack["consent"].grant(personal_scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u-1")
    assert stack["group_personal"].eligible_personal_scope(group, user) is None

    stack["group_personal"].set_user_opt_in(group, user, operator_id="u-1", enabled=True)
    assert stack["group_personal"].eligible_personal_scope(group, user).id == personal_scope.id

    stack["group_personal"].set_group_enabled(group, operator_id="admin", decision=decision, enabled=False)
    assert stack["group_personal"].eligible_personal_scope(group, user) is None


def test_member_cannot_enable_group_personal_and_opt_in_is_group_specific(stack):
    group_a = group_identity("g-a")
    group_b = group_identity("g-b")
    member_decision = stack["group_scopes"].admin_decision(group_a, "member")
    with pytest.raises(AuthorizationDenied):
        stack["group_personal"].set_group_enabled(
            group_a, operator_id="member", decision=member_decision, enabled=True,
        )

    admin_decision = stack["group_scopes"].admin_decision(group_a, "admin")
    stack["group_personal"].set_group_enabled(group_a, operator_id="admin", decision=admin_decision, enabled=True)
    stack["group_personal"].set_group_enabled(group_b, operator_id="admin", decision=stack["group_scopes"].admin_decision(group_b, "admin"), enabled=True)
    user = personal_identity()
    personal_scope = stack["personal_scopes"].resolve(user)
    stack["consent"].grant(personal_scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u-1")
    stack["group_personal"].set_user_opt_in(group_a, user, operator_id="u-1", enabled=True)
    assert stack["group_personal"].user_opted_in(group_a, user)
    assert not stack["group_personal"].user_opted_in(group_b, user)
    assert group_personal_consent_type(group_a) == group_personal_consent_type(group_b)
    assert stack["group_scopes"].find(group_a).id != stack["group_scopes"].find(group_b).id


def test_personal_opt_out_blocks_group_personal_even_if_group_opt_in_remains(stack):
    group = group_identity()
    user = personal_identity()
    decision = stack["group_scopes"].admin_decision(group, "admin")
    stack["group_personal"].set_group_enabled(group, operator_id="admin", decision=decision, enabled=True)
    scope = stack["personal_scopes"].resolve(user)
    stack["consent"].grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u-1")
    stack["group_personal"].set_user_opt_in(group, user, operator_id="u-1", enabled=True)
    stack["consent"].revoke(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u-1")
    assert not stack["group_personal"].user_opted_in(group, user)


def test_group_personal_composer_filters_sensitive_memory(stack):
    group = group_identity()
    user = personal_identity()
    decision = stack["group_scopes"].admin_decision(group, "admin")
    stack["group_personal"].set_group_enabled(group, operator_id="admin", decision=decision, enabled=True)
    scope = stack["personal_scopes"].resolve(user)
    stack["consent"].grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u-1")
    stack["group_personal"].set_user_opt_in(group, user, operator_id="u-1", enabled=True)
    asyncio.run(stack["memories"].save_explicit(scope, "我喜欢黑咖啡。", operator_id="u-1", memory_type=MemoryType.PREFERENCE))
    asyncio.run(stack["memories"].save_explicit(scope, "我的病史是糖尿病，正在服药。", operator_id="u-1", memory_type=MemoryType.FACT))
    composer = GroupPersonalContextComposer(stack["recall"], max_tokens=512)
    context = asyncio.run(composer.compose("喜欢什么饮料", scope))
    assert context is not None
    assert "黑咖啡" in context.content
    assert "糖尿病" not in context.content
    assert "病史" not in context.content


def test_user_cannot_opt_in_when_personal_memory_not_authorized(stack):
    group = group_identity()
    user = personal_identity()
    decision = stack["group_scopes"].admin_decision(group, "admin")
    stack["group_personal"].set_group_enabled(group, operator_id="admin", decision=decision, enabled=True)
    with pytest.raises(AuthorizationDenied):
        stack["group_personal"].set_user_opt_in(group, user, operator_id="u-1", enabled=True)


def test_member_opt_in_is_ledgered_by_group_scope_and_removed_on_group_destroy(stack):
    group = group_identity()
    user = personal_identity()
    decision = stack["group_scopes"].admin_decision(group, "admin")
    stack["group_personal"].set_group_enabled(
        group, operator_id="admin", decision=decision, enabled=True,
    )
    personal_scope = stack["personal_scopes"].resolve(user)
    stack["consent"].grant(personal_scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u-1")
    stack["group_personal"].set_user_opt_in(group, user, operator_id="u-1", enabled=True)
    group_scope = stack["group_scopes"].find(group)
    key = (group_scope.id, "u-1", group_personal_consent_type(group))
    assert key in stack["ledger"].latest_consent_states()

    # The group scope is still disabled (the Group shared switch is independent).
    assert stack["group_personal"].user_opted_in(group, user)
    stack["group_personal"].set_group_enabled(
        group, operator_id="admin", decision=decision, enabled=False,
    )
    receipt = asyncio.run(stack["group_lifecycle"].destroy(
        group_scope, decision, operator_id="admin",
    ))
    assert receipt.affected_count == 0
    assert stack["group_scopes"].find(group) is None
    assert not stack["group_personal"].user_opted_in(group, user)
    # Recreating the same external group cannot resurrect the previous opt-in.
    new_decision = decision
    stack["group_personal"].set_group_enabled(
        group, operator_id="admin", decision=new_decision, enabled=True,
    )
    assert not stack["group_personal"].user_opted_in(group, user)


def test_member_subject_exception_is_limited_to_group_personal_consent(stack):
    group = group_identity()
    decision = stack["group_scopes"].admin_decision(group, "admin")
    group_scope = stack["group_scopes"].ensure_scope(group, decision)
    now = SystemClock().now()
    valid = MemoryConsent(
        id="valid-member-consent", subject_id="u-1", scope_id=group_scope.id,
        consent_type="group_personal_member", state=ConsentState.GRANTED,
        source="test", granted_at=now,
    )
    stack["consent"]._repository.record(valid, allow_disabled_grant=True)

    invalid_type = MemoryConsent(
        id="invalid-type-consent", subject_id="u-2", scope_id=group_scope.id,
        consent_type="personal_memory", state=ConsentState.GRANTED,
        source="test", granted_at=now,
    )
    with pytest.raises(Conflict):
        stack["consent"]._repository.record(invalid_type, allow_disabled_grant=True)

    personal_scope = stack["personal_scopes"].resolve(personal_identity("u-3"))
    invalid_scope = MemoryConsent(
        id="invalid-scope-consent", subject_id="u-3", scope_id=personal_scope.id,
        consent_type="group_personal_member", state=ConsentState.GRANTED,
        source="test", granted_at=now,
    )
    with pytest.raises(Conflict):
        stack["consent"]._repository.record(invalid_scope, allow_disabled_grant=True)


def test_pending_group_member_consent_replays_through_existing_ledger(stack):
    group = group_identity()
    decision = stack["group_scopes"].admin_decision(group, "admin")
    group_scope = stack["group_scopes"].ensure_scope(group, decision)
    now = SystemClock().now()
    consent_type = group_personal_consent_type(group)
    payload = {
        "scope_id": group_scope.id,
        "subject_id": "u-replay",
        "consent_type": consent_type,
        "old_state": None,
        "new_state": ConsentState.GRANTED.value,
        "source": "explicit_group_personal_opt_in",
        "consent_id": "consent-replay",
        "granted_at": now.isoformat(),
        "revoked_at": None,
        "metadata": {"operator_id": "u-replay", "member_opt_in": True},
    }
    stack["ledger"].record_intent(
        LedgerEventType.CONSENT_CHANGE, payload=payload, source="user",
        occurred_at=now, scope_id=group_scope.id, subject_id="u-replay",
    )
    applied, failed = stack["ledger"].drain_pending(
        stack["store"], stack["consent"]._repository,
    )
    assert applied == 1
    assert failed == ()
    assert stack["consent"].is_granted(
        group_scope, consent_type, subject_id="u-replay",
    )
