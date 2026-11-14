"""Offline acceptance for the last-item race and hold lifetime."""
from __future__ import annotations

import base64
from pathlib import Path
import importlib.util
import sys

from goods_service import import_inventory, release_inventory, reserve_inventory, save_product
from migrations_runner import apply_migrations


def _helpers():
    spec = importlib.util.spec_from_file_location(
        "_w13_stock_helpers", Path(__file__).with_name("v52_helpers.py")
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


def test_last_item_and_hold_lifetime(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _helpers()
    attempts = helpers.guard_network(monkeypatch)
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", key)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w13-stock.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1794237000)) == 18
            conn.execute(
                "INSERT INTO bot_registry(bot_id,is_primary,username,status,verified_at,revision) "
                "VALUES(9001,1,'w13-stock','active',1794237000,1)"
            )
            conn.execute("INSERT INTO digital_categories(id,title) VALUES(13011,'Stock')")
        product = save_product(
            product_code="W13-LAST-ITEM",
            category_id=13011,
            title="One remaining code",
            delivery_type="auto_inventory",
            rep_price=10,
            user_price=20,
        )
        inventory_ids = import_inventory(product["id"], ["ONLY-ONE-CODE"], now=1794237000)
        with database.get_db() as conn:
            _create_order(conn, "W13-ORDER-A", product["id"], 1794237000)
            _create_order(conn, "W13-ORDER-B", product["id"], 1794237000)
        first = reserve_inventory(
            product["id"], "W13-ORDER-A", hold_seconds=60, now=1794237001
        )
        assert first["inventory_id"] == inventory_ids[0]
        assert first["reservation_until"] == 1794237061
        assert reserve_inventory(
            product["id"], "W13-ORDER-B", hold_seconds=60, now=1794237060
        ) is None
        second = reserve_inventory(
            product["id"], "W13-ORDER-B", hold_seconds=60, now=1794237061
        )
        assert second["inventory_id"] == inventory_ids[0]
        assert second["order_id"] == "W13-ORDER-B"
        assert release_inventory("W13-ORDER-B")
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT status,order_id,reservation_until FROM digital_inventory WHERE id=?",
                (inventory_ids[0],),
            ).fetchone()
            assert row["status"] == "available"
            assert row["order_id"] is None
            assert row["reservation_until"] is None
        assert Path(sentinel) != Path(database.DB_PATH)
    assert attempts == []
