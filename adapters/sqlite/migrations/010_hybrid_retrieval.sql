-- V2.0 schema v10: soft-delete columns and derived vector-index tables.
-- Derived tables (memory_embeddings/index_jobs/index_state) are rebuildable and
-- are not part of authoritative backups. provider_call_audit stores no body.

ALTER TABLE memories ADD COLUMN archived_at TEXT;
ALTER TABLE memories ADD COLUMN purge_after TEXT;
ALTER TABLE memories ADD COLUMN archive_reason TEXT
    CHECK (archive_reason IN ('USER_ARCHIVE','CANDIDATE_EXPIRED','LIFECYCLE'));

-- Backfill only rows that were already ARCHIVED. V1.8 had no user archive
-- command; every production ARCHIVED row came from candidate TTL expiry, so
-- classify them as CANDIDATE_EXPIRED (not restorable). Never touch ACTIVE rows.
UPDATE memories
SET archived_at = updated_at,
    archive_reason = 'CANDIDATE_EXPIRED',
    purge_after = strftime(
        '%Y-%m-%dT%H:%M:%f',
        updated_at,
        '+30 days'
    ) || '000Z'
WHERE status = 'ARCHIVED';

CREATE INDEX IF NOT EXISTS ix_memories_archive_purge ON memories(status, purge_after);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id TEXT PRIMARY KEY,
    db_lineage_id TEXT NOT NULL,
    creation_seq INTEGER NOT NULL CHECK (creation_seq >= 1),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL CHECK (dim >= 1),
    content_hash TEXT NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_memory_embeddings_lineage
    ON memory_embeddings(db_lineage_id, content_hash);

CREATE TABLE IF NOT EXISTS index_jobs (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    target TEXT NOT NULL CHECK (target = 'VECTOR'),
    operation TEXT NOT NULL CHECK (operation IN ('UPSERT','DELETE')),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING','RUNNING','DONE','FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (memory_id, target, operation, content_hash)
);

CREATE INDEX IF NOT EXISTS ix_index_jobs_status ON index_jobs(status, target);

CREATE TABLE IF NOT EXISTS index_state (
    target TEXT PRIMARY KEY,
    dim INTEGER,
    config_hash TEXT,
    status TEXT NOT NULL CHECK (status IN ('HEALTHY','STALE','REBUILDING','FAILED')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_call_audit (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id_hash TEXT NOT NULL,
    field_categories TEXT NOT NULL DEFAULT '[]',
    result TEXT NOT NULL,
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_provider_call_audit_created
    ON provider_call_audit(created_at);
