"""W11.1: default identity, token secrecy, quota serialization and atomic create."""
import concurrent.futures
import hashlib
import sqlite3
import threading

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
        "_w11_profiles_helpers", monkeypatch,
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
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w11-profiles.sqlite3"):
        with database.get_db() as conn:
            apply_migrations(conn, root / "migrations", now=1794400000)
            conn.execute(
                "INSERT INTO bot_registry("
                "bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision"
                ") VALUES(9001,NULL,1,'synthetic-verified-primary','w11_primary','active',1794400000,2)"
            )
        yield helpers, database, service, sentinel, attempts


def test_default_unique_quota_and_atomic_create(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    with _prepared(root, tmp_path, monkeypatch) as (helpers, database, service, sentinel, attempts):
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        helpers.seed_account(database, account_id=303, rep_id=202, owner_tg_id=101)
        helpers.seed_account(database, account_id=304, rep_id=202, owner_tg_id=505)
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO system_settings(key,value,label,description) "
                "VALUES('sub_profile_limit','2','synthetic quota','W11')"
            )
            first = service.ensure_default_profile(conn, 101, now=1794400001)
            second = service.ensure_default_profile(conn, 101, now=1794400002)
            assert second.profile_id == first.profile_id and second.created is False
            assert second.token is None
            assert first.is_default and first.name == "⭐ پروفایل پیش‌فرض من"
            assert first.token and len(first.token) == 43 and first.token not in repr(first)
            stored = conn.execute(
                "SELECT token_hash,token_cipher,created_bot_id FROM subscription_profiles "
                "WHERE profile_id=?", (first.profile_id,),
            ).fetchone()
            from crypto_store import decrypt_secret
            assert stored["token_hash"] == hashlib.sha256(first.token.encode("ascii")).hexdigest()
            assert decrypt_secret(stored["token_cipher"], "hstagent/columns/v1") == first.token
            assert stored["created_bot_id"] == 9001

            before = conn.execute(
                "SELECT COUNT(*) FROM subscription_profiles WHERE owner_tg_id=101"
            ).fetchone()[0]
            with pytest.raises(service.ProfilePermissionError):
                service.create_profile(conn, 101, "unauthorized", account_id=304, now=1794400003)
            assert conn.execute(
                "SELECT COUNT(*) FROM subscription_profiles WHERE owner_tg_id=101"
            ).fetchone()[0] == before
            custom = service.create_profile(conn, 101, "Atomic member", account_id=303, now=1794400004)
            assert conn.execute(
                "SELECT COUNT(*) FROM subscription_profile_accounts "
                "WHERE profile_id=? AND account_id=303", (custom.profile_id,),
            ).fetchone()[0] == 1

        barrier = threading.Barrier(2)
        def contender(label):
            conn = sqlite3.connect(str(database.DB_PATH), timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                barrier.wait(timeout=5)
                return service.create_profile(conn, 101, label, now=1794400005).profile_id
            except service.ProfileQuotaError:
                return "quota"
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(contender, ("Race A", "Race B")))
        assert sum(value != "quota" for value in outcomes) == 1
        assert outcomes.count("quota") == 1
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM subscription_profiles "
                "WHERE owner_tg_id=101 AND is_default=0 AND deleted_at IS NULL"
            ).fetchone()[0] == 2
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert attempts == []
        assert not Path(sentinel).exists()
