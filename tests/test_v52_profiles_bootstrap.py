"""W11.5: restartable default-profile bootstrap for all known Telegram principals."""
from contextlib import contextmanager
import base64
import importlib.util
from pathlib import Path
import sys

import pytest
from migrations_runner import apply_migrations


def _load(path, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _prepared(root, tmp_path, monkeypatch):
    helpers = _load(Path(__file__).with_name("v52_helpers.py"), "_w11_bootstrap_helpers", monkeypatch)
    attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    monkeypatch.setattr(sys.modules["config"], "ADMIN_TELEGRAM_ID", 7001)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    service = _load(root / "subscription_service.py", "subscription_service", monkeypatch)
    bootstrap = _load(root / "bootstrap_profiles.py", "bootstrap_profiles", monkeypatch)
    identity = _load(root / "identity_service.py", "identity_service", monkeypatch)
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w11-bootstrap.sqlite3"):
        with database.get_db() as conn:
            apply_migrations(conn, root / "migrations", now=1794400400)
        yield helpers, database, service, bootstrap, identity, sentinel, attempts


def test_all_principals_have_default(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    with _prepared(root, tmp_path, monkeypatch) as (
        helpers, database, service, bootstrap, identity, sentinel, attempts
    ):
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303, wallet_balance=4321)
        helpers.seed_account(database, account_id=303, rep_id=202, owner_tg_id=202)
        with database.get_db() as conn:
            identity.ensure_personal_identity(conn, 7001)
            conn.execute(
                "INSERT INTO account_access_grants("
                "account_id,grantee_tg_id,access_level,granted_by_tg_id,created_at,revoked_at"
                ") VALUES(303,404,'viewer',101,1794400401,NULL)"
            )
            wallets_before = {
                int(row["user_id"]): int(row["balance_toman"])
                for row in conn.execute("SELECT user_id,balance_toman FROM user_wallets")
            }
            with pytest.raises(service.ProfileBootstrapError):
                bootstrap.backfill_profiles(conn, now=1794400402)
            assert conn.execute("SELECT COUNT(*) FROM subscription_profiles").fetchone()[0] == 0
            assert {
                int(row["user_id"]): int(row["balance_toman"])
                for row in conn.execute("SELECT user_id,balance_toman FROM user_wallets")
            } == wallets_before
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9002,NULL,1,'','unknown','pending',NULL,1)"
            )
            with pytest.raises(service.ProfileBootstrapError):
                bootstrap.backfill_profiles(conn, now=1794400403)
            assert conn.execute("SELECT COUNT(*) FROM subscription_profiles").fetchone()[0] == 0
            conn.execute(
                "UPDATE bot_registry SET status='active',token_cipher='synthetic-verified-primary',"
                "verified_at=1794400404,revision=2 WHERE bot_id=9002"
            )

        with database.get_db() as conn:
            first = bootstrap.backfill_profiles(conn, now=1794400405)
            assert first == {
                "primary_bot_id": 9002,
                "principal_count": 5,
                "profiles_created": 5,
                "wallets_created": 3,
            }
            profiles = conn.execute(
                "SELECT owner_tg_id,is_default,created_bot_id FROM subscription_profiles ORDER BY owner_tg_id"
            ).fetchall()
            assert [tuple(row) for row in profiles] == [
                (101, 1, 9002), (202, 1, 9002), (303, 1, 9002),
                (404, 1, 9002), (7001, 1, 9002),
            ]
            second = bootstrap.backfill_profiles(conn, now=1794400406)
            assert second["principal_count"] == 5
            assert second["profiles_created"] == 0 and second["wallets_created"] == 0
            assert int(conn.execute(
                "SELECT balance_toman FROM user_wallets WHERE user_id=101"
            ).fetchone()[0]) == 4321
            assert conn.execute("SELECT COUNT(*) FROM subscription_profiles").fetchone()[0] == 5
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert attempts == []
        assert not Path(sentinel).exists()
