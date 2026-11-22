ALTER TABLE memory_scopes
ADD COLUMN is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1));

ALTER TABLE memory_scopes
ADD COLUMN disabled_at TEXT;
