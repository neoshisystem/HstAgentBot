"""Offline acceptance for competing manual operators and encrypted payloads."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import importlib.util
import sys
import threading

import crypto_store
from goods_fulfillment import claim_manual_order, complete_manual_order
from goods_service import save_product
from migrations_runner import apply_migrations


def _helpers():
    spec = importlib.util.spec_from_file_location(
        "_w13_manual_helpers", Path(__file__).with_name("v52_helpers.py")
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_two_operators_one_payload(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _helpers()
    attempts = helpers.guard_network(monkeypatch)
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", key)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w13-manual.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1794237000)) == 18
            conn.execute(
                "INSERT INTO bot_registry(bot_id,is_primary,username,status,verified_at,revision) "
                "VALUES(9001,1,'w13-manual','active',1794237000,1)"
            )
            conn.execute("INSERT INTO digital_categories(id,title) VALUES(13031,'Manual')")
        product = save_product(
            product_code="W13-MANUAL",
            category_id=13031,
            title="Manual setup",
            delivery_type="manual_fulfill",
            rep_price=0,
            user_price=0,
        )
        with database.get_db() as conn:
            quote_id = "Q-W13-MANUAL-ORDER"
            conn.execute(
                """
                INSERT INTO quotes(
                    quote_id,actor_tg_id,bot_id,merchant_key,purpose,product_id,amount_toman,
                    spec_json,price_edges_json,policy_version,created_at,expires_at,fingerprint
                ) VALUES(?,101,9001,'root','digital_product_buy',?,0,'{}','[]','v1.0',1794237000,1794238800,'fp-manual')
                """,
                (quote_id, product["id"]),
            )
            conn.execute(
                """
                INSERT INTO payment_orders(
                    order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,payment_rail,
                    amount_toman,status,received_toman,created_at,expires_at,revision
                ) VALUES('W13-MANUAL-ORDER',?,101,9001,'root','digital_product_buy',
                         'wallet',0,'accepted',0,1794237000,1794238800,1)
                """,
                (quote_id,),
            )

        barrier = threading.Barrier(2)

        def compete(operator_id):
            barrier.wait(timeout=10)
            return claim_manual_order(
                "W13-MANUAL-ORDER", operator_id, lease_seconds=60, now=1794237001
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(compete, (7001, 7002)))
        claims = [claim for claim in outcomes if claim is not None]
        assert len(claims) == 1
        winner = claims[0]
        completed = complete_manual_order(
            "W13-MANUAL-ORDER",
            winner["operator_id"],
            "MANUAL-DELIVERY-PAYLOAD",
            claim_token=winner["claim_token"],
            now=1794237002,
        )
        assert completed["state"] == "ready"
        assert completed["replayed"] is False
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT state,payload_cipher,claim_token,lease_until FROM goods_fulfillments "
                "WHERE order_id='W13-MANUAL-ORDER'"
            ).fetchone()
            assert row["state"] == "ready"
            assert row["claim_token"] is None
            assert row["lease_until"] is None
            assert "MANUAL-DELIVERY-PAYLOAD" not in row["payload_cipher"]
            assert crypto_store.decrypt_secret(
                row["payload_cipher"], "hstagent/columns/v1"
            ) == "MANUAL-DELIVERY-PAYLOAD"
        assert Path(sentinel) != Path(database.DB_PATH)
    assert attempts == []
