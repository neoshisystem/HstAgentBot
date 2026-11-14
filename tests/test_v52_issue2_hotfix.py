"""Regression coverage for the post-release Issue #2 hotfixes."""
from __future__ import annotations

import ast
from pathlib import Path
import sqlite3

import pytest

import database


ROOT = Path(__file__).resolve().parents[1]


TEST_REP_IDS = (910001, 910002, 910022, 910023, 910030, 910031, 910032)


def _seed_representatives():
    with database.get_db() as conn:
        for rep_id in TEST_REP_IDS:
            conn.execute(
                """
                INSERT INTO representatives(id, rep_code, name, scope_filter)
                VALUES (?, ?, ?, 'all')
                """,
                (rep_id, f"ISSUE2-REP-{rep_id}", f"Issue #2 test representative {rep_id}"),
            )


def _initialize_legacy_database(monkeypatch, tmp_path):
    monkeypatch.delenv("V52_MIGRATIONS_ENABLED", raising=False)
    db_path = tmp_path / "legacy-issue2.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()
    _seed_representatives()
    return db_path


def _initialize_migrated_database(monkeypatch, tmp_path):
    monkeypatch.delenv("V52_MIGRATIONS_ENABLED", raising=False)
    db_path = tmp_path / "migrated-issue2.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()
    monkeypatch.setenv("V52_MIGRATIONS_ENABLED", "1")
    database.init_db()
    _seed_representatives()
    with database.get_db() as conn:
        versions = {
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        assert "003_accounts" in versions
        assert {
            row["name"] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
        } >= {"panel_id", "email"}
    return db_path


def _insert_panel(panel_id, panel_code, *, is_default=False):
    with database.get_db() as conn:
        conn.execute(
            """
            INSERT INTO panels(id, panel_code, name, panel_type, is_default)
            VALUES (?, ?, ?, '3x-ui', ?)
            """,
            (panel_id, panel_code, panel_code, int(is_default)),
        )


def test_rep_bot_main_guard_is_present_at_module_end():
    source = (ROOT / "rep_bot.py").read_text(encoding="utf-8")
    module = ast.parse(source, filename="rep_bot.py")
    assert module.body, "rep_bot.py must not be empty"

    guard = module.body[-1]
    assert isinstance(guard, ast.If)
    assert (
        isinstance(guard.test, ast.Compare)
        and isinstance(guard.test.left, ast.Name)
        and guard.test.left.id == "__name__"
        and len(guard.test.ops) == 1
        and isinstance(guard.test.ops[0], ast.Eq)
        and len(guard.test.comparators) == 1
        and isinstance(guard.test.comparators[0], ast.Constant)
        and guard.test.comparators[0].value == "__main__"
    )
    assert len(guard.body) == 1
    call = guard.body[0]
    assert isinstance(call, ast.Expr)
    assert isinstance(call.value, ast.Call)
    assert isinstance(call.value.func, ast.Name)
    assert call.value.func.id == "main"
    assert not call.value.args and not call.value.keywords


def test_upsert_account_record_preserves_legacy_email_unique_schema(monkeypatch, tmp_path):
    _initialize_legacy_database(monkeypatch, tmp_path)

    database.upsert_account_record(
        910001, "legacy@example.invalid", "LEGACY-1",
        customer_name="First", total_gb=10,
    )
    database.upsert_account_record(
        910002, "legacy@example.invalid", "LEGACY-2",
        customer_name="Updated", total_gb=20,
    )

    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT rep_id, account_code, customer_name, total_gb FROM accounts "
            "WHERE email=?",
            ("legacy@example.invalid",),
        ).fetchall()
        assert len(rows) == 1
        assert tuple(rows[0]) == (910002, "LEGACY-2", "Updated", 20.0)


def test_upsert_account_record_migration_003_defaults_and_scopes_by_panel(monkeypatch, tmp_path):
    _initialize_migrated_database(monkeypatch, tmp_path)
    _insert_panel(101, "PANEL-DEFAULT", is_default=True)
    _insert_panel(202, "PANEL-SECOND")

    # Existing callsites can omit panel_id when one default panel is configured.
    database.upsert_account_record(
        910001, "Shared@Example.invalid", "DEFAULT-1", customer_name="Default",
    )
    database.upsert_account_record(
        910002, "shared@example.invalid", "DEFAULT-2", customer_name="Default updated",
    )
    # Explicit panel IDs permit the same email on a different panel.
    database.upsert_account_record(
        910022, "shared@example.invalid", "SECOND-1", customer_name="Second",
        panel_id=202,
    )
    database.upsert_account_record(
        910023, "SHARED@example.invalid", "SECOND-2", customer_name="Second updated",
        panel_id=202,
    )

    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT panel_id, rep_id, account_code, customer_name FROM accounts "
            "WHERE email=? COLLATE NOCASE ORDER BY panel_id",
            ("shared@example.invalid",),
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (101, 910002, "DEFAULT-2", "Default updated"),
            (202, 910023, "SECOND-2", "Second updated"),
        ]

    with pytest.raises(ValueError, match="ambiguous"):
        database.upsert_account_record(
            910030, "shared@example.invalid", "AMBIGUOUS-UPDATE",
        )


def test_upsert_account_record_migration_003_without_default_panel_uses_null_scope_safely(
    monkeypatch, tmp_path
):
    _initialize_migrated_database(monkeypatch, tmp_path)

    database.upsert_account_record(
        910031, "null-scope@example.invalid", "NULL-1", customer_name="First",
    )
    database.upsert_account_record(
        910032, "NULL-SCOPE@example.invalid", "NULL-2", customer_name="Updated",
    )

    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT panel_id, rep_id, account_code, customer_name FROM accounts "
            "WHERE email=? COLLATE NOCASE",
            ("null-scope@example.invalid",),
        ).fetchall()
        assert len(rows) == 1
        assert tuple(rows[0]) == (None, 910032, "NULL-2", "Updated")


def test_legacy_sync_refuses_panel_scoped_accounts_schema(monkeypatch, tmp_path):
    _initialize_migrated_database(monkeypatch, tmp_path)

    def forbidden_adapter(_integration):
        pytest.fail("_sync_legacy_schema must not instantiate the legacy panel adapter")

    monkeypatch.setattr(database, "_LegacyPanelListAdapter", forbidden_adapter)
    result = database._sync_legacy_schema([])
    assert result["status"] == "error"
    assert "panel-scoped schema" in result["message"]
    assert result["count"] == 0
