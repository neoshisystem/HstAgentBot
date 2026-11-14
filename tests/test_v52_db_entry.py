"""Offline acceptance test for the opt-in versioned database entry point."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import sys

import migrations_runner


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location(
        "_hstagentbot_v52_db_entry_helpers", helper_path
    )
    assert spec is not None and spec.loader is not None
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    return helpers


def test_init_db_migration_gate_and_reentry(tmp_path, monkeypatch):
    product_root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    monkeypatch.delenv("V52_MIGRATIONS_ENABLED", raising=False)

    database, sentinel = helpers.load_database_module(
        product_root, monkeypatch, tmp_path
    )
    with helpers.make_db(
        database, monkeypatch, tmp_path, filename="v52-db-entry.sqlite3"
    ) as db_path:
        assert Path(database.DB_PATH).resolve() == db_path.resolve()
        assert not Path(sentinel).exists()

        # Flag-off is the exact legacy entry path: core schema is present and
        # the versioned registry has not been created as a side effect.
        with database.get_db() as conn:
            tables = {
                row["name"] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert {
                "representatives", "accounts", "pricing_plans",
                "financial_ledger", "system_settings",
            } <= tables
            assert "schema_migrations" not in tables

        original_runner = migrations_runner.apply_migrations
        observed_transactions = []

        def tracked_runner(conn, migration_dir, now):
            observed_transactions.append(conn.in_transaction)
            assert conn.in_transaction is False
            return original_runner(conn, migration_dir, now)

        monkeypatch.setattr(migrations_runner, "apply_migrations", tracked_runner)
        monkeypatch.setenv("V52_MIGRATIONS_ENABLED", "true")

        database.init_db()
        with database.get_db() as conn:
            first_versions = [
                row["version"] for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
        expected_versions = sorted(
            path.stem for path in (product_root / "migrations").glob("*.sql")
        )
        assert first_versions == expected_versions
        assert len(first_versions) == 18

        # Re-entry must leave the migration registry byte-for-byte stable.
        database.init_db()
        with database.get_db() as conn:
            second_versions = [
                row["version"] for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
        assert second_versions == first_versions
        assert observed_transactions == [False, False]
        assert network_attempts == []
        assert not Path(sentinel).exists()
