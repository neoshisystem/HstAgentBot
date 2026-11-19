"""W11.3: confirmation-bound token rotation and profile pause/resume."""
from confirmation_service import create_confirmation, ConfirmationStale
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
        "_w11_rotate_helpers", monkeypatch,
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
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w11-rotate.sqlite3"):
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
            expected_revision=revision,
            payload={"profile_id": profile_id, "revision": revision}, now=now,
        )

def test_old_token_invalid_immediately(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    with _prepared(root, tmp_path, monkeypatch) as (helpers, database, service, sentinel, attempts):
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        with database.get_db() as conn:
            profile = service.ensure_default_profile(conn, 101, now=1794400201)
            old_token = profile.token
            assert service.resolve_profile_by_token(conn, old_token)["profile_id"] == profile.profile_id

        rotate = _nonce(database, profile.profile_id, "profile.rotate", 1794400202)
        with database.get_db() as conn:
            rotated = service.rotate_profile_token(
                conn, 101, profile.profile_id, nonce=rotate["nonce"], now=1794400203
            )
            assert rotated.token != old_token and rotated.token_version == 2
            assert service.resolve_profile_by_token(conn, old_token) is None
            assert service.resolve_profile_by_token(conn, rotated.token)["profile_id"] == profile.profile_id

        pause = _nonce(database, profile.profile_id, "profile.pause", 1794400204)
        with database.get_db() as conn:
            paused = service.set_profile_enabled(
                conn, 101, profile.profile_id, False, nonce=pause["nonce"], now=1794400205
            )
            assert paused.enabled is False
            assert service.resolve_profile_by_token(conn, rotated.token) is None

        resume = _nonce(database, profile.profile_id, "profile.resume", 1794400206)
        with database.get_db() as conn:
            resumed = service.set_profile_enabled(
                conn, 101, profile.profile_id, True, nonce=resume["nonce"], now=1794400207
            )
            assert resumed.enabled is True
            assert service.resolve_profile_by_token(conn, rotated.token)["profile_id"] == profile.profile_id

        stale = _nonce(database, profile.profile_id, "profile.pause", 1794400208)
        with database.get_db() as conn:
            current = _revision(database, profile.profile_id)
            service.rename_profile(conn, 101, profile.profile_id, "Renamed", expected_revision=current)
        with database.get_db() as conn:
            with pytest.raises(ConfirmationStale):
                service.set_profile_enabled(
                    conn, 101, profile.profile_id, False,
                    nonce=stale["nonce"], now=1794400209,
                )
            assert conn.execute(
                "SELECT enabled FROM subscription_profiles WHERE profile_id=?",
                (profile.profile_id,),
            ).fetchone()[0] == 1
        assert attempts == []
        assert not Path(sentinel).exists()
