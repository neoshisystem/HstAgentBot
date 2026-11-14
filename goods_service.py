"""Offline backend services for the digital-goods catalog and inventory.

The service reuses the schema introduced by migrations 012 and 017. It does not
own Telegram handlers, perform network I/O, or alter the database schema.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import time
from typing import Any, Iterable, Mapping

from crypto_store import encrypt_secret

_COLUMNS_LABEL = "hstagent/columns/v1"
_DELIVERY_TYPES = frozenset({"auto_inventory", "file_document", "manual_fulfill"})
_MAX_HOLD_SECONDS = 86400


class GoodsError(ValueError):
    """Base class for safe digital-goods domain errors."""


class GoodsValidationError(GoodsError):
    """Raised for invalid catalog, inventory, or category data."""


class InventoryUnavailable(GoodsError):
    """Raised when an inventory transition is not valid."""


class GoodsReturnError(GoodsError):
    """Raised when an item cannot be marked as a defective return."""


@contextmanager
def _database_connection(conn=None):
    """Use an injected connection or the application's configured SQLite DB."""
    if conn is not None:
        yield conn
        return
    import database
    with database.get_db() as managed:
        yield managed


@contextmanager
def _atomic(conn):
    """Use BEGIN IMMEDIATE when this service owns the transaction."""
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        raise


def _timestamp(value=None) -> int:
    value = int(time.time()) if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GoodsValidationError("timestamp must be non-negative integer epoch seconds")
    return value


def _integer(value, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise GoodsValidationError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise GoodsValidationError(f"{name} must be an integer") from None
    if result != value or result < minimum:
        raise GoodsValidationError(f"{name} must be at least {minimum}")
    return result


def _assert_category_chain(conn, category_id: int) -> None:
    """Reject missing categories and parent cycles before saving a product."""
    current = category_id
    visited: set[int] = set()
    while current is not None:
        if current in visited:
            raise GoodsValidationError("category parent cycle detected")
        visited.add(current)
        row = conn.execute(
            "SELECT parent_id FROM digital_categories WHERE id=?", (current,)
        ).fetchone()
        if row is None:
            raise GoodsValidationError("category does not exist")
        current = row["parent_id"]


def save_product(
    product: Mapping[str, Any] | None = None,
    *,
    product_id: int | None = None,
    conn=None,
    **fields,
) -> dict[str, Any]:
    """Create or update a digital product after validating its category path.

    The product argument may be a mapping or callers may pass the fields as
    keywords. The returned row is a plain dictionary without inventory payload.
    """
    data = dict(product or {})
    data.update(fields)
    code = str(data.get("product_code", "")).strip()
    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "") or "")
    delivery_type = str(data.get("delivery_type", "")).strip()
    if not code or len(code) > 80:
        raise GoodsValidationError("product_code is required and must be at most 80 characters")
    if not title or len(title) > 200:
        raise GoodsValidationError("title is required and must be at most 200 characters")
    if delivery_type not in _DELIVERY_TYPES:
        raise GoodsValidationError("unsupported digital delivery_type")
    category_id = _integer(data.get("category_id"), "category_id", minimum=1)
    rep_price = _integer(data.get("rep_price", 0), "rep_price")
    user_price = _integer(data.get("user_price", 0), "user_price")
    threshold = _integer(data.get("stock_warning_threshold", 3), "stock_warning_threshold")
    sort_order = _integer(data.get("sort_order", 0), "sort_order")
    is_active = 1 if bool(data.get("is_active", True)) else 0
    file_telegram_id = data.get("file_telegram_id")
    file_bot_id = data.get("file_bot_id")
    if file_telegram_id is not None:
        file_telegram_id = str(file_telegram_id).strip() or None
    if file_bot_id is not None:
        file_bot_id = _integer(file_bot_id, "file_bot_id", minimum=1)

    with _database_connection(conn) as db, _atomic(db):
        _assert_category_chain(db, category_id)
        values = (
            code, category_id, title, description, delivery_type, rep_price,
            user_price, file_telegram_id, file_bot_id, threshold, is_active, sort_order,
        )
        if product_id is None:
            cursor = db.execute(
                """
                INSERT INTO digital_products(
                    product_code,category_id,title,description,delivery_type,
                    rep_price,user_price,file_telegram_id,file_bot_id,
                    stock_warning_threshold,is_active,sort_order
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            saved_id = int(cursor.lastrowid)
        else:
            saved_id = _integer(product_id, "product_id", minimum=1)
            cursor = db.execute(
                """
                UPDATE digital_products SET
                    product_code=?,category_id=?,title=?,description=?,delivery_type=?,
                    rep_price=?,user_price=?,file_telegram_id=?,file_bot_id=?,
                    stock_warning_threshold=?,is_active=?,sort_order=?
                WHERE id=?
                """,
                (*values, saved_id),
            )
            if cursor.rowcount != 1:
                raise GoodsValidationError("product does not exist")
        row = db.execute(
            "SELECT * FROM digital_products WHERE id=?", (saved_id,)
        ).fetchone()
        return dict(row)


def import_inventory(
    product_id: int,
    items: Iterable[str | Mapping[str, Any]],
    *,
    notes: str = "",
    now: int | None = None,
    conn=None,
) -> list[int]:
    """Encrypt and atomically import inventory secrets for auto-inventory products."""
    product_id = _integer(product_id, "product_id", minimum=1)
    timestamp = _timestamp(now)
    if isinstance(items, (str, bytes, Mapping)):
        items = [items]
    prepared: list[tuple[str, str]] = []
    for item in items:
        if isinstance(item, Mapping):
            secret = item.get("secret", item.get("payload"))
            item_notes = str(item.get("notes", notes) or "")
        else:
            secret = item
            item_notes = str(notes or "")
        if not isinstance(secret, str) or not secret.strip():
            raise GoodsValidationError("each inventory item must contain a non-empty text secret")
        prepared.append((encrypt_secret(secret, _COLUMNS_LABEL), item_notes))
    if not prepared:
        return []

    with _database_connection(conn) as db, _atomic(db):
        product = db.execute(
            "SELECT delivery_type,is_active FROM digital_products WHERE id=?",
            (product_id,),
        ).fetchone()
        if product is None or not product["is_active"]:
            raise GoodsValidationError("active product does not exist")
        if product["delivery_type"] != "auto_inventory":
            raise GoodsValidationError("inventory secrets require auto_inventory delivery")
        inserted: list[int] = []
        for cipher, item_notes in prepared:
            cursor = db.execute(
                """
                INSERT INTO digital_inventory(
                    product_id,secret_cipher,notes,status,created_at
                ) VALUES(?,?,?,'available',?)
                """,
                (product_id, cipher, item_notes, timestamp),
            )
            inserted.append(int(cursor.lastrowid))
        return inserted


def reserve_inventory(
    product_id: int,
    order_id: str,
    *,
    hold_seconds: int = 900,
    now: int | None = None,
    conn=None,
) -> dict[str, Any] | None:
    """Atomically reserve one available item, including expiry reclamation.

    An order can hold at most one item. Expired, unexposed holds are returned to
    stock before selecting the next item. Exposed and sold items are never reclaimed.
    """
    product_id = _integer(product_id, "product_id", minimum=1)
    order_id = str(order_id).strip()
    if not order_id:
        raise GoodsValidationError("order_id is required")
    hold_seconds = _integer(hold_seconds, "hold_seconds", minimum=1)
    if hold_seconds > _MAX_HOLD_SECONDS:
        raise GoodsValidationError("hold_seconds exceeds the one-day safety bound")
    timestamp = _timestamp(now)
    expires_at = timestamp + hold_seconds

    with _database_connection(conn) as db, _atomic(db):
        order = db.execute(
            """
            SELECT o.actor_tg_id,o.purpose,o.status,q.product_id
            FROM payment_orders o JOIN quotes q ON q.quote_id=o.quote_id
            WHERE o.order_id=?
            """,
            (order_id,),
        ).fetchone()
        if (
            order is None
            or order["purpose"] != "digital_product_buy"
            or order["product_id"] != product_id
            or order["status"] not in {"accepted", "completed"}
        ):
            raise GoodsValidationError("order is not an accepted purchase for this product")
        product = db.execute(
            "SELECT delivery_type,is_active FROM digital_products WHERE id=?",
            (product_id,),
        ).fetchone()
        if product is None or not product["is_active"] or product["delivery_type"] != "auto_inventory":
            raise GoodsValidationError("product does not support automatic inventory")
        db.execute(
            """
            UPDATE digital_inventory
            SET status='available',order_id=NULL,sold_to_tg_id=NULL,reservation_until=NULL
            WHERE product_id=? AND status='reserved'
              AND reservation_until IS NOT NULL AND reservation_until<=?
              AND exposed_at IS NULL
            """,
            (product_id, timestamp),
        )
        existing = db.execute(
            """
            SELECT id,order_id,reservation_until FROM digital_inventory
            WHERE product_id=? AND order_id=? AND status='reserved'
            """,
            (product_id, order_id),
        ).fetchone()
        if existing is not None:
            return {
                "inventory_id": int(existing["id"]),
                "order_id": order_id,
                "reservation_until": int(existing["reservation_until"]),
                "replayed": True,
            }
        item = db.execute(
            """
            SELECT id FROM digital_inventory
            WHERE product_id=? AND status='available'
            ORDER BY id LIMIT 1
            """,
            (product_id,),
        ).fetchone()
        if item is None:
            return None
        cursor = db.execute(
            """
            UPDATE digital_inventory
            SET status='reserved',order_id=?,sold_to_tg_id=?,reservation_until=?
            WHERE id=? AND status='available'
            """,
            (order_id, int(order["actor_tg_id"]), expires_at, int(item["id"])),
        )
        if cursor.rowcount != 1:
            raise InventoryUnavailable("inventory changed while reserving")
        return {
            "inventory_id": int(item["id"]),
            "order_id": order_id,
            "reservation_until": expires_at,
            "replayed": False,
        }


def release_inventory(
    order_id: str,
    *,
    now: int | None = None,
    conn=None,
) -> bool:
    """Release an unexposed reservation; never release an exposed fulfillment."""
    del now
    order_id = str(order_id).strip()
    if not order_id:
        raise GoodsValidationError("order_id is required")
    with _database_connection(conn) as db, _atomic(db):
        fulfillment = db.execute(
            "SELECT state,exposed_at FROM goods_fulfillments WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if fulfillment is not None and (
            fulfillment["exposed_at"] is not None
            or fulfillment["state"] in {"ready", "delivered", "exception"}
        ):
            return False
        cursor = db.execute(
            """
            UPDATE digital_inventory
            SET status='available',order_id=NULL,sold_to_tg_id=NULL,reservation_until=NULL
            WHERE order_id=? AND status='reserved' AND exposed_at IS NULL
            """,
            (order_id,),
        )
        return cursor.rowcount > 0


def mark_returned(
    inventory_id: int,
    *,
    reason: str,
    actor_id: int = 0,
    actor_name: str = "system",
    now: int | None = None,
    conn=None,
) -> dict[str, Any]:
    """Mark an exposed sold item defective and append an encrypted audit record."""
    inventory_id = _integer(inventory_id, "inventory_id", minimum=1)
    timestamp = _timestamp(now)
    reason = str(reason).strip()
    if not reason or len(reason) > 1000:
        raise GoodsValidationError("return reason is required and must be at most 1000 characters")
    actor_id = _integer(actor_id, "actor_id")
    actor_name = str(actor_name).strip()[:120] or "system"
    with _database_connection(conn) as db, _atomic(db):
        row = db.execute(
            """
            SELECT i.id,i.product_id,i.order_id,i.status,i.exposed_at
            FROM digital_inventory i WHERE i.id=?
            """,
            (inventory_id,),
        ).fetchone()
        if row is None or row["status"] != "sold" or row["exposed_at"] is None:
            raise GoodsReturnError("only an exposed sold inventory item can be returned")
        cursor = db.execute(
            "UPDATE digital_inventory SET status='defective' WHERE id=? AND status='sold'",
            (inventory_id,),
        )
        if cursor.rowcount != 1:
            raise GoodsReturnError("inventory return lost a concurrent update")
        from crypto_store import encrypt_secret
        audit_cipher = encrypt_secret(
            json.dumps(
                {
                    "inventory_id": inventory_id,
                    "product_id": int(row["product_id"]),
                    "order_id": row["order_id"],
                    "reason": reason,
                    "returned_at": timestamp,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            _COLUMNS_LABEL,
        )
        db.execute(
            """
            INSERT INTO audit_logs(
                action,actor_id,actor_name,details_cipher,result_status
            ) VALUES('goods_return_defective',?,?,?,'success')
            """,
            (actor_id, actor_name, audit_cipher),
        )
        return {
            "inventory_id": inventory_id,
            "product_id": int(row["product_id"]),
            "order_id": row["order_id"],
            "status": "defective",
            "returned_at": timestamp,
        }


def low_stock_events(*, conn=None) -> list[dict[str, Any]]:
    """Return deterministic low-stock events for active auto-inventory products."""
    with _database_connection(conn) as db:
        rows = db.execute(
            """
            SELECT p.id AS product_id,p.product_code,p.title,
                   p.stock_warning_threshold AS threshold,
                   COUNT(i.id) AS available_count
            FROM digital_products p
            LEFT JOIN digital_inventory i
              ON i.product_id=p.id AND i.status='available'
            WHERE p.is_active=1 AND p.delivery_type='auto_inventory'
            GROUP BY p.id,p.product_code,p.title,p.stock_warning_threshold
            HAVING COUNT(i.id)<p.stock_warning_threshold
            ORDER BY p.id
            """
        ).fetchall()
        return [
            {
                "event": "low_stock",
                "product_id": int(row["product_id"]),
                "product_code": row["product_code"],
                "title": row["title"],
                "available_count": int(row["available_count"]),
                "threshold": int(row["threshold"]),
            }
            for row in rows
        ]
