"""Offline acceptance for exposed defective returns and low-stock events."""
from __future__ import annotations

import base64
from pathlib import Path
import importlib.util
import sys

import crypto_store
from goods_fulfillment import fulfill_inventory
from goods_service import (
    GoodsReturnError,
    import_inventory,
    mark_returned,
    reserve_inventory,
    save_product,
    low_stock_events,
)
from migrations_runner import apply_migrations


def _helpers():
    spec = importlib.util.spec_from_file_location(
        "_w13_returns_helpers", Path(__file__).with_name("v52_helpers.py")
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


def test_exposed_return_defective(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _helpers()
    attempts = helpers.guard_network(monkeypatch)
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", key)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w13-returns.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1794237000)) == 18
            conn.execute(
                "INSERT INTO bot_registry(bot_id,is_primary,username,status,verified_at,revision) "
                "VALUES(9001,1,'w13-returns','active',1794237000,1)"
            )
            conn.execute("INSERT INTO digital_categories(id,title) VALUES(13041,'Returns')")
        product = save_product(
            product_code="W13-RETURN-STOCK",
            category_id=13041,
            title="Low stock code",
            delivery_type="auto_inventory",
            rep_price=10,
            user_price=20,
            stock_warning_threshold=2,
        )
        inventory_ids = import_inventory(
            product["id"], ["W13-RETURN-ONE", "W13-RETURN-TWO"], now=1794237000
        )
        with database.get_db() as conn:
            _create_order(conn, "W13-RETURN-ORDER", product["id"], 1794237000)
        reserve_inventory(product["id"], "W13-RETURN-ORDER", now=1794237001)
        fulfill_inventory("W13-RETURN-ORDER", now=1794237002)
        returned = mark_returned(
            inventory_ids[0],
            reason="Synthetic defective test return",
            actor_id=303,
            actor_name="W13 operator",
            now=1794237003,
        )
        assert returned["status"] == "defective"
        events = low_stock_events()
        event = next(item for item in events if item["product_id"] == product["id"])
        assert event["available_count"] == 1
        assert event["threshold"] == 2
        with database.get_db() as conn:
            item = conn.execute(
                "SELECT status,exposed_at FROM digital_inventory WHERE id=?",
                (inventory_ids[0],),
            ).fetchone()
            audit = conn.execute(
                "SELECT details_cipher FROM audit_logs WHERE action='goods_return_defective'"
            ).fetchone()
            assert item["status"] == "defective"
            assert item["exposed_at"] == 1794237002
            assert audit is not None
            assert "Synthetic defective test return" not in audit["details_cipher"]
            assert crypto_store.decrypt_secret(
                audit["details_cipher"], "hstagent/columns/v1"
            ).find("Synthetic defective test return") >= 0
        try:
            mark_returned(
                inventory_ids[0],
                reason="duplicate return",
                now=1794237004,
            )
        except GoodsReturnError:
            pass
        else:
            raise AssertionError("a defective item must not be returned twice")
        assert Path(sentinel) != Path(database.DB_PATH)
    assert attempts == []
