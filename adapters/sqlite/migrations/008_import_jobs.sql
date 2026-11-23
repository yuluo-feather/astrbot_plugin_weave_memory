CREATE TABLE import_jobs (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES memory_scopes(id) ON DELETE CASCADE,
    subject_id TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'DONE', 'FAILED', 'SKIPPED')),
    cursor INTEGER NOT NULL DEFAULT 0 CHECK (cursor >= 0),
    created_memory_ids_json TEXT NOT NULL DEFAULT '[]',
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX ix_import_jobs_lookup ON import_jobs(scope_id, subject_id, file_sha256, created_at);
CREATE INDEX ix_import_jobs_status ON import_jobs(status, created_at);
CREATE INDEX ix_memory_sources_evidence_ref ON memory_sources(evidence_ref);
