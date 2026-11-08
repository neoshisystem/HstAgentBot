"""Acceptance coverage for v5.2 migrations W02.1-W02.3."""

import hashlib
import importlib.util
import shutil
import sqlite3
from pathlib import Path

import pytest

from migrations_runner import apply_migrations


def _helpers(root, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "_hstagentbot_v52_schema_helpers", Path(__file__).with_name("v52_helpers.py")
    )
    assert spec is not None and spec.loader is not None
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    return helpers, database, sentinel


def _migration_subset(root, tmp_path, names):
    source = root / "migrations"
    target = tmp_path / ("migrations-" + "-".join(names))
    target.mkdir()
    for name in names:
        shutil.copyfile(source / f"{name}.sql", target / f"{name}.sql")
    return target


def _seed_legacy(helpers, database):
    actor = helpers.seed_actor(
        database, actor_tg_id=101, rep_id=202, rep_tg_id=303, wallet_balance=24680
    )
    account = helpers.seed_account(
        database, account_id=303, rep_id=202, owner_tg_id=101,
        account_code="V52-W02-LEGACY-ACCOUNT",
        email="v52-w02-legacy@example.invalid",
        sub_id="V52_W02_LEGACY_SUB_MARKER",
    )
    with database.get_db() as seed:
        seed.execute(
            "UPDATE representatives SET balance_toman=13579 WHERE id=?", (202,)
        )
        seed.execute(
            "INSERT INTO financial_ledger "
            "(rep_id,account_code,email,plan_code,transaction_type,amount,balance_after,description,actor_id,actor_name) "
            "VALUES (?,?,?,'legacy-seed','debit',13579,13579,'W02 legacy fixture',?,'synthetic')",
            (202, account["account_code"], account["email"], actor["actor_tg_id"]),
        )
    return actor, account


def _snapshot_tables(conn, names=None):
    selected = names or [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    result = {}
    for name in selected:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()[0]
        result[name] = (
            conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall(),
            ddl,
        )
    return result


def test_migration_001_policy(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor, account = _seed_legacy(helpers, database)

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        old_state = _snapshot_tables(conn)
        migration_dir = _migration_subset(root, tmp_path, ("001_policy",))
        assert apply_migrations(conn, migration_dir, now=1794096000) == ["001_policy"]

        assert {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('policy_decisions','feature_flags')"
        )} == {"policy_decisions", "feature_flags"}
        assert [r[1] for r in conn.execute("PRAGMA table_info(policy_decisions)")] == [
            "decision_id", "status", "authority", "approved_date", "policy_version", "decision_text"
        ]
        assert [r[1] for r in conn.execute("PRAGMA table_info(feature_flags)")] == ["name", "enabled"]
        assert conn.execute(
            "SELECT version,checksum,applied_at FROM schema_migrations"
        ).fetchall() == [
            ("001_policy", hashlib.sha256(
                (migration_dir / "001_policy.sql").read_bytes()
            ).hexdigest(), 1794096000)
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "INSERT INTO feature_flags(name) VALUES ('default-off') RETURNING enabled"
        ).fetchone() == (0,)
        conn.execute("INSERT INTO feature_flags(name,enabled) VALUES ('enabled',1)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO policy_decisions VALUES "
                "('bad','PENDING','hisystem','2026-11-06','v1.0','bad')"
            )
        for invalid in (2, -1):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO feature_flags(name,enabled) VALUES (?,?)",
                    (f"bad-{invalid}", invalid),
                )
        conn.execute(
            "INSERT INTO policy_decisions VALUES "
            "('approved','APPROVED','hisystem','2026-11-06','v1.0','valid')"
        )
        conn.commit()

        applied_state = (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM policy_decisions ORDER BY rowid").fetchall(),
            conn.execute("SELECT * FROM feature_flags ORDER BY rowid").fetchall(),
        )
        assert apply_migrations(conn, migration_dir, now=1794099600) == []
        assert applied_state == (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM policy_decisions ORDER BY rowid").fetchall(),
            conn.execute("SELECT * FROM feature_flags ORDER BY rowid").fetchall(),
        )
        assert _snapshot_tables(conn, old_state.keys()) == old_state
        assert conn.execute(
            "SELECT owner_tg_id FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone() == (101,)
        assert conn.execute(
            "SELECT amount,description FROM financial_ledger WHERE account_code=?",
            (account["account_code"],),
        ).fetchall() == [(13579, "W02 legacy fixture")]
        conn.close()


def test_migration_002_panels(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor, account = _seed_legacy(helpers, database)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        old_state = _snapshot_tables(conn)
        migration_dir = _migration_subset(root, tmp_path, ("001_policy", "002_panels"))

        assert apply_migrations(conn, migration_dir, now=1794100000) == [
            "001_policy", "002_panels"
        ]
        assert [r[1] for r in conn.execute("PRAGMA table_info(panels)")] == [
            "id", "panel_code", "name", "panel_type", "adapter_version", "raw_url",
            "protocol", "host", "port", "base_path", "auth_type", "auth_cipher",
            "sub_base_url", "lifecycle", "is_default", "is_hidden", "max_clients",
            "for_end_users", "for_reps", "access_type", "min_rep_rank", "min_user_rank",
            "username_strategy", "custom_prefix", "renewal_method", "connect_first_guard",
            "allow_test_account", "test_duration_hours", "test_volume_mib", "allow_addons",
            "extra_gib_rep_price", "extra_gib_user_price", "extra_day_rep_price",
            "extra_day_user_price", "default_inbound_ids_json", "extra_config_json",
            "revision", "created_at",
        ]
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ux_panels_default'"
        ).fetchone() == ("ux_panels_default",)
        conn.execute(
            "INSERT INTO panels(id,panel_code,name,panel_type,is_default) "
            "VALUES (1,'panel-main','Main','3x-ui',1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,is_default) "
                "VALUES (2,'panel-second','Second','3x-ui',1)"
            )
        for port in (0, 65536):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO panels(id,panel_code,name,panel_type,port) VALUES (?,?,?,?,?)",
                    (10 + port, f"bad-port-{port}", "Bad", "3x-ui", port),
                )
        for lifecycle in ("unknown", "deleted"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO panels(id,panel_code,name,panel_type,lifecycle) VALUES (?,?,?,?,?)",
                    (30, f"bad-life-{lifecycle}", "Bad", "3x-ui", lifecycle),
                )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,is_default) VALUES (40,'bad-flag','Bad','3x-ui',2)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO panel_rep_assignments(panel_id,rep_id,effect,assigned_by) "
                "VALUES (1,202,'invalid',101)"
            )
        conn.execute(
            "INSERT INTO panel_rep_assignments(panel_id,rep_id,effect,assigned_by) "
            "VALUES (1,202,'allow',101)"
        )
        conn.execute("INSERT INTO panel_counters(panel_id,next_value) VALUES (1,7)")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.commit()

        applied_state = (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM panels ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM panel_rep_assignments ORDER BY panel_id,rep_id").fetchall(),
            conn.execute("SELECT * FROM panel_counters ORDER BY panel_id").fetchall(),
        )
        assert apply_migrations(conn, migration_dir, now=1794103600) == []
        assert applied_state == (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM panels ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM panel_rep_assignments ORDER BY panel_id,rep_id").fetchall(),
            conn.execute("SELECT * FROM panel_counters ORDER BY panel_id").fetchall(),
        )
        assert _snapshot_tables(conn, old_state.keys()) == old_state
        assert conn.execute(
            "SELECT owner_tg_id FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone() == (actor["actor_tg_id"],)
        assert conn.execute(
            "SELECT balance_toman FROM representatives WHERE id=202"
        ).fetchone() == (13579,)
        assert conn.execute(
            "SELECT amount,description FROM financial_ledger WHERE account_code=?",
            (account["account_code"],),
        ).fetchall() == [(13579, "W02 legacy fixture")]
        conn.close()


def test_migration_003_accounts(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor, account = _seed_legacy(helpers, database)
        with database.get_db() as seed:
            seed.execute(
                "INSERT INTO reserved_packages "
                "(rep_id,account_code,email,plan_code,plan_name,gb,days,charge_amount,actor_id,actor_name) "
                "VALUES (202,? ,?,'legacy-plan','Legacy Plan',5,30,25000,101,'synthetic')",
                (account["account_code"], account["email"]),
            )
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        reserved_before = conn.execute(
            "SELECT id,rep_id,account_code,email,plan_code,plan_name,gb,days,"
            "on_first_connect,charge_amount,actor_id,actor_name,status,created_at,activated_at "
            "FROM reserved_packages ORDER BY id"
        ).fetchall()
        ledger_before = conn.execute(
            "SELECT rep_id,account_code,email,plan_code,transaction_type,amount,balance_after,"
            "description,actor_id,actor_name FROM financial_ledger ORDER BY id"
        ).fetchall()
        rep_before = conn.execute(
            "SELECT id,rep_code,balance_toman FROM representatives WHERE id=202"
        ).fetchone()
        migration_dir = root / "migrations"
        assert apply_migrations(conn, migration_dir, now=1794110000) == [
            "001_policy", "002_panels", "003_accounts"
        ]
        expected_account_columns = [
            "id", "rep_id", "account_code", "email", "customer_name", "sub_id",
            "enable", "total_gb", "expiry_time", "owner_tg_id", "created_at",
            "updated_at", "first_connect_mode", "rollover_enabled", "panel_id",
            "sub_id_cipher", "seller_key", "revision", "acl_revision", "is_test",
            "is_personal_use", "deleted_at",
        ]
        assert [r[1] for r in conn.execute("PRAGMA table_info(accounts)")] == expected_account_columns
        preserved = conn.execute(
            "SELECT id,rep_id,account_code,email,customer_name,sub_id,enable,total_gb,"
            "expiry_time,owner_tg_id,first_connect_mode,rollover_enabled,panel_id,"
            "sub_id_cipher,seller_key,revision,acl_revision,is_test,is_personal_use,deleted_at "
            "FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone()
        assert preserved == (
            303, 202, account["account_code"], account["email"], "V52 Synthetic Customer 303",
            account["sub_id"], 1, 10.0, 0, actor["actor_tg_id"], 0, 1, None,
            None, "root", 1, 1, 0, 0, None,
        )
        reserved_after = conn.execute(
            "SELECT id,rep_id,account_code,email,plan_code,plan_name,gb,days,"
            "on_first_connect,charge_amount,actor_id,actor_name,status,created_at,activated_at "
            "FROM reserved_packages ORDER BY id"
        ).fetchall()
        assert reserved_after == reserved_before
        assert conn.execute(
            "SELECT panel_id,account_id FROM reserved_packages"
        ).fetchall() == [(None, None)]
        assert conn.execute(
            "SELECT rep_id,account_code,email,plan_code,transaction_type,amount,balance_after,"
            "description,actor_id,actor_name FROM financial_ledger ORDER BY id"
        ).fetchall() == ledger_before
        assert conn.execute(
            "SELECT id,rep_code,balance_toman FROM representatives WHERE id=202"
        ).fetchone() == rep_before
        conn.execute(
            "INSERT INTO panels(id,panel_code,name,panel_type) VALUES (77,'new-panel','New','3x-ui')"
        )
        conn.execute("UPDATE accounts SET panel_id=77 WHERE id=?", (account["account_id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO accounts(account_code,email,panel_id) VALUES ('DUP','V52-W02-LEGACY@EXAMPLE.INVALID',77)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE accounts SET panel_id=999 WHERE id=?", (account["account_id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE reserved_packages SET account_id=999 WHERE id=1")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.commit()

        state_after = (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM accounts ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM reserved_packages ORDER BY id").fetchall(),
        )
        assert apply_migrations(conn, migration_dir, now=1794113600) == []
        assert state_after == (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM accounts ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM reserved_packages ORDER BY id").fetchall(),
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()
