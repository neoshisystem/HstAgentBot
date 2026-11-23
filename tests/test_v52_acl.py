"""Offline acceptance tests for account permissions and root bypass."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from migrations_runner import apply_migrations


def _load_helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w05_acl_helpers", path)
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


def _seed_user(conn, telegram_id, *, persona="end_user", blocked=0, username=None):
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,username,is_blocked) VALUES(?,'end_user',?,?)",
        (telegram_id, username, blocked),
    )
    conn.execute(
        "INSERT INTO user_settings(user_id,first_connect_mode,active_persona) VALUES(?,0,?)",
        (telegram_id, persona),
    )


def test_permission_matrix_and_root_bypass(tmp_path, monkeypatch):
    root_path = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root_path, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9090)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w05-acl.sqlite3"):
        with database.get_db() as conn:
            assert apply_migrations(conn, root_path / "migrations", now=1794237000)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,parent_rep_id) "
                "VALUES(201,'W05-PARENT','W05 parent',199,'',NULL)"
            )
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,parent_rep_id) "
                "VALUES(202,'W05-SELLER','W05 seller',303,'',201)"
            )
            for actor_id, persona, blocked, username in [
                (101, "end_user", 0, "owner"),
                (102, "end_user", 0, "viewer"),
                (103, "end_user", 0, "manager"),
                (199, "rep", 0, "parent"),
                (303, "rep", 0, "seller"),
                (404, "end_user", 0, "stranger"),
                (405, "end_user", 1, "blocked"),
            ]:
                _seed_user(conn, actor_id, persona=persona, blocked=blocked, username=username)
            # Root is initialized from the configured Telegram ID, not role/username.
        helpers.seed_account(
            database, account_id=700, rep_id=202, owner_tg_id=101,
            account_code="W05-ACL-A", email="acl@example.invalid",
            sub_id="W05_SECRET_MUST_NOT_LEAK",
        )
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO account_access_grants(account_id,grantee_tg_id,access_level,"
                "granted_by_tg_id,created_at,revoked_at,revision) "
                "VALUES(700,102,'viewer',101,100,NULL,1)"
            )
            conn.execute(
                "INSERT INTO account_access_grants(account_id,grantee_tg_id,access_level,"
                "granted_by_tg_id,created_at,revoked_at,revision) "
                "VALUES(700,103,'manager',101,100,NULL,1)"
            )
        identity = _load_product_module(root_path, "identity_service.py", "identity_service", monkeypatch)
        access = _load_product_module(root_path, "access_service.py", "access_service", monkeypatch)
        with database.get_db() as conn:
            identity.ensure_personal_identity(conn, 9090)
            actors = {
                actor_id: identity.resolve_actor(conn, actor_id)
                for actor_id in (101, 102, 103, 199, 303, 404, 405, 9090)
            }

        expected = {
            "view_config": {
                101: True, 102: True, 103: True, 199: False,
                303: True, 404: False, 405: False, 9090: True,
            },
            "renew": {
                101: True, 102: False, 103: True, 199: False,
                303: True, 404: False, 405: False, 9090: True,
            },
            "rotate": {
                101: True, 102: False, 103: True, 199: False,
                303: True, 404: False, 405: False, 9090: True,
            },
            "grant": {
                101: True, 102: False, 103: False, 199: False,
                303: True, 404: False, 405: False, 9090: True,
            },
            "traffic": {
                101: False, 102: False, 103: False, 199: False,
                303: False, 404: False, 405: False, 9090: True,
            },
        }
        for action, matrix in expected.items():
            for actor_id, allowed in matrix.items():
                assert access.authorize_account(actors[actor_id], 700, action) is allowed

        assert actors[199].persona == "rep"
        assert access.authorize_account(actors[199], 700, "view_stats") is True
        parent_rows = access.list_visible_accounts(actors[199])
        assert parent_rows == [{
            "kind": "representative_summary",
            "rep_id": 202,
            "account_count": 1,
        }]
        assert "W05_SECRET_MUST_NOT_LEAK" not in repr(parent_rows)
        assert "sub_id" not in parent_rows[0]
        viewer_rows = access.list_visible_accounts(actors[102])
        assert len(viewer_rows) == 1
        assert "sub_id" not in viewer_rows[0]
        assert network_attempts == []
        assert not Path(sentinel).exists()
