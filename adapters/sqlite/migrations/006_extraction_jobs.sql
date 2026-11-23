CREATE TABLE extraction_jobs (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    scope_id TEXT NOT NULL REFERENCES memory_scopes(id) ON DELETE CASCADE,
    subject_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    watermark INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'RUNNING', 'SUCCEEDED', 'SKIPPED_DUPLICATE_ACTIVE', 'FAILED', 'CANCELLED'
    )),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (platform, tenant_id, scope_id, conversation_id, watermark, content_hash)
);

CREATE INDEX ix_extraction_jobs_status ON extraction_jobs(status, created_at);
CREATE INDEX ix_extraction_jobs_scope ON extraction_jobs(scope_id, subject_id);
