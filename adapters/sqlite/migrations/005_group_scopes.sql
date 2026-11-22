CREATE TABLE memory_scopes_v5 (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('PERSONAL','GROUP')),
    scope_external_id TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('USER','GROUP')),
    subject_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    disabled_at TEXT,
    admin_ids_json TEXT NOT NULL DEFAULT '[]',
    admin_source TEXT NOT NULL DEFAULT 'NONE'
        CHECK (admin_source IN ('PLATFORM_ROLE','EXPLICIT_CONFIG','NONE')),
    UNIQUE (platform, tenant_id, scope_type, scope_external_id, subject_type, subject_id)
);

INSERT INTO memory_scopes_v5
    (id, platform, tenant_id, scope_type, scope_external_id, subject_type, subject_id,
     created_at, is_enabled, disabled_at, admin_ids_json, admin_source)
SELECT id, platform, tenant_id, scope_type, scope_external_id, subject_type, subject_id,
       created_at, is_enabled, disabled_at, '[]', 'NONE'
FROM memory_scopes;

DROP TABLE memory_scopes;

ALTER TABLE memory_scopes_v5 RENAME TO memory_scopes;
