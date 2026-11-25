import asyncio
from datetime import timedelta

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
    SQLiteIdentityBindingStore,
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
from data.plugins.astrbot_plugin_weave_memory.app.group_memory_service import (
    GroupMemoryService,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_personal_service import (
    GroupPersonalService,
)
from data.plugins.astrbot_plugin_weave_memory.app.identity_binding_service import (
    IdentityBindingService,
)
from data.plugins.astrbot_plugin_weave_memory.app.ledger_service import LedgerService
from data.plugins.astrbot_plugin_weave_memory.app.lifecycle_service import (
    LifecycleService,
)
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
    Identity,
    MemoryStatus,
    ScopeType,
    SubjectType,
)


class Admins:
    def resolve(self, identity, sender_id, event=None):
        return AdminDecision(
            AdminSource.EXPLICIT_CONFIG, ("admin",), sender_id == "admin", True,
        )


class NonAdmins:
    def resolve(self, identity, sender_id, event=None):
        return AdminDecision(AdminSource.NONE, (), False, False)


def group_identity(group="g-1"):
    return Identity("astrbot", "bot-1", ScopeType.GROUP, group, SubjectType.GROUP, group)


def personal_identity(user="u-1"):
    return Identity("astrbot", "bot-1", ScopeType.PERSONAL, user, SubjectType.USER, user)


def build_stack(tmp_path, admins=None):
    db = SQLiteDatabase(tmp_path / "webui.sqlite3")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    clock = SystemClock()
    ids = UUID4IdGenerator()
    ledger_store = SQLiteLedger(tmp_path / "memory_ledger.sqlite3")
    ledger = LedgerService(ledger_store, clock, ids, enabled=True)
    coordinator = WriteCoordinator(store, index, ledger)
    consent = ConsentService(SQLiteConsentStore(db), coordinator, clock, ids)
    group_scopes = GroupScopeService(store, admins or Admins(), coordinator, clock)
    personal_scopes = PersonalScopeService(store, coordinator, clock)
    lifecycle = LifecycleService(coordinator, store, consent)
    group_lifecycle = GroupLifecycleService(
        store, coordinator, consent, index, clock, ids, tmp_path / "group-backups",
        ledger=ledger,
    )
    group_memory = GroupMemoryService(
        store, store, coordinator, consent, clock, ids, lifecycle=lifecycle,
    )
    policies = SQLiteGroupPersonalPolicyStore(db, clock)
    group_personal = GroupPersonalService(
        group_scopes, personal_scopes, consent, policies,
    )
    bindings = SQLiteIdentityBindingStore(db, clock)
    return {
        "db": db, "store": store, "clock": clock, "ledger_store": ledger_store,
        "consent": consent, "group_scopes": group_scopes,
        "personal_scopes": personal_scopes, "lifecycle": lifecycle,
        "group_lifecycle": group_lifecycle, "group_memory": group_memory,
        "policies": policies, "group_personal": group_personal,
        "bindings": bindings,
        "identity_binding": IdentityBindingService(bindings, clock),
    }


@pytest.fixture
def stack(tmp_path):
    built = build_stack(tmp_path)
    yield built
    built["ledger_store"].close()
    built["db"].close()


def prepare_group(stack, group="g-1"):
    identity = group_identity(group)
    decision = stack["group_scopes"].admin_decision(identity, "admin")
    scope = stack["group_scopes"].ensure_scope(identity, decision)
    asyncio.run(stack["group_lifecycle"].enable(scope, decision, operator_id="admin"))
    return scope, decision


def test_group_write_lifecycle_create_edit_archive_restore_purge(stack):
    scope, decision = prepare_group(stack)
    writer = stack["group_memory"]

    created, receipt, audit_id = asyncio.run(writer.create_group(
        scope, "周末去爬山", group_label="话题",
        operator_id="admin", decision=decision,
    ))
    assert receipt.revision == 1
    assert audit_id
    assert created.status is MemoryStatus.ACTIVE

    updated, receipt, edit_audit = asyncio.run(writer.update_group(
        scope, created.id, "周末去爬山，上午九点集合",
        expected_revision=1, operator_id="admin", decision=decision,
    ))
    assert receipt.revision == 2
    assert edit_audit

    with pytest.raises(Conflict):
        asyncio.run(writer.update_group(
            scope, created.id, "再改一次", expected_revision=1,
            operator_id="admin", decision=decision,
        ))

    _memory, archive_audit = asyncio.run(writer.archive_group(
        scope, created.id, operator_id="admin", decision=decision,
    ))
    assert archive_audit
    trail = writer.list_operations(scope, created.id)
    assert trail
    archived, total = writer.list_archived_page(scope)
    assert total == 1 and archived[0].id == created.id

    _memory, restore_audit = asyncio.run(writer.restore_group(
        scope, created.id, operator_id="admin", decision=decision,
    ))
    assert restore_audit
    assert writer.list_archived_page(scope)[1] == 0

    with pytest.raises(Conflict):
        asyncio.run(writer.purge_group(
            scope, created.id, operator_id="admin", decision=decision,
        ))

    asyncio.run(writer.archive_group(
        scope, created.id, operator_id="admin", decision=decision,
    ))
    purge_audit, purged = asyncio.run(writer.purge_group(
        scope, created.id, operator_id="admin", decision=decision,
    ))
    assert purge_audit and purged


def test_group_write_requires_admin_and_records_audit(stack):
    scope, decision = prepare_group(stack)
    writer = stack["group_memory"]
    denied = AdminDecision(AdminSource.NONE, (), False, False)
    with pytest.raises(AuthorizationDenied):
        asyncio.run(writer.create_group(
            scope, "不该写入", group_label="话题",
            operator_id="member", decision=denied,
        ))

    created, _receipt, audit_id = asyncio.run(writer.create_group(
        scope, "群约定：周三晚八点", group_label="约定",
        operator_id="admin", decision=decision,
    ))
    trail = writer.list_operations(scope, created.id)
    assert any(entry["audit_id"] == audit_id for entry in trail)
    assert all("content" not in entry for entry in trail)


def test_group_write_rejected_when_scope_disabled(tmp_path):
    stack = build_stack(tmp_path)
    try:
        identity = group_identity()
        decision = stack["group_scopes"].admin_decision(identity, "admin")
        scope = stack["group_scopes"].ensure_scope(identity, decision)
        with pytest.raises(AuthorizationDenied):
            asyncio.run(stack["group_memory"].create_group(
                scope, "x", group_label="话题",
                operator_id="admin", decision=decision,
            ))
    finally:
        stack["ledger_store"].close()
        stack["db"].close()


def test_reenabling_group_bumps_epoch_and_drops_old_opt_in(stack):
    scope, decision = prepare_group(stack)
    group = group_identity()
    user = personal_identity()
    personal_scope = stack["personal_scopes"].resolve(user)
    stack["consent"].grant(
        personal_scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u-1",
    )
    personal = stack["group_personal"]
    personal.set_group_enabled(group, operator_id="admin", decision=decision, enabled=True)
    personal.set_user_opt_in(group, user, operator_id="u-1", enabled=True)
    assert personal.user_opted_in(group, user)

    personal.set_group_enabled(group, operator_id="admin", decision=decision, enabled=False)
    personal.set_group_enabled(group, operator_id="admin", decision=decision, enabled=True)
    assert not personal.user_opted_in(group, user)
    assert personal.eligible_personal_scope(group, user) is None


def test_global_kill_switch_disables_group_personal(tmp_path):
    stack = build_stack(tmp_path)
    try:
        group = group_identity()
        decision = stack["group_scopes"].admin_decision(group, "admin")
        user = personal_identity()
        personal_scope = stack["personal_scopes"].resolve(user)
        stack["consent"].grant(
            personal_scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u-1",
        )
        policies = stack["policies"]
        enabled = {"value": True}
        personal = GroupPersonalService(
            stack["group_scopes"], stack["personal_scopes"], stack["consent"],
            policies, global_enabled=lambda: enabled["value"],
        )
        personal.set_group_enabled(group, operator_id="admin", decision=decision, enabled=True)
        personal.set_user_opt_in(group, user, operator_id="u-1", enabled=True)
        assert personal.user_opted_in(group, user)
        enabled["value"] = False
        assert not personal.group_enabled(group)
        assert personal.eligible_personal_scope(group, user) is None
    finally:
        stack["ledger_store"].close()
        stack["db"].close()


def test_binding_code_binds_once_and_resolves_identity(stack):
    service = stack["identity_binding"]
    user = personal_identity("member-9")
    code = service.issue_code(user)["code"]
    assert len(code) == 6

    binding = service.bind("dashboard-admin", code)
    assert binding["subject_id"] == "member-9"
    resolved = service.resolve("dashboard-admin")
    assert resolved is not None and resolved.subject_id == "member-9"

    with pytest.raises(AuthorizationDenied):
        service.bind("another-user", code)
    with pytest.raises(AuthorizationDenied):
        service.require("unbound-user")

    service.unbind("dashboard-admin")
    assert service.resolve("dashboard-admin") is None


def test_binding_code_expires(stack):
    service = stack["identity_binding"]
    payload = service.issue_code(personal_identity("member-1"))
    short = IdentityBindingService(
        stack["bindings"], stack["clock"], code_ttl=timedelta(seconds=-1),
    )
    expired = short.issue_code(personal_identity("member-2"))["code"]
    with pytest.raises(AuthorizationDenied):
        service.bind("dashboard-admin", expired)
    assert payload["code"]
