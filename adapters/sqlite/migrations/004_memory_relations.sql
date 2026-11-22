CREATE TABLE memory_relations (
    id TEXT PRIMARY KEY,
    source_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
    target_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
    relation_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    CHECK (source_memory_id <> target_memory_id),
    UNIQUE (source_memory_id, target_memory_id, relation_type)
);

CREATE INDEX ix_memory_relations_source ON memory_relations(source_memory_id);
CREATE INDEX ix_memory_relations_target ON memory_relations(target_memory_id);
