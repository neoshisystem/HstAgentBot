"""Offline acceptance for durable inventory and file fulfillment."""
from __future__ import annotations

import base64
from pathlib import Path
import importlib.util
import sys

import pytest

import crypto_store
from goods_fulfillment import fulfill_file, fulfill_inventory
from goods_service import import_inventory, reserve_inventory, save_product
from migrations_runner import apply_migrations


def _helpers():
    spec = importlib.util.spec_from_file_location(
        "_w13_delivery_helpers", Path(__file__).with_name("v52_helpers.py")
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_order(conn, order_id, product_id, now):
    quote_id = "Q-" + order_id
    conn.execute(
        """
        INSERT INTO quotes(
            quote_id,actor_tg_id,bot_id,merchant_key,purpose,product_id,amount_toman,
            spec_json,price_edges_json,policy_version,created_at,expires_at,fingerprint
        ) VALUES(?,101,9001,'root','digital_product_buy',?,0,'{}','[]','v1.0',?,?,?)
        """,
        (quote_id, product_id, now, now + 1800, "fp-" + order_id),
    )
    conn.execute(
        """
        INSERT INTO payment_orders(
            order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,payment_rail,
            amount_toman,status,received_toman,created_at,expires_at,revision
        ) VALUES(?,?,101,9001,'root','digital_product_buy','wallet',0,'accepted',0,?,?,1)
        """,
        (order_id, quote_id, now, now + 1800),
    )


def test_timeout_does_not_resell(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _helpers()
    attempts = helpers.guard_network(monkeypatch)
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", key)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w13-delivery.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1794237000)) == 18
            conn.execute(
                "INSERT INTO bot_registry(bot_id,is_primary,username,status,verified_at,revision) "
                "VALUES(9001,1,'w13-delivery','active',1794237000,1)"
            )
            conn.execute("INSERT INTO digital_categories(id,title) VALUES(13021,'Delivery')")
        auto_product = save_product(
            product_code="W13-AUTO-DELIVERY",
            category_id=13021,
            title="Auto code",
            delivery_type="auto_inventory",
            rep_price=10,
            user_price=20,
        )
        imported = import_inventory(auto_product["id"], ["W13-DELIVERY-SECRET"], now=1794237000)
        with database.get_db() as conn:
            _create_order(conn, "W13-DELIVERY-A", auto_product["id"], 1794237000)
            _create_order(conn, "W13-DELIVERY-B", auto_product["id"], 1794237000)
        reserve_inventory(auto_product["id"], "W13-DELIVERY-A", now=1794237001)

        def timeout(_payload):
            raise TimeoutError("synthetic delivery timeout")

        with pytest.raises(TimeoutError):
            fulfill_inventory("W13-DELIVERY-A", sender=timeout, now=1794237002)
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT status,exposed_at,order_id FROM digital_inventory WHERE id=?",
                (imported[0],),
            ).fetchone()
            assert row["status"] == "sold"
            assert row["exposed_at"] == 1794237002
            assert row["order_id"] == "W13-DELIVERY-A"
            fulfillment = conn.execute(
                "SELECT state,payload_cipher FROM goods_fulfillments WHERE order_id='W13-DELIVERY-A'"
            ).fetchone()
            assert fulfillment["state"] == "exception"
            assert "W13-DELIVERY-SECRET" not in fulfillment["payload_cipher"]
        assert reserve_inventory(
            auto_product["id"], "W13-DELIVERY-B", now=1794237003
        ) is None
        seen = []
        retried = fulfill_inventory(
            "W13-DELIVERY-A",
            sender=lambda payload: seen.append(payload) or "local-ack",
            now=1794237004,
        )
        assert seen == ["W13-DELIVERY-SECRET"]
        assert retried["payload"] == "W13-DELIVERY-SECRET"
        assert retried["state"] == "delivered"

        file_product = save_product(
            product_code="W13-FILE-DELIVERY",
            category_id=13021,
            title="File asset",
            delivery_type="file_document",
            rep_price=5,
            user_price=8,
            file_telegram_id="SYNTHETIC-TELEGRAM-FILE-ID",
            file_bot_id=9001,
        )
        with database.get_db() as conn:
            _create_order(conn, "W13-FILE-ORDER", file_product["id"], 1794237000)
        file_result = fulfill_file("W13-FILE-ORDER", now=1794237005)
        assert file_result["payload"]["file_telegram_id"] == "SYNTHETIC-TELEGRAM-FILE-ID"
        with database.get_db() as conn:
            cipher = conn.execute(
                "SELECT payload_cipher FROM goods_fulfillments WHERE order_id='W13-FILE-ORDER'"
            ).fetchone()["payload_cipher"]
            assert "SYNTHETIC-TELEGRAM-FILE-ID" not in cipher
        assert crypto_store.decrypt_secret(
            cipher, "hstagent/columns/v1"
        ).find("SYNTHETIC-TELEGRAM-FILE-ID") >= 0
        assert Path(sentinel) != Path(database.DB_PATH)
    assert attempts == []
