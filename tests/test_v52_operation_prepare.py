"""Offline acceptance for atomic, encrypted operation preparation."""
from __future__ import annotations
import base64
import importlib.util
import json
from pathlib import Path
import sys
import pytest
from confirmation_service import create_confirmation
from crypto_store import decrypt_secret
from migrations_runner import apply_migrations

def _load_helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w092_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _load_module(root: Path, name: str, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, root / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module

def _prepare_fixture(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    _load_module(root, "access_service", monkeypatch)
    operations = _load_module(root, "operation_service", monkeypatch)
    now = 1794237000
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w092-prepare.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=now)) == 18
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        helpers.seed_account(
            database, account_id=303, rep_id=202, owner_tg_id=101,
            account_code="W09-ACCOUNT-303", email="w09-303@example.invalid",
            sub_id="W09_SECRET_ACCOUNT_VALUE",
        )
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,NULL,'w09-primary','active',?,1)", (now,)
            )
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,lifecycle,for_end_users,for_reps) "
                "VALUES(501,'W09-PANEL','W09 synthetic panel','3x-ui','active',1,1)"
            )
            conn.execute("UPDATE accounts SET panel_id=501,seller_key='root' WHERE id=303")
            conn.execute(
                "INSERT INTO quotes(quote_id,actor_tg_id,bot_id,merchant_key,purpose,account_id,panel_id,"
                "amount_toman,spec_json,price_edges_json,policy_version,created_at,expires_at,fingerprint) "
                "VALUES('w09-quote',101,9001,'root','vpn_renew',303,501,1200,'{}','[]','v1.0',?,?,?)",
                (now, now + 1800, "w09-fingerprint"),
            )
            conn.execute(
                "INSERT INTO payment_orders(order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,"
                "payment_rail,amount_toman,status,received_toman,created_at,expires_at,revision) "
                "VALUES('w09-order','w09-quote',101,9001,'root','vpn_renew','card_to_card',"
                "1200,'accepted',1200,?,?,1)", (now, now + 1800)
            )
            confirmation = create_confirmation(
                conn, actor_tg_id=101, bot_id=9001, action="panel.operation.prepare",
                target_type="account", target_id=303, expected_revision=1,
                payload={"account_id": 303, "panel_id": 501, "quota_bytes": 10737418240},
                now=now, ttl_seconds=600,
            )
    # make_db restores DB_PATH on exit; keep this isolated DB active for the full test.
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "w092-prepare.sqlite3"))
    return {
        "root": root, "helpers": helpers, "database": database,
        "sentinel": sentinel, "operations": operations, "confirmation": confirmation,
        "now": now, "network_attempts": network_attempts,
    }

def test_persist_before_network(tmp_path, monkeypatch):
    f = _prepare_fixture(tmp_path, monkeypatch)
    db, ops, now = f["database"], f["operations"], f["now"]
    request = {
        "method": "renew_client",
        "params": {"candidate_email": "w09-303@example.invalid", "credential": "W09_PRIVATE_REQUEST"},
    }
    effect = {
        "snapshot": {
            "account_id": 303, "panel_id": 501, "account_revision": 1,
            "quota_mode": "finite", "quota_bytes": 10737418240, "used_bytes": 100,
            "time_mode": "dated", "expires_at": now + 86400,
        },
        "target": {"quota_bytes": 21474836480, "expires_at": now + 30 * 86400},
    }
    with db.get_db() as conn:
        prepared = ops.prepare_operation(
            conn, confirmation_nonce=f["confirmation"]["nonce"],
            actor_tg_id=101, bot_id=9001, order_id="w09-order", account_id=303,
            kind="vpn_renew", request=request, expected_effect=effect,
            idempotency_key="w09-idem-303", now=now + 1,
        )
        assert prepared["state"] == "prepared" and prepared["generation"] == 0
        assert prepared["lock_key"] == "account:303" and prepared["candidate_id"]
        assert prepared["request"] == request
        assert prepared["expected_effect"]["snapshot"]["account_revision"] == 1
        assert len(prepared["expected_effect"]["snapshot_sha256"]) == 64
        stored = conn.execute(
            "SELECT request_cipher,state,lock_key,lock_token,idempotency_key "
            "FROM panel_operations WHERE operation_id=?", (prepared["operation_id"],)
        ).fetchone()
        assert stored["state"] == "prepared" and stored["lock_key"] == "account:303"
        assert stored["lock_token"] and "W09_PRIVATE_REQUEST" not in stored["request_cipher"]
        decoded = json.loads(decrypt_secret(stored["request_cipher"], "hstagent/columns/v1"))
        assert decoded["candidate_id"] == prepared["candidate_id"] and decoded["request"] == request
        assert tuple(conn.execute(
            "SELECT status,revision FROM payment_orders WHERE order_id='w09-order'"
        ).fetchone()) == ("accepted", 2)
        assert tuple(conn.execute(
            "SELECT consumed_at,operation_id FROM action_confirmations WHERE nonce=?",
            (f["confirmation"]["nonce"],),
        ).fetchone()) == (now + 1, prepared["operation_id"])

    with db.get_db() as conn:
        replay = ops.prepare_operation(
            conn, confirmation_nonce=f["confirmation"]["nonce"],
            actor_tg_id=101, bot_id=9001, order_id="w09-order", account_id=303,
            kind="vpn_renew", request=request, expected_effect=effect,
            idempotency_key="w09-idem-303", now=now + 2,
        )
        assert replay["replayed"] is True
        assert replay["operation_id"] == prepared["operation_id"]
        assert replay["candidate_id"] == prepared["candidate_id"]
        assert conn.execute("SELECT COUNT(*) FROM panel_operations").fetchone()[0] == 1
        assert conn.execute(
            "SELECT revision FROM payment_orders WHERE order_id='w09-order'"
        ).fetchone()[0] == 2

    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO bot_users(telegram_id,role,rep_id,username,full_name,is_blocked) "
            "VALUES(102,'end_user',202,'w09-stranger','W09 stranger',0)"
        )
        conn.execute(
            "INSERT INTO quotes(quote_id,actor_tg_id,bot_id,merchant_key,purpose,account_id,panel_id,"
            "amount_toman,spec_json,price_edges_json,policy_version,created_at,expires_at,fingerprint) "
            "VALUES('w09-quote-2',102,9001,'root','vpn_renew',303,501,1200,'{}','[]','v1.0',?,?,?)",
            (now, now + 1800, "w09-fingerprint-2"),
        )
        conn.execute(
            "INSERT INTO payment_orders(order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,"
            "payment_rail,amount_toman,status,received_toman,created_at,expires_at,revision) "
            "VALUES('w09-order-2','w09-quote-2',102,9001,'root','vpn_renew','card_to_card',"
            "1200,'accepted',1200,?,?,1)", (now, now + 1800)
        )
        stale = create_confirmation(
            conn, actor_tg_id=102, bot_id=9001, action="panel.operation.prepare",
            target_type="account", target_id=303, expected_revision=1,
            payload={"account_id": 303, "panel_id": 501}, now=now + 2,
        )
    with pytest.raises(ops.OperationUnauthorized):
        with db.get_db() as conn:
            ops.prepare_operation(
                conn, confirmation_nonce=stale["nonce"], actor_tg_id=102, bot_id=9001,
                order_id="w09-order-2", account_id=303, kind="vpn_renew",
                request=request, expected_effect=effect,
                idempotency_key="w09-idem-stranger", now=now + 3,
            )
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM panel_operations").fetchone()[0] == 1
        assert conn.execute(
            "SELECT consumed_at FROM action_confirmations WHERE nonce=?", (stale["nonce"],)
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT revision FROM payment_orders WHERE order_id='w09-order-2'"
        ).fetchone()[0] == 1
    assert f["network_attempts"] == [] and not Path(f["sentinel"]).exists()
