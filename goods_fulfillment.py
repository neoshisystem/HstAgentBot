"""Idempotent automatic and manual digital-goods fulfillment services.

All database exposure and claim transitions are committed before an optional
injected delivery callback runs. A timeout is never treated as proof that a
payload was not delivered and cannot put an exposed item back into stock.
"""
from __future__ import annotations

import json
import secrets

from crypto_store import decrypt_secret, encrypt_secret
from goods_service import (
    GoodsValidationError,
    _atomic,
    _database_connection,
    _timestamp,
)

_COLUMNS_LABEL = "hstagent/columns/v1"
_PAID_STATES = frozenset({"accepted", "completed"})


class FulfillmentError(ValueError):
    """Raised when an order cannot be fulfilled safely."""


class ManualClaimConflict(FulfillmentError):
    """Raised when a manual claim is absent, expired, or owned by another operator."""


def _order_product(db, order_id: str) -> dict:
    row = db.execute(
        """
        SELECT o.order_id,o.actor_tg_id,o.purpose,o.status,
               q.product_id,p.delivery_type,p.file_telegram_id,p.file_bot_id
        FROM payment_orders o
        JOIN quotes q ON q.quote_id=o.quote_id
        JOIN digital_products p ON p.id=q.product_id
        WHERE o.order_id=?
        """,
        (order_id,),
    ).fetchone()
    if row is None:
        raise FulfillmentError("digital-goods order or product does not exist")
    result = dict(row)
    if result["purpose"] != "digital_product_buy" or result["status"] not in _PAID_STATES:
        raise FulfillmentError("order is not in a paid, fulfillable state")
    return result


def _decrypt_payload(cipher: str, *, as_json: bool = False):
    try:
        plaintext = decrypt_secret(cipher, _COLUMNS_LABEL)
        return json.loads(plaintext) if as_json else plaintext
    except Exception as exc:
        raise FulfillmentError("stored fulfillment payload could not be authenticated") from exc


def _dispatch(order_id: str, payload, sender, state: str) -> dict:
    """Invoke an injected sender only after exposure has been durably committed."""
    if sender is None or state == "delivered":
        return {
            "order_id": order_id,
            "state": state,
            "payload": payload,
            "replayed": state in {"ready", "exception", "delivered"},
        }
    try:
        result = sender(payload)
    except Exception:
        with _database_connection() as db, _atomic(db):
            db.execute(
                """
                UPDATE goods_fulfillments SET state='exception',revision=revision+1
                WHERE order_id=? AND state IN('ready','exception')
                """,
                (order_id,),
            )
        raise
    with _database_connection() as db, _atomic(db):
        db.execute(
            """
            UPDATE goods_fulfillments SET state='delivered',revision=revision+1
            WHERE order_id=? AND state IN('ready','exception')
            """,
            (order_id,),
        )
    return {
        "order_id": order_id,
        "state": "delivered",
        "payload": payload,
        "delivery_result": result,
        "replayed": state in {"ready", "exception", "delivered"},
    }


def _prepare_inventory(order_id: str, now: int):
    """Expose one reserved item, or recover an already exposed payload."""
    with _database_connection() as db, _atomic(db):
        order = _order_product(db, order_id)
        if order["delivery_type"] != "auto_inventory":
            raise FulfillmentError("order is not an automatic-inventory product")
        existing = db.execute(
            "SELECT state,payload_cipher FROM goods_fulfillments WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if existing is not None:
            if not existing["payload_cipher"]:
                raise FulfillmentError("fulfillment exists without an authenticated payload")
            return (
                _decrypt_payload(existing["payload_cipher"]),
                str(existing["state"]),
                True,
            )

        inventory = db.execute(
            """
            SELECT id,secret_cipher FROM digital_inventory
            WHERE order_id=? AND product_id=? AND status='reserved'
              AND reservation_until IS NOT NULL AND reservation_until>?
              AND exposed_at IS NULL
            """,
            (order_id, int(order["product_id"]), now),
        ).fetchone()
        if inventory is None:
            raise FulfillmentError("order has no live reserved inventory item")
        payload = decrypt_secret(inventory["secret_cipher"], _COLUMNS_LABEL)
        payload_cipher = encrypt_secret(payload, _COLUMNS_LABEL)
        cursor = db.execute(
            """
            UPDATE digital_inventory
            SET status='sold',reservation_until=NULL,sold_at=?,exposed_at=?
            WHERE id=? AND order_id=? AND status='reserved' AND exposed_at IS NULL
            """,
            (now, now, int(inventory["id"]), order_id),
        )
        if cursor.rowcount != 1:
            raise FulfillmentError("inventory exposure lost a concurrent update")
        db.execute(
            """
            INSERT INTO goods_fulfillments(
                order_id,state,inventory_id,payload_cipher,exposed_at,revision
            ) VALUES(?,'ready',?,?,?,1)
            """,
            (order_id, int(inventory["id"]), payload_cipher, now),
        )
        return payload, "ready", False


def fulfill_inventory(order_id: str, *, sender=None, now: int | None = None) -> dict:
    """Expose one reserved code exactly once; timeout leaves it sold and replayable."""
    order_id = str(order_id).strip()
    if not order_id:
        raise GoodsValidationError("order_id is required")
    timestamp = _timestamp(now)
    payload, state, replayed = _prepare_inventory(order_id, timestamp)
    if sender is None:
        return {
            "order_id": order_id,
            "state": state,
            "payload": payload,
            "replayed": replayed,
        }
    result = _dispatch(order_id, payload, sender, state)
    result["replayed"] = replayed
    return result


def _prepare_file(order_id: str, now: int):
    with _database_connection() as db, _atomic(db):
        order = _order_product(db, order_id)
        if order["delivery_type"] != "file_document":
            raise FulfillmentError("order is not a file-document product")
        existing = db.execute(
            "SELECT state,payload_cipher FROM goods_fulfillments WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if existing is not None:
            if not existing["payload_cipher"]:
                raise FulfillmentError("fulfillment exists without an authenticated file payload")
            return _decrypt_payload(existing["payload_cipher"], as_json=True), str(existing["state"]), True
        file_id = order["file_telegram_id"]
        bot_id = order["file_bot_id"]
        if not isinstance(file_id, str) or not file_id.strip() or bot_id is None:
            raise FulfillmentError("file product requires a Telegram file id and registered bot id")
        payload = {
            "file_telegram_id": file_id,
            "file_bot_id": int(bot_id),
            "order_id": order_id,
        }
        cipher = encrypt_secret(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            _COLUMNS_LABEL,
        )
        db.execute(
            """
            INSERT INTO goods_fulfillments(
                order_id,state,payload_cipher,exposed_at,revision
            ) VALUES(?,'ready',?,?,1)
            """,
            (order_id, cipher, now),
        )
        return payload, "ready", False


def fulfill_file(order_id: str, *, sender=None, now: int | None = None) -> dict:
    """Prepare or deliver the configured file via an optional injected sender."""
    order_id = str(order_id).strip()
    if not order_id:
        raise GoodsValidationError("order_id is required")
    timestamp = _timestamp(now)
    payload, state, replayed = _prepare_file(order_id, timestamp)
    if sender is None:
        return {
            "order_id": order_id,
            "state": state,
            "payload": payload,
            "replayed": replayed,
        }
    result = _dispatch(order_id, payload, sender, state)
    result["replayed"] = replayed
    return result


def claim_manual_order(
    order_id: str,
    operator_id: int,
    *,
    lease_seconds: int = 300,
    now: int | None = None,
) -> dict | None:
    """Acquire a single-operator expiring claim for a manual-fulfillment order."""
    order_id = str(order_id).strip()
    if not order_id:
        raise GoodsValidationError("order_id is required")
    if isinstance(operator_id, bool) or not isinstance(operator_id, int) or operator_id <= 0:
        raise GoodsValidationError("operator_id must be a positive integer")
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or not 1 <= lease_seconds <= 3600
    ):
        raise GoodsValidationError("lease_seconds must be between 1 and 3600")
    timestamp = _timestamp(now)
    token = f"{operator_id}.{secrets.token_urlsafe(24)}"
    with _database_connection() as db, _atomic(db):
        order = _order_product(db, order_id)
        if order["delivery_type"] != "manual_fulfill":
            raise FulfillmentError("order is not a manual-fulfillment product")
        db.execute(
            """
            INSERT OR IGNORE INTO goods_fulfillments(order_id,state,revision)
            VALUES(?,'awaiting_manual',1)
            """,
            (order_id,),
        )
        cursor = db.execute(
            """
            UPDATE goods_fulfillments
            SET state='preparing',claim_token=?,lease_until=?,revision=revision+1
            WHERE order_id=? AND (
                state='awaiting_manual'
                OR (state='preparing' AND lease_until IS NOT NULL AND lease_until<=?)
            )
            """,
            (token, timestamp + lease_seconds, order_id, timestamp),
        )
        if cursor.rowcount != 1:
            return None
        return {
            "order_id": order_id,
            "operator_id": operator_id,
            "claim_token": token,
            "lease_until": timestamp + lease_seconds,
            "state": "preparing",
        }


def complete_manual_order(
    order_id: str,
    operator_id: int,
    payload: str,
    *,
    claim_token: str,
    now: int | None = None,
) -> dict:
    """Complete only the current unexpired operator claim and encrypt its payload."""
    order_id = str(order_id).strip()
    payload = str(payload)
    if not order_id or not payload.strip():
        raise GoodsValidationError("order_id and non-empty manual payload are required")
    if isinstance(operator_id, bool) or not isinstance(operator_id, int) or operator_id <= 0:
        raise GoodsValidationError("operator_id must be a positive integer")
    if not isinstance(claim_token, str) or not claim_token.startswith(f"{operator_id}."):
        raise ManualClaimConflict("claim token does not belong to this operator")
    timestamp = _timestamp(now)
    with _database_connection() as db, _atomic(db):
        order = _order_product(db, order_id)
        if order["delivery_type"] != "manual_fulfill":
            raise FulfillmentError("order is not a manual-fulfillment product")
        current = db.execute(
            """
            SELECT state,claim_token,lease_until,payload_cipher
            FROM goods_fulfillments WHERE order_id=?
            """,
            (order_id,),
        ).fetchone()
        if current is None:
            raise ManualClaimConflict("manual order has not been claimed")
        if current["state"] == "ready" and current["payload_cipher"]:
            stored = _decrypt_payload(current["payload_cipher"])
            if secrets.compare_digest(stored, payload):
                return {"order_id": order_id, "state": "ready", "replayed": True}
            raise ManualClaimConflict("manual order is already completed with a different payload")
        if (
            current["state"] != "preparing"
            or current["claim_token"] != claim_token
            or current["lease_until"] is None
            or int(current["lease_until"]) <= timestamp
        ):
            raise ManualClaimConflict("manual claim is not current or has expired")
        cipher = encrypt_secret(payload, _COLUMNS_LABEL)
        db.execute(
            """
            UPDATE goods_fulfillments
            SET state='ready',payload_cipher=?,claim_token=NULL,lease_until=NULL,
                exposed_at=?,revision=revision+1
            WHERE order_id=? AND state='preparing' AND claim_token=?
            """,
            (cipher, timestamp, order_id, claim_token),
        )
        return {"order_id": order_id, "state": "ready", "replayed": False}
