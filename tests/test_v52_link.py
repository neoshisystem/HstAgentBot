"""Offline acceptance tests for atomic and unambiguous account linking."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import sys

from migrations_runner import apply_migrations


def _load_helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w05_link_helpers", path)
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


def _insert_account(conn, account_id, code, email, *, panel_id=None, owner=None):
    conn.execute(
        "INSERT INTO accounts(id,account_code,email,customer_name,sub_id,enable,total_gb,"
        "expiry_time,owner_tg_id,panel_id) VALUES(?,?,?,?,?,1,10,0,?,?)",
        (account_id, code, email, f"W05 account {account_id}",
         f"W05_SECRET_{account_id}", owner, panel_id),
    )


def test_two_claimants_and_ambiguous_code(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9090)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w05-link.sqlite3"):
        with database.get_db() as conn:
            assert apply_migrations(conn, root / "migrations", now=1794237000)
            for actor_id in (101, 501, 502, 601, 9090):
                _seed_user(conn, actor_id)
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,is_default,created_at) "
                "VALUES(1,'W05-PANEL-A','W05 panel A','3x-ui',0,1794237000)"
            )
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,is_default,created_at) "
                "VALUES(2,'W05-PANEL-B','W05 panel B','3x-ui',0,1794237000)"
            )
            _insert_account(conn, 710, "W05-SHARED", "shared@example.invalid")
            _insert_account(conn, 720, "W05-OWNED", "owned@example.invalid", owner=101)
            _insert_account(
                conn, 730, "W05-DUPLICATE", "dup-a@example.invalid",
                panel_id=1,
            )
            _insert_account(
                conn, 731, "w05-duplicate", "dup-b@example.invalid",
                panel_id=2,
            )
            conn.execute(
                "INSERT INTO account_access_grants(account_id,grantee_tg_id,access_level,"
                "granted_by_tg_id,created_at,revoked_at,revision) "
                "VALUES(720,501,'viewer',101,1794237000,NULL,1)"
            )
        identity = _load_product_module(root, "identity_service.py", "identity_service", monkeypatch)
        linker = _load_product_module(root, "account_link_service.py", "account_link_service", monkeypatch)
        with database.get_db() as conn:
            identity.ensure_personal_identity(conn, 9090)
            claimant_a = identity.resolve_actor(conn, 501)
            claimant_b = identity.resolve_actor(conn, 502)

        candidates = linker.resolve_link_candidates(claimant_a, "  w05-shared  ")
        assert candidates["status"] == "resolved"
        assert candidates["account_ids"] == [710]

        # Separate SQLite connections race on the same ownerless account. BEGIN
        # IMMEDIATE plus the WHERE owner_tg_id IS NULL CAS permits one winner.
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda actor: linker.claim_or_access(actor, 710),
                (claimant_a, claimant_b),
            ))
        assert sorted(item["status"] for item in results) == ["claimed", "denied"]
        with database.get_db() as conn:
            owner = conn.execute(
                "SELECT owner_tg_id,acl_revision FROM accounts WHERE id=710"
            ).fetchone()
        winner = int(owner["owner_tg_id"])
        assert winner in {501, 502}
        assert int(owner["acl_revision"]) == 2

        grant_access = linker.claim_or_access(claimant_a, 720)
        assert grant_access["status"] == "grant_access"
        assert grant_access["access_level"] == "viewer"
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT owner_tg_id FROM accounts WHERE id=720"
            ).fetchone()[0] == 101

        ambiguous = linker.resolve_link_candidates(claimant_b, "W05-DUPLICATE")
        assert ambiguous["status"] == "ambiguous"
        assert ambiguous["account_ids"] == [730, 731]
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE id IN (730,731) AND owner_tg_id IS NULL"
            ).fetchone()[0] == 2
        email_match = linker.resolve_link_candidates(claimant_b, "DUP-A@EXAMPLE.INVALID")
        assert email_match["status"] == "resolved"
        assert email_match["account_ids"] == [730]
        assert network_attempts == []
        assert not Path(sentinel).exists()
