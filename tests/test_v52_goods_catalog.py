"""Offline acceptance for encrypted digital catalog and category-cycle safety."""
from __future__ import annotations

import base64
from pathlib import Path
import importlib.util
import sys

import pytest

import crypto_store
from goods_service import GoodsValidationError, import_inventory, save_product
from migrations_runner import apply_migrations


def _prepare(root, monkeypatch, tmp_path):
    helpers_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w13_catalog_helpers", helpers_path)
    helpers = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(helpers)
    attempts = helpers.guard_network(monkeypatch)
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", key)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    return helpers, database, sentinel, attempts


def test_types_encryption_and_category_cycle(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel, network_attempts = _prepare(root, monkeypatch, tmp_path)
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w13-catalog.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1794237000)) == 18
            conn.execute("INSERT INTO digital_categories(id,title) VALUES(13001,'W13 root')")
            conn.execute(
                "INSERT INTO digital_categories(id,title,parent_id) VALUES(13002,'W13 child',13001)"
            )

        product = save_product(
            {
                "product_code": "W13-CATALOG-AUTO",
                "category_id": 13002,
                "title": "Encrypted digital code",
                "delivery_type": "auto_inventory",
                "rep_price": 100,
                "user_price": 150,
                "stock_warning_threshold": 2,
            }
        )
        assert product["delivery_type"] == "auto_inventory"
        ids = import_inventory(
            product["id"],
            ["W13-SECRET-ONE", {"secret": "W13-SECRET-TWO", "notes": "synthetic"}],
            now=1794237001,
        )
        assert len(ids) == 2
        with database.get_db() as conn:
            rows = conn.execute(
                "SELECT secret_cipher,notes,status FROM digital_inventory WHERE product_id=? ORDER BY id",
                (product["id"],),
            ).fetchall()
            assert len(rows) == 2
            assert all(row["status"] == "available" for row in rows)
            assert all("W13-SECRET" not in row["secret_cipher"] for row in rows)
            assert crypto_store.decrypt_secret(
                rows[0]["secret_cipher"], "hstagent/columns/v1"
            ) == "W13-SECRET-ONE"
            assert rows[1]["notes"] == "synthetic"

            conn.execute("UPDATE digital_categories SET parent_id=13002 WHERE id=13001")
        with pytest.raises(GoodsValidationError, match="cycle"):
            save_product(
                product,
                product_id=product["id"],
                title="Should reject cyclic category",
            )
        with pytest.raises(GoodsValidationError, match="delivery_type"):
            save_product(
                {
                    "product_code": "W13-BAD-TYPE",
                    "category_id": 13002,
                    "title": "Invalid type",
                    "delivery_type": "external",
                    "rep_price": 0,
                    "user_price": 0,
                }
            )
        assert Path(sentinel) != Path(database.DB_PATH)
    assert network_attempts == []
