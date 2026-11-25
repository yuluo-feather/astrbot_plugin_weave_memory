-- V2.1 schema v11: WebUI read/write indexes only (ADR-0019 / ADR-0032).
-- This migration carries no business semantics; it may be emptied when no
-- index change is required, but the version row must be registered.

CREATE INDEX IF NOT EXISTS ix_memories_scope_status_archived
    ON memories(scope_id, status, archived_at);

CREATE INDEX IF NOT EXISTS ix_memory_operations_scope_time
    ON memory_operations(scope_id, occurred_at);

CREATE INDEX IF NOT EXISTS ix_memory_operations_memory_time
    ON memory_operations(memory_id, occurred_at);
