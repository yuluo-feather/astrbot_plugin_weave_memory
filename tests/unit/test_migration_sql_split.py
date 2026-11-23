from __future__ import annotations

from pathlib import Path

from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    _split_sql_statements,
)


def test_split_sql_statements_ignores_semicolons_in_strings_and_comments():
    script = """
        -- line comment with ; semicolon
        CREATE TABLE t (value TEXT DEFAULT 'a;b');
        /* block comment ; here */
        INSERT INTO t(value) VALUES ('c;d');
        INSERT INTO t(value) VALUES ('it''s;ok');
    """
    statements = _split_sql_statements(script)
    assert len(statements) == 3
    assert "DEFAULT 'a;b'" in statements[0]
    assert statements[1].endswith("VALUES ('c;d')")
    assert "it''s;ok" in statements[2]


def test_split_sql_statements_handles_multiple_statements_per_line():
    statements = _split_sql_statements(
        "CREATE TABLE a(x); CREATE TABLE b(y);",
    )
    assert statements == ["CREATE TABLE a(x)", "CREATE TABLE b(y)"]


def test_all_migration_files_split_into_statements():
    root = Path(__file__).resolve().parents[2] / "adapters" / "sqlite" / "migrations"
    for path in sorted(root.glob("*.sql")):
        statements = _split_sql_statements(path.read_text(encoding="utf-8"))
        assert statements, path.name
        assert all(statement.strip() for statement in statements)
