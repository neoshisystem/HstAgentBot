"""W11.2: idempotent many-to-many membership, ACL privacy and confirmed removal."""
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
        "_w11_members_helpers", monkeypatch,
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
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w11-members.sqlite3"):
        with database.get_db() as conn:
            apply_migrations(conn, root / "migrations", now=1794400000)
            conn.execute(
                "INSERT INTO bot_registry("
                "bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision"
                ") VALUES(9001,NULL,1,'synthetic-verified-primary','w11_primary','active',1794400000,2)"
            )
        yield helpers, database, service, sentinel, attempts


def _nonce(database, actor_id, profile_id, account_id, now, revision):
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return create_confirmation(
            conn, actor_tg_id=actor_id, bot_id=9001,
            action="profile.member.remove",
            target_type="subscription_profile_account",
            target_id=f"{profile_id}:{account_id}",
            expected_revision=revision,
            payload={"profile_id": profile_id, "account_id": account_id, "revision": revision},
            now=now,
        )

def test_many_profiles_and_no_account_delete(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    with _prepared(root, tmp_path, monkeypatch) as (helpers, database, service, sentinel, attempts):
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        helpers.seed_actor(database, actor_tg_id=505, rep_id=203, rep_tg_id=606)
        helpers.seed_account(database, account_id=303, rep_id=202, owner_tg_id=101)
        with database.get_db() as conn:
            default = service.ensure_default_profile(conn, 101, now=1794400101)
            profile_b = service.create_profile(conn, 101, "Profile B", account_id=303, now=1794400102)
            added = service.add_profile_member(conn, 101, default.profile_id, 303, now=1794400103)
            duplicate = service.add_profile_member(conn, 101, profile_b.profile_id, 303, now=1794400104)
            assert added.added is True and duplicate.added is False
            assert conn.execute(
                "SELECT COUNT(*) FROM subscription_profile_accounts WHERE account_id=303"
            ).fetchone()[0] == 2

            hidden = service.create_profile(
                conn, 7001, "Private Profile Name", owner_tg_id=606, now=1794400105
            )
            service.add_profile_member(conn, 7001, hidden.profile_id, 303, now=1794400106)
            visible = service.add_profile_member(
                conn, 101, default.profile_id, 303, now=1794400107
            ).visible_profile_names
            assert "Profile B" in visible
            assert "Private Profile Name" not in visible

            conn.execute(
                "INSERT INTO account_access_grants("
                "account_id,grantee_tg_id,access_level,granted_by_tg_id,created_at,revoked_at"
                ") VALUES(303,505,'viewer',101,1794400108,NULL)"
            )
            viewer_profile = service.create_profile(conn, 505, "Viewer Profile", now=1794400109)
            assert service.add_profile_member(
                conn, 505, viewer_profile.profile_id, 303, now=1794400110
            ).added
            conn.execute(
                "UPDATE account_access_grants SET revoked_at=1794400111 "
                "WHERE account_id=303 AND grantee_tg_id=505"
            )
            revoked_profile = service.create_profile(conn, 505, "Revoked Profile", now=1794400112)
            with pytest.raises(service.ProfilePermissionError):
                service.add_profile_member(conn, 505, revoked_profile.profile_id, 303, now=1794400113)
            revision = int(conn.execute(
                "SELECT revision FROM subscription_profiles WHERE profile_id=?",
                (default.profile_id,),
            ).fetchone()[0])

        nonce = _nonce(database, 101, default.profile_id, 303, 1794400114, revision)
        with database.get_db() as conn:
            assert service.remove_profile_member(
                conn, 101, default.profile_id, 303,
                nonce=nonce["nonce"], now=1794400115,
            )
            assert conn.execute(
                "SELECT COUNT(*) FROM subscription_profile_accounts "
                "WHERE profile_id=? AND account_id=303", (default.profile_id,),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM subscription_profile_accounts "
                "WHERE profile_id=? AND account_id=303", (profile_b.profile_id,),
            ).fetchone()[0] == 1
            assert tuple(conn.execute(
                "SELECT owner_tg_id,enable FROM accounts WHERE id=303"
            ).fetchone()) == (101, 1)
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert attempts == []
        assert not Path(sentinel).exists()
