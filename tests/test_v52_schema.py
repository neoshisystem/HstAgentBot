"""Acceptance coverage for the exact W02.1 policy migration."""

import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from migrations_runner import apply_migrations


def test_migration_001_policy(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "_hstagentbot_v52_schema_helpers", Path(__file__).with_name("v52_helpers.py")
    )
    assert spec is not None and spec.loader is not None
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor = helpers.seed_actor(database, actor_tg_id=101, rep_id=202,
                                   rep_tg_id=303, wallet_balance=24680)
        account = helpers.seed_account(
            database, account_id=303, rep_id=202, owner_tg_id=101,
            account_code="V52-W02-LEGACY-ACCOUNT",
            email="v52-w02-legacy@example.invalid", sub_id="V52_W02_LEGACY_SUB_MARKER",
        )
        with database.get_db() as seed:
            seed.execute("UPDATE representatives SET balance_toman=13579 WHERE id=?", (202,))
            seed.execute(
                "INSERT INTO financial_ledger "
                "(rep_id,account_code,email,plan_code,transaction_type,amount,balance_after,description,actor_id,actor_name) "
                "VALUES (?,?,?,'legacy-seed','debit',13579,13579,'W02.1 legacy fixture',?,'synthetic')",
                (202, account["account_code"], account["email"], actor["actor_tg_id"]),
            )

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        old_tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        old_state = {
            name: (
                conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall(),
                conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0],
            )
            for name in old_tables
        }
        migration_dir = root / "migrations"
        assert apply_migrations(conn, migration_dir, now=1794096000) == ["001_policy"]

        assert {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('policy_decisions','feature_flags')"
        )} == {"policy_decisions", "feature_flags"}
        assert [r[1] for r in conn.execute("PRAGMA table_info(policy_decisions)")] == [
            "decision_id", "status", "authority", "approved_date", "policy_version", "decision_text"
        ]
        assert [r[1] for r in conn.execute("PRAGMA table_info(feature_flags)")] == ["name", "enabled"]
        assert conn.execute("SELECT version,checksum,applied_at FROM schema_migrations").fetchall() == [
            ("001_policy", hashlib.sha256((migration_dir / "001_policy.sql").read_bytes()).hexdigest(), 1794096000)
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("INSERT INTO feature_flags(name) VALUES ('default-off') RETURNING enabled").fetchone() == (0,)
        conn.execute("INSERT INTO feature_flags(name,enabled) VALUES ('enabled',1)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO policy_decisions VALUES ('bad','PENDING','hisystem','2026-11-06','v1.0','bad')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO feature_flags(name,enabled) VALUES ('bad-high',2)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO feature_flags(name,enabled) VALUES ('bad-low',-1)")
        conn.execute("INSERT INTO policy_decisions VALUES ('approved','APPROVED','hisystem','2026-11-06','v1.0','valid')")
        conn.commit()

        applied_state = (
            conn.execute("SELECT version,checksum,applied_at FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM policy_decisions ORDER BY rowid").fetchall(),
            conn.execute("SELECT * FROM feature_flags ORDER BY rowid").fetchall(),
        )
        assert apply_migrations(conn, migration_dir, now=1794099600) == []
        assert applied_state == (
            conn.execute("SELECT version,checksum,applied_at FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM policy_decisions ORDER BY rowid").fetchall(),
            conn.execute("SELECT * FROM feature_flags ORDER BY rowid").fetchall(),
        )
        for name, (rows, ddl) in old_state.items():
            assert conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() == rows
            assert conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0] == ddl
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT owner_tg_id FROM accounts WHERE id=?", (account["account_id"],)).fetchone() == (101,)
        assert conn.execute("SELECT amount,description FROM financial_ledger WHERE account_code=?", (account["account_code"],)).fetchall() == [(13579, "W02.1 legacy fixture")]
        conn.close()
