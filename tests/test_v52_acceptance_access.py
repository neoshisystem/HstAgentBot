"""W20.3 integrated multi-bot, ACL, profile rotation and subscription gateway acceptance."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from migrations_runner import apply_migrations

from crypto_store import encrypt_secret


def _helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w20_access_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(root, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, root / (name + ".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_multibot_grant_profile_revoke(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _helpers()
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9090, raising=False)

    access = _load(root, "access_service", monkeypatch)
    identity = _load(root, "identity_service", monkeypatch)
    subscriptions = _load(root, "subscription_service", monkeypatch)
    confirmations = _load(root, "confirmation_service", monkeypatch)
    bots = _load(root, "bot_context_service", monkeypatch)
    quotes = _load(root, "quote_service", monkeypatch)
    gateway = _load(root, "gateway_service", monkeypatch)
    sub_gateway = _load(root, "sub_gateway", monkeypatch)

    now = 1794500000
    with helpers.make_db(
        database, monkeypatch, tmp_path, filename="w20-access.sqlite3"
    ):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=now)) == 18
            helpers.seed_actor(database, actor_tg_id=101, rep_id=201, rep_tg_id=303)
            conn.execute(
                "INSERT INTO representatives("
                "id,rep_code,name,telegram_id,group_id,topic_id,scope_filter,parent_rep_id,"
                "tier,billing_mode,markup_bps,is_active,deleted_at"
                ") VALUES(204,'W20-REPB','W20 repB',404,0,0,'w20:repb',201,"
                "'normal','credit',0,1,NULL)"
            )
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,rep_id,username,full_name,is_blocked) "
                "VALUES(202,'end_user',NULL,'w20_viewer','W20 viewer',0)"
            )
            conn.execute(
                "INSERT INTO user_wallets(user_id,balance_toman) VALUES(202,0)"
            )
            conn.execute(
                "INSERT INTO user_settings(user_id,first_connect_mode,active_persona) "
                "VALUES(202,0,'end_user')"
            )
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,rep_id,username,full_name,is_blocked) "
                "VALUES(303,'rep',201,'w20_manager','W20 manager',0)"
            )
            conn.execute(
                "INSERT INTO user_wallets(user_id,balance_toman) VALUES(303,0)"
            )
            conn.execute(
                "INSERT INTO user_settings(user_id,first_connect_mode,active_persona) "
                "VALUES(303,0,'rep')"
            )
            for bot_id, rep_id, primary, username in (
                (9001, None, 1, "w20_primary"),
                (9002, 201, 0, "w20_rep_a"),
                (9003, 204, 0, "w20_rep_b"),
            ):
                conn.execute(
                    "INSERT INTO bot_registry("
                    "bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision"
                    ") VALUES(?,?,?,?,?,'active',?,1)",
                    (
                        bot_id, rep_id, primary,
                        encrypt_secret(f"W20_BOT_TOKEN_{bot_id}", "hstagent/columns/v1"),
                        username, now,
                    ),
                )
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,lifecycle,max_clients) "
                "VALUES(501,'W20-PA','W20 panel A','3x-ui','active',100)"
            )
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,lifecycle,max_clients) "
                "VALUES(502,'W20-PB','W20 panel B','3x-ui','active',100)"
            )

        helpers.seed_account(
            database, account_id=700, rep_id=201, owner_tg_id=101,
            account_code="W20-REPA-700", email="w20-a@example.invalid",
            sub_id="W20_SECRET_REPA_ACCOUNT",
        )
        helpers.seed_account(
            database, account_id=701, rep_id=204, owner_tg_id=101,
            account_code="W20-REPB-701", email="w20-b@example.invalid",
            sub_id="W20_SECRET_REPB_ACCOUNT",
        )
        with database.get_db() as conn:
            conn.execute(
                "UPDATE accounts SET panel_id=501,seller_key='rep:201' WHERE id=700"
            )
            conn.execute(
                "UPDATE accounts SET panel_id=502,seller_key='rep:204' WHERE id=701"
            )
            identity.ensure_personal_identity(conn, 9090)

        # Resolve every principal through the canonical identity service.
        with database.get_db() as conn:
            owner = identity.resolve_actor(conn, 101)
            viewer = identity.resolve_actor(conn, 202)
            manager = identity.resolve_actor(conn, 303)
            root_actor = identity.resolve_actor(conn, 9090)

        assert access.set_grant(owner, 700, 202, "viewer", now=now + 1)["access_level"] == "viewer"
        assert access.set_grant(owner, 701, 202, "viewer", now=now + 2)["access_level"] == "viewer"
        assert access.set_grant(owner, 700, 303, "manager", now=now + 3)["access_level"] == "manager"
        assert access.authorize_account(owner, 700, "view_config") is True
        assert access.authorize_account(viewer, 700, "view_config") is True
        assert access.authorize_account(viewer, 700, "renew") is False
        assert access.authorize_account(manager, 700, "renew") is True
        assert access.authorize_account(root_actor, 701, "view_config") is True

        # The parent sees a count-only child aggregate, never the child's account
        # identifier, private subscription field or credential.
        parent_rows = access.list_visible_accounts(manager)
        child_summary = next(
            item for item in parent_rows
            if item.get("kind") == "representative_summary" and item.get("rep_id") == 204
        )
        assert child_summary == {
            "kind": "representative_summary", "rep_id": 204, "account_count": 1
        }
        assert access.authorize_account(manager, 701, "view_config") is False
        assert all(item.get("id") != 701 for item in parent_rows)
        root_rows = access.list_visible_accounts(root_actor)
        assert {item["id"] for item in root_rows if "id" in item} >= {700, 701}
        assert all("sub_id" not in item and "sub_id_cipher" not in item for item in root_rows)

        # One aggregate profile spans the two panels for the viewer.
        with database.get_db() as conn:
            profile = subscriptions.create_profile(
                conn, 202, "W20 two-panel profile", now=now + 4
            )
            subscriptions.add_profile_member(
                conn, 202, profile.profile_id, 700, now=now + 5
            )
            subscriptions.add_profile_member(
                conn, 202, profile.profile_id, 701, now=now + 6
            )
            initial_token = profile.token
            revision = int(conn.execute(
                "SELECT revision FROM subscription_profiles WHERE profile_id=?",
                (profile.profile_id,),
            ).fetchone()[0])
            conn.execute("BEGIN IMMEDIATE")
            confirmation = confirmations.create_confirmation(
                conn, actor_tg_id=202, bot_id=9001,
                action="profile.rotate", target_type="subscription_profile",
                target_id=profile.profile_id, expected_revision=revision,
                payload={"profile_id": profile.profile_id, "revision": revision},
                now=now + 7,
            )
            conn.commit()
            rotated = subscriptions.rotate_profile_token(
                conn, 202, profile.profile_id,
                nonce=confirmation["nonce"], now=now + 8,
            )
            current_token = rotated.token
            assert current_token != initial_token
            assert subscriptions.resolve_profile_by_token(conn, initial_token) is None
            assert subscriptions.resolve_profile_by_token(conn, current_token)["profile_id"] == profile.profile_id

        # The primary and both representative bots have independent tenant identity.
        with database.get_db() as conn:
            rep_a_context = bots.resolve_bot_context(
                conn, SimpleNamespace(bot=SimpleNamespace(id=9002))
            )
            rep_b_context = bots.resolve_bot_context(
                conn, SimpleNamespace(bot=SimpleNamespace(id=9003))
            )
            assert rep_a_context.rep_id == 201
            assert rep_b_context.rep_id == 204
            with pytest.raises(bots.CheckoutRouteDenied):
                bots.authorize_checkout_route(
                    conn, SimpleNamespace(bot=SimpleNamespace(id=9002)),
                    actor_tg_id=202, action="renew", account_id=701,
                )
            with pytest.raises(quotes.QuoteTenantMismatch):
                quotes.create_quote(
                    conn, actor_tg_id=202, bot_id=9002, merchant_key="rep:204",
                    purpose="vpn_renew", amount_toman=100,
                    spec={"purpose": "vpn_renew", "payer_tg_id": 202},
                    price_edges=[], account_id=701, panel_id=502, now=now + 9,
                )

        class Adapter:
            async def get_client_configs(self, email):
                return [SimpleNamespace(uri=f"vless://w20@{email}:443")]

        class AdapterFactory:
            def resolve_account_adapter(self, account_id):
                return Adapter()

        app = sub_gateway.create_app(
            db_factory=lambda: database.get_db(),
            adapter_factory=AdapterFactory(),
            canonical_host="subscriptions.example",
        )

        async def exercise_gateway():
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                old = await client.get(
                    f"/s/{initial_token}?format=raw",
                    headers={"Host": "subscriptions.example"},
                )
                assert old.status == 404

                # This request represents a non-Telegram consumer; a valid token
                # is the only required client credential for subscription fetch.
                fetched = await client.get(
                    f"/s/{current_token}?format=raw",
                    headers={"Host": "subscriptions.example"},
                )
                assert fetched.status == 200
                body = await fetched.text()
                assert "w20-a@example.invalid" in body
                assert "w20-b@example.invalid" in body

                # Revocation is checked live on every fetch, even with a warm cache.
                assert access.revoke_grant(owner, 700, 202, now=now + 10) is True
                after_revoke = await client.get(
                    f"/s/{current_token}?format=raw",
                    headers={"Host": "subscriptions.example"},
                )
                assert after_revoke.status == 200
                remaining = await after_revoke.text()
                assert "w20-a@example.invalid" not in remaining
                assert "w20-b@example.invalid" in remaining
            finally:
                await client.close()

        asyncio.run(exercise_gateway())
        assert sentinel != database.DB_PATH
