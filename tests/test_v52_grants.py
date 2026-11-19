"""Offline acceptance tests for grant lifecycle and owner preservation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from migrations_runner import apply_migrations


def _load_helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w05_grant_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_product_module(root, path, module_name, monkeypatch):
    spec = importlib.util.spec_from_file_location(module_name, root / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _seed_user(conn, telegram_id):
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,username,is_blocked) VALUES(?,'end_user',?,0)",
        (telegram_id, f"w05_{telegram_id}"),
    )
    conn.execute(
        "INSERT INTO user_settings(user_id,first_connect_mode,active_persona) VALUES(?,0,'end_user')",
        (telegram_id,),
    )
    conn.execute(
        "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,?)",
        (telegram_id, 8000 + telegram_id),
    )


def test_grant_revoke_preserves_owner(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9090)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w05-grants.sqlite3"):
        with database.get_db() as conn:
            assert apply_migrations(conn, root / "migrations", now=1794237000)
            for actor_id in (101, 202, 303, 9090):
                _seed_user(conn, actor_id)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter) "
                "VALUES(550,'W05-SELLER','W05 seller',303,'')"
            )
            conn.execute("UPDATE representatives SET balance_toman=123456 WHERE id=550")

        helpers.seed_account(
            database, account_id=701, rep_id=550, owner_tg_id=101,
            account_code="W05-GRANT-A", email="grant@example.invalid",
        )
        identity = _load_product_module(root, "identity_service.py", "identity_service", monkeypatch)
        access = _load_product_module(root, "access_service.py", "access_service", monkeypatch)
        with database.get_db() as conn:
            identity.ensure_personal_identity(conn, 9090)
            owner = identity.resolve_actor(conn, 101)
            manager = identity.resolve_actor(conn, 202)
            seller = identity.resolve_actor(conn, 303)
            root_actor = identity.resolve_actor(conn, 9090)

        first = access.set_grant(owner, 701, 202, "viewer", now=1794237100)
        assert first["access_level"] == "viewer"
        assert first["revision"] == 1
        second = access.set_grant(owner, 701, 202, "manager", now=1794237200)
        assert second["access_level"] == "manager"
        assert second["revision"] == 2
        assert access.authorize_account(manager, 701, "renew") is True
        assert access.authorize_account(manager, 701, "grant") is False
        with pytest.raises(PermissionError):
            access.set_grant(manager, 701, 303, "viewer", now=1794237300)

        assert access.revoke_grant(owner, 701, 202, now=1794237400) is True
        assert access.authorize_account(manager, 701, "renew") is False
        with database.get_db() as conn:
            grant = conn.execute(
                "SELECT access_level,revoked_at,revision FROM account_access_grants "
                "WHERE account_id=701 AND grantee_tg_id=202"
            ).fetchone()
            account = conn.execute(
                "SELECT owner_tg_id,acl_revision FROM accounts WHERE id=701"
            ).fetchone()
            wallet = conn.execute(
                "SELECT balance_toman FROM user_wallets WHERE user_id=101"
            ).fetchone()[0]
            seller_balance = conn.execute(
                "SELECT balance_toman FROM representatives WHERE id=550"
            ).fetchone()[0]
        assert tuple(grant) == ("manager", 1794237400, 3)
        assert tuple(account) == (101, 4)
        assert wallet == 8101
        assert seller_balance == 123456

        root_grant = access.set_grant(root_actor, 701, 303, "viewer", now=1794237500)
        assert root_grant["access_level"] == "viewer"
        assert access.authorize_account(seller, 701, "grant") is True
        assert network_attempts == []
        assert not Path(sentinel).exists()
