"""W11.4: clear membership, protect default, rename safely and soft-delete custom."""
import sqlite3
from confirmation_service import create_confirmation
import pytest

from contextlib import contextmanager
import base64
import importlib.util
from pathlib import Path
import sys

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
    helpers = _load(
        Path(__file__).with_name("v52_helpers.py"),
        "_w11_edit_helpers", monkeypatch,
    )
    attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    monkeypatch.setattr(sys.modules["config"], "ADMIN_TELEGRAM_ID", 7001)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    service = _load(root / "subscription_service.py", "subscription_service", monkeypatch)
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w11-edit.sqlite3"):
        with database.get_db() as conn:
            apply_migrations(conn, root / "migrations", now=1794400000)
            conn.execute(
                "INSERT INTO bot_registry("
                "bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision"
                ") VALUES(9001,NULL,1,'synthetic-verified-primary','w11_primary','active',1794400000,2)"
            )
        yield helpers, database, service, sentinel, attempts


def _revision(database, profile_id):
    with database.get_db() as conn:
        return int(conn.execute(
            "SELECT revision FROM subscription_profiles WHERE profile_id=?", (profile_id,)
        ).fetchone()[0])

def _nonce(database, profile_id, action, now):
    revision = _revision(database, profile_id)
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return create_confirmation(
            conn, actor_tg_id=101, bot_id=9001, action=action,
            target_type="subscription_profile", target_id=profile_id,
            expected_revision=revision, payload={"profile_id": profile_id}, now=now,
        )

def test_default_protection_and_clear(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    with _prepared(root, tmp_path, monkeypatch) as (helpers, database, service, sentinel, attempts):
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        helpers.seed_account(database, account_id=303, rep_id=202, owner_tg_id=101)
        with database.get_db() as conn:
            default = service.ensure_default_profile(conn, 101, now=1794400301)
            custom = service.create_profile(conn, 101, "Custom", account_id=303, now=1794400302)
            service.add_profile_member(conn, 101, default.profile_id, 303, now=1794400303)
            renamed = service.rename_profile(
                conn, 101, custom.profile_id, ' <img src=x onerror="alert(1)"> '
            )
            assert renamed.name == '<img src=x onerror="alert(1)">'
            assert renamed.display_name == '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;'
            before_ledger = int(conn.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0])

        clear = _nonce(database, default.profile_id, "profile.clear", 1794400304)
        with database.get_db() as conn:
            assert service.clear_profile(
                conn, 101, default.profile_id, nonce=clear["nonce"], now=1794400305
            ) == 1
            row = conn.execute(
                "SELECT is_default,deleted_at FROM subscription_profiles WHERE profile_id=?",
                (default.profile_id,),
            ).fetchone()
            assert tuple(row) == (1, None)
            assert conn.execute(
                "SELECT COUNT(*) FROM subscription_profile_accounts WHERE profile_id=?",
                (default.profile_id,),
            ).fetchone()[0] == 0

        with database.get_db() as conn:
            with pytest.raises(service.ProfileError):
                service.delete_profile(
                    conn, 101, default.profile_id, nonce="A" * 24, now=1794400306
                )
        with pytest.raises(sqlite3.IntegrityError, match="default_profile_not_deletable"):
            with database.get_db() as conn:
                conn.execute("DELETE FROM subscription_profiles WHERE profile_id=?", (default.profile_id,))
        with pytest.raises(sqlite3.IntegrityError, match="default_profile_not_deletable"):
            with database.get_db() as conn:
                conn.execute(
                    "UPDATE subscription_profiles SET is_default=0 WHERE profile_id=?",
                    (default.profile_id,),
                )

        delete = _nonce(database, custom.profile_id, "profile.delete", 1794400307)
        with database.get_db() as conn:
            assert service.delete_profile(
                conn, 101, custom.profile_id, nonce=delete["nonce"], now=1794400308
            )
            assert service.resolve_profile_by_token(conn, custom.token) is None
            assert tuple(conn.execute(
                "SELECT owner_tg_id,enable FROM accounts WHERE id=303"
            ).fetchone()) == (101, 1)
            assert int(conn.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0]) == before_ledger
            assert conn.execute(
                "SELECT COUNT(*) FROM subscription_profiles WHERE profile_id=? AND deleted_at IS NULL",
                (default.profile_id,),
            ).fetchone()[0] == 1
        assert attempts == []
        assert not Path(sentinel).exists()
