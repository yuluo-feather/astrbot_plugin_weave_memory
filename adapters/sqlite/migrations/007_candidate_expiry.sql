ALTER TABLE memories
ADD COLUMN candidate_expires_at TEXT;

CREATE INDEX ix_memories_candidate_expiry
ON memories(status, candidate_expires_at);

UPDATE memories
SET candidate_expires_at = strftime('%Y-%m-%dT%H:%M:%f', created_at, '+7 days') || 'Z'
WHERE status = 'CANDIDATE';
