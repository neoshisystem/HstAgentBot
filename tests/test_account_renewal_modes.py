import sqlite3

import pytest

import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "renewal-modes.sqlite3"))
    database.init_db()


def add_account(code, owner_tg_id=None):
    with database.get_db() as conn:
        conn.execute(
            """
            INSERT INTO accounts
                (rep_id, account_code, email, customer_name, sub_id, enable,
                 total_gb, expiry_time, owner_tg_id)
            VALUES (NULL, ?, ?, '', '', 1, 20, 0, ?)
            """,
            (code, f"{code.lower()}@example.test", owner_tg_id),
        )


def read_account(code):
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE account_code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None


def test_init_db_migrates_legacy_accounts_table_idempotently(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-accounts.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))

    # Simulate the pre-Scope-5.1 accounts schema with no per-account policy fields.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rep_id INTEGER DEFAULT NULL,
                account_code TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                customer_name TEXT,
                sub_id TEXT,
                enable INTEGER DEFAULT 1,
                total_gb REAL DEFAULT 0,
                expiry_time INTEGER DEFAULT 0,
                owner_tg_id INTEGER DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO accounts (account_code, email)
            VALUES ('LEGACY-1', 'legacy-1@example.test')
            """
        )

    database.init_db()
    database.init_db()

    with database.get_db() as conn:
        columns = {
            row["name"]: row
            for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
        }
        assert columns["first_connect_mode"]["dflt_value"] == "0"
        assert columns["rollover_enabled"]["dflt_value"] == "1"
        names = [row["name"] for row in conn.execute("PRAGMA table_info(accounts)")]

    assert names.count("first_connect_mode") == 1
    assert names.count("rollover_enabled") == 1
    assert database.get_account_renewal_modes("LEGACY-1") == {
        "first_connect_mode": False,
        "rollover_enabled": True,
    }


def test_fresh_accounts_receive_policy_defaults(isolated_db):
    add_account("DEFAULTS-1")
    account = read_account("DEFAULTS-1")

    assert account["first_connect_mode"] == 0
    assert account["rollover_enabled"] == 1
    assert database.get_account_renewal_modes("DEFAULTS-1") == {
        "first_connect_mode": False,
        "rollover_enabled": True,
    }


def test_get_account_renewal_modes_prefers_account_values(isolated_db):
    add_account("ACCOUNT-1", owner_tg_id=71001)
    assert database.set_user_first_connect_mode(71001, True) is True

    # An indexed account's own setting wins over the previous user-level value.
    assert database.get_account_renewal_modes(
        "ACCOUNT-1", fallback_user_id=71001
    ) == {
        "first_connect_mode": False,
        "rollover_enabled": True,
    }

    with database.get_db() as conn:
        conn.execute(
            """
            UPDATE accounts
            SET first_connect_mode = 1, rollover_enabled = 0
            WHERE account_code = 'ACCOUNT-1'
            """
        )

    assert database.get_account_renewal_modes("ACCOUNT-1") == {
        "first_connect_mode": True,
        "rollover_enabled": False,
    }


def test_missing_account_uses_legacy_first_connect_and_rollover_default(isolated_db):
    assert database.set_user_first_connect_mode(72001, True) is True

    assert database.get_account_renewal_modes(
        "NOT-INDEXED", fallback_user_id=72001
    ) == {
        "first_connect_mode": True,
        "rollover_enabled": True,
    }
    assert database.get_account_renewal_modes("NOT-INDEXED") == {
        "first_connect_mode": False,
        "rollover_enabled": True,
    }


def test_set_account_first_connect_mode_writes_account_and_syncs_actor(isolated_db):
    add_account("WRITE-FIRST-1")

    assert database.set_account_first_connect_mode(
        "WRITE-FIRST-1", True, actor_id=73001
    ) is True
    assert read_account("WRITE-FIRST-1")["first_connect_mode"] == 1
    assert database.get_user_first_connect_mode(73001) is True

    assert database.set_account_first_connect_mode(
        "WRITE-FIRST-1", False, actor_id=73001
    ) is True
    assert read_account("WRITE-FIRST-1")["first_connect_mode"] == 0
    assert database.get_user_first_connect_mode(73001) is False


def test_set_account_first_connect_mode_requires_existing_account(isolated_db):
    assert database.set_account_first_connect_mode("MISSING", True) is False
    assert database.set_account_first_connect_mode("", True, actor_id=74001) is False
    assert database.get_user_first_connect_mode(74001) is False


def test_set_account_rollover_mode_writes_only_target_account(isolated_db):
    add_account("ROLLOVER-1")
    add_account("ROLLOVER-2")

    assert database.set_account_rollover_mode("ROLLOVER-1", False) is True
    assert read_account("ROLLOVER-1")["rollover_enabled"] == 0
    assert read_account("ROLLOVER-2")["rollover_enabled"] == 1

    assert database.set_account_rollover_mode("ROLLOVER-1", True) is True
    assert read_account("ROLLOVER-1")["rollover_enabled"] == 1
    assert database.set_account_rollover_mode("MISSING", False) is False
    assert database.set_account_rollover_mode("", False) is False


@pytest.mark.parametrize("enabled, expected", [(True, 1), (False, 0)])
def test_account_policy_setters_normalize_boolean_values(isolated_db, enabled, expected):
    add_account("NORMALIZE-1")
    assert database.set_account_first_connect_mode("NORMALIZE-1", enabled) is True
    assert database.set_account_rollover_mode("NORMALIZE-1", enabled) is True
    account = read_account("NORMALIZE-1")
    assert account["first_connect_mode"] == expected
    assert account["rollover_enabled"] == expected
