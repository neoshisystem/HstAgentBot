"""Atomic quote-to-order creation and unpaid-order expiry for HstAgentBot v5.2."""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from typing import Iterator

from capacity_service import reserve_capacity, release_capacity
from financial_service import available_balance, FinancialError, InsufficientFunds
from promotion_service import hold_coupon, finalize_coupon
from quote_service import load_quote_for_actor, QuoteError, QuoteExpired


_ALLOWED_RAILS = frozenset({"wallet", "card_to_card", "exempt"})
_PANEL_PURPOSES = frozenset({"vpn_buy"})


class OrderError(ValueError):
    """An order cannot be created or expired under the current immutable quote."""


class OrderConflict(OrderError):
    """An idempotency key or quote already belongs to a different order."""


class OrderCapacityError(OrderError):
    """The quote has no safe, fresh capacity snapshot."""


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if conn.in_transaction:
        conn.execute("SAVEPOINT w08_order")
        try:
            yield
            conn.execute("RELEASE SAVEPOINT w08_order")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT w08_order")
            conn.execute("RELEASE SAVEPOINT w08_order")
            raise
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


def _positive_id(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OrderError(f"{label} must be a positive integer")
    return value


def _merchant_rep_id(merchant_key: str) -> int | None:
    if merchant_key == "root":
        return None
    match = re.fullmatch(r"rep:([1-9][0-9]*)", merchant_key)
    if match is None:
        raise OrderError("merchant key is invalid")
    return int(match.group(1))


def _validate_merchant(conn: sqlite3.Connection, merchant_key: str) -> int | None:
    rep_id = _merchant_rep_id(merchant_key)
    if rep_id is None:
        return None
    row = conn.execute(
        "SELECT is_active,deleted_at FROM representatives WHERE id=?",
        (rep_id,),
    ).fetchone()
    if row is None or not bool(row[0]) or row[1] is not None:
        raise OrderError("merchant representative is inactive or unavailable")
    return rep_id


def _validate_account(
    conn: sqlite3.Connection, quote: Mapping[str, object], actor_tg_id: int,
    merchant_key: str,
) -> None:
    account_id = quote.get("account_id")
    purpose = str(quote.get("purpose") or "")
    if purpose in {"vpn_renew", "vpn_addon"} and account_id is None:
        raise OrderError("renewal/add-on quote requires an account")
    if account_id is None:
        return
    row = conn.execute(
        "SELECT owner_tg_id,rep_id,seller_key,deleted_at,enable FROM accounts WHERE id=?",
        (int(account_id),),
    ).fetchone()
    if row is None or row[3] is not None or not bool(row[4]):
        raise OrderError("quoted account is not active")
    if row[0] is not None and int(row[0]) != actor_tg_id:
        grant = conn.execute(
            "SELECT 1 FROM account_access_grants WHERE account_id=? AND grantee_tg_id=? "
            "AND revoked_at IS NULL AND access_level IN ('viewer','manager')",
            (int(account_id), actor_tg_id),
        ).fetchone()
        if grant is None:
            raise OrderError("actor does not have access to the quoted account")
    if str(row[2] or "root") != merchant_key:
        raise OrderError("quoted account merchant changed")


def _quote_holds(quote: Mapping[str, object]) -> dict[str, int]:
    edges = quote.get("price_edges")
    if not isinstance(edges, list):
        raise OrderError("quote price edges are invalid")
    result: dict[str, int] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            raise OrderError("quote price edge is invalid")
        source = edge.get("source_key")
        amount = edge.get("price_toman")
        if not isinstance(source, str) or not source:
            raise OrderError("quote price edge has no financial source")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise OrderError("quote edge amount is invalid")
        if amount:
            result[source] = result.get(source, 0) + amount
    return result


def _reserve_wallet_holds(
    conn: sqlite3.Connection, *, order_id: str, holds: Mapping[str, int],
    now: int, expires_at: int,
) -> list[str]:
    inserted: list[str] = []
    for source_key, amount in sorted(holds.items()):
        available = available_balance(conn, source_key)
        if available is not None and amount > available:
            raise InsufficientFunds(
                f"insufficient funds for {source_key}: requested={amount}, available={available}"
            )
        hold_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO financial_holds(hold_id,order_id,source_key,amount_toman,state,"
            "created_at,expires_at,captured_ledger_id) VALUES(?,?,?,?,'active',?,?,NULL)",
            (hold_id, order_id, source_key, amount, now, expires_at),
        )
        inserted.append(hold_id)
    return inserted


def _coupon_code(spec: Mapping[str, object]) -> str | None:
    coupon = spec.get("coupon")
    if coupon is None:
        return None
    if isinstance(coupon, str):
        code = coupon.strip()
    elif isinstance(coupon, Mapping):
        code = str(coupon.get("code") or "").strip()
    else:
        raise OrderError("quote coupon snapshot is invalid")
    if not code:
        raise OrderError("quote coupon code is empty")
    return code


def create_order(
    conn: sqlite3.Connection,
    *,
    quote_id: str,
    actor_tg_id: int,
    bot_id: int,
    payment_rail: str,
    now: int,
    capacity_snapshot_count: int | None = None,
    capacity_snapshot_at: int | None = None,
) -> dict:
    """Create one order, its financial/capacity holds and coupon reservation atomically."""
    actor_tg_id = _positive_id(actor_tg_id, "actor_tg_id")
    bot_id = _positive_id(bot_id, "bot_id")
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise OrderError("now must be a non-negative UTC epoch-seconds integer")
    rail = str(payment_rail or "").strip().lower()
    if rail not in _ALLOWED_RAILS:
        raise OrderError("payment rail is not enabled in W08-B01")

    with _atomic(conn):
        prior = conn.execute(
            "SELECT order_id,actor_tg_id,bot_id,merchant_key,purpose,payment_rail,"
            "amount_toman,status,received_toman,created_at,expires_at,revision "
            "FROM payment_orders WHERE quote_id=?",
            (quote_id,),
        ).fetchone()
        if prior is not None:
            if (
                int(prior[1]) != actor_tg_id or int(prior[2]) != bot_id
                or str(prior[5]) != rail
            ):
                raise OrderConflict("quote is already bound to a different order request")
            return {
                "order_id": str(prior[0]), "quote_id": quote_id,
                "merchant_key": str(prior[3]), "purpose": str(prior[4]),
                "payment_rail": str(prior[5]), "amount_toman": int(prior[6]),
                "status": str(prior[7]), "received_toman": int(prior[8]),
                "created_at": int(prior[9]), "expires_at": int(prior[10]),
                "revision": int(prior[11]), "replayed": True,
            }

        quote = load_quote_for_actor(conn, quote_id, actor_tg_id, bot_id, now=now)
        merchant_key = str(quote["merchant_key"])
        merchant_rep_id = _validate_merchant(conn, merchant_key)
        _validate_account(conn, quote, actor_tg_id, merchant_key)

        amount = int(quote["amount_toman"])
        purpose = str(quote["purpose"])
        if rail == "exempt" and amount != 0:
            raise OrderError("exempt rail requires a zero-priced quote")
        if rail in {"wallet", "card_to_card"} and amount <= 0:
            raise OrderError("paid rails require a positive quote amount")

        # A card/payment order is not a wallet debit. Wallet rails reserve every
        # source in the immutable price-edge chain under this same BEGIN IMMEDIATE.
        holds = _quote_holds(quote) if rail == "wallet" else {}
        if rail == "wallet" and amount > 0 and not holds:
            raise OrderError("wallet quote has no immutable financial hold sources")

        order_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO payment_orders(order_id,quote_id,actor_tg_id,bot_id,merchant_key,"
            "purpose,payment_rail,amount_toman,status,received_toman,created_at,expires_at,revision) "
            "VALUES(?,?,?,?,?,?,?,?, 'awaiting_payment',0,?,?,1)",
            (
                order_id, quote_id, actor_tg_id, bot_id, merchant_key, purpose, rail,
                amount, now, int(quote["expires_at"]),
            ),
        )

        hold_ids = _reserve_wallet_holds(
            conn, order_id=order_id, holds=holds, now=now,
            expires_at=int(quote["expires_at"]),
        ) if holds else []

        capacity_result = None
        if purpose in _PANEL_PURPOSES:
            panel_id = quote.get("panel_id")
            if panel_id is None:
                raise OrderCapacityError("VPN purchase quote has no panel binding")
            spec = quote.get("spec")
            panel_spec = spec.get("panel", {}) if isinstance(spec, Mapping) else {}
            snapshot_count = capacity_snapshot_count
            snapshot_at = capacity_snapshot_at
            if snapshot_count is None and isinstance(panel_spec, Mapping):
                snapshot_count = panel_spec.get("active_count", panel_spec.get("snapshot_count"))
            if snapshot_at is None and isinstance(panel_spec, Mapping):
                snapshot_at = panel_spec.get("snapshot_at")
            if snapshot_count is None or snapshot_at is None:
                raise OrderCapacityError("fresh panel capacity snapshot is required")
            capacity_result = reserve_capacity(
                conn,
                reservation_id=f"cap:{order_id}",
                order_id=order_id,
                panel_id=int(panel_id),
                snapshot_count=snapshot_count,
                snapshot_at=snapshot_at,
                now=now,
                account_id=int(quote["account_id"]) if quote.get("account_id") is not None else None,
            )

        coupon = _coupon_code(quote.get("spec", {}))
        coupon_result = None
        if coupon is not None:
            coupon_result = hold_coupon(
                conn, order_id=order_id, code=coupon, price_toman=amount, now=now
            )

        return {
            "order_id": order_id, "quote_id": quote_id, "merchant_key": merchant_key,
            "purpose": purpose, "payment_rail": rail, "amount_toman": amount,
            "status": "awaiting_payment", "received_toman": 0,
            "created_at": now, "expires_at": int(quote["expires_at"]),
            "revision": 1, "replayed": False, "hold_ids": hold_ids,
            "capacity": capacity_result, "coupon": coupon_result,
            "merchant_rep_id": merchant_rep_id,
        }


def release_order_resources(
    conn: sqlite3.Connection, *, order_id: str, now: int,
) -> dict:
    """Release only unpaid-order reservations; caller owns the transaction."""
    holds = conn.execute(
        "UPDATE financial_holds SET state='released' "
        "WHERE order_id=? AND state='active'",
        (order_id,),
    ).rowcount
    reservation = conn.execute(
        "SELECT reservation_id FROM capacity_reservations "
        "WHERE order_id=? AND state='active'",
        (order_id,),
    ).fetchone()
    capacity_released = False
    if reservation is not None:
        capacity_released = release_capacity(
            conn, str(reservation[0]), outcome="cancelled"
        )
    coupon = conn.execute(
        "SELECT state FROM coupon_uses WHERE order_id=?",
        (order_id,),
    ).fetchone()
    coupon_released = False
    if coupon is not None and str(coupon[0]) == "held":
        finalize_coupon(conn, order_id=order_id, success=False)
        coupon_released = True
    return {
        "holds_released": int(holds),
        "capacity_released": capacity_released,
        "coupon_released": coupon_released,
    }


def expire_unpaid_orders(
    conn: sqlite3.Connection, *, now: int,
) -> dict:
    """Expire awaiting_payment orders only; pending_review orders keep their resources."""
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise OrderError("now must be a non-negative UTC epoch-seconds integer")
    expired: list[str] = []
    retained_pending_review: list[str] = []
    with _atomic(conn):
        rows = conn.execute(
            "SELECT order_id,status FROM payment_orders "
            "WHERE expires_at<=? AND status IN ('awaiting_payment','pending_review') "
            "ORDER BY order_id",
            (now,),
        ).fetchall()
        for row in rows:
            order_id, status = str(row[0]), str(row[1])
            if status == "pending_review":
                retained_pending_review.append(order_id)
                continue
            cur = conn.execute(
                "UPDATE payment_orders SET status='expired',revision=revision+1 "
                "WHERE order_id=? AND status='awaiting_payment' AND expires_at<=?",
                (order_id, now),
            )
            if cur.rowcount == 1:
                release_order_resources(conn, order_id=order_id, now=now)
                expired.append(order_id)
        return {
            "expired_order_ids": expired,
            "retained_pending_review_ids": retained_pending_review,
            "expired_count": len(expired),
        }


__all__ = [
    "OrderError", "OrderConflict", "OrderCapacityError",
    "create_order", "expire_unpaid_orders", "release_order_resources",
]
