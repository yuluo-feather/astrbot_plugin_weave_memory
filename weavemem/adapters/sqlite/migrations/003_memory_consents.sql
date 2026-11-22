CREATE TABLE memory_consents (
    id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    scope_id TEXT NOT NULL REFERENCES memory_scopes(id) ON DELETE RESTRICT,
    consent_type TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('GRANTED','REVOKED')),
    source TEXT NOT NULL,
    granted_at TEXT,
    revoked_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    CHECK ((state = 'GRANTED' AND granted_at IS NOT NULL AND revoked_at IS NULL) OR
           (state = 'REVOKED' AND revoked_at IS NOT NULL))
);

CREATE INDEX ix_memory_consents_current
ON memory_consents(scope_id, subject_id, consent_type, granted_at, revoked_at);
