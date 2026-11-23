INSERT INTO memory_meta(key, value)
SELECT 'db_lineage_id', lower(hex(randomblob(16)))
WHERE NOT EXISTS (SELECT 1 FROM memory_meta WHERE key='db_lineage_id');

CREATE TABLE memories_v9 (
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
    candidate_expires_at TEXT,
    db_lineage_id TEXT NOT NULL,
    creation_seq INTEGER NOT NULL CHECK (creation_seq >= 1),
    CHECK ((status = 'SUPERSEDED' AND superseded_by IS NOT NULL) OR
           (status <> 'SUPERSEDED' AND superseded_by IS NULL)),
    UNIQUE (db_lineage_id, creation_seq)
);

INSERT INTO memories_v9
    (id, scope_id, subject_id, memory_type, content, normalized_content,
     normalization_version, status, source_type, current_revision,
     valid_from, valid_until, superseded_by, created_at, updated_at,
     last_accessed_at, metadata_json, candidate_expires_at,
     db_lineage_id, creation_seq)
SELECT m.id, m.scope_id, m.subject_id, m.memory_type, m.content,
       m.normalized_content, m.normalization_version, m.status, m.source_type,
       m.current_revision, m.valid_from, m.valid_until, m.superseded_by,
       m.created_at, m.updated_at, m.last_accessed_at, m.metadata_json,
       m.candidate_expires_at,
       (SELECT value FROM memory_meta WHERE key='db_lineage_id'),
       ROW_NUMBER() OVER (ORDER BY m.created_at, m.id)
FROM memories m;

DROP TABLE memories;

ALTER TABLE memories_v9 RENAME TO memories;

CREATE UNIQUE INDEX uq_memories_active_dedup
ON memories(scope_id, subject_id, memory_type, normalized_content)
WHERE status IN ('ACTIVE','CANDIDATE');

CREATE INDEX ix_memories_scope_status ON memories(scope_id, subject_id, status);

CREATE INDEX ix_memories_candidate_expiry ON memories(status, candidate_expires_at);

CREATE TABLE IF NOT EXISTS memory_lineage_meta (
    db_lineage_id TEXT PRIMARY KEY,
    high_watermark INTEGER NOT NULL CHECK (high_watermark >= 0)
);

INSERT OR IGNORE INTO memory_lineage_meta(db_lineage_id, high_watermark)
SELECT (SELECT value FROM memory_meta WHERE key='db_lineage_id'),
       COALESCE(MAX(creation_seq), 0)
FROM memories;

UPDATE memory_lineage_meta
SET high_watermark = MAX(
    high_watermark,
    (SELECT COALESCE(MAX(creation_seq), 0) FROM memories
     WHERE db_lineage_id = memory_lineage_meta.db_lineage_id)
)
WHERE db_lineage_id = (SELECT value FROM memory_meta WHERE key='db_lineage_id');
