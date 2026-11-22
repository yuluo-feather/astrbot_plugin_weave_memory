CREATE TABLE memory_scopes (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type = 'PERSONAL'),
    scope_external_id TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK (subject_type = 'USER'),
    subject_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (platform, tenant_id, scope_type, scope_external_id, subject_type, subject_id)
);

CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES memory_scopes(id) ON DELETE RESTRICT,
    subject_id TEXT NOT NULL,
    memory_type TEXT NOT NULL CHECK (memory_type IN ('PREFERENCE','FACT','EVENT')),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    normalized_content TEXT NOT NULL CHECK (length(normalized_content) > 0),
    normalization_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (status IN ('CANDIDATE','ACTIVE','SUPERSEDED','ARCHIVED','EXPIRED','DELETED','REJECTED')),
    source_type TEXT NOT NULL CHECK (source_type IN ('EXPLICIT','INFERRED','OBSERVED','IMPORTED','MANUAL','AGENT')),
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    valid_from TEXT,
    valid_until TEXT,
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    CHECK ((status = 'SUPERSEDED' AND superseded_by IS NOT NULL) OR
           (status <> 'SUPERSEDED' AND superseded_by IS NULL))
);

CREATE UNIQUE INDEX uq_memories_active_dedup
ON memories(scope_id, subject_id, memory_type, normalized_content)
WHERE status IN ('ACTIVE','CANDIDATE');
CREATE INDEX ix_memories_scope_status ON memories(scope_id, subject_id, status);

CREATE TABLE memory_revisions (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(memory_id, revision)
);

CREATE TABLE memory_sources (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
    source_type TEXT NOT NULL CHECK (source_type IN ('EXPLICIT','INFERRED','OBSERVED','IMPORTED','MANUAL','AGENT')),
    operator_type TEXT NOT NULL CHECK (operator_type IN ('user','agent','admin','system')),
    operator_id TEXT NOT NULL,
    evidence_ref TEXT,
    captured_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE memory_tags (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
    tag TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(memory_id, tag)
);

CREATE TABLE memory_operations (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES memory_scopes(id) ON DELETE RESTRICT,
    memory_id TEXT,
    operation_type TEXT NOT NULL,
    operator_type TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE memory_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO memory_meta(key, value) VALUES ('fts_dirty_ids', '[]');

CREATE VIRTUAL TABLE memory_fts USING fts5(
    memory_id UNINDEXED,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);
