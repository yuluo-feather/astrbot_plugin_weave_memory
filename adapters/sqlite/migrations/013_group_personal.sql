-- V2.4 schema v13: Group personal policies, sensitivity decisions and the
-- Dashboard username -> platform member binding used by the write console.
-- v12 stays unassigned (V2.3 not scheduled); ADR-0032 records the jump.

CREATE TABLE IF NOT EXISTS group_personal_policies (
    scope_id TEXT PRIMARY KEY REFERENCES memory_scopes(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_sensitivity (
    memory_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    source TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_memory_sensitivity_category
    ON memory_sensitivity(category);

CREATE TABLE IF NOT EXISTS webui_identity_bindings (
    username TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    last_used_at TEXT,
    UNIQUE (platform, tenant_id, subject_id)
);

CREATE TABLE IF NOT EXISTS webui_binding_codes (
    code TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_webui_binding_codes_expiry
    ON webui_binding_codes(expires_at);
