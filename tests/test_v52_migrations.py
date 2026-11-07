"""Acceptance test for atomic and checksum-verified SQLite migrations."""

import sqlite3

import pytest

from migrations_runner import MigrationChecksumMismatch, MigrationError, apply_migrations


def test_runner_atomic_repeat_and_checksum(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()

    # An active caller-owned transaction must be rejected without being closed
    # or leaving behind a migration registry.
    active_conn = sqlite3.connect(":memory:")
    active_conn.execute("BEGIN")
    with pytest.raises(MigrationError, match="active transaction"):
        apply_migrations(active_conn, migrations, now=100)
    assert active_conn.in_transaction
    assert active_conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()[0] == 0
    active_conn.rollback()
    active_conn.close()

    # Numeric ordering is intentional: 2 must run before 10 even though
    # lexicographic filename ordering would place 10 first.
    first = migrations / "2_first.sql"
    second = migrations / "10_broken.sql"
    first.write_text(
        "CREATE TABLE first_table (id INTEGER PRIMARY KEY);\n"
        "CREATE TRIGGER first_table_guard AFTER INSERT ON first_table\n"
        "BEGIN\n"
        "    SELECT CASE WHEN NEW.id < 0 THEN RAISE(ABORT, 'negative id') END;\n"
        "END;\n",
        encoding="utf-8",
    )
    second.write_text(
        "CREATE TABLE transient_table (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO deliberately_missing_table(id) VALUES (1);\n",
        encoding="utf-8",
    )

    conn = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError, match="deliberately_missing_table"):
        apply_migrations(conn, migrations, now=1234)

    # The first migration committed before the second started. The second
    # migration's first statement and registry row must both have rolled back.
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "first_table" in tables
    assert "transient_table" not in tables
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='first_table_guard'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT version, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall() == [("2_first", 1234)]

    # Remove only the deliberately broken migration so the first can be
    # replayed independently. Identical bytes must be a true no-op.
    second.rename(migrations / "10_broken.sql.off")
    before_registry = conn.execute(
        "SELECT version, checksum, applied_at FROM schema_migrations"
    ).fetchall()
    before_tables = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    assert apply_migrations(conn, migrations, now=9999) == []
    assert conn.execute(
        "SELECT version, checksum, applied_at FROM schema_migrations"
    ).fetchall() == before_registry
    assert conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall() == before_tables

    # A changed migration file is never silently treated as already applied.
    first.write_text(
        "CREATE TABLE first_table (id INTEGER PRIMARY KEY, extra TEXT);\n",
        encoding="utf-8",
    )
    with pytest.raises(MigrationChecksumMismatch, match="checksum mismatch"):
        apply_migrations(conn, migrations, now=10000)

    conn.close()
