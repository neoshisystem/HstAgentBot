"""Offline acceptance test for root identity and persona separation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from migrations_runner import apply_migrations


def _load_helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w05_identity_helpers", path)
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


def test_root_identity_and_idempotent_persona(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 7001)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w05-identity.sqlite3"):
        with database.get_db() as conn:
            assert apply_migrations(conn, root / "migrations", now=1794237000)

        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter) "
                "VALUES(77,'W05-DEMO','W05 demo',7077,'')"
            )
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,rep_id,username,is_blocked) "
                "VALUES(7001,'end_user',77,'hisystem',0)"
            )
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,username,is_blocked) "
                "VALUES(7002,'admin','hisystem',0)"
            )
            conn.execute(
                "INSERT INTO user_wallets(user_id,balance_toman) VALUES(7001,123456)"
            )
            conn.execute(
                "INSERT INTO user_settings(user_id,first_connect_mode,active_persona) "
                "VALUES(7001,1,'end_user')"
            )

        identity = _load_product_module(root, "identity_service.py", "identity_service", monkeypatch)
        with database.get_db() as conn:
            first = identity.ensure_personal_identity(conn, 7001)
        with database.get_db() as conn:
            second = identity.ensure_personal_identity(conn, 7001)
        assert first == second
        assert first["root_tg_id"] == 7001

        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM representatives WHERE rep_code='root_admin_rep'"
            ).fetchone()[0] == 1
            root_user = conn.execute(
                "SELECT role,rep_id,username FROM bot_users WHERE telegram_id=7001"
            ).fetchone()
            assert (root_user["role"], root_user["rep_id"], root_user["username"]) == (
                "end_user", 77, "hisystem"
            )
            assert conn.execute(
                "SELECT balance_toman FROM user_wallets WHERE user_id=7001"
            ).fetchone()[0] == 123456
            assert conn.execute(
                "SELECT first_connect_mode,active_persona FROM user_settings WHERE user_id=7001"
            ).fetchone()[:] == (1, "end_user")
            root = identity.resolve_actor(conn, 7001)
            impostor = identity.resolve_actor(conn, 7002)

        assert root.is_root is True
        assert root.persona == "end_user"
        assert root.actor_tg_id == 7001
        assert impostor.is_root is False
        assert impostor.persona == "end_user"
        assert network_attempts == []
        assert not Path(sentinel).exists()
